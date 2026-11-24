"""The three review modes, and the proof that only a human's yes commits.

No protocol operation can approve anything: the engine has no commit request,
and the review boundary is injected. This module tests **which boundary the
server injects, and where the answer comes from.** The channel is
`SWCRAFT_REVIEW`, read from the environment at server start, which the host
controls and the LLM cannot reach.

`stage-only` is the default and fail-closed: nothing is asked, nothing commits.
`elicit` asks through MCP elicitation; `panel` through the SWCraft VS Code panel
(`swcraft_mcp.panel_review`), played here by a thread. There is deliberately no
`host-confirmed` mode: a tool-confirmation dialog shows the *arguments* before
staging, not the delta computed after it, and `Allow in this Session` can
suppress it. An unrecognised value is a startup error, never a silent fallback.

The change sets come from the published acceptance vectors, an independent
oracle known to stage cleanly; `remove-memory-resource-with-referrers-cleared`
is the case with two questions (a delta that also weakens a check).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import editing_vector_fixtures as fixtures
import pytest
import swcraft_mcp.server as server
import swcraft_tools.editing.engine as engine_mod
import swcraft_tools.session as session
from fastmcp import Client, Context
from mcp.server.session import ServerSession
from mcp.types import Implementation
from protocol_client import (
    CANCEL,
    DECLINE,
    FAIL,
    ScriptedHuman,
    audit_records,
    body_of,
    call,
    call_reviewed,
    lifecycle_records,
)
from swcraft_mcp import panel_review
from swcraft_tools.editing.audit import (
    CHANGE_SET_COMMIT_INTENT,
    ELEMENT_WRITER_OPERATIONS,
    block_is_complete,
    commit_block,
    written_element_addresses,
)
from swcraft_tools.editing.engine import ChangeSetProposal
from swcraft_tools.editing.protocol import DeltaEntry
from swcraft_tools.editing.review import flatten, render_delta_review
from swcraft_tools.swcraft_home import swcraft_home

#: A valid, published change set with no resource weakening: one `create` of an implementation type.
PLAIN = "create-data-type-alias"

#: The published cascade that also weakens a check — removing a memory
#: pool is one of the eight weakening directions — so it is the vector with a
#: second question.
WEAKENING = "remove-memory-resource-with-referrers-cleared"

#: The other published cascade: a rename expressed as remove + create + the
#: reference updates that follow it.
RENAME = "rename-swc-via-remove-create-and-reference-updates"

#: The declared arguments, a weakening direction outside the `hardware` layer: the
#: wire-side witnesses that the second question reaches beyond the hardware classifier.
DECLARED_MEASURE = "create-port-measure"
DECLARED_COEXISTENCE = "update-task-coexistence"

#: A core's declared `asil_capability` raised: a `hardware` weakening whose reason
#: names checks that are not about budgets.
RAISED_CAPABILITY = "update-core-asil-capability"

#: A partition's argument, declared and gone live; the second touches no hardware
#: element at all (a task re-pointed into an argued partition).
DECLARED_PARTITION_COEXISTENCE = "update-partition-coexistence"
LIVE_PARTITION_COEXISTENCE = "update-task-into-argued-partition"

#: A partition's trust class, granted and given up. The grant is no weakening but
#: widens what the partition may write, so it is asked through the same second question.
GRANTED_PARTITION_TRUST = "update-partition-trust-granted"

#: The safety question's two openings these vectors reach: every weakening
#: kind's, and a set of grants alone's (``editing/review.py``).
WEAKENS = "This change set weakens a check."
WIDENS = "This change set widens a permission."
DECLARED_PARTITION_PROTECTION = "update-partition-trust-protected"

#: The ``wait_for`` timeout around the never-completing send's tool call.
_WAIT_FOR_SECONDS = 60

#: The hang guard of a progress send that never completes: half the ``wait_for``. Normal is
#: the 1 s patched bound plus one poll tick (at most 1.26 s measured), a margin of over twenty.
SEND_HANG_SECONDS = _WAIT_FOR_SECONDS / 2


def use_project(vector_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server at a freshly built fixture project for *vector_name*."""
    project = fixtures.build_fixture(tmp_path / "project", vector_name)
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(project / "swcraft.yaml"))
    # The same reset the autouse `_project_env` fixture performs, re-applied
    # because the manifest moved: `_get_project()` caches the loaded project.
    server._PROJECT = None
    return project


def arguments(vector_name: str, project: Path) -> dict[str, Any]:
    """The vector's request as the named arguments of the `change_set` tool, without the `operation` key."""
    request = fixtures.prepare_request(fixtures.VECTORS[vector_name], fixtures.current_baseline_token(project))
    named = {"baseline": request["baseline"], "operations": request["operations"]}
    if request.get("intent"):
        named["intent"] = request["intent"]
    return named


def state(project: Path) -> str:
    """A digest of the model tree, for the byte-identity assertions."""
    return fixtures.model_state_key(project / "model")


def safety_decisions(project: Path) -> list[str]:
    """The `safety_decision` of every audit record carrying one, in log order."""
    return [
        record["args"]["safety_decision"]
        for record in audit_records(project)
        if isinstance(record.get("args"), dict) and "safety_decision" in record["args"]
    ]


def set_mode(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Set `SWCRAFT_REVIEW` and re-read it, which is what a restart does."""
    if value is None:
        monkeypatch.delenv("SWCRAFT_REVIEW", raising=False)
    else:
        monkeypatch.setenv("SWCRAFT_REVIEW", value)
    server._configure_review_mode()


def proposal_of(response: dict[str, Any], intent: str | None) -> ChangeSetProposal:
    """The proposal the boundary was handed, rebuilt from the response, for an equality on the rendered question.

    The change-set id is not rendered, so an arbitrary one is safe here.
    """
    return ChangeSetProposal(
        change_set_id="cs-not-rendered",
        baseline=response["baseline"],
        intent=intent,
        delta=tuple(DeltaEntry.from_json(entry) for entry in response["delta"]),
    )


class TestStageOnlyIsTheDefault:
    """The safe default: no proven human channel, so nothing can commit."""

    def test_an_absent_variable_is_stage_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SWCRAFT_REVIEW", raising=False)
        assert server._resolve_review_mode() == server.REVIEW_STAGE_ONLY

    def test_an_empty_variable_is_stage_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A host exporting the variable empty has selected no mode; reading it as one would widen the default.
        monkeypatch.setenv("SWCRAFT_REVIEW", "   ")
        assert server._resolve_review_mode() == server.REVIEW_STAGE_ONLY

    def test_a_change_set_stages_and_nothing_is_asked_or_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        before = state(project)
        human = ScriptedHuman(True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(PLAIN, project))

        assert structured["status"] == "staged"
        assert "audit_ref" not in structured
        assert human.questions == [], "stage-only asked a question it has no answer channel for"
        assert state(project) == before

    def test_an_unrecognised_value_is_a_startup_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Including `host-confirmed`: accepting it and silently running `stage-only` would leave a host
        # believing it had a review channel.
        monkeypatch.setenv("SWCRAFT_REVIEW", "host-confirmed")
        with pytest.raises(server.ReviewModeError) as refusal:
            server._resolve_review_mode()
        assert "host-confirmed" in str(refusal.value)
        for mode in server.REVIEW_MODES:
            assert f"'{mode}'" in str(refusal.value)
        assert "host-confirmed" not in server.REVIEW_MODES

    def test_there_are_exactly_three_modes(self) -> None:
        assert server.REVIEW_MODES == (server.REVIEW_STAGE_ONLY, server.REVIEW_ELICIT, server.REVIEW_PANEL)


class TestElicitCommitsOnAnAffirmativeAnswerAndOnNothingElse:
    """The five ways an elicitation can end, and the one that commits."""

    def test_an_accepted_affirmative_answer_commits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)
        human = ScriptedHuman(True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(PLAIN, project))

        assert structured["status"] == "committed"
        assert structured["audit_ref"]
        assert len(human.questions) == 1, "a change set with no weakening has exactly one question"
        assert human.unscripted == []
        assert state(project) != before

    @pytest.mark.parametrize("answer", [False, DECLINE, CANCEL], ids=["negative", "declined", "cancelled"])
    def test_every_other_completed_answer_abandons(
        self, answer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)

        structured, _texts = call_reviewed(ScriptedHuman(answer), "change_set", **arguments(PLAIN, project))

        assert structured["status"] == "abandoned"
        assert "audit_ref" not in structured
        assert state(project) == before

    def test_a_client_with_no_elicitation_capability_abandons_and_names_the_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `human=None` is a client with no elicitation capability, not a person who said no: the same terminal
        # state, but the user is told which one happened.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)

        structured, texts = call_reviewed(None, "change_set", **arguments(PLAIN, project))

        assert structured["status"] == "abandoned"
        assert state(project) == before
        assert any("elicit" in text.lower() for text in texts), texts

    def test_an_elicitation_transport_failure_abandons_and_names_the_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)

        structured, texts = call_reviewed(ScriptedHuman(FAIL), "change_set", **arguments(PLAIN, project))

        assert structured["status"] == "abandoned"
        assert state(project) == before
        assert any("elicit" in text.lower() for text in texts), texts

    @pytest.mark.parametrize("vector_name", [WEAKENING, RENAME], ids=["remove-cascade", "rename-cascade"])
    def test_the_published_cascades_never_commit_without_an_affirmative_answer(
        self, vector_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The delta is larger than the requested operations: accepting the tool call is not accepting what it
        # turned out to mean.
        project = use_project(vector_name, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)

        structured, _texts = call_reviewed(ScriptedHuman(DECLINE), "change_set", **arguments(vector_name, project))

        assert structured["status"] == "abandoned"
        assert len(structured["delta"]) > 1, "this vector is here because its delta cascades"
        assert state(project) == before


class TestTheQuestionIsTheRenderedDelta:
    """One rendering, composed in `editing/review.py`, shown unchanged."""

    def test_the_delta_question_is_render_delta_review_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        named = arguments(WEAKENING, project)
        human = ScriptedHuman(DECLINE)

        structured, _texts = call_reviewed(human, "change_set", **named)

        assert human.questions[0] == render_delta_review(proposal_of(structured, named.get("intent")))

    def test_a_removed_element_is_shown_summarised_and_the_safety_question_is_still_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On the wire: the delta question shows the edge's summary, the guard read the whole.

        ``RAM0`` is removed whole, so its ``before`` is summarised; the removal still
        weakens a check, so the second question is asked.
        """
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, False)

        structured, _texts = call_reviewed(human, "change_set", **arguments(WEAKENING, project))

        assert len(human.questions) == 2
        assert "before (summary): " in human.questions[0]
        assert "the whole value is elided: " in human.questions[0]
        assert human.questions[1].startswith(WEAKENS)
        assert structured["status"] == "abandoned"

    def test_the_safety_question_is_render_safety_review_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, False)

        call_reviewed(human, "change_set", **arguments(WEAKENING, project))

        assert len(human.questions) == 2
        # The assertion is that the text is `review.py`'s, not that a particular weakening was found.
        assert human.questions[1].startswith("This change set weakens a check.")
        assert human.questions[1].rstrip().endswith("Approve the weakening as well?")

    @pytest.mark.parametrize(
        ("vector", "expected", "opening"),
        [
            (DECLARED_MEASURE, "port measure", WEAKENS),
            (DECLARED_COEXISTENCE, "task", WEAKENS),
            (DECLARED_PARTITION_COEXISTENCE, "partition 'PARTITION0'", WEAKENS),
            (LIVE_PARTITION_COEXISTENCE, "partition 'PARTITION1'", WEAKENS),
            (GRANTED_PARTITION_TRUST, "partition 'PARTITION0': trust non_trusted -> trusted", WIDENS),
            (
                DECLARED_PARTITION_PROTECTION,
                "partition 'PARTITION0': trust trusted -> trusted_with_protection",
                WEAKENS,
            ),
        ],
        ids=[
            "measures",
            "coexistence",
            "partition-coexistence",
            "partition-coexistence-gone-live",
            "partition-trust-granted",
            "partition-protection-declared",
        ],
    )
    def test_a_declared_argument_asks_the_second_question_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vector: str, expected: str, opening: str
    ) -> None:
        """A declared argument, outside `hardware`, asks the second question, reached as a host reaches it.

        The question names what the argument is about (the port and the signal, or
        the task). A grant of a partition's trust weakens no check, so its question
        opens with the widened permission instead.
        """
        project = use_project(vector, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(vector, project))

        assert len(human.questions) == 2, human.questions
        assert human.questions[1].startswith(opening)
        assert expected in human.questions[1]
        assert structured["status"] == "committed"

    def test_a_grant_alone_records_the_second_answer_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A change set whose only safety change grants a partition trust was ASKED.

        The record's `safety_decision` is the human's answer, not `not-applicable`.
        """
        project = use_project(GRANTED_PARTITION_TRUST, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, True)

        call_reviewed(human, "change_set", **arguments(GRANTED_PARTITION_TRUST, project))

        assert human.questions[1].rstrip().endswith("Approve the widening as well?")
        assert safety_decisions(project) == ["approved"], audit_records(project)

    def test_declining_a_declared_argument_abandons_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The answer is a real one: no is no, on this direction as on the others."""
        project = use_project(DECLARED_MEASURE, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, False)

        structured, _texts = call_reviewed(human, "change_set", **arguments(DECLARED_MEASURE, project))

        assert structured["status"] == "abandoned"
        assert len(human.questions) == 2

    def test_the_audit_record_of_a_declared_argument_records_the_second_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit record keeps the human's answer to a declared argument.

        `check-model` reports nothing for a finding a declaration closes, so the
        record is one place the argument stays visible. Asserted against a plain
        change set, so the claim is a difference rather than the presence of a key.
        """
        project = use_project(DECLARED_MEASURE, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, True)

        call_reviewed(human, "change_set", **arguments(DECLARED_MEASURE, project))

        assert safety_decisions(project) == ["approved"], audit_records(project)

    def test_a_plain_change_set_records_that_no_second_question_was_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the pair above: `not-applicable` is a real recorded state."""
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True)

        call_reviewed(human, "change_set", **arguments(PLAIN, project))

        assert safety_decisions(project) == ["not-applicable"], audit_records(project)

    def test_a_raised_capability_asks_the_second_question_and_names_the_placement_rules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raised capability asks the second question, naming the move and the checks it stops refusing."""
        project = use_project(RAISED_CAPABILITY, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(RAISED_CAPABILITY, project))

        assert len(human.questions) == 2, human.questions
        assert human.questions[1].startswith("This change set weakens a check.")
        assert "hardware element 'CORE0': asil_capability B -> D" in human.questions[1]
        assert "safety.partition-on-unqualified-core" in human.questions[1]
        assert "safety.memory-below-component" in human.questions[1]
        assert "safety.partition-memory-below-level" in human.questions[1]
        assert structured["status"] == "committed"

    def test_stage_only_stages_a_raised_capability_and_asks_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(RAISED_CAPABILITY, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        before = state(project)
        human = ScriptedHuman(True, True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(RAISED_CAPABILITY, project))

        assert structured["status"] == "staged"
        assert human.questions == []
        assert state(project) == before

    def test_the_audit_records_approved_for_a_raise_and_not_applicable_for_a_lowering(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which way the capability moved decides the record: a raise is asked, a lowering is not.

        A capability lowered refuses more, so the lowering commits after the delta
        question alone. What the `not-applicable` half does not witness: that the
        guard looked at the lowering at all (pinned at the engine, in
        `packages/tools/tests/test_editing_authorization.py`).
        """
        raised = use_project(RAISED_CAPABILITY, tmp_path / "raised", monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        raised_answer, _ = call_reviewed(
            ScriptedHuman(True, True), "change_set", **arguments(RAISED_CAPABILITY, raised)
        )
        assert raised_answer["status"] == "committed", raised_answer

        lowered = use_project(PLAIN, tmp_path / "lowered", monkeypatch)
        core = lowered / "model" / "hardware" / "CORE0.yaml"
        core.write_text(core.read_text(encoding="utf-8") + "asil_capability: C\n", encoding="utf-8", newline="\n")
        human = ScriptedHuman(True)
        structured, _texts = call_reviewed(
            human,
            "change_set",
            baseline=fixtures.current_baseline_token(lowered),
            intent="Lower the core's declared capability.",
            operations=[
                {
                    "operation": "update",
                    "address": {"layer": "hardware", "element": "CORE0", "path": [{"field": "asil_capability"}]},
                    "value": "A",
                }
            ],
        )
        assert structured["status"] == "committed", structured
        assert len(human.questions) == 1

        assert safety_decisions(raised) == ["approved"], audit_records(raised)
        assert safety_decisions(lowered) == ["not-applicable"], audit_records(lowered)


class TestTheSafetyAnswerIsNeverDerivedFromTheDeltaAnswer:
    """The second question is a second answer, in every mode.

    `ApprovalDecision.safety_approved` defaults to `False` and is never filled in
    from `approved`: a yes to the delta is no consent to a weaker check.
    """

    def test_yes_to_the_delta_and_no_to_the_weakening_does_not_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)
        human = ScriptedHuman(True, False)

        structured, texts = call_reviewed(human, "change_set", **arguments(WEAKENING, project))

        # `abandoned`, as for a declined delta, not `unauthorized-delta`: a human answered no. The reason says
        # which of the two questions was refused.
        assert structured["status"] == "abandoned"
        assert "error" not in structured
        assert len(human.questions) == 2
        # A declined weakening may be a coexistence argument or a level, so the note names no budget.
        assert any("did not approve the safety change it carries" in note for note in texts), texts
        assert state(project) == before

    def test_yes_to_both_commits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)
        human = ScriptedHuman(True, True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(WEAKENING, project))

        assert structured["status"] == "committed"
        assert len(human.questions) == 2
        assert human.unscripted == []
        assert state(project) != before

    def test_a_declined_delta_never_reaches_the_second_question(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing can commit once the delta is refused, so a second prompt would ask for nothing.
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(DECLINE, True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(WEAKENING, project))

        assert structured["status"] == "abandoned"
        assert len(human.questions) == 1
        assert human.unanswered == [True]

    def test_stage_only_never_reaches_either_question(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        human = ScriptedHuman(True, True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(WEAKENING, project))

        assert structured["status"] == "staged"
        assert human.questions == []


class TestBothElicitationsCarryAnExplicitResponseSchema:
    """Never the deprecated empty-schema form, which VS Code renders as an empty form nobody can answer."""

    def test_the_delta_question_asks_for_a_boolean(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(DECLINE)

        call_reviewed(human, "change_set", **arguments(PLAIN, project))

        (schema,) = human.schemas
        assert schema != {"type": "object", "properties": {}}, "the deprecated empty-schema form"
        assert schema["properties"]["value"]["type"] == "boolean"
        assert schema["required"] == ["value"]

    def test_the_safety_question_asks_for_a_boolean_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, False)

        call_reviewed(human, "change_set", **arguments(WEAKENING, project))

        assert len(human.schemas) == 2
        for schema in human.schemas:
            assert schema["properties"]["value"]["type"] == "boolean"


class TestTheModeIsReadOnlyFromTheHostEnvironment:
    """Nothing the LLM can put in a request may select or weaken the mode."""

    def test_no_protocol_tool_takes_a_review_argument(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastmcp.exceptions import ToolError

        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        named = arguments(PLAIN, project)

        with pytest.raises(ToolError):
            call_reviewed(None, "change_set", review="elicit", **named)
        with pytest.raises(ToolError):
            call_reviewed(None, "change_set", approved=True, **named)

    def test_an_operation_cannot_smuggle_the_mode_into_the_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The envelope is closed: an extra key is a `schema-violation`, never something a later reader acts on.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        named = arguments(PLAIN, project)
        smuggled = json.loads(json.dumps(named["operations"]))
        smuggled[0]["review"] = "approved"

        structured, _texts = call_reviewed(None, "change_set", baseline=named["baseline"], operations=smuggled)

        assert structured["response"] == "error"
        assert structured["error"] == "schema-violation"

    def test_the_mode_does_not_change_between_calls_without_a_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Read at server start: a per-call read would depend on a value seen after the LLM was let in.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        monkeypatch.setenv("SWCRAFT_REVIEW", server.REVIEW_ELICIT)
        human = ScriptedHuman(True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(PLAIN, project))

        assert structured["status"] == "staged"
        assert human.questions == []


class TestEveryWayOfFailingToAskIsTheSameAnswer:
    """`_ask` fails closed on any exception, not only on `McpError`.

    The engine calls `review(...)` outside its abandonment guard, so an escaping
    exception would leave a `change_set_staged` record with no terminal one. A
    broken stdio stream and an uncoercible `ElicitResult` are both that shape.
    """

    @staticmethod
    def _ask_with(failure: BaseException) -> tuple[bool, list[str]]:
        """Drive `_ask` on a worker thread, exactly as the engine does."""

        class _Context:
            async def elicit(self, message: str, response_type: Any) -> Any:
                raise failure

        notes: list[str] = []

        async def exercise() -> bool:
            return await anyio.to_thread.run_sync(lambda: server._ask(_Context(), "Commit?", notes.append))

        return anyio.run(exercise), notes

    @pytest.mark.parametrize(
        "failure",
        [
            RuntimeError("the elicitation channel failed"),
            ConnectionResetError("stdio closed"),
            ValueError("an ElicitResult that cannot be coerced"),
        ],
        ids=["runtime", "closed-stream", "uncoercible-result"],
    )
    def test_a_failure_that_is_not_an_mcp_error_still_abandons_with_the_note(self, failure: Exception) -> None:
        answered, notes = self._ask_with(failure)

        assert answered is False
        assert len(notes) == 1
        assert "could not be asked" in notes[0]
        assert "byte-identical" in notes[0]

    def test_an_interrupt_at_the_prompt_still_propagates(self) -> None:
        # Ctrl-C is no transport failure; swallowing it into a refusal would hide it.
        with pytest.raises(KeyboardInterrupt):
            self._ask_with(KeyboardInterrupt())


class TestTheEntryPointReadsTheChannelsAfterTheProjectEnvFile:
    """`main()` loads the `.env`, then reads the three host channels, then serves.

    A read at module import would ignore a `.env`-set channel: a scope would become
    an unscoped run, `elicit` would degrade to `stage-only`, and a typo would never
    raise.
    """

    @staticmethod
    def _serve(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
        """Run `main()` with *env* arriving from the project's `.env` file."""
        import swcraft_tools.env as env_module

        def _load_env_file(filename: str = ".env") -> None:
            # As `load_env_file` does (`override=False`): it fills in only what the host did not export.
            for name, value in env.items():
                if name not in os.environ:
                    monkeypatch.setenv(name, value)

        monkeypatch.setattr(env_module, "load_env_file", _load_env_file)
        monkeypatch.setattr(server.mcp, "run", lambda *args, **kwargs: None)
        server.main()

    def test_the_review_mode_from_the_env_file_is_the_mode_the_server_runs_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_mode(monkeypatch, None)
        assert server._REVIEW_MODE == server.REVIEW_STAGE_ONLY

        self._serve(monkeypatch, {"SWCRAFT_REVIEW": server.REVIEW_ELICIT})

        assert server._REVIEW_MODE == server.REVIEW_ELICIT

    def test_a_typo_in_the_env_file_is_a_startup_error_rather_than_a_silent_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A host that asked for a channel this server cannot provide must fail to start, not run weaker.
        with pytest.raises(server.ReviewModeError):
            self._serve(monkeypatch, {"SWCRAFT_REVIEW": "host-confirmed"})

    def test_the_scope_pair_from_the_env_file_bounds_the_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert server._APPROVED_SCOPE is None, "the fixture starts every test unscoped"

        self._serve(
            monkeypatch,
            {"SWCRAFT_APPROVED_SCOPE": "[]", "SWCRAFT_SCOPE_ID": "turn-from-the-env-file"},
        )

        # `[]` and absent are different scopes; failing open would make the file's `[]` an unrestricted gate 1.
        assert server._APPROVED_SCOPE == []
        assert server._SCOPE_ID == "turn-from-the-env-file"

    def test_half_a_scope_pair_in_the_env_file_is_a_startup_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(server.ScopeChannelError):
            self._serve(monkeypatch, {"SWCRAFT_APPROVED_SCOPE": "[]"})

    def test_a_scope_pair_the_host_pinned_empty_is_not_refilled_from_the_env_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pair the host exports empty is not refilled from the `.env`, which is why the host pins rather than pops.

        `load_dotenv(override=False)` fills only **absent** keys, so a popped pair let a
        project `.env` scope an unscoped spawn (or half-scope it into a startup error).
        """
        monkeypatch.setenv(server._APPROVED_SCOPE_ENV, "")
        monkeypatch.setenv(server._SCOPE_ID_ENV, "")

        self._serve(
            monkeypatch,
            {"SWCRAFT_APPROVED_SCOPE": "[]", "SWCRAFT_SCOPE_ID": "from-the-env-file"},
        )

        assert server._APPROVED_SCOPE is None
        assert server._SCOPE_ID is None

    def test_the_host_environment_still_wins_over_the_env_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `load_env_file` does not override an exported variable, so the host's environment still wins.
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        self._serve(monkeypatch, {"SWCRAFT_REVIEW": server.REVIEW_STAGE_ONLY})

        assert server._REVIEW_MODE == server.REVIEW_ELICIT


class TestTheAnyioSubmodulesAreImportedByName:
    """`anyio.from_thread` and `anyio.to_thread` are imported as submodules, not as attributes.

    A bare `import anyio` does not make them reachable as attributes.

    anyio 4.15.0 imports its submodules lazily, so `anyio.from_thread` after a bare
    `import anyio` raises `AttributeError`; inside `_ask()`'s `except Exception` that
    abandons every change set silently. The test pins the spelling at the source,
    because no runtime test on one installed anyio sees the other one's loader.
    """

    _SERVER = Path(server.__file__)

    def _imported_submodules(self) -> set[str]:
        import ast

        tree = ast.parse(self._SERVER.read_text(encoding="utf-8"))
        return {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.startswith("anyio.")
        }

    def test_both_bridge_submodules_are_imported_by_name(self) -> None:
        assert {"anyio.from_thread", "anyio.to_thread"} <= self._imported_submodules()

    def test_the_bridge_reaches_the_loop_from_the_worker_thread(self) -> None:
        # The same to_thread -> from_thread round trip `_ask()` makes, on whichever anyio is installed.
        async def answer() -> str:
            return "asked"

        def worker() -> str:
            return anyio.from_thread.run(answer)

        async def main() -> str:
            return await anyio.to_thread.run_sync(worker)

        assert anyio.run(main) == "asked"


class TestTheElicitationApiTheFastmcpFloorIsRaisedFor:
    """The dependency floor and ceiling, and the API the tests above exercised.

    A runtime test proves something only about the installed version, so the
    declared range is asserted to be the verified one, and the installed FastMCP
    to answer `Context.elicit(message, bool)` with the three result types the
    boundary branches on. The `<4` ceiling: fastmcp 4.0.0 changed elicitation
    handling and the greenfield acceptance's reviewed change set came back
    `abandoned`; raising it is a migration with that scenario as its oracle.
    """

    def test_the_declared_range_is_the_verified_one(self) -> None:
        import tomllib

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
        assert "fastmcp>=3.4.5,<4" in declared, declared

    def test_the_installed_fastmcp_satisfies_the_range(self) -> None:
        from importlib.metadata import version

        installed = tuple(int(part) for part in version("fastmcp").split(".")[:3] if part.isdigit())
        assert (3, 4, 5) <= installed < (4,), installed

    def test_the_boundary_branches_on_the_three_result_types_it_can_get(self) -> None:
        # Named here so a FastMCP that renamed one fails here, not in a mode test blaming the review logic.
        from fastmcp.server.elicitation import (
            AcceptedElicitation,
            CancelledElicitation,
            DeclinedElicitation,
        )

        assert {AcceptedElicitation, DeclinedElicitation, CancelledElicitation}
        assert "data" in AcceptedElicitation.model_fields
        assert not hasattr(DeclinedElicitation, "data")
        assert not hasattr(CancelledElicitation, "data")


class TestTheBridgeDoesNotBlockTheEventLoop:
    """The synchronous change-set chain runs on a worker thread, so an open prompt stalls no other request."""

    def test_a_second_call_completes_while_the_first_waits_at_a_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        named = arguments(PLAIN, project)
        order: list[str] = []

        async def waiting_human(message: str, response_type: Any, params: Any, context: Any) -> Any:
            order.append("asked")
            # Answer only after the other call finished: a held event loop would deadlock here.
            while "read" not in order:
                await asyncio.sleep(0.005)
            return response_type(value=False)

        async def exercise() -> tuple[Any, Any]:
            async def prompted() -> Any:
                async with Client(server.mcp, elicitation_handler=waiting_human) as client:
                    result = await client.call_tool("change_set", named)
                order.append("change_set")
                return result

            async def concurrent() -> Any:
                while "asked" not in order:
                    await asyncio.sleep(0.005)
                async with Client(server.mcp) as client:
                    result = await client.call_tool("read", {"address": {"layer": "implementation_types"}})
                order.append("read")
                return result

            return await asyncio.gather(prompted(), concurrent())

        async def bounded() -> tuple[Any, Any]:
            return await asyncio.wait_for(exercise(), timeout=30)

        prompted_result, read_result = asyncio.run(bounded())

        assert body_of(prompted_result)["status"] == "abandoned"
        assert body_of(read_result)["response"] == "read"
        assert order == ["asked", "read", "change_set"], order


class TestTwoConcurrentCommitsAreSerialized:
    """Two approved change sets in flight at once, against one real model.

    The commit's fingerprint check is a check-then-act: unserialized, both threads
    pass the same baseline and the second applies over a delta nobody reviewed.
    The two creates differ in address, so only the baseline makes them conflict;
    the interleaving is forced, not hoped for.
    """

    #: The two addresses, distinct so that nothing but the baseline collides.
    FIRST = "ConcurrentAlphaSpeed"
    SECOND = "ConcurrentBetaSpeed"

    @staticmethod
    def _create(name: str) -> dict[str, Any]:
        return {
            "operation": "create",
            "address": {"layer": "implementation_types", "element": name},
            "value": {
                "name": name,
                "kind": "value_type",
                "base": "float32_base",
                "unit": "kph",
                "value_min": 0.0,
                "value_max": 300.0,
                "description": f"{name}, authored concurrently",
            },
        }

    def _race(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[Any], list[str]]:
        """Two approved `change_set` calls, both held at the prompt, then released."""
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        baseline = fixtures.current_baseline_token(project)

        # Both calls reach the same point (answered prompt, commit next), so neither wins by being ahead.
        at_the_prompt = threading.Barrier(2, timeout=30)
        both_applying = threading.Event()
        arrivals = itertools.count(1)
        real_apply = session._apply_model_delta

        def observed_apply(delta: Any, shadow_files: Any, real_model_root: Any) -> Any:
            if next(arrivals) >= 2:
                both_applying.set()
            else:
                # Hold the first apply while the other thread reaches its fingerprint check. Serialized, it never
                # gets there and this times out: the passing shape, so the wait is short and unasserted.
                both_applying.wait(timeout=1.0)
            return real_apply(delta, shadow_files, real_model_root)

        monkeypatch.setattr(session, "_apply_model_delta", observed_apply)

        asked: list[str] = []

        async def human(message: str, response_type: Any, params: Any, context: Any) -> Any:
            asked.append(message)
            # Off the event loop: one loop drives both calls, so a barrier inside it would deadlock.
            await anyio.to_thread.run_sync(at_the_prompt.wait)
            return response_type(value=True)

        async def submit(name: str) -> Any:
            async with Client(server.mcp, elicitation_handler=human) as client:
                return await client.call_tool(
                    "change_set",
                    {"baseline": baseline, "intent": f"add {name}", "operations": [self._create(name)]},
                )

        async def exercise() -> list[Any]:
            calls = (submit(self.FIRST), submit(self.SECOND))
            return await asyncio.wait_for(asyncio.gather(*calls), timeout=60)

        results = asyncio.run(exercise())
        return project, [body_of(result) for result in results], asked

    def test_only_one_of_two_concurrent_change_sets_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project, responses, _ = self._race(tmp_path, monkeypatch)

        assert sorted(response.get("status") or response["error"] for response in responses) == [
            "committed",
            "stale-baseline",
        ], responses
        landed = sorted(
            path.stem for path in (project / "model" / "data_types" / "implementation").glob("Concurrent*.yaml")
        )
        assert len(landed) == 1, landed

    def test_the_serialization_is_not_held_across_the_elicitation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Serializing the commit must not serialize the review: a user who walks away from one prompt
        # blocks no other review.
        _, _, asked = self._race(tmp_path, monkeypatch)

        assert len(asked) == 2, asked
        assert sorted(self.FIRST in question for question in asked) == [False, True]

    def test_the_refused_commit_left_no_intent_record(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A commit refused at the fingerprint check is refused before the intent record, so the log holds
        # one commit block. `block_is_complete` reads writer records only, so the entries are named too.
        project, responses, _ = self._race(tmp_path, monkeypatch)

        records = audit_records(project)
        intents = [record for record in records if record["operation"] == CHANGE_SET_COMMIT_INTENT]
        assert len(intents) == 1, records
        committed = next(response for response in responses if response.get("status") == "committed")
        block = commit_block(records, committed["audit_ref"])
        assert block is not None
        assert {entry["operation"] for entry in block.entries} <= ELEMENT_WRITER_OPERATIONS
        assert written_element_addresses(block) == frozenset(
            (entry["address"]["layer"], entry["address"]["element"]) for entry in committed["delta"]
        )
        assert block_is_complete(block)


# ---------------------------------------------------------------------------
# `panel`: the SWCraft panel, played by a thread
# ---------------------------------------------------------------------------

#: The panel's answers beside `approve`/`reject`: a decision file that is not JSON,
#: and no answer at all (the client then closes its session, :class:`Panel`).
GARBAGE = "garbage"
SILENT = None

PanelAnswer = str | None


def write_answer(directory: Path, record: dict[str, Any], answer: str) -> None:
    """Answer *record* with *answer* by its decision record beside it, as the review tab does."""
    decision = directory / panel_review.decision_name(record["change_set_id"], record["question"])
    if answer == GARBAGE:
        decision.write_text("not a decision\n", encoding="utf-8", newline="\n")
        return
    panel_review.write_exclusively(
        decision,
        {
            "format": record["format"],
            "change_set_id": record["change_set_id"],
            "question": record["question"],
            "wait": record["wait"],
            "digest": panel_review.text_digest(record["text"]),
            "decision": answer,
        },
    )


class Panel:
    """The extension's review tab, scripted: one answer per pending record, in the order they appear.

    A thread polls `<SWCraft home>/reviews/` and answers each new pending record by
    its decision record; every record seen is kept in :attr:`records`. A record
    unscripted or :data:`SILENT` is left unanswered, and while :meth:`running` the
    server's session-closed probe answers from this panel, so a wait with no
    deadline ends in-process.

    A :data:`GARBAGE` answer is written again while its record is still pending and
    its decision is gone: ``panel_review._settle`` clears a garbage answer landing
    right after the link, and a panel answering once would leave the wait hanging.
    The re-write can leave one garbage file behind in the per-test home.
    """

    def __init__(self, *answers: PanelAnswer) -> None:
        self._answers = list(answers)
        self.records: list[dict[str, Any]] = []
        self._seen: set[str] = set()
        #: The pending records answered :data:`GARBAGE`, by path, re-answered while still pending.
        self._garbage: dict[Path, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._closed = threading.Event()
        #: Set once an answer has been written in full (after :func:`write_answer` returns),
        #: never on the record being seen: the forced-interleaving regression waits on it.
        self.answered = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def kinds(self) -> list[str]:
        return [record["question"] for record in self.records]

    def _run(self) -> None:
        directory = panel_review.reviews_dir()
        while not self._stop.is_set():
            for entry in sorted(directory.glob(f"*{panel_review.PENDING_SUFFIX}"), key=lambda path: path.as_posix()):
                if entry.name in self._seen:
                    continue
                try:
                    record = json.loads(entry.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                self._seen.add(entry.name)
                self.records.append(record)
                answer = self._answers.pop(0) if self._answers else SILENT
                if answer is SILENT:
                    self._closed.set()
                else:
                    write_answer(directory, record, answer)
                    self.answered.set()
                    if answer == GARBAGE:
                        self._garbage[entry] = record
            self._answer_the_cleared_garbage_again(directory)
            self._stop.wait(0.01)

    def _answer_the_cleared_garbage_again(self, directory: Path) -> None:
        for entry, record in self._garbage.items():
            decision = directory / panel_review.decision_name(record["change_set_id"], record["question"])
            if self._stop.is_set() or not entry.exists() or decision.exists():
                continue
            try:
                write_answer(directory, record, GARBAGE)
            except OSError:
                continue  # a Windows sharing violation while the server reads or removes; the next tick retries

    @contextmanager
    def running(self) -> Iterator[Panel]:
        probe = server._session_closed
        server._session_closed = lambda _ctx: self._closed.is_set
        self._thread.start()
        try:
            yield self
        finally:
            self._stop.set()
            self._thread.join(timeout=10)
            server._session_closed = probe


#: The change-set id :func:`take_the_question_name` makes the engine draw.
TAKEN_CHANGE_SET_ID = "cs-20261025T120000Z-c0ffee"


def take_the_question_name(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make the engine draw :data:`TAKEN_CHANGE_SET_ID` and plant another server's live delta question at its name.

    Returns the planted pending record's path.
    """
    monkeypatch.setattr(engine_mod, "new_change_set_id", lambda: TAKEN_CHANGE_SET_ID)
    other = panel_review.pending_record(
        "another server's question",
        kind="delta",
        wait=panel_review.new_wait_token(),
        proposal=ChangeSetProposal(change_set_id=TAKEN_CHANGE_SET_ID, baseline="sha256:other", intent=None, delta=()),
        project_name="another-project",
        manifest="/elsewhere/swcraft.yaml",
        now=time.time(),
    )
    path = panel_review.reviews_dir() / panel_review.pending_name(TAKEN_CHANGE_SET_ID, "delta")
    path.write_text(panel_review.record_json(other), encoding="utf-8", newline="\n")
    return path


def reviews_path() -> Path:
    """`<SWCraft home>/reviews` as the server resolves it, without creating it."""
    return Path(swcraft_home(os.environ, os.name, Path.home())) / "reviews"


def pending_files() -> list[Path]:
    directory = reviews_path()
    return sorted(directory.glob("*.json"), key=lambda path: path.as_posix()) if directory.is_dir() else []


def panel_call(panel: Panel, project: Path, vector: str = PLAIN) -> tuple[dict[str, Any], tuple[str, ...]]:
    """`change_set` with *vector* in `panel` mode, the thread playing the panel."""
    with panel.running():
        structured, texts = call("change_set", **arguments(vector, project))
    return structured, texts


def last_lifecycle(project: Path) -> dict[str, Any]:
    return lifecycle_records(project)[-1]


def approved_record(project: Path) -> dict[str, Any]:
    (record,) = [entry for entry in audit_records(project) if entry["operation"] == "change_set_approved"]
    return record


class TestTheRewriteWarningReachesThePanelAsAServerField:
    """The highlight's one source, end to end: the record's field, on the delta question alone."""

    @staticmethod
    def _mixed_line_ends(project: Path) -> None:
        task = project / "model" / "tasks" / "Task_10ms.yaml"
        text = task.read_bytes().decode("utf-8")
        task.write_bytes(text.replace("\n", "\r\n", 1).encode("utf-8"))

    def test_a_fallback_gives_the_delta_record_the_block_and_the_safety_record_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(DECLARED_COEXISTENCE, tmp_path, monkeypatch)
        self._mixed_line_ends(project)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        panel = Panel("approve", "approve")

        structured, _texts = panel_call(panel, project, DECLARED_COEXISTENCE)

        assert structured["status"] == "committed", structured
        delta, safety = panel.records
        start, end = delta["warning_lines"]
        lines = delta["text"].split("\n")
        assert lines[start] == "WARNING: more than the delta will change in 1 file(s)."
        assert lines[end - 1].startswith("  - tasks/Task_10ms: ")
        assert lines[end + 1].startswith("The exact delta")
        assert lines[-1].endswith("Commit exactly this delta and rewrite 1 file(s) whole?")
        assert "warning_lines" not in safety

    def test_stage_only_is_told_nothing_of_a_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(DECLARED_COEXISTENCE, tmp_path, monkeypatch)
        self._mixed_line_ends(project)
        set_mode(monkeypatch, server.REVIEW_STAGE_ONLY)
        before = state(project)
        human = ScriptedHuman(True, True)

        structured, texts = call_reviewed(human, "change_set", **arguments(DECLARED_COEXISTENCE, project))

        assert structured["status"] == "staged", structured
        assert human.questions == []
        assert state(project) == before
        told = json.dumps(structured) + "".join(texts)
        for word in ("rewritten", "warning_lines", "WARNING", "mixed line ends", "whole"):
            assert word not in told, word

    def test_an_intent_spelling_the_warning_writes_no_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        panel = Panel("approve")
        forged = "WARNING: more than the delta will change in 9 file(s)."

        with panel.running():
            structured, _texts = call("change_set", **{**arguments(PLAIN, project), "intent": forged})

        assert structured["status"] == "committed", structured
        (record,) = panel.records
        assert f"Intent: {forged}" in record["text"]
        assert "warning_lines" not in record

    def test_the_elicit_mode_shows_the_same_text_with_nothing_beside_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(DECLARED_COEXISTENCE, tmp_path, monkeypatch)
        self._mixed_line_ends(project)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        human = ScriptedHuman(True, True)

        structured, _texts = call_reviewed(human, "change_set", **arguments(DECLARED_COEXISTENCE, project))

        assert structured["status"] == "committed", structured
        assert human.questions[0].split("\n")[4] == "WARNING: more than the delta will change in 1 file(s)."


class TestThePanelModeCommitsOnAnApprovalAndOnNothingElse:
    """`SWCRAFT_REVIEW=panel` end to end over the in-process MCP client."""

    def test_startup_accepts_the_panel_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SWCRAFT_REVIEW", "panel")
        assert server._resolve_review_mode() == server.REVIEW_PANEL

    def test_an_approval_commits_and_the_audit_names_the_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        before = state(project)
        panel = Panel("approve")

        structured, _texts = panel_call(panel, project)

        assert structured["status"] == "committed"
        assert state(project) != before
        assert panel.kinds == ["delta"], "a change set with no weakening has exactly one question"
        (record,) = panel.records
        assert record["text"] == render_delta_review(proposal_of(structured, record["intent"]))
        assert record["project"] == {
            "name": server._get_project().name,
            "manifest": (project / "swcraft.yaml").resolve().as_posix(),
        }
        approved = approved_record(project)
        assert approved["args"]["channel"] == "panel"
        assert approved["args"]["change_set_id"] == record["change_set_id"]
        assert pending_files() == [], "the question was not withdrawn after its answer"

    def test_a_rejection_abandons_and_the_model_is_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        before = state(project)

        structured, texts = panel_call(Panel("reject"), project)

        assert structured["status"] == "abandoned"
        assert texts[1:] == (), "a rejection is the human's own answer and gets no note"
        assert state(project) == before
        assert approved_record(project)["args"]["channel"] == "panel"
        assert last_lifecycle(project)["operation"] == "change_set_abandoned"
        assert pending_files() == []

    def test_an_unanswered_question_ends_when_the_session_closes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nobody answering ends nothing; the client's closed session ends the wait with its own note.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        before = state(project)
        panel = Panel(SILENT)

        structured, texts = panel_call(panel, project)

        assert structured["status"] == "abandoned"
        assert texts[1:] == (panel_review.SESSION_CLOSED_NOTE,)
        assert panel.kinds == ["delta"]
        assert state(project) == before
        assert pending_files() == []
        assert last_lifecycle(project)["operation"] == "change_set_abandoned"

    def test_a_decision_that_is_not_one_abandons_with_the_mismatch_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        before = state(project)

        structured, texts = panel_call(Panel(GARBAGE), project)

        assert structured["status"] == "abandoned"
        assert texts[1:] == (panel_review.MISMATCH_NOTE,)
        assert state(project) == before

    def test_a_garbage_answer_inside_the_settle_window_still_ends_the_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The garbage answer is forced into the window between the link and `_settle`, whose clearing removes
        # it; a panel answering once left the wait hanging. The session-closed probe bounds the call, so a
        # regression ends with that note instead of hanging a worker.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        panel = Panel(GARBAGE)
        real_settle = panel_review._settle

        def settle_after_the_answer(*args: Any) -> Any:
            panel.answered.wait(timeout=5)
            return real_settle(*args)

        monkeypatch.setattr(panel_review, "_settle", settle_after_the_answer)
        bound = threading.Timer(8, panel._closed.set)
        bound.start()
        try:
            structured, texts = panel_call(panel, project)
        finally:
            bound.cancel()

        assert panel.answered.is_set(), "the panel never answered"
        assert structured["status"] == "abandoned"
        assert texts[1:] == (panel_review.MISMATCH_NOTE,)
        assert panel.kinds == ["delta"]

    def test_a_weakening_asks_the_safety_question_second_and_both_approvals_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        panel = Panel("approve", "approve")

        structured, _texts = panel_call(panel, project, WEAKENING)

        assert structured["status"] == "committed"
        assert panel.kinds == ["delta", "safety"]
        assert panel.records[1]["text"].startswith(WEAKENS)
        assert panel.records[0]["change_set_id"] == panel.records[1]["change_set_id"]
        assert safety_decisions(project) == ["approved"]

    def test_no_to_the_weakening_abandons(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        before = state(project)
        panel = Panel("approve", "reject")

        structured, _texts = panel_call(panel, project, WEAKENING)

        assert structured["status"] == "abandoned"
        assert panel.kinds == ["delta", "safety"]
        assert state(project) == before

    def test_a_rejected_delta_never_reaches_the_safety_question(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        panel = Panel("reject", "approve")

        structured, _texts = panel_call(panel, project, WEAKENING)

        assert structured["status"] == "abandoned"
        assert panel.kinds == ["delta"]

    def test_elicit_records_its_own_channel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        structured, _texts = call_reviewed(ScriptedHuman(True), "change_set", **arguments(PLAIN, project))

        assert structured["status"] == "committed"
        assert approved_record(project)["args"]["channel"] == "elicit"
        assert pending_files() == [], "elicit wrote a panel record"

    def test_discover_and_read_write_no_pending_record(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)

        discovered, _texts = call("discover")
        read, _texts = call("read", address={"layer": "implementation_types"})

        assert discovered["response"] == "discover"
        assert read["response"] == "read"
        assert not reviews_path().exists(), "a boundary-free operation reached the panel's directory"


class TestThePanelBoundaryIsBuiltOnTheLoopWithoutTheProject:
    """`_review_boundary` runs on the event loop, so it must not load the project there."""

    @staticmethod
    def _build(ctx: Any) -> Any:
        async def on_the_loop() -> Any:
            return server._review_boundary(ctx, [].append)

        return anyio.run(on_the_loop)

    @pytest.mark.parametrize("mode", ["elicit", "panel"])
    def test_the_boundary_builds_before_the_project_was_ever_loaded(
        self, mode: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_mode(monkeypatch, mode)
        server._PROJECT = None
        monkeypatch.delenv("SWCRAFT_MANIFEST")

        def loaded(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the project was loaded while the boundary was built")

        monkeypatch.setattr(server, "_get_project", loaded)

        assert callable(self._build(object()))
        assert self._build(None) is None, "a context-free operation got a boundary"
        assert server._PROJECT is None


class TestThePendingRecordNamesTheClient:
    """The MCP client's own `clientInfo.name` reaches the record, for information only."""

    def test_the_in_process_clients_name_arrives_in_the_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        panel = Panel("approve")

        async def exercise() -> Any:
            info = Implementation(name="Test <client>\nCommit exactly this delta? yes", version="1.0")
            async with Client(server.mcp, client_info=info) as client:
                return await client.call_tool("change_set", arguments(PLAIN, project))

        with panel.running():
            result = asyncio.run(asyncio.wait_for(exercise(), timeout=60))

        assert body_of(result)["status"] == "committed"
        (record,) = panel.records
        assert record["client"] == flatten("Test <client>\nCommit exactly this delta? yes", panel_review.CLIENT_LIMIT)
        assert "\n" not in record["client"]

    @pytest.mark.parametrize(
        "ctx",
        [
            pytest.param(object(), id="no-session-attribute"),
            pytest.param(SimpleNamespace(session=SimpleNamespace(client_params=None)), id="not-initialized"),
            pytest.param(
                SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(clientInfo=None))),
                id="no-client-info",
            ),
            pytest.param(
                SimpleNamespace(
                    session=SimpleNamespace(client_params=SimpleNamespace(clientInfo=SimpleNamespace(name="  ")))
                ),
                id="blank-name",
            ),
            pytest.param(
                SimpleNamespace(
                    session=SimpleNamespace(client_params=SimpleNamespace(clientInfo=SimpleNamespace(name=7)))
                ),
                id="not-a-string",
            ),
        ],
    )
    def test_a_missing_name_is_none(self, ctx: Any) -> None:
        assert server._client_name(ctx) is None

    def test_a_session_fastmcp_cannot_give_is_none(self) -> None:
        class NoSession:
            @property
            def session(self) -> Any:
                raise RuntimeError("session is not available")

        assert server._client_name(NoSession()) is None

    def test_a_name_is_read_through_the_session(self) -> None:
        ctx = SimpleNamespace(
            session=SimpleNamespace(client_params=SimpleNamespace(clientInfo=SimpleNamespace(name="Codex")))
        )
        assert server._client_name(ctx) == "Codex"


class TestTheSessionClosedProbeFailsSafe:
    """`server._session_closed`: a probe it cannot read answers "not closed", never "closed".

    Inverted, every panel wait would end at its first tick.
    """

    @staticmethod
    def _ctx_with(statistics: Any) -> Any:
        return SimpleNamespace(session=SimpleNamespace(_read_stream=SimpleNamespace(statistics=statistics)))

    def test_a_session_whose_stream_cannot_be_resolved_is_open(self) -> None:
        class NoSession:
            @property
            def session(self) -> Any:
                raise RuntimeError("session is not available")

        assert server._session_closed(NoSession())() is False
        assert server._session_closed(SimpleNamespace(session=object()))() is False

    def test_a_statistics_call_that_raises_is_open(self) -> None:
        def broken() -> Any:
            raise RuntimeError("the stream changed shape")

        assert server._session_closed(self._ctx_with(broken))() is False

    @pytest.mark.parametrize(("open_send_streams", "closed"), [(0, True), (1, False)])
    def test_the_answer_is_whether_a_send_stream_is_still_open(self, open_send_streams: int, closed: bool) -> None:
        probe = server._session_closed(self._ctx_with(lambda: SimpleNamespace(open_send_streams=open_send_streams)))
        assert probe() is closed


def record_tool_call_ids(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The request id of every ``tools/call`` the in-process client sends, read as the session assigns it.

    ``BaseSession.send_request`` takes the id before its first await, so the
    wrapper reads the very id the request goes out with.
    """
    from mcp.client.session import ClientSession

    ids: list[int] = []
    original = ClientSession.send_request

    async def recording(self: ClientSession, request: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(getattr(request, "root", request), "method", None) == "tools/call":
            ids.append(self._request_id)
        return await original(self, request, *args, **kwargs)

    monkeypatch.setattr(ClientSession, "send_request", recording)
    return ids


def the_one_tool_call(ids: list[int]) -> int:
    """The id of the one ``tools/call`` in flight; any other count fails here, not at a timeout."""
    assert len(ids) == 1, f"expected one tools/call in flight, saw {ids}"
    return ids[0]


class TestACancelledCallWithdrawsItsPanelReview:
    """`notifications/cancelled` during the panel wait, through the in-process client."""

    def test_the_pending_record_is_withdrawn_and_the_change_set_abandoned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        before = state(project)
        named = arguments(PLAIN, project)
        call_ids = record_tool_call_ids(monkeypatch)

        def abandoned() -> bool:
            records = lifecycle_records(project)
            return bool(records) and records[-1]["operation"] == "change_set_abandoned"

        async def exercise() -> BaseException | None:
            async with Client(server.mcp) as client:
                call_task = asyncio.create_task(client.call_tool("change_set", named))
                while not pending_files():
                    assert not call_task.done(), "the call ended before its question was handed to the panel"
                    await asyncio.sleep(0.01)
                await client.cancel(the_one_tool_call(call_ids), reason="the user stopped the turn")
                try:
                    await call_task
                except Exception as refused:  # the client receives the cancellation as an error
                    outcome: BaseException | None = refused
                else:
                    outcome = None
                # The worker thread sees the cancellation on its next tick; wait
                # for it inside the session, so the server is still running.
                deadline = time.monotonic() + 10
                while not (abandoned() and not pending_files()) and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                return outcome

        outcome = asyncio.run(asyncio.wait_for(exercise(), timeout=30))

        assert outcome is not None, "a cancelled call answered as if it had finished"
        assert pending_files() == [], "the cancelled call left its question pending"
        assert abandoned(), lifecycle_records(project)
        staged = [record for record in lifecycle_records(project) if record["operation"] == "change_set_staged"]
        assert last_lifecycle(project)["args"]["change_set_id"] == staged[-1]["args"]["change_set_id"]
        assert state(project) == before


class TestAFailedHandOffIsRecoverable:
    """A home the server cannot write to: `False`, a note, the terminal record, nothing applied."""

    def test_an_unwritable_home_abandons_with_the_hand_off_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        home = Path(swcraft_home(os.environ, os.name, Path.home()))
        home.parent.mkdir(parents=True, exist_ok=True)
        home.write_text("a file where the SWCraft home should be\n", encoding="utf-8", newline="\n")
        before = state(project)

        structured, texts = call("change_set", **arguments(PLAIN, project))

        assert structured["status"] == "abandoned"
        assert len(texts[1:]) == 1
        assert "could not hand the review to the SWCraft panel" in texts[1]
        assert state(project) == before
        records = lifecycle_records(project)
        assert records[-1]["operation"] == "change_set_abandoned"
        assert records[-1]["args"]["change_set_id"] == records[0]["args"]["change_set_id"]

    def test_a_question_name_another_wait_holds_abandons_and_leaves_its_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two servers that drew one change-set id in the same second: the later hand-off is refused, and the
        # earlier wait's pending record is neither replaced nor removed.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        taken = take_the_question_name(monkeypatch)
        held = taken.read_bytes()
        before = state(project)

        structured, texts = call("change_set", **arguments(PLAIN, project))

        assert structured["status"] == "abandoned"
        assert list(texts[1:]) == [panel_review.NAME_TAKEN_NOTE]
        assert taken.read_bytes() == held
        assert state(project) == before
        records = lifecycle_records(project)
        assert records[-1]["operation"] == "change_set_abandoned"
        assert records[-1]["args"]["change_set_id"] == TAKEN_CHANGE_SET_ID

    def test_a_project_folder_holding_the_home_abandons_before_the_question_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An agent confined to the project folder could read the pending record and write the approval itself.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        home = project / "user-home"
        home.mkdir()
        for variable in ("HOME", "LOCALAPPDATA", "USERPROFILE"):
            monkeypatch.setenv(variable, os.fspath(home))
        before = state(project)

        structured, texts = call("change_set", **arguments(PLAIN, project))

        assert structured["status"] == "abandoned"
        assert list(texts[1:]) == [panel_review.HOME_INSIDE_THE_PROJECT_NOTE]
        assert not panel_review.reviews_location().exists()
        assert state(project) == before
        records = lifecycle_records(project)
        assert records[-1]["operation"] == "change_set_abandoned"
        assert records[-1]["args"]["change_set_id"] == records[0]["args"]["change_set_id"]


# ---------------------------------------------------------------------------
# `panel`: progress across both questions, and the bounded send
# ---------------------------------------------------------------------------


#: More sleeps than any :class:`ScriptedTime` test takes (about 1 200): a wait that no
#: longer ends fails in seconds. Smaller than ``test_panel_review.TICK_CAP`` because
#: every scripted sleep also yields half a real millisecond.
SCRIPTED_TICK_CAP = 10_000


class ScriptedTime:
    """The panel wait's clocks, patched on `panel_review`: every sleep moves both by its length.

    With *answer_after* set, the sleep also plays the panel: it approves each
    pending record *answer_after* injected seconds after first seeing it. Every
    record seen is kept in :attr:`records`; the sleep refuses to run past
    :data:`SCRIPTED_TICK_CAP`.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer_after: float | None = None) -> None:
        self.now = float(int(time.time()))
        self.mono = 1000.0
        self.ticks = 0
        self._answer_after = answer_after
        self._first_seen: dict[str, float] = {}
        self.records: list[dict[str, Any]] = []
        monkeypatch.setattr(panel_review, "_monotonic", lambda: self.mono)
        monkeypatch.setattr(panel_review, "_wall", lambda: self.now)
        monkeypatch.setattr(panel_review, "_sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.ticks += 1
        if self.ticks > SCRIPTED_TICK_CAP:
            raise AssertionError(f"the wait did not end within {SCRIPTED_TICK_CAP} scripted sleeps")
        self.mono += seconds
        self.now += seconds
        time.sleep(0.0005)  # yield the interpreter to the event loop's thread
        if self._answer_after is None:
            return
        directory = panel_review.reviews_dir()
        for entry in sorted(directory.glob(f"*{panel_review.PENDING_SUFFIX}"), key=lambda path: path.as_posix()):
            seen = self._first_seen.setdefault(entry.name, self.mono)
            if seen == self.mono:
                self.records.append(json.loads(entry.read_text(encoding="utf-8")))
            if self.mono - seen >= self._answer_after:
                record = json.loads(entry.read_text(encoding="utf-8"))
                decision = directory / panel_review.decision_name(record["change_set_id"], record["question"])
                if not decision.exists():
                    write_answer(directory, record, "approve")


def record_the_asks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], float]]:
    """Wrap `panel_review.ask`: each call's notes and the real time it returned, in order."""
    original = panel_review.ask
    asks: list[tuple[list[str], float]] = []

    def recording(question: str, **kwargs: Any) -> bool:
        notes: list[str] = []
        sink = kwargs["notes"]

        def both(note: str) -> None:
            notes.append(note)
            sink(note)

        try:
            return original(question, **{**kwargs, "notes": both})
        finally:
            asks.append((notes, time.monotonic()))

    monkeypatch.setattr(panel_review, "ask", recording)
    return asks


class TestThePanelWaitSendsProgress:
    """`notifications/progress` every minute of a wait, one counter across both questions, bounded on the loop."""

    #: Injected seconds before the scripted panel answers each question: two progress notifications each.
    ANSWER_AFTER = 150.0

    def test_one_counter_rises_across_the_delta_and_the_safety_question(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(WEAKENING, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        clocks = ScriptedTime(monkeypatch, answer_after=self.ANSWER_AFTER)
        received: list[tuple[float, float | None, str | None]] = []

        async def handler(progress: float, total: float | None, message: str | None) -> None:
            received.append((progress, total, message))

        async def exercise() -> Any:
            async with Client(server.mcp) as client:
                return await client.call_tool("change_set", arguments(WEAKENING, project), progress_handler=handler)

        result = asyncio.run(asyncio.wait_for(exercise(), timeout=60))

        assert body_of(result)["status"] == "committed"
        assert [record["question"] for record in clocks.records] == ["delta", "safety"]
        values = [progress for progress, _total, _message in received]
        assert values == sorted(set(values)), f"the progress values do not strictly rise: {values}"
        assert values == [1, 2, 3, 4]
        assert all(total is None for _progress, total, _message in received)
        assert [message for _progress, _total, message in received] == [
            panel_review.progress_text("delta", 1),
            panel_review.progress_text("delta", 2),
            panel_review.progress_text("safety", 1),
            panel_review.progress_text("safety", 2),
        ]
        assert "(change)" in received[0][2] and "(safety question)" in received[2][2]

    def test_a_call_without_a_progress_token_gets_no_notification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        ScriptedTime(monkeypatch, answer_after=self.ANSWER_AFTER)
        reported: list[str | None] = []
        sent: list[Any] = []
        report = Context.report_progress
        send = ServerSession.send_progress_notification

        async def reporting(self: Context, progress: float, total: float | None = None, message: str | None = None):
            reported.append(message)
            await report(self, progress, total, message)

        async def sending(self: ServerSession, *args: Any, **kwargs: Any) -> None:
            sent.append((args, kwargs))
            await send(self, *args, **kwargs)

        monkeypatch.setattr(Context, "report_progress", reporting)
        monkeypatch.setattr(ServerSession, "send_progress_notification", sending)

        async def exercise() -> Any:
            async with Client(server.mcp) as client:
                # The raw session's call: no progress callback, so the request carries no progressToken.
                return await client.session.call_tool("change_set", arguments(PLAIN, project))

        result = asyncio.run(asyncio.wait_for(exercise(), timeout=60))

        assert body_of(result)["status"] == "committed"
        assert len(reported) == 2, "the wait did not try to report its minutes, so nothing was measured"
        assert sent == []

    def test_a_call_cancelled_while_a_send_is_in_flight_is_a_cancelled_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The cancellation reaches the worker as a class raised out of the real `from_thread.run` bridge, not
        # through `check_cancelled()`: the send is held until the call is cancelled, past its own bound.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        before = state(project)
        ScriptedTime(monkeypatch)
        monkeypatch.setattr(server, "PROGRESS_SEND_SECONDS", 600)
        asks = record_the_asks(monkeypatch)
        in_flight = threading.Event()
        send_ended_by: list[str] = []

        async def held(self: Context, progress: float, total: float | None = None, message: str | None = None):
            in_flight.set()
            try:
                await anyio.sleep_forever()
            except BaseException as ended:
                send_ended_by.append(type(ended).__name__)
                raise

        monkeypatch.setattr(Context, "report_progress", held)
        bridge = anyio.from_thread.run
        raised_by_the_bridge: list[type[BaseException]] = []

        def recording_bridge(func: Any, *args: Any) -> Any:
            try:
                return bridge(func, *args)
            except BaseException as raised:
                raised_by_the_bridge.append(type(raised))
                raise

        monkeypatch.setattr(anyio.from_thread, "run", recording_bridge)
        call_ids = record_tool_call_ids(monkeypatch)

        def abandoned() -> bool:
            records = lifecycle_records(project)
            return bool(records) and records[-1]["operation"] == "change_set_abandoned"

        async def exercise() -> BaseException | None:
            async with Client(server.mcp) as client:
                call_task = asyncio.create_task(client.call_tool("change_set", arguments(PLAIN, project)))
                while not in_flight.is_set():
                    assert not call_task.done(), "the call ended before a progress send was in flight"
                    await asyncio.sleep(0.01)
                await client.cancel(the_one_tool_call(call_ids), reason="the user stopped the turn")
                try:
                    await call_task
                except Exception as refused:
                    outcome: BaseException | None = refused
                else:
                    outcome = None
                deadline = time.monotonic() + 10
                while not (abandoned() and not pending_files()) and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                return outcome

        outcome = asyncio.run(asyncio.wait_for(exercise(), timeout=30))

        assert outcome is not None, "a cancelled call answered as if it had finished"
        assert send_ended_by == ["CancelledError"], send_ended_by
        (raised,) = raised_by_the_bridge
        assert issubclass(raised, (concurrent.futures.CancelledError, asyncio.CancelledError)), raised
        assert [notes for notes, _returned in asks] == [[panel_review.CANCELLED_NOTE]]
        assert abandoned(), lifecycle_records(project)
        assert pending_files() == []
        assert state(project) == before

    def test_a_send_that_never_completes_does_not_hold_the_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A client that keeps the pipe open and stops reading: the bound cuts the send, and the decision
        # written meanwhile is read on the next tick.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        ScriptedTime(monkeypatch)
        bound = 1.0
        monkeypatch.setattr(server, "PROGRESS_SEND_SECONDS", bound)
        asks = record_the_asks(monkeypatch)
        written: list[float] = []
        send_ended_by: list[str] = []

        async def never_completes(self: ServerSession, *args: Any, **kwargs: Any) -> None:
            if not written:
                directory = panel_review.reviews_dir()
                (entry,) = directory.glob(f"*{panel_review.PENDING_SUFFIX}")
                write_answer(directory, json.loads(entry.read_text(encoding="utf-8")), "approve")
                written.append(time.monotonic())
            try:
                await anyio.sleep_forever()
            except BaseException as ended:
                send_ended_by.append(type(ended).__name__)
                raise

        monkeypatch.setattr(ServerSession, "send_progress_notification", never_completes)
        # Records the scopes the server opens, to show the one that cancelled the send is the bound's own.
        bound_scopes: list[anyio.CancelScope] = []
        move_on_after = anyio.move_on_after

        def recorded(delay: float, *args: Any, **kwargs: Any) -> anyio.CancelScope:
            scope = move_on_after(delay, *args, **kwargs)
            if sys._getframe(1).f_globals.get("__name__") == server.__name__:
                assert delay == server.PROGRESS_SEND_SECONDS
                bound_scopes.append(scope)
            return scope

        monkeypatch.setattr(anyio, "move_on_after", recorded)

        async def exercise() -> Any:
            async with Client(server.mcp) as client:
                return await client.call_tool("change_set", arguments(PLAIN, project))

        result = asyncio.run(asyncio.wait_for(exercise(), timeout=_WAIT_FOR_SECONDS))

        assert body_of(result)["status"] == "committed"
        assert send_ended_by == ["CancelledError"], send_ended_by
        assert [scope.cancelled_caught for scope in bound_scopes] == [True], "the send was not cut by its bound"
        ((notes, returned),) = asks
        assert notes == []
        # The check is the send cut by its bound (above); this only names a hang.
        assert returned - written[0] < SEND_HANG_SECONDS
