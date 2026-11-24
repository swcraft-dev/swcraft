"""``swcraft guard``'s Claude Code dialect: its projects, tool classes, hand-off directory and command.

Also the refusal to repair an unreadable manifest, the MSYS drive spellings, the rendering and the payloads Claude
Code 2.1.319 sent (``fixtures/claude_hook/``). The helpers the dialects share are
``copilot_guard_support``'s, their project fixtures ``conftest.py``'s.

What this does not reach: whether Claude Code honours the answer; no host runs here. Every case that makes a link
goes through ``symlink_or_skip``, so on a Windows runner without the symbolic-link privilege those cases skip and
prove nothing there; the MSYS deny cases run only where paths have drive letters.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import _write_manifest
from copilot_guard_support import (
    BY_HAND,
    CLAUDE_EDIT_REASON,
    CLAUDE_FIXTURES,
    GUARD_INPUT_REASON,
    HANDOFF_REASON,
    MODEL_READ_REASON,
    NATIVE_DRIVES,
    SHARED_GUARD_INPUTS,
    SHELL_REASON,
    UNJUDGED_MARK,
    UNREADABLE_MANIFESTS,
    captured_payload,
    codex_patch,
    codex_project,
    decide_claude,
    decide_claude_bash,
    decide_claude_tool,
    decide_codex_shell,
    decide_codex_tool,
    decide_payload,
    guard_payload,
    handoff_reviews,
    run_guard,
    swcraft_home,
    write_claude_settings,
    write_codex_hook,
    write_unreadable_manifest,
)
from swcraft_tools import cli, copilot_guard, entry, project
from symlink_support import symlink_or_skip

_POWERSHELL_REASON = (
    "SWCraft: in this project the PowerShell tool is refused: the model guard reads bash, not PowerShell, so what "
    "this command touches cannot be judged. Use the Bash tool, or the SWCraft tools for the model."
)


_CLAUDE_UNDECIDED_REASON = (
    "SWCraft: the model guard could not decide this call, so a file read, search or edit, or a shell command in "
    "this project is refused; use the SWCraft tools."
)


class TestClaudeProject:
    """Which project the Claude dialect guards: the nearest folder with ``swcraft.yaml`` and either settings file."""

    @pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
    def test_either_settings_file_marks_the_project(self, project_dir: Path, user_home: Path, name: str) -> None:
        write_claude_settings(project_dir, name)
        assert not decide_claude_tool("Edit", project_dir, file_path="model/swc/Engine.yaml").allow

    def test_a_project_without_a_settings_file_is_not_guarded(self, project_dir: Path, user_home: Path) -> None:
        """A hook declared only in the user's settings finds no project and allows everything (the stated limit)."""
        assert decide_claude_tool("Edit", project_dir, file_path="model/swc/Engine.yaml").allow
        assert decide_claude_bash(project_dir, "rm -rf model").allow
        assert decide_claude_tool("Read", project_dir, file_path="model/swc/Engine.yaml").allow

    def test_the_codex_hook_file_does_not_mark_a_claude_project(self, codex_dir: Path, user_home: Path) -> None:
        assert decide_claude_tool("Write", codex_dir, file_path="model/swc/Engine.yaml", content="x").allow

    def test_a_session_in_a_subdirectory_is_guarded(self, claude_dir: Path) -> None:
        below = claude_dir / "notes"
        assert decide_claude_tool("Edit", below, file_path="../model/swc/Engine.yaml") == copilot_guard.Decision(
            allow=False, reason=CLAUDE_EDIT_REASON.format(path="model/swc/Engine.yaml")
        )
        assert decide_claude_bash(below, "cat ../model/swc/Engine.yaml") == copilot_guard.Decision(
            allow=False, reason=SHELL_REASON
        )
        assert decide_claude_tool("Edit", below, file_path="readme.md").allow

    def test_a_nested_manifest_without_a_settings_file_is_passed_over(self, claude_dir: Path) -> None:
        nested = claude_dir / "notes" / "inner"
        _write_manifest(nested)
        assert not decide_claude_tool("Write", nested, file_path="../../model/swc/Engine.yaml", content="x").allow

    def test_copilot_never_walks_upward(self, claude_dir: Path) -> None:
        assert copilot_guard.guarded_project(claude_dir / "notes", "copilot") == copilot_guard.GuardedProject(
            present=False
        )

    def test_an_unreadable_manifest_denies_reads_searches_edits_and_commands(self, claude_dir: Path) -> None:
        write_unreadable_manifest(claude_dir)
        for decision in (
            decide_claude_tool("Read", claude_dir, file_path="notes.md"),
            decide_claude_tool("Grep", claude_dir, pattern="x", path="notes"),
            decide_claude_tool("Edit", claude_dir, file_path="notes.md", old_string="a", new_string="b"),
            decide_claude_bash(claude_dir, "ls notes"),
        ):
            assert not decision.allow
            assert decision.reason is not None
            assert decision.reason.startswith("SWCraft: this project's swcraft.yaml cannot be read (")
            assert decision.reason.endswith(BY_HAND)
            assert str(claude_dir) not in decision.reason

    @pytest.mark.parametrize("kind", list(UNREADABLE_MANIFESTS))
    @pytest.mark.parametrize(
        ("cwd", "tool", "arguments"),
        [
            (".", "Read", {"file_path": "swcraft.yaml"}),
            ("notes", "Read", {"file_path": "../swcraft.yaml"}),
            (".", "Edit", {"file_path": "swcraft.yaml", "old_string": "[", "new_string": "x"}),
            ("notes", "Write", {"file_path": "../swcraft.yaml", "content": "x"}),
            (".", "MultiEdit", {"file_path": "swcraft.yaml", "edits": []}),
            (".", "Bash", {"command": "cat swcraft.yaml"}),
            (".", "Bash", {"command": "echo 'name: x' > swcraft.yaml"}),
            (".", "Monitor", {"command": "cat swcraft.yaml"}),
            (".", "Monitor", {"command": "tail -f notes.md"}),
        ],
    )
    def test_an_unreadable_manifest_is_never_read_or_edited_by_the_agent(
        self, claude_dir: Path, kind: str, cwd: str, tool: str, arguments: dict[str, Any]
    ) -> None:
        """With ``swcraft.yaml`` unreadable, Claude Code never reads or edits it; the reason says to fix it by hand."""
        write_unreadable_manifest(claude_dir, UNREADABLE_MANIFESTS[kind])
        assert copilot_guard.read_project(claude_dir).project is None
        decision = decide_claude_tool(tool, claude_dir / cwd, **arguments)
        assert not decision.allow
        assert decision.reason is not None
        assert decision.reason.startswith("SWCraft: this project's swcraft.yaml cannot be read (")
        assert decision.reason.endswith(BY_HAND)
        assert str(claude_dir) not in decision.reason

    def test_a_payload_outside_a_guarded_project_is_allowed(self, tmp_path: Path, user_home: Path) -> None:
        (tmp_path / "model").mkdir()
        assert decide_claude_bash(tmp_path, "cat model/x").allow
        assert decide_claude_tool("PowerShell", tmp_path, command="Remove-Item model").allow


def _case_twin_or_skip(directory: Path) -> Path:
    """``SWCRAFT.YAML`` beside *directory*'s ``swcraft.yaml`` as a second file, or skip.

    A capability probe, not a platform check -- on a case-insensitive filesystem (macOS's default APFS, NTFS) the two
    are one file."""
    twin = directory / "SWCRAFT.YAML"
    try:
        with twin.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write("{}\n")
    except FileExistsError:
        pytest.skip("the filesystem is case-insensitive: SWCRAFT.YAML and swcraft.yaml are one file")
    if os.path.samefile(twin, directory / "swcraft.yaml"):  # pragma: no cover - a folding filesystem, created anew
        pytest.skip("the filesystem is case-insensitive: SWCRAFT.YAML and swcraft.yaml are one file")
    return twin


class TestNoRepairOfAnUnreadableManifest:
    """Writing a guard input via an unreadable manifest is denied; a build names it by its exact resolved spelling."""

    def test_a_manifest_linked_onto_the_claude_settings_is_denied(self, claude_dir: Path) -> None:
        manifest = claude_dir / "swcraft.yaml"
        manifest.unlink()
        symlink_or_skip(manifest, Path(".claude") / "settings.json")
        assert copilot_guard.read_project(claude_dir).project is None
        assert not decide_claude_tool(
            "Edit", claude_dir, file_path="swcraft.yaml", old_string="{", new_string="x"
        ).allow
        assert not decide_claude_tool("Write", claude_dir, file_path="swcraft.yaml", content="x").allow
        assert not decide_claude_tool("Write", claude_dir, file_path=".claude/settings.json", content="x").allow

    def test_the_codex_hook_linked_onto_a_regular_manifest_is_denied(self, codex_dir: Path) -> None:
        """Hook JSON without ``name`` in a regular ``swcraft.yaml`` the hook file links onto."""
        (codex_dir / "swcraft.yaml").write_text('{"hooks": {}}\n', encoding="utf-8", newline="\n")
        hook = codex_dir / ".codex" / "hooks.json"
        hook.unlink()
        symlink_or_skip(hook, Path("..") / "swcraft.yaml")
        assert copilot_guard.read_project(codex_dir).project is None
        assert not decide_codex_tool(
            "apply_patch", codex_dir, command=codex_patch("*** Update File: swcraft.yaml")
        ).allow
        assert not decide_codex_shell(codex_dir, "cat swcraft.yaml").allow
        assert not decide_codex_shell(codex_dir, "printf '{}' | tee swcraft.yaml").allow

    def test_a_linked_guard_folder_is_denied(self, claude_dir: Path) -> None:
        """A guard folder linked elsewhere, holding a hook script that aliases the manifest."""
        write_unreadable_manifest(claude_dir)
        (claude_dir / "elsewhere").mkdir()
        symlink_or_skip(claude_dir / "elsewhere" / "hook.sh", Path("..") / "swcraft.yaml")
        (claude_dir / ".github").mkdir()
        symlink_or_skip(claude_dir / ".github" / "hooks", Path("..") / "elsewhere")
        assert not decide_claude_tool(
            "Edit", claude_dir, file_path="swcraft.yaml", old_string="[", new_string="x"
        ).allow
        assert not decide_claude_tool("Read", claude_dir, file_path="swcraft.yaml").allow
        assert not decide_claude_bash(claude_dir, "cat swcraft.yaml").allow

    def test_a_case_twin_of_an_unreadable_manifest_is_denied(self, claude_dir: Path) -> None:
        """Where the filesystem is case-sensitive, ``SWCRAFT.YAML`` is another file."""
        write_codex_hook(claude_dir)
        write_unreadable_manifest(claude_dir)
        _case_twin_or_skip(claude_dir)
        assert not decide_claude_tool(
            "Edit", claude_dir, file_path="SWCRAFT.YAML", old_string="{", new_string="x"
        ).allow
        assert not decide_codex_tool(
            "apply_patch", claude_dir, command=codex_patch("*** Update File: SWCRAFT.YAML")
        ).allow

    def test_a_build_of_a_case_twin_is_no_build_of_the_guarded_manifest(self, codex_dir: Path) -> None:
        """The build allowance checks the guarded manifest's outputs, so a build naming another file is not one."""
        _case_twin_or_skip(codex_dir)
        assert decide_codex_shell(codex_dir, "swcraft validate --project swcraft.yaml").allow
        assert not decide_codex_shell(codex_dir, "swcraft validate --project SWCRAFT.YAML").allow

    def test_a_build_names_the_manifest_by_its_exact_resolved_spelling(self, codex_dir: Path) -> None:
        """Exact equality of the *resolved* spelling, never a casefolded one: another resolved string is denied.

        Resolution is the host's, so the case asks it (a capability probe): Windows' ``Path.resolve()`` gives the
        spelling on disk, so there ``SWCRAFT.YAML`` is that build; macOS and Linux keep the spelling written.
        """
        assert decide_codex_shell(codex_dir, "swcraft validate --project ./swcraft.yaml").allow
        guarded = project.resolved_path(codex_dir / "swcraft.yaml")
        for spelling in ("SWCRAFT.YAML", "Swcraft.yaml"):
            same_string = project.resolved_path(codex_dir / spelling) == guarded
            assert decide_codex_shell(codex_dir, f"swcraft validate --project {spelling}").allow is same_string, (
                spelling
            )

    def test_a_link_to_the_manifest_from_another_folder_is_no_build(self, codex_dir: Path) -> None:
        """A link to the manifest from another folder is no build: the project folder's outputs are checked.

        The loader bases the outputs on the given path's folder, so ``.claude/alias.yaml -> ../swcraft.yaml`` with
        ``outputs.needs: settings.json`` would write ``.claude/settings.json``.
        """
        (codex_dir / ".claude").mkdir(exist_ok=True)
        try:
            (codex_dir / ".claude" / "alias.yaml").symlink_to(Path("..") / "swcraft.yaml")
        except OSError:
            pytest.skip("no symbolic links on this filesystem")
        assert decide_codex_shell(codex_dir, "swcraft generate needs --project swcraft.yaml").allow
        assert not decide_codex_shell(codex_dir, "swcraft generate needs --project .claude/alias.yaml").allow
        assert not decide_codex_shell(
            codex_dir, "SWCRAFT_MANIFEST=.claude/alias.yaml swcraft validate --project swcraft.yaml"
        ).allow


class TestClaudeSessionProject:
    """The call is judged against the project the walk from ``cwd`` finds and the one ``CLAUDE_PROJECT_DIR`` names.

    Its folder must hold the markers; ``cwd`` follows any ``cd``, out or nested, the session's project staying guarded.
    """

    def test_a_cwd_outside_the_project_still_guards_the_sessions_model(
        self, claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "added-dir"
        elsewhere.mkdir()
        model_file = str(claude_dir / "model" / "swc" / "Engine.yaml")
        assert decide_claude_tool("Edit", elsewhere, file_path=model_file).allow  # the walk alone finds nothing
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(claude_dir))
        assert decide_claude_tool("Edit", elsewhere, file_path=model_file) == copilot_guard.Decision(
            allow=False, reason=CLAUDE_EDIT_REASON.format(path="model/swc/Engine.yaml")
        )
        assert not decide_claude_tool("Read", elsewhere, file_path=model_file).allow
        assert not decide_claude_bash(elsewhere, f"cat {shlex.quote(model_file)}").allow
        assert not decide_claude_tool(
            "Write", elsewhere, file_path=str(claude_dir / ".claude" / "settings.local.json")
        ).allow
        assert decide_claude_tool("Edit", elsewhere, file_path=str(elsewhere / "x.md")).allow

    def test_a_cd_into_a_nested_project_still_guards_the_outer_model(
        self, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inner = codex_project(claude_dir / "notes" / "inner", "design")  # a model folder named apart from the outer
        write_claude_settings(inner)
        outer_model = "../../model/swc/Engine.yaml"
        assert decide_claude_tool("Edit", inner, file_path=outer_model).allow  # the walk stops at the inner project
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(claude_dir))
        assert not decide_claude_tool("Edit", inner, file_path=outer_model).allow
        assert not decide_claude_bash(inner, f"rm {outer_model}").allow
        assert not decide_claude_tool("Edit", inner, file_path="design/x.yaml").allow  # the inner project's model too

    @pytest.mark.parametrize("value", ["", "not-a-project"])
    def test_a_variable_naming_no_project_adds_none(
        self, claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """The limit, pinned: a variable naming no project adds nothing.

        Unset, empty or naming a folder without the markers, the variable adds nothing, and a ``cwd`` outside the
        project is then unguarded."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        if value:
            (tmp_path / value).mkdir()
            value = str(tmp_path / value)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", value)
        assert copilot_guard.claude_session_project() is None
        assert decide_claude_tool("Edit", elsewhere, file_path=str(claude_dir / "model" / "swc" / "Engine.yaml")).allow

    def test_one_project_found_both_ways_is_judged_once(
        self, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(claude_dir))
        assert len(copilot_guard.guarded_projects(claude_dir / "notes", "claude")) == 1

    def test_two_folders_are_one_project_only_when_their_identity_on_disk_is_one(self) -> None:
        """Names differing only by case stay two projects unless the injected identity says one folder.

        The tables are keyed by the exact POSIX spelling, never by the ``Path``: a ``WindowsPath`` compares and
        hashes casefolded, so a table keyed by it would hold one entry and test the table, not the rule.
        """
        upper, lower, other = Path("/work/Project"), Path("/work/project"), Path("/elsewhere")
        same_folder = {
            "/work/Project": ("inode", 1, 7),
            "/work/project": ("inode", 1, 7),
            "/elsewhere": ("inode", 1, 9),
        }
        two_folders = {"/work/Project": ("inode", 1, 7), "/work/project": ("inode", 1, 8)}

        def identity(table: dict[str, tuple[object, ...]]) -> Callable[[Path], tuple[object, ...]]:
            return lambda directory: table[directory.as_posix()]

        def spellings(directories: list[Path]) -> list[str]:
            return [directory.as_posix() for directory in directories]

        assert spellings(copilot_guard.distinct_directories([lower, upper], identity(same_folder))) == ["/work/project"]
        assert spellings(copilot_guard.distinct_directories([lower, upper], identity(two_folders))) == [
            "/work/project",
            "/work/Project",
        ]
        assert spellings(copilot_guard.distinct_directories([lower, other], identity(same_folder))) == [
            "/work/project",
            "/elsewhere",
        ]

    def test_an_identity_falls_back_to_the_exact_spelling_never_a_casefolded_one(self, tmp_path: Path) -> None:
        missing = tmp_path / "Gone"
        assert copilot_guard.directory_identity(missing) == ("path", str(missing))
        assert copilot_guard.directory_identity(missing) != copilot_guard.directory_identity(tmp_path / "gone")
        status = tmp_path.stat()
        if status.st_ino:  # a filesystem without file ids identifies by spelling, pinned above
            assert copilot_guard.directory_identity(tmp_path) == ("inode", status.st_dev, status.st_ino)

    def test_folders_differing_only_by_case_are_two_projects_where_the_filesystem_holds_both(
        self, tmp_path: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A capability probe, not a platform check.

        On a case-insensitive filesystem (macOS's default APFS, NTFS) ``Work`` and ``work`` are one folder and the
        case cannot arise."""
        upper, lower = tmp_path / "Work", tmp_path / "work"
        upper.mkdir()
        try:
            lower.mkdir()
        except FileExistsError:
            pytest.skip("the filesystem is case-insensitive: Work and work are one folder")
        if os.path.samefile(upper, lower):
            pytest.skip("the filesystem is case-insensitive: Work and work are one folder")
        _write_manifest(
            upper,
            {
                "name": "upper",
                "schema_version": "1.0",
                "model_root": "arch",
                "dist_root": "dist",
                "views_root": "views",
                "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
            },
        )
        _write_manifest(lower)
        for root in (upper, lower):
            write_claude_settings(root)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(upper))
        assert len(copilot_guard.guarded_projects(lower, "claude")) == 2
        assert not decide_claude_tool("Edit", lower, file_path=str(upper / "arch" / "x.yaml")).allow

    def test_the_codex_dialect_does_not_read_the_variable(
        self, codex_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_claude_settings(codex_dir)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(codex_dir))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        assert copilot_guard.guarded_projects(elsewhere, "codex") == [copilot_guard.GuardedProject(present=False)]

    def test_the_command_judges_against_the_sessions_project(
        self, claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        elsewhere = tmp_path / "added-dir"
        elsewhere.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(claude_dir))
        stdin = json.dumps(guard_payload("Write", elsewhere, file_path=str(claude_dir / "model" / "x.yaml"))).encode(
            "utf-8"
        )
        out, _ = run_guard(stdin, monkeypatch, capsys, elsewhere, entry.main, ("guard", "--host", "claude"))
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestClaudeToolClasses:
    """Reads, searches as Copilot's; writes by path; ``Bash`` by the shell judge; ``PowerShell`` denied; others pass."""

    def test_a_read_into_the_model_is_denied(self, claude_dir: Path) -> None:
        decision = decide_claude_tool("Read", claude_dir, file_path=str(claude_dir / "model" / "swc" / "Engine.yaml"))
        assert decision == copilot_guard.Decision(allow=False, reason=MODEL_READ_REASON)
        assert decide_claude_tool("Read", claude_dir, file_path="notes.md").allow

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("Grep", {"pattern": "x", "path": "model"}),
            ("Grep", {"pattern": "x"}),
            ("Glob", {"pattern": "model/**/*.yaml"}),
            ("Grep", {"pattern": "x", "path": "notes", "glob": "../model/*"}),
        ],
        ids=["grep-model", "grep-project-root", "glob-model", "glob-climbs"],
    )
    def test_a_search_reaching_the_model_is_denied(self, claude_dir: Path, tool: str, arguments: dict) -> None:
        """``Grep``/``Glob`` were absent from 2.1.319's captured tool list and stay judged."""
        assert not decide_claude_tool(tool, claude_dir, **arguments).allow

    def test_a_search_outside_the_model_is_allowed(self, claude_dir: Path) -> None:
        assert decide_claude_tool("Grep", claude_dir, pattern="x", path="notes").allow
        assert decide_claude_tool("Glob", claude_dir, pattern="*.md", path="notes").allow

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("Edit", {"file_path": "model/swc/Engine.yaml", "old_string": "a", "new_string": "b"}),
            ("Write", {"file_path": "model/swc/New.yaml", "content": "x"}),
            ("MultiEdit", {"file_path": "model/swc/Engine.yaml", "edits": []}),
            ("NotebookEdit", {"notebook_path": "model/swc/Engine.yaml", "cell_id": "c", "new_source": "x"}),
            ("edit", {"file_path": "model/swc/Engine.yaml"}),
        ],
        ids=["edit", "write", "multiedit", "notebookedit", "casefolded"],
    )
    def test_a_write_into_the_model_is_denied(self, claude_dir: Path, tool: str, arguments: dict) -> None:
        decision = decide_claude_tool(tool, claude_dir, **arguments)
        assert not decision.allow
        assert decision.reason is not None and decision.reason.startswith(
            "SWCraft: in this project the model directory (`model`) is changed through the SWCraft tools"
        )

    def test_the_write_reason_names_the_file_project_relative(self, claude_dir: Path) -> None:
        decision = decide_claude_tool(
            "Write", claude_dir, file_path=str(claude_dir / "model" / "swc" / "New.yaml"), content=""
        )
        assert decision == copilot_guard.Decision(
            allow=False, reason=CLAUDE_EDIT_REASON.format(path="model/swc/New.yaml")
        )

    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
    def test_a_write_outside_the_model_is_allowed(self, claude_dir: Path, tool: str) -> None:
        assert decide_claude_tool(tool, claude_dir, file_path="notes/n.md", notebook_path="notes/nb.ipynb").allow

    def test_every_write_path_key_is_judged(self, claude_dir: Path) -> None:
        """``file_path`` and ``notebook_path`` both: one inside the model denies the call."""
        assert not decide_claude_tool(
            "NotebookEdit", claude_dir, file_path="notes/x", notebook_path="model/swc/x"
        ).allow
        assert not decide_claude_tool("Edit", claude_dir, file_path="model/swc/x", notebook_path="notes/x").allow

    def test_the_read_path_key_path_is_no_write_path(self, claude_dir: Path) -> None:
        """No Claude write tool carries ``path``; the dialect judges ``file_path`` and ``notebook_path`` alone."""
        assert decide_claude_tool("Write", claude_dir, file_path="notes/x", path="model/swc/x").allow

    def test_a_symlink_into_the_model_is_judged_as_what_it_resolves_to(self, claude_dir: Path) -> None:
        symlink_or_skip(claude_dir / "notes" / "alias", claude_dir / "model" / "swc")
        assert not decide_claude_tool("Edit", claude_dir, file_path="notes/alias/Engine.yaml").allow
        assert not decide_claude_bash(claude_dir, "cat notes/alias/Engine.yaml").allow

    @pytest.mark.parametrize(
        ("command", "allowed"),
        [
            ("ls model", False),
            ("rm -rf model/swc", False),
            ("echo x > model/swc/Engine.yaml", False),
            ("ls notes", True),
            ("git status", True),
            ("pwd", True),
        ],
    )
    def test_bash_is_the_codex_shell_judge(self, claude_dir: Path, command: str, allowed: bool) -> None:
        decision = decide_claude_bash(claude_dir, command)
        assert decision.allow is allowed
        if not allowed:
            assert decision == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    def test_bash_resolves_against_the_payloads_cwd(self, claude_dir: Path) -> None:
        """Claude sends no ``workdir``; after ``cd model`` its ``cwd`` is the model directory."""
        assert not decide_claude_bash(claude_dir / "model", "ls").allow
        assert not decide_claude_bash(claude_dir / "notes", "ls ../model").allow
        assert decide_claude_bash(claude_dir / "notes", "ls").allow

    @pytest.mark.parametrize("command", ["Get-ChildItem notes", "Remove-Item model -Recurse", "", None])
    def test_powershell_is_denied_whatever_it_runs(self, claude_dir: Path, command: object) -> None:
        assert decide_claude_tool("PowerShell", claude_dir, command=command) == copilot_guard.Decision(
            allow=False, reason=_POWERSHELL_REASON
        )

    def test_powershell_is_denied_with_an_unreadable_manifest_too(self, claude_dir: Path) -> None:
        write_unreadable_manifest(claude_dir)
        assert not decide_claude_tool("PowerShell", claude_dir, command="Get-Content swcraft.yaml").allow

    @pytest.mark.parametrize(
        "tool", ["Agent", "mcp__swcraft__change_set", "mcp__swcraft__read", "WebFetch", "TodoWrite", "ToolSearch"]
    )
    def test_every_other_tool_is_allowed(self, claude_dir: Path, tool: str) -> None:
        assert decide_claude_tool(tool, claude_dir, command="rm -rf model", file_path="model/x", path="model/x").allow

    @pytest.mark.parametrize(
        ("command", "allowed"),
        [
            ("tail -f model/swc/Engine.yaml", False),
            ("printf x > model/swc/New.yaml", False),
            ("tail -f notes.md", True),
        ],
    )
    def test_monitor_is_judged_as_bash(self, claude_dir: Path, command: str, allowed: bool) -> None:
        """Claude Code 2.1.319's ``Monitor`` runs ``command`` "in the same shell environment as Bash": a Bash call."""
        decision = decide_claude_tool("Monitor", claude_dir, command=command, description="x", timeout_ms=1000)
        assert decision.allow is allowed
        if not allowed:
            assert decision == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    def test_a_monitor_without_a_command_is_denied(self, claude_dir: Path) -> None:
        """A WebSocket ``Monitor`` (``ws``) names no command the judge can read: denied, the stated over-reach."""
        assert not decide_claude_tool("Monitor", claude_dir, ws={"url": "wss://example.invalid"}, description="x").allow

    def test_copilot_still_allows_an_edit_of_the_model(self, claude_dir: Path) -> None:
        assert decide_payload(guard_payload("Edit", claude_dir, file_path="model/swc/Engine.yaml"), claude_dir).allow


class TestClaudeGuardInputs:
    """The guard inputs both dialects share: no edit or writing command, only a read names them."""

    @pytest.mark.parametrize("name", SHARED_GUARD_INPUTS)
    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
    def test_an_edit_of_a_guard_input_is_denied(self, claude_dir: Path, tool: str, name: str) -> None:
        assert decide_claude_tool(tool, claude_dir, file_path=name) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="edit")
        )

    def test_a_guard_input_named_through_a_subfolder_is_denied(self, claude_dir: Path) -> None:
        assert not decide_claude_tool("Write", claude_dir / "notes", file_path="../.claude/settings.local.json").allow
        assert not decide_claude_tool("Write", claude_dir, file_path=str(claude_dir / ".mcp.json")).allow

    @pytest.mark.parametrize("name", SHARED_GUARD_INPUTS)
    def test_a_read_of_a_guard_input_is_allowed(self, claude_dir: Path, name: str) -> None:
        assert decide_claude_tool("Read", claude_dir, file_path=name).allow
        assert decide_claude_bash(claude_dir, f"cat {name}").allow

    @pytest.mark.parametrize(
        "command",
        [
            "echo '{\"disableAllHooks\": true}' > .claude/settings.local.json",
            "rm -rf .claude",
            "cp notes.md .mcp.json",
            "rm .github/hooks/swcraft.json",
            "echo x > .codex/hooks.json",
            "sed -i s/a/b/ swcraft.yaml",
        ],
    )
    def test_a_command_writing_a_guard_input_is_denied(self, claude_dir: Path, command: str) -> None:
        assert decide_claude_bash(claude_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    def test_the_target_of_a_link_in_the_claude_folder_is_a_guard_input(self, claude_dir: Path) -> None:
        settings = claude_dir / ".claude" / "settings.json"
        settings.replace(claude_dir / "settings-input.json")
        symlink_or_skip(settings, Path("..") / "settings-input.json")
        assert not decide_claude_tool("Write", claude_dir, file_path="settings-input.json").allow
        assert not decide_claude_bash(claude_dir, "echo x > settings-input.json").allow
        assert decide_claude_tool("Read", claude_dir, file_path="settings-input.json").allow


class TestClaudeHandOff:
    """The review hand-off directory, ``<SWCraft home>/reviews/``: denied to every judged tool by a literal path.

    The stated limit, pinned: it is not reached through ``~`` or ``$HOME`` in a shell word (a file tool's leading
    ``~`` is judged under both readings: :class:`TestALeadingTilde`)."""

    def test_a_read_of_a_pending_record_is_denied(self, claude_dir: Path, user_home: Path) -> None:
        pending = handoff_reviews(user_home) / "x.delta.pending.json"
        decision = decide_claude_tool("Read", claude_dir, file_path=str(pending))
        assert decision == copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)
        assert str(user_home) not in (decision.reason or "")

    def test_a_write_of_a_decision_record_is_denied(self, claude_dir: Path, user_home: Path) -> None:
        decision_record = handoff_reviews(user_home) / "x.delta.decision.json"
        assert decide_claude_tool(
            "Write", claude_dir, file_path=str(decision_record), content="{}"
        ) == copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)

    def test_a_write_into_a_reviews_directory_not_yet_made_is_denied(self, claude_dir: Path, user_home: Path) -> None:
        home = swcraft_home(user_home)
        assert not decide_claude_tool("Write", claude_dir, file_path=str(home / "reviews" / "y.decision.json")).allow

    @pytest.mark.parametrize("where", ["reviews", "home"])
    def test_a_search_rooted_at_or_above_it_is_denied(self, claude_dir: Path, user_home: Path, where: str) -> None:
        reviews = handoff_reviews(user_home)
        root = reviews if where == "reviews" else user_home
        assert decide_claude_tool("Grep", claude_dir, pattern="x", path=str(root)) == copilot_guard.Decision(
            allow=False, reason=HANDOFF_REASON
        )

    def test_a_literal_shell_word_is_denied(self, claude_dir: Path, user_home: Path) -> None:
        reviews = handoff_reviews(user_home)
        for command in (
            f"cat {shlex.quote(str(reviews / 'x.delta.pending.json'))}",
            f"printf x > {shlex.quote(str(reviews / 'x.delta.decision.json'))}",
            f"ls {shlex.quote(str(reviews))}",
        ):
            assert decide_claude_bash(claude_dir, command) == copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)

    @pytest.mark.parametrize("above", ["home", "swcraft-home"])
    def test_a_literal_word_naming_a_folder_above_it_is_denied(
        self, claude_dir: Path, user_home: Path, above: str
    ) -> None:
        """A recursive or destructive command on a folder above the hand-off directory reaches the records below it."""
        reviews = handoff_reviews(user_home)
        folder = user_home if above == "home" else reviews.parent
        for command in (
            f"grep -r x {shlex.quote(str(folder))}",
            f"rm -rf {shlex.quote(str(folder))}",
            f"cp -r notes {shlex.quote(str(folder))}/",
        ):
            assert decide_claude_bash(claude_dir, command) == copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)

    def test_the_filesystem_root_is_left_out(self, claude_dir: Path, user_home: Path) -> None:
        """The stated limit, pinned: a word naming the root is not judged as one above the hand-off directory.

        Otherwise every command holding such a word (``tr / _``) would be refused."""
        handoff_reviews(user_home)
        assert decide_claude_bash(claude_dir, "tr / _ < notes.md").allow

    def test_a_shell_session_inside_it_is_denied(self, user_home: Path) -> None:
        """A command whose base lies inside the directory reaches it whatever its words say.

        Only a project stored there can put a session there, and outside a guarded project nothing is judged."""
        inside = handoff_reviews(user_home) / "proj"
        _write_manifest(inside)
        write_claude_settings(inside)
        assert decide_claude_bash(inside, "ls") == copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)

    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.swcraft/reviews/x.delta.pending.json",
            "printf x > ~/.swcraft/reviews/x.delta.decision.json",
            "cat $HOME/.swcraft/reviews/x.delta.pending.json",
        ],
    )
    def test_a_path_through_tilde_or_home_is_not_reached(self, claude_dir: Path, user_home: Path, command: str) -> None:
        """Stated limit: ``~`` and variables are taken literally, so these pass; the client confines them."""
        handoff_reviews(user_home)
        assert decide_claude_bash(claude_dir, command).allow

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("Read", {"file_path": "notes.md"}),
            ("Grep", {"pattern": "x", "path": "notes"}),
            ("Edit", {"file_path": "notes.md", "old_string": "a", "new_string": "b"}),
            ("Bash", {"command": "ls notes"}),
            ("Monitor", {"command": "tail -f notes.md"}),
        ],
    )
    def test_a_home_that_cannot_be_determined_denies(
        self, claude_dir: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]
    ) -> None:
        """Without the home every judged call is refused with that reason; an unjudged tool is still allowed."""

        def no_home() -> Path:
            raise RuntimeError("no home")

        monkeypatch.setattr(copilot_guard, "reviews_location", no_home)
        assert decide_claude_tool(tool, claude_dir, **arguments) == copilot_guard.Decision(
            allow=False, reason=copilot_guard._handoff_unknown_reason()
        )
        assert decide_claude_tool("Agent", claude_dir, prompt="x").allow

    def test_codex_is_not_given_the_hand_off_rule(self, codex_dir: Path, user_home: Path) -> None:
        """Not added to the Codex dialect: its sandbox confines it."""
        pending = handoff_reviews(user_home) / "x.delta.pending.json"
        assert decide_codex_shell(codex_dir, f"cat {shlex.quote(str(pending))}").allow


class TestClaudeMsysDrives:
    """On Windows Claude Code's ``Bash`` is Git Bash: ``/c/x`` is ``C:/x`` there."""

    @pytest.mark.parametrize(
        ("word", "translated"),
        [
            ("/c/Users/dev/proj", "C:/Users/dev/proj"),
            ("/D/x", "D:/x"),
            ("/c", "C:/"),
            ("/c/", "C:/"),
            ("/usr/bin", None),
            ("/tmp", None),
            ("//c/x", None),
            ("c/x", None),
            ("/cd/x", None),
        ],
    )
    def test_the_translation_is_a_pure_function(self, word: str, translated: str | None) -> None:
        assert copilot_guard._msys_drive_path(word) == translated

    @pytest.mark.parametrize(
        ("part", "readings"),
        [
            ("/c/proj/model/x", ["C:/proj/model/x"]),
            ("{/c/proj/model/x.yaml,}", ["C:/proj/model/x.yaml"]),
            ("/{c,d}/proj/model/x.yaml", ["D:/proj/model/x.yaml", "C:/proj/model/x.yaml"]),
            ("{/c,x}/proj/m", ["C:/proj/m"]),
            ("/usr/bin", []),
            ("a/c/x", []),
        ],
        ids=["plain", "brace-around", "brace-drive", "brace-prefix", "mount", "relative"],
    )
    def test_every_brace_expansion_is_read_as_a_drive_path(self, part: str, readings: list[str]) -> None:
        """Bash expands the braces before Git Bash reads ``/c/``: a drive reading per expansion, never the raw text."""
        assert sorted(copilot_guard._msys_readings(part)) == sorted(readings)

    @pytest.mark.skipif(not NATIVE_DRIVES, reason="needs native drive letters (a capability probe, Rule U)")
    @pytest.mark.parametrize(
        "spelling",
        ["cat {msys}/swc/Engine.yaml", "cat {{{msys}/swc/Engine.yaml,}}", "x --out={msys}/swc/New.yaml"],
        ids=["plain", "brace", "option"],
    )
    def test_an_msys_spelling_of_the_model_is_denied_on_windows(self, claude_dir: Path, spelling: str) -> None:
        model = (claude_dir / "model").resolve().as_posix()
        msys = f"/{model[0].lower()}{model[2:]}"
        assert decide_claude_bash(claude_dir, spelling.format(msys=msys)) == copilot_guard.Decision(
            allow=False, reason=SHELL_REASON
        )

    @pytest.mark.skipif(not NATIVE_DRIVES, reason="needs native drive letters (a capability probe, Rule U)")
    def test_a_brace_drive_spelling_is_denied_on_windows(self, claude_dir: Path) -> None:
        model = (claude_dir / "model").resolve().as_posix()
        letter, rest = model[0].lower(), model[2:]
        assert not decide_claude_bash(claude_dir, f"cat /{{{letter},q}}{rest}/swc/Engine.yaml").allow


class TestClaudeRefusals:
    """What cannot be judged is denied in a guarded project."""

    @pytest.mark.parametrize("command", ["cat 'model", "cat ${x", 42])
    def test_an_unparseable_shell_text_is_denied(self, claude_dir: Path, command: object) -> None:
        decision = decide_claude_bash(claude_dir, command)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    @pytest.mark.parametrize("value", [42, ["notes/x", 3], {"x": 1}])
    def test_a_non_string_path_is_denied(self, claude_dir: Path, value: object) -> None:
        decision = decide_claude_tool("Write", claude_dir, file_path=value, content="x")
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    @pytest.mark.parametrize("written", ["//server/share/x", "C:notes/x"])
    def test_a_path_no_resolution_reads_is_denied(self, claude_dir: Path, written: str) -> None:
        decision = decide_claude_tool("Edit", claude_dir, file_path=written)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_a_write_naming_no_path_is_denied(self, claude_dir: Path) -> None:
        decision = decide_claude_tool("Write", claude_dir, content="x")
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_undecodable_arguments_are_denied(self, claude_dir: Path) -> None:
        payload = {"toolName": "Edit", "toolArgs": "not json", "cwd": str(claude_dir)}
        decision = decide_claude(payload, claude_dir)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_every_reason_is_free_of_the_projects_location(self, claude_dir: Path, user_home: Path) -> None:
        for decision in (
            decide_claude_tool("Write", claude_dir, file_path=str(claude_dir.resolve() / "model" / "x")),
            decide_claude_tool("Write", claude_dir, file_path=42),
            decide_claude_bash(claude_dir, f"cat {claude_dir.resolve() / 'model' / 'x'}"),
            decide_claude_tool("Read", claude_dir, file_path=str(handoff_reviews(user_home))),
        ):
            assert not decision.allow and decision.reason is not None
            assert str(claude_dir) not in decision.reason and str(claude_dir.resolve()) not in decision.reason
            assert str(user_home) not in decision.reason


class TestClaudeRender:
    def test_the_claude_deny_carries_only_hook_specific_output(self) -> None:
        """The documented shape alone: output that fails validation lets the action go ahead."""
        rendered = json.loads(copilot_guard.render(copilot_guard.Decision(allow=False, reason="why"), "claude"))
        assert rendered == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "why",
            }
        }

    @pytest.mark.parametrize("reason", [None, ""])
    def test_a_claude_deny_never_has_an_empty_reason(self, reason: str | None) -> None:
        rendered = json.loads(copilot_guard.render(copilot_guard.Decision(allow=False, reason=reason), "claude"))
        assert rendered["hookSpecificOutput"]["permissionDecisionReason"].startswith("SWCraft: ")

    def test_allow_is_an_empty_object(self) -> None:
        assert copilot_guard.render(copilot_guard.Decision(allow=True), "claude") == "{}"


class TestClaudeCapturedPayloads:
    """The payloads Claude Code 2.1.319 sent, placed in a guarded project."""

    @pytest.mark.parametrize(
        ("name", "allowed"),
        [
            ("Agent", True),
            ("Bash-cd-sub", True),
            ("Bash-ls-model", False),
            ("Bash-pwd", True),
            ("Edit", True),
            ("mcp-tool", True),
            ("NotebookEdit", True),
            ("Read-nb-ipynb", True),
            ("Read-README-md", True),
            ("Read-subagent", True),
            ("Read-swcraft-yaml", True),
            ("Write-denied", True),
            ("Write-notes", True),
        ],
    )
    def test_each_captured_payload(self, claude_dir: Path, name: str, allowed: bool) -> None:
        payload = captured_payload(name, claude_dir)
        assert decide_claude(payload, claude_dir).allow is allowed

    def test_every_captured_payload_is_named_here(self) -> None:
        names = {path.stem for path in CLAUDE_FIXTURES.glob("*.json")}
        marks = next(mark for mark in self.test_each_captured_payload.pytestmark if mark.name == "parametrize")
        assert names == {name for name, _ in marks.args[1]}

    @pytest.mark.parametrize("name", ["Edit", "Write-notes", "Read-README-md", "Read-subagent", "NotebookEdit"])
    def test_a_captured_payload_aimed_at_the_model_is_denied(self, claude_dir: Path, name: str) -> None:
        payload = captured_payload(name, claude_dir)
        key = "notebook_path" if "notebook_path" in payload["tool_input"] else "file_path"
        payload["tool_input"][key] = str(claude_dir / "model" / "swc" / "Engine.yaml")
        assert not decide_claude(payload, claude_dir).allow

    def test_a_subagents_call_is_judged_as_the_sessions(self, claude_dir: Path) -> None:
        """The subagent's ``Read`` reached the hook with ``agent_id``/``agent_type``; it is judged."""
        payload = captured_payload("Read-subagent", claude_dir)
        assert payload["agent_type"] == "general-purpose"
        payload["tool_input"]["file_path"] = str(claude_dir / "model" / "swc" / "Engine.yaml")
        assert decide_claude(payload, claude_dir) == copilot_guard.Decision(allow=False, reason=MODEL_READ_REASON)


class TestClaudeCommand:
    """The Claude dialect through ``swcraft guard --host claude``."""

    @pytest.mark.parametrize("main", [entry.main, cli.main], ids=["entry", "cli"])
    def test_the_command_denies_an_edit_in_claude_shape(self, claude_dir: Path, monkeypatch, capsys, main) -> None:
        stdin = json.dumps(guard_payload("Edit", claude_dir, file_path="model/swc/Engine.yaml")).encode("utf-8")
        out, _ = run_guard(stdin, monkeypatch, capsys, claude_dir, main, ("guard", "--host", "claude"))
        assert json.loads(out) == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": CLAUDE_EDIT_REASON.format(path="model/swc/Engine.yaml"),
            }
        }

    def test_the_command_allows_with_an_empty_object(self, claude_dir: Path, monkeypatch, capsys) -> None:
        stdin = json.dumps(guard_payload("Edit", claude_dir, file_path="notes.md")).encode("utf-8")
        out, _ = run_guard(stdin, monkeypatch, capsys, claude_dir, entry.main, ("guard", "--host", "claude"))
        assert out.strip() == "{}"

    @pytest.mark.parametrize(
        ("tool", "allowed"),
        [("Bash", False), ("Edit", False), ("Read", False), ("PowerShell", False), ("Agent", True)],
    )
    def test_an_unexpected_failure_denies_a_judged_tool(
        self, claude_dir: Path, monkeypatch, capsys, tool: str, allowed: bool
    ) -> None:
        def fail(*_: object) -> copilot_guard.Decision:
            raise RuntimeError("broken")

        monkeypatch.setattr(copilot_guard, "decide", fail)
        stdin = json.dumps(guard_payload(tool, claude_dir / "notes", command="ls")).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, claude_dir, entry.main, ("guard", "--host", "claude"))
        if allowed:
            assert out.strip() == "{}"
        else:
            assert json.loads(out) == {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": _CLAUDE_UNDECIDED_REASON,
                }
            }
        assert ("this call is allowed" if allowed else "this call is denied") in err
        assert "broken" not in err

    def test_the_host_is_one_of_the_hosts(self) -> None:
        assert copilot_guard.HOSTS == ("copilot", "codex", "claude", "kiro")


# The Kiro dialect.
