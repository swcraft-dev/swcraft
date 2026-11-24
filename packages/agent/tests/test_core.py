"""Tests for the SwcraftAgent core loop and its turn guard.

Uses FakeLLMClient (from conftest) and a real SwcraftMcpClient against a
scaffolded tmp_path project; no real Azure OpenAI calls.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import swcraft_agent.core as _core

from tests.conftest import FakeLLMClient, FakeMcp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(scaffolded_project, fake_llm, tmp_path, monkeypatch):
    """Build a SwcraftAgent with injected FakeLLMClient and a real MCP client."""
    from swcraft_agent.core import SwcraftAgent
    from swcraft_agent.mcp_client import SwcraftMcpClient

    prompt_file = tmp_path / "system_prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    mcp = SwcraftMcpClient(str(scaffolded_project))
    agent = SwcraftAgent(project_path=str(scaffolded_project), llm=fake_llm, mcp=mcp)
    return agent, mcp


# ---------------------------------------------------------------------------
# Single-turn chat with no tool calls
# ---------------------------------------------------------------------------


def test_chat_single_turn_no_tools(scaffolded_project, tmp_path, monkeypatch):
    """chat() returns the LLM response when no tool calls are made."""
    fake = FakeLLMClient(["Hello from the agent!"])
    agent, mcp = _make_agent(scaffolded_project, fake, tmp_path, monkeypatch)
    try:
        reply = agent.chat("What can you do?")
        assert reply == "Hello from the agent!"
    finally:
        mcp.close()


# ---------------------------------------------------------------------------
# Multi-round chat with tool calls
# ---------------------------------------------------------------------------


def test_chat_triggers_tool_call_and_returns_text(scaffolded_project, tmp_path, monkeypatch):
    """chat() executes tool calls via MCP and returns the final text response.

    The scripted call is a ``change_set``, the one mutation surface, not a removed element-bound tool.
    """
    call = (
        "change_set",
        {
            "baseline": "rev1:0000",
            "operations": [
                {
                    "operation": "create",
                    "address": {"layer": "swc", "element": "CoreSWC"},
                    "value": {
                        "name": "CoreSWC",
                        "group": "core",
                        "type": "application",
                        "handle_termination_and_restart": "no_support",
                    },
                }
            ],
            "intent": "Add a new SWC called CoreSWC.",
        },
    )
    fake = FakeLLMClient([[call], "Added CoreSWC successfully."])
    agent, mcp = _make_agent(scaffolded_project, fake, tmp_path, monkeypatch)
    try:
        reply = agent.chat("Add a new SWC called CoreSWC.")
        assert reply == "Added CoreSWC successfully."
        assert fake.calls == [call]
    finally:
        mcp.close()


# ---------------------------------------------------------------------------
# Conversation accumulates turns across multiple chat() calls
# ---------------------------------------------------------------------------


def test_conversation_accumulates_turns(scaffolded_project, tmp_path, monkeypatch):
    """ConversationManager accumulates user and assistant turns across multiple chat() calls."""
    fake = FakeLLMClient(["First reply.", "Second reply."])
    agent, mcp = _make_agent(scaffolded_project, fake, tmp_path, monkeypatch)
    try:
        agent.chat("First question.")
        agent.chat("Second question.")
        msgs = agent._conversation.messages
        roles = [m["role"] for m in msgs]
        contents = [m["content"] for m in msgs]
        assert roles == ["system", "user", "assistant", "user", "assistant"]
        assert "First question." in contents
        assert "First reply." in contents
        assert "Second question." in contents
        assert "Second reply." in contents
    finally:
        mcp.close()


# ---------------------------------------------------------------------------
# Aborted turn (e.g. MAX_TOOL_ROUNDS backstop) keeps history turn-balanced
# ---------------------------------------------------------------------------


def test_chat_runtime_error_keeps_history_balanced(scaffolded_project, tmp_path, monkeypatch):
    """A RuntimeError from the tool-use loop re-raises, and the failed turn is closed with an abort note.

    The assistant-side note keeps the next chat() call from silently re-presenting
    the aborted request as a dangling user message.
    """
    from swcraft_agent.core import SwcraftAgent

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    class _FailThenRecoverLLM:
        """Raises the backstop error on the first complete(), then succeeds."""

        def __init__(self):
            self.completions = 0

        def complete(self, messages, tools, tool_executor):
            self.completions += 1
            if self.completions == 1:
                raise RuntimeError("Exceeded maximum tool-use rounds (test)")
            return "recovered"

    agent = SwcraftAgent(
        project_path=str(scaffolded_project),
        llm=_FailThenRecoverLLM(),
        mcp=FakeMcp(),
    )

    with pytest.raises(RuntimeError, match="Exceeded maximum tool-use rounds"):
        agent.chat("Trigger the backstop.")

    msgs = agent._conversation.messages
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert "turn aborted" in msgs[-1]["content"]
    assert "Exceeded maximum tool-use rounds" in msgs[-1]["content"]

    assert agent.chat("Try again.") == "recovered"
    roles = [m["role"] for m in agent._conversation.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]


def test_an_interrupt_at_a_review_prompt_leaves_the_history_balanced(scaffolded_project):
    """A `KeyboardInterrupt` at a review prompt closes the turn with an abort note.

    Without the note the next `chat()` would re-present the abandoned request to the model as if it were open.
    """

    from swcraft_agent.core import SwcraftAgent

    class _InterruptedLLM:
        """Interrupts on the first turn — the user walking away — then recovers."""

        def __init__(self):
            self.completions = 0

        def complete(self, messages, tools, tool_executor):
            self.completions += 1
            if self.completions == 1:
                raise KeyboardInterrupt
            return "recovered"

    agent = SwcraftAgent(
        project_path=str(scaffolded_project),
        llm=_InterruptedLLM(),
        mcp=FakeMcp(),
    )

    # Re-raised, never swallowed: the host's `with` block is what closes the
    # client and takes the subprocess down.
    with pytest.raises(KeyboardInterrupt):
        agent.chat("Rename the SWC.")

    msgs = agent._conversation.messages
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert "turn aborted" in msgs[-1]["content"]

    assert agent.chat("Try again.") == "recovered"
    assert [m["role"] for m in agent._conversation.messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
    ]


# ---------------------------------------------------------------------------
# Missing system prompt raises a clear error
# ---------------------------------------------------------------------------


def test_missing_system_prompt_raises(scaffolded_project, monkeypatch):
    """_load_system_prompt() raises a clear error when the prompt file cannot be found."""
    from swcraft_agent.core import SwcraftAgent

    monkeypatch.delenv("SWCRAFT_AGENT_SYSTEM_PROMPT", raising=False)

    # The packaged prompt is tracked source and always present in a checkout, so the
    # one discovery is made to fail.
    import swcraft_agent.core as _core_mod

    monkeypatch.setattr(_core_mod, "_packaged_system_prompt_path", lambda: None)

    with pytest.raises((FileNotFoundError, OSError, RuntimeError)):
        SwcraftAgent(
            project_path=str(scaffolded_project),
            llm=FakeLLMClient([]),
            mcp=FakeMcp(),
        )


# ---------------------------------------------------------------------------
# System prompt loaded from env var override
# ---------------------------------------------------------------------------


def test_system_prompt_env_var_override(scaffolded_project, tmp_path, monkeypatch):
    """SWCRAFT_AGENT_SYSTEM_PROMPT env var is used when set."""
    from swcraft_agent.core import SwcraftAgent

    prompt_file = tmp_path / "custom_prompt.md"
    prompt_file.write_text("Custom system prompt.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    agent = SwcraftAgent(
        project_path=str(scaffolded_project),
        llm=FakeLLMClient(["ok"]),
        mcp=FakeMcp(),
    )
    assert "Custom system prompt." in agent._conversation.messages[0]["content"]


# ---------------------------------------------------------------------------
# Self-correction retry limit: env-configurable with safe fallback
# ---------------------------------------------------------------------------


class TestMaxBuildRetries:
    """_max_build_retries() parses SWCRAFT_AGENT_MAX_BUILD_RETRIES safely."""

    def test_default_when_unset(self, monkeypatch):
        """Returns the documented default (3) when the env var is unset."""
        from swcraft_agent.core import _max_build_retries

        monkeypatch.delenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", raising=False)
        assert _max_build_retries() == 3

    def test_override_with_valid_integer(self, monkeypatch):
        """Returns the overridden int when the env var is a valid in-range integer."""
        from swcraft_agent.core import MAX_BUILD_RETRIES_CAP, _max_build_retries

        assert 2 <= MAX_BUILD_RETRIES_CAP, "test assumes 2 is within the clamp"
        monkeypatch.setenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", "2")
        assert _max_build_retries() == 2

    def test_zero_is_valid_no_retries(self, monkeypatch):
        """Zero is a legitimate value meaning 'no retries' — not a fallback case."""
        from swcraft_agent.core import _max_build_retries

        monkeypatch.setenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", "0")
        assert _max_build_retries() == 0

    def test_fallback_on_negative(self, monkeypatch):
        """Negative values are invalid and fall back to the default (3)."""
        from swcraft_agent.core import _max_build_retries

        monkeypatch.setenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", "-1")
        assert _max_build_retries() == 3

    def test_clamped_to_fit_max_tool_rounds(self, monkeypatch):
        """Values above MAX_BUILD_RETRIES_CAP are clamped.

        The give-up path stays reachable inside the MAX_TOOL_ROUNDS hard backstop.
        """
        from swcraft_agent.core import MAX_BUILD_RETRIES_CAP, _max_build_retries

        monkeypatch.setenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", "99")
        assert _max_build_retries() == MAX_BUILD_RETRIES_CAP

    def test_fallback_on_non_numeric(self, monkeypatch):
        """Falls back to the default (3) when the env var is non-numeric."""
        from swcraft_agent.core import _max_build_retries

        monkeypatch.setenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", "abc")
        assert _max_build_retries() == 3

    def test_fallback_on_empty_string(self, monkeypatch):
        """Falls back to the default (3) when the env var is empty/whitespace."""
        from swcraft_agent.core import _max_build_retries

        monkeypatch.setenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", "   ")
        assert _max_build_retries() == 3


# ---------------------------------------------------------------------------
# System prompt placeholder substitution
# ---------------------------------------------------------------------------


class TestSystemPromptSubstitution:
    """_load_system_prompt() substitutes the {{MAX_BUILD_RETRIES}} placeholder."""

    def _repo_prompt_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "src" / "swcraft_agent" / "agent-system-prompt.md"

    def test_real_prompt_has_no_placeholder_left(self, scaffolded_project, monkeypatch):
        """Loading the real swcraft_agent/agent-system-prompt.md leaves no literal token."""
        from swcraft_agent.core import SwcraftAgent

        real_prompt = self._repo_prompt_path()
        assert real_prompt.is_file(), f"missing prompt file: {real_prompt}"
        assert "{{MAX_BUILD_RETRIES}}" in real_prompt.read_text(encoding="utf-8")

        monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(real_prompt))
        monkeypatch.delenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", raising=False)

        agent = SwcraftAgent(
            project_path=str(scaffolded_project),
            llm=FakeLLMClient(["ok"]),
            mcp=FakeMcp(),
        )
        system_prompt = agent._conversation.messages[0]["content"]
        assert "{{MAX_BUILD_RETRIES}}" not in system_prompt
        assert "Retry up to 3 times" in system_prompt

    def test_substitution_reflects_env_override(self, scaffolded_project, tmp_path, monkeypatch):
        """The substituted value reflects SWCRAFT_AGENT_MAX_BUILD_RETRIES when set."""
        from swcraft_agent.core import SwcraftAgent

        prompt_file = tmp_path / "prompt_with_placeholder.md"
        prompt_file.write_text("Retry at most {{MAX_BUILD_RETRIES}} times.", encoding="utf-8")
        monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))
        monkeypatch.setenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", "2")

        agent = SwcraftAgent(
            project_path=str(scaffolded_project),
            llm=FakeLLMClient(["ok"]),
            mcp=FakeMcp(),
        )
        system_prompt = agent._conversation.messages[0]["content"]
        assert system_prompt == "Retry at most 2 times."

    def test_substitution_on_discovered_prompt_path(self, scaffolded_project, tmp_path, monkeypatch):
        """The placeholder is substituted on the packaged-prompt branch too.

        That branch is the default production path when SWCRAFT_AGENT_SYSTEM_PROMPT is unset.
        """
        import swcraft_agent.core as _core_mod
        from swcraft_agent.core import SwcraftAgent

        prompt_file = tmp_path / "discovered_prompt.md"
        prompt_file.write_text("Discovered: retry {{MAX_BUILD_RETRIES}} times.", encoding="utf-8")
        monkeypatch.delenv("SWCRAFT_AGENT_SYSTEM_PROMPT", raising=False)
        monkeypatch.delenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", raising=False)
        monkeypatch.setattr(_core_mod, "_packaged_system_prompt_path", lambda: prompt_file)

        agent = SwcraftAgent(
            project_path=str(scaffolded_project),
            llm=FakeLLMClient(["ok"]),
            mcp=FakeMcp(),
        )
        system_prompt = agent._conversation.messages[0]["content"]
        assert system_prompt == "Discovered: retry 3 times."

    def test_other_braces_untouched(self, scaffolded_project, tmp_path, monkeypatch):
        """Plain replace: unrelated {braces} in the markdown are not mangled."""
        from swcraft_agent.core import SwcraftAgent

        prompt_file = tmp_path / "prompt_with_braces.md"
        prompt_file.write_text('JSON like {"a": 1} and {{MAX_BUILD_RETRIES}} retries.', encoding="utf-8")
        monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))
        monkeypatch.delenv("SWCRAFT_AGENT_MAX_BUILD_RETRIES", raising=False)

        agent = SwcraftAgent(
            project_path=str(scaffolded_project),
            llm=FakeLLMClient(["ok"]),
            mcp=FakeMcp(),
        )
        system_prompt = agent._conversation.messages[0]["content"]
        assert system_prompt == 'JSON like {"a": 1} and 3 retries.'


# ---------------------------------------------------------------------------
# Constructor init order: Azure config checked before MCP starts
# ---------------------------------------------------------------------------


def test_llm_init_before_mcp(scaffolded_project, monkeypatch):
    """SwcraftAgent raises the LLM config error before starting the MCP subprocess."""
    import swcraft_agent.core as _core_mod

    for var in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path

    prompt_file = Path(str(scaffolded_project)).parent / "prompt.md"
    prompt_file.write_text("test prompt", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    class _ShouldNotStart:
        def __init__(self, *a, **kw):
            raise AssertionError("MCP was started before LLM config was validated")

    monkeypatch.setattr(_core_mod, "SwcraftMcpClient", _ShouldNotStart)

    # With the wrong order (MCP first) the AssertionError propagates and the test fails.
    with pytest.raises((KeyError, RuntimeError, OSError)):
        _core_mod.SwcraftAgent(project_path=str(scaffolded_project))


# ---------------------------------------------------------------------------
# Host wiring defaults and the small pure helpers
# ---------------------------------------------------------------------------


class TestScopedDefaults:
    """``scoped=True`` is opt-in; without it a turn proposes no scope."""

    def test_default_is_unscoped(self, scaffolded_project, tmp_path, monkeypatch):
        from swcraft_agent.core import SwcraftAgent

        prompt_file = tmp_path / "p.md"
        prompt_file.write_text("prompt", encoding="utf-8")
        monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))
        agent = SwcraftAgent(project_path=str(scaffolded_project), llm=FakeLLMClient(["ok"]), mcp=FakeMcp())
        assert agent._scoped is False

    def test_default_approver_is_the_terminal_one(self, scaffolded_project, tmp_path, monkeypatch):
        from swcraft_agent.approval import StdinApprover
        from swcraft_agent.core import SwcraftAgent

        prompt_file = tmp_path / "p.md"
        prompt_file.write_text("prompt", encoding="utf-8")
        monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))
        agent = SwcraftAgent(project_path=str(scaffolded_project), llm=FakeLLMClient(["ok"]), mcp=FakeMcp())
        assert isinstance(agent._approver, StdinApprover)

    def test_an_unscoped_turn_never_asks_the_llm_for_a_scope(self, scaffolded_project, tmp_path, monkeypatch):
        from swcraft_agent.core import SwcraftAgent

        prompt_file = tmp_path / "p.md"
        prompt_file.write_text("prompt", encoding="utf-8")
        monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))
        fake = FakeLLMClient(["ok"])
        agent = SwcraftAgent(project_path=str(scaffolded_project), llm=fake, mcp=FakeMcp())
        assert agent.chat("Hello.") == "ok"
        assert fake.structured_calls == []


class TestTheToolKeyedTurnMachineryIsGone:
    """The tool-keyed turn machinery is gone from the host; the editing engine owns review.

    What remains of ``swcraft_tools.authorization`` here is the *refusal*
    vocabulary, because the host still builds two refusals of its own: the
    export-path and escape-command containment of the retained tools.
    """

    def _core_path(self) -> Path:
        return Path(_core.__file__)

    def test_only_the_refusal_vocabulary_is_imported_from_authorization(self):
        """``Refusal`` and ``REFUSAL_MARKER``, and nothing else."""
        import ast

        tree = ast.parse(self._core_path().read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools.authorization"
            for alias in node.names
        }
        assert imported == {"REFUSAL_MARKER", "Refusal"}, imported

    def test_only_the_escape_env_helpers_are_imported_from_session(self):
        """``ScopedSession`` is the engine's staging mechanism, not the host's.

        The agent needs only the escape-variable pinning both client lifetimes apply.
        """
        import ast

        tree = ast.parse(self._core_path().read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "swcraft_tools.session"
            for alias in node.names
        }
        assert imported == {"ESCAPE_ENV_VARS", "pinned_escape_env"}, imported

    @pytest.mark.parametrize(
        "name",
        [
            "declined_scope",
            "tool_text",
            "scalar_arg",
            "_settle",
            "_session_turn",
            "_TOKEN_PREFIX",
            "_LEGACY_SCOPE_JSON_SCHEMA",
            "_LEGACY_PROPOSAL_INSTRUCTION",
            "_propose_task_scope",
            "_render_task_scope",
            "_render_task_scope_approval",
            "_render_safety_verdict",
            "_legacy_field_vocabulary_lines",
        ],
    )
    def test_the_deleted_helper_is_gone(self, name):
        assert not hasattr(_core, name), f"{name} is removed turn machinery and must stay removed"

    @pytest.mark.parametrize(
        "name",
        ["_delete_element", "_confirm_safety", "_classify", "_check_denied_safety_act"],
    )
    def test_the_deleted_guard_method_is_gone(self, name):
        from swcraft_agent.core import _TurnGuard

        assert not hasattr(_TurnGuard, name), f"_TurnGuard.{name} is removed turn machinery"

    def test_the_guard_takes_neither_a_token_registry_a_model_root_nor_an_approver(self):
        """The guard reads no model and asks the user nothing: its parameter set is exact."""
        import inspect

        from swcraft_agent.core import _TurnGuard

        parameters = set(inspect.signature(_TurnGuard.__init__).parameters)
        assert parameters == {"self", "call_tool", "record", "scoped", "server_env"}, parameters


class TestExportPathGateCompleteness:
    """Another tool, or another argument, taking a caller-supplied output path must not slip past.

    The map derived from the server's AST (every ``@mcp.tool()`` function, plain or ``async``, with its
    path arguments in signature order) must equal ``EXPORT_PATH_ARGS``. Not reached: a path argument
    named other than ``output_path``/``output_dir``; a tool registered outside ``server.py`` or by
    other than an ``@mcp.tool()``-style decorator; a path inside ``**kwargs`` or a nested value.
    """

    def test_it_covers_every_mcp_tool_with_a_caller_supplied_output_path(self):
        import ast

        from swcraft_agent.core import EXPORT_PATH_ARGS

        server = Path(__file__).resolve().parents[3] / "packages" / "mcp" / "src" / "swcraft_mcp" / "server.py"
        tree = ast.parse(server.read_text(encoding="utf-8"))
        found: dict[str, tuple[str, ...]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorated = any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool" for d in node.decorator_list
            )
            if not decorated:
                continue
            parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            paths = tuple(argument.arg for argument in parameters if argument.arg in ("output_path", "output_dir"))
            if paths:
                found[node.name] = paths
        assert found == dict(EXPORT_PATH_ARGS)
        assert any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree)), (
            "the server registers async tools; a walk that skipped them would have nothing to prove that on"
        )


class TestTheExportGateNamesTheArgumentItRefuses:
    """One tool, two path arguments: the refusal names the one that was given.

    Under a scope either is refused and the first non-empty one in signature order is named;
    with no scope both pass through untouched.
    """

    @staticmethod
    def _guard(*, scoped: bool, dispatched: list, recorded: list | None = None):
        from swcraft_agent.core import _TurnGuard

        records = [] if recorded is None else recorded
        return _TurnGuard(
            call_tool=lambda name, args: dispatched.append((name, args)) or "OK",  # type: ignore[func-returns-value]
            record=lambda *a: records.append(a),  # type: ignore[func-returns-value]
            scoped=scoped,
        )

    def test_a_scoped_output_dir_without_a_component_is_refused_naming_output_dir(self):
        dispatched: list = []
        result = self._guard(scoped=True, dispatched=dispatched)("export_component", {"output_dir": "/elsewhere"})

        assert result.startswith("REFUSED:"), result
        assert "'output_dir'" in result and "'output_path'" not in result
        assert dispatched == [], "a refused call never reaches the server"

    def test_both_path_arguments_name_the_first_in_signature_order(self):
        dispatched: list = []
        result = self._guard(scoped=True, dispatched=dispatched)(
            "export_component", {"output_dir": "/elsewhere", "output_path": "/elsewhere/A.arxml"}
        )

        assert result.startswith("REFUSED:"), result
        assert "'output_path'" in result and "'output_dir'" not in result

    def test_an_empty_first_argument_and_a_set_second_name_the_second(self):
        dispatched: list = []
        result = self._guard(scoped=True, dispatched=dispatched)(
            "export_component", {"output_path": "", "output_dir": "/elsewhere"}
        )

        assert result.startswith("REFUSED:"), result
        assert "'output_dir'" in result and "'output_path'" not in result
        assert dispatched == []

    def test_the_refusal_code_is_the_audit_value_it_always_was(self):
        refusal = self._guard(scoped=True, dispatched=[])._check_export_path(
            "export_component", {"output_dir": "/elsewhere"}
        )

        assert refusal is not None
        assert refusal.code == "export_path_outside_session"

    def test_the_audit_record_carries_the_code_the_tool_and_the_refused_path(self):
        from swcraft_agent.core import _AUDIT_CALL_REFUSED

        recorded: list = []
        result = self._guard(scoped=True, dispatched=[], recorded=recorded)(
            "export_component", {"output_dir": "/elsewhere"}
        )

        assert recorded == [
            (
                _AUDIT_CALL_REFUSED,
                {"tool": "export_component", "code": "export_path_outside_session", "target": "/elsewhere"},
                result,
            )
        ]

    @pytest.mark.parametrize("args", [{}, {"swc_name": "EngineController"}], ids=["every-component", "one-component"])
    def test_a_scoped_argument_free_export_passes(self, args):
        dispatched: list = []
        result = self._guard(scoped=True, dispatched=dispatched)("export_component", args)

        assert result == "OK"
        assert dispatched == [("export_component", args)]

    def test_an_unscoped_output_dir_passes(self):
        dispatched: list = []
        args = {"output_dir": "/elsewhere"}
        result = self._guard(scoped=False, dispatched=dispatched)("export_component", args)

        assert result == "OK"
        assert dispatched == [("export_component", args)]


class TestEscapeCommandGateCompleteness:
    """The escape-command gate must name every override the server honours.

    An override added to the server with no ``ESCAPE_COMMAND_TOOLS`` row would be a route
    out of a scoped turn that nothing consults.
    """

    def test_it_names_exactly_the_variables_a_session_neutralizes(self):
        from swcraft_agent.core import ESCAPE_COMMAND_TOOLS
        from swcraft_tools.session import ESCAPE_ENV_VARS

        assert set(ESCAPE_COMMAND_TOOLS.values()) == set(ESCAPE_ENV_VARS)

    def test_every_mcp_tool_that_reads_an_override_has_a_row(self):
        import ast

        from swcraft_agent.core import ESCAPE_COMMAND_TOOLS

        server = Path(__file__).resolve().parents[3] / "packages" / "mcp" / "src" / "swcraft_mcp" / "server.py"
        tree = ast.parse(server.read_text(encoding="utf-8"))
        found: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool" for d in node.decorator_list):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Constant) or not isinstance(inner.value, str):
                    continue
                if inner.value.startswith("SWCRAFT_MCP_") and inner.value.endswith("_COMMAND"):
                    found[node.name] = inner.value
        assert found == dict(ESCAPE_COMMAND_TOOLS)


class TestTheEscapeCommandRefusalStaysKeyedOnTheScope:
    """The escape-command refusal is keyed on the scope; the environment pinning is not.

    * :func:`~swcraft_tools.session.pinned_escape_env`, applied on every turn, keeps the
      project's ``.env`` from handing the spawned server an override this host never saw, so
      the only override in force is one the user really has in their environment: the LLM
      cannot set a variable of a process it does not spawn, nor smuggle one in through a file.
    * The **refusal** applies on a scoped turn only: the user approved a list of addresses and
      a shell command is not one of them. On an unscoped turn refusing would close no route the
      LLM could open and would disable the project's own build command, the self-correction
      path ``05-build-error-fix.yaml`` replays.
    """

    def _guard(self, *, scoped: bool, dispatched: list):
        from swcraft_agent.core import _TurnGuard

        return _TurnGuard(
            call_tool=lambda name, args: dispatched.append(name) or "OK",  # type: ignore[func-returns-value]
            record=lambda *a: None,
            scoped=scoped,
            server_env={"SWCRAFT_MCP_BUILD_COMMAND": "moon run :build"},
        )

    def test_a_scoped_turn_refuses_the_configured_override(self):
        dispatched: list = []
        result = self._guard(scoped=True, dispatched=dispatched)("build", {})

        assert result.startswith("REFUSED:"), result
        assert "SWCRAFT_MCP_BUILD_COMMAND" in result
        assert dispatched == [], "a refused call never reaches the server"

    def test_an_unscoped_turn_still_runs_the_projects_own_command(self):
        dispatched: list = []
        result = self._guard(scoped=False, dispatched=dispatched)("build", {})

        assert result == "OK"
        assert dispatched == ["build"], "with no scope in force the override is project configuration"

    def test_the_environment_pinning_is_not_keyed_on_the_scope(self):
        """The half that *is* unconditional, at the seam that applies it."""
        from swcraft_agent.core import review_server_env, scoped_server_env
        from swcraft_tools.session import ESCAPE_ENV_VARS

        unscoped = review_server_env({})
        scoped = scoped_server_env([], "abc123", {})
        for name in ESCAPE_ENV_VARS:
            assert name in unscoped, name
            assert name in scoped, name


class TestAHostNoteReachesTheModelThatHasToRelayIt:
    """A host note, sent as a later text block beside the response body, reaches the model.

    The note carries what `$defs/changeSetResponse` has no field for, e.g. a commit whose audit
    append failed. `ToolResult`'s string form is the body alone, so the guard must append it.
    Each case runs over both wire shapes `_normalize_result` reads: the body as one compact text
    block, and a structured half beside the text.
    """

    @staticmethod
    def _dispatch(string, structured, texts):
        from swcraft_agent.core import _TurnGuard
        from swcraft_agent.mcp_client import ToolResult

        result = ToolResult(string, structured=structured, texts=texts)
        guard = _TurnGuard(call_tool=lambda name, args: result, record=lambda *a: None)
        return guard("change_set", {})

    @pytest.mark.parametrize("shape", ["one copy", "two copies"])
    def test_the_note_is_appended_to_what_the_model_reads(self, shape):
        note = "the change set committed in full and its audit trail is incomplete: ..."
        structured = {"response": "change_set", "status": "committed"}
        compact = json.dumps(structured, separators=(",", ":"))
        string = compact if shape == "one copy" else json.dumps(structured)

        answer = self._dispatch(string, structured, (compact, note))

        # The body stays first and unchanged, so a model planning against the response keeps doing so.
        assert answer == f"{string}\n\n{note}"

    @pytest.mark.parametrize("shape", ["one copy", "two copies"])
    def test_the_body_is_not_repeated_as_a_note(self, shape):
        # Beside a structured half the body block is serialized from the tool's return value, not the
        # `{"result": ...}` envelope, so the two compare neither by string nor by value.
        block = '{"ok":true,"reason":""}'
        if shape == "one copy":
            structured = {"ok": True, "reason": ""}
            string = block
        else:
            structured = {"result": {"ok": True, "reason": ""}}
            string = json.dumps(structured)

        answer = self._dispatch(string, structured, (block,))

        assert answer == string
        assert json.loads(answer) == structured

    def test_a_result_with_no_structured_half_is_handed_back_untouched(self):
        # The retained tools' `OK:` / `ERROR:` / `REFUSED:` string convention.
        from swcraft_agent.core import _TurnGuard
        from swcraft_agent.mcp_client import ToolResult

        result = ToolResult("OK: built", structured=None, texts=("OK: built",))
        guard = _TurnGuard(call_tool=lambda name, args: result, record=lambda *a: None)

        assert guard("build", {}) == "OK: built"


# ---------------------------------------------------------------------------
# The removed vocabulary cannot come back by import (the integration half is
# `test_scoped_turn.py`)
# ---------------------------------------------------------------------------


def _agent_production_files() -> list[Path]:
    """Every agent **production** module (the tests legitimately name the removed vocabulary)."""
    package = Path(_core.__file__).parent
    return sorted(
        (path for path in package.glob("*.py") if path.name != "__init__.py"), key=lambda path: path.as_posix()
    )


def _removed_mcp_tool_names() -> frozenset[str]:
    """The removed and withdrawn MCP tool names, read from ``tool-projections.json`` so they cannot drift."""
    projections = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "tools"
        / "tests"
        / "fixtures"
        / "contracts"
        / "editing-protocol"
        / "tool-projections.json"
    )
    return frozenset(json.loads(projections.read_text(encoding="utf-8"))["projections"]["removed"])


def _removed_authorization_names() -> frozenset[str]:
    """Every name removed from ``authorization.py``, read from ``test_authorization_scope.py``'s ``REMOVED_NAMES``.

    Read, not copied: hand-kept copies drift. Read by AST because ``packages/agent/tests`` has no
    import path to ``packages/tools/tests``.
    """
    source = Path(__file__).resolve().parents[3] / "packages" / "tools" / "tests" / "test_authorization_scope.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(isinstance(target, ast.Name) and target.id == "REMOVED_NAMES" for target in targets):
            assert isinstance(node.value, ast.Call), "REMOVED_NAMES is expected to be a frozenset({...}) literal"
            return frozenset(ast.literal_eval(node.value.args[0]))
    raise AssertionError(f"{source} no longer declares REMOVED_NAMES")


#: The removed tool-keyed authorization vocabulary and delete-token protocol, as identifiers.
#: Spelled here: what the tools-side removal list cannot know, the delete-token protocol and
#: ``ScopedSession``, which still exists but is no longer the agent's to open.
_REMOVED_AUTHORIZATION_SYMBOLS = _removed_authorization_names() | frozenset(
    {
        "ScopedSession",
        "confirmation_token",
        "_PendingDelete",
        "_TOKEN_PREFIX",
    }
)

#: The delete-token protocol is a *string* protocol, so its residue is a string.
_DELETE_TOKEN_STRINGS = ("confirmation_token", "TOKEN")


def _forbidden_uses(path: Path) -> dict[str, list[str]]:
    """Every forbidden import, identifier and computed string in one module.

    * A gate over **code**: imported names, evaluated identifiers (names, attributes,
      definitions, parameters) and string literals the module computes with, which is what an
      MCP tool name is when a host calls one. Docstrings and comments are not read.
    * **Not** a gate over ``swcraft_tools``, where most removed MCP tool names are also
      legitimate Python API (``writer.add_port``); an agent module importing one of those
      writers would be a host writing the model behind the engine, a defect either way.
    """
    import ast
    import re

    from tests.conftest import code_string_constants

    forbidden_symbols = _REMOVED_AUTHORIZATION_SYMBOLS | _removed_mcp_tool_names()
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            identifiers.add(node.name)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            identifiers.add(node.arg)

    strings: set[str] = set()
    for value in code_string_constants(path):
        for name in forbidden_symbols:
            if re.search(rf"\b{re.escape(name)}\b", value):
                strings.add(name)
        for residue in _DELETE_TOKEN_STRINGS:
            if residue in value:
                strings.add(residue)

    return {
        "import": sorted(imported & forbidden_symbols),
        "identifier": sorted(identifiers & forbidden_symbols),
        "string": sorted(strings),
    }


class TestNoRemovedAuthorizationVocabularySurvivesInAgentProductionCode:
    """No agent production module imports, names or calls the removed vocabulary.

    An agent module still importing ``check_call``, naming ``TaskScope`` or calling a removed
    MCP tool would be a migration that only looks finished.
    """

    def test_every_production_module_is_scanned(self):
        """A gate over an empty file list would pass for the wrong reason."""
        names = {path.name for path in _agent_production_files()}

        assert names == {
            "approval.py",
            "cli.py",
            "conversation.py",
            "core.py",
            "llm_client.py",
            "mcp_client.py",
            "scope_proposal.py",
        }, names

    @pytest.mark.parametrize("path", _agent_production_files(), ids=lambda path: path.name)
    def test_no_production_module_imports_names_or_calls_the_removed_vocabulary(self, path):
        assert _forbidden_uses(path) == {"import": [], "identifier": [], "string": []}

    def test_the_gate_fires_on_a_module_that_still_speaks_the_removed_vocabulary(self, tmp_path):
        """The non-vacuity proof: a synthetic module with one offender per category."""
        offender = tmp_path / "unmigrated.py"
        offender.write_text(
            "from swcraft_tools.authorization import TaskScope, check_call\n"
            "from swcraft_tools.session import ScopedSession\n"
            "\n"
            "\n"
            "def run(client, scope: TaskScope):\n"
            '    """A docstring naming check_delta and read_swc, which is allowed."""\n'
            '    check_call(scope, "add_swc", {})\n'
            '    return client.call_tool("delete_element", {"confirmation_token": "TOKEN: abc"})\n',
            encoding="utf-8",
        )

        found = _forbidden_uses(offender)

        assert found["import"] == ["ScopedSession", "TaskScope", "check_call"], found
        assert {"TaskScope", "check_call"} <= set(found["identifier"]), found
        assert found["string"] == ["TOKEN", "add_swc", "confirmation_token", "delete_element"], found

    def test_the_gate_leaves_the_prose_that_explains_the_removal_alone(self, tmp_path):
        """Docstrings and comments naming the removed vocabulary are not a surviving dependency."""
        documented = tmp_path / "documented.py"
        documented.write_text(
            '"""The TaskScope proposal, check_call, check_delta\n'
            'and the TOKEN of the two-step delete_element are removed."""\n'
            "\n"
            "# add_swc and read_swc are gone too.\n"
            "VALUE = 1\n",
            encoding="utf-8",
        )

        assert _forbidden_uses(documented) == {"import": [], "identifier": [], "string": []}


class TestTheInstalledPackageCarriesTheSystemPrompt:
    """The prompt an installation reads is the package's own file; without it the agent refuses by name."""

    def test_the_package_answers_in_a_checkout(self) -> None:
        """The tracked source is the package file, found through importlib.resources."""
        from swcraft_agent import core

        found = core._packaged_system_prompt_path()
        assert found is not None, "the package carries the prompt in a checkout too"
        assert found.resolve() == Path(core.__file__).resolve().parent / core.SYSTEM_PROMPT_FILENAME, (
            "the prompt is read from inside the package, never from a directory above it"
        )

    def test_a_package_without_the_file_answers_none(self, monkeypatch, tmp_path) -> None:
        """A package directory that holds no prompt is ``None``, the refusal's input."""
        from swcraft_agent import core

        monkeypatch.setattr(core, "resource_files", lambda _package: tmp_path)
        assert core._packaged_system_prompt_path() is None

    def test_the_packaged_prompt_is_read(self, scaffolded_project, monkeypatch, tmp_path) -> None:
        """The packaged locator's answer is what the agent reads, substituted."""
        from swcraft_agent import core
        from swcraft_agent.core import SwcraftAgent

        payload = tmp_path / core.SYSTEM_PROMPT_FILENAME
        payload.write_text("packaged prompt, {{MAX_BUILD_RETRIES}} retries\n", encoding="utf-8", newline="\n")
        monkeypatch.delenv("SWCRAFT_AGENT_SYSTEM_PROMPT", raising=False)
        monkeypatch.setattr(core, "_packaged_system_prompt_path", lambda: payload)

        agent = SwcraftAgent(
            project_path=str(scaffolded_project),
            llm=FakeLLMClient(["ok"]),
            mcp=FakeMcp(),
        )
        system_prompt = agent._conversation.messages[0]["content"]
        assert "packaged prompt" in system_prompt
        assert "{{MAX_BUILD_RETRIES}}" not in system_prompt, "the payload is substituted like any other source"

    def test_no_source_is_a_named_refusal_that_names_the_package(self, scaffolded_project, monkeypatch) -> None:
        """The message names the package file and the override."""
        from swcraft_agent import core
        from swcraft_agent.core import SwcraftAgent, SystemPromptNotFoundError

        monkeypatch.delenv("SWCRAFT_AGENT_SYSTEM_PROMPT", raising=False)
        monkeypatch.setattr(core, "_packaged_system_prompt_path", lambda: None)

        with pytest.raises(SystemPromptNotFoundError) as excinfo:
            SwcraftAgent(
                project_path=str(scaffolded_project),
                llm=FakeLLMClient(["ok"]),
                mcp=FakeMcp(),
            )
        message = str(excinfo.value)
        assert "swcraft_agent" in message
        assert core.SYSTEM_PROMPT_FILENAME in message
        assert "SWCRAFT_AGENT_SYSTEM_PROMPT" in message
