"""``scripts/history_markers.py``: the marker kinds, the units and ``check``, over planted texts."""

from __future__ import annotations

import functools
import os
from pathlib import Path

import pytest
from conftest import history_markers_module
from scratch_git import git
from symlink_support import symlink_or_skip

_MARKERS = history_markers_module()


def _found(text: str, path: str = "notes.md") -> list[tuple[str, str]]:
    return [(hit.kind, hit.text) for hit in _MARKERS.scan_text(path, text, with_units=True)]


# ---------------------------------------------------------------------------
# The kinds
# ---------------------------------------------------------------------------


_POSITIVE = [
    ("phase", "Phase 23"),
    ("phase", "phase 18"),
    ("adr", "ADR"),
    ("adr", "ADRs"),
    ("review", "review round"),
    ("review", "Codex iterations"),
    ("review", "third codex review"),
    ("review", "post-execution review"),
    ("review", "plan-review"),
    ("review", "Codex's verdict"),
    ("review", "MAJOR 4"),
    ("review", "finding F-A5"),
    ("review", "C-B3"),
    ("decided-by", "maintainer's decision"),
    ("decided-by", "(the maintainer"),
    ("decided-by", "the maintainer,"),
    ("decided-by", "coordinator's ruling"),
    ("decided-by", "accepted by the coordinator"),
    ("decided-by", "(the coordinator"),
    ("plan", "Task 3"),
    ("plan", "slice 32.7"),
    ("plan", "wave 6"),
    ("plan", "plan 14a"),
    ("plan", "execution log"),
    ("plan", "findings row 4"),
    ("plan", "row 3 of the findings"),
    ("plan", "verify run 597"),
    ("plan", "Decision 4"),
    ("requirement-id", "FR-14"),
    ("requirement-id", "NFR-2"),
    ("closed-path", "docs/concepts"),
    ("closed-path", "internal/tests"),
    ("closed-path", ".gitea/"),
    ("closed-path", "docs/agent"),
    ("closed-path", "CLAUDE.md §"),
    ("closed-path", "CLAUDE.md's invariant"),
    ("closed-path", "011-model-contract.md"),
    ("closed-path", "20261116-the-plan.md"),
    ("closed-path", "-execution-log"),
    ("decision-id", "D7"),
    ("decision-id", "P1"),
    ("decision-id", "N10"),
    ("decision-id", "M2"),
    ("decision-id", "D7.1"),
]


@pytest.mark.parametrize(("kind", "marker"), _POSITIVE)
def test_each_kind_finds_its_marker_in_prose(kind: str, marker: str) -> None:
    assert _found(f"See {marker} here.\n") == [(kind, marker)]


_PRODUCT_WORDS = [
    "Codex runs its hooks",
    "the reviewer approves",
    "a round trip",
    "Round 1: initial build attempt",
    "a second review",
    "the final review",
    "the planner decides every target",
    "the maintainer of a project",
    "the coordinator process",
    ".claude/settings.json",
    ".github/agents/x.agent.md",
    "swcraft init writes CLAUDE.md",
    "[A-Z0-9]+",
    "FINDINGS 0 error(s)",
    "a phase shift",
    "the task list",
    "#D1",
    "x.D1",
    "docs/agent-system-prompt.md",
    "internal/tests-notes.md",
]


@pytest.mark.parametrize("text", _PRODUCT_WORDS)
def test_a_product_word_is_no_marker(text: str) -> None:
    assert _found(f"{text}\n") == []


def test_a_marker_split_across_two_comment_lines_is_one_hit_on_its_first_line() -> None:
    hits = _MARKERS.scan_text("tool.py", "# since Phase\n# 23 the rule holds\nx = 1\n")
    assert [(hit.kind, hit.line, hit.text) for hit in hits] == [("phase", 1, "Phase 23")]


# ---------------------------------------------------------------------------
# The decision id: prose only, never a code span, never an id the code uses
# ---------------------------------------------------------------------------


def test_a_decision_id_in_code_is_no_marker() -> None:
    element = "D1"
    assert _found(f'element = DataElement("{element}")\n', "gen.py") == []


def test_a_comment_naming_the_value_the_code_holds_is_no_marker() -> None:
    partition = "P1"
    text = f'PARTITION = "{partition}"  # B -> {partition}: the partition\n'
    assert _found(text, "alloc.py") == []


def test_an_id_in_a_code_span_is_no_marker_anywhere_in_the_file() -> None:
    first, second = "P1", "P2"
    assert _found(f"Pass `{first},{second},...` as the list.\n", "guide.md") == []
    assert _found(f"Pass `{first}` here; {first} is the first.\n", "guide.md") == []


def test_an_svg_path_is_no_marker() -> None:
    assert _found('<svg><path d="M11.1 17.2"/></svg>\n', "icon.svg") == []


def test_a_decision_id_in_a_comment_alone_is_a_marker() -> None:
    marker = "D38"
    assert _found(f"x = 1  # bounded by {marker}\n", "tool.py") == [("decision-id", marker)]


def test_a_decision_id_in_a_python_string_is_code_and_counts_nowhere_in_the_file() -> None:
    marker = "K1"
    assert _found(f'CASE = "{marker}"\n# {marker} is the first case\n', "tool.py") == []


_STANDARD_TERMS = [
    ("a.ts", "/** CIE76 colour difference of two opaque colours (sRGB, D65). */\n"),
    ("a.md", "The white point is the D50 illuminant.\n"),
    ("a.py", "# the AUTOSAR M1 class's own short name\n"),
    ("a.md", "An AUTOSAR M2 class and its M1 instances.\n"),
    ("a.md", "The meta-level M2 defines the templates.\n"),
]


@pytest.mark.parametrize(("path", "text"), _STANDARD_TERMS)
def test_a_standard_term_spelled_like_a_decision_id_is_no_marker(path: str, text: str) -> None:
    assert _found(text, path) == []


_IDS_WITHOUT_THE_CONTEXT = [
    ("a.ts", "// bounded by D65\n", "D65"),
    ("a.md", "D50 decided the white space.\n", "D50"),
    ("a.py", "# M1 decided it\n", "M1"),
    ("a.md", "As M2 says, the type stays.\n", "M2"),
    ("a.md", "See M4 level here.\n", "M4"),
    ("a.md", "The sRGB conversion follows D7.\n", "D7"),
    ("a.md", "M2 elements were cut.\n", "M2"),
    ("a.md", "The level M1 decided.\n", "M1"),
    ("a.md", "M2 types are deferred.\n", "M2"),
    ("a.md", "M3 objects stay.\n", "M3"),
    ("a.md", "The AUTOSAR schema is read; M2 elements were cut.\n", "M2"),
    ("a.md", "See D65 in the CIE note; D50 decided it.\n", "D50"),
]


@pytest.mark.parametrize(("path", "text", "marker"), _IDS_WITHOUT_THE_CONTEXT)
def test_the_same_id_without_its_standard_context_is_a_marker(path: str, text: str, marker: str) -> None:
    assert _found(text, path) == [("decision-id", marker)]


# ---------------------------------------------------------------------------
# The units per suffix
# ---------------------------------------------------------------------------


_UNITS = [
    ("a.md", "prose\n```\ncode\n```\nprose\n", {1, 5}),
    ("a.md", "prose\n~~~\ncode\n~~~\nprose\n", {1, 5}),
    ("a.md", "prose\n````\n```\ncode\n```\n````\nprose\n", {1, 7}),
    ("a.md", "prose\n```\n~~~\nmid\n```\nprose\n", {1, 6}),
    ("a.md", "prose\n```\n``` info\nmid\n```\nprose\n", {1, 6}),
    ("a.md", "```code```\nprose\n", {1, 2}),
    ("a.md", "> ```\n> code\n> ```\nprose\n", {4}),
    ("a.md", "- ```sh\n  code\n  ```\nprose\n", {4}),
    ("a.md", "1. Run:\n\n    ```sh\n    code\n    ```\nprose\n", {1, 2, 6}),
    ("a.md", "prose\n\n    ```\nprose\n", {1, 2, 3, 4}),
    ("a.md", "prose\n\n\t```\nprose\n", {1, 2, 3, 4}),
    ("a.md", "prose\n   ```\ncode\n```\nprose\n", {1, 5}),
    ("a.md", "- item\n\n  more\n\n    ```\n    code\n    ```\nprose\n", {1, 2, 3, 4, 8}),
    ("a.md", "- item\n\nprose\n\n    ```\nprose\n", {1, 2, 3, 4, 5, 6}),
    ("a.py", '"""Doc.\n\nmore\n"""\nx = 1  # why\ny = "# no"\n', {1, 2, 3, 4, 5}),
    ("a.ts", "// c\nconst a = 1; // why\nconst b = 2;\n/* one\n   two */\nconst c = 3;\n", {1, 2, 4, 5}),
    ("a.mjs", " * doc\nlet x = 1;\n", {1}),
    ("a.ts", "const a = 2\n  * 3;\n", {2}),
    ("a.toml", "# c\nkey = 1\n", {1}),
    ("a.yml", "key: 1 # c\nother: 2\n", {1}),
    ("a.yml", "a: \"# no\"\nb: '#no' # c\nc: it's # c\n", {2, 3}),
    (".gitignore", "# c\n*.pyc\n", {1}),
    ("a.xml", "<a>\n<!-- one\ntwo -->\n<b/>\n", {2, 3}),
    ("a.xml", "<a><!-- c --></a>\n<b/>\n", {1}),
    ("a.py", 'x = "\u2028\\f"\n# c\n', {2}),
    ("a.json", '{"a": 1}\n', set()),
    ("a.py", "def broken(:\n# c\n", set()),
]


@pytest.mark.parametrize(("path", "text", "prose"), _UNITS)
def test_the_prose_units_of_each_suffix(path: str, text: str, prose: set[int]) -> None:
    assert _MARKERS.units(path, text).prose == prose


def test_a_comment_on_a_docstring_line_is_counted_once() -> None:
    marker = "D7"
    assert _found(f'"""Doc."""  # {marker}\n', "a.py") == [("decision-id", marker)]


def test_a_decision_id_after_a_long_fence_holding_a_short_one_is_a_marker() -> None:
    text = "````\n```\nD7 in code\n```\n````\nD8 decided it\n"
    assert _found(text, "guide.md") == [("decision-id", "D8")]


def test_an_inline_triple_backtick_span_opens_no_fence() -> None:
    assert _found("```code```\nD8 decided it\n", "guide.md") == [("decision-id", "D8")]


def test_a_fence_inside_a_blockquote_is_code() -> None:
    assert _found("> ```\n> D7 in code\n> ```\n", "guide.md") == []


def test_a_fence_line_in_an_indented_code_block_hides_no_prose_after_it() -> None:
    assert _found("Run:\n\n    ```\n    D7 in the block\nD8 decided it\n", "guide.md") == [
        ("decision-id", "D7"),
        ("decision-id", "D8"),
    ]


def test_an_indented_code_block_is_read_as_prose() -> None:
    assert _found("Text\n\n    ```\n    D7\n", "guide.md") == [("decision-id", "D7")]


def test_an_indented_fence_in_a_list_item_still_hides_its_code() -> None:
    assert _found("1. Run:\n\n    ```\n    D7 in code\n    ```\nD8 decided it\n", "guide.md") == [("decision-id", "D8")]


def test_a_fence_opened_on_a_list_item_closes_on_its_indented_line() -> None:
    assert _found("- ```sh\n  D7 in code\n  ```\nD8 decided it\n", "guide.md") == [("decision-id", "D8")]


def test_unequal_backtick_runs_make_no_code_span() -> None:
    marker = "D7"
    assert _found(f"Pass ``{marker}` here.\n", "guide.md") == [("decision-id", marker)]


def test_a_continued_python_string_with_crlf_line_ends_holds_no_comment() -> None:
    assert _found('TEXT = "a\\\r\n# D1 in the string"\r\n', "a.py") == []


def test_a_hash_inside_a_quoted_yaml_value_is_no_comment() -> None:
    assert _found('value: "# D7"\n', "a.yml") == []


def test_only_the_comment_of_an_xml_line_is_prose() -> None:
    assert _found('<path d="M1 0"/><!-- note -->\n', "icon.svg") == []


def test_a_character_python_breaks_no_line_at_misplaces_no_comment() -> None:
    assert _found('x = "\u2028"\n# bounded by D7\n', "a.py") == [("decision-id", "D7")]


def test_a_kept_literal_keeps_its_exemption_beside_a_trailing_comment() -> None:
    text = 'NAME = "Phase 9"  # the input, Phase 9\n'
    hits = _MARKERS.scan_text("a.py", text, with_units=True)
    assert [(hit.kind, hit.prose) for hit in hits] == [("phase", False), ("phase", True)]
    key = ("phase", "a.py", "Phase 9")
    used, left = _MARKERS.counted(hits, {key: "an input"})
    assert used == {key}
    assert [(hit.kind, hit.prose) for hit in left] == [("phase", True)]


def test_a_byte_order_mark_hides_no_python_comment(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes("\ufeffx = 1  # bounded by D38\n".encode())
    # A file a KEPT entry names is read with its units, which decide the hit's unit.
    hits = _MARKERS.scan(tmp_path, ["a.py"], kept={("decision-id", "a.py", "D38"): "an input"})
    assert [(hit.kind, hit.text, hit.prose) for hit in hits] == [("decision-id", "D38", True)]


def test_a_hash_inside_a_python_string_is_no_comment() -> None:
    marker = "Phase 9"
    hits = _MARKERS.scan_text("a.py", f'TEXT = "# {marker}"\n', with_units=True)
    assert [(hit.kind, hit.prose) for hit in hits] == [("phase", False)]


# ---------------------------------------------------------------------------
# check, on a planted tree
# ---------------------------------------------------------------------------


def _tree(root: Path, files: dict[str, str]) -> list[str]:
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    return sorted(files)


_ONE = "Phase 1 only\n"


def test_a_counted_hit_fails_check_with_its_place_and_text(tmp_path: Path) -> None:
    paths = _tree(tmp_path, {"tools/a.md": "clean\n", "tools/b.py": "x = 1\n# bounded by Phase 4\n"})
    assert _MARKERS.check(tmp_path, paths, {}) == ["tools/b.py:2: phase Phase 4"]


def test_a_kept_hit_passes_check(tmp_path: Path) -> None:
    literal = "Phase 4"
    paths = _tree(tmp_path, {"tools/a.py": f'INPUT = "{literal}"\n'})
    assert _MARKERS.check(tmp_path, paths, {}) == [f"tools/a.py:1: phase {literal}"]
    assert _MARKERS.check(tmp_path, paths, {("phase", "tools/a.py", literal): "an input the gate refuses"}) == []


def test_a_kept_entry_does_not_pass_the_same_text_in_prose(tmp_path: Path) -> None:
    literal = "Phase 4"
    paths = _tree(tmp_path, {"tools/a.py": f'INPUT = "{literal}"  # since {literal}\n'})
    kept = {("phase", "tools/a.py", literal): "an input the gate refuses"}
    assert _MARKERS.check(tmp_path, paths, kept) == [f"tools/a.py:1: phase {literal}"]


def test_a_symbolic_link_and_a_file_holding_a_nul_byte_are_skipped(tmp_path: Path) -> None:
    paths = _tree(tmp_path, {"tools/a.md": _ONE})
    (tmp_path / "tools/bin.md").write_bytes(_ONE.encode("utf-8") + b"\0")
    symlink_or_skip(tmp_path / "tools/link.md", Path("a.md"))
    files = [path for path, _ in _MARKERS.read_open_files(tmp_path, [*paths, "tools/bin.md", "tools/link.md"])]
    assert files == ["tools/a.md"]


# ---------------------------------------------------------------------------
# KEPT
# ---------------------------------------------------------------------------


def test_a_kept_entry_silences_its_code_hit_and_not_its_prose_hit(tmp_path: Path) -> None:
    closed = "internal/tests"
    paths = _tree(tmp_path, {"tools/a.py": f'TREE = "{closed}"\n', "tools/b.py": f"# {closed}\n"})
    kept = {("closed-path", "tools/a.py", closed): "a tree the tool names", ("closed-path", "tools/b.py", closed): "x"}
    used, left = _MARKERS.counted(_MARKERS.scan(tmp_path, paths, kept=kept), kept)
    assert [(hit.path, hit.kind) for hit in left] == [("tools/b.py", "closed-path")]
    assert used == {("closed-path", "tools/a.py", closed)}


def test_check_refuses_an_unused_kept_entry_and_one_without_a_reason(tmp_path: Path) -> None:
    paths = _tree(tmp_path, {"tools/a.py": "x = 1\n"})
    problems = _MARKERS.check(tmp_path, paths, {("adr", "tools/a.py", "ADR"): " "})
    assert len(problems) == 2
    assert "silences no hit" in problems[0] and "no reason" in problems[1]


# ---------------------------------------------------------------------------
# The command line on a git repository: the expansion
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A one-commit repository whose allow-list opens ``tools/`` and itself, with a closed ``notes/``."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "source"
    _tree(root, {"public-paths.txt": "tools/\npublic-paths.txt\n", "tools/a.md": _ONE, "notes/n.md": "Phase 2\n"})
    git("init", "--quiet", cwd=root)
    _commit(root)
    return root


def _commit(root: Path) -> None:
    git("add", "--all", cwd=root)
    git("commit", "--quiet", "-m", "state", cwd=root)


def test_check_on_the_command_line_exits_1_naming_the_hit_and_the_remedy(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _MARKERS.main(["--repo-root", str(repo), "check"]) == 1
    lines = capsys.readouterr().out.splitlines()
    # The planted tree holds none of the real KEPT entries' files, so each of them is also reported unused.
    assert [line for line in lines if not line.startswith("KEPT entry ")] == [
        "tools/a.md:1: phase Phase 1",
        _MARKERS.FAILURE_HELP,
    ]


def test_check_on_the_command_line_exits_0_on_a_tree_at_zero(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(repo, {"tools/a.md": "No marker here.\n"})
    _commit(repo)
    # The planted tree holds none of the real KEPT entries' files, so the command runs with no entry.
    monkeypatch.setattr(_MARKERS, "check", functools.partial(_MARKERS.check, kept={}))
    assert _MARKERS.main(["--repo-root", str(repo), "check"]) == 0
    assert capsys.readouterr().out == "history markers: none\n"


def test_a_repository_without_the_allow_list_exits_1_with_one_line_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _MARKERS.main(["--repo-root", str(tmp_path), "check"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("history markers: ")
    assert len(captured.err.splitlines()) == 1
