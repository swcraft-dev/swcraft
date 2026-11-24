"""Helpers and constants the ``swcraft guard`` test modules share: payloads, judges, projects and reasons.

The project fixtures (``project_dir`` and the ``*_dir`` of each dialect, ``user_home``, ``elsewhere``) and the
isolation every ``test_copilot_guard_*`` module runs under (``_copilot_guard_isolation``) are the suite's
``conftest.py``'s; they take the hook-file writers and the deadline from here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from swcraft_tools import cli, copilot_guard

#: A Codex judgement's deadline under the guard's tests: no run on any host reaches it.
UNREACHABLE_JUDGE_SECONDS = 3600.0


def write_codex_hook(root: Path) -> None:
    """Write the Codex hook file into *root*, which marks it as a project the Codex dialect guards."""
    (root / ".codex").mkdir()
    (root / ".codex" / "hooks.json").write_text("{}\n", encoding="utf-8", newline="\n")


def write_claude_settings(root: Path, name: str = "settings.json") -> None:
    """Write a Claude Code project settings file into *root*, marking it a project the Claude dialect guards."""
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / name).write_text("{}\n", encoding="utf-8", newline="\n")


def write_kiro_hooks(root: Path) -> None:
    """Write a Kiro hook file into *root*'s ``.kiro/hooks/``, which marks it as a project the Kiro dialect guards."""
    (root / ".kiro" / "hooks").mkdir(parents=True, exist_ok=True)
    (root / ".kiro" / "hooks" / "swcraft-guard.json").write_text("{}\n", encoding="utf-8", newline="\n")


#: The Codex deny of a run that failed unexpectedly, whole: the shape Codex reads and the reason.
CODEX_UNDECIDED_DENY = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "SWCraft: the model guard could not decide this call, so a file read or search, an edit or a shell "
            "command in this project is refused; use the SWCraft tools."
        ),
    }
}


MODEL_READ_REASON = (
    "SWCraft: the model under `model` is read through the SWCraft tools, never by file tools; a saved "
    "SWCraft answer may be viewed."
)


def guard_payload(tool: str, cwd: Path | None, **arguments: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool_name": tool, "tool_input": arguments, "hook_event_name": "PreToolUse"}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


def decide_payload(
    payload: dict[str, Any], working_directory: Path, guarded: copilot_guard.GuardedProject | None = None
) -> copilot_guard.Decision:
    """:func:`copilot_guard.decide` with the project read from the payload's directory, as ``run_hook`` reads it."""
    if guarded is None:
        guarded = copilot_guard.read_project(copilot_guard.payload_directory(payload, working_directory))
    return copilot_guard.decide(payload, working_directory, guarded)


def write_unreadable_manifest(directory: Path, text: str = "name: [unclosed\n") -> None:
    """Overwrite *directory*'s ``swcraft.yaml`` with *text*, by default YAML the manifest loader cannot parse."""
    (directory / "swcraft.yaml").write_text(text, encoding="utf-8", newline="\n")


#: The three ways a ``swcraft.yaml`` is unreadable to the guard: YAML that cannot be parsed, an empty file, and a
#: parsed document the manifest loader refuses (hook JSON without ``name``).
UNREADABLE_MANIFESTS = {"unparseable": "name: [unclosed\n", "empty": "", "invalid": '{"hooks": {}}\n'}


#: The clause every Codex and Claude Code denial under an unreadable manifest ends with: the agent never repairs
#: ``swcraft.yaml``, the user fixes it in the editor.
BY_HAND = "ask the user to fix swcraft.yaml by hand in the editor."


#: An extra argument nested past CPython's recursion limit, which V8's JSON parses.
DEEPLY_NESTED = b'{"x": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"


def saved_answer_name() -> str:
    return "1791840327146-copilot-tool-output-c782aa01ffee4c0d8a9b0e1f2a3b4c5d.txt"


#: The capability probe for drive letters: whether this host's native paths read ``C:/x`` as a drive at
#: all (a probe of the path flavour, never a platform name). Where they do, a drive-letter path
#: is judged by resolution; where they do not, it cannot be, and the guard denies it.
NATIVE_DRIVES = bool(Path("C:/x").drive)


def run_guard(
    stdin: bytes | None, monkeypatch, capsys, cwd: Path, main=cli.main, argv: tuple[str, ...] = ("guard",)
) -> tuple[str, str]:
    """*main* (``cli.main`` by default) with *stdin* as the payload's bytes, or with no stdin at all for ``None``."""

    class _Stdin:
        buffer = __import__("io").BytesIO(stdin or b"")

    monkeypatch.setattr("sys.stdin", None if stdin is None else _Stdin())
    monkeypatch.setattr("sys.argv", ["swcraft", *argv])
    monkeypatch.chdir(cwd)
    main()
    captured = capsys.readouterr()
    return captured.out, captured.err


SHELL_REASON = (
    "SWCraft: in this project the model directory (`model`) is read and changed through the SWCraft tools "
    "(`find`, `read`, `change_set`), whose changes the panel reviews; this command names it."
)


#: The guard inputs the write-judging dialects share, Kiro's three folders among them, named in every guard-input
#: reason; a folder holding one counts too.
GUARD_INPUT_REASON = (
    "SWCraft: this project's swcraft.yaml, .mcp.json and its .codex, .claude, .github/hooks, .kiro/hooks, "
    ".kiro/settings and .kiro/agents directories are the model guard's own inputs; they are edited by the user "
    "outside the agent, so this {kind}, which writes or names one of them, or a folder holding one, outside a read, "
    "is refused."
)


#: What every Codex reason for a call the guard cannot read holds, and what every budget reason holds.
UNJUDGED_MARK = "cannot be judged"


def codex_project(root: Path, model_root: str = "model") -> Path:
    """A project at *root* with *model_root* as its model root and the Codex hook file.

    The hook file makes the Codex dialect guard it."""
    from conftest import _write_manifest  # imported here: conftest imports this module for its fixtures

    data = {
        "name": "p",
        "schema_version": "1.0",
        "model_root": model_root,
        "dist_root": "dist",
        "views_root": "views",
        "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
    }
    _write_manifest(root, data)
    write_codex_hook(root)
    return root


def decide_codex_tool(tool: str, cwd: Path, **arguments: Any) -> copilot_guard.Decision:
    """:func:`copilot_guard.decide` under ``--host codex``, the project found as ``run_hook`` finds it."""
    payload = guard_payload(tool, cwd, **arguments)
    guarded = copilot_guard.guarded_project(copilot_guard.payload_directory(payload, cwd), "codex")
    return copilot_guard.decide(payload, cwd, guarded, "codex")


def codex_patch(*headers: str) -> str:
    body = "\n".join(f"{header}\n+x" for header in headers)
    return f"*** Begin Patch\n{body}\n*** End Patch\n"


def decide_codex_shell(cwd: Path, command: object, **extra: Any) -> copilot_guard.Decision:
    return decide_codex_tool("Bash", cwd, command=command, **extra)


CLAUDE_EDIT_REASON = (
    "SWCraft: in this project the model directory (`model`) is changed through the SWCraft tools, whose changes "
    "the panel reviews; this edit writes `{path}` there. Use the SWCraft MCP tool `change_set` instead."
)


HANDOFF_REASON = (
    "SWCraft: the SWCraft review hand-off directory (`<SWCraft home>/reviews`) belongs to the review tab; no agent "
    "tool reads or writes it, so this call is refused. A model change is reviewed through the SWCraft MCP tool "
    "`change_set`."
)


#: The guard inputs both dialects share, each as a project-relative POSIX name of a file.
SHARED_GUARD_INPUTS = [
    "swcraft.yaml",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/commands/x.md",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".mcp.json",
    ".github/hooks/swcraft.json",
    ".kiro/hooks/swcraft-guard.json",
    ".kiro/settings/mcp.json",
    ".kiro/agents/a.json",
]


def swcraft_home(user_home: Path) -> Path:
    """The SWCraft home under :func:`user_home`: ``<it>/.swcraft``, ``<it>/SWCraft`` on Windows; not created."""
    return user_home / ("SWCraft" if os.name == "nt" else ".swcraft")


def handoff_reviews(user_home: Path) -> Path:
    """The hand-off directory under :func:`user_home`, created as the server creates it."""
    reviews = swcraft_home(user_home) / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "x.delta.pending.json").write_text("{}\n", encoding="utf-8", newline="\n")
    return reviews


def decide_claude_tool(tool: str, cwd: Path, **arguments: Any) -> copilot_guard.Decision:
    """:func:`copilot_guard.decide_all` under ``--host claude``, every project found as ``run_hook`` finds them."""
    return decide_claude(guard_payload(tool, cwd, **arguments), cwd)


def decide_claude_bash(cwd: Path, command: object) -> copilot_guard.Decision:
    return decide_claude_tool("Bash", cwd, command=command, description="x")


def decide_claude(payload: dict[str, Any], cwd: Path) -> copilot_guard.Decision:
    projects = copilot_guard.guarded_projects(copilot_guard.payload_directory(payload, cwd), "claude")
    return copilot_guard.decide_all(payload, cwd, projects, "claude")


#: Every Kiro write tool with the key its path is under, built from the table, so a write tool added there is
#: covered by every test that takes this list.
KIRO_WRITE_TOOLS = [
    (tool, spec.path_keys[0]) for tool, spec in copilot_guard.KIRO_JUDGED_TOOLS.items() if spec.kind == "write"
]


def decide_kiro(payload: dict[str, Any], cwd: Path) -> copilot_guard.Decision:
    """:func:`copilot_guard.decide_all` under ``--host kiro``, every project found as ``run_hook`` finds them."""
    projects = copilot_guard.guarded_projects(copilot_guard.payload_directory(payload, cwd), "kiro")
    return copilot_guard.decide_all(payload, cwd, projects, "kiro")


def kiro_payload(tool: str, where: Path, /, **arguments: Any) -> dict[str, Any]:
    """:func:`guard_payload` for Kiro; positional-only, so a tool argument may be named ``cwd`` (``execute_bash``)."""
    return {"tool_name": tool, "tool_input": arguments, "hook_event_name": "PreToolUse", "cwd": str(where)}


def decide_kiro_tool(tool: str, where: Path, /, **arguments: Any) -> copilot_guard.Decision:
    return decide_kiro(kiro_payload(tool, where, **arguments), where)


def decide_kiro_bash(cwd: Path, command: object, workdir: object = None) -> copilot_guard.Decision:
    return decide_kiro_tool(
        "execute_bash", cwd, command=command, description=None, cwd=workdir, run_in_background=False, timeout=None
    )


def kiro_deny_line(err: str) -> str:
    """*err* as Kiro's one deny line: exactly one line, ``SWCraft:`` first.

    Its end, ``\\n`` or -- a child process's stderr on Windows -- ``\\r\\n``, is stripped."""
    assert err.endswith("\n") and err.count("\n") == 1, err
    assert err.startswith("SWCraft: "), err
    line = err[:-1]
    return line[:-1] if line.endswith("\r") else line


HOME_REASON = (
    "SWCraft: the SWCraft installation folder (`<SWCraft home>`) belongs to SWCraft; no agent tool writes or "
    "removes it, so this call is refused. The user updates or reinstalls SWCraft."
)


def from_home(user_home: Path, *parts: str) -> str:
    """The hand-off directory (or a place below it) written from the user's home with ``~/``.

    It is the SWCraft home's name under :func:`user_home` (``.swcraft``, ``SWCraft`` on Windows) and *parts*."""
    return "/".join(("~", swcraft_home(user_home).name, *parts))


GREP_ARGUMENTS = {"query": "x", "caseSensitive": None, "excludePattern": None, "explanation": "x"}


def decide_kiro_sub_agent(kiro_dir: Path, *paths: str) -> copilot_guard.Decision:
    return decide_kiro_tool(
        "invoke_sub_agent", kiro_dir, name="x", prompt="x", contextFiles=[{"path": path} for path in paths]
    )


#: The payloads Claude Code 2.1.319 sent its ``PreToolUse`` hook, captured and sanitised:
#: the project's path is ``/project``, which :func:`captured_payload` replaces with a test project's.
CLAUDE_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "claude_hook"


def captured_payload(name: str, root: Path, fixtures: Path = CLAUDE_FIXTURES) -> dict[str, Any]:
    """Return the captured payload *name* of *fixtures* with its ``/project`` paths placed at *root*."""

    def place(value: Any) -> Any:
        if isinstance(value, str) and (value == "/project" or value.startswith("/project/")):
            return str(root.joinpath(*value.split("/")[2:]))
        if isinstance(value, dict):
            return {key: place(member) for key, member in value.items()}
        return value

    return place(json.loads((fixtures / f"{name}.json").read_text(encoding="utf-8")))
