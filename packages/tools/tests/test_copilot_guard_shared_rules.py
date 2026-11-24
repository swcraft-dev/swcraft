"""``swcraft guard``'s rules shared across the Codex, Claude Code and Kiro dialects.

The shared guard inputs and their folders and ancestors, the model folder's ancestors and the model folder anywhere,
a nested model folder, a model entry's tail, the SWCraft home, a leading tilde, and each host through each entry
point. The helpers the dialects share are
``copilot_guard_support``'s, their project fixtures ``conftest.py``'s.

What this does not reach: whether a host honours the answer; no host runs here. Every case that makes a link goes
through ``symlink_or_skip``, so on a Windows runner without the symbolic-link privilege those cases skip and prove
nothing there.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from conftest import _write_manifest
from copilot_guard_support import (
    DEEPLY_NESTED,
    GREP_ARGUMENTS,
    GUARD_INPUT_REASON,
    HANDOFF_REASON,
    HOME_REASON,
    KIRO_WRITE_TOOLS,
    SHARED_GUARD_INPUTS,
    UNREACHABLE_JUDGE_SECONDS,
    codex_patch,
    codex_project,
    decide_claude_bash,
    decide_claude_tool,
    decide_codex_shell,
    decide_codex_tool,
    decide_kiro_bash,
    decide_kiro_sub_agent,
    decide_kiro_tool,
    decide_payload,
    from_home,
    guard_payload,
    handoff_reviews,
    kiro_deny_line,
    swcraft_home,
    write_claude_settings,
    write_codex_hook,
    write_kiro_hooks,
)
from swcraft_tools import copilot_guard, guard_model_paths, project, tilde_paths
from swcraft_tools.agents_md import HOST_PREAMBLE, KIRO_PREAMBLE
from symlink_support import symlink_or_skip

_GUARD_FOLDERS = [".codex", ".claude", ".github/hooks", ".kiro/hooks", ".kiro/settings", ".kiro/agents"]


def _guarded_both(root: Path) -> None:
    """Mark *root* as a project both the Codex and the Claude Code dialects guard."""
    if not (root / ".codex" / "hooks.json").exists():
        write_codex_hook(root)
    write_claude_settings(root)


def _judged_in(dialect: str, root: Path, command: str) -> copilot_guard.Decision:
    return decide_codex_shell(root, command) if dialect == "codex" else decide_claude_bash(root, command)


def test_this_guard_module_runs_under_the_isolation() -> None:
    """This module's tests see the unreachable judge deadline and no ``CLAUDE_PROJECT_DIR``.

    The other guard modules are isolated by the same key, their ``test_copilot_guard_`` name; this checks one."""
    assert copilot_guard._JUDGE_SECONDS == UNREACHABLE_JUDGE_SECONDS
    assert "CLAUDE_PROJECT_DIR" not in os.environ


class TestGuardFolderScan:
    """The entry cap and an unresolvable link refuse every shared folder in both dialects, with the folder reason."""

    @pytest.mark.parametrize("dialect", ["codex", "claude"])
    @pytest.mark.parametrize("folder", _GUARD_FOLDERS)
    def test_a_folder_past_the_entry_cap_denies_every_command(
        self, project_dir: Path, user_home: Path, folder: str, dialect: str
    ) -> None:
        _guarded_both(project_dir)
        (project_dir / folder).mkdir(parents=True, exist_ok=True)
        for index in range(copilot_guard._MAX_GUARD_FOLDER_ENTRIES + 1):
            (project_dir / folder / f"f{index}").touch()
        assert _judged_in(dialect, project_dir, "ls notes.md") == copilot_guard.Decision(
            allow=False, reason=copilot_guard._guard_folder_reason()
        )
        if dialect == "claude":
            assert decide_claude_tool("Edit", project_dir, file_path="notes.md") == copilot_guard.Decision(
                allow=False, reason=copilot_guard._guard_folder_reason()
            )

    @pytest.mark.parametrize("dialect", ["codex", "claude"])
    @pytest.mark.parametrize("folder", _GUARD_FOLDERS)
    def test_a_link_that_cannot_be_resolved_denies_every_command(
        self, project_dir: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch, folder: str, dialect: str
    ) -> None:
        """A link there whose target cannot be resolved leaves what the hook depends on unknown.

        Here the resolution raises, as a link loop does on some hosts."""
        _guarded_both(project_dir)
        (project_dir / folder).mkdir(parents=True, exist_ok=True)
        symlink_or_skip(project_dir / folder / "linked.json", project_dir / "notes.md")
        real = copilot_guard.resolved_path

        def refuse_the_link(path: Any) -> str:
            if Path(path).name == "linked.json":
                raise OSError("unresolvable")
            return real(path)

        monkeypatch.setattr(copilot_guard, "resolved_path", refuse_the_link)
        assert _judged_in(dialect, project_dir, "ls notes.md") == copilot_guard.Decision(
            allow=False, reason=copilot_guard._guard_folder_reason()
        )


class TestCrossClientGuardInputs:
    """One client never disarms the other's guard: the Codex dialect's guard-input set widened too."""

    @pytest.mark.parametrize(
        "name", [".claude/settings.local.json", ".claude/settings.json", ".mcp.json", ".github/hooks/swcraft.json"]
    )
    def test_codex_writing_another_clients_guard_file_is_denied(self, codex_dir: Path, name: str) -> None:
        assert decide_codex_tool(
            "apply_patch", codex_dir, command=codex_patch(f"*** Add File: {name}")
        ) == copilot_guard.Decision(allow=False, reason=GUARD_INPUT_REASON.format(kind="patch"))
        assert decide_codex_shell(codex_dir, f"echo x > {name}") == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )
        assert decide_codex_shell(codex_dir, f"cat {name}").allow

    @pytest.mark.parametrize("name", [".codex/hooks.json", ".mcp.json", ".github/hooks/swcraft.json"])
    def test_claude_writing_another_clients_guard_file_is_denied(self, claude_dir: Path, name: str) -> None:
        assert not decide_claude_tool("Write", claude_dir, file_path=name, content="{}").allow
        assert not decide_claude_bash(claude_dir, f"cp notes.md {name}").allow


_KIRO_INPUT_FILES = [".kiro/hooks/x.json", ".kiro/settings/mcp.json", ".kiro/agents/a.json"]


_KIRO_FREE_FILES = [".kiro/steering/x.md", ".kiro/specs/f/requirements.md", ".github/workflows/ci.yml"]


def _guarded_all(root: Path) -> None:
    """Mark *root* as a project the Codex, the Claude Code and the Kiro dialects all guard."""
    _guarded_both(root)
    if not (root / copilot_guard.KIRO_HOOKS_FOLDER).exists():
        write_kiro_hooks(root)


def _written_in(dialect: str, root: Path, name: str) -> copilot_guard.Decision:
    """A write of *name* by the dialect's write tool: Codex's patch, Claude Code's ``Write``, Kiro's ``fs_write``."""
    if dialect == "codex":
        return decide_codex_tool("apply_patch", root, command=codex_patch(f"*** Add File: {name}"))
    if dialect == "claude":
        return decide_claude_tool("Write", root, file_path=name, content="x")
    return decide_kiro_tool("fs_write", root, path=name, text="x")


def _shell_in(dialect: str, root: Path, command: str) -> copilot_guard.Decision:
    if dialect == "kiro":
        return decide_kiro_bash(root, command)
    return _judged_in(dialect, root, command)


class TestKiroFoldersAreGuardInputs:
    """``.kiro/{hooks,settings,agents}`` join the shared set; ``.kiro/steering`` and ``.kiro/specs`` do not."""

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    @pytest.mark.parametrize("name", _KIRO_INPUT_FILES)
    def test_writing_a_kiro_input_is_denied(self, project_dir: Path, user_home: Path, dialect: str, name: str) -> None:
        _guarded_all(project_dir)
        kind = "patch" if dialect == "codex" else "edit"
        assert _written_in(dialect, project_dir, name) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind=kind)
        )
        assert _shell_in(dialect, project_dir, f"echo x > {name}") == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    @pytest.mark.parametrize("name", _KIRO_FREE_FILES)
    def test_writing_steering_specs_or_a_workflow_is_allowed(
        self, project_dir: Path, user_home: Path, dialect: str, name: str
    ) -> None:
        _guarded_all(project_dir)
        assert _written_in(dialect, project_dir, name).allow
        assert _shell_in(dialect, project_dir, f"echo x > {name}").allow

    @pytest.mark.parametrize(("tool", "key"), KIRO_WRITE_TOOLS)
    @pytest.mark.parametrize(
        "name", [".codex/hooks.json", ".claude/settings.local.json", ".mcp.json", ".github/hooks/x.json"]
    )
    def test_kiro_writing_another_clients_guard_file_is_denied(
        self, kiro_dir: Path, tool: str, key: str, name: str
    ) -> None:
        assert decide_kiro_tool(tool, kiro_dir, **{key: name}) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="edit")
        )

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    def test_reading_a_kiro_input_is_allowed(self, project_dir: Path, user_home: Path, dialect: str) -> None:
        _guarded_all(project_dir)
        assert _shell_in(dialect, project_dir, "cat .kiro/settings/mcp.json").allow
        assert _shell_in(dialect, project_dir, "ls .kiro/hooks").allow
        if dialect == "kiro":
            assert decide_kiro_tool("read_file", project_dir, path=".kiro/hooks/swcraft-guard.json").allow
        if dialect == "claude":
            assert decide_claude_tool("Read", project_dir, file_path=".kiro/hooks/swcraft-guard.json").allow

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    def test_the_target_of_a_link_directly_in_kiro_hooks_counts(
        self, project_dir: Path, user_home: Path, dialect: str
    ) -> None:
        """A link directly inside ``.kiro/hooks`` pointing out of it makes its target a guard input.

        That holds as for every input folder; the target stays readable."""
        _guarded_all(project_dir)
        (project_dir / "hook-input.json").write_text("{}\n", encoding="utf-8", newline="\n")
        symlink_or_skip(project_dir / ".kiro" / "hooks" / "linked.json", Path("..") / ".." / "hook-input.json")
        assert not _written_in(dialect, project_dir, "hook-input.json").allow
        assert _shell_in(dialect, project_dir, "rm hook-input.json") == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )
        assert _shell_in(dialect, project_dir, "cat hook-input.json").allow


class TestGuardInputAncestors:
    """A folder strictly between the project directory and a guard input (``.github``, ``.kiro``) is a guard input's.

    So it is to a write path or a non-reader shell word naming it -- resolved or as named, ``..`` collapsed -- in the
    Codex, Claude Code and Kiro dialects, and readable. The project directory itself is not one, the stated limit.
    """

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf .github",
            "rm -rf .kiro",
            "mv .kiro x",
            "mv .github x",
            "mv .codex x",
            "rm -rf .kiro/",
            "rm -rf notes/../.kiro",
            "rm -rf .kiro/hooks/..",
            "rm -rf .k*",
            "rm -rf .gith?b",
            "rm -rf .{kiro,x}",
        ],
    )
    def test_a_command_removing_a_folder_holding_an_input_is_denied(
        self, project_dir: Path, user_home: Path, dialect: str, command: str
    ) -> None:
        _guarded_all(project_dir)
        (project_dir / "notes").mkdir(exist_ok=True)
        (project_dir / ".github" / "workflows").mkdir(parents=True)
        assert _shell_in(dialect, project_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    def test_a_link_naming_the_folder_is_judged_by_its_target(
        self, project_dir: Path, user_home: Path, dialect: str
    ) -> None:
        _guarded_all(project_dir)
        symlink_or_skip(project_dir / "alias", Path(".kiro"))
        assert not _shell_in(dialect, project_dir, "rm -rf alias/").allow

    @pytest.mark.parametrize("name", [".github", ".kiro", "notes/../.kiro", ".kiro/settings/.."])
    @pytest.mark.parametrize("tool", ["Write", "Edit"])
    def test_a_claude_write_naming_the_folder_is_denied(self, claude_dir: Path, tool: str, name: str) -> None:
        assert decide_claude_tool(tool, claude_dir, file_path=name, content="x") == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="edit")
        )

    @pytest.mark.parametrize("name", [".github", ".kiro"])
    def test_a_codex_patch_naming_the_folder_is_denied(self, codex_dir: Path, name: str) -> None:
        assert decide_codex_tool(
            "apply_patch", codex_dir, command=codex_patch(f"*** Delete File: {name}")
        ) == copilot_guard.Decision(allow=False, reason=GUARD_INPUT_REASON.format(kind="patch"))

    @pytest.mark.parametrize(("tool", "key"), KIRO_WRITE_TOOLS)
    @pytest.mark.parametrize("name", [".kiro", ".github"])
    def test_a_kiro_write_naming_the_folder_is_denied(self, kiro_dir: Path, tool: str, key: str, name: str) -> None:
        assert decide_kiro_tool(tool, kiro_dir, **{key: name}) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="edit")
        )

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    @pytest.mark.parametrize(
        "command",
        ["ls .github", "ls .kiro", "cat .codex/hooks.json", "cat .kiro/settings/mcp.json", "ls -la .kiro .github"],
    )
    def test_a_reader_of_the_folder_is_allowed(
        self, project_dir: Path, user_home: Path, dialect: str, command: str
    ) -> None:
        _guarded_all(project_dir)
        assert _shell_in(dialect, project_dir, command).allow

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    @pytest.mark.parametrize(
        "command", ["rm -rf .github/workflows", "mkdir -p .kiro/steering", "rm -rf .kiro/specs/f", "rm -rf .gi*/w*"]
    )
    def test_a_path_inside_the_folder_and_beside_the_inputs_is_allowed(
        self, project_dir: Path, user_home: Path, dialect: str, command: str
    ) -> None:
        """The folder alone counts, never a path inside it that is no input: the rule is no new containment."""
        _guarded_all(project_dir)
        (project_dir / ".github" / "workflows").mkdir(parents=True)
        assert _shell_in(dialect, project_dir, command).allow

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    def test_the_project_directory_itself_is_no_ancestor(
        self, project_dir: Path, user_home: Path, dialect: str
    ) -> None:
        """The stated limit, pinned: ``rm -rf .`` is allowed.

        It names neither an input nor a folder strictly between the project and one, and the model rule does not deny
        it either (the model directory lies below the word, which names only itself)."""
        _guarded_all(project_dir)
        assert _shell_in(dialect, project_dir, "rm -rf .").allow
        assert copilot_guard._guard_input_ancestors(
            copilot_guard._guard_inputs(project_dir / "swcraft.yaml") or (), str(project_dir.resolve())
        ).isdisjoint({str(project_dir.resolve()).casefold()})

    def test_the_folders_are_derived_from_the_inputs(self, project_dir: Path) -> None:
        root = str(project_dir.resolve())
        ancestors = copilot_guard._guard_input_ancestors(
            copilot_guard._guard_inputs(project_dir / "swcraft.yaml") or (), root
        )
        assert ancestors == {os.path.join(root, ".github").casefold(), os.path.join(root, ".kiro").casefold()}

    @pytest.mark.parametrize("dialect", ["codex", "claude"])
    @pytest.mark.parametrize(
        "command",
        ["git add .github", "git diff .kiro", "find .github -name x", "cd .github && ls", "chmod -R 755 .kiro"],
    )
    def test_the_stated_over_reach_of_a_non_reader_naming_the_folder(
        self, project_dir: Path, user_home: Path, dialect: str, command: str
    ) -> None:
        """Pinned over-reach: a non-reader naming ``.github`` or ``.kiro`` is denied though it writes nothing."""
        _guarded_all(project_dir)
        (project_dir / ".github" / "workflows").mkdir(parents=True)
        assert _shell_in(dialect, project_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )


#: The denial of a patch, edit or command naming a folder that holds the model.
_MODEL_FOLDER_REASON = (
    "SWCraft: in this project the model directory (`arch/model`) is changed through the SWCraft tools, whose changes "
    "the panel reviews; this {kind} names a folder that holds it, and removing or moving that folder removes or "
    "moves the model, so it is refused. Use the SWCraft MCP tool `change_set` for a model change."
)


def _nested_model_project(root: Path) -> Path:
    """A project at *root* whose model root is ``arch/model``, guarded by the Codex, Claude Code and Kiro dialects.

    It has ``arch/other`` beside the model and a ``notes`` folder."""
    codex_project(root, "arch/model")
    _guarded_all(root)
    (root / "arch" / "model" / "swc").mkdir(parents=True)
    (root / "arch" / "model" / "swc" / "Engine.yaml").write_text("name: Engine\n", encoding="utf-8", newline="\n")
    (root / "arch" / "other").mkdir()
    (root / "notes").mkdir()
    return root


class TestModelAncestors:
    """A folder strictly between the project directory and the model root (``arch`` for ``arch/model``) is the model's.

    So it is to a write path, a patch path, a Kiro ``delete_file`` and a non-reader shell word -- resolved or as named
    -- in the Codex, Claude Code and Kiro dialects; read freely. Kiro's ``delete_file`` is refused on the project
    directory and every folder holding it too. A shell word naming the project directory stays the stated limit.
    """

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf arch",
            "mv arch x",
            "rm -rf arch/",
            "rm -rf ./arch",
            "rm -rf notes/../arch",
            "rm -rf arch/other/..",
            "rm -rf ar*",
            "rm -rf {arch,x}",
            "mv ARCH x",
        ],
    )
    def test_a_command_removing_a_folder_holding_the_model_is_denied(
        self, tmp_path: Path, user_home: Path, dialect: str, command: str
    ) -> None:
        root = _nested_model_project(tmp_path / "proj")
        assert _shell_in(dialect, root, command) == copilot_guard.Decision(
            allow=False, reason=_MODEL_FOLDER_REASON.format(kind="command")
        )

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    def test_the_absolute_folder_is_denied(self, tmp_path: Path, user_home: Path, dialect: str) -> None:
        root = _nested_model_project(tmp_path / "proj")
        assert not _shell_in(dialect, root, f"rm -rf {shlex.quote(str(root / 'arch'))}").allow

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    @pytest.mark.parametrize("command", ["ls arch", "ls -la arch", "cat arch/other/x", "rm -rf arch/other"])
    def test_a_reader_or_a_path_beside_the_model_is_allowed(
        self, tmp_path: Path, user_home: Path, dialect: str, command: str
    ) -> None:
        root = _nested_model_project(tmp_path / "proj")
        assert _shell_in(dialect, root, command).allow

    @pytest.mark.parametrize("dialect", ["codex", "claude", "kiro"])
    def test_the_project_directory_stays_the_stated_limit(self, tmp_path: Path, user_home: Path, dialect: str) -> None:
        """Pinned: a shell word naming the project directory is not judged as a folder holding the model.

        Denying every non-reader naming ``.`` would deny ``git add .``, so ``rm -rf .`` is allowed."""
        root = _nested_model_project(tmp_path / "proj")
        assert _shell_in(dialect, root, "rm -rf .").allow
        assert _shell_in(dialect, root, f"rm -rf {shlex.quote(str(root))}").allow

    @pytest.mark.parametrize("name", ["arch", "arch/", "notes/../arch", "ARCH"])
    def test_a_codex_patch_naming_the_folder_is_denied(self, tmp_path: Path, name: str) -> None:
        root = _nested_model_project(tmp_path / "proj")
        assert decide_codex_tool(
            "apply_patch", root, command=codex_patch(f"*** Delete File: {name}")
        ) == copilot_guard.Decision(allow=False, reason=_MODEL_FOLDER_REASON.format(kind="patch"))

    @pytest.mark.parametrize("tool", ["Write", "Edit"])
    @pytest.mark.parametrize("name", ["arch", "notes/../arch"])
    def test_a_claude_write_naming_the_folder_is_denied(
        self, tmp_path: Path, user_home: Path, tool: str, name: str
    ) -> None:
        root = _nested_model_project(tmp_path / "proj")
        assert decide_claude_tool(tool, root, file_path=name, content="x") == copilot_guard.Decision(
            allow=False, reason=_MODEL_FOLDER_REASON.format(kind="edit")
        )

    @pytest.mark.parametrize(("tool", "key"), KIRO_WRITE_TOOLS)
    @pytest.mark.parametrize("name", ["arch", "arch/", "notes/../arch"])
    def test_a_kiro_write_naming_the_folder_is_denied(
        self, tmp_path: Path, user_home: Path, tool: str, key: str, name: str
    ) -> None:
        root = _nested_model_project(tmp_path / "proj")
        assert decide_kiro_tool(tool, root, **{key: name}) == copilot_guard.Decision(
            allow=False, reason=_MODEL_FOLDER_REASON.format(kind="edit")
        )

    def test_a_kiro_delete_of_the_project_or_above_is_denied(self, tmp_path: Path, user_home: Path) -> None:
        root = _nested_model_project(tmp_path / "proj")
        denied = copilot_guard.Decision(allow=False, reason=_MODEL_FOLDER_REASON.format(kind="edit"))
        for name in (".", str(root), "..", str(tmp_path), root.anchor):
            assert decide_kiro_tool("delete_file", root, targetFile=name) == denied, name
        assert decide_kiro_tool("delete_file", root / "notes", targetFile="..") == denied
        assert decide_kiro_tool("delete_file", root, targetFile="arch/other").allow
        assert decide_kiro_tool("delete_file", root, targetFile="notes").allow

    def test_the_folders_are_derived_from_the_model_root(self, tmp_path: Path) -> None:
        root = _nested_model_project(tmp_path / "proj")
        loaded = copilot_guard.read_project(root).project
        assert loaded is not None
        resolved = str(root.resolve())
        assert copilot_guard._model_ancestors(loaded) == {os.path.join(resolved, "arch").casefold()}

    def test_a_model_root_directly_in_the_project_has_none(self, project_dir: Path) -> None:
        loaded = copilot_guard.read_project(project_dir).project
        assert loaded is not None and copilot_guard._model_ancestors(loaded) == frozenset()


class TestTheGuardInputSetIsDeclaredOnce:
    """The set is the module's ``SHARED_GUARD_INPUTS``, and every place that names it agrees with it.

    The folder scan, the two reasons that name it, this file's list of files and the preamble ``AGENTS.md`` gives
    Codex and Claude Code all agree with it."""

    def test_the_folders_scanned_are_the_sets_folders(self) -> None:
        assert set(copilot_guard._GUARD_INPUT_FOLDERS) <= set(copilot_guard._SHARED_GUARD_INPUTS)
        assert set(copilot_guard._SHARED_GUARD_INPUTS) == {
            *copilot_guard._GUARD_INPUT_FOLDERS,
            *copilot_guard._SHARED_GUARD_INPUT_FILES,
        }

    def test_both_reasons_name_every_member(self) -> None:
        folder_reason = copilot_guard._guard_folder_reason()
        for member in copilot_guard._SHARED_GUARD_INPUTS:
            assert member.as_posix() in copilot_guard._GUARD_INPUTS_NAMED, member
        for folder in copilot_guard._GUARD_INPUT_FOLDERS:
            assert folder.as_posix() in folder_reason, folder
        assert "swcraft.yaml" in copilot_guard._GUARD_INPUTS_NAMED

    def test_this_files_list_covers_the_set_and_nothing_else(self) -> None:
        members = {member.as_posix() for member in copilot_guard._SHARED_GUARD_INPUTS} | {"swcraft.yaml"}
        covered = {
            member
            for name in SHARED_GUARD_INPUTS
            for member in members
            if name == member or name.startswith(f"{member}/")
        }
        assert covered == members
        assert all(any(name == m or name.startswith(f"{m}/") for m in members) for name in SHARED_GUARD_INPUTS)

    @pytest.mark.parametrize("preamble", [HOST_PREAMBLE, KIRO_PREAMBLE], ids=["agents-md", "kiro-steering"])
    def test_the_agents_md_preamble_names_every_member(self, preamble: str) -> None:
        """Both preambles, Codex's and Claude Code's, and Kiro's steering file's, name the whole set."""
        for folder in copilot_guard._GUARD_INPUT_FOLDERS:
            assert f"`{folder.as_posix()}/`" in preamble, folder
        for file in copilot_guard._SHARED_GUARD_INPUT_FILES:
            assert f"`{file.as_posix()}`" in preamble, file
        assert "`swcraft.yaml`" in preamble
        # The SWCraft home, named on one line.
        assert "the SWCraft installation folder (`~/.swcraft`)" in preamble.replace("\n", " ")


def _swcraft_home(user_home: Path) -> Path:
    """The SWCraft home under :func:`user_home`, with a toolchain in it as the installer leaves one."""
    home = swcraft_home(user_home)
    (home / "current" / "bin").mkdir(parents=True)
    (home / "current" / "bin" / "swcraft").write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    (home / "current" / "RUNTIME.json").write_text("{}\n", encoding="utf-8", newline="\n")
    return home


def _home_bash(request: pytest.FixtureRequest, dialect: str, command: str) -> copilot_guard.Decision:
    """*command* judged by the Claude Code ``Bash`` tool or Kiro's ``execute_bash``, in that dialect's project."""
    directory = request.getfixturevalue(f"{dialect}_dir")
    return decide_claude_bash(directory, command) if dialect == "claude" else decide_kiro_bash(directory, command)


class TestSwcraftHomeProtected:
    """No Claude Code or Kiro tool writes, removes or moves the SWCraft home, or removes a non-root folder above it.

    The home holds the hook's ``swcraft``, read freely outside the hand-off directory; Codex's sandbox confines writes.
    """

    def test_a_claude_write_into_it_is_denied(self, claude_dir: Path, user_home: Path) -> None:
        program = _swcraft_home(user_home) / "current" / "bin" / "swcraft"
        denied = copilot_guard.Decision(allow=False, reason=HOME_REASON)
        assert decide_claude_tool("Write", claude_dir, file_path=str(program), content="x") == denied
        assert decide_claude_tool("Edit", claude_dir, file_path=str(program), old_string="a", new_string="b") == denied
        assert str(user_home) not in HOME_REASON

    @pytest.mark.parametrize(("tool", "key"), KIRO_WRITE_TOOLS)
    def test_a_kiro_write_into_it_is_denied(self, kiro_dir: Path, user_home: Path, tool: str, key: str) -> None:
        program = _swcraft_home(user_home) / "current" / "bin" / "swcraft"
        assert decide_kiro_tool(tool, kiro_dir, **{key: str(program)}) == copilot_guard.Decision(
            allow=False, reason=HOME_REASON
        )

    def test_a_kiro_delete_of_it_or_its_toolchain_is_denied(self, kiro_dir: Path, user_home: Path) -> None:
        """The toolchain's ``current`` holds no review record, so only this rule refuses it.

        The home itself holds the hand-off directory too, whose reason it keeps."""
        home = _swcraft_home(user_home)
        assert decide_kiro_tool("delete_file", kiro_dir, targetFile=str(home / "current")) == copilot_guard.Decision(
            allow=False, reason=HOME_REASON
        )
        assert not decide_kiro_tool("delete_file", kiro_dir, targetFile=str(home)).allow

    @pytest.mark.parametrize("dialect", ["claude", "kiro"])
    @pytest.mark.parametrize(
        "command",
        ["rm -rf {home}/current", "cp notes.md {home}/current/bin/swcraft", "printf x > {home}/current/bin/swcraft"],
    )
    def test_a_command_changing_it_is_denied(
        self, request: pytest.FixtureRequest, user_home: Path, dialect: str, command: str
    ) -> None:
        text = command.format(home=shlex.quote(str(_swcraft_home(user_home))))
        assert _home_bash(request, dialect, text) == copilot_guard.Decision(allow=False, reason=HOME_REASON)

    @pytest.mark.parametrize("dialect", ["claude", "kiro"])
    @pytest.mark.parametrize("command", ["mv {home} x", "rm -rf {above}"])
    def test_a_command_moving_it_or_a_folder_above_is_denied(
        self, request: pytest.FixtureRequest, user_home: Path, dialect: str, command: str
    ) -> None:
        """A folder holding the home holds the hand-off directory too (whose reason it keeps), made or not."""
        home = _swcraft_home(user_home)
        text = command.format(home=shlex.quote(str(home)), above=shlex.quote(str(user_home)))
        assert not _home_bash(request, dialect, text).allow

    @pytest.mark.parametrize("dialect", ["claude", "kiro"])
    @pytest.mark.parametrize(
        "command", ["cat {home}/current/RUNTIME.json", "ls {home}/current", "head -c 4 {home}/current/bin/swcraft"]
    )
    def test_a_reader_of_it_is_allowed(
        self, request: pytest.FixtureRequest, user_home: Path, dialect: str, command: str
    ) -> None:
        """Reads below the home, outside the hand-off directory, stay free (integrity, not confidentiality).

        Naming the home or a folder above it reaches the review records (``ls -R``): the hand-off rule refuses that.
        """
        home = _swcraft_home(user_home)
        text = command.format(home=shlex.quote(str(home)))
        assert _home_bash(request, dialect, text).allow
        runtime = str(home / "current" / "RUNTIME.json")
        directory = request.getfixturevalue(f"{dialect}_dir")
        if dialect == "claude":
            assert decide_claude_tool("Read", directory, file_path=runtime).allow
        else:
            assert decide_kiro_tool("read_file", directory, path=runtime).allow

    def test_the_hand_off_directory_stays_unread(self, claude_dir: Path, user_home: Path) -> None:
        _swcraft_home(user_home)
        pending = handoff_reviews(user_home) / "x.delta.pending.json"
        handoff = copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)
        assert decide_claude_tool("Read", claude_dir, file_path=str(pending)) == handoff
        assert decide_claude_bash(claude_dir, f"cat {shlex.quote(str(pending))}") == handoff

    @pytest.mark.parametrize("dialect", ["claude", "kiro"])
    def test_the_filesystem_root_is_left_out(
        self, request: pytest.FixtureRequest, user_home: Path, dialect: str
    ) -> None:
        """A word naming the root (``tr / _``) is not one above the home, or every such command would be refused."""
        _swcraft_home(user_home)
        assert _home_bash(request, dialect, "tr / _ < notes.md").allow

    def test_codex_is_not_given_the_rule(self, codex_dir: Path, user_home: Path) -> None:
        program = _swcraft_home(user_home) / "current" / "bin" / "swcraft"
        assert decide_codex_tool(
            "apply_patch", codex_dir, command=codex_patch(f"*** Update File: {program.as_posix()}")
        ).allow
        assert decide_codex_shell(codex_dir, f"rm -rf {shlex.quote(str(program.parent))}").allow


_TILDE_REASON = tilde_paths.tilde_reason()


class TestALeadingTilde:
    """Kiro paths opening with ``~`` are judged expanded to :meth:`Path.home` and literal, a denial from either winning.

    Kiro's tool service expands ``~``, ``~/`` and ``~\\`` to the home (kiro-cli 3.1.0's ``resolveRelativePath``): this
    holds for the Kiro dialect's paths, globs, context files and ``execute_bash``'s ``cwd``; ``~name`` is unjudgeable.
    The Claude Code file tools take the same rule, fail closed. Shell words are unchanged (the stated limit).
    """

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("fs_write", lambda reviews, home: {"path": f"{reviews}/a.decision.json", "text": "{}"}),
            ("read_file", lambda reviews, home: {"path": f"{reviews}/a.pending.json"}),
            ("grep_search", lambda reviews, home: {**GREP_ARGUMENTS, "includePattern": f"{reviews}/**"}),
            ("list_directory", lambda reviews, home: {"path": reviews}),
            ("delete_file", lambda reviews, home: {"targetFile": home}),
            ("delete_file", lambda reviews, home: {"targetFile": "~"}),
            ("delete_file", lambda reviews, home: {"targetFile": "~/"}),
            (
                "invoke_sub_agent",
                lambda reviews, home: {
                    "name": "x",
                    "prompt": "x",
                    "contextFiles": [{"path": f"{reviews}/a.pending.json"}],
                },
            ),
        ],
    )
    def test_kiro_reaching_the_hand_off_directory_is_denied(
        self, kiro_dir: Path, user_home: Path, tool: str, arguments: Callable[[str, str], dict[str, Any]]
    ) -> None:
        handoff_reviews(user_home)
        filled = arguments(from_home(user_home, "reviews"), from_home(user_home))
        assert decide_kiro_tool(tool, kiro_dir, **filled) == copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("fs_write", {"path": "~/../proj/model/x.yaml", "text": "x"}),
            ("str_replace", {"path": "~/../proj/model/swc/Engine.yaml", "oldStr": "a", "newStr": "b"}),
            ("read_file", {"path": "~/../proj/model/swc/Engine.yaml"}),
            ("code", {"operation": "search_symbols", "path": "~/../proj/model"}),
            ("grep_search", {**GREP_ARGUMENTS, "includePattern": "~/../proj/model/**"}),
        ],
    )
    def test_kiro_reaching_the_model_is_denied(
        self, kiro_dir: Path, user_home: Path, tool: str, arguments: dict[str, Any]
    ) -> None:
        decision = decide_kiro_tool(tool, kiro_dir, **arguments)
        assert not decision.allow and "`model`" in (decision.reason or "")

    def test_kiro_shell_working_directory_in_the_model_is_denied(self, kiro_dir: Path, user_home: Path) -> None:
        decision = decide_kiro_bash(kiro_dir, "ls", "~/../proj/model")
        assert not decision.allow and "`model`" in (decision.reason or "")

    def test_a_backslash_after_the_tilde_is_expanded_on_every_os(self, kiro_dir: Path, user_home: Path) -> None:
        handoff_reviews(user_home)
        written = from_home(user_home, "reviews", "a.decision.json").replace("/", "\\")
        assert written.startswith("~\\")
        assert decide_kiro_tool("fs_write", kiro_dir, path=written, text="x") == copilot_guard.Decision(
            allow=False, reason=HANDOFF_REASON
        )

    @pytest.mark.parametrize("written", ["~other/x", "~other", "~+/x"])
    def test_another_users_home_is_denied(self, kiro_dir: Path, user_home: Path, written: str) -> None:
        denied = copilot_guard.Decision(allow=False, reason=_TILDE_REASON)
        assert decide_kiro_tool("fs_write", kiro_dir, path=written, text="x") == denied
        assert decide_kiro_tool("read_file", kiro_dir, path=written) == denied

    def test_a_harmless_path_in_the_home_is_allowed(self, kiro_dir: Path, user_home: Path) -> None:
        handoff_reviews(user_home)
        assert decide_kiro_tool("fs_write", kiro_dir, path="~/notes.txt", text="x").allow
        assert decide_kiro_tool("read_file", kiro_dir, path="~/notes.txt").allow
        assert decide_kiro_bash(kiro_dir, "ls", "~").allow

    def test_claude_reaching_the_hand_off_directory_is_denied(self, claude_dir: Path, user_home: Path) -> None:
        handoff_reviews(user_home)
        handoff = copilot_guard.Decision(allow=False, reason=HANDOFF_REASON)
        assert decide_claude_tool(
            "Read", claude_dir, file_path=from_home(user_home, "reviews", "x.delta.pending.json")
        ) == (handoff)
        assert decide_claude_tool("Grep", claude_dir, pattern="x", path=from_home(user_home, "reviews")) == handoff
        assert (
            decide_claude_tool("Glob", claude_dir, path="notes", pattern=from_home(user_home, "reviews", "**"))
            == handoff
        )
        assert decide_claude_tool(
            "Grep", claude_dir, pattern="x", path="notes", glob=from_home(user_home, "reviews", "**")
        ) == (handoff)
        written = from_home(user_home, "reviews", "x.delta.decision.json")
        assert decide_claude_tool("Write", claude_dir, file_path=written, content="{}") == handoff

    def test_claude_writing_the_model_is_denied(self, claude_dir: Path, user_home: Path) -> None:
        decision = decide_claude_tool("Write", claude_dir, file_path="~/../proj/model/x.yaml", content="x")
        assert not decision.allow and "`model`" in (decision.reason or "")
        assert not decide_claude_tool("Read", claude_dir, file_path="~/../proj/model/swc/Engine.yaml").allow

    def test_claude_naming_another_users_home_is_denied(self, claude_dir: Path, user_home: Path) -> None:
        denied = copilot_guard.Decision(allow=False, reason=_TILDE_REASON)
        assert decide_claude_tool("Read", claude_dir, file_path="~other/x") == denied
        assert decide_claude_tool("Write", claude_dir, file_path="~other/x", content="x") == denied

    def test_claude_harmless_paths_in_the_home_are_allowed(self, claude_dir: Path, user_home: Path) -> None:
        handoff_reviews(user_home)
        assert decide_claude_tool("Read", claude_dir, file_path="~/notes.txt").allow
        assert decide_claude_tool("Write", claude_dir, file_path="~/notes.txt", content="x").allow

    def test_a_home_that_cannot_be_determined_is_unjudgeable(
        self, kiro_dir: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """:func:`tilde_paths.tilde_forms` asked alone: ``Path.home`` failing makes a ``~`` path unjudgeable."""

        def fail() -> Path:
            raise RuntimeError("no home")

        monkeypatch.setattr(tilde_paths.Path, "home", staticmethod(fail))
        assert tilde_paths.tilde_forms("~/x") is None
        assert tilde_paths.tilde_forms("notes/~x") == ("notes/~x",)

    def test_a_harmless_kiro_glob_in_the_home_is_allowed(self, kiro_dir: Path, user_home: Path) -> None:
        """The expanded reading spells the home in POSIX form, so a ``~`` glob reaches the folder rules on every OS."""
        handoff_reviews(user_home)
        assert decide_kiro_tool("grep_search", kiro_dir, **{**GREP_ARGUMENTS, "includePattern": "~/notes/**"}).allow

    def test_a_harmless_claude_glob_in_the_home_is_allowed(self, claude_dir: Path, user_home: Path) -> None:
        handoff_reviews(user_home)
        assert decide_claude_tool("Glob", claude_dir, path="notes", pattern="~/notes/**").allow
        assert decide_claude_tool("Grep", claude_dir, pattern="x", path="notes", glob="~/notes/**").allow

    def test_a_windows_home_is_read_in_posix_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tilde_paths.Path, "home", staticmethod(lambda: PureWindowsPath("C:/Users/u")))
        assert tilde_paths.tilde_forms("~/notes/**") == ("~/notes/**", "C:/Users/u/notes/**")
        assert (tilde_paths.tilde_forms("~") or ())[1] == "C:/Users/u"
        assert (tilde_paths.tilde_forms("~\\a\\b") or ())[2] == "C:/Users/u/a/b"

    @pytest.mark.parametrize("home", [PureWindowsPath("C:/Users/A{B,C}"), PurePosixPath("/home/a(b)")])
    def test_a_home_holding_a_glob_character_is_unjudgeable(
        self, monkeypatch: pytest.MonkeyPatch, home: PurePath
    ) -> None:
        monkeypatch.setattr(tilde_paths.Path, "home", staticmethod(lambda: home))
        assert tilde_paths.tilde_forms("~/notes/**") is None
        assert tilde_paths.tilde_forms("~/notes.txt") is None

    @staticmethod
    def _glob_character_home(user_home: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        """A home named ``a{b,c}`` holding a hand-off directory, and a ``~`` glob into that directory."""
        home = user_home / "a{b,c}"
        home.mkdir()
        for variable in ("HOME", "LOCALAPPDATA", "USERPROFILE"):
            monkeypatch.setenv(variable, str(home))
        handoff_reviews(home)
        return from_home(home, "reviews", "**")

    def test_a_kiro_glob_under_a_glob_character_home_is_denied(
        self, kiro_dir: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The home is not brace-expanded into ``ab`` and ``ac``, which would miss its ``reviews``."""
        pattern = self._glob_character_home(user_home, monkeypatch)
        assert decide_kiro_tool(
            "grep_search", kiro_dir, **{**GREP_ARGUMENTS, "includePattern": pattern}
        ) == copilot_guard.Decision(allow=False, reason=_TILDE_REASON)

    def test_a_claude_glob_under_a_glob_character_home_is_denied(
        self, claude_dir: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pattern = self._glob_character_home(user_home, monkeypatch)
        assert decide_claude_tool("Glob", claude_dir, path="notes", pattern=pattern) == copilot_guard.Decision(
            allow=False, reason=_TILDE_REASON
        )

    def test_the_copilot_dialect_is_unchanged(self, project_dir: Path, user_home: Path) -> None:
        """Copilot's reads still resolve ``~`` literally (its stated limit), so no folder rule refuses this path.

        Only the rule on the model folder's path anywhere does."""
        decision = decide_payload(
            guard_payload("Read", project_dir, file_path="~/../proj/model/swc/Engine.yaml"), project_dir
        )
        assert decision == _held(project_dir)


#: Per host, a call the guard denies and one it allows in a project every client's hook marks.
_SMOKE_CALLS = {
    "copilot": (("Read", {"path": "model/swc/Engine.yaml"}), ("Read", {"path": "notes.md"})),
    "codex": (("Bash", {"command": "cat model/swc/Engine.yaml"}), ("Bash", {"command": "cat notes.md"})),
    "claude": (("Edit", {"file_path": "model/swc/Engine.yaml"}), ("Edit", {"file_path": "notes.md"})),
    "kiro": (("fs_write", {"path": "model/swc/X.yaml", "text": "x"}), ("read_file", {"path": "notes.md"})),
}


def _every_client(root: Path) -> None:
    """Mark *root* as a project every dialect guards."""
    write_codex_hook(root)
    write_claude_settings(root)
    write_kiro_hooks(root)
    (root / "notes").mkdir(exist_ok=True)


def _guard_child(module: str, argv: list[str], stdin: bytes, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", module, *argv],
        input=stdin,
        capture_output=True,
        cwd=str(cwd),
        env={key: value for key, value in os.environ.items() if key not in ("SWCRAFT_MANIFEST", "CLAUDE_PROJECT_DIR")},
        check=False,
    )


@pytest.mark.parametrize("module", ["swcraft_tools.entry", "swcraft_tools.cli"])
@pytest.mark.parametrize("host", [None, "codex", "claude", "kiro"])
def test_each_host_through_each_entry_point(project_dir: Path, user_home: Path, module: str, host: str | None) -> None:
    """Subprocess smoke: ``swcraft guard`` and each ``--host``, an allow and a deny, through both entry points.

    The entry points are the console script's module and the module form; the hook runs in the project root as Kiro
    runs it."""
    _every_client(project_dir)
    argv = ["guard"] if host is None else ["guard", "--host", host]
    deny, allow = _SMOKE_CALLS[host or "copilot"]
    denied = _guard_child(
        module, argv, json.dumps(guard_payload(deny[0], project_dir, **deny[1])).encode("utf-8"), project_dir
    )
    allowed = _guard_child(
        module, argv, json.dumps(guard_payload(allow[0], project_dir, **allow[1])).encode("utf-8"), project_dir
    )
    if host == "kiro":
        assert (denied.returncode, denied.stdout) == (2, b""), denied.stderr
        kiro_deny_line(denied.stderr.decode("utf-8"))
        assert (allowed.returncode, allowed.stdout, allowed.stderr) == (0, b"", b"")
    else:
        assert (denied.returncode, allowed.returncode) == (0, 0), (denied.stderr, allowed.stderr)
        assert json.loads(denied.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert allowed.stdout.decode("utf-8").strip() == "{}"


@pytest.mark.parametrize("module", ["swcraft_tools.entry", "swcraft_tools.cli"])
@pytest.mark.parametrize("host", [None, "codex", "claude", "kiro"])
def test_each_host_answers_an_unexpected_failure(
    project_dir: Path, user_home: Path, module: str, host: str | None
) -> None:
    """JSON past the parser's recursion limit is an unexpected failure, denied in the project under every host.

    Each host answers in its own form, the exception's type named and no traceback."""
    _every_client(project_dir)
    argv = ["guard"] if host is None else ["guard", "--host", host]
    tool, arguments = _SMOKE_CALLS[host or "copilot"][1]
    payload = json.dumps(guard_payload(tool, project_dir, **arguments)).encode("utf-8")
    result = _guard_child(module, argv, payload[:-1] + b', "x": ' + DEEPLY_NESTED[6:-1] + b"}", project_dir)
    err = result.stderr.decode("utf-8")
    assert "Traceback" not in err
    if host == "kiro":
        assert (result.returncode, result.stdout) == (2, b"")
        assert kiro_deny_line(err).endswith("; the guard failed unexpectedly (RecursionError).")
    else:
        assert result.returncode == 0, err
        assert json.loads(result.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Note: the guard failed unexpectedly (RecursionError); this call is denied" in err


@pytest.mark.parametrize(
    ("tool", "arguments", "code"),
    [("fs_write", {"path": "model/swc/X.yaml", "text": "x"}, 2), ("read_file", {"path": "notes.md"}, 0)],
)
def test_the_console_script_answers_kiro_without_importing_cli(
    project_dir: Path, user_home: Path, tool: str, arguments: dict[str, str], code: int
) -> None:
    """``entry`` answers ``guard --host kiro`` itself, exit code included, and never imports the ``cli`` package."""
    write_kiro_hooks(project_dir)
    script = (
        "import sys\n"
        "sys.argv = ['swcraft', 'guard', '--host', 'kiro']\n"
        "from swcraft_tools.entry import main\n"
        "try:\n"
        "    main()\n"
        "    code = 0\n"
        "except SystemExit as exited:\n"
        "    code = exited.code\n"
        "print(f'exit {code}', 'cli imported' if 'swcraft_tools.cli' in sys.modules else 'cli not imported',"
        " file=sys.stderr)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(guard_payload(tool, project_dir, **arguments)).encode("utf-8"),
        capture_output=True,
        cwd=str(project_dir),
        env={key: value for key, value in os.environ.items() if key != "SWCRAFT_MANIFEST"},
        check=False,
    )
    assert result.stdout == b""
    lines = result.stderr.decode("utf-8").splitlines()
    assert lines[-1] == f"exit {code} cli not imported"
    assert len(lines) == (2 if code else 1)


def _read_in(dialect: str, root: Path, name: str) -> copilot_guard.Decision:
    """A read of *name* by the dialect's read tool.

    Copilot's, Codex's and Claude Code's ``Read``, Kiro's ``read_file``."""
    if dialect == "copilot":
        return decide_payload(guard_payload("Read", root, path=name), root)
    if dialect == "codex":
        return decide_codex_tool("Read", root, path=name)
    if dialect == "claude":
        return decide_claude_tool("Read", root, file_path=name)
    return decide_kiro_tool("read_file", root, path=name)


def _glob_in(dialect: str, root: Path, pattern: str) -> copilot_guard.Decision:
    """A search of *pattern* rooted at ``notes`` by the dialect's search tool.

    Copilot's, Codex's and Claude Code's ``Glob``, Kiro's ``grep_search``."""
    if dialect == "copilot":
        return decide_payload(guard_payload("Glob", root, path="notes", pattern=pattern), root)
    if dialect == "codex":
        return decide_codex_tool("Glob", root, path="notes", pattern=pattern)
    if dialect == "claude":
        return decide_claude_tool("Glob", root, path="notes", pattern=pattern)
    return decide_kiro_tool("grep_search", root, **{**GREP_ARGUMENTS, "includePattern": pattern})


def _held_reason(root: Path) -> str:
    """The denial of a path holding the model folder's path, for the project at *root*."""
    project = copilot_guard.read_project(root).project
    assert project is not None
    return guard_model_paths.model_path_reason(copilot_guard._model_subject(project))


def _held(root: Path) -> copilot_guard.Decision:
    return copilot_guard.Decision(allow=False, reason=_held_reason(root))


@pytest.fixture
def every_dialect(kiro_dir: Path) -> Path:
    """:func:`kiro_dir`, guarded by every dialect."""
    _guarded_all(kiro_dir)
    return kiro_dir


_READERS = ["copilot", "codex", "claude", "kiro"]


_WRITERS = ["codex", "claude", "kiro"]


class TestTheModelFolderAnywhere:
    """A judged path holding the model folder's path as consecutive segments is denied whatever a client does with it.

    That is read, search, write and Kiro's ``execute_bash`` ``cwd``; a relative path opening with the last folders of
    a multi-folder model path is denied too; a project kept under a folder named like the model is matched below its
    own directory only."""

    @pytest.mark.parametrize("dialect", _READERS)
    def test_a_prefix_drop_is_denied(self, every_dialect: Path, dialect: str) -> None:
        decision = _read_in(dialect, every_dialect, "zz/yy/model/swc/app/Engine.yaml")
        assert decision == _held(every_dialect)

    @pytest.mark.parametrize("dialect", _READERS)
    def test_a_case_change_is_denied(self, every_dialect: Path, dialect: str) -> None:
        assert _read_in(dialect, every_dialect, "zz/MODEL/swc/Engine.yaml") == _held(every_dialect)
        assert not _read_in(dialect, every_dialect, "MODEL/swc/Engine.yaml").allow

    @pytest.mark.parametrize("dialect", _READERS)
    def test_a_search_under_a_disguised_path_is_denied(self, every_dialect: Path, dialect: str) -> None:
        decision = _glob_in(dialect, every_dialect, "zz/yy/{model,x}/**")
        assert not decision.allow
        assert decision.reason is not None
        assert decision.reason.startswith(_held_reason(every_dialect))

    @pytest.mark.parametrize("dialect", _WRITERS)
    def test_a_new_file_under_a_disguised_path_is_denied(self, every_dialect: Path, dialect: str) -> None:
        assert _written_in(dialect, every_dialect, "zz/yy/model/swc/app/New.yaml") == _held(every_dialect)

    def test_a_kiro_shell_in_a_disguised_folder_is_denied(self, every_dialect: Path) -> None:
        (every_dialect / "zz" / "model").mkdir(parents=True)
        assert decide_kiro_bash(every_dialect, "ls", "zz/model") == _held(every_dialect)
        assert decide_kiro_bash(every_dialect, "ls", "notes").allow

    @pytest.mark.parametrize("dialect", _READERS)
    def test_another_folder_named_like_the_model_is_refused(self, every_dialect: Path, dialect: str) -> None:
        """The accepted false refusal: the way around it is to rename the folder, or to use the SWCraft tools."""
        assert _read_in(dialect, every_dialect, "docs/model/notes.md") == _held(every_dialect)

    @pytest.mark.parametrize("dialect", _WRITERS)
    def test_a_write_to_another_folder_named_like_the_model_is_refused(self, every_dialect: Path, dialect: str) -> None:
        assert _written_in(dialect, every_dialect, "other/model/swc/Engine.yaml") == _held(every_dialect)

    @pytest.mark.parametrize("dialect", _READERS)
    def test_a_harmless_path_is_allowed(self, every_dialect: Path, dialect: str) -> None:
        assert _read_in(dialect, every_dialect, "notes/models/x.md").allow
        assert _glob_in(dialect, every_dialect, "notes/**/*.md").allow

    @pytest.mark.parametrize("dialect", _READERS)
    def test_a_project_under_a_folder_named_like_the_model_is_judged_below_it(
        self, tmp_path: Path, user_home: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch, dialect: str
    ) -> None:
        """Below the project directory resolved, and as the guard found it: through a link named like the model."""
        real = tmp_path / "model" / "proj"
        _write_manifest(real)
        (real / "model").mkdir()
        (real / "notes.md").write_text("x\n", encoding="utf-8", newline="\n")
        _guarded_all(real)
        monkeypatch.chdir(elsewhere)
        assert _read_in(dialect, real, str(real / "notes.md")).allow
        symlink_or_skip(tmp_path / "alias", tmp_path / "model")
        found = tmp_path / "alias" / "proj"
        assert _read_in(dialect, found, str(found / "notes.md")).allow
        relinked = tmp_path / "other" / "model"
        relinked.parent.mkdir()
        symlink_or_skip(relinked, tmp_path / "model" / "proj")
        assert _read_in(dialect, relinked, str(relinked / "notes.md")).allow
        assert _read_in(dialect, relinked, str(relinked / "model" / "x.yaml")).allow is False

    @pytest.mark.parametrize("dialect", _READERS)
    def test_both_spellings_of_a_linked_model_root_are_judged(
        self, tmp_path: Path, user_home: Path, dialect: str
    ) -> None:
        root = tmp_path / "proj"
        (root / "real" / "model" / "swc").mkdir(parents=True)
        symlink_or_skip(root / "link", root / "real")
        codex_project(root, "link/model")
        _guarded_all(root)
        assert _read_in(dialect, root, "zz/link/model/x.yaml") == _held(root)
        assert _read_in(dialect, root, "zz/real/model/x.yaml") == _held(root)

    def test_a_call_directory_linked_deeper_into_the_project_than_its_own_path_is_judged(
        self, claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A folder reached through a link whose own path has fewer folders than its target lies below the project.

        No spelling of the project is derived from it, and a call there is judged as any other."""
        deep = claude_dir.joinpath(*"abcdefghijklmnopqrst")
        deep.mkdir(parents=True)
        link = tmp_path / "l"
        symlink_or_skip(link, deep)
        assert len(link.parents) < len(deep.relative_to(claude_dir).parts)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(claude_dir))
        assert decide_claude_tool("Read", link, file_path="notes.md").allow
        assert decide_claude_tool("Read", link, file_path="zz/model/x.yaml") == _held(claude_dir)

    @pytest.mark.parametrize(
        "tool", sorted(name for name, spec in copilot_guard.KIRO_JUDGED_TOOLS.items() if spec.kind == "write")
    )
    def test_every_kiro_write_tool_is_judged_by_both_rules(self, every_dialect: Path, tool: str) -> None:
        spec = copilot_guard.KIRO_JUDGED_TOOLS[tool]

        def write(path: str) -> copilot_guard.Decision:
            return decide_kiro_tool(
                tool, every_dialect, **dict.fromkeys(spec.other_keys, "x"), **{spec.path_keys[0]: path}
            )

        assert write("zz/model/swc/New.yaml") == _held(every_dialect)
        assert write("docs/swc/Engine.yaml") == _tail_held(every_dialect)
        assert write("notes/a/b.md").allow

    @pytest.mark.parametrize(
        ("tool", "key"),
        [
            (name, key)
            for name, spec in sorted(copilot_guard.KIRO_JUDGED_TOOLS.items())
            if spec.kind not in ("write", "shell")
            for key in spec.path_keys
        ],
    )
    def test_every_kiro_path_key_of_a_reading_tool_is_judged_by_both_rules(
        self, every_dialect: Path, tool: str, key: str
    ) -> None:
        operation = {"operation": "search_symbols"} if tool == "code" else {}

        def read(path: str) -> copilot_guard.Decision:
            return decide_kiro_tool(tool, every_dialect, **operation, **{key: path})

        assert read("zz/model/swc") == _held(every_dialect)
        assert read("docs/swc/Engine.yaml") == _tail_held(every_dialect)
        assert read("notes").allow

    @pytest.mark.parametrize(
        "patch",
        [
            codex_patch("*** Update File: zz/model/swc/Engine.yaml"),
            "*** Begin Patch\n*** Update File: notes/a.md\n*** Move to: zz/model/x.yaml\n@@\n-a\n+b\n*** End Patch\n",
        ],
    )
    def test_a_codex_patch_updating_or_moving_into_a_disguised_path_is_denied(
        self, every_dialect: Path, patch: str
    ) -> None:
        (every_dialect / "notes" / "a.md").write_text("a\n", encoding="utf-8", newline="\n")
        assert decide_codex_tool("apply_patch", every_dialect, command=patch) == _held(every_dialect)


class TestANestedModelFolder:
    """With ``model_root: arch/model`` a relative path opening with ``model`` is denied too.

    The run is ``arch``, ``model``; a second Kiro workspace root nested at the model folder makes ``model/x.yaml`` a
    model file."""

    @pytest.fixture
    def nested(self, tmp_path: Path, user_home: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(elsewhere)
        return _nested_model_project(tmp_path / "nested")

    @pytest.mark.parametrize("dialect", _WRITERS)
    def test_a_path_opening_with_the_last_folder_is_denied(self, nested: Path, dialect: str) -> None:
        assert _written_in(dialect, nested, "model/x.yaml") == _held(nested)

    @pytest.mark.parametrize("dialect", _READERS)
    def test_the_reads_it_refuses(self, nested: Path, dialect: str) -> None:
        """``model/notes.md`` is the added false refusal; ``Arch/Model`` was refused already."""
        assert _read_in(dialect, nested, "model/notes.md") == _held(nested)
        assert _read_in(dialect, nested, "zz/arch/model/x.yaml") == _held(nested)
        assert not _read_in(dialect, nested, "Arch/Model/swc/Engine.yaml").allow

    @pytest.mark.parametrize("dialect", _READERS)
    def test_one_segment_of_the_run_elsewhere_is_allowed(self, nested: Path, dialect: str) -> None:
        assert _read_in(dialect, nested, "notes/arch/x").allow
        assert _read_in(dialect, nested, "notes/x/model").allow


def _tail_held(
    root: Path, reason: Callable[[str], str] = guard_model_paths.model_entry_reason
) -> copilot_guard.Decision:
    """The denial of a path ending like a model entry (or, with *reason*, of an unreadable model tree) at *root*."""
    project = copilot_guard.read_project(root).project
    assert project is not None
    return copilot_guard.Decision(allow=False, reason=reason(copilot_guard._model_subject(project)))


class TestAModelEntrysTail:
    """A judged path whose last two segments are an existing model entry's folder and name is denied.

    It is denied whatever a client does with it -- ``model/swc/Engine.yaml`` exists, so ``elsewhere/swc/Engine.yaml``
    is refused -- and while the model tree cannot be read every path of two or more segments is."""

    @pytest.mark.parametrize("dialect", _READERS)
    def test_a_name_only_lookup_is_denied(self, every_dialect: Path, dialect: str) -> None:
        assert _read_in(dialect, every_dialect, "elsewhere/swc/Engine.yaml") == _tail_held(every_dialect)
        assert _glob_in(dialect, every_dialect, "notes/**/SWC/engine.yaml") == _tail_held(every_dialect)

    def test_a_kiro_context_file_is_denied(self, every_dialect: Path) -> None:
        assert decide_kiro_sub_agent(every_dialect, "elsewhere/swc/Engine.yaml") == _tail_held(every_dialect)

    @pytest.mark.parametrize("dialect", _READERS)
    def test_a_same_tail_file_outside_the_model_is_refused(self, every_dialect: Path, dialect: str) -> None:
        """The accepted false refusal: the way around it is to rename the file, or to use the SWCraft tools."""
        (every_dialect / "docs" / "swc").mkdir(parents=True)
        (every_dialect / "docs" / "swc" / "Engine.yaml").write_text("x\n", encoding="utf-8", newline="\n")
        assert _read_in(dialect, every_dialect, "docs/swc/Engine.yaml") == _tail_held(every_dialect)

    @pytest.mark.parametrize("dialect", _WRITERS)
    def test_a_write_ending_like_a_model_entry_is_refused(self, every_dialect: Path, dialect: str) -> None:
        assert _written_in(dialect, every_dialect, "docs/swc/Engine.yaml") == _tail_held(every_dialect)

    @pytest.mark.parametrize("dialect", _READERS)
    def test_a_tail_no_entry_has_and_a_glob_tail_are_allowed(self, every_dialect: Path, dialect: str) -> None:
        assert _read_in(dialect, every_dialect, "notes/a/b.md").allow
        assert _glob_in(dialect, every_dialect, "notes/swc/*.yaml").allow

    @pytest.mark.parametrize("dialect", _READERS)
    def test_an_unreadable_model_tree_locks_out_every_path_of_two_segments(
        self, every_dialect: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch, dialect: str
    ) -> None:
        monkeypatch.setattr(guard_model_paths, "_MAX_MODEL_ENTRIES", 0)
        locked = _tail_held(every_dialect, guard_model_paths.unreadable_tree_reason)
        assert _read_in(dialect, every_dialect, str(elsewhere / "x.md")) == locked
        assert _read_in(dialect, every_dialect, "notes/a/b.md") == locked
        assert _read_in(dialect, every_dialect, "notes.md").allow

    def test_a_codex_patch_walks_the_model_tree_within_its_judgement_deadline(
        self, every_dialect: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One bound for the whole judgement: the run's model-path bound is lowered to the patch's deadline.

        The bound applies from the project's spellings on; a Claude Code write hands on none."""
        limits: list[float] = []
        run_end = guard_model_paths.run_end

        def recording(limit: float | None = None) -> float:
            if limit is not None:
                limits.append(limit)
            return run_end(limit)

        monkeypatch.setattr(guard_model_paths, "run_end", recording)
        before = time.monotonic()
        assert _written_in("claude", every_dialect, "notes/a/b.md").allow
        assert limits == []
        assert _written_in("codex", every_dialect, "notes/a/b.md").allow
        assert len(set(limits)) == 1
        assert len(limits) >= 2
        assert before < limits[0] <= time.monotonic() + copilot_guard._JUDGE_SECONDS

    def test_the_rules_on_the_written_path_resolve_the_roots_once(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One judgement resolves the project directory and the model root once each, the rest reusing them."""
        guarded = copilot_guard.read_project(project_dir).project
        assert guarded is not None
        resolved: list[Path] = []
        resolve = project._resolved

        def counting(path: Path) -> str:
            resolved.append(path)
            return resolve(path)

        monkeypatch.setattr(project, "_resolved", counting)
        decision = copilot_guard._model_path_decision(guarded, project_dir, ["notes/a/b.md"])
        assert decision.allow
        assert len(resolved) == 2

    def test_no_root_is_resolved_past_the_runs_bound(self, project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        guarded = copilot_guard.read_project(project_dir).project
        assert guarded is not None
        resolved: list[Path] = []
        monkeypatch.setattr(project, "_resolved", resolved.append)
        monkeypatch.setattr(guard_model_paths.time, "monotonic", lambda: float("inf"))
        decision = copilot_guard._model_path_decision(guarded, project_dir, ["notes.md"])
        locked = guard_model_paths.unreadable_tree_reason("the model directory")
        assert decision == copilot_guard.Decision(allow=False, reason=locked)
        assert resolved == []

    def test_nothing_to_judge_resolves_nothing(self, project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        guarded = copilot_guard.read_project(project_dir).project
        assert guarded is not None
        resolved: list[Path] = []
        monkeypatch.setattr(project, "_resolved", resolved.append)
        assert copilot_guard._model_path_decision(guarded, project_dir, [None]).allow
        assert resolved == []

    def test_a_kiro_shell_in_a_folder_ending_like_a_model_entry_is_denied(self, every_dialect: Path) -> None:
        (every_dialect / "docs" / "swc" / "Engine.yaml").mkdir(parents=True)
        assert decide_kiro_bash(every_dialect, "ls", "docs/swc/Engine.yaml") == _tail_held(every_dialect)

    def test_an_unreadable_model_tree_locks_out_a_kiro_shell_in_an_absolute_folder(
        self, every_dialect: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(guard_model_paths, "_MAX_MODEL_ENTRIES", 0)
        locked = _tail_held(every_dialect, guard_model_paths.unreadable_tree_reason)
        assert decide_kiro_bash(every_dialect, "ls", str(every_dialect / "notes")) == locked
        assert decide_kiro_bash(every_dialect, "ls", "notes").allow
