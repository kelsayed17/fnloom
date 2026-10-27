"""Result stores for the cache: in memory and on disk.

A store maps a 32-byte key to a node's result. It implements `CacheStore`: `get`
returns the value or `MISSING`, `put` stores one, `discard` forgets one. Both stores
here are safe to share between threads, and between drivers in one process.

A store never fails a run. A corrupt entry, a value that cannot be serialized, or a
disk that cannot be written is a miss or a skipped write, reported with a
`CacheWarning`. An entry written by another interpreter, serializer or library
version is an expected miss, not reported.

`DiskCache` stores values with pickle by default. Loading a pickle can run any code
the pickle names, so keep the cache directory as private as the code itself: whoever
can write to it can run code in every process that reads it.
"""

import contextlib
import enum
import json
import os
import pickle
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import warnings
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, override

from fnloom._codeversion import distribution_version

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "MISSING",
    "CacheStore",
    "CacheWarning",
    "DiskCache",
    "MemoryCache",
    "Missing",
]


class Missing(enum.Enum):
    """The type of `MISSING`, so a type checker narrows on `value is MISSING`."""

    MISSING = enum.auto()

    @override
    def __repr__(self) -> str:
        """Reads as the name it is exported under."""
        return "fnloom.MISSING"


MISSING: Final = Missing.MISSING
"""What `CacheStore.get` returns for a key it does not hold."""


class CacheWarning(UserWarning):
    """A cache store failed to read or write an entry; the run went on without it."""


class CacheStore(Protocol):
    """Where cached results live: any object with these three methods.

    A store must be safe to call from several threads at once. It must never raise
    for a store error: it returns `MISSING` or skips the write, and warns with
    `CacheWarning`.
    """

    def get(self, key: bytes) -> object:
        """The value stored under `key`, or `MISSING`."""
        ...

    def put(self, key: bytes, value: object) -> None:
        """Stores `value` under `key`, replacing any value already there."""
        ...

    def discard(self, key: bytes) -> None:
        """Forgets `key`, if the store holds it."""
        ...


def _warn(message: str, depth: int = 1) -> None:
    """Reports a store error at the caller of the public store method.

    Args:
        message: What failed.
        depth: How many store frames lie between the public method and this call:
            1 when the public method calls `_warn` itself.
    """
    warnings.warn(message, CacheWarning, stacklevel=depth + 2)


class MemoryCache:
    """Results held in memory, least recently used evicted first.

    A hit returns the stored object itself, not a copy, so a cached node must not
    mutate its arguments: a change would reach every later run that hits.

    Args:
        max_entries: The most entries held at once.
        max_bytes: The most bytes held at once, or None for no bound. Each value is
            counted by `sys.getsizeof` of the object itself, not of what it refers
            to, so this is an approximation: a list of large arrays counts as the
            list.
    """

    def __init__(
        self, max_entries: int = 100_000, max_bytes: int | None = None
    ) -> None:
        """Makes an empty store with these bounds."""
        if max_entries < 1:
            msg = f"max_entries must be at least 1, not {max_entries}"
            raise ValueError(msg)
        if max_bytes is not None and max_bytes < 1:
            msg = f"max_bytes must be at least 1 or None, not {max_bytes}"
            raise ValueError(msg)
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[bytes, tuple[object, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: bytes) -> object:
        """The value stored under `key`, the same object `put` stored, or `MISSING`."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return MISSING
            self._entries.move_to_end(key)
        return entry[0]

    def put(self, key: bytes, value: object) -> None:
        """Stores `value` under `key`, evicting the least recently used past a bound."""
        size = 0
        if self._max_bytes is not None:
            try:
                size = sys.getsizeof(value)
            except Exception as error:  # noqa: BLE001  # any failure skips the write
                _warn(f"cannot size a {type(value).__qualname__} to cache it: {error}")
                return
        # Evicted values stay referenced by this list until the method returns, after
        # the lock is released, in case releasing one runs code that uses the store.
        evicted: list[tuple[object, int]] = []
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
                evicted.append(old)
            # A value over the bound on its own is not stored, and evicts nothing.
            if self._max_bytes is not None and size > self._max_bytes:
                return
            self._entries[key] = (value, size)
            self._bytes += size
            while len(self._entries) > self._max_entries or (
                self._max_bytes is not None and self._bytes > self._max_bytes
            ):
                _, entry = self._entries.popitem(last=False)
                self._bytes -= entry[1]
                evicted.append(entry)

    def discard(self, key: bytes) -> None:
        """Forgets `key`, if the store holds it."""
        # The value is released on return, after the lock, as in `put`.
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._bytes -= entry[1]


# The entry format: a prefix of the magic, the format version, the header's and the
# payload's lengths and a CRC-32 of both; then the JSON header; then the payload.
_MAGIC = b"FNLC"
_FORMAT = 1
_PREFIX = struct.Struct(">4sHIQI")
# Where entries live under the store's path; a new format gets a new directory.
_LAYOUT = f"v{_FORMAT}"
# An entry's file name: its key in lowercase hex.
_ENTRY_NAME = re.compile(r"[0-9a-f]+")
# A temporary file being written, named by `put`.
_TEMPORARY_NAME = re.compile(r"\..*\.tmp")
# A temporary file older than this was left by a writer that died; prune removes it.
_STALE_TEMPORARY_NS = 3600 * 10**9
# Writes between the prunes of a bounded store.
_PRUNE_EVERY = 100


class _CorruptEntryError(ValueError):
    """An entry that cannot be read back: damaged, or not in this format."""


class _OtherVersionError(Exception):
    """A whole entry written by another interpreter, serializer or library version."""


def _pack(header: bytes, payload: bytes) -> bytes:
    """An entry's bytes: the prefix, then `header` and `payload`."""
    checksum = zlib.crc32(payload, zlib.crc32(header))
    prefix = _PREFIX.pack(_MAGIC, _FORMAT, len(header), len(payload), checksum)
    return prefix + header + payload


def _unpack(data: bytes) -> tuple[bytes, bytes]:
    """An entry's header and payload, checked against its prefix."""
    if len(data) < _PREFIX.size:
        msg = "the entry is shorter than its prefix"
        raise _CorruptEntryError(msg)
    magic, version, header_length, payload_length, checksum = _PREFIX.unpack_from(data)
    if magic != _MAGIC or version != _FORMAT:
        msg = "the entry is not in this cache's format"
        raise _CorruptEntryError(msg)
    if len(data) != _PREFIX.size + header_length + payload_length:
        msg = "the entry's length does not match its prefix"
        raise _CorruptEntryError(msg)
    header = data[_PREFIX.size : _PREFIX.size + header_length]
    payload = data[_PREFIX.size + header_length :]
    if zlib.crc32(payload, zlib.crc32(header)) != checksum:
        msg = "the entry's checksum does not match"
        raise _CorruptEntryError(msg)
    return header, payload


def _libraries(value: object) -> dict[str, str | None]:
    """The value's type's top-level module and its distribution version.

    None for the standard library and builtins, whose version is the interpreter's,
    recorded apart.
    """
    module = type(value).__module__.partition(".")[0]
    return {module: distribution_version(module)}


class DiskCache:
    """Results stored as files, one per key, under a directory.

    An entry lives at `<path>/v1/<first two hex digits of the key>/<key in hex>`. It
    starts with a header naming the format version, the serializer, the Python
    version, and the version of the library that defines the value's type. An entry
    whose header does not match the running interpreter is a miss. Writes go to a
    temporary file that then replaces the entry, so a reader sees a whole entry or
    none, and of two writers of one key the last one wins.

    The default serializer is pickle, and loading a pickle can run code: keep the
    directory as private as the code that uses it.

    Args:
        path: The directory, created on the first write.
        max_bytes: The most bytes of entries kept, or None for no bound. When the
            store opens and after every 100 writes, the least recently read
            entries are removed until the rest fit.
        dumps: Turns a value into bytes.
        loads: Turns those bytes back into the value.
        serializer: The name of the `dumps`/`loads` pair, recorded in every entry.
            Entries written under another name are misses, so give a new name
            whenever the pair changes.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int | None = None,
        dumps: Callable[[object], bytes] = pickle.dumps,
        loads: Callable[[bytes], object] = pickle.loads,
        serializer: str = "pickle",
    ) -> None:
        """Opens the store at `path`, pruning it to `max_bytes` if given.

        An existing directory that other users can write is reported with a
        `CacheWarning`: whoever can write it can run code in every reader.

        Raises:
            ValueError: `max_bytes` is negative, or `path` holds a NUL byte.
            TypeError: `serializer` is not a non-empty `str`: it names the pair, it
                is not a module (pass `dumps` and `loads` for that).
        """
        if max_bytes is not None and max_bytes < 0:
            msg = f"max_bytes must be at least 0 or None, not {max_bytes}"
            raise ValueError(msg)
        if not isinstance(serializer, str) or not serializer:
            msg = (
                "serializer must be a non-empty str naming dumps and loads, "
                f"not {serializer!r}"
            )
            raise TypeError(msg)
        if "\x00" in os.fspath(path):
            msg = f"a cache path cannot hold a NUL byte: {os.fspath(path)!r}"
            raise ValueError(msg)
        self._path = Path(path)
        self._root = self._path / _LAYOUT
        _warn_if_shared(self._path)
        self._max_bytes = max_bytes
        self._dumps = dumps
        self._loads = loads
        self._serializer = serializer
        self._writes = 0
        self._lock = threading.Lock()
        if max_bytes is not None:
            self._prune_quietly(max_bytes)

    @property
    def path(self) -> Path:
        """The store's directory."""
        return self._path

    def _file(self, key: bytes) -> Path:
        """Where `key`'s entry lives."""
        name = key.hex()
        return self._root / name[:2] / name

    def get(self, key: bytes) -> object:
        """The value stored under `key`, or `MISSING`.

        An entry written by another Python, serializer or library version is a
        quiet miss, left for the next `put` to replace. An entry that cannot be read
        back is a miss too: it is removed, with a warning.
        """
        path = self._file(key)
        try:
            with path.open("rb") as file:
                read = os.fstat(file.fileno())
                data = file.read()
        except FileNotFoundError:
            return MISSING
        except OSError as error:
            _warn(f"cannot read cache entry {path}: {error}")
            return MISSING
        try:
            value = self._decode(data)
        except _OtherVersionError:
            return MISSING
        except Exception as error:  # noqa: BLE001  # any failure to load is a miss
            _warn(f"removed unreadable cache entry {path}: {error!r}")
            self._remove_if_unchanged(path, read)
            return MISSING
        # Mark the entry as just read, for pruning; a failure only ages it.
        with contextlib.suppress(OSError):
            os.utime(path)
        return value

    def put(self, key: bytes, value: object) -> None:
        """Stores `value` under `key`, replacing any entry there.

        A value that cannot be serialized, or a directory that cannot be written,
        skips the write with a warning.
        """
        try:
            data = self._encode(value)
        except Exception as error:  # noqa: BLE001  # any failure skips the write
            kind = type(value).__qualname__
            _warn(f"cannot serialize a value of type {kind!r} to cache it: {error}")
            return
        path = self._file(key)
        temporary: str | None = None
        try:
            self._make_directories(path.parent)
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".", suffix=".tmp", delete=False
            ) as file:
                temporary = file.name
                file.write(data)
            Path(temporary).replace(path)
            temporary = None
        except OSError as error:
            _warn(f"cannot write cache entry {path}: {error}")
            return
        finally:
            if temporary is not None:
                self._remove(Path(temporary))
        if self._max_bytes is not None and self._count_write():
            self._prune_quietly(self._max_bytes)

    def discard(self, key: bytes) -> None:
        """Removes `key`'s entry, if there is one."""
        self._remove(self._file(key))

    def clear(self) -> None:
        """Removes every entry, and any temporary file left by a failed write.

        What cannot be removed is reported with a `CacheWarning`.
        """

        def report(_function: object, path: str, error: BaseException) -> None:
            if not isinstance(error, FileNotFoundError):
                _warn(f"cannot remove {path} from the cache: {error}", depth=3)

        shutil.rmtree(self._root, onexc=report)

    def prune(self, max_bytes: int) -> None:
        """Removes the least recently read entries until the rest fit in `max_bytes`.

        Sizes are the files' sizes on disk. Another process may prune or write the
        same directory at once; an entry it removes first is skipped. A temporary
        file older than an hour, left by a writer that died, is removed; a newer
        one may be in use, and is neither removed nor counted.
        """
        entries: list[tuple[int, int, str]] = []
        stale = time.time_ns() - _STALE_TEMPORARY_NS
        for shard in _scan(self._root):
            if not shard.is_dir(follow_symlinks=False):
                continue
            for entry in _scan(Path(shard.path)):
                is_entry = _ENTRY_NAME.fullmatch(entry.name) is not None
                if not is_entry and not _TEMPORARY_NAME.fullmatch(entry.name):
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if is_entry:
                    entries.append((stat.st_atime_ns, stat.st_size, entry.path))
                elif stat.st_mtime_ns < stale:
                    Path(entry.path).unlink(missing_ok=True)
        total = sum(size for _, size, _ in entries)
        for _, size, file in sorted(entries):
            if total <= max_bytes:
                break
            Path(file).unlink(missing_ok=True)
            total -= size

    def _count_write(self) -> bool:
        """Counts a write; True on every `_PRUNE_EVERY`th."""
        with self._lock:
            self._writes += 1
            return self._writes % _PRUNE_EVERY == 0

    def _prune_quietly(self, max_bytes: int) -> None:
        """Prunes, turning a failure into a warning."""
        try:
            self.prune(max_bytes)
        except OSError as error:
            _warn(f"cannot prune cache directory {self._path}: {error}", depth=2)

    def _make_directories(self, shard: Path) -> None:
        """Makes the store's directories, its own private ones owner-only.

        Its entries can run code when read, so nobody else may write them. Parents
        above the store's directory are made as any directory is.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        for directory in (self._path, self._root, shard):
            directory.mkdir(mode=_PRIVATE, exist_ok=True)

    def _remove_if_unchanged(self, path: Path, read: os.stat_result) -> None:
        """Removes the entry that was read, unless another writer has replaced it."""
        try:
            now = path.stat()
        except OSError:
            return
        if (now.st_ino, now.st_dev, now.st_mtime_ns) == (
            read.st_ino,
            read.st_dev,
            read.st_mtime_ns,
        ):
            self._remove(path)

    def _remove(self, path: Path) -> None:
        """Removes a file if it is there, warning if it cannot."""
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            _warn(f"cannot remove cache file {path}: {error}", depth=2)

    def _encode(self, value: object) -> bytes:
        """An entry's bytes for `value`."""
        payload = self._dumps(value)
        header = json.dumps(self._header(_libraries(value))).encode()
        return _pack(header, payload)

    def _decode(self, data: bytes) -> object:
        """The value in an entry's bytes, checking its header against this process."""
        encoded, payload = _unpack(data)
        header = json.loads(encoded)
        if (
            not isinstance(header, dict)
            or set(header) != {"serializer", "python", "libraries"}
            or not isinstance(libraries := header["libraries"], dict)
        ):
            msg = "the entry's header is malformed"
            raise _CorruptEntryError(msg)
        expected = self._header(
            {str(name): distribution_version(str(name)) for name in libraries}
        )
        if header != expected:
            raise _OtherVersionError
        return self._loads(payload)

    def _header(self, libraries: dict[str, str | None]) -> dict[str, object]:
        """The header this process writes for a value of these libraries."""
        return {
            "serializer": self._serializer,
            "python": sys.version,
            "libraries": libraries,
        }


# The mode of the directories a store makes: its owner's alone.
_PRIVATE = 0o700

# Permission bits that let the group or others write.
_WRITABLE_BY_OTHERS = 0o022


def _warn_if_shared(path: Path) -> None:
    """Warns when `path` exists and someone other than this user may write to it."""
    try:
        status = path.stat()
    except OSError:
        return
    others = status.st_mode & _WRITABLE_BY_OTHERS
    getuid = getattr(os, "getuid", None)
    foreign = getuid is not None and status.st_uid != getuid()
    if others or foreign:
        _warn(
            f"cache directory {path} is writable by other users or owned by another:"
            " whoever can write it can run code in every process that reads it",
            depth=2,
        )


def _scan(directory: Path) -> list[os.DirEntry[str]]:
    """The entries of `directory`, or none if it does not exist (or no longer does)."""
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except FileNotFoundError:
        return []
