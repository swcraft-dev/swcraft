"""Hold the public files at zero development-record markers.

    python scripts/history_markers.py check

A marker is text a reader of the open copy cannot follow: a numbered phase, an architecture decision record, a
numbered review or finding, a ruling and its author, a plan's task or slice, a roadmap requirement id, a path into a
closed document, or a plan's decision id (:data:`KINDS`). The files are the expansion of ``public-paths.txt`` at
``HEAD``, read from disk, but a symbolic link, a file holding a NUL byte and the gate's own three files
(:data:`EXEMPT`, which spell out every marker they define). Each kind but the decision id is matched in the whole
text with line breaks joined; the decision id only in prose (comments, docstrings, Markdown outside code), and never
when the same id stands in the file's code. A hit listed in :data:`KEPT` and standing outside prose is not counted.

``check`` lists every counted hit and fails on one, and on a :data:`KEPT` entry that silences none or has no
reason.

What the gate does not reach: names (of tests, classes, files and folders); history written without a marker, a date
or a commit id in prose; a closed document named by its bare file name; a decision id in code, or one the file's
code also uses; a TypeScript comment after code without `` // ``; Markdown and YAML/TOML read line by line, so a
code span across lines and a multi-line scalar or string may be misread, a Markdown list is taken to end at a line
at column 0 after a blank line, and an indented code block's lines are read as prose (an id there counts); an
unnumbered review or finding; the product words the patterns exclude and the standard terms :data:`_STANDARD_TERMS`
reads in their context, when used for history; a literal a :data:`KEPT` entry silences, whatever else it says; a
file outside the expansion, a binary file or a link's target; the gate's own three files; the quality of a
docstring; and a file not yet committed (the expansion is read at ``HEAD``).
"""

from __future__ import annotations

import argparse
import ast
import bisect
import functools
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import public_snapshot

REPO_ROOT = Path(__file__).resolve().parent.parent

_ORD = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|final|\d+(?:st|nd|rd|th))"

#: What a text needs before a pattern can match in it: word groups, each of which must have a word in
#: the lower-cased text, or a regular expression that must be found in it.
_Need = tuple[tuple[str, ...], ...] | re.Pattern[str]

_MAINTAINER: _Need = (("maintainer",),)
_COORDINATOR: _Need = (("coordinator",),)
_FINDING_ROW: _Need = (("finding",), ("row",))

#: The whole-text kinds, each pattern beside its need. A pattern whose need fails is left out of its
#: kind's alternation, which leaves the alternation's matches as they are and keeps the scan fast.
_PATTERNS: Mapping[str, Sequence[tuple[str, _Need]]] = {
    "phase": [(r"(?i:\bphase\s+\d+)", (("phase",),))],
    "adr": [(r"\bADRs?\b", (("adr",),))],
    "review": [
        (
            r"(?i:\b(?:review|codex|external)\s+(?:round|phase|iteration|cycle)s?\b)",
            (("review", "codex", "external"), ("round", "phase", "iteration", "cycle")),
        ),
        (rf"(?i:\b{_ORD}\s+(?:post-execution\s+|external\s+|plan-|codex\s+)review\b)", (("review",),)),
        (r"(?i:\bpost-execution\s+review)", (("post-execution",),)),
        (r"\bplan-review\b", (("review",),)),
        (r"\bCodex(?:'s)?\s+(?:round|iteration|blocker|finding|review|verdict)", (("codex",),)),
        (r"\b(?:BLOCKER|MAJOR|MINOR)\s+\d+\b", (("blocker", "major", "minor"),)),
        (r"(?!FINDINGS\b)(?i:\bfindings?\s+(?:[A-Z]{1,2}-)?[A-Z]?\d+[a-z]?\b)", (("finding",),)),
        (r"(?<!\[)\b[A-Z]-[A-Z]\d+\b", re.compile(r"[A-Z]-[A-Z]\d")),
    ],
    "decided-by": [
        (
            r"(?i:\bmaintainer(?:'s)?\s+(?:word|words|decision|decided|ruling|ruled|reading|re-?walk|walk|call|choice"
            r"|chose|said|asked|accepted|amendment|answer|condition|rule|question|correction|verdict|review|note)\b)",
            _MAINTAINER,
        ),
        (r"(?i:\(the maintainer\b)", _MAINTAINER),
        (r"(?i:\bthe maintainer,)", _MAINTAINER),
        (
            r"(?i:\bcoordinator(?:'s)?\s+(?:decision|decisions|ruling|rulings|word|reading|call|amendment|answer"
            r"|choice)\b)",
            _COORDINATOR,
        ),
        (r"(?i:\b(?:accepted|decided|ruled)\s+by\s+the\s+coordinator)", _COORDINATOR),
        (r"(?i:\(the coordinator\b)", _COORDINATOR),
    ],
    "plan": [
        (r"\bTask\s+\d+[a-z]?\b", (("task",),)),
        (r"(?i:\bslice\s+\d+(?:\.\d+)*\b)", (("slice",),)),
        (r"(?i:\bwave\s+\d+\b)", (("wave",),)),
        (r"(?i:\bplan\s+\d+[a-z]?\b)", (("plan",),)),
        (r"(?i:\bexecution\s+log\b)", (("execution",),)),
        (r"(?i:\bfindings?\b[^.\n]{0,30}?\brow\s+\d+)", _FINDING_ROW),
        (r"(?i:\brow\s+\d+\s+of\s+the\s+findings)", _FINDING_ROW),
        (r"(?i:\b(?:verify|quick|push|main|CI)\s+run\s+\d+)", (("run",), ("verify", "quick", "push", "main", "ci"))),
        (r"\bDecision\s+\d+\b", (("decision",),)),
    ],
    "requirement-id": [(r"\bN?FR-\d+\b", (("fr-",),))],
    "closed-path": [
        (
            r"(?<![\w/.-])(?:docs/(?:concepts|plans|adr|checklists|scenarios|agent|contracts)(?![\w-])"
            r"|internal/(?:tests|scripts|ci)(?![\w-])|\.gitea/)",
            (("docs/", "internal", "gitea"),),
        ),
        (r"\bCLAUDE\.md(?:\s*§|'s\s+(?:§|invariant|table|row|rule))", (("claude.md",),)),
        (r"(?<![\w/.-])\d{3}-[a-z0-9-]+\.md\b", ((".md",),)),
        (r"\b20\d{6}-[\w.-]+\.md\b", ((".md",),)),
        (r"-execution-log\b", (("-log",),)),
    ],
}

#: The whole-text kinds, as regular expressions.
KINDS: Mapping[str, Sequence[str]] = {kind: [pattern for pattern, _ in entries] for kind, entries in _PATTERNS.items()}

#: The prose-only kind's name and pattern.
DECISION_ID = "decision-id"
_DECISION_ID = re.compile(r"(?<![\w$#.-])[DMNPK]\d{1,2}(?:\.\d+)?(?![\w-])")

#: Standard terms spelled like a decision id, as ``(context, term)``: an id the *term* pattern's named group
#: matches is the standard's term, not a marker, when the *context* pattern is found in the same clause (the text
#: between two of ``. ; : ! ?``). A CIE standard illuminant in a clause naming colour (CIE, sRGB, an illuminant, a
#: white point); a meta-level of the AUTOSAR (MOF) model hierarchy right after "AUTOSAR", "MOF" or "meta-level", or
#: right before "meta-level", "metamodel", "class" or "instance", in a clause naming AUTOSAR, MOF, a meta-level or a
#: metamodel. The same id without its context is still a marker.
_STANDARD_TERMS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (
        re.compile(r"(?i:\b(?:CIE|sRGB|illuminants?|white\s+points?)\b)"),
        re.compile(r"\b(?P<id>D(?:50|55|65|75))\b"),
    ),
    (
        re.compile(r"(?i:\b(?:AUTOSAR|MOF|meta[-\s]?levels?|meta-?models?)\b)"),
        re.compile(
            r"(?i:\b(?:AUTOSAR|MOF|meta[-\s]?level))\s+(?P<after>M[0-3])\b"
            r"|\b(?P<before>M[0-3])(?=[\s-]+(?i:meta[-\s]?levels?|meta-?models?|class(?:es)?|instances?)\b)"
        ),
    ),
)
_CLAUSE = re.compile(r"[^.;:!?]+")


def _standard_term_starts(segment: str) -> set[int]:
    """Return the start of every id in *segment* that :data:`_STANDARD_TERMS` reads as a standard's term."""
    starts: set[int] = set()
    for clause in _CLAUSE.finditer(segment):
        text = clause.group(0)
        for context, term in _STANDARD_TERMS:
            if context.search(text):
                starts.update(clause.start() + match.start(match.lastgroup or 0) for match in term.finditer(text))
    return starts


def _met(need: _Need, text: str, lowered: str) -> bool:
    if isinstance(need, re.Pattern):
        return need.search(text) is not None
    return all(any(word in lowered for word in group) for group in need)


@functools.cache
def _compiled(kind: str, chosen: tuple[int, ...]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{KINDS[kind][index]})" for index in chosen))


def _alternation(kind: str, text: str, lowered: str) -> re.Pattern[str] | None:
    """Return *kind*'s alternation of the patterns that can match in *text*, or ``None`` when none can."""
    chosen = tuple(index for index, (_, need) in enumerate(_PATTERNS[kind]) if _met(need, text, lowered))
    return _compiled(kind, chosen) if chosen else None


_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
#: A fence line: blockquote markers, the indentation, list markers, the run. Any indentation is read, since a fence
#: in a list item is indented; outside a list only up to three spaces open one (:func:`_markdown_units`).
_FENCE = re.compile(r"(?: {0,3}> ?)*(?P<indent>[ \t]*)(?P<items>(?:(?:[-*+]|\d{1,9}[.)])\s+)*)(`{3,}|~{3,})")
#: A line's blockquote markers, its indentation, and whether a list marker follows.
_LINE_START = re.compile(r"(?: {0,3}> ?)*(?P<indent>[ \t]*)(?P<item>(?:[-*+]|\d{1,9}[.)])(?:\s|$))?")

_HASH_SUFFIXES = frozenset({".toml", ".yml", ".yaml", ".cff", ".ini", ".cfg", ".txt"})
_HASH_NAMES = frozenset({".gitignore", ".gitattributes", ".prototools", ".env.example"})
_SCRIPT_SUFFIXES = frozenset({".ts", ".js", ".mjs", ".cjs"})
_XML_SUFFIXES = frozenset({".arxml", ".xml", ".svg", ".html"})

#: The gate's own files, left out of every scan: they define the patterns, so their tests and pattern
#: sources spell out every marker kind.
EXEMPT = frozenset(
    {
        "scripts/history_markers.py",
        "scripts/tests/test_history_markers.py",
        "packages/tools/tests/test_public_tree_history.py",
    }
)

_HYGIENE = "packages/tools/tests/test_public_tree_hygiene.py"
_SURFACES = "packages/tools/tests/test_user_facing_surfaces.py"
_SNAPSHOT_TESTS = "scripts/tests/test_public_snapshot.py"
_RATCHET_TESTS = "scripts/tests/test_quality_ratchet.py"
_REMOVED_NAMES = "packages/mcp/tests/test_removed_tool_names.py"
_CLOSED_TESTS = "internal/tests"


#: Hits that are right where they are, as ``{(kind, path, matched text): reason}``. An entry silences
#: every hit of its kind and text in its file that stands outside the file's prose.
KEPT: Mapping[tuple[str, str, str], str] = {
    **{
        ("closed-path", _REMOVED_NAMES, f"docs/{folder}"): "the closed documents the removed-names gate leaves out"
        for folder in ("adr", "plans", "checklists", "contracts")
    },
    ("phase", _REMOVED_NAMES, "Phase 19"): "an input of the removed-names gate",
    ("phase", "packages/mcp/tests/test_tool_reference.py", "Phase 14"): (
        "an input the tool-description gate must refuse"
    ),
    ("adr", "packages/tools/tests/support/guide_fixtures.py", "ADR"): "a term the guide's vocabulary gate refuses",
    ("closed-path", "packages/tools/tests/support/guide_fixtures.py", "docs/concepts"): (
        "a path the guide's vocabulary gate refuses"
    ),
    ("adr", "packages/tools/tests/test_guide_surface.py", "ADR"): "an input of the guide's vocabulary gate",
    ("closed-path", "packages/tools/tests/test_guide_surface.py", "docs/concepts"): (
        "an input of the guide's vocabulary gate"
    ),
    ("phase", "packages/tools/tests/test_guide_surface.py", "Phase 25"): "an input of the guide's vocabulary gate",
    ("closed-path", "packages/tools/tests/test_language.py", "docs/plans"): (
        "the closed test-data file the language gate leaves to the closed half"
    ),
    ("closed-path", "packages/tools/tests/test_language.py", "20261009-one-copy-on-the-wire.md"): (
        "the closed test-data file the language gate leaves to the closed half"
    ),
    ("adr", "packages/tools/tests/test_model_contract.py", "ADR"): "an input of the contract-description gate",
    ("phase", "packages/tools/tests/test_model_contract.py", "Phase 9"): "an input of the contract-description gate",
    ("adr", "packages/tools/tests/test_model_contract_wiring.py", "ADR"): (
        "a pattern and the reasons of the contract-prose gate"
    ),
    **{
        ("closed-path", _HYGIENE, value): "an input of the hygiene gate's closed-path rule"
        for value in (
            *(
                f"docs/{folder}"
                for folder in ("concepts", "adr", "contracts", "checklists", "scenarios", "plans", "agent")
            ),
            _CLOSED_TESTS,
            ".gitea/",
            "CLAUDE.md §",
        )
    },
    ("closed-path", "packages/tools/tests/test_template_contents.py", "docs/adr"): (
        "a path a scaffolded project's instructions must not name"
    ),
    **{
        ("closed-path", _SURFACES, value): "an input or the refusal text of the user-facing-surfaces gate"
        for value in (
            "docs/plans",
            "docs/checklists",
            "docs/concepts",
            "docs/adr",
            "20260706-windows-azure-pending-phases.md",
            "20260906-phase23-compositions.md",
        )
    },
    **{
        ("phase", _SURFACES, f"Phase {number}"): "an input of the user-facing-surfaces gate"
        for number in ("17", "18", "24")
    },
    ("phase", _SURFACES, "phase 2"): "ordinary prose the user-facing-surfaces gate must abstain from",
    ("closed-path", "packages/tools/tests/test_guide_reference_model.py", "docs/concepts"): (
        "an input the citation scan must refuse"
    ),
    ("adr", "packages/tools/tests/fixtures/recorded_oracles/registry_tables.json", "ADR"): (
        "a retired layer's row of a table recorded from the code, frozen and never re-recorded"
    ),
    ("closed-path", "ruff.toml", _CLOSED_TESTS): "the lint settings of a closed test file",
    ("closed-path", "ruff.toml", "internal/scripts"): "the lint settings of the closed scripts",
    ("closed-path", "scripts/dev.py", _CLOSED_TESTS): "test-internal runs the closed test tree",
    ("closed-path", "scripts/quality_baseline.json", _CLOSED_TESTS): "the size ratchet's keys of closed test files",
    ("closed-path", "scripts/quality_ratchet.py", _CLOSED_TESTS): "the closed test tree the size ratchet measures",
    ("closed-path", "scripts/tests/test_dev_script.py", _CLOSED_TESTS): "test-internal's arguments in its own test",
    **{
        ("closed-path", _SNAPSHOT_TESTS, value): "an input of the snapshot tool's closed-path tests"
        for value in (
            *(f"docs/{folder}" for folder in ("plans", "checklists", "concepts", "adr", "contracts", "scenarios")),
            ".gitea/",
        )
    },
    **{
        ("closed-path", _RATCHET_TESTS, value): "a closed file the ratchet must classify or leave unmeasured"
        for value in (_CLOSED_TESTS, "docs/concepts", "docs/contracts")
    },
}


@dataclass(frozen=True)
class Hit:
    """One marker: where it stands, its kind, whether its line is prose, and the matched text."""

    path: str
    line: int
    kind: str
    prose: bool
    text: str


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def _suffix_kind(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name in _HASH_NAMES:
        return "hash"
    dot = name.rfind(".")
    suffix = name[dot:] if dot > 0 else ""
    if suffix in (".md", ".py"):
        return suffix[1:]
    if suffix in _SCRIPT_SUFFIXES:
        return "script"
    if suffix in _HASH_SUFFIXES:
        return "hash"
    if suffix in _XML_SUFFIXES:
        return "xml"
    return "none"


@dataclass
class Units:
    """A file's prose lines, its prose segments (code spans blanked), its code, and the code before each comment."""

    prose: set[int]
    segments: list[tuple[int, str]]
    code: list[str]
    code_before: dict[int, str] = field(default_factory=dict)

    def add_code_and_prose(self, number: int, code: str, prose: str) -> None:
        """Record line *number*'s *code* part as code and its *prose* part as prose."""
        self.code.append(code)
        self.code_before[number] = code
        self.add_prose(number, prose)

    def add_prose(self, number: int, text: str) -> None:
        """Record line *number* as prose and keep *text* as a segment with its code spans blanked."""
        self.prose.add(number)
        self.segments.append((number, _CODE_SPAN.sub(self._span, text)))

    def _span(self, match: re.Match[str]) -> str:
        self.code.append(match.group(2))
        return " " * len(match.group(0))


def _markdown_units(lines: list[str], units: Units) -> None:
    # A fence closes only on its own character, at least as long as the opening run, with nothing after;
    # a backtick run with a backtick after it on its line opens none (an inline span, as in CommonMark).
    # Outside a list a run indented four columns or more opens none either: it is an indented code block's
    # line or a paragraph's continuation. A list lasts from its item until a line at column 0 after a blank.
    fence = ""
    in_list = False
    after_blank = True
    for number, line in enumerate(lines, 1):
        if not fence and line.strip():
            start = _LINE_START.match(line)
            if start and start.group("item"):
                in_list = True
            elif start and after_blank and not start.group("indent"):
                in_list = False
        found = _FENCE.match(line)
        run, rest = (found.group(3), line[found.end() :]) if found else ("", "")
        deep = found is not None and not found.group("items") and len(found.group("indent").expandtabs(4)) > 3
        closes = bool(fence) and run.startswith(fence) and not rest.strip()
        opens = not fence and bool(run) and not (run[0] == "`" and "`" in rest) and not (deep and not in_list)
        after_blank = not line.strip()
        if opens or closes:
            fence = run if opens else ""
            units.code.append(line)
        elif fence:
            units.code.append(line)
        else:
            units.add_prose(number, line)


#: A Python string literal or a comment. A ``#`` inside a string is consumed with the string, so every
#: ``comment`` match is a comment, as :mod:`tokenize` finds them, at a fraction of its cost.
_PY_LEXEME = re.compile(
    r"[rRbBuUfF]{0,2}(?:'\'\'(?:\\.|[^\\])*?'\'\'|\"\"\"(?:\\.|[^\\])*?\"\"\""
    r"|'(?:\\\r\n|\\.|[^\\'\n])*'|\"(?:\\\r\n|\\.|[^\\\"\n])*\")|(?P<comment>#[^\r\n]*)",
    re.DOTALL,
)


_NEWLINE = re.compile("\n")


def _string_statement_lines(tree: ast.Module) -> set[int]:
    """Return the lines of every statement that is a bare string; walk statement lists only."""
    lines: set[int] = set()
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        for name in ("body", "orelse", "finalbody", "handlers", "cases"):
            for child in getattr(node, name, ()):
                if isinstance(child, ast.Expr):
                    value = child.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        lines.update(range(child.lineno, (child.end_lineno or child.lineno) + 1))
                elif isinstance(child, ast.AST):
                    pending.append(child)
    return lines


def _python_units(text: str, lines: list[str], units: Units) -> None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        units.code.append(text)
        return
    strings = _string_statement_lines(tree)
    comment_at: dict[int, int] = {}
    starts = [0, *(match.end() for match in _NEWLINE.finditer(text))]
    for match in _PY_LEXEME.finditer(text):
        if match.group("comment") is not None:
            number = bisect.bisect_right(starts, match.start())
            comment_at[number] = match.start() - starts[number - 1]
    for number, line in enumerate(lines, 1):
        if number in strings:
            units.add_prose(number, line)
        elif number in comment_at:
            units.add_code_and_prose(number, line[: comment_at[number]], line[comment_at[number] :])
        else:
            units.code.append(line)


def _script_units(lines: list[str], units: Units) -> None:
    block = False
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if block:
            units.add_prose(number, line)
            block = "*/" not in line
        elif stripped.startswith(("//", "/*", "*")):
            units.add_prose(number, line)
            block = stripped.startswith("/*") and "*/" not in stripped[2:]
        elif " // " in line:
            cut = line.index(" // ")
            units.add_code_and_prose(number, line[:cut], line[cut:])
        else:
            units.code.append(line)


#: A line's code before its first ``#`` outside a closed quoted string.
_HASH_CODE = re.compile(r"""(?:"(?:\\.|[^"\\\n])*"|'[^'\n]*'|[^"'#\n])*""")


def _hash_units(lines: list[str], units: Units) -> None:
    for number, line in enumerate(lines, 1):
        end = next(_HASH_CODE.finditer(line)).end()
        # An unclosed quote (an apostrophe in plain text) opens no string: the first ``#`` starts the comment.
        cut = -1 if end == len(line) else end if line[end] == "#" else line.find("#")
        if cut >= 0:
            units.add_code_and_prose(number, line[:cut], line[cut:])
        else:
            units.code.append(line)


_XML_COMMENT_EDGE = re.compile("(<!--|-->)")


def _xml_units(lines: list[str], units: Units) -> None:
    block = False
    for number, line in enumerate(lines, 1):
        if not block and "<!--" not in line:
            units.code.append(line)
            continue
        code: list[str] = []
        prose: list[str] = []
        for token in _XML_COMMENT_EDGE.split(line):
            opens = token == "<!--" and not block
            (prose if block or opens else code).append(token)
            block = opens or (block and token != "-->")
        units.add_code_and_prose(number, " ".join(code), " ".join(prose))


def units(path: str, text: str) -> Units:
    """Return the prose and code units of *path*'s *text* by its suffix; a Python file that does not parse has none."""
    # By line feeds, as the whole-text kinds count lines; ``splitlines`` also breaks at U+2028 and a form feed.
    lines = text.removesuffix("\n").split("\n")
    result = Units(set(), [], [])
    kind = _suffix_kind(path)
    if kind == "md":
        _markdown_units(lines, result)
    elif kind == "py":
        _python_units(text, lines, result)
    elif kind == "script":
        _script_units(lines, result)
    elif kind == "hash":
        _hash_units(lines, result)
    elif kind == "xml":
        _xml_units(lines, result)
    else:
        result.code.extend(lines)
    return result


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def scan_text(path: str, text: str, *, with_units: bool = False) -> list[Hit]:
    """Return every marker in *path*'s *text*; a hit's ``prose`` is computed only *with_units* (else ``False``)."""
    lowered = text.lower()
    patterns = [(kind, pattern) for kind in KINDS if (pattern := _alternation(kind, text, lowered)) is not None]
    found: list[tuple[int, str, str]] = []
    if patterns:
        joined, starts = public_snapshot.unwrapped(text)
        for kind, pattern in patterns:
            for match in pattern.finditer(joined):
                found.append((bisect.bisect_right(starts, match.start()) + 1, kind, match.group(0)))
    file_units = units(path, text) if with_units or _DECISION_ID.search(text) else None
    if file_units is not None and file_units.segments:
        in_code = set(_DECISION_ID.findall("\n".join(file_units.code)))
        for number, segment in file_units.segments:
            terms = _standard_term_starts(segment)
            found.extend(
                (number, DECISION_ID, match.group(0))
                for match in _DECISION_ID.finditer(segment)
                if match.start() not in terms
            )
        found = [item for item in found if item[1] != DECISION_ID or item[2] not in in_code]
    if file_units is None or not with_units:
        return [Hit(path, line, kind, False, matched) for line, kind, matched in found]
    # On a line of code and a comment the code comes first: its occurrences of a text are the literal ones.
    seen: dict[tuple[int, str], int] = {}
    hits: list[Hit] = []
    for line, kind, matched in found:
        nth = seen[line, matched] = seen.get((line, matched), 0) + 1
        literal = file_units.code_before.get(line, "").count(matched)
        hits.append(Hit(path, line, kind, line in file_units.prose and nth > literal, matched))
    return hits


def read_open_files(repo: Path, paths: Iterable[str]) -> list[tuple[str, str]]:
    """Return ``(path, text)`` of every file of *paths* under *repo*, skipping symbolic links and NUL files."""
    files: list[tuple[str, str]] = []
    for path in paths:
        target = repo / path
        if target.is_symlink() or not target.is_file():
            continue
        data = target.read_bytes()
        if b"\0" not in data:
            files.append((path, data.decode("utf-8-sig", errors="replace")))
    return files


def scan(repo: Path, paths: Iterable[str], *, kept: Mapping[tuple[str, str, str], str] = KEPT) -> list[Hit]:
    """Return every marker in the open *paths* of *repo* but :data:`EXEMPT`; read a *kept* file with its units."""
    unit_paths = {path for _, path, _ in kept}
    hits: list[Hit] = []
    for path, text in read_open_files(repo, [path for path in paths if path not in EXEMPT]):
        hits.extend(scan_text(path, text, with_units=path in unit_paths))
    return hits


def counted(
    hits: Iterable[Hit], kept: Mapping[tuple[str, str, str], str] = KEPT
) -> tuple[set[tuple[str, str, str]], list[Hit]]:
    """Return the :data:`KEPT` entries used and the hits they leave counted (a prose hit is never kept)."""
    used: set[tuple[str, str, str]] = set()
    counted_hits: list[Hit] = []
    for hit in hits:
        key = (hit.kind, hit.path, hit.text)
        if key in kept and not hit.prose:
            used.add(key)
        else:
            counted_hits.append(hit)
    return used, counted_hits


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

FAILURE_HELP = (
    "A marker is rewritten as the reason it stands for, or removed. KEPT takes a literal only, with its reason: "
    "the input or refusal text of a gate that refuses markers, a code reference to a closed tree, text a generator "
    "writes, or a value recorded from the code it guards; a marker in prose is always rewritten."
)


def check(repo: Path, paths: Sequence[str], kept: Mapping[tuple[str, str, str], str] = KEPT) -> list[str]:
    """Return every counted hit and every unused or unreasoned :data:`KEPT` entry as report lines."""
    used, hits = counted(scan(repo, paths, kept=kept), kept)
    problems = [
        f"{hit.path}:{hit.line}: {hit.kind} {hit.text}"
        for hit in sorted(hits, key=lambda hit: (hit.path, hit.line, hit.kind))
    ]
    for key in sorted(set(kept) - used):
        problems.append(f"KEPT entry {key} silences no hit: remove it")
    for key, reason in sorted(kept.items()):
        if not reason.strip():
            problems.append(f"KEPT entry {key} has no reason")
    return problems


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="history_markers.py", description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    # One command, kept as a subcommand so the documented `history_markers.py check` call stays valid.
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    return parser


def _run_check(repo: Path, paths: Sequence[str]) -> int:
    problems = check(repo, paths)
    for line in problems:
        print(line)
    if problems:
        print(FAILURE_HELP)
        return 1
    print("history markers: none")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repo: Path = arguments.repo_root.resolve()
    try:
        return _run_check(repo, public_snapshot.expansion(repo, "HEAD"))
    except public_snapshot.SnapshotError as error:
        print(f"history markers: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
