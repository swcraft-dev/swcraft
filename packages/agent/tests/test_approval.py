"""Tests for the approval channel.

The channel is fail-closed: the only way any method here returns ``True`` is a recorded answer of ``y``; EOF, an
empty line and an exhausted scripted list all deny. The server's two questions are displayed unchanged with the
host's answer convention appended, the scope prompt renders every ``{operation, address}`` entry, and ``[y/N]``
is written down in exactly one place.
"""

import ast
import inspect
import io
from pathlib import Path

import pytest
from swcraft_agent import approval as approval_module
from swcraft_agent.approval import (
    ANSWER_PROMPT,
    SCOPE_QUESTION,
    Approver,
    ScriptedApprover,
    StdinApprover,
    is_approval,
    render_scope_approval,
)
from swcraft_agent.mcp_client import ELICITATION_ANSWER_PROMPT, render_elicitation
from swcraft_agent.scope_proposal import parse_proposal, render_scope

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

_MUTATION_PAYLOAD = {
    "summary": "Add a provide port pEngineSpeed on ISpeed to EngineController",
    "entries": [
        {
            "operation": "update",
            "address": {"layer": "swc", "element": "EngineController", "path": [{"field": "ports"}]},
        },
        {"operation": "create", "address": {"layer": "interfaces", "element": "ISpeed"}},
    ],
}

_READ_ONLY_PAYLOAD = {
    "summary": "List the SWCs in the model",
    "entries": [],
}

#: A stand-in for the delta review the server sends, as one opaque block of text. Not a copy of the real
#: renderer's output: the host never re-words the message, so any text serves, and a copy would drift silently.
_SERVER_DELTA_MESSAGE = (
    "Proposed change to the model\n"
    "  intent: add pEngineSpeed to EngineController\n"
    "  update swc / EngineController / ports\n"
    "    before: []\n"
    "    after:  [{'name': 'pEngineSpeed', 'direction': 'provide', 'interface': 'ISpeed'}]"
)

#: The same, for the second, independent question about the resource weakenings.
_SERVER_SAFETY_MESSAGE = (
    "This change weakens a check\n"
    "  - RamPool: threshold 0.7 -> 0.95\n"
    "  - RamPool: size 1024 -> 4096\n"
    "  - RamPool: enforcement strict -> advisory"
)


def _proposal(payload: dict | None = None):
    return parse_proposal(payload or _MUTATION_PAYLOAD)


class RecordingApprover(Approver):
    """An approver whose answer is fixed, recording every prompt it was shown."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def ask(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self.answer


class UnanswerableApprover(Approver):
    """An approver that cannot answer — used to prove every path goes via ask()."""

    def ask(self, prompt: str) -> bool:
        raise AssertionError(f"ask() reached with: {prompt}")


# ---------------------------------------------------------------------------
# is_approval
# ---------------------------------------------------------------------------


class TestIsApproval:
    @pytest.mark.parametrize("answer", ["y", "Y", " y ", "y\n", "\ty\r\n"])
    def test_approving_answers(self, answer: str) -> None:
        assert is_approval(answer) is True

    @pytest.mark.parametrize("answer", ["", " ", "n", "N", "no", "yes", "yy", "ok", "1", "true", "approve"])
    def test_everything_else_denies(self, answer: str) -> None:
        assert is_approval(answer) is False


# ---------------------------------------------------------------------------
# StdinApprover
# ---------------------------------------------------------------------------


class TestStdinApprover:
    def test_y_approves(self) -> None:
        approver = StdinApprover(stream=io.StringIO("y\n"), out=io.StringIO())
        assert approver.ask("Approve? [y/N]: ") is True

    def test_uppercase_y_approves(self) -> None:
        approver = StdinApprover(stream=io.StringIO("Y\n"), out=io.StringIO())
        assert approver.ask("Approve? [y/N]: ") is True

    @pytest.mark.parametrize("line", ["n\n", "no\n", "yes\n", "\n", "   \n", "maybe\n", "Y E S\n"])
    def test_anything_else_denies(self, line: str) -> None:
        approver = StdinApprover(stream=io.StringIO(line), out=io.StringIO())
        assert approver.ask("Approve? [y/N]: ") is False

    def test_an_empty_line_denies(self) -> None:
        """Pressing Enter is the ``[y/N]`` default, and the default is no."""
        approver = StdinApprover(stream=io.StringIO("\n"), out=io.StringIO())
        assert approver.ask("Approve? [y/N]: ") is False

    def test_eof_denies(self) -> None:
        approver = StdinApprover(stream=io.StringIO(""), out=io.StringIO())
        assert approver.ask("Approve? [y/N]: ") is False

    def test_eof_after_an_approval_denies(self) -> None:
        """The default is deny per question, not per session."""
        approver = StdinApprover(stream=io.StringIO("y\n"), out=io.StringIO())
        assert approver.ask("first") is True
        assert approver.ask("second") is False

    def test_successive_questions_consume_successive_lines(self) -> None:
        approver = StdinApprover(stream=io.StringIO("y\nn\ny\n"), out=io.StringIO())
        assert [approver.ask("q") for _ in range(3)] == [True, False, True]

    def test_prompt_is_shown_before_reading(self) -> None:
        out = io.StringIO()
        approver = StdinApprover(stream=io.StringIO("y\n"), out=out)
        approver.ask("Proposed task scope\n  summary: add a port")
        assert "Proposed task scope" in out.getvalue()
        assert "summary: add a port" in out.getvalue()

    def test_streams_default_to_sys_stdin_and_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = io.StringIO()
        monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
        monkeypatch.setattr("sys.stdout", out)
        assert StdinApprover().ask("question") is True
        assert "question" in out.getvalue()

    def test_defaults_are_resolved_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A host that redirects stdin after construction still reaches the user."""
        approver = StdinApprover()
        monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
        monkeypatch.setattr("sys.stdout", io.StringIO())
        assert approver.ask("question") is False
        monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
        assert approver.ask("question") is True


# ---------------------------------------------------------------------------
# ScriptedApprover
# ---------------------------------------------------------------------------


class TestScriptedApprover:
    def test_answers_are_consumed_in_order(self) -> None:
        approver = ScriptedApprover(["y", "n", "y"])
        assert [approver.ask("q") for _ in range(3)] == [True, False, True]
        assert approver.answers_given == ["y", "n", "y"]

    def test_running_out_denies_without_raising(self) -> None:
        approver = ScriptedApprover(["y"])
        assert approver.ask("q") is True
        assert approver.ask("q") is False
        assert approver.ask("q") is False

    def test_running_out_is_recorded_as_no_answer(self) -> None:
        approver = ScriptedApprover(["y"])
        approver.ask("q")
        approver.ask("q")
        assert approver.answers_given == ["y", None]

    def test_no_answers_at_all_denies_every_question(self) -> None:
        """A scenario without approvals approves nothing."""
        approver = ScriptedApprover()
        assert approver.confirm_scope(_proposal()) is False
        assert approver.ask(render_elicitation(_SERVER_SAFETY_MESSAGE)) is False
        assert approver.ask(render_elicitation(_SERVER_DELTA_MESSAGE)) is False

    def test_an_exhausted_script_denies_the_change_set_review(self) -> None:
        """The exhausted-list answer is *no*, never "unanswered, carry on"."""
        approver = ScriptedApprover(["y"])
        assert approver.confirm_scope(_proposal()) is True
        assert approver.ask(render_elicitation(_SERVER_DELTA_MESSAGE)) is False
        assert approver.answers_given == ["y", None]

    def test_empty_answer_list_is_the_same_as_none(self) -> None:
        assert ScriptedApprover([]).ask("q") is False

    def test_prompts_are_recorded(self) -> None:
        approver = ScriptedApprover(["y", "y"])
        approver.confirm_scope(_proposal())
        approver.ask(render_elicitation(_SERVER_DELTA_MESSAGE))
        assert len(approver.prompts) == 2
        assert SCOPE_QUESTION in approver.prompts[0]
        assert ANSWER_PROMPT in approver.prompts[1]

    def test_remaining_counts_unconsumed_answers(self) -> None:
        approver = ScriptedApprover(["y", "n"])
        assert approver.remaining == 2
        approver.ask("q")
        assert approver.remaining == 1
        approver.ask("q")
        assert approver.remaining == 0
        approver.ask("q")
        assert approver.remaining == 0

    @pytest.mark.parametrize("answer", [True, 1, None, ["y"]])
    def test_non_string_answers_are_rejected_at_construction(self, answer: object) -> None:
        with pytest.raises(TypeError) as exc:
            ScriptedApprover(["y", answer])  # type: ignore[list-item]
        assert "approval 1" in str(exc.value)

    def test_answers_are_not_shared_between_approvers(self) -> None:
        answers = ["y"]
        first = ScriptedApprover(answers)
        second = ScriptedApprover(answers)
        assert first.ask("q") is True
        assert second.ask("q") is True
        assert answers == ["y"]


# ---------------------------------------------------------------------------
# The scope prompt — the one thing the host still composes
# ---------------------------------------------------------------------------


class TestScopePrompt:
    def test_renders_the_whole_scope_before_asking(self) -> None:
        proposal = _proposal()
        prompt = render_scope_approval(proposal)
        rendered = render_scope(proposal)
        assert rendered in prompt
        assert prompt.index(rendered) < prompt.index(SCOPE_QUESTION)

    def test_names_the_operation_and_the_address_of_every_entry(self) -> None:
        prompt = render_scope_approval(_proposal())
        for fragment in ("update", "swc", "EngineController", "ports", "create", "interfaces", "ISpeed"):
            assert fragment in prompt, f"{fragment!r} missing from the scope prompt"
        assert _MUTATION_PAYLOAD["summary"] in prompt

    def test_the_read_only_proposal_renders_too(self) -> None:
        prompt = render_scope_approval(_proposal(_READ_ONLY_PAYLOAD))
        assert _READ_ONLY_PAYLOAD["summary"] in prompt
        assert "none" in prompt.lower()
        assert SCOPE_QUESTION in prompt

    def test_confirm_scope_shows_that_prompt(self) -> None:
        approver = RecordingApprover(answer=True)
        proposal = _proposal()
        assert approver.confirm_scope(proposal) is True
        assert approver.prompts == [render_scope_approval(proposal)]

    def test_rendering_is_stable(self) -> None:
        assert render_scope_approval(_proposal()) == render_scope_approval(_proposal())


# ---------------------------------------------------------------------------
# The two review prompts — the server's message, displayed unchanged
# ---------------------------------------------------------------------------


class TestTheServersMessageIsDisplayedUnchanged:
    """The host adds its answer convention and nothing else.

    The question a user answers must not depend on the host. The subject is `mcp_client.render_elicitation`, which
    the live path calls for both server questions.
    """

    @pytest.mark.parametrize(
        "message",
        [_SERVER_DELTA_MESSAGE, _SERVER_SAFETY_MESSAGE],
        ids=["delta", "safety"],
    )
    def test_the_prompt_carries_the_server_message_verbatim(self, message: str) -> None:
        prompt = render_elicitation(message)
        assert prompt == f"{message}\n\n{ELICITATION_ANSWER_PROMPT}"
        assert prompt.index(message) < prompt.index(ELICITATION_ANSWER_PROMPT)

    def test_the_before_and_after_values_survive_untouched(self) -> None:
        prompt = render_elicitation(_SERVER_DELTA_MESSAGE)
        for value in ("before:", "after:", "pEngineSpeed", "ISpeed"):
            assert value in prompt

    def test_every_weakening_survives_untouched(self) -> None:
        prompt = render_elicitation(_SERVER_SAFETY_MESSAGE)
        for value in ("0.7", "0.95", "1024", "4096", "strict", "advisory"):
            assert value in prompt, f"{value!r} missing from the safety prompt"

    def test_the_approver_is_shown_exactly_that_prompt(self) -> None:
        approver = RecordingApprover(answer=True)
        assert approver.ask(render_elicitation(_SERVER_DELTA_MESSAGE)) is True
        assert approver.prompts == [render_elicitation(_SERVER_DELTA_MESSAGE)]

    def test_rendering_is_stable(self) -> None:
        assert render_elicitation(_SERVER_DELTA_MESSAGE) == render_elicitation(_SERVER_DELTA_MESSAGE)
        assert render_elicitation(_SERVER_SAFETY_MESSAGE) == render_elicitation(_SERVER_SAFETY_MESSAGE)

    def test_the_answer_convention_is_written_down_in_exactly_one_place(self) -> None:
        assert ELICITATION_ANSWER_PROMPT is ANSWER_PROMPT
        assert SCOPE_QUESTION.endswith(ANSWER_PROMPT)
        source = Path(inspect.getfile(approval_module)).read_text(encoding="utf-8")
        assert source.count('"[y/N]: "') == 1

    def test_the_module_composes_no_second_prompt_for_the_servers_questions(self) -> None:
        """`confirm_commit` / `confirm_safety` do not exist: an elicitation carries no discriminator to route by."""
        for name in ("confirm_commit", "confirm_safety", "render_commit_approval", "render_safety_approval"):
            assert not hasattr(approval_module, name), f"approval.py still defines {name}"

    def test_the_module_no_longer_formats_a_safety_verdict(self) -> None:
        """The ``SafetyVerdict`` rendering is the server's (``render_safety_review``), not re-homed here."""
        source = Path(inspect.getfile(approval_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
        assert "SafetyVerdict" not in imported
        assert "SafetyChange" not in imported
        assert "classify_safety_act" not in imported
        assert "describe()" not in source


# ---------------------------------------------------------------------------
# The approver interface is the only approval path
# ---------------------------------------------------------------------------


class TestApprovalOnlyComesFromAnAnswer:
    """``confirm_scope`` is the only ``confirm_*``, and it is not a shortcut."""

    def test_confirm_scope_goes_through_ask(self) -> None:
        approver = UnanswerableApprover()
        with pytest.raises(AssertionError):
            approver.confirm_scope(_proposal())

    def test_a_denying_answer_denies_confirm_scope(self) -> None:
        assert RecordingApprover(answer=False).confirm_scope(_proposal()) is False

    def test_confirm_scope_returns_exactly_what_ask_returned(self) -> None:
        assert RecordingApprover(answer=True).confirm_scope(_proposal()) is True

    @pytest.mark.parametrize("approver_class", [StdinApprover, ScriptedApprover])
    def test_concrete_approvers_do_not_override_confirm_scope(self, approver_class: type) -> None:
        """The confirm is implemented once, on the ABC — nothing may shortcut it."""
        assert approver_class.confirm_scope is Approver.confirm_scope

    def test_no_answer_at_all_denies(self) -> None:
        """Both concrete approvers, asked with nothing to read, deny."""
        stdin_approver = StdinApprover(stream=io.StringIO(""), out=io.StringIO())
        assert stdin_approver.confirm_scope(_proposal()) is False
        assert ScriptedApprover().confirm_scope(_proposal()) is False

    def test_an_empty_line_denies(self) -> None:
        approver = StdinApprover(stream=io.StringIO("\n"), out=io.StringIO())
        assert approver.confirm_scope(_proposal()) is False

    def test_ask_is_abstract(self) -> None:
        assert getattr(Approver.ask, "__isabstractmethod__", False) is True
        with pytest.raises(TypeError):
            Approver()  # type: ignore[abstract]

    def test_the_module_never_returns_a_literal_true(self) -> None:
        """No function here may answer yes on its own.

        A bare ``return True`` in the module would be a bypass of ``is_approval``.
        """
        source = Path(inspect.getfile(approval_module)).read_text(encoding="utf-8")
        offenders = [
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value is True
        ]
        assert offenders == [], f"literal 'return True' at lines {offenders} in approval.py"
