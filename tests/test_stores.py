"""Tests for the cache stores: in memory and on disk, and how each fails safe."""

import gc
import json
import os
import pickle
import struct
import subprocess
import sys
import textwrap
import threading
import time
import warnings
import weakref
from pathlib import Path
from typing import override

import pytest

import fnloom
from fnloom import MISSING, CacheStore, CacheWarning, DiskCache, MemoryCache
from fnloom._stores import _pack, _unpack

THREADS = 8
KEYS_PER_THREAD = 10_000
ENTRIES = 10
ANSWER = 42


def _identity(value: object) -> bytes:
    """A serializer for bytes that keeps them as they are."""
    assert isinstance(value, bytes)
    return value


def _identity_loads(data: bytes) -> object:
    """The loader matching `_identity`."""
    return data


def _key(n: int) -> bytes:
    """A 32-byte key, as the binding makes them."""
    return n.to_bytes(32, "big")


def _entry(root: Path, key: bytes) -> Path:
    """Where a disk store keeps `key`."""
    return root / "v1" / key.hex()[:2] / key.hex()


def _files(root: Path) -> list[Path]:
    """Every file under the store's directory, temporary ones included."""
    return sorted(path for path in root.rglob("*") if path.is_file())


def _rewrite(path: Path, payload: bytes | None = None, **changes: object) -> None:
    """Rewrites a whole entry with `changes` to its header, or another payload."""
    encoded, old = _unpack(path.read_bytes())
    header = json.loads(encoded)
    header.update(changes)
    path.write_bytes(
        _pack(json.dumps(header).encode(), old if payload is None else payload)
    )


def _quiet_miss(store: DiskCache, key: bytes, path: Path) -> None:
    """A whole entry from another version is a miss with no warning, and stays."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert store.get(key) is MISSING
    assert path.exists()


def _miss_warns_once(store: DiskCache, key: bytes, path: Path) -> None:
    """A bad entry is a miss that warns once, and is gone afterwards."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert store.get(key) is MISSING
    assert [warning.category for warning in caught] == [CacheWarning]
    assert not path.exists()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert store.get(key) is MISSING


def test_stores_meet_the_protocol(tmp_path: Path) -> None:
    """Both stores are `CacheStore`s, and `MISSING` reads as itself."""
    stores: list[CacheStore] = [MemoryCache(), DiskCache(tmp_path)]
    for store in stores:
        assert store.get(_key(0)) is MISSING
    assert repr(MISSING) == "fnloom.MISSING"
    assert fnloom.cache.__module__ == "fnloom._decorators"


def test_memory_round_trip_and_identity() -> None:
    """A stored value comes back as the same object; an unknown key is a miss."""
    store = MemoryCache()
    value = {"rows": [1, 2, 3]}
    store.put(_key(1), value)
    assert store.get(_key(1)) is value
    assert store.get(_key(2)) is MISSING
    store.discard(_key(1))
    store.discard(_key(1))
    assert store.get(_key(1)) is MISSING


def test_memory_evicts_least_recently_used() -> None:
    """Past either bound, the least recently used entry goes first."""
    a, b, c = _key(1), _key(2), _key(3)
    by_count = MemoryCache(max_entries=2)
    by_count.put(a, "a")
    by_count.put(b, "b")
    assert by_count.get(a) == "a"
    by_count.put(c, "c")
    assert by_count.get(b) is MISSING
    assert by_count.get(a) == "a"
    assert by_count.get(c) == "c"

    values = [bytes([n]) * 100 for n in range(3)]
    size = sys.getsizeof(values[0])
    by_size = MemoryCache(max_bytes=2 * size + 1)
    by_size.put(a, values[0])
    by_size.put(b, values[1])
    assert by_size.get(a) is values[0]
    by_size.put(c, values[2])
    assert by_size.get(b) is MISSING
    assert by_size.get(a) is values[0]
    assert by_size.get(c) is values[2]

    too_big = MemoryCache(max_bytes=2 * size + 1)
    too_big.put(a, values[0])
    too_big.put(b, values[1])
    too_big.put(a, b"y" * (3 * size))
    too_big.put(c, b"z" * (3 * size))
    assert too_big.get(a) is MISSING
    assert too_big.get(c) is MISSING
    assert too_big.get(b) is values[1]


def test_memory_counts_its_entries_and_refuses_bad_keys() -> None:
    """`len` is the number of entries; a key that is not 32 bytes is refused."""
    bound = 2
    store = MemoryCache(max_entries=bound)
    assert len(store) == 0
    for n in range(bound + 1):
        store.put(_key(n), n)
    assert len(store) == bound
    store.discard(_key(2))
    assert len(store) == 1
    with pytest.raises(ValueError, match="32 bytes"):
        store.put(b"short", 1)


class _Unsizable:
    """A value `sys.getsizeof` cannot size."""

    @override
    def __sizeof__(self) -> int:
        msg = "no size"
        raise RuntimeError(msg)


def test_memory_skips_a_value_it_cannot_size() -> None:
    """With `max_bytes`, a value that cannot be sized is not stored, and warned of."""
    store = MemoryCache(max_bytes=1000)
    with pytest.warns(CacheWarning, match="cannot size a _Unsizable") as caught:
        store.put(_key(1), _Unsizable())
    assert caught[0].filename == __file__
    assert store.get(_key(1)) is MISSING
    unbounded = MemoryCache()
    value = _Unsizable()
    unbounded.put(_key(1), value)
    assert unbounded.get(_key(1)) is value


class _Holder:
    """A value that refers back to the store holding it."""

    def __init__(self, store: MemoryCache) -> None:
        self.store = store


def test_memory_values_in_a_cycle_through_the_store_are_collected() -> None:
    """The cyclic GC sees the store's values, so a cycle through it is freed."""
    store = MemoryCache()
    holder = _Holder(store)
    store.put(_key(1), holder)
    alive = weakref.ref(holder)
    del store, holder
    gc.collect()
    assert alive() is None


def test_memory_rejects_bad_bounds() -> None:
    """The bounds must be positive."""
    with pytest.raises(ValueError, match="max_entries"):
        MemoryCache(max_entries=0)
    with pytest.raises(ValueError, match="max_bytes"):
        MemoryCache(max_bytes=0)


def test_memory_is_thread_safe() -> None:
    """Threads storing and reading at once lose nothing below the bound."""
    store = MemoryCache()
    errors: list[BaseException] = []
    start = threading.Barrier(THREADS)

    def work(thread: int) -> None:
        try:
            start.wait()
            for n in range(KEYS_PER_THREAD):
                key = _key(thread * KEYS_PER_THREAD + n)
                store.put(key, (thread, n))
                assert store.get(key) == (thread, n)
        except BaseException as error:  # noqa: BLE001  # reported to the main thread
            errors.append(error)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    for thread in range(THREADS):
        for n in range(KEYS_PER_THREAD):
            assert store.get(_key(thread * KEYS_PER_THREAD + n)) == (thread, n)


def test_disk_round_trip_and_layout(tmp_path: Path) -> None:
    """An entry is one file named by its key, and reads back equal."""
    store = DiskCache(tmp_path)
    value = {"a": [1, 2.5, "three"], "b": (None, True)}
    key = bytes(range(32))
    store.put(key, value)
    assert _files(tmp_path) == [tmp_path / "v1" / "00" / key.hex()]
    assert store.get(key) == value
    assert store.get(_key(7)) is MISSING
    store.put(key, "replaced")
    assert store.get(key) == "replaced"
    store.discard(key)
    store.discard(key)
    assert store.get(key) is MISSING


def test_disk_entry_format(tmp_path: Path) -> None:
    """An entry starts with the magic, the format version and a JSON header."""
    store = DiskCache(tmp_path)
    store.put(_key(1), ANSWER)
    data = _entry(tmp_path, _key(1)).read_bytes()
    assert data[:4] == b"FNLC"
    assert struct.unpack_from(">H", data, 4) == (1,)
    header, payload = _unpack(data)
    assert json.loads(header) == {
        "serializer": "pickle",
        "python": sys.version,
        "libraries": {"builtins": None},
    }
    assert pickle.loads(payload) == ANSWER  # noqa: S301  # our own file


def test_disk_write_is_atomic(tmp_path: Path) -> None:
    """A failed write leaves nothing behind; racing writers leave one whole entry."""

    def broken_dumps(value: object) -> bytes:
        pickle.dumps(value)
        msg = "halfway"
        raise RuntimeError(msg)

    broken = DiskCache(tmp_path, dumps=broken_dumps)
    with pytest.warns(CacheWarning, match="halfway"):
        broken.put(_key(1), "value")
    assert _files(tmp_path) == []

    script = textwrap.dedent(
        """
        import sys
        from fnloom import DiskCache

        store = DiskCache(sys.argv[1])
        value = sys.argv[2].encode() * 1_000_000
        for _ in range(20):
            store.put(bytes(32), value)
        """
    )
    writers = [
        subprocess.Popen([sys.executable, "-c", script, str(tmp_path), letter])  # noqa: S603  # our own interpreter
        for letter in "ab"
    ]
    for writer in writers:
        assert writer.wait(timeout=60) == 0
    assert _files(tmp_path) == [_entry(tmp_path, bytes(32))]
    assert DiskCache(tmp_path).get(bytes(32)) in {b"a" * 1_000_000, b"b" * 1_000_000}


def test_disk_failed_replace_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure to move the written file into place removes it."""

    def failing_replace(source: str, target: str) -> None:
        msg = f"cannot replace {target} with {source}"
        raise OSError(msg)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.warns(CacheWarning, match="cannot replace"):
        DiskCache(tmp_path).put(_key(1), "value")
    assert _files(tmp_path) == []


def test_disk_bad_entries_are_misses(tmp_path: Path) -> None:
    """A corrupt entry is a miss, warned once and removed."""
    store = DiskCache(tmp_path)
    key = _key(1)
    path = _entry(tmp_path, key)

    store.put(key, list(range(1000)))
    path.write_bytes(path.read_bytes()[:-10])
    _miss_warns_once(store, key, path)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(500))
    _miss_warns_once(store, key, path)

    path.write_bytes(b"FNLC")
    _miss_warns_once(store, key, path)

    store.put(key, "value")
    path.write_bytes(path.read_bytes()[:-1] + b"!")
    _miss_warns_once(store, key, path)

    store.put(key, "value")
    _rewrite(path, unexpected=True)
    _miss_warns_once(store, key, path)

    store.put(key, "value")
    _, payload = _unpack(path.read_bytes())
    path.write_bytes(_pack(b"not json", payload))
    _miss_warns_once(store, key, path)


def test_disk_truncation_is_a_miss_with_any_serializer(tmp_path: Path) -> None:
    """A cut or damaged payload is a miss even if the serializer would accept it."""
    store = DiskCache(
        tmp_path, dumps=_identity, loads=_identity_loads, serializer="identity"
    )
    key = _key(1)
    path = _entry(tmp_path, key)
    store.put(key, b"abcdef")
    assert store.get(key) == b"abcdef"
    path.write_bytes(path.read_bytes()[:-3])
    _miss_warns_once(store, key, path)

    store.put(key, b"abcdef")
    path.write_bytes(path.read_bytes()[:-1] + b"g")
    _miss_warns_once(store, key, path)


def test_disk_other_versions_are_quiet_misses(tmp_path: Path) -> None:
    """An entry from another Python, library or serializer: a miss, kept, unreported."""
    store = DiskCache(tmp_path)
    key = _key(1)
    path = _entry(tmp_path, key)

    store.put(key, "value")
    _rewrite(path, python="2.7.18")
    _quiet_miss(store, key, path)
    store.put(key, "value")
    assert store.get(key) == "value"

    _rewrite(path, libraries={"pytest": "0.0.1"})
    _quiet_miss(store, key, path)

    store.put(key, "value")
    other = DiskCache(
        tmp_path,
        serializer="json",
        dumps=lambda value: json.dumps(value).encode(),
        loads=json.loads,
    )
    _quiet_miss(other, key, path)


def test_disk_unloadable_class_is_a_miss(tmp_path: Path) -> None:
    """A pickle naming a class that moved is a miss."""
    store = DiskCache(tmp_path)
    key = _key(1)
    store.put(key, "value")
    path = _entry(tmp_path, key)
    _rewrite(path, payload=b"cno_such_module_fnloom\nThing\n.")
    _miss_warns_once(store, key, path)


def test_warnings_point_at_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning raised inside a helper is reported at the store method's caller."""
    store = DiskCache(tmp_path)
    store.put(_key(1), "value")

    def failing_unlink(path: Path, *, missing_ok: bool = False) -> None:
        msg = f"cannot unlink {path} ({missing_ok=})"
        raise PermissionError(msg)

    def failing_prune(store: DiskCache, max_bytes: int) -> None:
        msg = f"cannot prune {store.path} to {max_bytes}"
        raise PermissionError(msg)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.warns(CacheWarning, match="cannot remove") as caught:
        store.discard(_key(1))
    assert [warning.filename for warning in caught] == [__file__]

    monkeypatch.setattr(DiskCache, "prune", failing_prune)
    with pytest.warns(CacheWarning, match="cannot prune") as caught:
        DiskCache(tmp_path, max_bytes=1)
    assert [warning.filename for warning in caught] == [__file__]


@pytest.mark.parametrize("serializer", [pickle, "", None, b"pickle"])
def test_disk_serializer_must_be_a_name(tmp_path: Path, serializer: object) -> None:
    """`serializer` names the pair: a module, an empty name or a non-str is refused."""
    with pytest.raises(TypeError, match="serializer must be a non-empty str"):
        DiskCache(tmp_path, serializer=serializer)  # ty: ignore[invalid-argument-type]


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="permissions do not stop this user writing",
)
def test_disk_unwritable_is_a_warning(tmp_path: Path) -> None:
    """A read-only directory or an unpicklable value skips the write, with a warning."""
    store = DiskCache(tmp_path)
    with pytest.warns(CacheWarning, match="of type 'function'"):
        store.put(_key(1), lambda: None)
    assert _files(tmp_path) == []

    tmp_path.chmod(0o500)
    try:
        with pytest.warns(CacheWarning):
            store.put(_key(2), "value")
        assert store.get(_key(2)) is MISSING
    finally:
        tmp_path.chmod(0o700)
    assert _files(tmp_path) == []


def _fill(store: DiskCache, count: int) -> int:
    """Stores `count` entries of one size, and returns that size on disk."""
    for n in range(count):
        store.put(_key(n), b"x" * 1000)
    return _entry(store.path, _key(0)).stat().st_size


def test_disk_prune(tmp_path: Path) -> None:
    """Pruning keeps the most recently read entries within the bound."""
    store = DiskCache(tmp_path)
    size = _fill(store, ENTRIES)
    for n in range(ENTRIES):
        os.utime(_entry(tmp_path, _key(n)), ns=(n * 10**9, n * 10**9))
    recent = [_key(2), _key(5), _key(0)]
    for key in recent:
        assert store.get(key) == b"x" * 1000
    store.prune(3 * size)
    assert _files(tmp_path) == sorted(_entry(tmp_path, key) for key in recent)

    store.prune(3 * size - 1)
    assert len(_files(tmp_path)) == len(recent) - 1

    store.clear()
    assert _files(tmp_path) == []
    assert store.get(_key(0)) is MISSING


def test_disk_prune_removes_stale_temporary_files(tmp_path: Path) -> None:
    """Prune removes a writer's leftover file after an hour, and never counts one."""
    store = DiskCache(tmp_path)
    size = _fill(store, 2)
    shard = _entry(tmp_path, _key(0)).parent
    stale = shard / ".stale.tmp"
    fresh = shard / ".fresh.tmp"
    stale.write_bytes(b"x" * 10_000)
    fresh.write_bytes(b"x" * 10_000)
    two_hours_ago = time.time_ns() - 2 * 3600 * 10**9
    os.utime(stale, ns=(two_hours_ago, two_hours_ago))
    store.prune(2 * size)
    assert not stale.exists()
    assert fresh.exists()
    assert _entry(tmp_path, _key(0)).exists()
    assert _entry(tmp_path, _key(1)).exists()


def test_disk_bound_prunes_on_open_and_every_hundred_writes(tmp_path: Path) -> None:
    """A bounded store prunes when it opens and after every 100 writes."""
    size = _fill(DiskCache(tmp_path), 20)
    DiskCache(tmp_path, max_bytes=5 * size)
    assert len(_files(tmp_path)) == 5  # noqa: PLR2004  # five entries fit

    other = tmp_path / "other"
    bounded = DiskCache(other, max_bytes=10 * size)
    _fill(bounded, 99)
    assert len(_files(other)) == 99  # noqa: PLR2004  # no prune yet
    bounded.put(_key(99), b"x" * 1000)
    assert len(_files(other)) == 10  # noqa: PLR2004  # ten entries fit


def test_disk_clear_and_prune_on_a_new_directory(tmp_path: Path) -> None:
    """A store with no entries yet clears and prunes to nothing."""
    store = DiskCache(tmp_path / "missing")
    store.clear()
    store.prune(0)
    assert store.get(_key(1)) is MISSING


_POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
# Permission bits for the group and for others.
_SHARED_BITS = 0o077


@_POSIX
def test_disk_directories_are_private(tmp_path: Path) -> None:
    """The store's directories are its owner's alone: their entries can run code."""
    path = tmp_path / "cache"
    store = DiskCache(path)
    store.put(_key(1), b"x")
    entry = next(path.rglob(_key(1).hex()))
    for directory in (path, entry.parent.parent, entry.parent):
        assert directory.stat().st_mode & _SHARED_BITS == 0, directory


@_POSIX
def test_disk_warns_about_a_directory_others_can_write(tmp_path: Path) -> None:
    """Whoever can write the directory can run code in every reader, so say so."""
    path = tmp_path / "shared"
    path.mkdir()
    path.chmod(0o777)
    with pytest.warns(CacheWarning, match="writable by other users"):
        DiskCache(path)
    path.chmod(0o700)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        DiskCache(path)


@pytest.mark.skipif(os.name == "posix", reason="mode bits mean access")
def test_disk_does_not_judge_access_by_mode_bits_off_posix(tmp_path: Path) -> None:
    """Off POSIX the mode bits say nothing about who can write, so no warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        DiskCache(tmp_path)


@_POSIX
def test_disk_clear_warns_about_what_it_cannot_remove(tmp_path: Path) -> None:
    """A failed `clear` says so, rather than leaving entries behind in silence."""
    store = DiskCache(tmp_path)
    store.put(_key(1), b"x")
    shard = next(tmp_path.rglob(_key(1).hex())).parent
    shard.chmod(0o500)
    try:
        with pytest.warns(CacheWarning, match="cannot remove"):
            store.clear()
    finally:
        shard.chmod(0o700)
    store.clear()
    assert store.get(_key(1)) is MISSING


def test_disk_clear_on_a_new_directory_is_quiet(tmp_path: Path) -> None:
    """Clearing a store that never wrote anything has nothing to report."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        DiskCache(tmp_path / "never").clear()


def test_disk_keeps_an_entry_rewritten_while_a_bad_one_was_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader removes the bad entry it read, never a good one written since."""
    store = DiskCache(tmp_path, dumps=_identity, loads=_identity_loads, serializer="b")
    writer = DiskCache(tmp_path, dumps=_identity, loads=_identity_loads, serializer="b")
    store.put(_key(1), b"old")

    def decode_then_rewrite(data: bytes) -> object:
        # Another process replaces the entry after this one read it, before it
        # finds what it read unreadable.
        writer.put(_key(1), b"new")
        msg = f"unreadable {len(data)} bytes"
        raise ValueError(msg)

    monkeypatch.setattr(store, "_decode", decode_then_rewrite)
    with pytest.warns(CacheWarning, match="unreadable"):
        assert store.get(_key(1)) is MISSING
    assert writer.get(_key(1)) == b"new"


def test_disk_refuses_a_path_with_a_nul_byte(tmp_path: Path) -> None:
    """A path no file system accepts is a mistake at once, not a warning per entry."""
    with pytest.raises(ValueError, match="NUL"):
        DiskCache(f"{tmp_path}/a\x00b")


def test_with_cache_refuses_what_is_not_a_store() -> None:
    """A store needs `get`, `put` and `discard`; anything else fails at once."""
    with pytest.raises(TypeError, match="get, put and discard"):
        fnloom.Builder().with_cache(object())  # ty: ignore[invalid-argument-type]
