"""``swcraft guard``'s Kiro dialect: its decision table, closed argument schemas and form (exit 2, one stderr line).

Also the project, the guard inputs, the hand-off directory, the path fallbacks, Node's path readings, the payloads
kiro-cli 3.1.0 sent (``fixtures/kiro_hook/``), the rendering and the command. The helpers the dialects share are
``copilot_guard_support``'s, their project fixtures ``conftest.py``'s.

What this does not reach: whether Kiro honours the answer; no host runs here. Every case that makes a link goes
through ``symlink_or_skip``, so on a Windows runner without the symbolic-link privilege those cases skip and prove
nothing there.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import _write_manifest
from copilot_guard_support import (
    BY_HAND,
    CLAUDE_EDIT_REASON,
    GREP_ARGUMENTS,
    GUARD_INPUT_REASON,
    HANDOFF_REASON,
    HOME_REASON,
    KIRO_WRITE_TOOLS,
    MODEL_READ_REASON,
    NATIVE_DRIVES,
    SHARED_GUARD_INPUTS,
    SHELL_REASON,
    UNJUDGED_MARK,
    UNREADABLE_MANIFESTS,
    captured_payload,
    codex_project,
    decide_claude_tool,
    decide_kiro,
    decide_kiro_bash,
    decide_kiro_sub_agent,
    decide_kiro_tool,
    from_home,
    guard_payload,
    handoff_reviews,
    kiro_deny_line,
    run_guard,
    saved_answer_name,
    swcraft_home,
    write_claude_settings,
    write_codex_hook,
    write_kiro_hooks,
    write_unreadable_manifest,
)
from filesystem_support import accepts_path_segment
from swcraft_tools import cli, copilot_guard, entry, guard_model_paths, kiro_paths, tilde_paths
from symlink_support import symlink_or_skip

#: The payloads kiro-cli 3.1.0 (and, ``ide-*``, the Kiro IDE 1.4.121) sent its ``PreToolUse`` hook, captured and
#: sanitised: the project's path is ``/project``, which :func:`captured_payload` replaces.
_KIRO_FIXTURES = Path(__file__).parent / "fixtures" / "kiro_hook"


#: The end of a Kiro search's denial (the include pattern is how its search tools name a folder).
_KIRO_SEARCH_REASON = (
    f"{MODEL_READ_REASON} Narrow it to a folder outside the model -- for a text search an include pattern such as "
    "`notes/**` -- or use the SWCraft tools."
)


_KIRO_KIRO_ARGV = ("guard", "--host", "kiro")


def _run_kiro(
    stdin: bytes | None, monkeypatch, capsys, cwd: Path, main=entry.main, argv: tuple[str, ...] = _KIRO_KIRO_ARGV
) -> tuple[int, str, str]:
    """:func:`run_guard` with the exit code: 0 when *main* returns, else the code it exits with."""
    try:
        out, err = run_guard(stdin, monkeypatch, capsys, cwd, main, argv)
    except SystemExit as exited:
        captured = capsys.readouterr()
        return int(exited.code or 0), captured.out, captured.err
    return 0, out, err


#: Per judged Kiro tool, a call into the model and the same call outside it (the decision table).
_KIRO_MODEL_AND_OUTSIDE = [
    ("read_file", {"path": "model/swc/Engine.yaml", "offset": None, "limit": None}, {"path": "notes.md"}),
    ("list_directory", {"path": "model", "explanation": "x", "depth": None}, {"path": "notes", "depth": None}),
    ("grep_search", {"query": "x", "includePattern": "model/**"}, {"query": "x", "includePattern": "notes/**"}),
    ("code", {"operation": "search_symbols", "path": "model"}, {"operation": "search_symbols", "path": "notes"}),
    ("code", {"operation": "hover", "file_path": "model/swc/Engine.yaml"}, {"operation": "hover", "file_path": "a.c"}),
    ("fs_write", {"path": "model/swc/X.yaml", "text": "x"}, {"path": "notes/x.txt", "text": "x"}),
    ("fs_append", {"path": "model/swc/Engine.yaml", "text": "x"}, {"path": "notes.md", "text": "x"}),
    (
        "str_replace",
        {"path": "model/swc/Engine.yaml", "oldStr": "a", "newStr": "b", "replace_all": False},
        {"path": "notes.md", "oldStr": "a", "newStr": "b", "replace_all": False},
    ),
    ("delete_file", {"explanation": "x", "targetFile": "model/swc/Engine.yaml"}, {"targetFile": "notes.md"}),
    ("execute_bash", {"command": "cat model/swc/Engine.yaml", "cwd": None}, {"command": "cat notes.md", "cwd": None}),
    (
        "invoke_sub_agent",
        {"name": "g", "prompt": "p", "contextFiles": [{"path": "model/swc/Engine.yaml", "startLine": 1}]},
        {"name": "g", "prompt": "p", "contextFiles": [{"path": "notes.md"}]},
    ),
]


#: Per judged Kiro tool, arguments it is allowed with in :func:`kiro_dir`.
_KIRO_HARMLESS: dict[str, dict[str, Any]] = {
    "read_file": {"path": "notes.md"},
    "list_directory": {"path": "notes", "depth": None},
    "grep_search": {"query": "x", "includePattern": "notes/**"},
    "code": {"operation": "hover", "file_path": "notes.md"},
    "fs_write": {"path": "notes/x.txt", "text": "x"},
    "delete_file": {"targetFile": "notes.md"},
    "execute_bash": {"command": "ls notes"},
    "invoke_sub_agent": {"name": "g", "contextFiles": [{"path": "notes.md"}]},
}


_KIRO_UNNAMED_REASON = (
    "SWCraft: the model guard cannot read this hook call as a JSON object naming its tool, so in this project it is "
    "refused; use the SWCraft tools."
)


_KIRO_NO_OBJECT_NOTE = "Note: the hook payload is not a JSON object, so no tool is named; allowed.\n"


class TestKiroDialect:
    """The decision table: each judged tool into the model denied, outside allowed; the unjudgeable denied."""

    @pytest.mark.parametrize(
        ("tool", "into", "outside"),
        _KIRO_MODEL_AND_OUTSIDE,
        ids=[f"{tool}-{index}" for index, (tool, _, _) in enumerate(_KIRO_MODEL_AND_OUTSIDE)],
    )
    def test_each_judged_tool(self, kiro_dir: Path, tool: str, into: dict, outside: dict) -> None:
        assert not decide_kiro_tool(tool, kiro_dir, **into).allow
        assert decide_kiro_tool(tool, kiro_dir, **outside).allow

    def test_a_read_file_of_a_model_file_is_denied(self, kiro_dir: Path) -> None:
        """``read_file`` is no Copilot read name, so an unchanged ``_decide_call`` would have allowed it."""
        assert decide_kiro_tool("read_file", kiro_dir, path="model/x.yaml") == copilot_guard.Decision(
            allow=False, reason=MODEL_READ_REASON
        )
        assert decide_kiro_tool("READ_FILE", kiro_dir, path="model/x.yaml") == copilot_guard.Decision(
            allow=False, reason=MODEL_READ_REASON
        )

    def test_a_write_names_the_file_it_writes(self, kiro_dir: Path) -> None:
        assert decide_kiro_tool("fs_write", kiro_dir, path="model/swc/X.yaml", text="x") == copilot_guard.Decision(
            allow=False, reason=CLAUDE_EDIT_REASON.format(path="model/swc/X.yaml")
        )
        assert decide_kiro_tool("delete_file", kiro_dir, targetFile=str(kiro_dir / "model")) == copilot_guard.Decision(
            allow=False, reason=CLAUDE_EDIT_REASON.format(path="model")
        )

    @pytest.mark.parametrize("tool", ["read_file", "fs_write", "fs_append", "str_replace", "delete_file"])
    @pytest.mark.parametrize("arguments", [{}, {"path": None}, {"path": ""}, {"targetFile": None}])
    def test_a_read_or_write_naming_no_path_is_denied(self, kiro_dir: Path, tool: str, arguments: dict) -> None:
        decision = decide_kiro_tool(tool, kiro_dir, **arguments)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    @pytest.mark.parametrize("value", ["model/swc/Engine.yaml", "notes.md", ["model/x"], {"path": "model/x"}, 3, None])
    @pytest.mark.parametrize("tool", list(_KIRO_HARMLESS))
    def test_an_undeclared_key_of_any_value_is_denied(self, kiro_dir: Path, tool: str, value: object) -> None:
        """A closed schema per tool: a key it does not declare denies the call, whatever its value.

        That holds for a harmless value too, beside harmless arguments the tool alone would be allowed with."""
        assert decide_kiro_tool(tool, kiro_dir, **_KIRO_HARMLESS[tool]).allow
        decision = decide_kiro_tool(tool, kiro_dir, **_KIRO_HARMLESS[tool], paths=value)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    @pytest.mark.parametrize("value", [["notes.md"], {"x": "notes.md"}, 3, True])
    @pytest.mark.parametrize(
        ("tool", "key"), [("read_file", "path"), ("fs_write", "path"), ("delete_file", "targetFile")]
    )
    def test_a_path_key_that_is_no_string_is_denied(self, kiro_dir: Path, tool: str, key: str, value: object) -> None:
        decision = decide_kiro_tool(tool, kiro_dir, **{key: value})
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_an_unknown_glob_key_on_a_search_is_denied(self, kiro_dir: Path) -> None:
        assert not decide_kiro_tool(
            "grep_search", kiro_dir, query="x", includePattern="notes/**", glob="model/**"
        ).allow
        assert not decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern="notes/**", path="notes").allow

    @pytest.mark.parametrize("pattern", [["notes/**"], ["model/**"], 3, {"glob": "notes/**"}, True])
    def test_a_list_valued_or_non_string_glob_is_denied(self, kiro_dir: Path, pattern: object) -> None:
        """``_glob_roots`` skips a non-string glob silently; under Kiro the schema refuses it first."""
        decision = decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern=pattern)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    @pytest.mark.parametrize("pattern", ["{notes,model}/**", "notes/../model/**", "ABSOLUTE", "notes/.*/x", "notes\\x"])
    def test_a_glob_reaching_or_climbing_into_the_model_is_denied(self, kiro_dir: Path, pattern: str) -> None:
        if pattern == "ABSOLUTE":
            pattern = (kiro_dir / "model").as_posix() + "/**"
        assert not decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern=pattern).allow

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("file_search", {"query": "a.txt", "explanation": "x", "excludePattern": None}),
            ("grep_search", {"query": "x", "includePattern": None}),
            ("grep_search", {"query": "x", "includePattern": "**/*.md"}),
            ("grep_search", {"query": "x", "includePattern": "*.md"}),
            ("grep_search", {"query": "x", "includePattern": "{notes,**}/*.md"}),
            ("list_directory", {"path": ".", "depth": 2}),
            ("code", {"operation": "search_symbols", "symbol_name": "main", "path": None}),
        ],
    )
    def test_a_whole_project_search_is_denied_with_the_narrowing_reason(
        self, kiro_dir: Path, tool: str, arguments: dict
    ) -> None:
        """The stated over-reach: a search with no literal prefix searches the whole project."""
        assert decide_kiro_tool(tool, kiro_dir, **arguments) == copilot_guard.Decision(
            allow=False, reason=_KIRO_SEARCH_REASON
        )

    def test_a_search_narrowed_by_its_include_pattern_is_allowed(self, kiro_dir: Path) -> None:
        assert decide_kiro_tool(
            "grep_search", kiro_dir, query="x", includePattern="notes/**/*.md", excludePattern="model/**"
        ).allow
        assert decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern="{notes,docs}/*.md").allow

    def test_a_search_from_a_subfolder_is_judged_against_the_project_too(self, kiro_dir: Path) -> None:
        """A prefixless pattern from ``cwd`` ``notes`` searches the whole project, denied; a literal prefix passes."""
        assert decide_kiro_tool(
            "grep_search", kiro_dir / "notes", query="x", includePattern="*.md"
        ) == copilot_guard.Decision(allow=False, reason=_KIRO_SEARCH_REASON)
        assert decide_kiro_tool("grep_search", kiro_dir / "notes", query="x", includePattern="notes/*.md").allow

    @pytest.mark.parametrize(
        "pattern", ["notes/**,model/**", "notes/** model/**", "notes/**\tmodel/**", "notes/**,", " notes/**", "a,{b}"]
    )
    def test_an_include_pattern_holding_a_list_is_denied(self, kiro_dir: Path, pattern: str) -> None:
        """A ``,`` outside braces or any whitespace makes a pattern list, which cannot be judged, so it is denied."""
        decision = decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern=pattern)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    @pytest.mark.parametrize("pattern", ["{notes,docs}/**", "notes/{a,{b,c}}/*.md", "notes/**"])
    def test_an_include_pattern_with_commas_inside_braces_is_judged(self, kiro_dir: Path, pattern: str) -> None:
        assert decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern=pattern).allow
        assert not decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern="{notes,model}/**").allow

    @pytest.mark.parametrize(
        "pattern",
        [
            "Engine.yaml",
            "IVehicleOperatingState.yaml",
            "*.yaml",
            "model",
            "x.yaml/",
            "{docs/**,x.yaml}",
            "{notes/a.md,b.md}",
        ],
    )
    def test_a_slashless_include_pattern_searches_the_whole_project(self, kiro_dir: Path, pattern: str) -> None:
        """A slash-less include pattern, each such brace alternative too, searches the whole project and is denied.

        Kiro hands it to ripgrep's ``--glob``, read as gitignore does (no slash, or only a trailing one, matches at any
        depth), so a bare file name reads the model's file of that name wherever it lies.
        """
        (kiro_dir / "docs").mkdir()
        assert decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern=pattern) == copilot_guard.Decision(
            allow=False, reason=_KIRO_SEARCH_REASON
        )

    @pytest.mark.parametrize("pattern", ["docs/**/*.md", "/docs/*.md", "notes/a.md", "{docs,notes}/**/*.md"])
    def test_an_include_pattern_anchored_by_a_slash_keeps_its_prefix(self, kiro_dir: Path, pattern: str) -> None:
        """A leading or middle slash anchors ripgrep's glob at the search root: its literal prefix roots it."""
        (kiro_dir / "docs").mkdir()
        assert decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern=pattern).allow

    @pytest.mark.parametrize("pattern", ["/model/**", "/{notes,model}/*.yaml"])
    def test_a_leading_slash_is_also_read_from_the_search_root(self, kiro_dir: Path, pattern: str) -> None:
        """ripgrep reads ``/model/**`` as ``model/**`` below the search root, not as the filesystem's ``/model``."""
        assert not decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern=pattern).allow

    @pytest.mark.parametrize(
        ("depth", "allowed"),
        [(None, True), (1, True), (2, False), (3, False), (0, False), (-1, False), (10**30, False)],
    )
    def test_listing_the_project_root(self, kiro_dir: Path, depth: int | None, allowed: bool) -> None:
        """Depth absent, null or 1 reads the root; deeper searches above the model (kiro-cli 3.1.0 lists one level)."""
        assert decide_kiro_tool("list_directory", kiro_dir, path=".", explanation="x", depth=depth).allow is allowed
        assert decide_kiro_tool("list_directory", kiro_dir, path=str(kiro_dir), depth=depth).allow is allowed

    def test_listing_the_project_root_without_a_depth_key_is_allowed(self, kiro_dir: Path) -> None:
        assert decide_kiro_tool("list_directory", kiro_dir, path=".").allow

    @pytest.mark.parametrize("depth", ["2", 2.0, True, [2]])
    def test_a_depth_that_is_no_integer_is_denied(self, kiro_dir: Path, depth: object) -> None:
        decision = decide_kiro_tool("list_directory", kiro_dir, path="notes", depth=depth)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_listing_the_model_is_denied_at_every_depth(self, kiro_dir: Path) -> None:
        for depth in (None, 1, 2):
            assert not decide_kiro_tool("list_directory", kiro_dir, path="model/swc", depth=depth).allow

    @pytest.mark.parametrize(
        ("workdir", "allowed"),
        [(None, True), ("notes", True), ("model", False), ("model/swc", False), ("", False), (3, False)],
    )
    def test_execute_bash_and_its_working_directory(self, kiro_dir: Path, workdir: object, allowed: bool) -> None:
        """``cwd`` null is the payload's; in the model, empty, not a string or unresolvable denies (Codex's rule)."""
        assert decide_kiro_bash(kiro_dir, "ls", workdir).allow is allowed

    def test_execute_bash_with_an_absolute_working_directory_in_the_model_is_denied(self, kiro_dir: Path) -> None:
        assert decide_kiro_bash(kiro_dir, "ls", str(kiro_dir / "model")) == copilot_guard.Decision(
            allow=False, reason=SHELL_REASON
        )
        assert decide_kiro_bash(
            kiro_dir, "cat ../model/swc/Engine.yaml", str(kiro_dir / "notes")
        ) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    @pytest.mark.parametrize("command", ["cat 'model", 42, None])
    def test_an_unparseable_or_missing_command_is_denied(self, kiro_dir: Path, command: object) -> None:
        decision = decide_kiro_bash(kiro_dir, command)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    @pytest.mark.parametrize("tool", ["execute_pwsh", "execute_cmd", "EXECUTE_PWSH"])
    @pytest.mark.parametrize("command", ["Get-Content notes.md", "Remove-Item model -Recurse", None])
    def test_the_windows_shells_are_denied_whatever_they_carry(
        self, kiro_dir: Path, tool: str, command: object
    ) -> None:
        decision = decide_kiro_tool(tool, kiro_dir, command=command)
        assert (
            not decision.allow
            and decision.reason is not None
            and "reads bash, not PowerShell or cmd" in decision.reason
        )

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("fs_read", {"operations": [{"mode": "Line", "path": "notes.md"}]}),
            ("fs_write", {"command": "create", "path": "notes/x", "file_text": "x"}),
            ("execute_bash", {"command": "ls", "summary": "x", "working_dir": "notes"}),
        ],
    )
    def test_the_v2_engines_file_tools_are_denied(self, kiro_dir: Path, tool: str, arguments: dict) -> None:
        """``fs_read`` by name; V2's ``fs_write`` and ``execute_bash`` by the keys V3's schemas do not declare."""
        assert not decide_kiro_tool(tool, kiro_dir, **arguments).allow

    @pytest.mark.parametrize("operation", ["rename_symbol", "format_code", "pattern_rewrite", "refactor", None, 3])
    def test_a_code_operation_that_can_change_files_is_denied(self, kiro_dir: Path, operation: object) -> None:
        """An operation editing files beyond its path, or unknown to this release, is denied, outside the model too.

        ``rename_symbol``, ``format_code`` and ``pattern_rewrite`` edit beyond the path given.
        """
        decision = decide_kiro_tool("code", kiro_dir, operation=operation, file_path="notes/a.c", new_name="y")
        assert not decision.allow and decision.reason is not None
        assert "is refused: it can change files beyond the path it names" in decision.reason

    @pytest.mark.parametrize("operation", sorted(copilot_guard.KIRO_CODE_OPERATIONS))
    def test_a_reading_code_operation_is_judged_as_a_search(self, kiro_dir: Path, operation: str) -> None:
        assert decide_kiro_tool("code", kiro_dir, operation=operation, path="notes").allow
        assert not decide_kiro_tool("code", kiro_dir, operation=operation, path="model").allow
        assert not decide_kiro_tool(
            "code", kiro_dir, operation=operation, path="notes", file_path="model/swc/Engine.yaml"
        ).allow

    def test_the_code_operations_are_kas_enum_minus_the_writing_ones(self) -> None:
        """The enum of KAS 0.66.91, written here as the oracle the table is held to."""
        kas = {
            "search_symbols", "lookup_symbols", "get_document_symbols", "generate_codebase_overview",
            "search_codebase_map", "pattern_search", "pattern_rewrite", "goto_definition", "find_references",
            "rename_symbol", "format_code", "hover", "completion", "diagnostics",
        }  # fmt: skip
        assert copilot_guard.KIRO_CODE_OPERATIONS == kas - {"rename_symbol", "format_code", "pattern_rewrite"}

    @pytest.mark.parametrize(
        ("files", "allowed"),
        [
            ([], True),
            (None, True),
            ([{"path": "notes.md", "startLine": 1, "endLine": 3}], True),
            ([{"path": "notes.md"}, {"path": "model/swc/Engine.yaml"}], False),
            ([{"path": "notes.md", "extra": "model/x"}], False),
            ([{"startLine": 1}], False),
            ([{"path": ""}], False),
            ([{"path": 3}], False),
            (["notes.md"], False),
            ({"path": "notes.md"}, False),
            ("notes.md", False),
        ],
    )
    def test_the_sub_agents_context_files(self, kiro_dir: Path, files: object, allowed: bool) -> None:
        """A sub-agent is allowed (its calls reach the hook); its context files are reads, one off the schema denies."""
        decision = decide_kiro_tool("invoke_sub_agent", kiro_dir, name="g", prompt="p", contextFiles=files, preset=None)
        assert decision.allow is allowed

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("mcp_swcraft_read", {"address": {"layer": "swc"}}),
            ("mcp_swcraft_change_set", {"operations": [{"path": "model/swc/Engine.yaml"}]}),
            ("mcp_other_server_write", {"path": "model/swc/Engine.yaml"}),
            ("tool_load", {"tool_ids": ["swcraft::read"]}),
            ("introspect", {"query": "x"}),
            ("get_process_output", {"terminalId": "t1", "lines": 20}),
            ("list_processes", {}),
        ],
    )
    def test_the_tools_allowed_by_name(self, kiro_dir: Path, tool: str, arguments: dict) -> None:
        """MCP tools (another server's file tools a stated limit) and the built-ins that touch no file."""
        assert decide_kiro_tool(tool, kiro_dir, **arguments).allow

    @pytest.mark.parametrize(
        ("url", "allowed"),
        [
            ("https://example.org", True),
            ("http://example.org/a", True),
            ("HTTPS://EXAMPLE.ORG", True),
            ("file:///etc/passwd", False),
            ("FILE:///x", False),
            ("ftp://example.org", False),
            (" https://example.org", False),
            ("example.org", False),
            ("", False),
            (None, False),
            (3, False),
        ],
    )
    def test_web_fetch_is_allowed_for_an_http_url_only(self, kiro_dir: Path, url: object, allowed: bool) -> None:
        """Only ``http``/``https`` URLs pass: a ``file:`` read by Kiro's fetch is unchecked; the schema is closed."""
        assert decide_kiro_tool("web_fetch", kiro_dir, url=url, mode="truncated", searchPhrase=None).allow is allowed
        assert not decide_kiro_tool(
            "web_fetch", kiro_dir, url="https://example.org", path="model/swc/Engine.yaml"
        ).allow

    def test_web_fetch_outside_a_project_is_allowed(self, elsewhere: Path) -> None:
        assert decide_kiro_tool("web_fetch", elsewhere, url="file:///x").allow

    @pytest.mark.parametrize("tool", ["control_bash_process", "control_pwsh_process"])
    def test_a_background_process_starter_is_denied_with_its_own_reason(self, kiro_dir: Path, tool: str) -> None:
        """It starts a command (KAS 0.66.91: action ``start``, a ``command``): denied with the judged way's reason."""
        decision = decide_kiro_tool(tool, kiro_dir, action="start", command="ls", cwd=None)
        assert decision == copilot_guard.Decision(
            allow=False,
            reason=(
                f"SWCraft: in this project the tool `{tool}` is refused: the command it starts is not one the model "
                "guard judges. Use execute_bash with run_in_background, then get_process_output, or the SWCraft "
                "tools for the model."
            ),
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {
                "tool_name": "mcp_swcraft_read",
                "toolName": "fs_write",
                "tool_input": {"path": "model/swc/Engine.yaml", "text": "x"},
            },
            {
                "tool_name": "fs_write",
                "tool_input": {"path": "notes/x.txt", "text": "x"},
                "toolArgs": json.dumps({"path": "model/swc/Engine.yaml", "text": "x"}),
            },
        ],
        ids=["two-names", "two-argument-sets"],
    )
    def test_a_payload_carrying_both_key_shapes_is_denied_by_either(self, kiro_dir: Path, payload: dict) -> None:
        """Both shapes are read, every pairing judged.

        A harmless name or argument set beside one naming the model never hides it."""
        assert not decide_kiro({**payload, "cwd": str(kiro_dir)}, kiro_dir).allow

    @pytest.mark.parametrize(
        "envelope", [json.dumps({"path": "notes.txt", "text": "x"}), "{", ["notes.txt"], 3, True, None]
    )
    @pytest.mark.parametrize("key", ["tool_input", "toolArgs"])
    @pytest.mark.parametrize(
        "tool", ["fs_write", "read_file", "some_new_tool", "mcp_swcraft_read", "get_process_output"]
    )
    def test_an_argument_envelope_that_is_not_an_object_is_denied_whatever_the_tool(
        self, kiro_dir: Path, elsewhere: Path, tool: str, key: str, envelope: Any
    ) -> None:
        """A Kiro envelope that is not an object as sent is denied for any tool in a guarded project.

        A JSON string is never decoded (the other hosts decode it).
        """
        name_key = "tool_name" if key == "tool_input" else "toolName"
        payload = {name_key: tool, key: envelope, "hook_event_name": "PreToolUse", "cwd": str(kiro_dir)}
        assert decide_kiro(payload, kiro_dir) == copilot_guard.Decision(
            allow=False, reason=copilot_guard._KIRO_ENVELOPE_REASON
        )
        outside = {**payload, "cwd": str(elsewhere)}
        assert decide_kiro(outside, elsewhere).allow  # outside every project nothing is guarded

    @pytest.mark.parametrize("host", ["copilot", "codex", "claude"])
    def test_the_other_hosts_still_decode_a_json_string_envelope(self, kiro_dir: Path, host: str) -> None:
        """The Kiro envelope rule is Kiro's alone: Copilot's ``toolArgs`` may be a JSON string, decoded and judged."""
        guarded = copilot_guard.read_project(kiro_dir)

        def string_call(path: str) -> dict[str, Any]:
            return {"tool_name": "view", "tool_input": json.dumps({"path": path}), "cwd": str(kiro_dir)}

        assert copilot_guard.decide(string_call("notes.md"), kiro_dir, guarded, host).allow
        assert not copilot_guard.decide(string_call("model/swc/Engine.yaml"), kiro_dir, guarded, host).allow

    def test_the_reproduction_a_string_envelope_on_fs_write_is_denied_by_the_hook(
        self, kiro_dir: Path, monkeypatch, capsys
    ) -> None:
        stdin = json.dumps(
            {
                "tool_name": "fs_write",
                "tool_input": json.dumps({"path": "notes.txt", "text": "x"}),
                "cwd": str(kiro_dir),
            }
        ).encode("utf-8")
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, kiro_dir)
        assert (code, out) == (2, "")
        assert kiro_deny_line(err) == copilot_guard._KIRO_ENVELOPE_REASON
        # The object form of the same call is allowed: only the envelope decides here.
        stdin = json.dumps(
            {"tool_name": "fs_write", "tool_input": {"path": "notes.txt", "text": "x"}, "cwd": str(kiro_dir)}
        ).encode("utf-8")
        assert _run_kiro(stdin, monkeypatch, capsys, kiro_dir)[0] == 0

    @pytest.mark.parametrize("tool", ["mcp_", "MCP_swcraft_read", "Tool_Load", "read", "Read", "fsWrite", "readFile"])
    def test_the_allowlist_is_matched_exactly(self, kiro_dir: Path, tool: str) -> None:
        """An allowance is never widened by casefolding, and Copilot's and Claude Code's names are unknown here."""
        assert not decide_kiro_tool(tool, kiro_dir, path="notes.md").allow

    @pytest.mark.parametrize("tool", ["read_files", "createHook", "kiro_powers", "memory", "glob", "some_new_tool"])
    @pytest.mark.parametrize(
        "arguments",
        [
            {"paths": ["model/swc/Engine.yaml"]},
            {"path": "swcraft.yaml", "text": "x"},
            {"path": ".codex/hooks.json"},
            {"target": "HANDOFF"},
            {},
        ],
    )
    def test_an_unknown_tool_is_denied_whatever_it_carries(
        self, kiro_dir: Path, user_home: Path, tool: str, arguments: dict
    ) -> None:
        """The allowlist: Kiro updates itself, and a renamed or new file tool must not pass unjudged."""
        arguments = {
            key: str(handoff_reviews(user_home)) if value == "HANDOFF" else value for key, value in arguments.items()
        }
        decision = decide_kiro_tool(tool, kiro_dir, **arguments)
        assert not decision.allow and decision.reason is not None
        assert f"the tool `{tool}` is not one this release's model guard knows" in decision.reason

    def test_an_unknown_tool_name_that_could_be_a_path_is_not_quoted(self, kiro_dir: Path) -> None:
        name = str(kiro_dir / "x")
        decision = decide_kiro_tool(name, kiro_dir)
        assert not decision.allow and decision.reason is not None
        assert str(kiro_dir) not in decision.reason and "this tool is not one" in decision.reason

    @pytest.mark.parametrize("tool", [None, 3, ["read_file"], {"name": "read_file"}])
    def test_a_tool_that_cannot_be_named_is_denied(self, kiro_dir: Path, tool: object) -> None:
        payload = {"tool_name": tool, "tool_input": {"path": "notes.md"}, "cwd": str(kiro_dir)}
        assert not decide_kiro(payload, kiro_dir).allow

    @pytest.mark.parametrize("arguments", ["not json", "[1]", 3, ["path"]])
    def test_arguments_that_are_no_object_are_denied(self, kiro_dir: Path, arguments: object) -> None:
        payload = {"tool_name": "read_file", "tool_input": arguments, "cwd": str(kiro_dir)}
        decision = decide_kiro(payload, kiro_dir)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_a_saved_answer_is_viewed(self, kiro_dir: Path) -> None:
        saved = Path(tempfile.gettempdir()) / saved_answer_name()
        assert decide_kiro_tool("read_file", kiro_dir, path=str(saved)).allow


class TestKiroProject:
    """Which project the Kiro dialect guards: the nearest folder with ``swcraft.yaml`` and ``.kiro/hooks``.

    It is walked from the payload's ``cwd`` and from the hook's own working directory."""

    def test_the_project_is_found_from_a_subfolder(self, kiro_dir: Path) -> None:
        below = kiro_dir / "notes"
        assert not decide_kiro_tool("read_file", below, path="../model/swc/Engine.yaml").allow
        assert not decide_kiro_tool("fs_write", below, path="../model/swc/X.yaml", text="x").allow
        assert decide_kiro_tool("read_file", below, path="../notes.md").allow

    def test_a_hook_file_of_any_name_marks_the_project(self, kiro_dir: Path) -> None:
        hooks = kiro_dir / ".kiro" / "hooks"
        (hooks / "swcraft-guard.json").rename(hooks / "renamed.json")
        assert not decide_kiro_tool("read_file", kiro_dir, path="model/swc/Engine.yaml").allow
        (hooks / "renamed.json").unlink()
        assert not decide_kiro_tool("read_file", kiro_dir, path="model/swc/Engine.yaml").allow  # the folder marks it

    def test_a_manifest_without_the_hook_folder_is_not_guarded(
        self, project_dir: Path, elsewhere: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(elsewhere)
        (project_dir / ".kiro" / "settings").mkdir(parents=True)
        assert decide_kiro_tool("fs_write", project_dir, path="model/swc/X.yaml", text="x").allow
        assert decide_kiro_tool("some_new_tool", project_dir).allow

    def test_the_hook_folder_without_a_manifest_is_not_guarded(
        self, tmp_path: Path, elsewhere: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(elsewhere)
        write_kiro_hooks(tmp_path / "bare")
        (tmp_path / "bare" / "model").mkdir()
        assert decide_kiro_tool("fs_write", tmp_path / "bare", path="model/x", text="x").allow

    @pytest.mark.parametrize("marker", [".codex/hooks.json", ".claude/settings.json", ".github/hooks/swcraft.json"])
    def test_another_clients_hook_does_not_mark_a_kiro_project(
        self, project_dir: Path, elsewhere: Path, monkeypatch, marker: str
    ) -> None:
        monkeypatch.chdir(elsewhere)
        (project_dir / marker).parent.mkdir(parents=True, exist_ok=True)
        (project_dir / marker).write_text("{}\n", encoding="utf-8", newline="\n")
        assert decide_kiro_tool("fs_write", project_dir, path="model/swc/X.yaml", text="x").allow

    @pytest.mark.parametrize("anchor_in_project", [True, False])
    def test_a_payload_cwd_outside_the_project_is_judged_through_the_process_anchor(
        self, kiro_dir: Path, elsewhere: Path, monkeypatch, anchor_in_project: bool
    ) -> None:
        """The hook runs in the project root (measured), so a ``cwd`` moved out of the project still judges it.

        With both anchors outside, nothing is found (the stated limit)."""
        monkeypatch.chdir(kiro_dir if anchor_in_project else elsewhere)
        model_file = str(kiro_dir / "model" / "swc" / "Engine.yaml")
        assert decide_kiro_tool("read_file", elsewhere, path=model_file).allow is not anchor_in_project
        assert decide_kiro_tool("fs_write", elsewhere, path=model_file, text="x").allow is not anchor_in_project
        command = decide_kiro_bash(elsewhere, f"cat {shlex.quote(model_file)}")
        if anchor_in_project:
            assert command == copilot_guard.Decision(allow=False, reason=SHELL_REASON)
        else:
            assert command.allow

    @pytest.mark.parametrize("anchor_in_project", [True, False])
    def test_a_payload_cwd_in_a_nested_project_still_judges_the_original(
        self, kiro_dir: Path, elsewhere: Path, monkeypatch, anchor_in_project: bool
    ) -> None:
        nested = codex_project(kiro_dir / "notes" / "inner", "design")  # a model folder named apart from the outer
        write_kiro_hooks(nested)
        monkeypatch.chdir(kiro_dir if anchor_in_project else elsewhere)
        model_file = str(kiro_dir / "model" / "swc" / "Engine.yaml")
        assert decide_kiro_tool("fs_write", nested, path=model_file, text="x").allow is not anchor_in_project
        assert decide_kiro_tool("read_file", nested, path=model_file).allow is not anchor_in_project
        assert not decide_kiro_tool(
            "fs_write", nested, path="design/x.yaml", text="x"
        ).allow  # the inner project's own model

    def test_a_relative_path_is_judged_against_the_project_too(
        self, kiro_dir: Path, elsewhere: Path, monkeypatch
    ) -> None:
        """A relative path or rootless search is judged against ``cwd`` and the project directory, a denial winning.

        A ``cwd`` outside the project or in a subfolder does not hide ``model/...``.
        """
        monkeypatch.chdir(kiro_dir)
        assert not decide_kiro_tool("fs_write", elsewhere, path="model/swc/X.yaml", text="x").allow
        assert not decide_kiro_tool("read_file", kiro_dir / "notes", path="model/swc/Engine.yaml").allow
        assert not decide_kiro_tool("delete_file", elsewhere, targetFile="model/swc/Engine.yaml").allow
        assert decide_kiro_bash(elsewhere, "cat model/swc/Engine.yaml") == copilot_guard.Decision(
            allow=False, reason=SHELL_REASON
        )
        for where in (elsewhere, kiro_dir / "notes"):
            assert decide_kiro_tool("grep_search", where, query="x").allow is False
            assert decide_kiro_tool("file_search", where, query="x").allow is False
        assert decide_kiro_tool("read_file", elsewhere, path="notes.md").allow
        assert decide_kiro_tool("fs_write", kiro_dir / "notes", path="x.txt", text="x").allow

    def test_two_anchors_on_one_project_judge_it_once(self, kiro_dir: Path, monkeypatch) -> None:
        monkeypatch.chdir(kiro_dir)
        assert len(copilot_guard.guarded_projects(kiro_dir / "notes", "kiro")) == 1
        assert copilot_guard.kiro_project_directory(kiro_dir / "notes") == copilot_guard.kiro_project_directory(
            kiro_dir
        )

    def test_no_project_allows_every_tool(self, tmp_path: Path, elsewhere: Path, monkeypatch) -> None:
        monkeypatch.chdir(elsewhere)
        (tmp_path / "model").mkdir()
        for tool in ("read_file", "execute_pwsh", "some_new_tool", "fs_write"):
            assert decide_kiro_tool(tool, tmp_path, path="model/x").allow
        assert decide_kiro({"tool_name": None, "cwd": str(tmp_path)}, tmp_path).allow

    @pytest.mark.parametrize("kind", list(UNREADABLE_MANIFESTS))
    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("read_file", {"path": "notes.md"}),
            ("read_file", {"path": "swcraft.yaml"}),
            ("list_directory", {"path": "notes", "depth": None}),
            ("grep_search", {"query": "x", "includePattern": "notes/**"}),
            ("code", {"operation": "hover", "file_path": "notes.md"}),
            ("invoke_sub_agent", {"name": "g", "contextFiles": []}),
            ("invoke_sub_agent", {"name": "g", "contextFiles": [{"path": "notes.md"}]}),
            ("web_fetch", {"url": "https://example.com"}),
            ("web_fetch", {"url": "http://example.com", "mode": "full"}),
            ("web_fetch", {"url": "file:///etc/hosts"}),
            ("fs_write", {"path": "swcraft.yaml", "text": "name: x"}),
            ("str_replace", {"path": "notes.md", "oldStr": "a", "newStr": "b"}),
            ("delete_file", {"targetFile": "notes.md"}),
            ("execute_bash", {"command": "cat swcraft.yaml", "cwd": None}),
            ("read_file", {"path": "notes.md", "paths": "x"}),
        ],
    )
    def test_an_unreadable_manifest_denies_every_judged_call(
        self, kiro_dir: Path, kind: str, tool: str, arguments: dict
    ) -> None:
        """Every judged call is denied but a saved answer, the reason asking for the fix by hand."""
        write_unreadable_manifest(kiro_dir, UNREADABLE_MANIFESTS[kind])
        decision = decide_kiro_tool(tool, kiro_dir, **arguments)
        assert not decision.allow and decision.reason is not None
        assert decision.reason.startswith("SWCraft: this project's swcraft.yaml cannot be read (")
        assert decision.reason.endswith(BY_HAND)
        assert str(kiro_dir) not in decision.reason

    def test_an_unreadable_manifest_leaves_the_mcp_tools_and_denies_the_unknown(self, kiro_dir: Path) -> None:
        write_unreadable_manifest(kiro_dir)
        assert decide_kiro_tool("mcp_swcraft_read", kiro_dir).allow
        assert decide_kiro_tool("tool_load", kiro_dir, tool_ids=[]).allow
        assert not decide_kiro_tool("some_new_tool", kiro_dir).allow
        assert not decide_kiro_tool("execute_pwsh", kiro_dir, command="x").allow
        assert decide_kiro_tool(
            "read_file", kiro_dir, path=str(Path(tempfile.gettempdir()) / saved_answer_name())
        ).allow


class TestKiroGuardInputs:
    """No Kiro tool writes the shared guard inputs, its own three folders included; they are read freely."""

    @pytest.mark.parametrize("name", SHARED_GUARD_INPUTS)
    @pytest.mark.parametrize(("tool", "key"), KIRO_WRITE_TOOLS)
    def test_a_write_of_a_guard_input_is_denied(self, kiro_dir: Path, tool: str, key: str, name: str) -> None:
        assert decide_kiro_tool(tool, kiro_dir, **{key: name}) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="edit")
        )

    @pytest.mark.parametrize(
        "command", ["rm -rf .claude", "echo x > .codex/hooks.json", "mv .github/hooks x", "sed -i s/a/b/ swcraft.yaml"]
    )
    def test_a_command_writing_a_guard_input_is_denied(self, kiro_dir: Path, command: str) -> None:
        assert decide_kiro_bash(kiro_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize("name", SHARED_GUARD_INPUTS)
    def test_a_read_of_a_guard_input_is_allowed(self, kiro_dir: Path, name: str) -> None:
        assert decide_kiro_tool("read_file", kiro_dir, path=name).allow
        assert decide_kiro_bash(kiro_dir, f"cat {name}").allow


class TestKiroHandOff:
    """The review hand-off directory: the Claude Code rule, unchanged (no Kiro write sandbox observed)."""

    def test_every_kind_reaching_it_is_denied(self, kiro_dir: Path, user_home: Path) -> None:
        reviews = handoff_reviews(user_home)
        pending = str(reviews / "x.delta.pending.json")
        denied = copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)
        assert decide_kiro_tool("read_file", kiro_dir, path=pending) == denied
        assert decide_kiro_tool("list_directory", kiro_dir, path=str(reviews), depth=None) == denied
        assert decide_kiro_tool("fs_write", kiro_dir, path=str(reviews / "x.decision.json"), text="{}") == denied
        assert decide_kiro_tool("delete_file", kiro_dir, targetFile=pending) == denied
        assert (
            decide_kiro_tool("grep_search", kiro_dir, query="x", includePattern=f"{user_home.as_posix()}/**") == denied
        )
        assert decide_kiro_tool("code", kiro_dir, operation="hover", file_path=pending) == denied
        assert decide_kiro_tool("invoke_sub_agent", kiro_dir, name="g", contextFiles=[{"path": pending}]) == denied
        assert decide_kiro_bash(kiro_dir, f"cat {shlex.quote(pending)}") == denied
        assert decide_kiro_bash(kiro_dir, "ls", str(reviews)) == denied

    def test_a_delete_of_a_folder_holding_it_is_denied(self, kiro_dir: Path, user_home: Path) -> None:
        """``delete_file`` of ``<HOME>/.swcraft`` or ``<HOME>`` removes the records below it, as ``rm -rf`` of it does.

        That is the shell judge's ``_above_handoff``; a sibling of the hand-off directory -- the toolchain's
        ``current`` -- holds no record and is the SWCraft home rule's alone (``TestSwcraftHomeProtected``)."""
        reviews = handoff_reviews(user_home)
        home = reviews.parent
        (home / "current").mkdir()
        denied = copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)
        for target in (home, user_home, Path(f"{home}{os.sep}")):
            assert decide_kiro_tool("delete_file", kiro_dir, targetFile=str(target)) == denied, target
            assert decide_kiro_bash(kiro_dir, f"rm -rf {shlex.quote(str(target))}") == denied, target
        assert decide_kiro_tool("delete_file", kiro_dir, targetFile=str(reviews)) == denied
        home_denied = copilot_guard.Decision(allow=False, reason=HOME_REASON)
        assert decide_kiro_tool("delete_file", kiro_dir, targetFile=str(home / "current")) == home_denied
        assert decide_kiro_tool("fs_write", kiro_dir, path=str(home / "notes.md"), text="x") == home_denied

    def test_a_home_that_cannot_be_determined_denies(self, kiro_dir: Path, monkeypatch) -> None:
        monkeypatch.setattr(copilot_guard, "_handoff_directory", lambda: None)
        for decision in (
            decide_kiro_tool("read_file", kiro_dir, path="notes.md"),
            decide_kiro_tool("fs_write", kiro_dir, path="notes.md", text="x"),
            decide_kiro_bash(kiro_dir, "ls"),
        ):
            assert (
                not decision.allow
                and decision.reason is not None
                and "SWCraft home cannot be determined" in decision.reason
            )


#: The reasons of the rules on the written path (:mod:`swcraft_tools.guard_model_paths`) for the model ``model``.
_WRITTEN_PATH_REASONS = {
    reason("the model directory (`model`)")
    for reason in (
        guard_model_paths.model_path_reason,
        guard_model_paths.model_entry_reason,
        guard_model_paths.unreadable_tree_reason,
    )
}


@pytest.fixture
def readings_alone(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """The rules on the written path (:func:`copilot_guard._model_path_decision`) switched off.

    A denial then comes from a reading the guard gives the path: those rules refuse every path below the model
    folder's name, whatever its readings, and would hide a reading that is lost. Gives the calls the stand-in took,
    so a test whose call is allowed -- which reaches those rules last -- asserts the patch was reached."""
    calls: list[tuple[object, ...]] = []

    def allow(*args: object, **_kwargs: object) -> copilot_guard.Decision:
        calls.append(args)
        return copilot_guard.Decision(True)

    monkeypatch.setattr(copilot_guard, "_model_path_decision", allow)
    return calls


def _is_model_denial(decision: copilot_guard.Decision) -> bool:
    """A denial naming the model by the folder rules, on a reading that reaches it.

    Not by the rules on the written path, which refuse such a path whatever its readings, so a denial of theirs would
    hide a reading that is lost."""
    return not decision.allow and "`model`" in (decision.reason or "") and decision.reason not in _WRITTEN_PATH_REASONS


@pytest.mark.usefixtures("readings_alone")
class TestKiroPathFallbacks:
    """The readings Kiro's tool service gives a path beyond the one written are judged too, a denial from any winning.

    The service is kiro-cli 3.1.0 and 3.3.0's; the readings are ``read_file``'s retry with the first segment stripped
    (``readWithPathFallbacks``/``stripLeadingPathSegment``), a context file's retry (``tryReadWithFallback``, no
    ``..`` check) and a multi-folder workspace's folder-name match (``resolveWorkspaceRelativePath``)."""

    @pytest.mark.parametrize("written", ["zz/model/swc/Engine.yaml", "~/model/swc/Engine.yaml"])
    def test_read_file_falling_back_into_the_model_is_denied(
        self, kiro_dir: Path, user_home: Path, written: str
    ) -> None:
        assert _is_model_denial(decide_kiro_tool("read_file", kiro_dir, path=written))

    @pytest.mark.parametrize("written", ["zz/model/swc/Engine.yaml", "~/model/swc/Engine.yaml"])
    def test_a_context_file_falling_back_into_the_model_is_denied(
        self, kiro_dir: Path, user_home: Path, written: str
    ) -> None:
        assert _is_model_denial(decide_kiro_sub_agent(kiro_dir, "docs/a.md", written))

    def test_a_context_file_falling_back_through_dot_dot_into_the_hand_off_directory_is_denied(
        self, kiro_dir: Path, user_home: Path
    ) -> None:
        handoff_reviews(user_home)
        written = "/".join(("zz", "..", user_home.name, swcraft_home(user_home).name, "reviews", "a.pending.json"))
        assert decide_kiro_sub_agent(kiro_dir, written) == copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)

    def test_read_file_does_not_fall_back_through_dot_dot(self, kiro_dir: Path, user_home: Path) -> None:
        """``stripLeadingPathSegment`` gives up on a ``..`` segment, so ``read_file`` reads the path as written."""
        handoff_reviews(user_home)
        written = "/".join(("zz", "..", user_home.name, swcraft_home(user_home).name, "reviews", "a.pending.json"))
        assert decide_kiro_tool("read_file", kiro_dir, path=written).allow

    def test_a_fallback_opening_with_a_tilde_is_expanded(self, kiro_dir: Path, user_home: Path) -> None:
        handoff_reviews(user_home)
        handoff = copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)
        written = "zz/" + from_home(user_home, "reviews", "a.pending.json")
        assert decide_kiro_tool("read_file", kiro_dir, path=written) == handoff
        assert decide_kiro_sub_agent(kiro_dir, written) == handoff

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("fs_write", {"path": "proj/model/swc/Engine.yaml", "text": "x"}),
            ("str_replace", {"path": "PROJ/model/swc/Engine.yaml", "oldStr": "a", "newStr": "b"}),
            ("read_file", {"path": "./proj/model/swc/Engine.yaml"}),
            ("list_directory", {"path": "proj/model"}),
            ("code", {"operation": "search_symbols", "path": "proj/model"}),
            ("grep_search", {**GREP_ARGUMENTS, "includePattern": "proj/model/**"}),
            ("invoke_sub_agent", {"name": "x", "prompt": "x", "contextFiles": [{"path": "proj/model/x.yaml"}]}),
        ],
    )
    def test_a_path_naming_the_project_folder_is_judged_in_it(
        self, kiro_dir: Path, tool: str, arguments: dict[str, Any]
    ) -> None:
        assert _is_model_denial(decide_kiro_tool(tool, kiro_dir, **arguments))

    def test_a_shell_working_directory_naming_the_project_folder_is_judged_in_it(self, kiro_dir: Path) -> None:
        assert _is_model_denial(decide_kiro_bash(kiro_dir, "ls", "proj/model"))

    def test_the_folder_names_are_the_project_and_the_payload_cwd(self, kiro_dir: Path) -> None:
        guarded = copilot_guard.read_project(kiro_dir)
        payload = {"cwd": str(kiro_dir / "notes")}
        names = copilot_guard._kiro_folder_names(payload, kiro_dir, guarded)
        assert names == frozenset({"proj", "notes"})
        assert kiro_paths.workspace_folder_readings("Notes/../x.md", names) == ()
        assert kiro_paths.workspace_folder_readings("Notes/x.md", names) == ("x.md",)
        assert kiro_paths.workspace_folder_readings("proj", names) == (".",)
        assert kiro_paths.workspace_folder_readings("~/proj/x", names) == ()
        assert kiro_paths.workspace_folder_readings(str(kiro_dir / "x"), names) == ()

    def test_the_fallbacks_are_kiros(self) -> None:
        strip, context = kiro_paths.read_fallback, kiro_paths.context_fallback
        assert strip("zz/model/x") == "model/x"
        assert strip("zz\\model\\x") == "model/x"
        assert strip("x.md") is None
        assert strip("zz/../x") is None
        assert strip("zz//x") is None
        assert strip("/abs/x") is None
        assert kiro_paths.workspace_folder_readings("/abs/x", frozenset({"abs"})) == ()
        assert context("zz/../x") == "../x"
        assert context("zz/x") == "x"
        assert context("x.md") is None
        assert context("/abs/x") is None
        assert context("\\abs\\x") is None
        assert context("C:x/y") is None

    @pytest.mark.parametrize(
        ("value", "posix", "windows"),
        [
            ("/abs/x", True, True),
            ("\\abs\\x", False, True),
            ("\\\\host\\share\\x", False, True),
            ("C:\\x", False, True),
            ("C:/x", False, True),
            ("C:x", False, False),
            ("zz/x", False, False),
            ("", False, False),
        ],
    )
    def test_kiros_absolute_path_is_nodes_on_the_host(self, value: str, posix: bool, windows: bool) -> None:
        """``readWithPathFallbacks`` skips the strip for a path Node's ``path.isAbsolute`` calls absolute.

        That is kiro-cli 3.1.0's, by the host's rule: both rules hold on every host, and the host's own decides the
        fallback."""
        assert kiro_paths.is_absolute(value, windows=False) is posix
        assert kiro_paths.is_absolute(value, windows=True) is windows
        host = windows if kiro_paths.WIN32 else posix
        assert kiro_paths.is_absolute(value) is host
        expected = None if host else kiro_paths.strip_leading_segment(value)
        assert kiro_paths.read_fallback(value) == expected

    @pytest.mark.parametrize("win32", [False, True])
    def test_the_host_rule_is_read_at_the_call(self, monkeypatch: pytest.MonkeyPatch, win32: bool) -> None:
        """``is_absolute``'s default and ``node_join`` read the same flag, so patching it moves both."""
        monkeypatch.setattr(kiro_paths, "WIN32", win32)
        assert kiro_paths.is_absolute("C:\\x") is win32

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("fs_write", {"path": "linkproj/model/hardware/CPU.yaml", "text": "x"}),
            ("list_directory", {"path": "linkproj/model"}),
            ("fs_write", {"path": "proj/model/hardware/CPU.yaml", "text": "x"}),
        ],
    )
    def test_a_project_opened_through_a_symlink_is_named_by_the_link_and_its_target(
        self, kiro_dir: Path, tool: str, arguments: dict[str, Any]
    ) -> None:
        """A project opened as ``linkproj`` is the folder ``linkproj`` names; the resolved name stays a reading too.

        kiro-cli 3.1.0's ``resolveWorkspaceRelativePath`` compares the basename of the workspace path as Kiro holds
        it (no realpath), and the payload's ``cwd`` is that path."""
        link = kiro_dir.parent / "linkproj"
        symlink_or_skip(link, kiro_dir)
        assert _is_model_denial(decide_kiro_tool(tool, link, **arguments))

    def test_the_folder_names_are_read_as_given_and_resolved(self, kiro_dir: Path) -> None:
        link = kiro_dir.parent / "linkproj"
        symlink_or_skip(link, kiro_dir)
        guarded = copilot_guard.read_project(link)
        names = copilot_guard._kiro_folder_names({"cwd": str(link)}, link, guarded)
        assert names == frozenset({"linkproj", "proj"})

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("read_file", {"path": "notes/x.md"}),
            ("read_file", {"path": "~/notes.txt"}),
            ("read_file", {"path": "other/proj/notes.md"}),
            ("fs_write", {"path": "proj/notes/x.md", "text": "x"}),
            ("invoke_sub_agent", {"name": "x", "prompt": "x", "contextFiles": [{"path": "docs/a.md"}]}),
        ],
    )
    def test_harmless_paths_are_allowed(
        self,
        kiro_dir: Path,
        user_home: Path,
        readings_alone: list[tuple[object, ...]],
        tool: str,
        arguments: dict[str, Any],
    ) -> None:
        handoff_reviews(user_home)
        assert decide_kiro_tool(tool, kiro_dir, **arguments).allow
        assert readings_alone


#: Whether a backslash is a name character on this host, not a separator (a capability probe, never a platform check).
_BACKSLASH_IS_A_NAME = Path("a\\b").name == "a\\b"


@pytest.mark.usefixtures("readings_alone")
class TestNodeReadings:
    """The readings Node gives a path in Kiro's tool service are judged beside the others, a denial from any winning.

    A URI's ``fsPath`` drops the slash before a drive (``/C:/x`` is ``C:/x``, relative on POSIX); ``path.join``
    applies ``..`` before any link is followed; ``~`` is joined to the home as Node joins it, a backslash a name on
    POSIX; a workspace folder's name is matched on the host's own separator."""

    @pytest.mark.skipif(NATIVE_DRIVES, reason="needs a host without drive letters (a capability probe, Rule U)")
    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("read_file", {"path": "/C:/../model/swc/Engine.yaml"}),
            ("fs_write", {"path": "/c:/../model/x.yaml", "text": "x"}),
        ],
    )
    def test_a_drive_after_a_slash_is_judged_as_the_uri_reads_it(
        self, kiro_dir: Path, tool: str, arguments: dict[str, Any]
    ) -> None:
        assert not decide_kiro_tool(tool, kiro_dir, **arguments).allow

    @pytest.mark.parametrize(
        ("windows", "base", "rest", "joined"),
        [
            (False, "/w/p", "hop/../model/x", "/w/p/model/x"),
            (False, "/w/p", "../../x", "/x"),
            (False, None, "a/../b", "b"),
            (False, "/w", "a\\..\\b", "/w/a\\..\\b"),
            (True, "C:/w/p", "hop\\..\\model\\x", "C:/w/p/model/x"),
            (True, "C:/w", "../../x", "C:/x"),
            (True, None, "a\\..\\b", "b"),
        ],
    )
    def test_nodes_join_applies_dot_dot_by_the_hosts_rule(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool, base: str | None, rest: str, joined: str
    ) -> None:
        """``..`` cancels the folder before it, the base's included, before any link is followed.

        On POSIX a backslash is a name, on Windows a separator, and the reading is spelled with ``/``."""
        monkeypatch.setattr(kiro_paths, "WIN32", windows)
        assert kiro_paths.node_join(base, rest) == joined

    @pytest.mark.parametrize(
        ("windows", "value", "reading"),
        [
            (False, "hop/../model/x", "/w/p/model/x"),
            (False, "/a/../model/x", "/model/x"),
            (False, "hop\\..\\model", "/w/p/hop\\..\\model"),
            (True, "hop\\..\\model", "/w/p/model"),
            (False, "notes/x", None),
            (False, "~/../model/x", None),
        ],
    )
    def test_the_dot_dot_reading_is_nodes_join_with_the_call_directory(
        self, monkeypatch: pytest.MonkeyPatch, windows: bool, value: str, reading: str | None
    ) -> None:
        """A ``..`` segment, split at ``/`` or ``\\``, is joined to the directory unless absolute.

        There is no reading without one, or for a ``~`` path."""
        monkeypatch.setattr(kiro_paths, "WIN32", windows)
        assert kiro_paths.dotted_reading(value, "/w/p") == reading

    @pytest.mark.parametrize(
        ("value", "readings"), [("/C:/x/y", ("C:/x/y",)), ("/c:", ("c:",)), ("/Cx/y", ()), ("C:/x", ())]
    )
    def test_the_uri_drive_reading_drops_the_slash_before_a_drive(self, value: str, readings: tuple[str, ...]) -> None:
        assert kiro_paths.uri_drive_reading(value) == readings

    def test_a_dot_dot_path_outside_the_model_is_allowed(
        self, kiro_dir: Path, readings_alone: list[tuple[object, ...]]
    ) -> None:
        assert decide_kiro_tool("read_file", kiro_dir, path="hop/../notes/x.md").allow
        assert readings_alone

    def test_a_dot_dot_reading_is_joined_to_the_call_directory(self) -> None:
        forms = copilot_guard._kiro_forms(copilot_guard._KIRO_JUDGED_FOLDED["list_directory"], frozenset(), "/w/p")
        assert forms("path", "hop/../model/x") == ("hop/../model/x", kiro_paths.node_join("/w/p", "hop/../model/x"))
        assert forms("path", "notes/x") == ("notes/x",)

    def test_the_uri_reading_drops_the_slash_before_a_drive(self) -> None:
        forms = copilot_guard._kiro_forms(copilot_guard._KIRO_JUDGED_FOLDED["read_file"], frozenset(), "/w")
        assert "C:/x/y" in (forms("path", "/C:/x/y") or ())
        assert forms("path", "/Cx/y") == ("/Cx/y",)

    def test_the_uri_reading_is_given_a_fallbacks_path_too(self) -> None:
        """A context file's retry can open with ``/C:``; its URI reading is judged like the written path's."""
        forms = copilot_guard._kiro_forms(copilot_guard._KIRO_JUDGED_FOLDED["read_file"], frozenset(), "/w")
        assert "C:/../alias/x" in (forms(None, "zz//C:/../alias/x") or ())

    @pytest.mark.skipif(NATIVE_DRIVES, reason="needs a host without drive letters (a capability probe, Rule U)")
    def test_a_context_files_fallback_is_judged_as_the_uri_reads_it(self, kiro_dir: Path) -> None:
        """``zz//C:/../alias/x`` is retried as ``/C:/../alias/x``.

        Its ``fsPath`` reads ``alias`` in the call's folder: a link there to the model is followed."""
        symlink_or_skip(kiro_dir / "alias", kiro_dir / "model")
        context = [{"path": "zz//C:/../alias/swc/Engine.yaml"}]
        assert _is_model_denial(
            decide_kiro_tool("invoke_sub_agent", kiro_dir, name="x", prompt="x", contextFiles=context)
        )

    def test_a_reading_set_still_growing_after_the_rounds_cannot_be_judged(self) -> None:
        assert tilde_paths.derived_closure(["a"], [], lambda path: (path + "a",)) is None
        assert tilde_paths.derived_closure(["a"], ["b"], lambda path: ("c",) if path == "a" else ()) == ("a", "b", "c")

    @pytest.mark.skipif(NATIVE_DRIVES, reason="needs a host without drive letters (a capability probe, Rule U)")
    def test_the_uri_reading_is_given_a_dot_dot_reading_too(self) -> None:
        """``..`` applied first can leave ``/c:/...``, whose ``fsPath`` drops the slash before the drive."""
        forms = copilot_guard._kiro_forms(copilot_guard._KIRO_JUDGED_FOLDED["fs_write"], frozenset(), "/w/p")
        assert "c:/alias/x" in (forms("path", "../../c:/alias/x") or ())

    @pytest.mark.skipif(NATIVE_DRIVES, reason="needs a host without drive letters (a capability probe, Rule U)")
    def test_the_uri_reading_is_given_a_tilde_reading_too(self, user_home: Path) -> None:
        """``~`` expanded and ``..`` applied can leave ``/c:/...`` as well."""
        forms = copilot_guard._kiro_forms(copilot_guard._KIRO_JUDGED_FOLDED["fs_write"], frozenset(), "/w/p")
        climb = "../" * (len(user_home.parts) - 1)
        assert "c:/alias/x" in (forms("path", f"~/{climb}c:/alias/x") or ())

    @pytest.mark.skipif(NATIVE_DRIVES, reason="needs a host without drive letters (a capability probe, Rule U)")
    def test_a_dot_dot_reading_is_judged_as_the_uri_reads_it(self, kiro_dir: Path) -> None:
        """``../../c:/alias/New.yaml`` from the project is ``/c:/alias/New.yaml``.

        Its ``fsPath`` reads ``c:/alias`` in the call's folder: a link there to the model is followed."""
        if not accepts_path_segment(kiro_dir, "c:"):
            pytest.skip("the file system refuses a ':' in a name")
        (kiro_dir / "c:").mkdir()
        symlink_or_skip(kiro_dir / "c:" / "alias", kiro_dir / "model")
        climb = "../" * (len(kiro_dir.parts) - 1)
        assert _is_model_denial(decide_kiro_tool("fs_write", kiro_dir, path=f"{climb}c:/alias/New.yaml", text="x"))

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("read_file", {"path": "hop/../model/swc/Engine.yaml"}),
            ("fs_write", {"path": "hop/../model/x.yaml", "text": "x"}),
            ("invoke_sub_agent", {"name": "x", "prompt": "x", "contextFiles": [{"path": "hop/../model/x.yaml"}]}),
        ],
    )
    def test_dot_dot_after_a_link_is_applied_before_the_link(
        self, kiro_dir: Path, elsewhere: Path, tool: str, arguments: dict[str, Any]
    ) -> None:
        (elsewhere / "deep").mkdir()
        symlink_or_skip(kiro_dir / "hop", elsewhere / "deep")
        assert _is_model_denial(decide_kiro_tool(tool, kiro_dir, **arguments))

    def test_a_shell_working_directory_after_a_link_is_read_lexically(self, kiro_dir: Path, elsewhere: Path) -> None:
        (elsewhere / "deep").mkdir()
        symlink_or_skip(kiro_dir / "hop", elsewhere / "deep")
        assert _is_model_denial(decide_kiro_bash(kiro_dir, "ls", "hop/../model"))

    @pytest.mark.skipif(not _BACKSLASH_IS_A_NAME, reason="a backslash separates here (a capability probe, Rule U)")
    def test_a_backslash_after_the_tilde_is_a_name_where_it_does_not_separate(
        self, kiro_dir: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Node joins the home and everything after the ``~``: ``~\\/..`` cancels the name ``\\`` and stays in the home.

        So four ``..`` from a home three folders below the project's parent reach the project."""
        deeper = user_home / "a" / "b"
        deeper.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(deeper))
        assert _is_model_denial(
            decide_kiro_tool("read_file", kiro_dir, path="~\\/../../../../proj/model/swc/Engine.yaml")
        )

    @pytest.mark.skipif(not _BACKSLASH_IS_A_NAME, reason="a backslash separates here (a capability probe, Rule U)")
    def test_a_home_ending_in_a_backslash_keeps_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tilde_paths.Path, "home", staticmethod(lambda: Path("/home/u\\")))
        assert "/home/u\\/x" in (tilde_paths.tilde_forms("~/x") or ())

    @pytest.mark.skipif(not _BACKSLASH_IS_A_NAME, reason="a backslash separates here (a capability probe, Rule U)")
    def test_a_workspace_folder_named_with_a_backslash_is_matched(self, tmp_path: Path) -> None:
        root = tmp_path / "pro\\j"
        _write_manifest(root)
        (root / "model" / "swc").mkdir(parents=True)
        write_kiro_hooks(root)
        assert _is_model_denial(decide_kiro_tool("fs_write", root, path="pro\\j/model/x.yaml", text="x"))
        assert kiro_paths.workspace_folder_readings("pro\\j/x", frozenset({"pro\\j"})) == ("x",)

    def test_a_search_root_and_its_glob_are_judged_in_every_combination(
        self, claude_dir: Path, user_home: Path
    ) -> None:
        """The root expanded and the pattern literal: ``<home>/other/~/../../..`` is the project's parent."""
        decision = decide_claude_tool("Glob", claude_dir, path="~/other", pattern="~/../../../proj/model/**")
        assert not decision.allow

    @pytest.mark.parametrize("pattern", [["~/../proj/model/**"], ["notes/**"], 42])
    def test_a_glob_that_is_not_a_string_is_denied(self, claude_dir: Path, user_home: Path, pattern: object) -> None:
        assert not decide_claude_tool("Glob", claude_dir, path="notes", pattern=pattern).allow


class TestKiroCapturedPayloads:
    """The payloads kiro-cli 3.1.0 and the Kiro IDE sent, placed in a guarded project."""

    @pytest.mark.parametrize(
        ("name", "allowed"),
        [
            ("code-search_symbols", True),
            ("delete_file-notes", True),
            ("execute_bash-model-recon", False),
            ("execute_bash-notes", True),
            ("file_search", False),
            ("fs_append-notes", True),
            ("fs_write-model-recon", False),
            ("fs_write-notes", True),
            ("fs_write-subagent", True),
            ("grep_search-include", True),
            ("ide-list_directory", False),
            ("ide-mcp_swcraft_change_set", True),
            ("ide-mcp_swcraft_read", True),
            ("ide-tool_load", True),
            ("invoke_sub_agent", True),
            ("list_directory-absolute-depth-3", True),
            ("list_directory-depth-2", True),
            ("list_directory-model-recon", False),
            ("list_directory-no-depth", True),
            ("mcp_swcraft_discover-recon", True),
            ("read_file-model-recon", False),
            ("read_file-README", True),
            ("read_file-subagent", True),
            ("SessionStart", False),
            ("str_replace-model-recon", False),
            ("str_replace-notes", True),
            ("str_replace-README", True),
        ],
    )
    def test_each_captured_payload(self, kiro_dir: Path, name: str, allowed: bool) -> None:
        """A payload naming no tool (``SessionStart``) is denied in a guarded project; ``PreToolUse`` hooks get none."""
        payload = captured_payload(name, kiro_dir, _KIRO_FIXTURES)
        assert decide_kiro(payload, kiro_dir).allow is allowed

    def test_every_captured_payload_is_named_here(self) -> None:
        names = {path.stem for path in _KIRO_FIXTURES.glob("*.json")}
        marks = next(mark for mark in self.test_each_captured_payload.pytestmark if mark.name == "parametrize")
        assert names == {name for name, _ in marks.args[1]}

    @pytest.mark.parametrize(
        ("name", "key"),
        [
            ("read_file-README", "path"),
            ("read_file-subagent", "path"),
            ("fs_write-notes", "path"),
            ("fs_append-notes", "path"),
            ("str_replace-notes", "path"),
            ("delete_file-notes", "targetFile"),
            ("code-search_symbols", "path"),
            ("list_directory-no-depth", "path"),
        ],
    )
    def test_a_captured_payload_aimed_at_the_model_is_denied(self, kiro_dir: Path, name: str, key: str) -> None:
        payload = captured_payload(name, kiro_dir, _KIRO_FIXTURES)
        payload["tool_input"][key] = str(kiro_dir / "model" / "swc" / "Engine.yaml")
        assert not decide_kiro(payload, kiro_dir).allow

    def test_every_captured_tool_is_tabled_or_allowed(self) -> None:
        """No captured tool falls to the unknown-tool denial: each is in the table or allowed by name."""
        for path in _KIRO_FIXTURES.glob("*.json"):
            tool = json.loads(path.read_text(encoding="utf-8")).get("tool_name")
            if tool is not None:
                assert tool in copilot_guard.KIRO_JUDGED_TOOLS or copilot_guard._kiro_allowed(tool), tool

    def test_every_captured_argument_key_is_declared(self) -> None:
        """The table's closed schemas accept every key the capture recorded (or the capture would be denied)."""
        for path in _KIRO_FIXTURES.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            spec = copilot_guard.KIRO_JUDGED_TOOLS.get(payload.get("tool_name"))
            if spec is not None:
                assert copilot_guard._kiro_arguments_judgeable(spec, payload["tool_input"]), path.name


class TestKiroRender:
    """Kiro's form: a deny is exit 2 and exactly one stderr line, nothing on stdout; an allow is nothing."""

    def test_render_is_nothing_to_allow_and_the_line_to_deny(self) -> None:
        assert copilot_guard.render(copilot_guard.Decision(allow=True), "kiro") == ""
        assert copilot_guard.render(copilot_guard.Decision(allow=True), "kiro", ["Warning: x"]) == ""
        assert copilot_guard.render(copilot_guard.Decision(allow=False, reason="SWCraft: x."), "kiro") == "SWCraft: x."
        assert (
            copilot_guard.render(copilot_guard.Decision(allow=False, reason="SWCraft: x."), "kiro", ["Warning: p"])
            == "SWCraft: x; Warning: p."
        )

    @pytest.mark.parametrize(
        ("reason", "line"),
        [
            ("SWCraft: a\nb\r\nc", "SWCraft: a b  c"),
            ("SWCraft: a b\x85c\vd", "SWCraft: a b c d"),
            (None, "SWCraft: the model guard refused this call; use the SWCraft tools."),
            ("", "SWCraft: the model guard refused this call; use the SWCraft tools."),
            ("\n\n", "SWCraft: the model guard refused this call; use the SWCraft tools."),
        ],
    )
    def test_the_reason_line_is_one_line_and_never_empty(self, reason: str | None, line: str) -> None:
        assert copilot_guard.kiro_reason_line(reason) == line

    def test_notes_are_folded_into_the_line(self) -> None:
        assert (
            copilot_guard.kiro_reason_line("SWCraft: no.", ["the guard failed unexpectedly (X)", "Warning: pin\nx."])
            == "SWCraft: no; the guard failed unexpectedly (X); Warning: pin x."
        )


class TestKiroCommand:
    """``swcraft guard --host kiro`` through the entry points: exit codes, the one line, the notes."""

    @pytest.mark.parametrize("main", [entry.main, cli.main], ids=["entry", "cli"])
    def test_a_denial_is_exit_2_and_one_stderr_line(self, kiro_dir: Path, monkeypatch, capsys, main) -> None:
        stdin = json.dumps(guard_payload("fs_write", kiro_dir, path="model/swc/X.yaml", text="x")).encode("utf-8")
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, kiro_dir, main)
        assert (code, out) == (2, "")
        assert kiro_deny_line(err) == CLAUDE_EDIT_REASON.format(path="model/swc/X.yaml")

    @pytest.mark.parametrize("main", [entry.main, cli.main], ids=["entry", "cli"])
    def test_an_allow_is_exit_0_and_nothing(self, kiro_dir: Path, monkeypatch, capsys, main) -> None:
        stdin = json.dumps(guard_payload("read_file", kiro_dir, path="notes.md")).encode("utf-8")
        assert _run_kiro(stdin, monkeypatch, capsys, kiro_dir, main) == (0, "", "")

    @pytest.mark.parametrize(
        "stdin",
        [
            b"\xff\xfe{}",
            b"not json",
            b"[1, 2]",
            b'"read_file"',
            b'{"tool_input": {"path": "notes.md"}}',
            b'{"tool_name": 3, "tool_input": {"path": "notes.md"}}',
            b'{"tool_name": null, "toolName": null}',
            None,
        ],
        ids=["utf-8", "json", "array", "string", "no-tool", "number-tool", "null-tools", "no-stdin"],
    )
    @pytest.mark.parametrize("in_project", [True, False])
    def test_malformed_input_fails_closed_in_a_project(
        self, kiro_dir: Path, elsewhere: Path, monkeypatch, capsys, stdin: bytes | None, in_project: bool
    ) -> None:
        """Not a UTF-8 JSON object naming its tool: denied in a guarded project, allowed if determinably in none."""
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, kiro_dir if in_project else elsewhere)
        assert out == ""
        if in_project:
            assert code == 2
            assert kiro_deny_line(err) == _KIRO_UNNAMED_REASON
        else:
            assert code == 0
            # A payload that is no JSON object names no tool, which the note says; a JSON object naming none, with
            # no ``cwd`` and the hook outside every project, finds no project and is allowed silently.
            no_object = stdin is None or not stdin.startswith(b"{")
            assert err == (_KIRO_NO_OBJECT_NOTE if no_object else "")

    def test_a_payload_naming_no_tool_with_its_cwd_in_the_project_is_denied(
        self, kiro_dir: Path, elsewhere: Path, monkeypatch, capsys
    ) -> None:
        """The payload's own anchor finds the project although the hook runs elsewhere."""
        stdin = json.dumps({"cwd": str(kiro_dir), "tool_input": {"path": "notes.md"}}).encode("utf-8")
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, elsewhere)
        assert (code, out) == (2, "")
        kiro_deny_line(err)

    @pytest.mark.parametrize(
        ("stdin", "host_answer"),
        [
            (b"not json", "{}"),
            (b"[1]", "{}"),
            (b'{"tool_input": {"path": "model/swc/Engine.yaml"}}', "{}"),
            (b'{"tool_name": 3, "tool_input": {"path": "model/swc/Engine.yaml"}}', "{}"),
        ],
    )
    @pytest.mark.parametrize("host", ["copilot", "codex", "claude"])
    def test_malformed_input_is_unchanged_under_the_other_hosts(
        self, kiro_dir: Path, monkeypatch, capsys, stdin: bytes, host_answer: str, host: str
    ) -> None:
        write_codex_hook(kiro_dir)
        write_claude_settings(kiro_dir)
        code, out, _ = _run_kiro(stdin, monkeypatch, capsys, kiro_dir, entry.main, ("guard", "--host", host))
        assert (code, out.strip()) == (0, host_answer)

    @pytest.mark.parametrize("which", ["payload", "process"])
    @pytest.mark.parametrize("tool", ["read_file", "fs_write", "some_new_tool"])
    def test_an_exception_while_finding_the_projects_is_denied(
        self, kiro_dir: Path, monkeypatch, capsys, which: str, tool: str
    ) -> None:
        """Either anchor's discovery failing cannot rule a project out, so the call is denied.

        The failure is folded into the one line."""
        payload_cwd, process_cwd = (kiro_dir / "notes").resolve(), kiro_dir.resolve()
        trigger = payload_cwd if which == "payload" else process_cwd
        original = copilot_guard.kiro_project_directory

        def broken(directory: Path) -> Path | None:
            if Path(directory).resolve() == trigger:
                raise RuntimeError(f"broken at {kiro_dir}")
            return original(directory)

        monkeypatch.setattr(copilot_guard, "kiro_project_directory", broken)
        stdin = json.dumps(guard_payload(tool, payload_cwd, path="../notes.md", text="x")).encode("utf-8")
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, process_cwd)
        assert (code, out) == (2, "")
        line = kiro_deny_line(err)
        assert line.endswith("; the guard failed unexpectedly (RuntimeError).")
        assert str(kiro_dir) not in line

    def test_an_exception_while_finding_the_projects_allows_an_mcp_tool(
        self, kiro_dir: Path, monkeypatch, capsys
    ) -> None:
        """The unexpected-failure path applies the allowlist: SWCraft's own tools stay reachable."""

        def broken(directory: Path) -> Path | None:
            raise RuntimeError("broken")

        monkeypatch.setattr(copilot_guard, "kiro_project_directory", broken)
        stdin = json.dumps(guard_payload("mcp_swcraft_read", kiro_dir)).encode("utf-8")
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, kiro_dir)
        assert (code, out) == (0, "")
        assert err.startswith("Note: the guard failed unexpectedly (RuntimeError); this call is allowed")
        assert err.count("\n") == 1

    @pytest.mark.parametrize(
        ("host", "discovery"), [("codex", "codex_project_directory"), ("claude", "claude_project_directory")]
    )
    def test_an_exception_while_finding_the_projects_is_unchanged_under_codex_and_claude(
        self, kiro_dir: Path, monkeypatch, capsys, host: str, discovery: str
    ) -> None:
        """No project can be named, so none is known: allowed."""

        def broken(directory: Path) -> Path | None:
            raise RuntimeError("broken")

        monkeypatch.setattr(copilot_guard, discovery, broken)
        stdin = json.dumps(guard_payload("Read", kiro_dir, path="model/swc/Engine.yaml")).encode("utf-8")
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, kiro_dir, entry.main, ("guard", "--host", host))
        assert (code, out.strip()) == (0, "{}")
        assert "this call is allowed" in err

    @pytest.mark.parametrize(
        ("tool", "allowed"), [("read_file", False), ("execute_bash", False), ("mcp_swcraft_read", True)]
    )
    def test_an_unexpected_failure_is_folded_on_a_deny_and_a_note_on_an_allow(
        self, kiro_dir: Path, monkeypatch, capsys, tool: str, allowed: bool
    ) -> None:
        def fail(*_: object) -> copilot_guard.Decision:
            raise RuntimeError(f"broken at {kiro_dir}")

        monkeypatch.setattr(copilot_guard, "decide", fail)
        stdin = json.dumps(guard_payload(tool, kiro_dir, path="notes.md")).encode("utf-8")
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, kiro_dir)
        assert out == ""
        assert str(kiro_dir) not in err
        if allowed:
            assert code == 0
            assert err == (
                "Note: the guard failed unexpectedly (RuntimeError); this call is allowed: it is not a file read, "
                "search or edit, a shell command or a tool the guard does not know in a SWCraft project.\n"
            )
        else:
            assert code == 2
            assert kiro_deny_line(err) == (
                "SWCraft: the model guard could not decide this call, so a file read, search or edit, a shell command "
                "or a tool the guard does not know in this project is refused; use the SWCraft tools; the guard "
                "failed unexpectedly (RuntimeError)."
            )

    @pytest.mark.parametrize("allowed", [False, True])
    def test_a_pin_mismatch_is_folded_on_a_deny_and_a_warning_on_an_allow(
        self, kiro_dir: Path, monkeypatch, capsys, allowed: bool
    ) -> None:
        manifest = kiro_dir / "swcraft.yaml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["toolchain_version"] = "0.0.1"
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8", newline="\n")
        path = "notes.md" if allowed else "model/swc/Engine.yaml"
        stdin = json.dumps(guard_payload("read_file", kiro_dir, path=path)).encode("utf-8")
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, kiro_dir)
        assert out == ""
        if allowed:
            assert code == 0
            assert err.startswith("Warning: this project's swcraft.yaml pins toolchain_version 0.0.1")
            assert err.count("\n") == 1
        else:
            assert code == 2
            line = kiro_deny_line(err)
            assert line.startswith(MODEL_READ_REASON.rstrip("."))
            assert "; Warning: this project's swcraft.yaml pins toolchain_version 0.0.1" in line

    def test_a_reason_holding_a_line_break_is_one_line(self, kiro_dir: Path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(copilot_guard, "decide_all", lambda *_: copilot_guard.Decision(False, "SWCraft: a\nb"))
        stdin = json.dumps(guard_payload("read_file", kiro_dir, path="notes.md")).encode("utf-8")
        code, out, err = _run_kiro(stdin, monkeypatch, capsys, kiro_dir)
        assert (code, out, err) == (2, "", "SWCraft: a b\n")

    @pytest.mark.parametrize("host", ["copilot", "codex", "claude"])
    def test_the_other_hosts_return_0_on_a_denial(self, kiro_dir: Path, monkeypatch, capsys, host: str) -> None:
        """The exit-0 rule of the three JSON hosts is unchanged: their decision is on stdout."""
        write_codex_hook(kiro_dir)
        write_claude_settings(kiro_dir)
        monkeypatch.setattr("sys.stdin", type("S", (), {"buffer": __import__("io").BytesIO(json.dumps(
            guard_payload("Read", kiro_dir, path="model/swc/Engine.yaml")).encode("utf-8"))})())  # fmt: skip
        monkeypatch.chdir(kiro_dir)
        assert copilot_guard.run_hook(host) == 0
        assert "deny" in capsys.readouterr().out

    def test_kiro_returns_2_on_a_denial_and_0_on_an_allow(self, kiro_dir: Path, monkeypatch, capsys) -> None:
        for path, expected in (("model/swc/Engine.yaml", 2), ("notes.md", 0)):
            stdin = json.dumps(guard_payload("read_file", kiro_dir, path=path)).encode("utf-8")
            monkeypatch.setattr("sys.stdin", type("S", (), {"buffer": __import__("io").BytesIO(stdin)})())
            assert copilot_guard.run_hook("kiro") == expected
        assert capsys.readouterr().out == ""
