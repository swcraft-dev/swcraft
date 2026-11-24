"""``swcraft guard``'s Copilot dialect: payload shapes, tool names, paths, the saved answer and the command.

Also the project's states (absent, invalid, unreadable), the rendering, the console script and the entry points,
and the command reference's hook tuple. The helpers the dialects share are
``copilot_guard_support``'s, their project fixtures ``conftest.py``'s.

What this does not reach: whether VS Code's agent host honours the answer; no host runs here. Every case that makes
a link goes through ``symlink_or_skip``, so on a Windows runner without the symbolic-link privilege those cases skip
and prove nothing there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, _write_manifest
from copilot_guard_support import (
    CODEX_UNDECIDED_DENY,
    DEEPLY_NESTED,
    MODEL_READ_REASON,
    NATIVE_DRIVES,
    decide_payload,
    guard_payload,
    run_guard,
    saved_answer_name,
    write_codex_hook,
    write_unreadable_manifest,
)
from swcraft_tools import cli, copilot_guard, entry, project
from swcraft_tools._version import __version__
from swcraft_tools.cli import project_args
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.guide_reference import cli as guide_reference_cli
from symlink_support import symlink_or_skip

_SEARCH_REASON = f"{MODEL_READ_REASON} Narrow the search to a directory outside the model, or ask the SWCraft tools."


#: Short ids for the cases built on :data:`DEEPLY_NESTED` and a 5000-digit integer: pytest spells a
#: bytes value into the id, and an id that long overflows ``PYTEST_CURRENT_TEST`` on Windows, whose
#: environment variables hold at most 32767 characters.
_UNPARSEABLE_IDS = ["deep-nesting", "long-integer"]


# Payload shapes.


class TestPayloadShapes:
    def test_snake_case_keys(self, project_dir: Path) -> None:
        decision = decide_payload(guard_payload("Read", project_dir, path="model/swc/Engine.yaml"), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=MODEL_READ_REASON)

    def test_camel_case_keys_with_an_object(self, project_dir: Path) -> None:
        payload = {"toolName": "view", "toolArgs": {"path": "model/swc/Engine.yaml"}, "cwd": str(project_dir)}
        assert not decide_payload(payload, project_dir).allow

    def test_camel_case_keys_with_a_json_string(self, project_dir: Path) -> None:
        payload = {
            "toolName": "view",
            "toolArgs": json.dumps({"path": "model/swc/Engine.yaml"}),
            "cwd": str(project_dir),
        }
        assert not decide_payload(payload, project_dir).allow

    @pytest.mark.parametrize("arguments", ["{not json", "[1, 2]", "3", ["model"], 3])
    @pytest.mark.parametrize("tool", ["view", "Glob"])
    def test_arguments_that_are_not_a_json_object_are_denied_in_a_project(
        self, project_dir: Path, tmp_path: Path, tool: str, arguments: Any
    ) -> None:
        """An undecodable payload is never the way past the guard; outside a project nothing is guarded."""
        payload = {"toolName": tool, "toolArgs": arguments, "cwd": str(project_dir)}
        decision = decide_payload(payload, project_dir)
        assert not decision.allow
        assert (
            decision.reason
            == f"{MODEL_READ_REASON} This call's arguments could not be decoded, so its path cannot be judged."
        )
        elsewhere = {"toolName": tool, "toolArgs": arguments, "cwd": str(tmp_path)}
        assert decide_payload(elsewhere, tmp_path).allow

    @pytest.mark.parametrize(
        "payload",
        [
            {"tool_name": None, "tool_input": None, "toolName": "Read", "toolArgs": {"path": "model/swc/Engine.yaml"}},
            {"toolName": None, "toolArgs": None, "tool_name": "Read", "tool_input": {"path": "model/swc/Engine.yaml"}},
            {"tool_name": "Read", "toolName": None, "tool_input": None, "toolArgs": {"path": "model/swc/Engine.yaml"}},
        ],
        ids=["snake-null-camel-read", "camel-null-snake-read", "names-and-arguments-split-across-shapes"],
    )
    def test_a_null_under_one_key_shape_never_hides_the_other(self, project_dir: Path, payload: dict) -> None:
        assert decide_payload({**payload, "cwd": str(project_dir)}, project_dir) == copilot_guard.Decision(
            allow=False, reason=MODEL_READ_REASON
        )

    def test_both_key_shapes_agreeing_on_a_safe_path_are_allowed(self, project_dir: Path) -> None:
        arguments = {"path": "notes.md"}
        payload = {
            "tool_name": "Read",
            "toolName": "Read",
            "tool_input": arguments,
            "toolArgs": json.dumps(arguments),
            "cwd": str(project_dir),
        }
        assert decide_payload(payload, project_dir).allow

    @pytest.mark.parametrize(
        ("snake", "camel"),
        [
            (("Edit", {"path": "model/swc/Engine.yaml"}), ("Read", {"path": "notes.md"})),
            (("Read", {"path": "notes.md"}), ("Edit", {"path": "model/swc/Engine.yaml"})),
            (("Read", {"path": "notes.md"}), ("Read", {"path": "model/swc/Engine.yaml"})),
            (("Read", {"path": "notes.md"}), ("Grep", {"pattern": "x"})),
        ],
        ids=["edit-then-read", "read-then-edit", "same-tool-conflicting-arguments", "read-then-search-cwd"],
    )
    def test_conflicting_key_shapes_are_denied_when_any_pairing_is(
        self, project_dir: Path, snake: tuple[str, dict], camel: tuple[str, dict]
    ) -> None:
        """Which shape the host acts on is not known, so every name is judged with every argument set."""
        payload = {
            "tool_name": snake[0],
            "tool_input": snake[1],
            "toolName": camel[0],
            "toolArgs": camel[1],
            "cwd": str(project_dir),
        }
        assert not decide_payload(payload, project_dir).allow

    def test_conflicting_key_shapes_are_allowed_when_every_pairing_is(self, project_dir: Path) -> None:
        payload = {
            "tool_name": "Edit",
            "tool_input": {"path": "notes.md"},
            "toolName": "Read",
            "toolArgs": {"path": "notes.md"},
            "cwd": str(project_dir),
        }
        assert decide_payload(payload, project_dir).allow

    def test_no_arguments_at_all_name_no_path(self, project_dir: Path) -> None:
        assert decide_payload({"toolName": "view", "cwd": str(project_dir)}, project_dir).allow

    def test_a_payload_without_cwd_uses_the_working_directory(self, project_dir: Path, tmp_path: Path) -> None:
        payload = guard_payload("Read", None, path="model/swc/Engine.yaml")
        assert not decide_payload(payload, project_dir).allow
        assert decide_payload(payload, tmp_path).allow, "no swcraft.yaml in the fallback directory"

    def test_parse_payload_reads_utf8_bytes(self) -> None:
        data = json.dumps({"tool_name": "Read", "note": "Grüße"}, ensure_ascii=False).encode("utf-8")
        assert copilot_guard.parse_payload(data) == {"tool_name": "Read", "note": "Grüße"}

    @pytest.mark.parametrize("data", [b"not json", b"[1, 2]", b"\xff\xfe", b""])
    def test_parse_payload_refuses_what_is_not_a_json_object(self, data: bytes) -> None:
        assert copilot_guard.parse_payload(data) is None

    @pytest.mark.parametrize(
        ("data", "error"),
        [(DEEPLY_NESTED, RecursionError), (b'{"x": ' + b"9" * 5000 + b"}", ValueError)],
        ids=_UNPARSEABLE_IDS,
    )
    def test_parse_payload_raises_on_json_it_cannot_parse(self, data: bytes, error: type[Exception]) -> None:
        """JSON the interpreter cannot parse is not "not JSON": ``run_hook`` answers it as unexpected."""
        with pytest.raises(error):
            copilot_guard.parse_payload(data)


# Tool names and paths.


class TestReadTools:
    @pytest.mark.parametrize("tool", sorted(copilot_guard.READ_TOOLS))
    @pytest.mark.parametrize("key", copilot_guard.READ_PATH_KEYS)
    def test_every_read_tool_and_key_is_denied_inside_the_model(self, project_dir: Path, tool: str, key: str) -> None:
        decision = decide_payload(guard_payload(tool, project_dir, **{key: "model/swc/Engine.yaml"}), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=MODEL_READ_REASON)

    @pytest.mark.parametrize("tool", sorted(copilot_guard.READ_TOOLS))
    def test_every_read_tool_is_allowed_outside_the_model(self, project_dir: Path, tool: str) -> None:
        assert decide_payload(guard_payload(tool, project_dir, path="notes.md"), project_dir).allow
        assert decide_payload(guard_payload(tool, project_dir, path="swcraft.yaml"), project_dir).allow

    def test_the_model_root_itself_is_denied(self, project_dir: Path) -> None:
        assert not decide_payload(guard_payload("LS", project_dir, path="model"), project_dir).allow

    def test_an_absolute_path_inside_is_denied(self, project_dir: Path) -> None:
        path = str(project_dir / "model" / "swc" / "Engine.yaml")
        assert not decide_payload(guard_payload("Read", project_dir, path=path), project_dir).allow

    def test_a_relative_path_escaping_back_into_the_model_is_denied(self, project_dir: Path) -> None:
        payload = guard_payload("Read", project_dir / "views", path="../model/swc/Engine.yaml")
        assert not decide_payload(payload, project_dir, copilot_guard.read_project(project_dir)).allow

    def test_a_different_case_spelling_of_the_model_root_is_denied(self, project_dir: Path) -> None:
        assert not decide_payload(guard_payload("Read", project_dir, path="MODEL/swc/Engine.yaml"), project_dir).allow
        assert not decide_payload(guard_payload("Read", project_dir, path="Model"), project_dir).allow

    def test_a_sibling_sharing_the_prefix_is_allowed(self, project_dir: Path) -> None:
        assert decide_payload(guard_payload("Read", project_dir, path="model_notes/a.md"), project_dir).allow

    def test_a_symlink_into_the_model_is_denied(self, project_dir: Path) -> None:
        link = project_dir / "shortcut"
        symlink_or_skip(link, project_dir / "model" / "swc")
        assert not decide_payload(guard_payload("Read", project_dir, path="shortcut/Engine.yaml"), project_dir).allow

    def test_a_read_tool_without_a_path_is_allowed(self, project_dir: Path) -> None:
        assert decide_payload(guard_payload("Read", project_dir), project_dir).allow

    def test_a_path_that_cannot_be_resolved_is_denied(self, project_dir: Path) -> None:
        assert not decide_payload(guard_payload("Read", project_dir, path="model\0x"), project_dir).allow

    @pytest.mark.parametrize("tool", sorted(copilot_guard.READ_TOOLS))
    @pytest.mark.parametrize(
        ("decoy", "key"),
        [(d, k) for d in copilot_guard.READ_PATH_KEYS for k in copilot_guard.READ_PATH_KEYS if d != k],
    )
    def test_every_key_is_judged_not_only_the_first(self, project_dir: Path, tool: str, decoy: str, key: str) -> None:
        """A decoy outside the model under one key never lets another key name the model."""
        arguments = {decoy: "notes.md", key: "model/swc/Engine.yaml"}
        decision = decide_payload(guard_payload(tool, project_dir, **arguments), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=MODEL_READ_REASON)

    def test_two_keys_both_outside_the_model_are_allowed(self, project_dir: Path) -> None:
        assert decide_payload(
            guard_payload("Read", project_dir, path="notes.md", file_path="swcraft.yaml"), project_dir
        ).allow

    def test_an_unresolvable_second_key_is_denied(self, project_dir: Path) -> None:
        assert not decide_payload(
            guard_payload("Read", project_dir, path="notes.md", file_path="model\0x"), project_dir
        ).allow


class TestSearchTools:
    @pytest.mark.parametrize("tool", sorted(copilot_guard.SEARCH_TOOLS))
    def test_every_search_tool_rooted_at_the_project_is_denied(self, project_dir: Path, tool: str) -> None:
        decision = decide_payload(guard_payload(tool, project_dir, pattern="Engine"), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=_SEARCH_REASON)

    @pytest.mark.parametrize("root", ["model", "model/swc", "MODEL", ".", ".."])
    def test_a_root_at_inside_or_above_the_model_is_denied(self, project_dir: Path, root: str) -> None:
        assert not decide_payload(guard_payload("Grep", project_dir, path=root, pattern="x"), project_dir).allow

    @pytest.mark.parametrize("root", ["views", "dist", "notes.md"])
    def test_a_root_beside_the_model_is_allowed(self, project_dir: Path, root: str) -> None:
        (project_dir / "views").mkdir(exist_ok=True)
        assert decide_payload(guard_payload("Glob", project_dir, path=root, pattern="*"), project_dir).allow

    def test_a_root_that_cannot_be_resolved_is_denied(self, project_dir: Path) -> None:
        assert not decide_payload(guard_payload("Grep", project_dir, path="views\0x", pattern="x"), project_dir).allow

    def test_a_payload_without_cwd_searches_the_working_directory(self, project_dir: Path, tmp_path: Path) -> None:
        payload = guard_payload("Grep", None, pattern="x")
        assert not decide_payload(payload, project_dir).allow, "the project directory is an ancestor of the model"
        (project_dir / "views").mkdir()
        assert decide_payload(payload, project_dir / "views").allow, "no swcraft.yaml in views/"

    @pytest.mark.parametrize("tool", ["Glob", "glob"])
    def test_an_absolute_glob_into_the_model_is_denied_whatever_the_root(self, project_dir: Path, tool: str) -> None:
        (project_dir / "views").mkdir()
        pattern = str(project_dir / "model" / "**" / "*.yaml")
        decision = decide_payload(guard_payload(tool, project_dir, path="views", pattern=pattern), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=_SEARCH_REASON)

    @pytest.mark.parametrize("tool", sorted(copilot_guard.GLOB_PATH_KEYS))
    def test_a_glob_filter_climbing_into_the_model_is_denied(self, project_dir: Path, tool: str) -> None:
        (project_dir / "views").mkdir()
        payload = guard_payload(tool, project_dir, path="views", glob="../model/**/*.yaml")
        assert not decide_payload(payload, project_dir).allow

    @pytest.mark.parametrize(
        "pattern",
        [
            "{../model,x}/**",
            "{x,{y,../model}}/**",
            "{.,..}/model/*",
            ".{.,x}/model/*",
            "**/../../model/*.yaml",
            "*/../../model/**",
            "[.][.]/model/*",
            "??/model/*",
            "x\\..\\..\\model\\*",
            "\\.\\./model/*",
            "*/\\.\\./\\.\\./model/*",
            "*/[^a][^a]/[^a][^a]/model/*",
            "*/[!a][!a]/[!a][!a]/model/*",
            "*/..*/..*/model/*",
            "*/.*./model/*",
            "*/@(..)/@(..)/model/*",
            "*/+(.)/+(.)/model/*",
            "*/!(x)/!(x)/model/*",
            "{../model",
            "..*/model/*",
            pytest.param("*/" + "x" * copilot_guard._MAX_GLOB_LENGTH, id="longer-than-the-limit"),
        ],
    )
    @pytest.mark.parametrize("tool", ["Glob", "Grep"])
    def test_a_glob_that_can_climb_after_a_wildcard_or_inside_braces_is_denied(
        self, project_dir: Path, tool: str, pattern: str
    ) -> None:
        """A component that could match ``..`` after the literal prefix cannot be judged against a root.

        The rule is structural (``_can_climb``): escapes, ``[^...]``, a ``*`` beside dots and extglob are not named.
        """
        (project_dir / "docs").mkdir()
        key = "pattern" if tool == "Glob" else "glob"
        payload = guard_payload(tool, project_dir, path="docs", **{key: pattern})
        assert not decide_payload(payload, project_dir).allow

    def test_a_brace_alternative_naming_a_directory_beside_the_model_is_allowed(self, project_dir: Path) -> None:
        (project_dir / "docs").mkdir()
        (project_dir / "views").mkdir()
        payload = guard_payload("Glob", project_dir, path="docs", pattern="{../views,sub}/*.{yaml,yml}")
        assert decide_payload(payload, project_dir).allow

    def test_a_glob_past_the_expansion_bound_is_denied(self, project_dir: Path) -> None:
        (project_dir / "docs").mkdir()
        pattern = "{a,b}" * 9 + "/*.yaml"
        assert len(copilot_guard._brace_expansions("{a,b}" * 8) or []) == copilot_guard._MAX_EXPANSIONS
        assert not decide_payload(guard_payload("Glob", project_dir, path="docs", pattern=pattern), project_dir).allow

    @pytest.mark.parametrize(
        "pattern",
        [
            "**/*.yaml",
            "*.md",
            "sub/*.txt",
            "*.{yaml,yml}",
            "sub/{a,b}/*.md",
            "../views/**",
            "../notes.md",
            "sub/mod[e]l/*.yaml",
            "**/x?.md",
        ],
    )
    @pytest.mark.parametrize("tool", ["Glob", "Grep"])
    def test_a_glob_below_a_root_beside_the_model_is_allowed(self, project_dir: Path, tool: str, pattern: str) -> None:
        (project_dir / "views").mkdir()
        key = "pattern" if tool == "Glob" else "glob"
        assert decide_payload(guard_payload(tool, project_dir, path="views", **{key: pattern}), project_dir).allow

    @pytest.mark.parametrize("tool", ["Glob", "Grep"])
    def test_an_absolute_glob_below_a_directory_outside_the_project_is_allowed(
        self, project_dir: Path, tool: str
    ) -> None:
        """An absolute glob names its own root; here a sibling of the project, never an ancestor of it.

        Created rather than named (``/tmp`` is an ancestor of ``tmp_path`` on Linux), spelled ``as_posix()``
        because a backslash is denied outright.
        """
        (project_dir / "views").mkdir()
        elsewhere = project_dir.parent / "elsewhere"
        elsewhere.mkdir()
        key = "pattern" if tool == "Glob" else "glob"
        pattern = elsewhere.resolve().as_posix() + "/*"
        assert decide_payload(guard_payload(tool, project_dir, path="views", **{key: pattern}), project_dir).allow

    @pytest.mark.parametrize("tool", ["Glob", "Grep"])
    def test_an_absolute_glob_at_an_ancestor_of_the_project_is_denied(self, project_dir: Path, tool: str) -> None:
        (project_dir / "views").mkdir()
        key = "pattern" if tool == "Glob" else "glob"
        pattern = project_dir.parent.resolve().as_posix() + "/*"
        assert not decide_payload(guard_payload(tool, project_dir, path="views", **{key: pattern}), project_dir).allow

    @pytest.mark.parametrize("tool", ["Glob", "Grep"])
    def test_a_dot_star_component_is_denied_by_the_conservative_rule(self, project_dir: Path, tool: str) -> None:
        """``.*`` is denied by the conservative rule although a search below a root beside the model is safe.

        What the rule does not reach, stated: ``.*`` holds only dots and glob characters, and some shells match it to
        ``..``.
        """
        (project_dir / "views").mkdir()
        key = "pattern" if tool == "Glob" else "glob"
        assert not decide_payload(guard_payload(tool, project_dir, path="views", **{key: ".*"}), project_dir).allow

    @pytest.mark.parametrize(
        ("component", "climbs"),
        [
            ("..", True),
            ("*", False),
            ("**", False),
            ("*.yaml", False),
            ("x*", False),
            ("model", False),
            ("...", False),
            (".*", True),
            ("?", True),
            ("[e]", True),
            ("[^a]", True),
            ("@(..)", True),
            ("+(.)", True),
            ("{..", True),
            ("[a]x[b]", True),
            ("mod[e]l", False),
        ],
    )
    def test_can_climb_is_structural(self, component: str, climbs: bool) -> None:
        assert copilot_guard._can_climb(component) is climbs

    def test_the_brace_scan_is_linear(self) -> None:
        """A long run of unbalanced braces finds no group in one linear pass."""
        assert copilot_guard._first_brace_group("{" * copilot_guard._MAX_GLOB_LENGTH) is None
        assert copilot_guard._first_brace_group("{x{a,{b}},c}") == (0, 11, ["x{a,{b}}", "c"])
        assert copilot_guard._first_brace_group("{{a,b}") == (1, 5, ["a", "b"])

    def test_a_grep_pattern_is_a_regular_expression_and_never_a_path(self, project_dir: Path) -> None:
        """What the guard does not reach, stated: ``Grep``'s ``pattern`` is not judged as a path."""
        (project_dir / "views").mkdir()
        pattern = str(project_dir / "model")
        assert decide_payload(guard_payload("Grep", project_dir, path="views", pattern=pattern), project_dir).allow

    def test_a_search_rooted_at_the_filesystem_root_is_denied(self, project_dir: Path) -> None:
        """``/`` on POSIX, the drive root on Windows: a filesystem root contains anything below it."""
        anchor = Path(project_dir.resolve().anchor)
        assert not decide_payload(guard_payload("Grep", project_dir, path=str(anchor), pattern="x"), project_dir).allow


class TestListedPaths:
    """A ``Grep``'s ``paths`` list, as Agent mode's hook was observed to carry it, and its read-side effects."""

    @pytest.fixture(autouse=True)
    def _docs(self, project_dir: Path) -> None:
        (project_dir / "docs").mkdir()

    def test_a_list_of_roots_beside_the_model_is_allowed(self, project_dir: Path) -> None:
        assert decide_payload(guard_payload("Grep", project_dir, pattern="x", paths=["docs"]), project_dir).allow

    @pytest.mark.parametrize("paths", [["model"], ["docs", "model"], ["."]], ids=["model", "docs+model", "dot"])
    def test_a_list_naming_the_model_or_above_it_is_denied(self, project_dir: Path, paths: list[str]) -> None:
        decision = decide_payload(guard_payload("Grep", project_dir, pattern="x", paths=paths), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=_SEARCH_REASON)

    def test_an_empty_list_falls_back_to_the_working_directory(self, project_dir: Path) -> None:
        assert copilot_guard._written_paths({"paths": []}, copilot_guard.SEARCH_PATH_KEYS) == []
        decision = decide_payload(guard_payload("Grep", project_dir, pattern="x", paths=[]), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=_SEARCH_REASON), (
            "the project folder holds the model"
        )
        docs = project_dir / "docs"
        guarded = copilot_guard.read_project(project_dir)
        assert guarded.project is not None
        assert decide_payload(guard_payload("Grep", docs, pattern="x", paths=[]), docs, guarded).allow, (
            "docs/ is beside the model"
        )

    @pytest.mark.parametrize(("paths", "allowed"), [(["", "docs"], True), (["", "model"], False)])
    def test_an_empty_string_member_names_no_path(self, project_dir: Path, paths: list[str], allowed: bool) -> None:
        assert copilot_guard._written_paths({"paths": paths}, copilot_guard.SEARCH_PATH_KEYS) == paths[1:]
        assert (
            decide_payload(guard_payload("Grep", project_dir, pattern="x", paths=paths), project_dir).allow is allowed
        )

    def test_a_list_of_empty_strings_falls_back_to_the_working_directory(self, project_dir: Path) -> None:
        assert copilot_guard._written_paths({"paths": [""]}, copilot_guard.SEARCH_PATH_KEYS) == []
        assert not decide_payload(guard_payload("Grep", project_dir, pattern="x", paths=[""]), project_dir).allow
        docs = project_dir / "docs"
        guarded = copilot_guard.read_project(project_dir)
        assert decide_payload(guard_payload("Grep", docs, pattern="x", paths=[""]), docs, guarded).allow

    @pytest.mark.parametrize(
        ("pattern", "paths", "allowed"),
        [("swc/**", ["model"], False), ("**/*.md", ["docs"], True), ("*.md", ["docs", "model"], False)],
        ids=["paths-names-the-model", "paths-beside-the-model", "one-member-names-the-model"],
    )
    def test_a_glob_with_paths_judges_each_listed_root(
        self, project_dir: Path, pattern: str, paths: list[str], allowed: bool
    ) -> None:
        assert (
            decide_payload(guard_payload("Glob", project_dir, pattern=pattern, paths=paths), project_dir).allow
            is allowed
        )

    def test_a_string_under_paths_is_judged_as_a_string(self, project_dir: Path) -> None:
        assert not decide_payload(guard_payload("Grep", project_dir, pattern="x", paths="model"), project_dir).allow
        assert decide_payload(guard_payload("Grep", project_dir, pattern="x", paths="docs"), project_dir).allow

    @pytest.mark.parametrize("paths", [["docs", 3], [3], ["docs", None], ["docs", ["model"]], 3, {}])
    def test_an_entry_that_is_not_a_string_is_denied_unjudged(self, project_dir: Path, paths: object) -> None:
        decision = decide_payload(guard_payload("Grep", project_dir, pattern="x", paths=paths), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=_SEARCH_REASON)

    def test_paths_beside_path_are_both_judged(self, project_dir: Path) -> None:
        assert not decide_payload(
            guard_payload("Grep", project_dir, pattern="x", path="docs", paths=["model"]), project_dir
        ).allow
        assert not decide_payload(
            guard_payload("Grep", project_dir, pattern="x", path="model", paths=["docs"]), project_dir
        ).allow
        assert decide_payload(
            guard_payload("Grep", project_dir, pattern="x", path="docs", paths=["docs"]), project_dir
        ).allow

    def test_the_unjudgeable_entry_never_reaches_resolve(self, project_dir: Path, monkeypatch) -> None:
        """``_resolve`` takes strings alone: its ``_judgeable_here`` would raise on ``None``."""
        real = copilot_guard._resolve
        seen: list[object] = []

        def recording(directory: Path, written: object) -> str | None:
            seen.append(written)
            return real(directory, written)  # type: ignore[arg-type]

        monkeypatch.setattr(copilot_guard, "_resolve", recording)
        for payload in (
            guard_payload("Grep", project_dir, pattern="x", paths=[3]),
            guard_payload("Read", project_dir, path={}),
        ):
            assert not decide_payload(payload, project_dir).allow
        assert all(isinstance(written, str) for written in seen)

    @pytest.mark.parametrize(
        "payload",
        [
            {"tool_name": "Grep", "tool_input": {"pattern": "x", "paths": [3]}},
            {"tool_name": "Read", "tool_input": {"path": {}}},
        ],
        ids=["grep-paths-3", "read-path-object"],
    )
    def test_the_command_answers_an_unjudgeable_entry_with_a_deny(
        self, project_dir: Path, monkeypatch, capsys, payload: dict[str, Any]
    ) -> None:
        out, err = run_guard(json.dumps(payload).encode("utf-8"), monkeypatch, capsys, project_dir)
        assert json.loads(out)["permissionDecision"] == "deny"
        assert err == ""

    @pytest.mark.parametrize(("paths", "allowed"), [(["docs"], True), (["model"], False)])
    def test_the_observed_grep_payload_goes_through_the_command(
        self, project_dir: Path, monkeypatch, capsys, paths: list[str], allowed: bool
    ) -> None:
        payload = {"tool_name": "Grep", "tool_input": {"pattern": "Engine", "paths": paths}, "cwd": str(project_dir)}
        out, err = run_guard(json.dumps(payload).encode("utf-8"), monkeypatch, capsys, project_dir)
        assert (out.strip() == "{}") is allowed
        assert err == ""

    @pytest.mark.parametrize("path", [["model/swc/Engine.yaml"], 3], ids=["list", "integer"])
    def test_a_read_path_that_is_a_list_or_not_a_string_is_denied(self, project_dir: Path, path: object) -> None:
        decision = decide_payload(guard_payload("Read", project_dir, path=path), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=MODEL_READ_REASON)

    def test_a_read_with_a_null_path_is_allowed_as_today(self, project_dir: Path) -> None:
        assert decide_payload(guard_payload("Read", project_dir, path=None, view_range=[1, 5]), project_dir).allow

    def test_the_observed_read_of_a_saved_answer_is_allowed(self, project_dir: Path) -> None:
        saved = os.path.join(tempfile.gettempdir(), saved_answer_name())
        assert decide_payload(guard_payload("Read", project_dir, path=saved, view_range=[1, 40]), project_dir).allow

    def test_paths_is_a_search_key_and_never_a_read_key(self) -> None:
        assert copilot_guard.SEARCH_PATH_KEYS == ("path", "paths")
        assert "paths" not in copilot_guard.READ_PATH_KEYS


def _drive_spelled(path: Path, *, lower: bool = False) -> str:
    """*path* resolved and POSIX-spelled with a drive letter: its own on Windows, ``C:`` prefixed elsewhere.

    The second is a Windows host's shape, which a POSIX guard must never read as a relative directory ``C:``.
    """
    spelled = path.resolve().as_posix()
    if not PureWindowsPath(spelled).drive:
        spelled = "C:" + spelled
    return spelled[0].lower() + spelled[1:] if lower else spelled


def _rooted_without_drive(path: Path) -> str:
    """*path* resolved, POSIX-spelled, its drive (if any) removed: ``/Users/...`` on every host."""
    spelled = path.resolve().as_posix()
    return spelled[len(PureWindowsPath(spelled).drive) :]


class TestPathsReadByBothPlatformsRules:
    """Drive-letter, UNC and device forms are read by Windows' rules on every host.

    A payload's spelling does not depend on where the guard runs. Guards a ``C:/...`` glob at the project's parent
    allowed on Windows, its anchor ``C:\\`` read as a glob component.
    """

    @pytest.mark.parametrize("lower", [False, True], ids=["upper-drive", "lower-drive"])
    @pytest.mark.parametrize("tool", ["Glob", "Grep"])
    def test_a_drive_letter_glob_at_the_projects_parent_is_denied(
        self, project_dir: Path, tool: str, lower: bool
    ) -> None:
        (project_dir / "views").mkdir()
        key = "pattern" if tool == "Glob" else "glob"
        pattern = _drive_spelled(project_dir.parent, lower=lower) + "/*"
        assert not decide_payload(guard_payload(tool, project_dir, path="views", **{key: pattern}), project_dir).allow

    @pytest.mark.parametrize("tool", ["Glob", "Grep"])
    def test_a_drive_letter_glob_into_the_model_is_denied(self, project_dir: Path, tool: str) -> None:
        (project_dir / "views").mkdir()
        key = "pattern" if tool == "Glob" else "glob"
        pattern = _drive_spelled(project_dir / "model") + "/**/*.yaml"
        decision = decide_payload(guard_payload(tool, project_dir, path="views", **{key: pattern}), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=_SEARCH_REASON)

    def test_a_drive_letter_brace_alternative_at_the_projects_parent_is_denied(self, project_dir: Path) -> None:
        (project_dir / "views").mkdir()
        pattern = "{sub," + _drive_spelled(project_dir.parent) + "}/*"
        assert not decide_payload(guard_payload("Glob", project_dir, path="views", pattern=pattern), project_dir).allow

    @pytest.mark.parametrize("tool", ["Glob", "Grep"])
    def test_a_rooted_glob_without_a_drive_at_the_projects_parent_is_denied(self, project_dir: Path, tool: str) -> None:
        """On Windows the anchor of ``/x`` is ``\\``, which must not end the literal prefix either."""
        (project_dir / "views").mkdir()
        key = "pattern" if tool == "Glob" else "glob"
        pattern = _rooted_without_drive(project_dir.parent) + "/*"
        assert not decide_payload(guard_payload(tool, project_dir, path="views", **{key: pattern}), project_dir).allow

    @pytest.mark.parametrize(
        "pattern",
        [
            "//server/share/*",
            "//server/share/proj/model/**",
            "//?/C:/proj/model/*",
            "//./C:/proj/model/*",
            "///proj/model/*",
            "C:model/*",
            "c:*",
            "C:",
        ],
        ids=[
            "unc-share",
            "unc-model",
            "device-question",
            "device-dot",
            "triple-slash",
            "drive-relative",
            "drive-relative-wildcard",
            "bare-drive",
        ],
    )
    @pytest.mark.parametrize("tool", ["Glob", "Grep"])
    def test_a_unc_device_or_drive_relative_glob_is_denied(self, project_dir: Path, tool: str, pattern: str) -> None:
        """UNC, device and drive-relative globs are never judgeable.

        UNC and device forms can alias any directory, and ``C:x`` reads that drive's current directory, which no
        payload names."""
        (project_dir / "views").mkdir()
        key = "pattern" if tool == "Glob" else "glob"
        assert not decide_payload(guard_payload(tool, project_dir, path="views", **{key: pattern}), project_dir).allow

    @pytest.mark.parametrize(
        "root", ["//server/share", "//?/C:/proj", "C:views"], ids=["unc", "device", "drive-relative"]
    )
    def test_a_unc_device_or_drive_relative_search_root_is_denied(self, project_dir: Path, root: str) -> None:
        assert not decide_payload(guard_payload("Grep", project_dir, path=root, pattern="x"), project_dir).allow

    def test_a_drive_letter_glob_beside_the_model_is_judged_only_where_drives_exist(self, project_dir: Path) -> None:
        """Allowed on Windows, where it resolves outside the model; denied where no native path has a drive."""
        (project_dir / "views").mkdir()
        elsewhere = project_dir.parent / "elsewhere"
        elsewhere.mkdir()
        pattern = _drive_spelled(elsewhere) + "/*"
        assert (
            decide_payload(guard_payload("Glob", project_dir, path="views", pattern=pattern), project_dir).allow
            is NATIVE_DRIVES
        )

    @pytest.mark.parametrize("pattern", ["*.md", "sub/*.txt", "../views/**"])
    def test_an_ordinary_relative_glob_is_still_allowed(self, project_dir: Path, pattern: str) -> None:
        (project_dir / "views").mkdir()
        assert decide_payload(guard_payload("Glob", project_dir, path="views", pattern=pattern), project_dir).allow

    @pytest.mark.parametrize("lower", [False, True], ids=["upper-drive", "lower-drive"])
    @pytest.mark.parametrize("key", copilot_guard.READ_PATH_KEYS)
    def test_a_drive_letter_read_into_the_model_is_denied(self, project_dir: Path, key: str, lower: bool) -> None:
        path = _drive_spelled(project_dir / "model" / "swc" / "Engine.yaml", lower=lower)
        decision = decide_payload(guard_payload("Read", project_dir, **{key: path}), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=MODEL_READ_REASON)

    @pytest.mark.parametrize(
        "path",
        [
            "//server/share/proj/model/swc/Engine.yaml",
            "\\\\server\\share\\proj\\notes.md",
            "//localhost/C$/proj/model/swc/Engine.yaml",
            "C:model/swc/Engine.yaml",
            "c:notes.md",
        ],
        ids=["unc-forward", "unc-backslash", "admin-share", "drive-relative-model", "drive-relative-outside"],
    )
    def test_a_unc_or_drive_relative_read_is_denied(self, project_dir: Path, path: str) -> None:
        assert not decide_payload(guard_payload("Read", project_dir, path=path), project_dir).allow

    def test_a_device_form_read_into_the_model_is_denied(self, project_dir: Path) -> None:
        path = "//?/" + _drive_spelled(project_dir / "model" / "swc" / "Engine.yaml")
        assert not decide_payload(guard_payload("Read", project_dir, path=path), project_dir).allow

    def test_a_drive_letter_read_outside_the_model_is_judged_only_where_drives_exist(self, project_dir: Path) -> None:
        path = _drive_spelled(project_dir / "notes.md")
        assert decide_payload(guard_payload("Read", project_dir, path=path), project_dir).allow is NATIVE_DRIVES

    def test_an_ordinary_relative_read_is_still_allowed(self, project_dir: Path) -> None:
        assert decide_payload(guard_payload("Read", project_dir, path="notes.md"), project_dir).allow


class TestCustomAgentAliases:
    """``read`` and ``search``, the custom-agent profile's primary aliases, matched casefolded like every name."""

    @pytest.mark.parametrize("tool", ["read", "Read", "READ", "rEaD", "VIEW", "ls", "notebookread"])
    @pytest.mark.parametrize("key", copilot_guard.READ_PATH_KEYS)
    def test_a_read_alias_into_the_model_is_denied(self, project_dir: Path, tool: str, key: str) -> None:
        decision = decide_payload(guard_payload(tool, project_dir, **{key: "model/swc/Engine.yaml"}), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=MODEL_READ_REASON)

    @pytest.mark.parametrize("tool", ["read", "READ", "View", "Ls"])
    def test_a_read_alias_outside_the_model_is_allowed(self, project_dir: Path, tool: str) -> None:
        assert decide_payload(guard_payload(tool, project_dir, path="notes.md"), project_dir).allow

    @pytest.mark.parametrize("tool", ["search", "Search", "SEARCH", "GREP", "gLoB", "RG"])
    @pytest.mark.parametrize("root", ["model", "model/swc", "."])
    def test_a_search_alias_at_inside_or_above_the_model_is_denied(
        self, project_dir: Path, tool: str, root: str
    ) -> None:
        decision = decide_payload(guard_payload(tool, project_dir, path=root, pattern="Engine"), project_dir)
        assert decision == copilot_guard.Decision(allow=False, reason=_SEARCH_REASON)

    @pytest.mark.parametrize("tool", ["search", "SEARCH"])
    @pytest.mark.parametrize("key", ["pattern", "glob"])
    def test_a_search_alias_glob_into_the_model_is_denied(self, project_dir: Path, tool: str, key: str) -> None:
        (project_dir / "views").mkdir()
        payload = guard_payload(tool, project_dir, path="views", **{key: "../model/**/*.yaml"})
        assert not decide_payload(payload, project_dir).allow

    @pytest.mark.parametrize("tool", ["search", "Search", "SEARCH"])
    def test_a_search_alias_outside_the_model_is_allowed(self, project_dir: Path, tool: str) -> None:
        (project_dir / "views").mkdir()
        assert decide_payload(
            guard_payload(tool, project_dir, path="views", pattern="*.svg", glob="**/*.html"), project_dir
        ).allow

    def test_an_unexpected_failure_denies_an_alias_in_a_project(self, project_dir: Path, monkeypatch, capsys) -> None:
        def fail(*_: object) -> copilot_guard.Decision:
            raise RuntimeError("broken")

        monkeypatch.setattr(copilot_guard, "decide", fail)
        for tool in ("READ", "search"):
            stdin = json.dumps(guard_payload(tool, None, path="notes.md")).encode("utf-8")
            out, err = run_guard(stdin, monkeypatch, capsys, project_dir)
            assert json.loads(out)["permissionDecision"] == "deny"
            assert "this call is denied" in err


class TestOtherTools:
    @pytest.mark.parametrize(
        "tool",
        [
            "Edit",
            "Write",
            "Bash",
            "swcraft-read",
            "mcp_swcraft_read",
            "terminal",
            "execute",
            "shell",
            "edit",
            "web",
            "todo",
            "agent",
            "reader",
            "searches",
        ],
    )
    def test_every_other_tool_is_allowed(self, project_dir: Path, tool: str) -> None:
        payload = guard_payload(tool, project_dir, path="model/swc/Engine.yaml", file_path="model/swc/Engine.yaml")
        assert decide_payload(payload, project_dir).allow

    @pytest.mark.parametrize("tool", [None, 3, ["Read"]])
    def test_a_tool_name_that_is_not_a_string_is_allowed(self, project_dir: Path, tool: Any) -> None:
        payload = {"tool_name": tool, "tool_input": {"path": "model"}, "cwd": str(project_dir)}
        assert decide_payload(payload, project_dir).allow


# The saved answer.


class TestSavedAnswer:
    def test_a_saved_answer_in_the_temporary_directory_is_allowed(self, project_dir: Path) -> None:
        # Unresolved on purpose: on macOS `gettempdir()` is under `/var`, which resolves to `/private/var`.
        path = os.path.join(tempfile.gettempdir(), saved_answer_name())
        assert decide_payload(guard_payload("Read", project_dir, path=path), project_dir).allow

    @pytest.mark.parametrize(("tool", "key"), [("Read", "file_path"), ("NotebookRead", "notebook_path")])
    def test_a_saved_answer_beside_a_model_path_is_denied(self, project_dir: Path, tool: str, key: str) -> None:
        """The allowance holds only when every path the call carries is a saved answer; the decoy comes first."""
        saved = os.path.join(tempfile.gettempdir(), saved_answer_name())
        arguments = {"path": saved, key: "model/swc/Engine.yaml"}
        assert not decide_payload(guard_payload(tool, project_dir, **arguments), project_dir).allow

    def test_a_saved_answer_beside_a_path_outside_the_model_is_judged_as_a_read(self, project_dir: Path) -> None:
        saved = os.path.join(tempfile.gettempdir(), saved_answer_name())
        assert decide_payload(guard_payload("Read", project_dir, path=saved, file_path="notes.md"), project_dir).allow

    def test_a_saved_answer_under_every_key_is_allowed(self, project_dir: Path) -> None:
        saved = os.path.join(tempfile.gettempdir(), saved_answer_name())
        arguments = dict.fromkeys(copilot_guard.READ_PATH_KEYS, saved)
        assert decide_payload(guard_payload("NotebookRead", project_dir, **arguments), project_dir).allow

    def test_a_look_alike_inside_the_model_is_denied(self, project_dir: Path) -> None:
        path = f"model/{saved_answer_name()}"
        assert not decide_payload(guard_payload("Read", project_dir, path=path), project_dir).allow

    def test_a_saved_answer_name_linked_into_the_model_is_denied(self, project_dir: Path) -> None:
        """The allowance judges the resolved target, so a link in the temporary directory is no way in."""
        link = Path(tempfile.gettempdir()) / f"{os.getpid()}-copilot-tool-output-ab.txt"
        symlink_or_skip(link, project_dir / "model" / "swc" / "Engine.yaml")
        try:
            assert not decide_payload(guard_payload("Read", project_dir, path=str(link)), project_dir).allow
        finally:
            link.unlink()

    def test_a_look_alike_below_the_temporary_directory_is_not_a_saved_answer(self) -> None:
        nested = project.resolved_path(Path(tempfile.gettempdir()) / "sub" / saved_answer_name())
        assert not copilot_guard.is_saved_answer(nested)

    @pytest.mark.parametrize("name", ["copilot-tool-output-abc.txt.yaml", "1-copilot-tool-output-xyz.txt", "a.txt"])
    def test_other_names_in_the_temporary_directory_are_not_saved_answers(self, name: str) -> None:
        assert not copilot_guard.is_saved_answer(project.resolved_path(Path(tempfile.gettempdir()) / name))


# The project: absent, invalid, unreadable; the reason.


class TestTheProject:
    def test_no_manifest_allows_everything(self, tmp_path: Path) -> None:
        (tmp_path / "model").mkdir()
        assert decide_payload(guard_payload("Read", tmp_path, path="model/x.yaml"), tmp_path).allow
        assert decide_payload(guard_payload("Grep", tmp_path, pattern="x"), tmp_path).allow

    @pytest.mark.parametrize("kind", ["directory", "broken symlink", "unstatable"])
    def test_an_entry_that_is_no_readable_manifest_fails_closed(self, tmp_path: Path, monkeypatch, kind: str) -> None:
        """Presence is any entry at the path, never ``is_file()``: what cannot be read as one is denied."""
        root = tmp_path / "proj"
        (root / "model").mkdir(parents=True)
        manifest = root / "swcraft.yaml"
        if kind == "directory":
            manifest.mkdir()
        elif kind == "broken symlink":
            symlink_or_skip(manifest, root / "nowhere.yaml")
        else:
            real_lstat = Path.lstat

            def refuse(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
                if self == manifest:
                    raise PermissionError(13, "Permission denied", str(self))
                return real_lstat(self, *args, **kwargs)

            monkeypatch.setattr(Path, "lstat", refuse)
        assert copilot_guard.manifest_present(root)
        guarded = copilot_guard.read_project(root)
        assert guarded.present and guarded.project is None
        for payload in (guard_payload("Read", root, path="model/x.yaml"), guard_payload("Grep", root, pattern="x")):
            decision = decide_payload(payload, root, guarded)
            assert not decision.allow
            assert decision.reason is not None and str(root) not in decision.reason

    def test_only_a_missing_entry_is_absent(self, tmp_path: Path) -> None:
        assert not copilot_guard.manifest_present(tmp_path)
        assert not copilot_guard.manifest_present(tmp_path / "missing")
        (tmp_path / "file").write_text("x\n", encoding="utf-8", newline="\n")
        assert not copilot_guard.manifest_present(tmp_path / "file")

    def test_a_manifest_in_a_parent_directory_is_not_found(self, project_dir: Path) -> None:
        """Never ``load_project``'s walk: a subdirectory without its own manifest is not guarded."""
        below = project_dir / "model" / "swc"
        assert copilot_guard.read_project(below) == copilot_guard.GuardedProject(present=False)

    def test_swcraft_manifest_is_ignored(self, tmp_path: Path, project_dir: Path, monkeypatch) -> None:
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(project_dir / "swcraft.yaml"))
        assert copilot_guard.read_project(tmp_path) == copilot_guard.GuardedProject(present=False)

    @pytest.mark.parametrize(
        "text",
        [
            "name: [unclosed\n",
            "- a list\n",
            "name: p\nschema_version: '1.0'\nmodel_root: ../outside\ndist_root: dist\nviews_root: views\n"
            "outputs:\n  arxml: dist/model.arxml\n  needs: dist/needs.json\n",
        ],
        ids=["unparseable-yaml", "not-a-mapping", "model-root-outside"],
    )
    def test_an_invalid_manifest_denies_read_and_search_but_not_a_saved_answer(
        self, project_dir: Path, text: str
    ) -> None:
        (project_dir / "swcraft.yaml").write_text(text, encoding="utf-8", newline="\n")
        for payload in (
            guard_payload("Read", project_dir, path="notes.md"),
            guard_payload("view", project_dir, path="model/swc/Engine.yaml"),
            guard_payload("Glob", project_dir, path="dist", pattern="*"),
        ):
            decision = decide_payload(payload, project_dir)
            assert not decision.allow
            assert decision.reason is not None
            assert decision.reason.startswith("SWCraft: this project's swcraft.yaml cannot be read (")
            assert str(project_dir) not in decision.reason
            assert str(project_dir.resolve()) not in decision.reason
        saved = os.path.join(tempfile.gettempdir(), saved_answer_name())
        assert decide_payload(guard_payload("Read", project_dir, path=saved), project_dir).allow
        assert decide_payload(guard_payload("Edit", project_dir, path="model/swc/Engine.yaml"), project_dir).allow

    def test_an_unreadable_manifest_is_named_without_its_location(self, project_dir: Path, monkeypatch) -> None:
        manifest = project_dir / "swcraft.yaml"

        def refuse(path: str) -> project.SwcraftProject:
            raise PermissionError(13, "Permission denied", str(manifest.resolve()))

        monkeypatch.setattr(copilot_guard, "require_project", refuse)
        guarded = copilot_guard.read_project(project_dir)
        assert guarded == copilot_guard.GuardedProject(
            present=True, problem="[Errno 13] Permission denied: swcraft.yaml"
        )
        decision = decide_payload(guard_payload("Read", project_dir, path="notes.md"), project_dir, guarded)
        assert not decision.allow
        assert decision.reason is not None and str(project_dir) not in decision.reason

    def test_an_absolute_model_root_inside_the_project_is_named_relative(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        data = {
            "name": "p",
            "schema_version": "1.0",
            "model_root": str(root.resolve() / "arch" / "model"),
            "dist_root": "dist",
            "views_root": "views",
            "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
        }
        _write_manifest(root, data)
        decision = decide_payload(guard_payload("Read", root, path="arch/model/x.yaml"), root)
        assert decision.reason == MODEL_READ_REASON.replace("`model`", "`arch/model`")
        assert str(tmp_path) not in decision.reason
        assert str(tmp_path.resolve()) not in decision.reason


# Rendering.


class TestRender:
    def test_allow_is_an_empty_object(self) -> None:
        assert copilot_guard.render(copilot_guard.Decision(allow=True)) == "{}"

    @pytest.mark.parametrize("host", ["copilot", "codex", "claude"])
    def test_allow_is_an_empty_object_under_every_json_host(self, host: str) -> None:
        """Every host that reads JSON allows on ``{}``; Kiro reads none, its allow is nothing (``TestKiroRender``)."""
        assert copilot_guard.render(copilot_guard.Decision(allow=True), host) == "{}"

    @pytest.mark.parametrize("host", [None, "copilot"])
    def test_the_copilot_deny_is_byte_identical(self, host: str | None) -> None:
        """The Copilot dialect's bytes are the ones the guard printed before the Codex dialect existed."""
        decision = copilot_guard.Decision(allow=False, reason="why")
        rendered = copilot_guard.render(decision) if host is None else copilot_guard.render(decision, host)
        assert rendered == (
            '{"permissionDecision": "deny", "permissionDecisionReason": "why", "hookSpecificOutput": '
            '{"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "why"}}'
        )

    def test_the_codex_deny_carries_only_hook_specific_output(self) -> None:
        """Codex's schema allows no top-level ``permissionDecision``; one there is a failed hook, the tool run."""
        rendered = json.loads(copilot_guard.render(copilot_guard.Decision(allow=False, reason="why"), "codex"))
        assert rendered == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "why",
            }
        }

    @pytest.mark.parametrize("reason", [None, ""])
    def test_a_codex_deny_never_has_an_empty_reason(self, reason: str | None) -> None:
        """Codex refuses a deny without a non-empty reason, failing open."""
        rendered = json.loads(copilot_guard.render(copilot_guard.Decision(allow=False, reason=reason), "codex"))
        assert rendered["hookSpecificOutput"]["permissionDecisionReason"].startswith("SWCraft: ")

    def test_deny_carries_both_decision_shapes(self) -> None:
        rendered = json.loads(copilot_guard.render(copilot_guard.Decision(allow=False, reason="why")))
        assert rendered == {
            "permissionDecision": "deny",
            "permissionDecisionReason": "why",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "why",
            },
        }


# The containment helper: a filesystem root contains what is below it.


class TestContainsAtARoot:
    def test_the_posix_root(self, monkeypatch) -> None:
        monkeypatch.setattr(project.os, "sep", "/")
        assert project.path_contains("/", "/proj/model")
        assert project.path_contains("/", "/")
        assert project.path_contains("/proj", "/proj/model")
        assert not project.path_contains("/proj", "/project")

    def test_a_windows_drive_root(self, monkeypatch) -> None:
        monkeypatch.setattr(project.os, "sep", "\\")
        assert project.path_contains("C:\\", "C:\\proj\\model")
        assert project.path_contains("C:\\proj", "C:\\proj\\model")
        assert not project.path_contains("C:\\proj", "C:\\project")
        assert not project.path_contains("D:\\", "C:\\proj")


# The CLI: the command, the pin, a payload that is not JSON.


class TestTheCommand:
    def test_a_payload_that_is_not_json_is_allowed_with_a_note(self, project_dir: Path, monkeypatch, capsys) -> None:
        out, err = run_guard(b"not json", monkeypatch, capsys, project_dir)
        assert out.strip() == "{}"
        assert "not a JSON object" in err

    def test_a_denial_is_printed_and_the_command_returns(self, project_dir: Path, monkeypatch, capsys) -> None:
        stdin = json.dumps(guard_payload("Read", None, path="model/swc/Engine.yaml")).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, project_dir)
        assert json.loads(out)["permissionDecision"] == "deny"
        assert err == ""

    @pytest.mark.parametrize("allowed", [False, True])
    def test_a_pin_mismatch_warns_and_the_decision_stands(
        self, project_dir: Path, monkeypatch, capsys, allowed: bool
    ) -> None:
        manifest = project_dir / "swcraft.yaml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["toolchain_version"] = "0.0.1"
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8", newline="\n")
        path = "notes.md" if allowed else "model/swc/Engine.yaml"
        stdin = json.dumps(guard_payload("Read", project_dir, path=path)).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, project_dir)
        assert (out.strip() == "{}") is allowed
        assert err.startswith("Warning: this project's swcraft.yaml pins toolchain_version 0.0.1")
        assert __version__ in err

    def test_no_stdin_is_no_payload(self, project_dir: Path, monkeypatch, capsys) -> None:
        """A host that spawns the hook with no stdin: ``sys.stdin`` is ``None``, and the answer is still given."""
        out, err = run_guard(None, monkeypatch, capsys, project_dir)
        assert out.strip() == "{}"
        assert "not a JSON object" in err

    def test_an_invalid_manifest_denies_on_stdout_with_no_pin_warning(
        self, project_dir: Path, monkeypatch, capsys
    ) -> None:
        write_unreadable_manifest(project_dir)
        stdin = json.dumps(guard_payload("Read", None, path="notes.md")).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, project_dir)
        denied = json.loads(out)
        assert denied["permissionDecision"] == "deny"
        assert denied["permissionDecisionReason"].startswith("SWCraft: this project's swcraft.yaml cannot be read (")
        assert "Warning:" not in err

    @pytest.mark.parametrize(
        ("tool", "in_project", "allowed"),
        [("Read", True, False), ("Glob", True, False), ("Edit", True, True), ("Read", False, True)],
    )
    def test_an_unexpected_failure_is_answered_and_names_no_path(
        self, project_dir: Path, tmp_path: Path, monkeypatch, capsys, tool: str, in_project: bool, allowed: bool
    ) -> None:
        """Fail closed for a read or search in a project, open elsewhere; never a traceback or a non-zero exit."""

        def fail(*_: object) -> copilot_guard.Decision:
            raise RuntimeError(f"broken at {project_dir}")

        monkeypatch.setattr(copilot_guard, "decide", fail)
        cwd = project_dir if in_project else tmp_path
        stdin = json.dumps(guard_payload(tool, None, path="notes.md")).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, cwd)
        assert (out.strip() == "{}") is allowed
        assert "RuntimeError" in err
        assert ("this call is allowed" if allowed else "this call is denied") in err
        assert str(project_dir) not in out + err

    def test_an_unexpected_failure_counts_a_directory_manifest_as_present(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The unexpected-failure path uses the same presence rule as ``read_project``."""

        def fail(*_: object) -> copilot_guard.Decision:
            raise RuntimeError("broken")

        monkeypatch.setattr(copilot_guard, "decide", fail)
        (tmp_path / "swcraft.yaml").mkdir()
        stdin = json.dumps(guard_payload("Read", None, path="notes.md")).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, tmp_path)
        assert json.loads(out)["permissionDecision"] == "deny"
        assert "this call is denied" in err

    @pytest.mark.parametrize(("snake", "camel"), [(None, "Read"), ("Edit", "Read"), ("Read", "Edit")])
    def test_an_unexpected_failure_judges_both_tool_name_keys(
        self, project_dir: Path, monkeypatch, capsys, snake: str | None, camel: str
    ) -> None:
        """A null or a harmless name under one key never hides a read under the other."""

        def fail(*_: object) -> copilot_guard.Decision:
            raise RuntimeError("broken")

        monkeypatch.setattr(copilot_guard, "decide", fail)
        stdin = json.dumps({"tool_name": snake, "toolName": camel}).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, project_dir)
        assert json.loads(out)["permissionDecision"] == "deny"
        assert "this call is denied" in err

    @pytest.mark.parametrize(("tool", "allowed"), [("Read", False), ("update_plan", True)])
    def test_an_unexpected_failure_under_codex_names_edits_and_shell_commands(
        self, project_dir: Path, monkeypatch, capsys, tool: str, allowed: bool
    ) -> None:
        """The note names what the Codex dialect judges; the answer is rendered in Codex's shape."""

        def fail(*_: object) -> copilot_guard.Decision:
            raise RuntimeError("broken")

        monkeypatch.setattr(copilot_guard, "decide", fail)
        write_codex_hook(project_dir)
        stdin = json.dumps(guard_payload(tool, None, path="notes.md")).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, project_dir, entry.main, ("guard", "--host", "codex"))
        if allowed:
            assert out.strip() == "{}"
        else:
            assert json.loads(out) == CODEX_UNDECIDED_DENY
        assert "a file read or search, an edit or a shell command" in err

    def test_an_unexpected_failure_under_codex_without_the_hook_file_is_allowed(
        self, project_dir: Path, monkeypatch, capsys
    ) -> None:
        """Under Codex a manifest alone is no project (the hook file marks it), so the failure allows."""

        def fail(*_: object) -> copilot_guard.Decision:
            raise RuntimeError("broken")

        monkeypatch.setattr(copilot_guard, "decide", fail)
        stdin = json.dumps(guard_payload("Bash", None, command="cat model/swc/Engine.yaml")).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, project_dir, entry.main, ("guard", "--host", "codex"))
        assert out.strip() == "{}"
        assert "this call is allowed" in err

    @pytest.mark.parametrize("main", [cli.main, entry.main], ids=["cli", "entry"])
    @pytest.mark.parametrize(
        ("argv", "host"),
        [
            (("guard",), "copilot"),
            (("guard", "--host", "copilot"), "copilot"),
            (("guard", "--host", "codex"), "codex"),
            (("guard", "--host=codex"), "codex"),
        ],
    )
    def test_every_argument_list_reaches_run_hook_with_its_host(
        self, project_dir: Path, monkeypatch, capsys, main, argv: tuple[str, ...], host: str
    ) -> None:
        """Both entry points hand the host to ``run_hook``; ``--host=codex`` reaches it through ``cli.main``."""
        seen: list[str] = []
        monkeypatch.setattr(copilot_guard, "run_hook", lambda chosen="copilot": seen.append(chosen))
        run_guard(b"{}", monkeypatch, capsys, project_dir, main, argv)
        assert seen == [host]

    @pytest.mark.parametrize("main", [cli.main, entry.main], ids=["cli", "entry"])
    def test_an_unknown_host_is_a_usage_error(self, project_dir: Path, monkeypatch, capsys, main) -> None:
        """``--host other`` is argparse's usage error, exit 2, before the command runs."""
        with pytest.raises(SystemExit) as raised:
            run_guard(b"{}", monkeypatch, capsys, project_dir, main, ("guard", "--host", "other"))
        assert raised.value.code == 2
        assert "invalid choice: 'other'" in capsys.readouterr().err

    @pytest.mark.parametrize("main", [cli.main, entry.main], ids=["cli", "entry"])
    @pytest.mark.parametrize(
        ("extra", "error"),
        [(DEEPLY_NESTED[6:-1], "RecursionError"), (b"9" * 5000, "ValueError")],
        ids=_UNPARSEABLE_IDS,
    )
    @pytest.mark.parametrize("in_project", [True, False])
    def test_json_that_cannot_be_parsed_fails_closed_in_a_project(
        self, project_dir: Path, tmp_path: Path, monkeypatch, capsys, main, extra: bytes, error: str, in_project: bool
    ) -> None:
        """A read the guard denies stays denied when an extra argument breaks the outer parse."""
        cwd = project_dir if in_project else tmp_path
        payload = json.dumps(guard_payload("Read", cwd, path="model/swc/Engine.yaml")).encode("utf-8")
        stdin = payload[:-1] + b', "x": ' + extra + b"}"
        out, err = run_guard(stdin, monkeypatch, capsys, cwd, main)
        if in_project:
            assert json.loads(out)["permissionDecision"] == "deny"
            assert "this call is denied" in err
        else:
            assert out.strip() == "{}"
            assert "this call is allowed" in err
        assert error in err
        assert str(project_dir) not in out + err

    @pytest.mark.parametrize("tool", ["Read", "Glob"])
    def test_tool_arguments_that_cannot_be_parsed_are_denied(
        self, project_dir: Path, monkeypatch, capsys, tool: str
    ) -> None:
        """A ``toolArgs`` string past the parser's limits is undecodable arguments, denied in a project."""
        stdin = json.dumps({"toolName": tool, "toolArgs": '{"path": ' + "9" * 5000 + "}"}).encode("utf-8")
        out, _ = run_guard(stdin, monkeypatch, capsys, project_dir)
        assert json.loads(out)["permissionDecision"] == "deny"

    def test_guard_is_not_an_advisory_pin_command(self) -> None:
        """Its warning is its own, so the advisory set stays the two exit-zero tuples (``test_cli.py``)."""
        assert "guard" not in project_args._PIN_ADVISORY_COMMANDS


@pytest.mark.parametrize("module", ["swcraft_tools.entry", "swcraft_tools.cli"])
def test_the_real_entry_point_answers_on_stdout(project_dir: Path, module: str) -> None:
    """One child process each way through the console script's module and the module form, exit 0 both times."""
    deny = json.dumps(guard_payload("Read", project_dir, path="model/swc/Engine.yaml")).encode("utf-8")
    allow = json.dumps(guard_payload("Read", project_dir, path="notes.md")).encode("utf-8")
    results = [
        subprocess.run(
            [sys.executable, "-m", module, "guard"],
            input=stdin,
            capture_output=True,
            cwd=str(REPO_ROOT),
            env={key: value for key, value in os.environ.items() if key != "SWCRAFT_MANIFEST"},
            check=False,
        )
        for stdin in (deny, allow)
    ]
    assert [result.returncode for result in results] == [0, 0], [result.stderr for result in results]
    denied = json.loads(results[0].stdout.decode("utf-8"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert denied["permissionDecisionReason"] == MODEL_READ_REASON
    assert results[1].stdout.decode("utf-8").strip() == "{}"


@pytest.mark.parametrize(
    ("argv", "top_level", "tool", "arguments"),
    [
        (["guard"], True, "Read", {"path": "model/swc/Engine.yaml"}),
        (["guard", "--host", "copilot"], True, "Read", {"path": "model/swc/Engine.yaml"}),
        (["guard", "--host", "codex"], False, "Read", {"path": "model/swc/Engine.yaml"}),
        (["guard", "--host", "codex"], False, "Bash", {"command": "cat model/swc/Engine.yaml"}),
        (
            ["guard", "--host", "codex"],
            False,
            "apply_patch",
            {"command": "*** Begin Patch\n*** Update File: model/swc/Engine.yaml\n+x\n*** End Patch\n"},
        ),
    ],
    ids=["copilot-bare", "copilot", "codex-read", "codex-bash", "codex-patch"],
)
def test_the_console_script_answers_without_importing_cli(
    project_dir: Path, argv: list[str], top_level: bool, tool: str, arguments: dict[str, str]
) -> None:
    """``swcraft guard`` loads the manifest and never the ``cli`` package's eager imports, which cost start-up.

    For each argument list ``entry`` answers itself. What this does not reach: a pin mismatch, which imports
    ``cli`` for its sentence by design.
    """
    write_codex_hook(project_dir)
    script = (
        "import sys\n"
        f"sys.argv = {['swcraft', *argv]!r}\n"
        "from swcraft_tools.entry import main\n"
        "main()\n"
        "print('cli imported' if 'swcraft_tools.cli' in sys.modules else 'cli not imported', file=sys.stderr)\n"
    )
    stdin = json.dumps(guard_payload(tool, project_dir, **arguments)).encode("utf-8")
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=stdin,
        capture_output=True,
        cwd=str(REPO_ROOT),
        env={key: value for key, value in os.environ.items() if key != "SWCRAFT_MANIFEST"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout.decode("utf-8"))
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ("permissionDecision" in answer) is top_level
    assert result.stderr.decode("utf-8").strip() == "cli not imported"


def test_the_console_script_is_the_entry_module() -> None:
    pyproject = (REPO_ROOT / "packages" / "tools" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'swcraft = "swcraft_tools.entry:main"' in pyproject


# The command reference.


def test_the_hook_tuple_is_declared_disjoint_and_on_the_page() -> None:
    """The fifth tuple, held as ``test_guide_reference_cli.py`` holds the fourth."""
    declared = set(build_parser()._subparsers._group_actions[0].choices)
    tuple_ = set(guide_reference_cli.ANSWERS_A_HOOK_ON_STDOUT)
    assert tuple_ == {"guard"}
    assert tuple_ <= declared
    for other in (
        guide_reference_cli.WRITES_NOTHING,
        guide_reference_cli.WRITES_ONLY_WITH_REPORT,
        guide_reference_cli.ALWAYS_EXITS_ZERO,
        guide_reference_cli.REFUSES_AN_UNANSWERABLE_REQUEST,
        guide_reference_cli.REFUSES_A_MODEL_CHECK_MODEL_REFUSES,
        guide_reference_cli.WRITES_A_CHANGE_REPORT,
    ):
        assert tuple_ & set(other) == set()
    sentence = guide_reference_cli._behaviour_sentence("guard")
    page = (REPO_ROOT / "docs" / "guide" / guide_reference_cli.CLI_PATH).read_text(encoding="utf-8")
    section = page.split("## `swcraft guard`")[1].split("\n## ")[0]
    assert sentence in section
    assert "exits 0 once it runs, a denial included, under every host but Kiro, whose denial is exit 2" in sentence
    assert "A deny is exit 2 with exactly one line on stderr, the reason, and nothing on stdout" in sentence
    assert "`.kiro/hooks/`" in sentence
    assert (
        "Under Kiro a hook that cannot run, or that runs past its timeout, lets the tool run, and an older `swcraft` "
        "invoked with `--host kiro` exits 2 with its usage error" in sentence
    )
    assert "a usage error exits 2 before the command runs" in sentence
    assert "makes Copilot deny every tool call in the project, SWCraft's own tools included" in sentence
    assert "Under Codex a hook that cannot run is a failed hook, and Codex runs the tool anyway" in sentence
    assert (
        "an older `swcraft` invoked with `--host codex` rejects the option with a usage error, which Codex reads "
        "as a block of every shell command and edit" in sentence
    )
    assert "`--host`" in section
    assert "not measured" not in sentence
    assert "This command writes no file." not in section
    assert "always succeeds" not in section


# The Codex dialect's two judges.
