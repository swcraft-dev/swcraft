"""Tests for the swcraft-agent CLI: the REPL, ``--script`` replays, the scenario-turn forms and ``--scoped``.

Uses FakeLLMClient (no real Azure calls) and a real MCP client against a scaffolded project. Without
``--scoped`` no scope is proposed; whether a change set commits is the server's review boundary, not this host's.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml
from swcraft_tools import writer
from swcraft_tools.session import model_fingerprint

from tests.conftest import MODEL_SUBDIRS, FakeLLMClient, FakeMcp, seed_root_composition

_SCENARIOS_DIR = Path(__file__).resolve().parent / "fixtures" / "scenarios"

#: Every scenario in the repository; the files mix the plain-string and the mapping form.
_EXISTING_SCENARIOS = [
    "01-add-swc.yaml",
    "02-connect-ports.yaml",
    "03-incompatible-ports.yaml",
    "04-requirement-trace.yaml",
    "05-build-error-fix.yaml",
    "06-scoped-authorization.yaml",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_prompt(tmp_path: Path, monkeypatch) -> None:
    """Set SWCRAFT_AGENT_SYSTEM_PROMPT to a temp file."""
    p = tmp_path / "prompt.md"
    p.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(p))


def _make_script(tmp_path: Path, turns: list) -> Path:
    path = tmp_path / "script.yaml"
    path.write_text(yaml.safe_dump(turns), encoding="utf-8")
    return path


@pytest.fixture()
def resource_project(tmp_path: Path) -> Path:
    """A real project carrying one processor, one core and one memory; returns its manifest path."""
    root = tmp_path / "proj"
    model_root = root / "model"
    for subdir in MODEL_SUBDIRS:
        (model_root / subdir).mkdir(parents=True, exist_ok=True)
    (root / "dist").mkdir(parents=True, exist_ok=True)
    root_str = str(model_root)
    writer.create_hardware_element(root_str, "CPU", "processor")
    writer.create_hardware_element(root_str, "CORE0", "core", threshold=0.7)
    writer.create_hardware_element(
        root_str, "RAM0", "memory", memory_type="RAM", size=1024, accessible=["CORE0"], threshold=0.5
    )
    # Without a root, `composition.no-root` refuses the project for a reason no case here is about.
    seed_root_composition(root_str)
    manifest = root / "swcraft.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "name": "cli-test-project",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return manifest


# ---------------------------------------------------------------------------
# run_script() — unit tests (no subprocess, no CLI)
# ---------------------------------------------------------------------------


def test_run_script_returns_correct_pairs(scaffolded_project, tmp_path, monkeypatch):
    """run_script() returns (user_turn, agent_reply) pairs in order."""
    from swcraft_agent.cli import run_script
    from swcraft_agent.core import SwcraftAgent
    from swcraft_agent.mcp_client import SwcraftMcpClient

    _write_prompt(tmp_path, monkeypatch)
    fake = FakeLLMClient(["Reply A.", "Reply B."])
    script = _make_script(tmp_path, ["Turn 1", "Turn 2"])

    with SwcraftMcpClient(str(scaffolded_project)) as mcp:
        agent = SwcraftAgent(str(scaffolded_project), llm=fake, mcp=mcp)
        transcript = run_script(agent, str(script))

    assert transcript == [("Turn 1", "Reply A."), ("Turn 2", "Reply B.")]


def test_run_script_single_turn(scaffolded_project, tmp_path, monkeypatch):
    """run_script() works with a single-turn script."""
    from swcraft_agent.cli import run_script
    from swcraft_agent.core import SwcraftAgent
    from swcraft_agent.mcp_client import SwcraftMcpClient

    _write_prompt(tmp_path, monkeypatch)
    fake = FakeLLMClient(["Only reply."])
    script = _make_script(tmp_path, ["Only turn"])

    with SwcraftMcpClient(str(scaffolded_project)) as mcp:
        agent = SwcraftAgent(str(scaffolded_project), llm=fake, mcp=mcp)
        transcript = run_script(agent, str(script))

    assert transcript == [("Only turn", "Only reply.")]


def test_run_script_missing_file_raises(scaffolded_project, tmp_path, monkeypatch):
    """run_script() raises a clear error for a missing script file."""
    from swcraft_agent.cli import run_script
    from swcraft_agent.core import SwcraftAgent

    _write_prompt(tmp_path, monkeypatch)

    agent = SwcraftAgent(str(scaffolded_project), llm=FakeLLMClient([]), mcp=FakeMcp())

    with pytest.raises((FileNotFoundError, OSError)):
        run_script(agent, str(tmp_path / "nonexistent.yaml"))


def test_run_script_malformed_file_raises(scaffolded_project, tmp_path, monkeypatch):
    """run_script() raises a clear error when the script file is not a list of strings."""
    from swcraft_agent.cli import run_script
    from swcraft_agent.core import SwcraftAgent

    _write_prompt(tmp_path, monkeypatch)

    bad_script = tmp_path / "bad.yaml"
    bad_script.write_text("not_a_list: true", encoding="utf-8")  # dict, not list

    agent = SwcraftAgent(str(scaffolded_project), llm=FakeLLMClient([]), mcp=FakeMcp())

    with pytest.raises((ValueError, TypeError, AssertionError)):
        run_script(agent, str(bad_script))


def test_run_script_aborts_on_runtime_error_with_turn_context(scaffolded_project, tmp_path, monkeypatch):
    """run_script() stops at a failing turn and names it in the error.

    Continuing would replay the rest of the script against a broken transcript.
    """
    from swcraft_agent.cli import run_script
    from swcraft_agent.core import SwcraftAgent

    _write_prompt(tmp_path, monkeypatch)
    script = _make_script(tmp_path, ["Turn 1", "Turn 2"])

    # Program covers turn 1 only — turn 2 exhausts the FakeLLMClient program,
    # which raises RuntimeError just like the MAX_TOOL_ROUNDS backstop.
    agent = SwcraftAgent(str(scaffolded_project), llm=FakeLLMClient(["Reply A."]), mcp=FakeMcp())

    with pytest.raises(RuntimeError, match=r"aborted at turn 1 \('Turn 2'\)"):
        run_script(agent, str(script))


# ---------------------------------------------------------------------------
# --script mode via CLI (monkeypatch sys.argv)
# ---------------------------------------------------------------------------


def test_cli_script_mode_prints_transcript(scaffolded_project, tmp_path, monkeypatch, capsys):
    """swcraft-agent chat --project PATH --script FILE prints transcript to stdout."""
    from swcraft_agent import cli as cli_mod

    _write_prompt(tmp_path, monkeypatch)
    script = _make_script(tmp_path, ["Hello agent."])

    # Inject FakeLLMClient so no real Azure calls
    import swcraft_agent.core as _core_mod

    monkeypatch.setattr(
        _core_mod,
        "AzureOpenAIClient",
        lambda: FakeLLMClient(["Hi there!"]),
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["swcraft-agent", "chat", "--project", str(scaffolded_project), "--script", str(script)],
    )

    cli_mod.main()

    captured = capsys.readouterr()
    assert "Hello agent." in captured.out
    assert "Hi there!" in captured.out


# ---------------------------------------------------------------------------
# Interactive REPL mode via CLI (monkeypatch sys.argv + stdin)
# ---------------------------------------------------------------------------


def test_cli_repl_mode_processes_stdin_and_exits(scaffolded_project, tmp_path, monkeypatch, capsys):
    """swcraft-agent chat --project PATH reads stdin lines and exits cleanly on EOF."""
    import swcraft_agent.core as _core_mod
    from swcraft_agent import cli as cli_mod

    _write_prompt(tmp_path, monkeypatch)

    monkeypatch.setattr(
        _core_mod,
        "AzureOpenAIClient",
        lambda: FakeLLMClient(["REPL reply."]),
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["swcraft-agent", "chat", "--project", str(scaffolded_project)],
    )

    # Simulate piped stdin with one line then EOF
    monkeypatch.setattr(sys, "stdin", io.StringIO("What can you do?\n"))

    cli_mod.main()

    captured = capsys.readouterr()
    assert "REPL reply." in captured.out
    # A REPL run reviews in VS Code, so the terminal says so before the first wait.
    assert cli_mod.PANEL_REVIEW_HINT in captured.out


def test_the_panel_review_hint_states_the_servers_wait() -> None:
    # The server's panel wait has no deadline, so the hint names no time limit that could drift from it.
    from swcraft_agent import cli as cli_mod

    hint = cli_mod.PANEL_REVIEW_HINT
    assert "waits until you answer it there" in hint
    assert "Ctrl-C ends the agent and withdraws it" in hint
    assert "minutes" not in hint


# ---------------------------------------------------------------------------
# The scenario format
#
# A turn is a plain string or a `{user, approvals}` mapping. Anything else is rejected with the turn's index: a
# silently dropped key would be an approval the replay never consumed.
# ---------------------------------------------------------------------------


def test_load_script_accepts_plain_string_turns(tmp_path):
    """The plain form: a list of strings, each turn approving nothing."""
    from swcraft_agent.cli import load_script

    script = _make_script(tmp_path, ["Turn 1", "Turn 2"])
    turns = load_script(str(script))

    assert [turn.user for turn in turns] == ["Turn 1", "Turn 2"]
    assert [turn.approvals for turn in turns] == [(), ()]


def test_load_script_accepts_a_mapping_turn_with_approvals(tmp_path):
    """`{user, approvals}` carries the answers this turn's prompts consume."""
    from swcraft_agent.cli import load_script

    script = _make_script(tmp_path, [{"user": "Add a port.", "approvals": ["y", "y"]}])
    turns = load_script(str(script))

    assert turns[0].user == "Add a port."
    assert turns[0].approvals == ("y", "y")


def test_load_script_accepts_a_mapping_turn_without_approvals(tmp_path):
    """`approvals` is optional; omitting it means the turn approves nothing."""
    from swcraft_agent.cli import load_script

    script = _make_script(tmp_path, [{"user": "What SWCs exist?"}])
    turns = load_script(str(script))

    assert turns[0].user == "What SWCs exist?"
    assert turns[0].approvals == ()


def test_load_script_accepts_a_mix_of_both_forms(tmp_path):
    from swcraft_agent.cli import load_script

    script = _make_script(tmp_path, ["Plain turn", {"user": "Mapping turn", "approvals": ["y"]}])
    turns = load_script(str(script))

    assert [turn.user for turn in turns] == ["Plain turn", "Mapping turn"]
    assert [turn.approvals for turn in turns] == [(), ("y",)]


def test_load_script_rejects_an_unknown_mapping_key(tmp_path):
    from swcraft_agent.cli import load_script

    script = _make_script(tmp_path, [{"user": "Hi", "aprovals": ["y"]}])

    with pytest.raises(ValueError, match=r"turn 0.*aprovals"):
        load_script(str(script))


def test_load_script_rejects_a_mapping_without_user(tmp_path):
    from swcraft_agent.cli import load_script

    script = _make_script(tmp_path, [{"approvals": ["y"]}])

    with pytest.raises(ValueError, match=r"turn 0.*'user'"):
        load_script(str(script))


def test_load_script_rejects_a_non_string_user(tmp_path):
    from swcraft_agent.cli import load_script

    script = _make_script(tmp_path, [{"user": 42}])

    with pytest.raises(TypeError, match=r"turn 0.*must be a string"):
        load_script(str(script))


def test_load_script_rejects_a_non_list_approvals(tmp_path):
    from swcraft_agent.cli import load_script

    script = _make_script(tmp_path, [{"user": "Hi", "approvals": "y"}])

    with pytest.raises(TypeError, match=r"turn 0.*approvals"):
        load_script(str(script))


def test_load_script_rejects_a_non_string_approval(tmp_path):
    """A bare YAML ``yes`` parses as a bool — that is a scenario-file error."""
    from swcraft_agent.cli import load_script

    script = tmp_path / "script.yaml"
    script.write_text("- user: Hi\n  approvals: [yes]\n", encoding="utf-8")

    with pytest.raises(TypeError, match=r"turn 0.*approval 0"):
        load_script(str(script))


def test_load_script_rejects_a_turn_that_is_neither_form(tmp_path):
    from swcraft_agent.cli import load_script

    script = _make_script(tmp_path, ["ok", [1, 2]])

    with pytest.raises(TypeError, match=r"turn 1"):
        load_script(str(script))


def test_load_script_rejects_a_non_list_document(tmp_path):
    from swcraft_agent.cli import load_script

    script = tmp_path / "script.yaml"
    script.write_text("not_a_list: true\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"YAML list"):
        load_script(str(script))


def test_load_script_missing_file_raises(tmp_path):
    from swcraft_agent.cli import load_script

    with pytest.raises(FileNotFoundError):
        load_script(str(tmp_path / "nope.yaml"))


# ---------------------------------------------------------------------------
# run_script() installs each turn's approvals
# ---------------------------------------------------------------------------


class _RecordingAgent:
    """An agent stand-in that records the approver in force for each turn."""

    def __init__(self, asks_per_turn: int = 0) -> None:
        self.approver = None
        self.turns: list[tuple[str, object]] = []
        self._asks_per_turn = asks_per_turn

    def chat(self, message: str) -> str:
        self.turns.append((message, self.approver))
        for _ in range(self._asks_per_turn):
            self.approver.ask("approve? [y/N]: ")
        return f"reply to {message}"


def test_run_script_installs_a_scripted_approver_carrying_the_turns_answers(tmp_path):
    """Each turn gets its own approver, holding exactly that turn's answers."""
    from swcraft_agent.approval import ScriptedApprover
    from swcraft_agent.cli import run_script

    script = _make_script(
        tmp_path,
        [{"user": "First", "approvals": ["y", "y"]}, "Second"],
    )
    agent = _RecordingAgent()

    transcript = run_script(agent, str(script))

    assert transcript == [("First", "reply to First"), ("Second", "reply to Second")]
    first, second = (approver for _, approver in agent.turns)
    assert isinstance(first, ScriptedApprover) and isinstance(second, ScriptedApprover)
    assert first.remaining == 2
    assert second.remaining == 0, "a turn with no approvals approves nothing"


def test_run_script_does_not_carry_unused_answers_into_the_next_turn(tmp_path):
    """A fresh approver per turn: an unconsumed `y` cannot leak forward."""
    from swcraft_agent.cli import run_script

    script = _make_script(tmp_path, [{"user": "First", "approvals": ["y", "y"]}, "Second"])
    agent = _RecordingAgent(asks_per_turn=1)

    run_script(agent, str(script))

    approvals = [approver.answers_given for _, approver in agent.turns]
    assert approvals == [["y"], [None]], "turn 2 must find no leftover answer"


def test_run_script_consumes_answers_in_order(tmp_path):
    from swcraft_agent.cli import run_script

    script = _make_script(tmp_path, [{"user": "First", "approvals": ["y", "n"]}])
    agent = _RecordingAgent(asks_per_turn=2)

    run_script(agent, str(script))

    approver = agent.turns[0][1]
    assert approver.answers_given == ["y", "n"]


# ---------------------------------------------------------------------------
# --scoped
# ---------------------------------------------------------------------------


class _AgentRecorder:
    """Captures the constructor arguments ``_cmd_chat`` builds an agent with."""

    instances: list = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.approver = kwargs.get("approver")
        _AgentRecorder.instances.append(self)

    def chat(self, message: str) -> str:
        return f"reply to {message}"


@pytest.fixture()
def agent_recorder(monkeypatch):
    import swcraft_agent.core as _core_mod

    _AgentRecorder.instances = []
    monkeypatch.setattr(_core_mod, "SwcraftAgent", _AgentRecorder)
    return _AgentRecorder


def test_scoped_flag_defaults_to_off(scaffolded_project):
    from swcraft_agent.cli import _build_parser

    args = _build_parser().parse_args(["chat", "--project", str(scaffolded_project)])

    assert args.scoped is False


def test_scoped_flag_parses(scaffolded_project):
    from swcraft_agent.cli import _build_parser

    args = _build_parser().parse_args(["chat", "--project", str(scaffolded_project), "--scoped"])

    assert args.scoped is True


def test_cli_without_scoped_builds_an_unscoped_agent_with_a_stdin_approver(
    scaffolded_project, tmp_path, monkeypatch, agent_recorder, capsys
):
    """Absent flag -> today's behavior: scoped=False."""
    from swcraft_agent import cli as cli_mod
    from swcraft_agent.approval import StdinApprover

    _write_prompt(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["swcraft-agent", "chat", "--project", str(scaffolded_project)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))

    cli_mod.main()

    agent = agent_recorder.instances[0]
    assert agent.kwargs["scoped"] is False
    assert isinstance(agent.kwargs["approver"], StdinApprover)
    assert "reply to hello" in capsys.readouterr().out


def test_cli_scoped_repl_mode_uses_a_stdin_approver(scaffolded_project, tmp_path, monkeypatch, agent_recorder, capsys):
    from swcraft_agent import cli as cli_mod
    from swcraft_agent.approval import StdinApprover

    _write_prompt(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["swcraft-agent", "chat", "--project", str(scaffolded_project), "--scoped"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))

    cli_mod.main()

    agent = agent_recorder.instances[0]
    assert agent.kwargs["scoped"] is True
    assert isinstance(agent.kwargs["approver"], StdinApprover)
    capsys.readouterr()


def test_cli_scoped_script_mode_uses_a_scripted_approver(
    scaffolded_project, tmp_path, monkeypatch, agent_recorder, capsys
):
    """A scripted replay must never block on stdin — the answers come from the file."""
    from swcraft_agent import cli as cli_mod
    from swcraft_agent.approval import ScriptedApprover

    _write_prompt(tmp_path, monkeypatch)
    script = _make_script(tmp_path, [{"user": "Add a SWC.", "approvals": ["y", "y"]}])
    monkeypatch.setattr(
        sys,
        "argv",
        ["swcraft-agent", "chat", "--project", str(scaffolded_project), "--script", str(script), "--scoped"],
    )

    cli_mod.main()

    agent = agent_recorder.instances[0]
    assert agent.kwargs["scoped"] is True
    assert isinstance(agent.kwargs["approver"], ScriptedApprover)
    # run_script replaced it with the turn's own approver before dispatching.
    assert isinstance(agent.approver, ScriptedApprover)
    assert agent.approver.remaining == 2
    capsys.readouterr()


def test_cli_scoped_hands_the_agent_no_long_lived_client(scaffolded_project, tmp_path, monkeypatch, agent_recorder):
    """A scoped run owns a client **per turn**, so the CLI starts none of its own.

    A scope approved mid-conversation cannot reach a server spawned before it.
    """
    from swcraft_agent import cli as cli_mod

    _write_prompt(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["swcraft-agent", "chat", "--project", str(scaffolded_project), "--scoped"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))

    cli_mod.main()

    assert agent_recorder.instances[0].kwargs.get("mcp") is None


@pytest.mark.parametrize("name", ["SWCRAFT_MCP_BUILD_COMMAND", "SWCRAFT_MCP_VALIDATE_COMMAND"])
def test_cli_pins_the_escape_vars_on_every_run(scaffolded_project, tmp_path, monkeypatch, agent_recorder, name):
    """Every server this CLI spawns is pinned — scoped or not.

    The spawned server's ``load_dotenv(override=False)`` fills exactly the absent keys, so an absent escape
    variable could come from a project ``.env`` behind a gate that already answered "no override".
    """
    from swcraft_agent import cli as cli_mod

    monkeypatch.delenv(name, raising=False)
    _write_prompt(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["swcraft-agent", "chat", "--project", str(scaffolded_project)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))

    cli_mod.main()

    server_env = agent_recorder.instances[0].kwargs["mcp"].server_env
    assert name in server_env, f"{name} must be PRESENT, or the child's own .env can add it"
    assert server_env[name] == ""


def test_cli_keeps_a_configured_override_visible_to_the_gate(scaffolded_project, tmp_path, monkeypatch, agent_recorder):
    """Pinning is not neutralizing: a real override stays visible, so the gate can refuse."""
    from swcraft_agent import cli as cli_mod

    monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", "moon run :build")
    _write_prompt(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["swcraft-agent", "chat", "--project", str(scaffolded_project)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))

    cli_mod.main()

    server_env = agent_recorder.instances[0].kwargs["mcp"].server_env
    assert server_env["SWCRAFT_MCP_BUILD_COMMAND"] == "moon run :build"


@pytest.mark.parametrize(
    ("scripted", "expected"),
    [(False, "panel"), (True, "elicit")],
    ids=["repl-panel", "script-elicit"],
)
def test_cli_spawns_its_unscoped_client_in_a_reviewing_mode(
    scaffolded_project, tmp_path, monkeypatch, agent_recorder, scripted, expected
):
    """The review boundary is a property of the server's mode, not of the scope.

    The unscoped client carries a reviewing ``SWCRAFT_REVIEW``: ``panel`` for a REPL run, ``elicit`` for a
    ``--script`` replay. Both scope keys go down present and empty (unset to the server; a popped key could be
    filled from a project ``.env``, and ``[]`` would be a scope approving nothing).
    """
    import swcraft_agent.core as _core_mod
    from swcraft_agent import cli as cli_mod

    _write_prompt(tmp_path, monkeypatch)
    argv = ["swcraft-agent", "chat", "--project", str(scaffolded_project)]
    if scripted:
        argv += ["--script", str(_make_script(tmp_path, ["hello"]))]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))

    cli_mod.main()

    agent = agent_recorder.instances[0]
    client = agent.kwargs["mcp"]
    assert client.server_env[_core_mod.REVIEW_ENV] == expected
    assert agent.kwargs["review"] == expected
    assert client.server_env[_core_mod.APPROVED_SCOPE_ENV] == ""
    assert client.server_env[_core_mod.SCOPE_ID_ENV] == ""
    # The client answers the server's review elicitation (a replay's) through
    # the same approver the agent was built with.
    assert client.approver is agent.kwargs["approver"]


@pytest.mark.parametrize(("scripted", "expected"), [(False, "panel"), (True, "elicit")], ids=["repl", "script"])
def test_cli_hands_a_scoped_agent_the_mode_its_per_turn_servers_carry(
    scaffolded_project, tmp_path, monkeypatch, agent_recorder, scripted, expected, capsys
):
    """The CLI hands a scoped run the same review mode an unscoped run's client carries.

    A scoped run spawns its servers per turn inside the agent.
    """
    from swcraft_agent import cli as cli_mod

    _write_prompt(tmp_path, monkeypatch)
    argv = ["swcraft-agent", "chat", "--project", str(scaffolded_project), "--scoped"]
    if scripted:
        argv += ["--script", str(_make_script(tmp_path, ["hello"]))]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))

    cli_mod.main()

    assert agent_recorder.instances[0].kwargs["review"] == expected
    capsys.readouterr()


def test_cli_without_scoped_proposes_no_scope(scaffolded_project, tmp_path, monkeypatch, capsys):
    """No ``--scoped`` -> no structured proposal call at all.

    The double raises when an unqueued structured round is asked for, so a proposal would fail the run.
    """
    import swcraft_agent.core as _core_mod
    from swcraft_agent import cli as cli_mod

    _write_prompt(tmp_path, monkeypatch)
    fake = FakeLLMClient(["Hi there!"], structured=[])
    monkeypatch.setattr(_core_mod, "AzureOpenAIClient", lambda: fake)
    script = _make_script(tmp_path, ["Hello agent."])
    monkeypatch.setattr(
        sys,
        "argv",
        ["swcraft-agent", "chat", "--project", str(scaffolded_project), "--script", str(script)],
    )

    cli_mod.main()

    assert fake.structured_calls == [], "an unscoped turn must not ask the LLM for a scope"
    assert "Hi there!" in capsys.readouterr().out


def test_cli_scoped_proposes_a_scope(scaffolded_project, tmp_path, monkeypatch, capsys):
    """With the flag, the turn starts with the tool-free proposal call."""
    import swcraft_agent.core as _core_mod
    from swcraft_agent import cli as cli_mod

    _write_prompt(tmp_path, monkeypatch)
    fake = FakeLLMClient(["Nothing to change."], structured=[{"summary": "Just a question", "entries": []}])
    monkeypatch.setattr(_core_mod, "AzureOpenAIClient", lambda: fake)
    script = _make_script(tmp_path, ["What SWCs exist?"])
    monkeypatch.setattr(
        sys,
        "argv",
        ["swcraft-agent", "chat", "--project", str(scaffolded_project), "--script", str(script), "--scoped"],
    )

    cli_mod.main()

    assert len(fake.structured_calls) == 1
    assert "Nothing to change." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The safety guard is not this host's
# ---------------------------------------------------------------------------
#
# The weakened-check question is the server's second elicitation (tested in the mcp suite) and is answered
# through `Approver.ask`. What is left to test here is that an ordinary write is not gated by this host.


def test_an_ordinary_write_is_not_gated_without_scoped(resource_project, tmp_path, monkeypatch):
    """No scope, no host prompt, no session: an unscoped turn reaches the server.

    `SWCRAFT_REVIEW` is unset here, so the change set stops at `staged` and the model is byte-identical: the
    host adds no gate of its own; the review boundary is the server's.
    """
    from swcraft_agent.cli import run_script
    from swcraft_agent.core import SwcraftAgent
    from swcraft_agent.mcp_client import SwcraftMcpClient

    _write_prompt(tmp_path, monkeypatch)
    model_root = resource_project.parent / "model"
    script = _make_script(tmp_path, ["Add a uint8 base type."])

    with SwcraftMcpClient(str(resource_project)) as mcp:
        # A change set cannot be stated without a baseline.
        baseline = json.loads(mcp.call_tool("discover", {}))["baseline"]
        fake = FakeLLMClient(
            [
                [
                    (
                        "change_set",
                        {
                            "baseline": baseline,
                            "intent": "Add the uint8 base type.",
                            "operations": [
                                {
                                    "operation": "create",
                                    "address": {"layer": "base_types", "element": "uint8"},
                                    "value": {
                                        "name": "uint8",
                                        "kind": "base_type",
                                        "size": 8,
                                        "encoding": "NONE",
                                        "native_declaration": "uint8_t",
                                    },
                                }
                            ],
                        },
                    )
                ],
                "Staged.",
            ]
        )
        agent = SwcraftAgent(str(resource_project), llm=fake, mcp=mcp, scoped=False)
        run_script(agent, str(script))
        approver = agent.approver

    assert fake.structured_calls == [], "still no scope proposal without --scoped"
    assert not fake.results[-1].startswith("REFUSED:"), fake.results[-1]
    assert json.loads(fake.results[-1])["status"] == "staged", fake.results[-1]
    assert approver.prompts == [], "the host asks nothing; the review boundary is the server's"
    assert not (model_root / "data_types" / "base" / "uint8.yaml").exists(), "a staged change set writes nothing"


# ---------------------------------------------------------------------------
# Both turn forms, read off the real scenario files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", _EXISTING_SCENARIOS)
def test_existing_scenarios_load_under_the_extended_loader(filename):
    """Every real scenario turn loads as itself, in whichever form it is written.

    A plain string approves nothing; a ``{user, approvals}`` mapping carries that turn's answers. The count of
    answers is pinned in ``test_scenario_files.py``.
    """
    from swcraft_agent.cli import load_script

    path = _SCENARIOS_DIR / filename
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    turns = load_script(str(path))

    assert len(turns) == len(raw)
    for turn, entry in zip(turns, raw, strict=True):
        if isinstance(entry, str):
            assert turn.user == entry
            assert turn.approvals == (), "a plain-string turn approves nothing"
        else:
            assert turn.user == entry["user"]
            assert turn.approvals == tuple(entry.get("approvals", ()))


def test_a_scenario_turn_without_approvals_commits_nothing_under_scoped(scaffolded_project, tmp_path, monkeypatch):
    """Scoped replay of a plain-string turn: the scope is declined, so no write lands."""
    from swcraft_agent.cli import run_script
    from swcraft_agent.core import SwcraftAgent

    _write_prompt(tmp_path, monkeypatch)
    model_root = str(scaffolded_project.parent / "model")
    before = model_fingerprint(model_root)
    fake = FakeLLMClient(
        [
            [
                (
                    "change_set",
                    {
                        "baseline": "rev1:whatever",
                        "intent": "Add the SWC BrakeController",
                        "operations": [
                            {
                                "operation": "create",
                                "address": {"layer": "swc", "element": "BrakeController"},
                                "value": {
                                    "name": "BrakeController",
                                    "group": "chassis",
                                    "type": "application",
                                    "handle_termination_and_restart": "no_support",
                                },
                            }
                        ],
                    },
                )
            ],
            "I could not do that.",
        ],
        structured=[
            {
                "summary": "Add the SWC BrakeController",
                "entries": [
                    {"operation": "create", "address": {"layer": "swc", "element": "BrakeController"}},
                ],
            }
        ],
    )
    script = _make_script(tmp_path, ["Add a SWC called BrakeController."])

    # No client is injected: a scoped run spawns the turn's own server, carrying
    # the approved scope — here `[]`, because nothing was approved.
    agent = SwcraftAgent(str(scaffolded_project), llm=fake, scoped=True, review="elicit")
    run_script(agent, str(script))
    approver = agent.approver

    assert approver.answers_given == [None], "the scope prompt found no scripted answer"
    response = json.loads(fake.results[0])
    assert response["response"] == "error"
    assert response["error"] == "unauthorized-delta", response
    assert model_fingerprint(model_root) == before
    assert not (scaffolded_project.parent / "model" / "swc" / "chassis" / "BrakeController.yaml").exists()
