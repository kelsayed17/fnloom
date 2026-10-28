from collections.abc import Callable, Sequence
from typing import Any, Self, final

__all__ = [
    "Graph",
    "KeyPlan",
    "Keys",
    "MemoryStore",
    "ObserverBase",
    "Plan",
    "UnfingerprintableError",
    "_panic_for_test",
    "core_version",
    "fingerprint",
    "hash_parts",
]

def core_version() -> str: ...
def fingerprint(
    value: object, fallback: Callable[[object], object | None]
) -> bytes: ...
def hash_parts(parts: Sequence[bytes]) -> bytes: ...

class UnfingerprintableError(TypeError):
    path: str
    type_name: str

# Compiled only with the `test-hooks` feature, which release builds never enable.
def _panic_for_test() -> None: ...

@final
class Graph:
    def __new__(
        cls,
        nodes: list[
            tuple[str, list[tuple[str, bool]], dict[str, str], Callable[..., Any]]
        ],
        renames: dict[str, dict[str, str]] | None = None,
    ) -> Self: ...
    def node_names(self) -> list[str]: ...
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
    ) -> dict[str, object]: ...
    def steps(self) -> list[tuple[str, str]]: ...

@final
class KeyPlan:
    def __new__(
        cls,
        nodes: list[tuple[str, bytes | None, list[str], bool, list[str]]],
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
    def pre_node(self, node: str) -> None: ...
    def post_node(self, node: str, *, ok: bool) -> None: ...
    def post_graph(self, *, ok: bool) -> None: ...
