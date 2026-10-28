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

# Imported for real: `DiskCache`'s signature annotates with these, and
# `inspect.signature` resolves that at run time.
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, override

from fnloom import _core
from fnloom._codeversion import distribution_version

__all__ = [
    "MISSING",
    "CacheStore",
    "CacheWarning",
    "DiskCache",
    "Format",
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


@dataclass(frozen=True, slots=True)
class Format:
    """How a `DiskCache` turns one node's results into bytes and back.

    A node picks one with `@cache(format=...)`, by this object or by its name. The
    name is recorded in every entry written with it: an entry written under another
    name is a miss, so give a new name whenever `dumps` or `loads` changes.

    Attributes:
        name: Names the pair, in entries and in `@cache(format="name")`.
        dumps: Turns a value into bytes; raising skips the write, with a warning.
        loads: Turns those bytes back into an equal value.
    """

    name: str
    dumps: Callable[[object], bytes]
    loads: Callable[[bytes], object]

    def __post_init__(self) -> None:
        """Checks the name and the pair."""
        given: object = self.name
        if not isinstance(given, str) or not given:
            msg = f"a format's name must be a non-empty str, not {given!r}"
            raise TypeError(msg)
        if not callable(self.dumps) or not callable(self.loads):
            msg = f"format {given!r} needs callable dumps and loads"
            raise TypeError(msg)


# JSON values that load back as the same types, compared exactly: a subclass, such as
# `bool` for `int` or an enum for `str`, is a different value.
_JSON_EXACT = (dict, list, str, int, float, bool, type(None))


def _json_dumps(value: object) -> bytes:
    """`value` as JSON, refusing anything that would load back as something else.

    A tuple would load as a list, a key that is not a `str` as one, and a subclass as
    its base type, so a hit would return a different value than the node computed.
    NaN and the infinities are refused too, being no JSON at all, as is a container
    holding itself.
    """
    # First, so that the walk below never meets a cycle.
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    stack: list[tuple[object, str]] = [(value, "")]
    while stack:
        item, path = stack.pop()
        if type(item) not in _JSON_EXACT:
            where = f" at {path}" if path else ""
            msg = f"json cannot store a {type(item).__qualname__}{where} exactly"
            raise TypeError(msg)
        if type(item) is dict:
            for key, nested in item.items():
                if type(key) is not str:
                    msg = f"json cannot store the {type(key).__qualname__} key {key!r}"
                    raise TypeError(msg + (f" at {path}" if path else ""))
                stack.append((nested, f"{path}[{key!r}]"))
        elif type(item) is list:
            stack.extend(
                (nested, f"{path}[{index}]") for index, nested in enumerate(item)
            )
    return data


def _json_loads(data: bytes) -> object:
    """The value `_json_dumps` wrote."""
    return json.loads(data)


PICKLE: Final = Format("pickle", pickle.dumps, pickle.loads)
"""Any value pickle can store; the default."""

JSON: Final = Format("json", _json_dumps, _json_loads)
"""Dicts with `str` keys, lists, `str`, `int`, finite `float`, `bool` and `None`.

Each of exactly that type.
A value that would load back as anything else, such as a tuple, is not stored: the
write is skipped with a `CacheWarning`, and the node runs again next time.
"""

# The formats every `DiskCache` knows by name.
_BUILT_IN = MappingProxyType({f.name: f for f in (PICKLE, JSON)})


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


# The package's directory, whose frames a warning raised during a run skips.
_PACKAGE = str(Path(__file__).parent)


class MemoryCache:
    """Results held in memory, least recently used evicted first.

    A hit returns the stored object itself, not a copy, so a cached node must not
    mutate its arguments: a change would reach every later run that hits. The entries
    live in the native extension, which a cached run reads and writes directly. Values
    are never serialized, so a node's `@cache(format=...)` does not apply here.

    Pickled or copied, the store comes back empty, with the same bounds: its entries
    belong to its process, so a driver sent to a worker process starts a cache of
    its own there.

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
        self.native: Final = _core.MemoryStore(
            max_entries, max_bytes, None if max_bytes is None else _size
        )

    @override
    def __reduce__(self) -> tuple[type[MemoryCache], tuple[int, int | None]]:
        """An empty store with the same bounds; see the class."""
        return MemoryCache, (self._max_entries, self._max_bytes)

    def get(self, key: bytes) -> object:
        """The value stored under `key`, the same object `put` stored, or `MISSING`."""
        return self.native.get(key, MISSING)

    def put(self, key: bytes, value: object) -> None:
        """Stores `value` under `key`, evicting the least recently used past a bound."""
        self.native.put(key, value)

    def discard(self, key: bytes) -> None:
        """Forgets `key`, if the store holds it."""
        self.native.discard(key)

    def __len__(self) -> int:
        """The number of entries held."""
        return len(self.native)

    @property
    def _entries(self) -> dict[bytes, object]:
        """A copy of every entry, for tests."""
        return dict(self.native.items())


def _size(value: object) -> int | None:
    """`value`'s size for `MemoryCache`'s bound; `None`, having warned, if unknown."""
    try:
        return sys.getsizeof(value)
    except Exception as error:  # noqa: BLE001  # any failure skips the write
        # Called by `MemoryCache.put`, or by the binding during a run: either way,
        # reported at the first frame outside fnloom.
        warnings.warn(
            f"cannot size a {type(value).__qualname__} to cache it: {error}",
            CacheWarning,
            skip_file_prefixes=(_PACKAGE,),
        )
        return None


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

    Pickled or copied, the store reopens the same directory with the same
    arguments, so worker processes share its entries; `dumps` and `loads` must
    pickle too, as the default pair does.

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
        formats: More `Format`s that `@cache(format="name")` can name, besides the
            built-in `"pickle"` and `"json"`. A node naming a format the store does
            not know is refused when the driver is built.
    """

    def __init__(  # noqa: PLR0913  # the directory, its bound, and how it serializes
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int | None = None,
        dumps: Callable[[object], bytes] = pickle.dumps,
        loads: Callable[[bytes], object] = pickle.loads,
        serializer: str = "pickle",
        formats: Iterable[Format] = (),
    ) -> None:
        """Opens the store at `path`, pruning it to `max_bytes` if given.

        On POSIX, an existing directory that other users can write is reported with
        a `CacheWarning`: whoever can write it can run code in every reader.

        Raises:
            ValueError: `max_bytes` is negative, or `path` holds a NUL byte.
            TypeError: `serializer` is not a non-empty `str`: it names the pair, it
                is not a module (pass `dumps` and `loads` for that); or `formats`
                holds something other than a `Format`.
            ValueError: Two formats, or a format and the default pair, share a name
                but not their `dumps` and `loads`. The default pair takes the place
                of a built-in format of its name.
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
        self._default = Format(serializer, dumps, loads)
        self._given = tuple(formats)
        self._formats = _formats(self._default, self._given)
        self._writes = 0
        self._lock = threading.Lock()
        if max_bytes is not None:
            self._prune_quietly(max_bytes)

    @override
    def __reduce__(self) -> tuple[Callable[..., DiskCache], tuple[object, ...]]:
        """The store reopened at its directory with its arguments; see the class."""
        default = self._default
        return _reopen, (
            self._path,
            self._max_bytes,
            default.dumps,
            default.loads,
            default.name,
            self._given,
        )

    @property
    def path(self) -> Path:
        """The store's directory."""
        return self._path

    @property
    def formats(self) -> Mapping[str, Format]:
        """The formats `@cache(format="name")` can name, by name."""
        return self._formats

    def _file(self, key: bytes) -> Path:
        """Where `key`'s entry lives."""
        name = key.hex()
        return self._root / name[:2] / name

    def get(self, key: bytes, format: Format | None = None) -> object:  # noqa: A002  # the name `@cache` gives it
        """The value stored under `key`, or `MISSING`.

        An entry written by another Python, format or library version is a quiet
        miss, left for the next `put` to replace. An entry that cannot be read back
        is a miss too: it is removed, with a warning.

        Args:
            key: The entry's key.
            format: How the entry was written; None for the store's default pair.
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
            value = self._decode(data, format or self._default)
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

    def put(self, key: bytes, value: object, format: Format | None = None) -> None:  # noqa: A002  # as in `get`
        """Stores `value` under `key`, replacing any entry there.

        A value that cannot be serialized, or a directory that cannot be written,
        skips the write with a warning.

        Args:
            key: The entry's key.
            value: What to store.
            format: How to write it; None for the store's default pair.
        """
        try:
            data = self._encode(value, format or self._default)
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

    @staticmethod
    def _encode(value: object, format: Format) -> bytes:  # noqa: A002  # as in `get`
        """An entry's bytes for `value`, written with `format`."""
        payload = format.dumps(value)
        header = json.dumps(_header(format, _libraries(value))).encode()
        return _pack(header, payload)

    @staticmethod
    def _decode(data: bytes, format: Format) -> object:  # noqa: A002  # as in `get`
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
        expected = _header(
            format, {str(name): distribution_version(str(name)) for name in libraries}
        )
        if header != expected:
            raise _OtherVersionError
        return format.loads(payload)


def _header(format: Format, libraries: dict[str, str | None]) -> dict[str, object]:  # noqa: A002  # as in `DiskCache.get`
    """The header this process writes for a value of these libraries."""
    return {"serializer": format.name, "python": sys.version, "libraries": libraries}


def _formats(default: Format, given: Iterable[Format]) -> Mapping[str, Format]:
    """The built-in formats, the default pair and `given`, by name.

    The default pair takes its own name, in place of a built-in format of that name:
    entries it writes carry that name, so a node naming it reads them back.
    """
    formats = {**_BUILT_IN, default.name: default}
    for item in given:
        if not isinstance(item, Format):
            msg = f"formats must be Format objects, not {type(item).__qualname__}"
            raise TypeError(msg)
        if formats.setdefault(item.name, item) != item:
            msg = f"two formats are named {item.name!r}"
            raise ValueError(msg)
    return MappingProxyType(formats)


# The mode of the directories a store makes: its owner's alone.
_PRIVATE = 0o700

# Permission bits that let the group or others write.
_WRITABLE_BY_OTHERS = 0o022


def _warn_if_shared(path: Path) -> None:
    """Warns when `path` exists and someone other than this user may write to it."""
    # Only POSIX mode bits and owners say who can write. Windows grants access through
    # ACLs and reports every directory as mode 0o777, so there the check would misfire.
    if os.name != "posix":
        return
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


def _reopen(
    path: Path,
    max_bytes: int | None,
    dumps: Callable[[object], bytes],
    loads: Callable[[bytes], object],
    serializer: str,
    formats: tuple[Format, ...] = (),
) -> DiskCache:
    """Opens a `DiskCache` from the arguments a pickled one was made with.

    `formats` defaults to none, so a store pickled before formats existed loads.
    """
    return DiskCache(
        path,
        max_bytes=max_bytes,
        dumps=dumps,
        loads=loads,
        serializer=serializer,
        formats=formats,
    )
