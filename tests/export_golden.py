"""The export of a graph with no node that may be absent, which the tests pin (Q95).

`fnloom.export/1` stays what it was before `fnloom.export/2` (Q135): `text()` is
compared with `golden/export_1.json`, which matches `main`'s output before `/2`,
its hashes aside. The graph is `export_golden_graph`, so these helpers are no nodes.
"""

import json
from typing import Any

import export_golden_graph
import fnloom


def text() -> str:
    """The whole graph's export and one run's, as JSON, with no hash.

    fnloom's version, each code version and each `graph_version` are placeholders:
    a code version hashes the interpreter's build and fnloom's release (the
    library parts a function reads), so it differs between machines and releases.
    `hashes()` gives them, to check apart.
    """
    found = documents()
    for document in found:
        document["fnloom"] = "<version>"
        document["graph_version"] = "<hash>"
        for node in document["nodes"]:
            if node["version"] is not None:
                node["version"] = "<hash>"
    return json.dumps(found, indent=2) + "\n"


def hashes() -> list[object]:
    """Every `graph_version` and code version the placeholders stand for."""
    found: list[object] = []
    for document in documents():
        found.append(document["graph_version"])
        found.extend(
            node["version"] for node in document["nodes"] if node["kind"] == "function"
        )
    return found


def documents() -> list[dict[str, Any]]:
    """The whole graph's export, and one run's."""
    built = fnloom.build(export_golden_graph)
    return [
        built.export(),
        built.export(["report"], inputs={"region": "n", "units": 1}),
    ]
