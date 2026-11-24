"""The thirty-two removed tool names, held out of the released surface and the docs.

The thirty-two are every row of the frozen decision table whose disposition is
not `retain-specialized`, plus the retained rows a `withdrawn` record withdrew;
both are read off the table. Seventeen are also live Python function names (a
supported API whose MCP wrappers are gone), so a bare-name grep would fail on
correct prose. The rule has three parts:

1. **No registration.** None of the thirty-two is decorated `@mcp.tool()` in
   `server.py`, read off the AST.
2. **No tool-name string in a consumer.** No string literal equal to one of them
   in the server, the agent, the VS Code extension, the shipped templates or the
   CLI; literals are read, not prose.
3. **No document sentence describing one as an MCP tool.** A mention is a
   violation when an MCP marker (`_MARKERS`) appears within a few lines; a name
   that denotes nothing any more may appear only in a sentence that says it is
   gone (`_HISTORICAL`).

The split between the seventeen and the other fifteen is computed from the source
tree, so a deleted function moves its name into the stricter class by itself.
Records of what happened are excluded (the decision records, the plans, the
frozen editing-protocol contract and the release notes);
`test_no_exclusion_covers_current_guidance` pins that no exclusion reaches
current guidance.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from decision_table import load_projections, removed

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_PATH = REPO_ROOT / "packages" / "mcp" / "src" / "swcraft_mcp" / "server.py"

# ---------------------------------------------------------------------------
# The thirty-two, and the two classes they fall into
# ---------------------------------------------------------------------------

#: Everything the frozen decision table does not retain, and every retained row
#: a `withdrawn` record withdrew.
REMOVED_TOOLS: frozenset[str] = removed(load_projections())

#: The trees a surviving function could legitimately live in.
_SOURCE_ROOTS = ("packages/tools/src", "packages/mcp/src", "packages/agent/src")


def _names_still_defined_in_python() -> frozenset[str]:
    """Which of the thirty-two still name a real function somewhere."""
    found: set[str] = set()
    for root in _SOURCE_ROOTS:
        for path in sorted(((REPO_ROOT / root).rglob("*.py")), key=lambda path: path.as_posix()):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in REMOVED_TOOLS:
                    found.add(node.name)
    return frozenset(found)


#: Names that survive as `swcraft_tools` functions — legitimate in prose, so
#: only the MCP-marker rule applies to them.
SURVIVING_FUNCTION_NAMES = _names_still_defined_in_python()

#: Names that denote nothing at all any more. Mentioning one is fine only while
#: saying that it is gone.
DANGLING_NAMES = REMOVED_TOOLS - SURVIVING_FUNCTION_NAMES

# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"\b(" + "|".join(sorted(REMOVED_TOOLS)) + r")\b")

#: What turns a mention into a claim about the MCP surface. The bare word "tool"
#: is not here: prose discusses writer functions beside tooling.
_MARKERS = re.compile(
    r"\bMCP\b|@mcp\.tool|mcp_server|swcraft_mcp|mcp\.json|tools/call|tools/list|\btool surface\b"
    r"|\bwrite tools?\b|\bread tools?\b"
)

#: Vocabulary that marks a mention as a statement about the past. One of these
#: must appear near a dangling name.
_HISTORICAL = re.compile(
    r"\bremoved\b|\bremoves\b|\bgone\b|\bformer\b|\bformerly\b|\bused to\b|\bno longer\b"
    r"|\bpre-19\.3\b|\bsuperseded\b|\bsupersedes\b|\bretired\b|\bretires\b|\bwas\b|\bwere\b"
    r"|\buntil\b|\bPhase 16\b|\bPhase 19\.3\b|\b19\.3\b",
    re.IGNORECASE,
)

#: How many lines from the mention the marker may be: a sentence spans a few
#: wrapped lines, while a whole markdown block can be a 1000-line listing.
_WINDOW = 2

#: Records of what happened. Never current guidance — see the module docstring
#: and `test_no_exclusion_covers_current_guidance`.
EXCLUDED_DOC_PATHS = (
    "docs/adr/",
    "docs/plans/",
    "docs/checklists/phase16-design-ledger.md",
    "docs/contracts/editing-protocol/",
    "docs/release-notes/",
    "docs/guide/release-notes/",
)

#: The scanned surface: everything read as current in the open trees. Every other document is
#: scanned by the closed half of this module with this module's rule.
# Public: the closed half of this module reads it.
SCANNED_DOC_ROOTS = ("docs/guide", "docs/release-notes")
_SCANNED_DOC_FILES = (
    "README.md",
    "examples/basic/.github/copilot-instructions.md",
    "packages/agent/src/swcraft_agent/agent-system-prompt.md",
    # The agent task benchmark's two READMEs, open test data.
    "packages/tools/tests/fixtures/agent-bench/README.md",
    "packages/tools/tests/fixtures/agent-bench/bench/imports/README.md",
)

#: Where a tool name would really be sent over the wire.
_CONSUMER_PATHS = (
    "packages/mcp/src",
    "packages/agent/src",
    "vscode_extension/src",
    "packages/tools/src/swcraft_tools/templates",
    "packages/tools/src/swcraft_tools/cli",
)


#: Scanned although an exclusion reaches them: a frozen directory's living index is current guidance.
RE_INCLUDED_DOC_PATHS = ("docs/contracts/editing-protocol/README.md",)


# Public: the closed half of this module calls it.
def is_excluded(relative: str) -> bool:
    if relative in RE_INCLUDED_DOC_PATHS:
        return False
    return any(relative == excluded or relative.startswith(excluded) for excluded in EXCLUDED_DOC_PATHS)


def _scanned_documents() -> list[Path]:
    paths: list[Path] = []
    for root in SCANNED_DOC_ROOTS:
        paths.extend(sorted((REPO_ROOT / root).rglob("*.md"), key=lambda path: path.as_posix()))
    for name in _SCANNED_DOC_FILES:
        candidate = REPO_ROOT / name
        if candidate.exists():
            paths.append(candidate)
    kept = [p for p in paths if not is_excluded(p.relative_to(REPO_ROOT).as_posix())]
    assert kept, "the document scan found nothing — the roots or the exclusions are wrong"
    return kept


def _window(lines: list[str], index: int) -> str:
    start = max(0, index - _WINDOW)
    return "\n".join(lines[start : index + _WINDOW + 1])


def _violations_in_lines(lines: list[str], relative: str) -> list[str]:
    """Every line of *lines* that names one of the thirty-two unacceptably.

    Over text rather than a file, so :func:`test_the_document_rule_bites`
    exercises this very function rather than a copy.
    """
    found: list[str] = []
    for index, line in enumerate(lines):
        names = set(_NAME_RE.findall(line))
        if not names:
            continue
        context = _window(lines, index)
        if _MARKERS.search(context):
            found.append(f"{relative}:{index + 1}: {sorted(names)} framed as an MCP tool -- {line.strip()}")
            continue
        dangling = names & DANGLING_NAMES
        if dangling and not _HISTORICAL.search(context):
            found.append(
                f"{relative}:{index + 1}: {sorted(dangling)} names nothing that exists, "
                f"and the sentence does not say it is gone -- {line.strip()}"
            )
    return found


# Public: the closed half of this module calls it.
def document_violations(path: Path) -> list[str]:
    """The rule over one real document."""
    return _violations_in_lines(
        path.read_text(encoding="utf-8").splitlines(),
        path.relative_to(REPO_ROOT).as_posix(),
    )


# ---------------------------------------------------------------------------
# 1. No registration
# ---------------------------------------------------------------------------


def _registered_tool_names() -> set[str]:
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    registered: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                registered.add(node.name)
    return registered


def test_no_removed_tool_is_registered() -> None:
    """Part 1 of the rule, read off `server.py`'s AST."""
    still_registered = sorted(_registered_tool_names() & REMOVED_TOOLS)
    assert not still_registered, f"removed tools are still registered: {still_registered}"


def test_the_class_split_is_derived_and_covers_all_thirty_two() -> None:
    """The two classes partition the table's thirty-two, and both are non-empty."""
    assert len(REMOVED_TOOLS) == 32
    # Pinned so the module docstring's figures cannot go stale unnoticed.
    assert len(SURVIVING_FUNCTION_NAMES) == 17
    assert len(DANGLING_NAMES) == 15
    # The three withdrawn tools keep their functions.
    assert {
        "check_port_compatibility",
        "check_connector_consistency",
        "export_all_components",
    } <= SURVIVING_FUNCTION_NAMES
    assert SURVIVING_FUNCTION_NAMES | DANGLING_NAMES == REMOVED_TOOLS
    assert not (SURVIVING_FUNCTION_NAMES & DANGLING_NAMES)
    # Both halves must be populated, or one of the two rules is silently inert.
    assert SURVIVING_FUNCTION_NAMES, "no removed name survives as a function -- the marker rule is untested"
    assert DANGLING_NAMES, "every removed name still exists as a function -- the historical rule is untested"


# ---------------------------------------------------------------------------
# 2. No tool-name string in a consumer
# ---------------------------------------------------------------------------


def _consumer_files() -> list[Path]:
    suffixes = {".py", ".ts", ".js", ".json", ".md", ".txt"}
    paths: list[Path] = []
    for entry in _CONSUMER_PATHS:
        target = REPO_ROOT / entry
        if target.is_file():
            paths.append(target)
        else:
            paths.extend(
                sorted(
                    (p for p in target.rglob("*") if p.is_file() and p.suffix in suffixes),
                    key=lambda path: path.as_posix(),
                )
            )
    assert paths, "the consumer scan found nothing -- the paths are wrong"
    return paths


#: A quoted string equal to one of the names, in any of the scanned languages.
_LITERAL_RE = re.compile(r"[\"'](" + "|".join(sorted(REMOVED_TOOLS)) + r")[\"']")


def test_no_consumer_carries_a_removed_tool_name_as_a_string() -> None:
    """Part 2: prose may recall a removed tool; a string literal may not name one."""
    offenders: list[str] = []
    for path in _consumer_files():
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            for match in _LITERAL_RE.finditer(line):
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{index + 1}: {match.group(1)}")
    assert not offenders, "removed MCP tool names are still sent by a consumer:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# 3. No `docs/` sentence describing one as an MCP tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _scanned_documents(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_no_current_document_presents_a_removed_tool_as_an_mcp_tool(path: Path) -> None:
    """Part 3, per document, so a failure names the file it is in."""
    offenders = document_violations(path)
    assert not offenders, "\n".join(offenders)


def test_the_document_rule_bites() -> None:
    """Both failing shapes fail, and both passing ones pass, through the scan's own :func:`_violations_in_lines`."""

    def check(text: str) -> list[str]:
        return _violations_in_lines(text.splitlines(), "synthetic.md")

    # (a) a surviving function name, framed as an MCP tool
    assert check("The MCP surface answers reads.\nCall `read_interfaces` for the layer.")
    # (b) the same name with no MCP framing is fine — it is a real function
    assert not check("`reader.read_interfaces()` parses every interface file.")
    # (c) a dangling name presented as if it existed
    assert check("Create the component by calling `add_swc` with its group.")
    # (d) the same name in a sentence that says it is gone
    assert not check("Phase 19.3 removed `add_swc`; `change_set` carries a create instead.")
    # (e), a real, current document still passing, is the closed half's.


def test_no_exclusion_covers_current_guidance() -> None:
    """No exclusion reaches current prompts or user guidance; such a document is fixed, not excluded."""
    # Every named file, so a moved or renamed one fails here instead of dropping out of the scan.
    must_be_scanned = _SCANNED_DOC_FILES
    for relative in must_be_scanned:
        assert (REPO_ROOT / relative).exists(), relative
        assert not is_excluded(relative), f"{relative} is current guidance and may not be excluded"

    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in _scanned_documents()}
    for relative in must_be_scanned:
        assert relative in scanned, f"{relative} is not reached by the document scan"
