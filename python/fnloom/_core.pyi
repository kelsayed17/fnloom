import concurrent.futures
import contextvars
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal, Never, NoReturn, Self, final, override

__all__ = [
    "ABSENT",
    "Absent",
    "Graph",
    "Held",
    "KeyPlan",
    "Keys",
    "Limits",
    "MemoryStore",
    "ObserverBase",
    "Plan",
    "Stepper",
    "Stop",
    "StoppedError",
    "Threads",
    "UnfingerprintableError",
    "_panic_for_test",
    "arm_hook_panic",
    "arm_perform_panic",
    "arm_report_panic",
    "arm_state_panic",
    "arm_stepper_panic",
    "arm_worker_panic",
    "core_version",
    "fingerprint",
    "hash_parts",
    "held_slots",
    "live_workers",
]

def core_version() -> str: ...
def fingerprint(
    value: object, fallback: Callable[[object], object | None]
) -> bytes: ...
def hash_parts(parts: Sequence[bytes]) -> bytes: ...

class StoppedError(RuntimeError):
    # A parallel run stopped because the run that started it stopped or ended.
    ...

class UnfingerprintableError(TypeError):
    path: str
    type_name: str

@final
class Absent:
    # `ABSENT` is the only instance: calling the class is a `TypeError`.
    def __new__(cls, never: Never, /) -> NoReturn: ...
    def __bool__(self) -> NoReturn: ...
    @override
    def __reduce__(self) -> str: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: object, /) -> Self: ...

ABSENT: Final[Absent]

type Kind = Literal["required", "optional", "admits", "admits_optional"]
type CollectMode = Literal["present", "all"]

# Compiled only with the `test-hooks` feature, which release builds never enable.
def _panic_for_test() -> None: ...
def live_workers() -> int: ...
def arm_worker_panic() -> None: ...
def arm_hook_panic() -> None: ...
def arm_report_panic() -> None: ...
def arm_perform_panic() -> None: ...
def arm_stepper_panic() -> None: ...
def arm_state_panic() -> None: ...
def held_slots(limits: Limits) -> tuple[int, dict[str, int], dict[str, int]]: ...

@final
class Graph:
    def __new__(
        cls,
        nodes: Sequence[
            tuple[
                str,
                list[tuple[str, Kind, CollectMode | None]],
                Mapping[str, str | tuple[str, ...]],
                Callable[..., Any],
                bool,
                Callable[..., Any] | None,
            ]
        ],
        renames: dict[str, dict[str, str]] | None = None,
        absent_on: dict[str, tuple[type[BaseException], ...]] | None = None,
    ) -> Self: ...
    def node_names(self) -> list[str]: ...
    def may_be_absent(self) -> list[str]: ...
    def absent_on_nodes(self) -> list[str]: ...
    def blocks(self) -> dict[str, list[str]]: ...
    def upstream_of(self, names: list[str]) -> list[str]: ...
    def downstream_of(self, names: list[str]) -> list[str]: ...
    def between(self, a: str, b: str) -> list[str]: ...
    def prepare(
        self, outputs: list[str], inputs: list[str], overrides: list[str]
    ) -> Plan: ...

@final
class Plan:
    def run(
        self,
        inputs: dict[str, Any],
        overrides: dict[str, Any],
        observers: Sequence[ObserverBase] = (),
        on_value: Callable[[str, object], None] | Keys | None = None,
        late: object | None = None,
        *,
        threads: Threads | None = None,
        limits: Limits | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]: ...
    def steps(self) -> list[tuple[str, str]]: ...
    def late(self) -> list[str]: ...
    def blocks(self) -> dict[str, list[tuple[str, str]]]: ...
    def start(
        self,
        inputs: dict[str, Any],
        overrides: dict[str, Any],
        observers: Sequence[ObserverBase] = (),
        on_value: Callable[[str, object], None] | None = None,
        late: object | None = None,
        *,
        limits: Limits | None = None,
        threads: Threads | None = None,
        pooled: Sequence[str] = (),
        in_flight: int = ...,
    ) -> Stepper: ...

type Work = Literal["compute", "pull", "finish", "close"]
type Completion = Literal["value", "error", "refused", "done", "dropped"]
type Handed = tuple[int, str, tuple[int, ...], Work, Any, contextvars.Context | None]

@final
class Held:
    def release(self) -> None: ...

@final
class Stepper:
    def next(self) -> Handed | int: ...
    def complete(self, task: int, kind: Completion, value: object, /) -> bool: ...
    def stop(self) -> None: ...
    def detach(self, task: int, /) -> Held | None: ...
    @property
    def pooling(self) -> bool: ...
    @property
    def cancelled(self) -> Stop: ...
    def outputs(self) -> dict[str, object]: ...

@final
class Limits:
    def __new__(
        cls,
        graph: Graph,
        max_workers: int | None = None,
        node_caps: Mapping[str, int] | None = None,
        node_groups: Mapping[str, str] | None = None,
        group_caps: Mapping[str, int] | None = None,
    ) -> Self: ...
    def add_waker(self, waker: Callable[[], object], /) -> None: ...

@final
class Threads:
    def __new__(cls, limits: Limits, max_workers: int) -> Self: ...
    @property
    def max_workers(self) -> int: ...
    def close(self) -> None: ...
    def submit(
        self, function: Callable[[], object], context: contextvars.Context, /
    ) -> concurrent.futures.Future[Any]: ...
    def abandon(self) -> None: ...
    @staticmethod
    def leave() -> None: ...

@final
class Stop:
    # A run's stop flag: true once the run is stopping. Made by the binding only.
    def __new__(cls, never: Never, /) -> NoReturn: ...
    def __bool__(self) -> bool: ...

@final
class KeyPlan:
    def __new__(
        cls,
        nodes: list[tuple[str, bytes | None, list[str], bool, list[str], list[str]]],
        outputs: Sequence[str] = (),
    ) -> Self: ...
    def keys(self, versions: dict[str, bytes | None]) -> dict[str, bytes | None]: ...
    def walk(
        self, versions: dict[str, bytes | None], store: MemoryStore
    ) -> tuple[Keys, dict[str, object]]: ...

@final
class Keys: ...

@final
class MemoryStore:
    def __new__(
        cls,
        max_entries: int,
        max_bytes: int | None = None,
        sizeof: Callable[[object], int | None] | None = None,
    ) -> Self: ...
    def get(self, key: bytes, default: object) -> object: ...
    def put(self, key: bytes, value: object) -> None: ...
    def discard(self, key: bytes) -> None: ...
    def __len__(self) -> int: ...
    def items(self) -> list[tuple[bytes, object]]: ...

class ObserverBase:
    def __new__(cls, *_args: object, **_kwargs: object) -> Self: ...
    def pre_graph(self) -> None: ...
    # A block node's `pre_node` and `post_node` also get `branch=`, its branch path;
    # a node outside every block is called without it.
    def pre_node(
        self,
        node: str,
        kwargs: dict[str, object],
        *,
        branch: tuple[int, ...] = ...,
    ) -> None: ...
    def post_node(
        self,
        node: str,
        *,
        ok: bool,
        value: object,
        error: BaseException | None,
        branch: tuple[int, ...] = ...,
    ) -> None: ...
    def hit_in(self, node: str, *, branch: tuple[int, ...], value: object) -> None: ...
    def skipped(self, node: str, *, branch: tuple[int, ...]) -> None: ...
    def absent_error(
        self, node: str, *, branch: tuple[int, ...], error: BaseException
    ) -> None: ...
    def post_expand(
        self, node: str, *, branch: tuple[int, ...], items: int
    ) -> None: ...
    def post_graph(self, *, ok: bool) -> None: ...
