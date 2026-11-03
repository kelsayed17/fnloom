"""The documentation's samples run, and its links lead somewhere.

* Every Python block in ``README.md`` and ``docs/*.md`` runs. A block whose first line
  is ``# file: <name>.py`` is written out as that module, importable by the blocks after
  it; every other block runs, in order, in one namespace per page. A comment line
  ``# -> <value>`` under a statement says what the statement evaluates to, and
  ``# raises <Error>: <message>`` says what it raises; both are checked.
* Every relative link in the Markdown files names a file that exists, and a heading in
  it when it names one.
* Every link into the docs site names a page under ``docs/``, and a heading in it when
  it names one.
"""

import ast
import importlib
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
# Pages whose samples show the current API. The design and the plan are records.
SAMPLE_PAGES = [ROOT / "README.md", *sorted(DOCS.glob("*.md"))]
ROOT_PAGES = ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md")
LINK_PAGES = [
    *(ROOT / name for name in ROOT_PAGES),
    ROOT / "benches" / "README.md",
    *sorted(DOCS.rglob("*.md")),
]

FENCE = re.compile(r"^```(\w*)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
MODULE_HEADER = re.compile(r"\A# file: (\w+)\.py\n")
RESULT = re.compile(r"^# -> (?P<expected>.+)$")
RAISES = re.compile(r"^# raises (?P<error>\w+): (?P<message>.+)$")
LINK = re.compile(r"\]\(([^)\s]+)\)|href=\"([^\"]+)\"")
SITE_LINK = re.compile(
    r"https://kelsayed17\.github\.io/fnloom/([\w/.-]*?)/?(?:#([\w-]+))?(?=[\"\s)]|$)"
)
HEADING = re.compile(r"^#{1,6} +(.+?) *$", re.MULTILINE)


def _python_blocks(page: Path) -> list[str]:
    """Returns the bodies of the Python blocks on `page`, in order."""
    text = page.read_text(encoding="utf-8")
    return [body for language, body in FENCE.findall(text) if language == "python"]


def _checked(code: str) -> str:
    """Rewrites each statement a marker comment follows into a check of itself."""
    # A blank line after the last statement, so every statement has a line after it.
    lines = [*code.splitlines(), ""]
    out: list[str] = []
    for statement in ast.parse(code).body:
        source = ast.get_source_segment(code, statement) or ""
        # `end_lineno` is 1-based, so as an index it is the line after the statement.
        marker = lines[statement.end_lineno or 0]
        if result := RESULT.match(marker):
            out.append(f"_expect(({source}), {result['expected']}, {source!r})")
        elif raised := RAISES.match(marker):
            out.append(f"with _raises({raised['error']}, {raised['message']!r}):")
            out.extend(f"    {line}" for line in source.splitlines())
        else:
            out.append(source)
    return "\n".join(out)


def _expect(actual: object, expected: object, statement: str) -> None:
    """Checks a statement marked `# -> value`."""
    assert actual == expected, f"{statement}\n  gave {actual!r}"


def _raises(error: type[BaseException], message: str) -> AbstractContextManager[object]:
    """Checks a statement marked `# raises Error: message`.

    pytest matches against the notes too, so the message must be the first line.
    """
    return pytest.raises(error, match=rf"\A{re.escape(message)}(?:\n|\Z)")


@pytest.fixture
def module_dir(tmp_path: Path) -> Iterator[Path]:
    """A directory on `sys.path` for the modules a page writes."""
    sys.path.insert(0, str(tmp_path))
    yield tmp_path
    sys.path.remove(str(tmp_path))


@pytest.mark.parametrize("page", SAMPLE_PAGES, ids=[page.name for page in SAMPLE_PAGES])
def test_every_python_sample_runs(page: Path, module_dir: Path) -> None:
    """Writes the page's modules, then runs its other blocks in order."""
    namespace: dict[str, object] = {"_expect": _expect, "_raises": _raises}
    for number, block in enumerate(_python_blocks(page), start=1):
        if header := MODULE_HEADER.match(block):
            (module_dir / f"{header[1]}.py").write_text(block, encoding="utf-8")
            importlib.invalidate_caches()
            continue
        code = compile(_checked(block), f"{page.name} block {number}", "exec")
        exec(code, namespace)  # noqa: S102  # running the docs' own samples is the point


def test_the_guides_have_samples() -> None:
    """Finds samples on the pages, so an emptied page cannot pass unnoticed."""
    assert _python_blocks(ROOT / "README.md")
    assert sum(len(_python_blocks(page)) for page in SAMPLE_PAGES) > len(SAMPLE_PAGES)


def test_markers_follow_the_statement_they_check() -> None:
    """Rewrites a marked statement, however many lines it spans, and leaves others."""
    code = "x = 1\nf(\n    x,\n)\n# -> 2\ng(x)\n# raises ValueError: no\nh(x)\n"
    assert _checked(code) == (
        "x = 1\n"
        "_expect((f(\n    x,\n)), 2, 'f(\\n    x,\\n)')\n"
        "with _raises(ValueError, 'no'):\n"
        "    g(x)\n"
        "h(x)"
    )


def _slug(heading: str) -> str:
    """Returns a heading's anchor: lower case, no punctuation, spaces as hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.lower())
    return text.strip().replace(" ", "-")


def _anchors(page: Path) -> set[str]:
    """Returns the anchors of the headings on `page`."""
    return {_slug(heading) for heading in HEADING.findall(page.read_text("utf-8"))}


def _targets(page: Path) -> list[str]:
    """Returns every link target on `page`, Markdown and HTML alike."""
    text = page.read_text(encoding="utf-8")
    return [markdown or html for markdown, html in LINK.findall(text)]


LINK_IDS = [str(page.relative_to(ROOT)) for page in LINK_PAGES]


@pytest.mark.parametrize("page", LINK_PAGES, ids=LINK_IDS)
def test_every_relative_link_resolves(page: Path) -> None:
    """Finds the file each relative link names, and the heading its anchor names."""
    broken: list[str] = []
    for target in _targets(page):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        linked = (page.parent / file_part).resolve() if file_part else page
        has_anchor = linked.suffix != ".md" or not anchor or anchor in _anchors(linked)
        if not (linked.exists() and has_anchor):
            broken.append(target)
    assert not broken, f"{page.name}: broken links {broken}"


@pytest.mark.parametrize("page", LINK_PAGES, ids=LINK_IDS)
def test_every_docs_site_link_resolves(page: Path) -> None:
    """Finds the page each docs-site link names, and the heading its anchor names."""
    broken: list[str] = []
    for match in SITE_LINK.finditer(page.read_text(encoding="utf-8")):
        guide = DOCS / f"{match[1] or 'index'}.md"
        if not guide.exists() or (match[2] and match[2] not in _anchors(guide)):
            broken.append(match[0])
    assert not broken, f"{page.name}: broken docs-site links {broken}"


# The comparison tables of `from-hamilton.md` whose rows each name their evidence.
COMPARISON_TABLES = ("Decorators", "What changes in your code", "Caching")
REGISTER = DOCS / "quirk-register.md"
REGISTER_ROW = re.compile(r"^\| (Q\d+) \|", re.MULTILINE)
CITED = re.compile(r"\bQ\d+\b")
EVIDENCE = re.compile(
    r"^@pytest\.mark\.(?:deliberate|keeps)\(\"(Q\d+)\"\)$", re.MULTILINE
)
SEPARATOR = re.compile(r"^\|\s*:?-{3,}")


def _comparison_rows() -> list[tuple[str, str]]:
    """Each body row of the comparison tables, with its table's heading."""
    rows: list[tuple[str, str]] = []
    heading = ""
    for line in (DOCS / "from-hamilton.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            # Any heading ends a table's section, so a subsection's tables don't count.
            heading = line.removeprefix("## ") if line.startswith("## ") else ""
        elif heading in COMPARISON_TABLES and line.startswith("|"):
            rows.append((heading, line))
    # Drop each table's header row: the one followed by its `| --- |` separator.
    return [
        row
        for row, after in zip(rows, [*rows[1:], ("", "")], strict=True)
        if not SEPARATOR.match(row[1]) and not SEPARATOR.match(after[1])
    ]


def test_every_comparison_row_cites_a_register_row() -> None:
    """Each comparison row names at least one register row, and only real ones."""
    known = set(REGISTER_ROW.findall(REGISTER.read_text(encoding="utf-8")))
    rows = _comparison_rows()
    assert len(rows) >= len(COMPARISON_TABLES)
    uncited = [f"{table}: {row[:80]}" for table, row in rows if not CITED.search(row)]
    unknown = sorted({q for _, row in rows for q in CITED.findall(row)} - known)
    assert not uncited, "rows citing no register row:\n" + "\n".join(uncited)
    assert not unknown, f"cited but not in the register: {unknown}"


def test_every_cited_row_has_a_differential_test() -> None:
    """Each register row a comparison row cites has a `deliberate` or `keeps` test."""
    tested = {
        q
        for path in (ROOT / "tests" / "differential").glob("test_*.py")
        for q in EVIDENCE.findall(path.read_text(encoding="utf-8"))
    }
    cited = {q for _, row in _comparison_rows() for q in CITED.findall(row)}
    untested = sorted(cited - tested, key=lambda q: int(q[1:]))
    assert not untested, f"cited rows with no differential test: {untested}"
