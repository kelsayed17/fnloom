# Caching

A cache lets a run skip a node whose result is already known: the same function with the same arguments gives the
same value. It pays off in two places. In one process, a prepared plan run once per incoming record skips the nodes
that depend only on what did not change since the last record. Across processes, a pipeline rerun after an edit
runs only the nodes the edit touches, and the nodes downstream of them.

fnloom's cache is built so that **a hit is never wrong**: when fnloom cannot be sure a stored value is what the node
would return, the node runs, and `explain_cache` says why. There is one way left to get a stale result, and it comes
first on this page: a node that reads the outside world must say so.

The examples on this page use one module:

```python
# file: quotes.py
def spread(bid: float, ask: float) -> float:
    """The distance between the best prices."""
    return ask - bid


def mid(bid: float, ask: float) -> float:
    """The price halfway between them."""
    return (bid + ask) / 2


def relative_spread(spread: float, mid: float) -> float:
    """The spread as a share of the mid price."""
    return spread / mid
```

## Turning it on

`Builder.with_cache(store)` caches every node of the driver in `store`. `fnloom.MemoryCache` keeps results for the
life of the process; `fnloom.DiskCache(path)` keeps them on disk, across processes ([Stores](#stores)).

```python
import fnloom
import quotes

driver = fnloom.Builder().with_modules(quotes).with_cache(fnloom.MemoryCache()).build()
driver.execute(["relative_spread"], inputs={"bid": 99.0, "ask": 101.0})
# -> {'relative_spread': 0.02}
```

Nothing else changes: `execute`, `prepare` and `PreparedPlan.run` work as without a cache, and return what they would
return without it. Each run works out every node's **key** from the node's code and the values it reads, finds the
stored results nearest the outputs, and runs and stores the rest. A second run with the same inputs loads
`relative_spread` and runs nothing; `explain_cache` shows it without running anything:

```python
driver.explain_cache(["relative_spread"], inputs={"bid": 99.0, "ask": 101.0})
# -> {"mid": ("hit", None), "relative_spread": ("hit", None), "spread": ("hit", None)}
```

With other inputs, every key changes, so every node misses:

```python
outcomes = driver.explain_cache(["relative_spread"], inputs={"bid": 99.0, "ask": 102.0})
{name: outcome.status for name, outcome in outcomes.items()}
# -> {"mid": "miss", "relative_spread": "miss", "spread": "miss"}
```

A key covers a node's code as well as its inputs, so an edit to the function, to a helper it calls or to a constant
it reads misses too. The code is read when the driver is built, never per run: **rebuild the driver after editing
code** ([Code versions](#code-versions)).

### On the async driver

`with_cache` works the same way with `build_async()` (Q68). The keys, stores, [policies](#policies) and
`explain_cache` are the sync driver's, so the two drivers share what a store holds:

```python
import asyncio

store = fnloom.MemoryCache()
builder = fnloom.Builder().with_modules(quotes).with_cache(store)
quote = {"bid": 98.0, "ask": 102.0}
asyncio.run(builder.build_async().execute(["relative_spread"], inputs=quote))
# -> {'relative_spread': 0.04}
outcomes = builder.build().explain_cache(["relative_spread"], inputs=quote)
{name: outcome.status for name, outcome in outcomes.items()}
# -> {"mid": "hit", "relative_spread": "hit", "spread": "hit"}
```

Each node is stored as it succeeds. A node that fails, or that a failing node cancels, stores nothing, so the next run
loads what succeeded and runs the rest. A `recompute` node that something reads runs first in every run, and an
`async def` one is awaited. `explain_cache` is synchronous on `AsyncDriver` too, since it runs nothing.

Store calls run on the event loop's thread. A `MemoryCache` costs next to nothing there. A `DiskCache` reads and
writes files, which stops the loop while it does, as an inline sync node would.

## The purity contract

A hit returns a stored value in place of running the node, so the cache is only right for a node whose result depends
on its parameters and nothing else. fnloom checks what it can (the code, the helpers and constants it reads, and the
values passed in) and cannot see the rest. Two rules are yours to keep.

**A node that reads the outside world is `recompute`.** A loader that reads a file, a node that reads the clock, a
request to a service: their result can change while their parameters stay the same. Mark each one
`@cache(behavior="recompute")`. It then runs on every run, so it is never stale, and the nodes downstream of it still
hit when its result did not change. An unmarked loader is the one way to get a stale result from the cache.

```python
# file: prices_file.py
from pathlib import Path

from fnloom import cache


@cache(behavior="recompute")
def raw(path: str) -> str:
    """The file's text, read on every run."""
    return Path(path).read_text(encoding="utf-8")


def prices(raw: str) -> list[float]:
    """One price per line."""
    return [float(line) for line in raw.split()]


def high(prices: list[float]) -> float:
    """The highest price."""
    return max(prices)
```

```python
import tempfile
from pathlib import Path

import prices_file

folder = Path(tempfile.mkdtemp())
path = folder / "prices.txt"
path.write_text("1.5\n2.5\n", encoding="utf-8")
loader = (
    fnloom.Builder().with_modules(prices_file).with_cache(fnloom.MemoryCache()).build()
)
loader.execute(["high"], inputs={"path": str(path)})
# -> {'high': 2.5}
path.write_text("1.5\n3.5\n", encoding="utf-8")
loader.execute(["high"], inputs={"path": str(path)})
# -> {'high': 3.5}
```

`raw` read the new text, and `prices` and `high` ran again because `raw`'s result changed. Had the file been
unchanged, they would have hit.

**A cached node never mutates its arguments.** A `MemoryCache` hit returns the stored object itself, not a copy, so a
node that appends to a list it was given changes the stored value, and every later run that hits sees the change.
Without a cache such a node already corrupts the other readers of that value in the same run; with one, the
corruption outlives the run.

## Policies

A node changes how it is cached with `@cache`, which records the policy and reads nothing when it decorates, like
`@tag`. It keeps the name and the `behavior` values of Hamilton's `@cache`, and moves `ignore` to where it is true
([Coming from Hamilton](from-hamilton.md#caching)).

| Policy | Runs | Stored | Nodes downstream are keyed by |
| --- | --- | --- | --- |
| `@cache()`, or no decorator | on a miss | yes | its key |
| `@cache(behavior="recompute")` | always | no | the fingerprint of its result |
| `@cache(behavior="disable")` | always | never | nothing: they cannot be cached either, unless they `ignore` it |
| A field or column node of `@extract_fields` or `@extract_columns` | always, so its type check always runs | never | its key: the producer's, with the field or column, its declared type and its fill |

**`recompute`** is for a node that reads the outside world, as above. Its result is never stored, since the node
always runs and nothing would read it back. Its result must be fingerprintable ([Fingerprints](#fingerprints)), or
the nodes reading it cannot be cached.

**`disable`** never stores the node's result and never versions it, so it suits a value that cannot or must not be
pickled: a client, a connection, a lock.

**`ignore`** is said by the node that reads such a value, for each parameter that does not change its result:
`@cache(ignore=("client",))` leaves `client` out of that node's key, so the node can hit although `client` cannot be
versioned. Every other node reading `client` is unaffected.

```python
# file: market.py
from fnloom import cache


class Client:
    """A connection to a price service, which must never be pickled."""

    def last_price(self, symbol: str) -> float:
        """What the service quotes; fixed here."""
        return 101.0


@cache(behavior="disable")
def client() -> Client:
    """A new connection."""
    return Client()


@cache(behavior="recompute", ignore=("client",))
def last(client: Client, symbol: str) -> float:
    """The latest price: the outside world, so `recompute`."""
    return client.last_price(symbol)


@cache(ignore=("client",))
def lot_value(client: Client, last: float, lots: int) -> float:
    """What the lots are worth, whichever client is passed."""
    return last * lots * 100
```

```python
import market

feed = fnloom.Builder().with_modules(market).with_cache(fnloom.MemoryCache()).build()
feed.execute(["lot_value"], inputs={"symbol": "ABC", "lots": 2})
# -> {'lot_value': 20200.0}
feed.explain_cache(["lot_value"], inputs={"symbol": "ABC", "lots": 2})
# -> {"client": ("uncacheable", "policy disable"), "last": ("miss", "policy recompute"), "lot_value": ("unknown", "downstream of recompute node 'last'")}
```

`lot_value` is `unknown` to `explain_cache` because its key depends on `last`'s result, which only a run knows: in a
run, `last` runs first, and `lot_value` hits when `last` returned the same price.

**`version="..."`** replaces the automatic code version with your own string, for a function the automatic one
cannot cover, such as one that reads a mutable global or a file next to its module. Change the string whenever what
the function returns may change. It replaces the code version only: the values the node reads, a `@parameterize`
row's `value()` constants and bindings included, are still part of its key.

```python
# file: weights.py
from fnloom import cache

WEIGHTS = {"bid": 0.4, "ask": 0.6}


def plain_weighted(bid: float, ask: float) -> float:
    """Reads a dict, which may change unseen: uncacheable."""
    return bid * WEIGHTS["bid"] + ask * WEIGHTS["ask"]


@cache(version="weights-1")
def weighted(bid: float, ask: float) -> float:
    """The same, vouched for by hand."""
    return bid * WEIGHTS["bid"] + ask * WEIGHTS["ask"]
```

```python
import weights

vouched = (
    fnloom.Builder().with_modules(weights).with_cache(fnloom.MemoryCache()).build()
)
outcomes = vouched.explain_cache(
    ["plain_weighted", "weighted"], inputs={"bid": 1.0, "ask": 2.0}
)
outcomes["plain_weighted"].reason
# -> "global 'WEIGHTS' of 'plain_weighted' is a dict, which may change; give @cache(version=...)"
outcomes["weighted"]
# -> ("miss", None)
```

**Switching a node's behavior between builds.** A node's behavior is part of its key, so `default` and `recompute`
results are kept apart. A default run after a recompute run serves the default entries stored before it, for the node
and everything downstream, consistently: never a recompute result beside downstream entries keyed before it (Q49 in
[the register](quirk-register.md)).

## Fingerprints

An input's or override's version is its **fingerprint**: a hash of its exact type and its whole content, computed
once per run. Two values share one only if they hold the same content of the same types, so `1`, `1.0`, `True` and
`"1"` all differ, as do `[1, 2]` and `(1, 2)`. The limits at the end of this section list the few ways a node could
still tell two such values apart. A computed node's value is never fingerprinted: its version is its own key, so a
large intermediate costs nothing to version.

```python
fnloom.fingerprint(1) == fnloom.fingerprint(1.0)
# -> False
fnloom.fingerprint([1, 2]) == fnloom.fingerprint([1, 2])
# -> True
```

These can be fingerprinted:

- `None`, `bool`, `int`, `float` (by bit pattern, so `-0.0` and `0.0` differ, and every NaN is the same), `complex`,
  `str`, `bytes` and `bytearray`;
- `tuple` and `list` by element, in order; `dict` by entry, in insertion order, and `set` and `frozenset` by member,
  in iteration order, since a node can see the order. Two equal sets that iterate differently differ;
- `datetime`, `date`, `time` and `timedelta`, with a time zone from `zoneinfo` or `datetime.timezone`; `Decimal`,
  `Fraction`, `UUID`, the `pathlib` paths and enum members;
- dataclass instances and named tuples, by type and fields, then any other attribute set on the instance (in its
  `__dict__` or a slot, by `__post_init__` or later), by name;
- a value of a class from your own code (a dataclass, a named tuple, an enum, or one you registered) also by its
  class's code version, so editing a method its nodes call misses. A class holding a mutable class attribute cannot
  be versioned, so its values cannot be fingerprinted;
- numpy arrays, by dtype, shape, strides and content, so a Fortran-ordered array or a strided view differs from its
  C-ordered copy; pandas and polars frames and series, by their columns' names, types and
  values and their name, and a pandas one by its index (with a datetime index's frequency) and `attrs` too (polars
  has none). fnloom never imports these libraries
  itself: it reads their values through the modules your code already loaded.

Anything else cannot be fingerprinted, at any depth, and is never replaced by a constant, a `repr` or an `id`: a
node reading it runs, and `explain_cache` names its type and where it sits in the value.

```python
# file: sizes_book.py
def total_size(levels: list[object]) -> int:
    """The size at every level of the book."""
    return sum(level.size for level in levels)
```

```python
import sizes_book


class Level:
    """A price level, which fnloom cannot fingerprint until told how."""

    __slots__ = ("price", "size")

    def __init__(self, price: float, size: int) -> None:
        self.price = price
        self.size = size


book = (
    fnloom.Builder().with_modules(sizes_book).with_cache(fnloom.MemoryCache()).build()
)
levels = [Level(100.0, 5), Level(100.5, 3)]
book.explain_cache(["total_size"], inputs={"levels": levels})
# -> {"total_size": ("uncacheable", "levels[0]: Level")}
```

**Registering more.** A class you own can define `__fnloom_fingerprint__`, returning something that can itself be
fingerprinted. For a class you do not own, `fnloom.register_fingerprint(cls, fn)` does the same; it applies to
subclasses too, the most specific registration first.

```python
fnloom.register_fingerprint(Level, lambda level: (level.price, level.size))
book.explain_cache(["total_size"], inputs={"levels": levels})
# -> {"total_size": ("miss", None)}
```

The form you return is fingerprinted under the value's own type, so two classes returning the same tuple still
differ. It must hold everything that can change a node's result.

**Limits.** Some values miss more often than you might expect, are fingerprinted by less than all their state, or
are refused although they look plain:

- A set of strings iterates in an order the hash seed decides, which changes with every process unless
  `PYTHONHASHSEED` is set. Its fingerprint follows that order, so an input holding one, or a global `frozenset` of
  strings a node reads, misses in each new process with `DiskCache`. Pass a sorted tuple instead, or fix
  `PYTHONHASHSEED`, to share results across processes.
- An object of a type fnloom does not know that exposes a buffer (a `bytes` subclass, a `ctypes` value) is hashed
  by its type's name, its buffer and the attributes set on the instance, so state held elsewhere (in memory a
  `ctypes` pointer reaches, say) is not seen.
- A dataclass or a named tuple is fingerprinted by what the instance holds and its class's code: state kept
  elsewhere, behind a property reading another object say, is not seen. Register a fingerprint for such a class.
- A value nested more than 1,000 levels deep, or holding itself, is refused.
- Classes are told apart by module and qualified name, so classes made at run time under one name (by
  `dataclasses.make_dataclass` in a loop, say) can share fingerprints.
- `pandas.NaT` and `pytz` time zones are refused; use `zoneinfo` zones.

## Code versions

A node's key covers its **code version**, computed once, when the driver is built, from code objects rather than
source text, so it works in a notebook too. It hashes:

- the function's bytecode, constants and names, nested functions and comprehensions included; not its line numbers,
  comments or docstring, so moving or reformatting a function keeps its version;
- its defaults, fingerprinted at build, and its closure cells, by the rules for globals below;
- every global name it reads: a function or class of your own code by its own code version, recursively; a module of
  your own code by every attribute that any code it reaches reads from a module, whichever function reads it (a helper
  handed the module, say), and by its `__getattr__` for a name it does not define; a function's own attributes it
  reads, by the same rules; a library function, class or module by its name and the version of the installed
  distribution, so upgrading a library misses; an immutable value (an `int`, `float`, `complex`, `bool`, `str`,
  `bytes`, `None`, an enum member, or a tuple or frozenset of these) by its fingerprint.

A name read by a string constant counts as read by name, so `globals()["RATE"]` and `getattr(config, "RATE")` see
`RATE`. Your own code is any module that is neither built in, nor the standard library, nor installed: a module whose
file sits in the interpreter's library directories, or in any `site-packages` or `dist-packages` directory on
`sys.path`, is a library. A project installed in editable mode keeps its files in the project, so it stays your own.

A `@parameterize` row's `value()` constants and its bindings are not part of the code version: they are parameters
of the node's key, fingerprinted once, at build, so `version=` does not replace them and a changed constant misses.

**Anything else makes the function uncacheable**: a list, dict or set, or any other object, held in a global, a
closure or a class attribute, can change between runs without fnloom seeing it. The node then always runs, the build
warns nothing, and `explain_cache` gives the reason, as for `plain_weighted` above. If you know the value does not
change, or change the version yourself when it does, give `@cache(version=...)`.

**Known limits.** A code version cannot see:

- a global **rebound after the build**, by assignment from outside the function or by reloading its module: the
  driver keeps the version read at build until it is rebuilt;
- code reached only by **dynamic import**, `importlib.import_module(name)` or `__import__(name)`;
- a name **built at run time**, as in `getattr(config, prefix + "RATE")`;
- the **iteration order of a set literal** in a function's code (`for x in {"a", "b"}`), which a code version hashes
  in sorted order so that `x in {"a", "b"}`, the common use, keeps its version across processes;
- **reflection at run time** on what the version leaves out: a function or class reading its own docstring, a class's
  generic arguments (`__orig_bases__`), or the annotations of a method;
- a **mutable default mutated after the build**: `def f(x, opts={"k": 1})` is fingerprinted with `{"k": 1}` at
  build, and a later `f.__defaults__[0]["k"] = 2` is not seen.

A node whose result depends on any of these should be given a `version=` or be `recompute`.

## Explaining a run

`Driver.explain_cache(final_vars, *, inputs=None, overrides=None)` takes the request `execute` takes, checks it the
same way, runs nothing, and returns every computed node of the plan, by name, with its `CacheOutcome`, a named tuple of
`status` and `reason`:

| `status` | Means | `reason` |
| --- | --- | --- |
| `hit` | the store holds the node's key | `None` |
| `miss` | it does not, so a run runs and stores the node | `None`, or `"policy recompute"` for a `recompute` node, which always runs and is never stored, or `"extracted field: always recomputed from its producer"` for a field or column node of `@extract_fields` or `@extract_columns` |
| `uncacheable` | the node has no key: it always runs and is never stored | why, for example the global (`"global 'WEIGHTS' of 'f' is a dict, ..."`), the value path (`"levels[0]: Level"`), the policy (`"policy disable"`), a source (`"reads uncacheable 'x'"`) or a failing fingerprint function (`"cannot fingerprint 'x' for the cache: ..."`) |
| `unknown` | the key depends on a `recompute` node's result, which only a run knows | for example `"downstream of recompute node 'x'"`, or `"no key"` |

`explain_cache` consults the store for **every** `default`-policy node with a key, including nodes upstream of a hit,
which a run would neither load nor run. With a `DiskCache`, that reads every such entry from disk.

## Stores

A store is any object with three methods, the `fnloom.CacheStore` protocol: `get(key)` returns the value or
`fnloom.MISSING`, `put(key, value)` stores one, replacing any there, and `discard(key)` forgets one. A key is 32 bytes.
A store must be safe to call from several threads at once, and must never raise for a store error: it misses or skips
the write, and warns with `fnloom.CacheWarning`. A miss is `MISSING`, never `None`, which is a value a node may
return. `with_cache` refuses an object without these three methods. fnloom has two; they live in the private module
`fnloom._stores`, so use them through `fnloom`: `fnloom.MemoryCache`, `fnloom.DiskCache`, `fnloom.CacheStore`,
`fnloom.MISSING` and `fnloom.CacheWarning`. (`fnloom.cache` is the decorator.) One store can be shared by several
drivers in one process. A pickled driver takes its store along: a `DiskCache` reopens the same directory, and a
`MemoryCache` arrives empty (see [Pickling and process pools](running.md#pickling-and-process-pools)).

**`MemoryCache(max_entries=100_000, max_bytes=None)`** holds the results themselves, so a hit costs a lookup and no
copy (hence the [purity contract](#the-purity-contract)). Past either bound it evicts the least recently used entries.
`len(store)` is the number of entries it holds. Its entries live in the native extension: a run walks a plain
`MemoryCache` and stores into it without calling Python per node. A subclass is used through its methods, like any
other store.
`max_bytes` counts each value by `sys.getsizeof` of the object itself, not of what it holds, so it is an
approximation: a list of large arrays counts as the list.

**`DiskCache(path, *, max_bytes=None, dumps=pickle.dumps, loads=pickle.loads, serializer="pickle", formats=())`**
keeps one file per key, at `<path>/v1/<first two hex digits of the key>/<key in hex>`. There is no index, so nothing
to lock or corrupt, and a lookup is one file read.

- Each entry starts with a header naming the format version, the serializer, the Python version, and the version of
  the library that defines the value's type. An entry written by another Python, serializer or library version is a
  quiet miss, and the file is left for the next write to replace: a read never deletes another interpreter's entry;
  a write still replaces it. One directory per interpreter is the better layout.
- An entry that cannot be read back (truncated, corrupt, or naming a class that moved) is a miss: it is removed, with
  a `CacheWarning`, and the node runs.
- Writes go to a temporary file in the same directory that then replaces the entry, so a reader sees a whole entry
  or none, and of two writers of one key the last one wins.
- The header names the library of the value's own type only: a list of DataFrames is recorded as a list, so after
  a pandas upgrade its entry is read back by the new pandas, or found unreadable and removed.
- Without `max_bytes` the directory grows without bound, and entries of another Python or library version stay until
  a write replaces them. With `max_bytes`, the least recently read entries are removed until the rest fit, when the
  store opens and after every 100 writes. `prune(max_bytes)` does it on demand, and `clear()` removes every entry,
  warning about any it cannot.
- A write is not synced to disk. After a crash or a power loss an entry may be torn, and a torn entry is found
  unreadable, so it is a miss, never a wrong value.
- `dumps` and `loads` replace pickle. Give `serializer` a new name whenever you change them: entries written under
  another name are misses.

**Formats.** A node can pick how `DiskCache` writes its result with `@cache(format=...)`: a `fnloom.Format`, which is
a name with a `dumps` and `loads` pair, or the name of one the store knows. Every `DiskCache` knows `"pickle"` and
`"json"`, and its `formats=` argument adds more. A name the store doesn't know is refused when the driver is built,
naming the node, not when the node first runs (Q55). A `MemoryCache` holds values, never bytes, so it takes any format
and ignores it. An entry records its format's name, and an entry read with another format is a miss. Only `"pickle"`
and `"json"` are built in, since fnloom has no dependencies: write a `Format` for Parquet or CSV with the library
you already use.

The `"json"` format stores dicts with `str` keys, lists, `str`, `int`, finite `float`, `bool` and `None`, each of
exactly that type, so a hit returns the value the node computed. A value that JSON would load back as something else,
such as a tuple, which would come back as a list, is not stored: the write is skipped with a `CacheWarning` naming
where the value is, and the node runs again next time (Q54). Loading JSON runs no code, unlike pickle.

```python
# file: summary.py
from fnloom import cache


@cache(format="json")
def summary(bid: float, ask: float) -> dict[str, float]:
    """The quote, stored as JSON that any tool can read."""
    return {"bid": bid, "ask": ask}
```

```python
import tempfile

import summary

store = fnloom.DiskCache(tempfile.mkdtemp())
built = fnloom.Builder().with_modules(summary).with_cache(store).build()
built.execute(["summary"], inputs={"bid": 99.0, "ask": 101.0})
# -> {'summary': {'bid': 99.0, 'ask': 101.0}}
sorted(store.formats)
# -> ['json', 'pickle']
```

!!! warning "Loading a pickle can run code"

    The default serializer is pickle, and loading a pickle runs whatever code it names. Whoever can write to the cache
    directory can run code in every process that reads it. Keep the directory as private as the code itself.
    `DiskCache` makes its directories readable and writable by their owner only, and on POSIX warns when it opens an
    existing directory that other users can write or that another user owns. Windows controls access with ACLs, which
    `DiskCache` does not inspect, so there it never warns: check the directory's permissions yourself.

```python
import shutil

directory = tempfile.mkdtemp()
quote = {"bid": 99.0, "ask": 101.0}
writer = fnloom.Builder().with_modules(quotes).with_cache(fnloom.DiskCache(directory))
writer.build().execute(["relative_spread"], inputs=quote)
# -> {'relative_spread': 0.02}
reader = fnloom.Builder().with_modules(quotes).with_cache(fnloom.DiskCache(directory))
reader.build().explain_cache(["relative_spread"], inputs=quote)["relative_spread"]
# -> ("hit", None)
shutil.rmtree(directory)
shutil.rmtree(folder)
```

The second driver stands in for a later process: it reads the entries the first one wrote.

**The cache never fails a run.** A store error, a corrupt entry, a value that cannot be pickled or a full disk is a
miss or a skipped write, reported with a `CacheWarning`. A node's own exception still fails the run, as without the
cache, and nothing is stored for it.
