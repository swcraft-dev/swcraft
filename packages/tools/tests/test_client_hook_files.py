"""The hook files a project ships for Codex, Claude Code and Kiro refuse a model edit.

Each client's ``PreToolUse`` entry is read out of a project ``swcraft init`` scaffolds, never restated here, and run
from the project folder in a minimal environment (no ``SWCRAFT_*`` variable), one payload per call: Claude Code's
and Kiro's are payloads Claude Code 2.1.319 and kiro-cli 3.1.0 sent (``fixtures/claude_hook/``,
``fixtures/kiro_hook/``; a shell tool not captured is the ``Bash``/``execute_bash`` capture renamed), Codex's are
the shapes of the Codex dialect's tests in ``test_copilot_guard_codex.py``.

Codex and Claude Code deny in exactly ``{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision":
"deny", "permissionDecisionReason": <non-empty>}}`` and nothing else, with exit 0, an empty stderr and no reason naming
where the project is stored; an allow is ``{}``. Kiro denies with exit 2, exactly one stderr line starting ``SWCraft:``
and an empty stdout; an allow is exit 0 with both streams empty.

The Codex and Claude Code matchers are anchored ``^(...)$`` alternations listing exactly the tool names their dialect
judges, and never SWCraft's own MCP tools. They are applied with Python's ``re``; that the clients' engines (Rust's for
Codex, JavaScript's for Claude Code) agree on this grammar is a reading, not a test. Kiro's matcher must be ``*``: its
dialect judges by an allowlist, so every call must be handed over. :data:`RUN_SECONDS` is a deadline per process, not
the hook file's own ``timeout`` (10 s). The Copilot hook file's run is ``test_copilot_guard_copilot.py``'s.

What it does not reach: a live client run; whether the client runs the hook at all (a folder or hook not trusted,
hooks turned off, a session started outside the project or, for Claude Code, in a subfolder, no or an older
``swcraft`` on the login shell's ``PATH``); how the client spawns the command (its shell, ``PATH``, deadline); the
tool names and payload keys of a client release other than those captured or written in the Codex dialect's tests;
Kiro's own reading of its matcher, its trust of the folder and its hook timeout (a timed-out hook lets the tool
run); and what each host's limits on the guide page ``docs/guide/model-guard.md`` list -- a command
reaching the model without naming it, a script, a variable, ``~`` in a shell word (the Kiro and Claude Code file
tools judge a leading ``~`` under both readings), another MCP server's file tools.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sysconfig
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from swcraft_tools import copilot_guard
from symlink_support import symlink_or_skip

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook"

#: The kiro-cli 3.1.0 payloads.
KIRO_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kiro_hook"

#: The folder the Claude Code and Kiro captures were sanitised to.
CAPTURED_PROJECT = "/project"

#: The deadline for one run: the scaffold, or one hook process.
RUN_SECONDS = 60

#: Claude Code's read and search tool names, judged by the Copilot rules (``Grep`` and ``Glob`` were absent
#: from 2.1.319's tool list and stay in the matcher).
CLAUDE_CODE_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})

#: Names the clients give SWCraft's own MCP tools (Claude Code's ``mcp__<server>__<tool>``) and a tool no
#: dialect judges: the matchers must not hand them over.
NOT_GUARDED = ("mcp__swcraft__read", "mcp__swcraft__change_set", "Agent", "ToolSearch")

#: The variables a process needs to start at all; nothing else is inherited.
CARRIED_VARIABLES = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")


# The project and its hook files


@dataclass(frozen=True)
class Hook:
    """The guard's ``PreToolUse`` entry as one hook file declares it."""

    origin: str
    matcher: str
    command: str


@dataclass(frozen=True)
class Project:
    root: Path
    environment: Mapping[str, str]


def scripts_directory() -> Path:
    return Path(sysconfig.get_path("scripts")).resolve()


def environment_for(home: Path) -> dict[str, str]:
    """A minimal environment: ``PATH`` led by this interpreter's scripts directory, the test's own home."""
    environment = {name: os.environ[name] for name in CARRIED_VARIABLES if name in os.environ}
    environment["PATH"] = os.fspath(scripts_directory()) + os.pathsep + os.environ.get("PATH", os.defpath)
    for name in ("HOME", "USERPROFILE"):
        environment[name] = os.fspath(home)
    environment["LOCALAPPDATA"] = os.fspath(home / "AppData" / "Local")
    return environment


def resolved_swcraft(name: str, environment: Mapping[str, str]) -> str:
    """*name* found on *environment*'s ``PATH``, as a client finds it, and asserted to be this interpreter's."""
    resolved = shutil.which(name, path=environment["PATH"])
    assert resolved is not None, f"{name!r} is not found on {environment['PATH']!r}"
    assert Path(resolved).resolve().parent == scripts_directory(), (
        f"{name!r} resolved to {Path(resolved).as_posix()}, not into {scripts_directory().as_posix()}"
    )
    return resolved


#: The clients whose hook files this module runs.
CLIENTS = ("--client", "codex", "--client", "claude", "--client", "kiro")


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    """A project scaffolded with the three clients' hook files; the hooks only read it, so one serves all.

    The clients are named because the default is Copilot alone, which writes none of their hook files.
    """
    base = tmp_path_factory.mktemp("hook-files")
    home = base / "home"
    home.mkdir()
    environment = environment_for(home)
    root = base / "project"
    completed = subprocess.run(
        [
            resolved_swcraft("swcraft", environment),
            "init",
            os.fspath(root),
            "--integration",
            "moon",
            *CLIENTS,
            "--name",
            "hooks",
        ],
        cwd=os.fspath(base),
        env=environment,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=RUN_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return Project(root.resolve(), environment)


def guard_hook(project: Project, relative: str) -> Hook:
    """The one ``PreToolUse`` entry of *relative* whose command hook runs ``swcraft guard``."""
    document = json.loads((project.root / relative).read_text(encoding="utf-8"))
    found = [
        Hook(relative, entry.get("matcher"), hook.get("command"))
        for entry in document.get("hooks", {}).get("PreToolUse", [])
        for hook in entry.get("hooks", [])
        if hook.get("type") == "command" and str(hook.get("command", "")).startswith("swcraft guard")
    ]
    assert len(found) == 1, f"{relative}: expected one PreToolUse command hook running `swcraft guard`, got {found!r}"
    hook = found[0]
    assert isinstance(hook.matcher, str) and isinstance(hook.command, str), hook
    return hook


def claude_hook(project: Project) -> Hook:
    return guard_hook(project, ".claude/settings.json")


def codex_hook(project: Project) -> Hook:
    return guard_hook(project, ".codex/hooks.json")


#: Kiro's hook file in the template.
KIRO_HOOK_FILE = ".kiro/hooks/swcraft-guard.json"


def kiro_hook(project: Project) -> Hook:
    """The one entry of Kiro's hook file whose command action runs ``swcraft guard``, its trigger ``PreToolUse``."""
    document = json.loads((project.root / KIRO_HOOK_FILE).read_text(encoding="utf-8"))
    entries = [
        entry
        for entry in document.get("hooks", [])
        if entry.get("action", {}).get("type") == "command"
        and str(entry.get("action", {}).get("command", "")).startswith("swcraft guard")
    ]
    assert len(entries) == 1, f"{KIRO_HOOK_FILE}: expected one command hook running `swcraft guard`, got {entries!r}"
    entry = entries[0]
    assert entry.get("trigger") == "PreToolUse", f"{KIRO_HOOK_FILE}: trigger {entry.get('trigger')!r}"
    hook = Hook(KIRO_HOOK_FILE, entry.get("matcher"), entry["action"]["command"])
    assert isinstance(hook.matcher, str) and isinstance(hook.command, str), hook
    return hook


# The run


def run_hook(project: Project, hook: Hook, payload: Mapping[str, Any]) -> dict[str, Any]:
    """*hook*'s command run from the project folder with *payload* on stdin: its answer, exit 0 and stderr empty."""
    argv = shlex.split(hook.command)
    command = [resolved_swcraft(argv[0], project.environment), *argv[1:]]
    completed = subprocess.run(
        command,
        input=json.dumps(payload).encode("utf-8"),
        cwd=os.fspath(project.root),
        env=dict(project.environment),
        capture_output=True,
        timeout=RUN_SECONDS,
        check=False,
    )
    stdout = completed.stdout.decode("utf-8")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    assert completed.returncode == 0, f"{hook.origin}: exit {completed.returncode}; stderr:\n{stderr}"
    assert stderr == "", f"{hook.origin}: stderr:\n{stderr}"
    for spelling in {os.fspath(project.root), project.root.as_posix()}:
        assert spelling not in stdout, f"{hook.origin}: the answer names where the project is stored: {stdout}"
    answer = json.loads(stdout)
    assert isinstance(answer, dict), stdout
    return answer


@dataclass(frozen=True)
class KiroRun:
    returncode: int
    stdout: str
    stderr: str


def run_kiro_hook(project: Project, hook: Hook, payload: Mapping[str, Any] | bytes) -> KiroRun:
    """Kiro's *hook* run with *payload* (a mapping as JSON, bytes as given); no stream names the project's place."""
    argv = shlex.split(hook.command)
    command = [resolved_swcraft(argv[0], project.environment), *argv[1:]]
    completed = subprocess.run(
        command,
        input=payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8"),
        cwd=os.fspath(project.root),
        env=dict(project.environment),
        capture_output=True,
        timeout=RUN_SECONDS,
        check=False,
    )
    run = KiroRun(
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )
    for spelling in {os.fspath(project.root), project.root.as_posix()}:
        assert spelling not in run.stdout + run.stderr, (
            f"{hook.origin}: the answer names where the project is stored: {run}"
        )
    return run


def assert_kiro_denied(run: KiroRun, phrase: str) -> None:
    """Kiro's deny: exit 2, nothing on stdout, exactly one stderr line starting ``SWCraft:`` and holding *phrase*."""
    assert run.returncode == 2, run
    assert run.stdout == "", run
    lines = run.stderr.splitlines()
    assert len(lines) == 1 and run.stderr in (lines[0], lines[0] + "\n", lines[0] + "\r\n"), run
    assert lines[0].startswith("SWCraft:"), run
    assert phrase in lines[0], f"the deny's line does not hold {phrase!r}: {lines[0]}"


def assert_kiro_allowed(run: KiroRun) -> None:
    assert run.returncode == 0 and run.stdout == "" and run.stderr == "", run


def assert_denied(answer: Mapping[str, Any], phrase: str) -> None:
    """The deny both clients read: ``hookSpecificOutput`` alone, its three keys, a reason holding *phrase*."""
    assert set(answer) == {"hookSpecificOutput"}, answer
    specific = answer["hookSpecificOutput"]
    assert set(specific) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}, specific
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    reason = specific["permissionDecisionReason"]
    assert isinstance(reason, str) and reason.strip(), specific
    assert phrase in reason, f"the deny's reason does not hold {phrase!r}: {reason}"


def assert_allowed(answer: Mapping[str, Any]) -> None:
    assert answer == {}, answer


# The payloads


def _repointed(value: Any, root: str) -> Any:
    if isinstance(value, str):
        if value == CAPTURED_PROJECT or value.startswith(CAPTURED_PROJECT + "/"):
            return root + value[len(CAPTURED_PROJECT) :]
        return value
    if isinstance(value, list):
        return [_repointed(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _repointed(item, root) for key, item in value.items()}
    return value


def claude_payload(
    project: Project,
    capture: str,
    tool_input: Mapping[str, Any] | None = None,
    tool_name: str | None = None,
    fixtures: Path = FIXTURES,
) -> dict[str, Any]:
    """The captured payload *capture* re-pointed at the project; *tool_input* (``/project`` paths) replaces its own.

    *fixtures* is the folder of captures: Claude Code's, or :data:`KIRO_FIXTURES` (:func:`kiro_payload`).
    """
    payload = json.loads((fixtures / f"{capture}.json").read_text(encoding="utf-8"))
    if tool_input is not None:
        payload["tool_input"] = copy.deepcopy(dict(tool_input))
    if tool_name is not None:
        payload["tool_name"] = tool_name
    # The native spelling, as Claude Code sends ``cwd`` and ``file_path`` (``C:\\...`` on Windows), never
    # ``as_posix()``; the captured tail keeps its ``/``, which every Windows path reader accepts.
    return _repointed(payload, os.fspath(project.root))


def kiro_payload(
    project: Project, capture: str, tool_input: Mapping[str, Any] | None = None, tool_name: str | None = None
) -> dict[str, Any]:
    """A Kiro capture re-pointed at the project, as :func:`claude_payload` re-points Claude Code's."""
    return claude_payload(project, capture, tool_input, tool_name, fixtures=KIRO_FIXTURES)


def codex_payload(project: Project, tool: str, command: str) -> dict[str, Any]:
    """A Codex payload in the shape the Codex dialect's tests use, ``cwd`` the project folder."""
    return {
        "tool_name": tool,
        "tool_input": {"command": command},
        "hook_event_name": "PreToolUse",
        "cwd": os.fspath(project.root),
    }


def patch(path: str) -> str:
    return f"*** Begin Patch\n*** Update File: {path}\n@@\n-a\n+b\n*** End Patch\n"


#: A model file the template ships (``model_root: model``).
MODEL_FILE = "model/hardware/CPU.yaml"


#: The phrases that tell the reasons apart: the model directory named relative to the project, the
#: shared guard-input set, the PowerShell refusal.
MODEL = "`model`"
GUARD_INPUT = (
    "swcraft.yaml, .mcp.json and its .codex, .claude, .github/hooks, .kiro/hooks, .kiro/settings and .kiro/agents "
    "directories"
)
POWERSHELL = "PowerShell"
#: The Kiro dialect's closed argument schema, and its allowlist.
UNKNOWN_KEY = "arguments are not the ones the model guard knows"
UNKNOWN_TOOL = "is not one this release's model guard knows"
#: The Kiro dialect's denial of a hook call it cannot read as a JSON object naming its tool (malformed input).
UNNAMED = "cannot read this hook call as a JSON object naming its tool"
#: The denial of a call reaching the review hand-off directory.
HANDOFF = "hand-off directory"
#: The hand-off directory written from the user's home with ``~`` (:func:`environment_for`'s SWCraft home):
#: ``~/.swcraft/reviews``, ``~/AppData/Local/SWCraft/reviews`` on Windows.
HANDOFF_FROM_HOME = "~/AppData/Local/SWCraft/reviews" if os.name == "nt" else "~/.swcraft/reviews"


@dataclass(frozen=True)
class ClaudeCase:
    capture: str
    tool_input: Mapping[str, Any] | None
    #: A phrase the deny's reason must hold (:data:`MODEL`, :data:`GUARD_INPUT`, :data:`POWERSHELL`), so a
    #: deny for another cause -- a fail-closed one -- does not pass; ``None`` for an allow.
    denied: str | None
    tool_name: str | None = None


CLAUDE_CASES: dict[str, ClaudeCase] = {
    "edit-model-file": ClaudeCase(
        "Edit",
        {"file_path": f"/project/{MODEL_FILE}", "old_string": "a", "new_string": "b", "replace_all": False},
        MODEL,
    ),
    "write-model-file": ClaudeCase(
        "Write-notes", {"file_path": "/project/model/hardware/NEW.yaml", "content": "x"}, MODEL
    ),
    "notebook-in-model": ClaudeCase(
        "NotebookEdit", {"notebook_path": "/project/model/nb.ipynb", "cell_id": "cell-0", "new_source": "x"}, MODEL
    ),
    "shell-writes-model-file": ClaudeCase(
        "Bash-ls-model", {"command": f"printf 'x' > {MODEL_FILE}", "description": "Write a model file"}, MODEL
    ),
    "read-model-file": ClaudeCase("Read-README-md", {"file_path": f"/project/{MODEL_FILE}"}, MODEL),
    "shell-lists-model-as-captured": ClaudeCase("Bash-ls-model", None, MODEL),
    "edit-guard-input-manifest": ClaudeCase(
        "Edit",
        {"file_path": "/project/swcraft.yaml", "old_string": "a", "new_string": "b", "replace_all": False},
        GUARD_INPUT,
    ),
    "edit-own-hook-file": ClaudeCase(
        "Edit",
        {"file_path": "/project/.claude/settings.json", "old_string": "a", "new_string": "b", "replace_all": False},
        GUARD_INPUT,
    ),
    "edit-codex-hook-file": ClaudeCase(
        "Edit",
        {"file_path": "/project/.codex/hooks.json", "old_string": "a", "new_string": "b", "replace_all": False},
        GUARD_INPUT,
    ),
    "write-codex-hook-file": ClaudeCase(
        "Write-notes", {"file_path": "/project/.codex/hooks.json", "content": "{}"}, GUARD_INPUT
    ),
    "write-kiro-hook-file": ClaudeCase(
        "Write-notes", {"file_path": "/project/.kiro/hooks/x.json", "content": "{}"}, GUARD_INPUT
    ),
    "monitor-writes-model-file": ClaudeCase(
        "Bash-ls-model",
        {"command": f"printf 'x' > {MODEL_FILE}", "description": "Watch", "timeout_ms": 1000},
        MODEL,
        tool_name="Monitor",
    ),
    "powershell-naming-nothing": ClaudeCase(
        "Bash-pwd", {"command": "Get-Location", "description": "Print the folder"}, POWERSHELL, tool_name="PowerShell"
    ),
    "edit-outside-model-as-captured": ClaudeCase("Edit", None, None),
    "write-outside-model-as-captured": ClaudeCase("Write-notes", None, None),
    "read-outside-model-as-captured": ClaudeCase("Read-README-md", None, None),
    "shell-outside-model-as-captured": ClaudeCase("Bash-pwd", None, None),
}


CODEX_CASES: dict[str, tuple[str, str, str | None]] = {
    "patch-model-file": ("apply_patch", patch(MODEL_FILE), MODEL),
    "shell-writes-model-file": ("Bash", f"printf 'x' > {MODEL_FILE}", MODEL),
    "shell-reads-model-file": ("Bash", f"cat {MODEL_FILE}", MODEL),
    "patch-guard-input-manifest": ("apply_patch", patch("swcraft.yaml"), GUARD_INPUT),
    "patch-own-hook-file": ("apply_patch", patch(".codex/hooks.json"), GUARD_INPUT),
    "patch-claude-settings": ("apply_patch", patch(".claude/settings.json"), GUARD_INPUT),
    "patch-kiro-hook-file": ("apply_patch", patch(".kiro/hooks/swcraft-guard.json"), GUARD_INPUT),
    "shell-writes-claude-local-settings": (
        "Bash",
        """printf '{"disableAllHooks": true}' > .claude/settings.local.json""",
        GUARD_INPUT,
    ),
    "patch-outside-model": ("apply_patch", patch("README.md"), None),
    "shell-reads-outside-model": ("Bash", "cat README.md", None),
}


def _fs_write(path: str) -> dict[str, Any]:
    return {"path": path, "text": "{}"}


@dataclass(frozen=True)
class KiroCase:
    capture: str
    tool_input: Mapping[str, Any] | None
    #: A phrase the denial's line must hold, so a denial for another cause does not pass; ``None`` for an allow.
    denied: str | None
    tool_name: str | None = None
    #: The bytes sent instead of the capture (malformed input); the hook still runs in the project folder.
    raw: bytes | None = None
    #: Whether the payload's ``cwd`` is moved to a folder outside the project (the hook still runs in the project).
    cwd_outside: bool = False


def _read_file(path: str) -> dict[str, Any]:
    return {"path": path, "offset": None, "limit": None}


def _sub_agent(*paths: str) -> dict[str, Any]:
    """``invoke_sub_agent``'s arguments as captured, its context files at *paths*."""
    return {
        "name": "general-task-execution",
        "prompt": "x",
        "explanation": "x",
        "preset": None,
        "contextFiles": [{"path": path} for path in paths],
        "specTask": None,
    }


KIRO_CASES: dict[str, KiroCase] = {
    "fs-write-model-file-as-captured": KiroCase("fs_write-model-recon", None, MODEL),
    "str-replace-model-file-as-captured": KiroCase("str_replace-model-recon", None, MODEL),
    "delete-model-file": KiroCase(
        "delete_file-notes", {"explanation": "Delete a model file", "targetFile": f"/project/{MODEL_FILE}"}, MODEL
    ),
    "shell-writes-model-file-as-captured": KiroCase("execute_bash-model-recon", None, MODEL),
    "read-model-file-as-captured": KiroCase("read_file-model-recon", None, MODEL),
    "fs-write-guard-input-manifest": KiroCase("fs_write-notes", _fs_write("swcraft.yaml"), GUARD_INPUT),
    "fs-write-own-hook-folder": KiroCase("fs_write-notes", _fs_write(".kiro/hooks/x.json"), GUARD_INPUT),
    "fs-write-codex-hook-file": KiroCase("fs_write-notes", _fs_write(".codex/hooks.json"), GUARD_INPUT),
    "fs-write-claude-settings": KiroCase("fs_write-notes", _fs_write(".claude/settings.json"), GUARD_INPUT),
    "fs-write-copilot-hook-file": KiroCase("fs_write-notes", _fs_write(".github/hooks/swcraft.json"), GUARD_INPUT),
    "fs-write-claude-server-file": KiroCase("fs_write-notes", _fs_write(".mcp.json"), GUARD_INPUT),
    "delete-kiro-folder": KiroCase(
        "delete_file-notes", {"explanation": "Delete the folder", "targetFile": "/project/.kiro"}, GUARD_INPUT
    ),
    "execute-pwsh": KiroCase("execute_bash-notes", None, POWERSHELL, tool_name="execute_pwsh"),
    "unknown-argument-key": KiroCase(
        "read_file-README", {"path": "README.md", "offset": None, "limit": None, "paths": ["notes"]}, UNKNOWN_KEY
    ),
    "unknown-tool": KiroCase("read_file-README", None, UNKNOWN_TOOL, tool_name="read_files"),
    "not-json": KiroCase("fs_write-notes", None, UNNAMED, raw=b"not json"),
    "names-no-tool": KiroCase("fs_write-notes", None, UNNAMED, raw=b'{"tool_input": {"path": "notes/x.md"}}'),
    "cwd-outside-relative-model-write": KiroCase(
        "fs_write-notes", {"path": MODEL_FILE, "text": "x"}, MODEL, cwd_outside=True
    ),
    "cwd-outside-absolute-model-read": KiroCase(
        "read_file-model-recon",
        {"path": f"/project/{MODEL_FILE}", "offset": None, "limit": None},
        MODEL,
        cwd_outside=True,
    ),
    "cwd-outside-write-elsewhere": KiroCase(
        "fs_write-notes", {"path": "notes/x.md", "text": "x"}, None, cwd_outside=True
    ),
    "fs-write-outside-model": KiroCase("fs_write-notes", {"path": "notes/x.md", "text": "x"}, None),
    # ripgrep's ``--glob`` matches a bare file name at any depth, the model's file of that name too.
    "grep-bare-model-file-name": KiroCase(
        "grep_search-include",
        {
            "query": "name",
            "caseSensitive": None,
            "excludePattern": None,
            "explanation": "x",
            "includePattern": "CPU.yaml",
        },
        MODEL,
    ),
    "grep-anchored-outside-model": KiroCase(
        "grep_search-include",
        {
            "query": "name",
            "caseSensitive": None,
            "excludePattern": None,
            "explanation": "x",
            "includePattern": "notes/*.md",
        },
        None,
    ),
    "swcraft-mcp-read-as-captured": KiroCase("ide-mcp_swcraft_read", None, None),
    # Kiro expands a leading ``~`` to the home, so both readings are judged.
    "fs-write-hand-off-record-through-tilde": KiroCase(
        "fs_write-notes", _fs_write(f"{HANDOFF_FROM_HOME}/a.decision.json"), HANDOFF
    ),
    "fs-write-model-file-through-tilde": KiroCase("fs_write-notes", _fs_write(f"~/../project/{MODEL_FILE}"), MODEL),
    "read-hand-off-record-through-tilde": KiroCase(
        "read_file-README", {"path": f"{HANDOFF_FROM_HOME}/a.pending.json", "offset": None, "limit": None}, HANDOFF
    ),
    "fs-write-in-the-home-through-tilde": KiroCase("fs_write-notes", _fs_write("~/notes.txt"), None),
    # read_file retries a failed read with the first segment stripped, a context file the same way
    # with no ``..`` check, and a path's first segment may name a workspace folder: every reading is judged.
    "read-model-file-through-read-fallback": KiroCase("read_file-README", _read_file(f"zz/{MODEL_FILE}"), MODEL),
    "read-model-file-through-tilde-fallback": KiroCase("read_file-README", _read_file(f"~/{MODEL_FILE}"), MODEL),
    "read-outside-model-with-a-fallback": KiroCase("read_file-README", _read_file("notes/x.md"), None),
    "context-file-model-file-through-fallback": KiroCase("invoke_sub_agent", _sub_agent(f"zz/{MODEL_FILE}"), MODEL),
    "context-file-hand-off-record-through-fallback": KiroCase(
        "invoke_sub_agent", _sub_agent(f"zz/../home/{HANDOFF_FROM_HOME[2:]}/a.pending.json"), HANDOFF
    ),
    "context-file-outside-model": KiroCase("invoke_sub_agent", _sub_agent("docs/a.md"), None),
    "fs-write-model-file-named-from-the-project-folder": KiroCase(
        "fs_write-notes", _fs_write(f"project/{MODEL_FILE}"), MODEL
    ),
}


# The runs


@pytest.mark.parametrize("case", list(CLAUDE_CASES))
def test_the_claude_hook_file_answers(project: Project, case: str) -> None:
    spec = CLAUDE_CASES[case]
    hook = claude_hook(project)
    payload = claude_payload(project, spec.capture, spec.tool_input, spec.tool_name)
    assert re.search(hook.matcher, payload["tool_name"]), f"{hook.matcher!r} does not hand {payload['tool_name']!r}"
    answer = run_hook(project, hook, payload)
    if spec.denied is not None:
        assert_denied(answer, spec.denied)
    else:
        assert_allowed(answer)


@pytest.mark.parametrize("case", list(CODEX_CASES))
def test_the_codex_hook_file_answers(project: Project, case: str) -> None:
    tool, command, denied = CODEX_CASES[case]
    hook = codex_hook(project)
    assert re.search(hook.matcher, tool), f"{hook.matcher!r} does not hand {tool!r}"
    answer = run_hook(project, hook, codex_payload(project, tool, command))
    if denied is not None:
        assert_denied(answer, denied)
    else:
        assert_allowed(answer)


@pytest.mark.parametrize("case", list(KIRO_CASES))
def test_the_kiro_hook_file_answers(project: Project, case: str) -> None:
    spec = KIRO_CASES[case]
    hook = kiro_hook(project)
    payload = kiro_payload(project, spec.capture, spec.tool_input, spec.tool_name)
    assert hook.matcher == "*", f"{hook.origin}: the matcher {hook.matcher!r} does not hand {payload['tool_name']!r}"
    if spec.cwd_outside:
        outside = project.root.parent / "outside-the-project"
        outside.mkdir(exist_ok=True)
        payload = {**payload, "cwd": os.fspath(outside)}
    run = run_kiro_hook(project, hook, spec.raw if spec.raw is not None else payload)
    if spec.denied is not None:
        assert_kiro_denied(run, spec.denied)
    else:
        assert_kiro_allowed(run)


def test_the_kiro_hook_names_a_project_opened_through_a_symlink_by_the_link(project: Project) -> None:
    """A write through the name of a symlink the project was opened by is denied.

    kiro-cli 3.1.0 matches a path's first segment against the workspace's basename without a realpath."""
    link = project.root.parent / "linkproj"
    if not link.is_symlink():
        symlink_or_skip(link, project.root)
    payload = kiro_payload(project, "fs_write-notes", _fs_write(f"linkproj/{MODEL_FILE}"))
    run = run_kiro_hook(project, kiro_hook(project), {**payload, "cwd": os.fspath(link)})
    assert_kiro_denied(run, MODEL)


def test_the_model_files_named_are_the_scaffolds(project: Project) -> None:
    """The cases name a model file that is there, and the guard input and hook files the template ships."""
    for relative in (
        MODEL_FILE,
        "swcraft.yaml",
        ".claude/settings.json",
        ".codex/hooks.json",
        KIRO_HOOK_FILE,
        ".kiro/settings/mcp.json",
        "README.md",
    ):
        assert (project.root / relative).is_file(), relative


# The matchers


_ANCHORED_ALTERNATION = re.compile(r"\^\(([A-Za-z_|]+)\)\$")


def alternatives(hook: Hook) -> frozenset[str]:
    """The tool names an anchored ``^(A|B|...)$`` matcher lists; another form fails, naming the file."""
    match = _ANCHORED_ALTERNATION.fullmatch(hook.matcher)
    assert match is not None, f"{hook.origin}: the matcher {hook.matcher!r} is not an anchored ^(A|B|...)$"
    return frozenset(match.group(1).split("|"))


def test_the_claude_matcher_hands_every_judged_tool_and_only_those(project: Project) -> None:
    hook = claude_hook(project)
    judged = (
        CLAUDE_CODE_READ_TOOLS
        | copilot_guard.CLAUDE_WRITE_TOOLS
        | copilot_guard.CLAUDE_SHELL_TOOLS
        | copilot_guard.CLAUDE_DENIED_TOOLS
    )
    assert CLAUDE_CODE_READ_TOOLS <= copilot_guard.READ_TOOLS | copilot_guard.SEARCH_TOOLS
    for tool in sorted(judged):
        assert re.search(hook.matcher, tool), f"{hook.origin}: {hook.matcher!r} does not hand {tool!r}"
    assert alternatives(hook) == judged, f"{hook.origin}: the matcher lists {sorted(alternatives(hook))}"
    for tool in NOT_GUARDED:
        assert not re.search(hook.matcher, tool), f"{hook.origin}: {hook.matcher!r} hands {tool!r}"
        assert not re.search(hook.matcher, tool + "x") and not re.search(hook.matcher, "x" + tool)


def test_the_codex_matcher_hands_every_judged_tool_and_only_those(project: Project) -> None:
    hook = codex_hook(project)
    judged = copilot_guard.CODEX_PATCH_TOOLS | copilot_guard.CODEX_SHELL_TOOLS
    for tool in sorted(judged):
        assert re.search(hook.matcher, tool), f"{hook.origin}: {hook.matcher!r} does not hand {tool!r}"
    assert alternatives(hook) == judged, f"{hook.origin}: the matcher lists {sorted(alternatives(hook))}"
    for tool in NOT_GUARDED:
        assert not re.search(hook.matcher, tool), f"{hook.origin}: {hook.matcher!r} hands {tool!r}"


def test_the_matchers_are_anchored(project: Project) -> None:
    """A name that only contains a judged one is not handed over: ``Bashful`` is no ``Bash``."""
    for hook in (claude_hook(project), codex_hook(project)):
        assert hook.matcher.startswith("^") and hook.matcher.endswith("$"), hook
        for tool in alternatives(hook):
            assert not re.search(hook.matcher, tool + "ful") and not re.search(hook.matcher, "Pre" + tool)


def test_each_hook_names_its_own_dialect(project: Project) -> None:
    """The host is chosen by the command line, never sniffed: each file names its client's dialect."""
    assert shlex.split(claude_hook(project).command) == ["swcraft", "guard", "--host", "claude"]
    assert shlex.split(codex_hook(project).command) == ["swcraft", "guard", "--host", "codex"]
    assert shlex.split(kiro_hook(project).command) == ["swcraft", "guard", "--host", "kiro"]


def test_the_kiro_hook_hands_every_tool(project: Project) -> None:
    """Kiro's matcher is ``*``, so no call bypasses the allowlist; Kiro's own reading of ``*`` is not tested."""
    assert kiro_hook(project).matcher == "*"
