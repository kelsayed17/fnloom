"""Namespaces: a module's nodes under a dotted prefix (Q74)."""


def qualified(namespace: str | None, name: str) -> str:
    """`name` under `namespace`: `ns.name`, or `name` itself without one."""
    return name if namespace is None else f"{namespace}.{name}"


def checked_namespace(namespace: object) -> str | None:
    """`namespace` when it is `None` or a non-empty dotted identifier path.

    `TypeError` for anything but a `str` or `None`; `ValueError` for a `str` that
    is not such a path, as `""`, `"a."` or `"1a"` are not.
    """
    if namespace is None:
        return None
    if not isinstance(namespace, str):
        msg = f"namespace must be a str or None, got {type(namespace).__name__}"
        raise TypeError(msg)
    if not all(part.isidentifier() for part in namespace.split(".")):
        msg = (
            f"namespace {namespace!r} is not a dotted identifier path, such as 'a' "
            "or 'a.b'"
        )
        raise ValueError(msg)
    return namespace
