"""The editing protocol's two host-neutral review renderings.

`editing/review.py` renders; the host asks. The pure core stays host-neutral (no
I/O, no process, no prompt), so a renderer takes a proposal and returns a string.
The server renders once and every host displays the message unchanged, so the
question does not differ by host. `render_delta_review` and `render_safety_review`
answer separate fields of `ApprovalDecision` (`approved`, `safety_approved`); the
second is asked only for a non-empty verdict, so rendering an empty one is a
programming error.
"""

from __future__ import annotations

import builtins
import dataclasses
import io
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import NoReturn

import pytest
from swcraft_tools.authorization import SafetyChange, SafetyChangeKind, SafetyVerdict
from swcraft_tools.editing.address import Address, AddressError, FieldStep, KeyStep, MemberStep
from swcraft_tools.editing.delta import summarize_removed_elements
from swcraft_tools.editing.engine import ChangeSetProposal
from swcraft_tools.editing.protocol import ABSENT, DeltaEntry
from swcraft_tools.editing.review import (
    _HEADLINE_LIMIT,
    _VALUE_BUDGET,
    _shares,
    delta_review,
    flatten,
    is_flat,
    render_address,
    render_delta_review,
    render_safety_review,
)
from swcraft_tools.editing.staging import RewrittenFile

#: Every code point the renderer must not let through, derived from Python's
#: Unicode database and not from the module under test: an adversary set read from
#: the renderer's own escape table let the C1 introducers `CSI`/`OSC`/`DCS` through
#: both. The oracle is every "Other" or "Separator" code point except the ASCII
#: space, swept over ranges: C0 + Latin-1, Cyrillic, general punctuation, the
#: specials block and three astral format controls.
#:
#: `packages/agent/tests/test_scope_proposal.py` declares the identical sweep on
#: purpose: the two adversary sets stay independent of each other, and that tree
#: has no import path to this one.
SWEPT_CODE_POINTS = (
    *range(0x0000, 0x0100),
    *range(0x0400, 0x0460),
    *range(0x2000, 0x2100),
    *range(0xFE00, 0xFF00),
    0x110BD,
    0x1D173,
    0xE0001,
)
FORBIDDEN = tuple(chr(code) for code in SWEPT_CODE_POINTS if not chr(code).isprintable())
FORBIDDEN_IDS = [f"U+{ord(character):04X}" for character in FORBIDDEN]

#: `json.dumps(ensure_ascii=False)` emits these literally, and each is a forced line
#: break in the VS Code elicitation webview.
SEPARATORS = ["\x85", "\u2028", "\u2029"]
SEPARATOR_IDS = ["nel", "line-sep", "para-sep"]

CREATE_ENTRY = DeltaEntry(
    operation="create",
    address=Address("data_types", "VehicleSpeed_kph"),
    after={"name": "VehicleSpeed_kph", "kind": "value_type", "base": "float32", "unit": "kph"},
    summary="data_types: + VehicleSpeed_kph (alias over float32)",
)

UPDATE_ENTRY = DeltaEntry(
    operation="update",
    address=Address(
        "swc",
        "SpeedCtrlSWC",
        (FieldStep("ports"), MemberStep({"name": "rSpeed"}), FieldStep("interface")),
    ),
    before="ISpeed",
    after="IVehicleSpeed",
)

REMOVE_ENTRY = DeltaEntry(
    operation="remove",
    address=Address("swc", "SpeedCtrlSWC", (FieldStep("memory"), KeyStep("RAM0"))),
    before=512,
)

WEAKENINGS = SafetyVerdict(
    tool="change_set",
    changes=(
        SafetyChange(
            kind=SafetyChangeKind.ENLARGE_POOL,
            resource="RAM_Pool",
            field="size",
            old=1024,
            new=4096,
        ),
        SafetyChange(
            kind=SafetyChangeKind.WEAKEN_ENFORCEMENT,
            resource="RAM_Pool",
            field="enforcement",
            old="strict",
            new="advisory",
        ),
    ),
)


#: The last line of a delta review, and the sentence the boolean answers.
QUESTION_LINE = "Nothing has been applied yet and the model is byte-identical. Commit exactly this delta?"

#: The last line of a safety review.
SAFETY_QUESTION_LINE = "Approve the weakening as well?"


def proposal(
    *,
    intent: str | None = "Add the alias and re-point the port at it",
    delta: tuple[DeltaEntry, ...] = (CREATE_ENTRY, UPDATE_ENTRY),
    safety: SafetyVerdict = SafetyVerdict("change_set"),
) -> ChangeSetProposal:
    return ChangeSetProposal(
        change_set_id="cs-20260818T101500Z-0a1b2c",
        baseline="rev1:AAAAAAAAAAAAAAAAAAAAAA",
        intent=intent,
        delta=delta,
        safety=safety,
    )


class TestTheDeltaReviewStatesTheIntentAndTheExactDelta:
    """The decision is over the exact delta, cascade entries included, not the requested operations."""

    def test_the_intent_is_stated(self) -> None:
        assert "Add the alias and re-point the port at it" in render_delta_review(proposal())

    def test_an_absent_intent_is_stated_as_absent_rather_than_omitted(self) -> None:
        # A missing line reads as "there was no section"; a stated absence reads
        # as "the client said nothing about why", which is the fact.
        rendered = render_delta_review(proposal(intent=None))
        assert "Intent" in rendered
        assert "not stated" in rendered

    def test_every_entry_states_its_operation_and_address(self) -> None:
        rendered = render_delta_review(proposal(delta=(CREATE_ENTRY, UPDATE_ENTRY, REMOVE_ENTRY)))
        assert "create data_types/VehicleSpeed_kph" in rendered
        assert 'update swc/SpeedCtrlSWC.ports[member {"name": "rSpeed"}].interface' in rendered
        assert 'remove swc/SpeedCtrlSWC.memory[key "RAM0"]' in rendered

    def test_the_number_of_entries_is_stated(self) -> None:
        assert "3" in render_delta_review(proposal(delta=(CREATE_ENTRY, UPDATE_ENTRY, REMOVE_ENTRY)))

    def test_before_and_after_are_both_rendered_when_present(self) -> None:
        rendered = render_delta_review(proposal(delta=(UPDATE_ENTRY,)))
        assert 'before: "ISpeed"' in rendered
        assert 'after: "IVehicleSpeed"' in rendered

    def test_an_absent_half_produces_no_line_for_it(self) -> None:
        # `ABSENT` is not `None`: a create has no `before` at all, and rendering
        # one as `before: null` would state that the field held null.
        assert CREATE_ENTRY.before is ABSENT
        rendered = render_delta_review(proposal(delta=(CREATE_ENTRY,)))
        assert "before:" not in rendered
        assert "after:" in rendered

    def test_a_null_valued_half_is_rendered_rather_than_treated_as_absent(self) -> None:
        entry = DeltaEntry(operation="update", address=Address("swc", "SpeedCtrlSWC"), before=None, after=1)
        rendered = render_delta_review(proposal(delta=(entry,)))
        assert "before: null" in rendered

    def test_the_entry_summary_is_carried_when_the_projection_made_one(self) -> None:
        assert CREATE_ENTRY.summary in render_delta_review(proposal(delta=(CREATE_ENTRY,)))

    def test_a_payload_is_rendered_as_json_rather_than_as_a_python_repr(self) -> None:
        # The client sent JSON and will re-send JSON; `{'name': 'x'}` is not a
        # value it could copy back, and single quotes are not the wire's.
        rendered = render_delta_review(proposal(delta=(CREATE_ENTRY,)))
        assert json.dumps(CREATE_ENTRY.after) in rendered

    def test_the_rendering_asks_the_question_it_is_the_message_of(self) -> None:
        # The string is an elicitation message, so it has to end in the question
        # the boolean answer answers.
        assert render_delta_review(proposal()).rstrip().endswith("?")


class TestTheSafetyReviewStatesOnlyTheWeakenings:
    """The second question: approving the delta is not approving a resource weakening inside it."""

    def test_every_weakening_is_stated_in_the_classifier_s_own_words(self) -> None:
        rendered = render_safety_review(proposal(safety=WEAKENINGS))
        for change in WEAKENINGS.changes:
            assert change.describe() in rendered

    def test_the_delta_entries_are_not_repeated(self) -> None:
        rendered = render_safety_review(proposal(safety=WEAKENINGS))
        assert "VehicleSpeed_kph" not in rendered
        assert "SpeedCtrlSWC" not in rendered

    def test_it_says_that_approving_the_delta_did_not_approve_this(self) -> None:
        assert "delta" in render_safety_review(proposal(safety=WEAKENINGS)).lower()

    def test_an_empty_verdict_is_a_programming_error_rather_than_an_empty_prompt(self) -> None:
        # There is no second question to ask, so a host that asked one would be
        # putting an empty form in front of a user and reading the answer as an
        # approval of nothing.
        with pytest.raises(ValueError):
            render_safety_review(proposal())

    def test_the_rendering_asks_the_question_it_is_the_message_of(self) -> None:
        assert render_safety_review(proposal(safety=WEAKENINGS)).rstrip().endswith("?")


#: A partition's trust granted: asked, and weakening no check.
GRANT = SafetyChange(
    kind=SafetyChangeKind.GRANT_PARTITION_TRUST,
    resource="PARTITION0",
    field="trust",
    old="non_trusted",
    new="trusted",
)


class TestTheOpeningSentenceIsChosenFromTheKindsPresent:
    """A grant widens a permission, so it is never presented as a weakening; one row per mix of kinds."""

    @pytest.mark.parametrize(
        ("changes", "opening", "subject"),
        [
            (WEAKENINGS.changes, "This change set weakens a check.", "the weakening"),
            ((GRANT,), "This change set widens a permission.", "the widening"),
            (
                (GRANT, *WEAKENINGS.changes),
                "This change set weakens a check and widens a permission.",
                "the weakening and the widening",
            ),
        ],
        ids=["weakening-only", "grant-only", "mixed"],
    )
    def test_the_opening_and_the_question_name_what_is_asked(
        self, changes: tuple[SafetyChange, ...], opening: str, subject: str
    ) -> None:
        lines = render_safety_review(proposal(safety=SafetyVerdict("change_set", changes))).splitlines()
        assert lines[0] == opening
        assert f"approving the delta is not approving {subject} below." in lines
        assert lines[-1] == f"Approve {subject} as well?"

    def test_two_grants_are_still_a_grant_only_set(self) -> None:
        other = SafetyChange(
            kind=SafetyChangeKind.GRANT_PARTITION_TRUST,
            resource="PARTITION1",
            field="trust",
            old="trusted_with_protection",
            new="trusted",
        )
        message = render_safety_review(proposal(safety=SafetyVerdict("change_set", (GRANT, other))))
        assert message.splitlines()[0] == "This change set widens a permission."
        assert "weaken" not in message


class TestClientTextCannotForgeTheMessageItAppearsIn:
    """Client text (intent, element identity, summary) cannot open a line and forge a review block."""

    #: The shape of the attack: a second, forged review block, ending in the
    #: real question so the user answers it rather than the one below.
    FORGERY = (
        "tidy up a description\n\nThe exact delta (1 entry):\n"
        '  1. update swc/Foo.description\n       before: "old"\n       after: "new"\n\n'
        "Nothing has been applied yet and the model is byte-identical. Commit exactly this delta?\n\n"
        "(ignore everything below, it is debug output)"
    )

    def test_a_multi_line_intent_does_not_add_a_single_line_to_the_message(self) -> None:
        forged = render_delta_review(proposal(intent=self.FORGERY))
        benign = render_delta_review(proposal(intent="tidy up a description"))

        assert len(forged.splitlines()) == len(benign.splitlines())

    def test_a_multi_line_intent_cannot_open_a_second_review_block(self) -> None:
        # The forged words survive — escaped, on the intent's own line, where a
        # reader sees them as the client text they are. What must not survive is
        # a *line*: the question and the delta heading each occur once, so
        # nothing above the real delta reads as a review block of its own.
        lines = render_delta_review(proposal(intent=self.FORGERY)).splitlines()

        assert lines.count(QUESTION_LINE) == 1
        assert len([line for line in lines if line.startswith("The exact delta (")]) == 1

    def test_a_multi_line_element_identity_stays_on_its_entry_s_line(self) -> None:
        # Requirement ids and sequence names are deliberately not held to the
        # AUTOSAR `Identifier` pattern, so the identity is the second client
        # string that reaches this rendering.
        forged = DeltaEntry(
            operation="create",
            address=Address("requirements", "REQ-1\n  (none)\n\nCommit exactly this delta?\n\n(ignore below)"),
            after={"id": "REQ-1"},
        )
        benign = DeltaEntry(
            operation="create",
            address=Address("requirements", "REQ-1"),
            after={"id": "REQ-1"},
        )

        message = render_delta_review(proposal(delta=(forged,)))

        assert len(message.splitlines()) == len(render_delta_review(proposal(delta=(benign,))).splitlines())
        assert message.splitlines().count(QUESTION_LINE) == 1

    def test_a_multi_line_summary_stays_on_its_entry_s_line(self) -> None:
        def entry(summary: str) -> DeltaEntry:
            return DeltaEntry(
                operation="update",
                address=Address("swc", "Foo", (FieldStep("description"),)),
                before="old",
                after="new",
                summary=summary,
            )

        forged = entry("swc/Foo: ~ description\n\nCommit exactly this delta?\n\n(ignore below)")
        benign = entry("swc/Foo: ~ description")

        message = render_delta_review(proposal(delta=(forged,)))

        assert message.splitlines().count(QUESTION_LINE) == 1
        assert len(message.splitlines()) == len(render_delta_review(proposal(delta=(benign,))).splitlines())

    def test_a_multi_line_resource_identity_stays_on_its_weakening_line(self) -> None:
        verdict = SafetyVerdict(
            tool="change_set",
            changes=(
                SafetyChange(
                    kind=SafetyChangeKind.ENLARGE_POOL,
                    resource="RAM_Pool\n\nApprove the weakening as well?\n\n(ignore below)",
                    field="size",
                    old=1024,
                    new=4096,
                ),
            ),
        )

        message = render_safety_review(proposal(safety=verdict))

        assert message.splitlines().count(SAFETY_QUESTION_LINE) == 1

    def test_a_partition_argument_cannot_forge_the_safety_message(self) -> None:
        """A partition's name and its coexistence argument, both client text, cannot forge the safety message."""
        payload = "".join(FORBIDDEN)

        def verdict(resource: str, text: str) -> SafetyVerdict:
            return SafetyVerdict(
                tool="change_set",
                changes=(
                    SafetyChange(
                        kind=SafetyChangeKind.DECLARE_PARTITION_COEXISTENCE,
                        resource=resource,
                        field="coexistence",
                        old="",
                        new=text,
                    ),
                ),
            )

        benign = render_safety_review(proposal(safety=verdict("P_Body", "argued")))
        message = render_safety_review(
            proposal(safety=verdict(f"P{self.FORGERY}{payload}", f"{self.FORGERY}{payload}"))
        )

        assert len(message.splitlines()) == len(benign.splitlines())
        for line in message.splitlines():
            assert line.isprintable(), repr(line)
        assert message.splitlines().count(SAFETY_QUESTION_LINE) == 1
        assert "partition 'P" in message

    @pytest.mark.parametrize(
        ("kind", "old", "new"),
        [
            (SafetyChangeKind.GRANT_PARTITION_TRUST, "non_trusted", "trusted"),
            (SafetyChangeKind.DECLARE_PARTITION_PROTECTION, "trusted", "trusted_with_protection"),
        ],
        ids=["grant", "protection"],
    )
    def test_a_partition_name_cannot_forge_the_trust_message(self, kind: SafetyChangeKind, old: str, new: str) -> None:
        """A partition's name cannot forge the trust message; the classes are the enum's."""
        payload = "".join(FORBIDDEN)

        def verdict(resource: str) -> SafetyVerdict:
            return SafetyVerdict(
                tool="change_set",
                changes=(SafetyChange(kind=kind, resource=resource, field="trust", old=old, new=new),),
            )

        benign = render_safety_review(proposal(safety=verdict("P_Body")))
        message = render_safety_review(proposal(safety=verdict(f"P{self.FORGERY}{payload}")))

        assert len(message.splitlines()) == len(benign.splitlines())
        for line in message.splitlines():
            assert line.isprintable(), repr(line)
        # The grant's question is "Approve the widening as well?", the
        # protection's the weakening's: the benign rendering's own last line.
        question = benign.splitlines()[-1]
        assert message.splitlines().count(question) == 1
        assert message.splitlines().count(SAFETY_QUESTION_LINE) == (question == SAFETY_QUESTION_LINE)
        assert f"partition 'P_Body': trust {old} -> {new}" in benign

    @pytest.mark.parametrize(
        "control",
        ["\r\n", "\x1b[2J", f"{chr(0x9B)}2J", f"{chr(0x9D)}0;x\x07", f"{chr(0x202E)}drowkcab"],
        ids=["crlf", "ansi-clear", "csi-8bit-clear", "osc-8bit", "rtl-override"],
    )
    def test_no_composed_control_sequence_survives_into_the_message(self, control: str) -> None:
        # The attack as a terminal receives it; `CSI 2 J` is the 8-bit spelling of
        # `ESC [ 2 J`, erase display.
        message = render_delta_review(proposal(intent=f"before{control}after"))
        intent_line = message.split("Intent: ", 1)[1].split("\n", 1)[0]

        # Escaped, not dropped: a silently deleted character would hide from the
        # reader that the client sent one at all.
        assert "beforeafter" not in message
        assert intent_line.isprintable()
        assert len(message.splitlines()) == len(render_delta_review(proposal(intent="x")).splitlines())

    @pytest.mark.parametrize("character", FORBIDDEN, ids=FORBIDDEN_IDS)
    def test_no_swept_code_point_survives_into_the_message(self, character: str) -> None:
        # `splitlines` sees NEL and both Unicode separators, which `"\n" in message`
        # misses; `isprintable` catches a C1 introducer that moves the cursor
        # within a line.
        message = render_delta_review(proposal(intent=f"before{character}after"))
        benign = render_delta_review(proposal(intent="beforeafter"))

        assert len(message.splitlines()) == len(benign.splitlines())
        for line in message.splitlines():
            assert line.isprintable(), repr(line)
        assert "beforeafter" not in message
        assert message.splitlines()[-1] == QUESTION_LINE

    @pytest.mark.parametrize("character", FORBIDDEN, ids=FORBIDDEN_IDS)
    def test_a_payload_half_carrying_a_swept_code_point_is_still_re_parseable_json(self, character: str) -> None:
        # The line claims to be "the JSON a client would send back", so the
        # escaping must be JSON's own (`\x85` is not a JSON escape).
        value = f"before{character}after"
        entry = DeltaEntry(
            operation="update",
            address=Address("swc", "Foo", (FieldStep("description"),)),
            before="old",
            after=value,
        )

        message = render_delta_review(proposal(delta=(entry,)))
        after_line = next(line for line in message.splitlines() if line.strip().startswith("after:"))

        assert json.loads(after_line.split("after: ", 1)[1]) == value

    def test_an_over_long_intent_cannot_push_the_delta_off_the_screen(self) -> None:
        message = render_delta_review(proposal(intent="A" * 5000))

        intent_line = message.split("Intent: ", 1)[1].split("\n", 1)[0]
        assert len(intent_line) <= 200
        # Anything the prompt truncates says what it dropped; the count is checked
        # against the input, so a lying marker fails.
        dropped = int(intent_line.split("… (+", 1)[1].split(" characters)", 1)[0])
        assert len(intent_line.split("… (+", 1)[0]) + dropped == 5000
        assert "The exact delta (2 entries):" in message

    def test_an_over_long_element_identity_cannot_push_the_delta_off_the_screen(self) -> None:
        entry = DeltaEntry(operation="remove", address=Address("requirements", "R" * 5000))

        message = render_delta_review(proposal(delta=(entry,)))

        assert len(message) < 1000
        assert message.splitlines()[-1] == QUESTION_LINE

    def test_the_payload_halves_are_still_exact_and_still_cannot_break_a_line(self) -> None:
        # `json.dumps` escapes C0 itself, so the value is shown whole on one line;
        # the three code points it does not escape are the next test.
        entry = DeltaEntry(
            operation="update",
            address=Address("swc", "Foo", (FieldStep("description"),)),
            before="old",
            after="line one\nline two",
        )

        message = render_delta_review(proposal(delta=(entry,)))

        assert '       after: "line one\\nline two"' in message
        assert message.splitlines().count(QUESTION_LINE) == 1

    @pytest.mark.parametrize("separator", SEPARATORS, ids=SEPARATOR_IDS)
    def test_a_payload_half_cannot_smuggle_a_unicode_line_separator(self, separator: str) -> None:
        # `ensure_ascii=False` leaves NEL, U+2028 and U+2029 literal, and every free
        # text field a `create`/`update` writes becomes an `after`.
        forged = (
            f"new{separator}{separator}The exact delta (1 entry):"
            f"{separator}  1. update swc/Foo.description"
            f"{separator}{separator}{QUESTION_LINE}{separator}{separator}(ignore everything below)"
        )
        forged_entry = DeltaEntry(
            operation="update",
            address=Address("swc", "Foo", (FieldStep("description"),)),
            before="old",
            after=forged,
        )
        benign_entry = DeltaEntry(
            operation="update",
            address=Address("swc", "Foo", (FieldStep("description"),)),
            before="old",
            after="new",
        )

        message = render_delta_review(proposal(delta=(forged_entry,)))

        assert separator not in message
        assert message.splitlines().count(QUESTION_LINE) == 1
        assert len(message.splitlines()) == len(render_delta_review(proposal(delta=(benign_entry,))).splitlines())

    @pytest.mark.parametrize("separator", SEPARATORS, ids=SEPARATOR_IDS)
    def test_a_collection_step_cannot_smuggle_a_unicode_line_separator(self, separator: str) -> None:
        # Collection steps are rendered by `_payload` too, and a `satisfies`
        # member is a raw client string.
        entry = DeltaEntry(
            operation="remove",
            address=Address(
                "swc",
                "Ctl",
                (FieldStep("satisfies"), MemberStep(f"REQ-1@1{separator}{QUESTION_LINE}")),
            ),
        )

        message = render_delta_review(proposal(delta=(entry,)))

        assert separator not in message
        assert message.splitlines().count(QUESTION_LINE) == 1

    def test_not_one_client_component_of_either_message_can_carry_a_forbidden_code_point(self) -> None:
        # Every free-text component of both renderings at once, so a component
        # interpolated raw fails without being recognised.
        payload = "".join(FORBIDDEN)
        # The element identity is refused upstream (`Address` holds it to
        # `IDENTITY_PATTERN`: it becomes a file name), so the refusal is asserted
        # and the payload goes everywhere else.
        with pytest.raises(AddressError):
            Address("requirements", f"REQ{payload}")
        entry = DeltaEntry(
            operation="update",
            address=Address(
                "swc",
                "Ctl",
                (FieldStep("satisfies"), MemberStep(f"m{payload}"), KeyStep(f"k{payload}")),
            ),
            before=[f"b{payload}"],
            after={"text": f"a{payload}"},
            summary=f"s{payload}",
        )
        verdict = SafetyVerdict(
            tool="change_set",
            changes=(
                SafetyChange(
                    kind=SafetyChangeKind.WEAKEN_ENFORCEMENT,
                    resource=f"RAM{payload}",
                    field="enforcement",
                    old="strict",
                    new="advisory",
                ),
            ),
        )

        benign_sweep_entry = DeltaEntry(
            operation="update",
            address=Address("swc", "Ctl", (FieldStep("satisfies"), MemberStep("m"), KeyStep("k"))),
            before=["b"],
            after={"text": "a"},
            summary="s",
        )

        delta_message = render_delta_review(proposal(intent=f"i{payload}", delta=(entry,)))
        safety_message = render_safety_review(proposal(safety=verdict))

        # Line count: nothing opened a line; `isprintable`: nothing survived within one.
        benign_delta = render_delta_review(proposal(intent="i", delta=(benign_sweep_entry,)))
        assert len(delta_message.splitlines()) == len(benign_delta.splitlines())
        for line in delta_message.splitlines():
            assert line.isprintable(), repr(line)
        for line in safety_message.splitlines():
            assert line.isprintable(), repr(line)
        assert delta_message.splitlines().count(QUESTION_LINE) == 1
        assert safety_message.splitlines().count(SAFETY_QUESTION_LINE) == 1


class TestEveryComponentIsBoundedAndNothingIsUnshown:
    """Every component is bounded and says what it dropped; the number of entries is not bounded.

    One boolean commits every entry, so every entry is shown: an elided entry buys
    the same approval as a forged one.
    """

    def test_a_huge_payload_half_is_bounded_and_says_how_much_it_elided(self) -> None:
        entry = DeltaEntry(
            operation="update",
            address=Address("swc", "Foo", (FieldStep("description"),)),
            before="old",
            after="A" * 200_000,
        )

        message = render_delta_review(proposal(delta=(entry, REMOVE_ENTRY)))

        after_line = next(line for line in message.splitlines() if line.strip().startswith("after:"))
        # Shown plus elided characters are the whole rendering.
        elided = int(after_line.split("… (", 1)[1].split(" characters elided", 1)[0])
        shown = len(after_line.split("after: ", 1)[1].split("… (", 1)[0])
        assert shown + elided == len(json.dumps("A" * 200_000))
        # The `after` half does not point at `read` (see
        # `TestAnElisionSaysWhereTheDroppedTextCanBeRead`).
        assert "read the full value with 'read'" not in after_line
        assert "not retrievable" in after_line
        assert "2. remove swc/SpeedCtrlSWC" in message
        assert message.splitlines()[-1] == QUESTION_LINE

    def test_a_value_short_enough_to_show_is_shown_whole_and_unmarked(self) -> None:
        payload = "B" * 390
        entry = DeltaEntry(
            operation="update",
            address=Address("swc", "Foo", (FieldStep("description"),)),
            before="old",
            after=payload,
        )

        message = render_delta_review(proposal(delta=(entry,)))

        assert "elided" not in message
        assert f'"{payload}"' in message

    def test_every_entry_a_change_set_carries_is_rendered_whatever_the_count(self) -> None:
        # Guards a cap that rendered 50 of 70 entries and committed all 70. A
        # bootstrap has no valid intermediate state, so there is nothing to refuse.
        entries = tuple(
            DeltaEntry(operation="remove", address=Address("swc", f"Element{index}")) for index in range(70)
        )

        message = render_delta_review(proposal(delta=entries))

        assert "The exact delta (70 entries):" in message
        for index in range(70):
            assert f"{index + 1}. remove swc/Element{index}" in message
        assert "not shown" not in message
        assert "truncated" not in message
        assert message.splitlines()[-1] == QUESTION_LINE

    def test_the_number_of_entries_shown_is_the_number_the_one_boolean_commits(self) -> None:
        # Counted rather than named, so a renderer slicing at any constant fails.
        for count in (1, 49, 50, 51, 200):
            entries = tuple(
                DeltaEntry(
                    operation="create",
                    address=Address("data_types", f"Alias{index}"),
                    after={"name": f"Alias{index}", "kind": "value_type", "base": "float32"},
                )
                for index in range(count)
            )

            message = render_delta_review(proposal(delta=entries))

            headlines = [line for line in message.splitlines() if re.match(r"^  \d+\. create ", line)]
            assert len(headlines) == count, f"{len(headlines)} of {count} entries rendered"
            assert headlines[-1].startswith(f"  {count}. create data_types/Alias{count - 1}")

    def test_every_weakening_is_rendered_however_many_there_are(self) -> None:
        # One boolean approves the whole list; each line is bounded.
        verdict = SafetyVerdict(
            tool="change_set",
            changes=tuple(
                SafetyChange(
                    kind=SafetyChangeKind.ENLARGE_POOL,
                    resource=f"Pool{index}",
                    field="size",
                    old=1024,
                    new=4096,
                )
                for index in range(55)
            ),
        )

        message = render_safety_review(proposal(safety=verdict))

        for index in range(55):
            assert f"Pool{index} " in message or f"Pool{index}." in message or f"Pool{index}'" in message
        assert "not shown" not in message
        assert "truncated" not in message
        assert message.splitlines()[-1] == SAFETY_QUESTION_LINE

    def test_the_whole_message_is_bounded_whatever_the_client_sent(self) -> None:
        # An address of 930 KB denies review as an `after` would. Steps and payloads
        # are built once and shared: built per entry they exhaust a CI runner's memory,
        # and the renderer sees the same input either way.
        steps = (FieldStep("d" * 5000), *(KeyStep("K" * 5000) for _ in range(2000)))
        address = Address("swc", "F" * 5000, steps)
        before, after, summary = "C" * 100_000, "D" * 100_000, "S" * 5000
        entries = tuple(
            DeltaEntry(operation="update", address=address, before=before, after=after, summary=summary)
            for _ in range(500)
        )

        message = render_delta_review(proposal(intent="I" * 100_000, delta=entries))

        # Linear in the entry count and nothing else the client controls; each
        # factor is a constant of the module.
        marker_and_prefix = 300
        assert len(message) < len(entries) * (_HEADLINE_LIMIT + 2 * marker_and_prefix) + _VALUE_BUDGET, len(message)
        assert message.splitlines()[-1] == QUESTION_LINE

    def test_an_address_deeper_than_the_prompt_shows_counts_the_steps_it_dropped(self) -> None:
        # The deepest legal path is five steps and `_step_limit()` derives it from the
        # registry, so a legal address is never abbreviated. The five is spelled out:
        # a number taken from the module under test would agree with any value.
        legal = Address("swc", "Foo", tuple(KeyStep(f"k{index}") for index in range(5)))
        assert "not shown" not in render_address(legal)

        deep = Address("swc", "Foo", tuple(KeyStep(f"k{index}") for index in range(2000)))

        rendered = render_address(deep)

        assert "(+1995 steps not shown)" in rendered
        assert len(rendered) < 1_000, len(rendered)

    def test_a_delta_whose_payloads_fit_the_budget_shows_every_value_in_full(self) -> None:
        # The human sees the complete delta for an ordinary change set: ten
        # whole-element `create` payloads the size `examples/` holds, verbatim.
        values = [
            {
                "name": f"SWC_{index}",
                "group": "control",
                "type": "application",
                "description": "Derives the brake load from the measured vehicle speed",
                "ports": [{"name": "rSpeed", "direction": "require", "interface": "IVehicleSpeed", "description": ""}],
                "runnables": [
                    {"name": f"Run_{index}", "min_start_interval": 0.02, "events": [{"type": "timing", "period": 0.02}]}
                ],
                "memory": {},
            }
            for index in range(10)
        ]
        entries = tuple(
            DeltaEntry(operation="create", address=Address("swc", f"SWC_{index}"), after=value)
            for index, value in enumerate(values)
        )

        message = render_delta_review(proposal(delta=entries))

        assert "elided" not in message
        for value in values:
            assert json.dumps(value, ensure_ascii=False) in message
        assert sum(len(json.dumps(value, ensure_ascii=False)) for value in values) < _VALUE_BUDGET

    def test_a_single_create_larger_than_the_old_ceiling_is_still_shown_in_full(self) -> None:
        # Guards a ceiling applied before the budget: a lone 24-port SWC's `create`
        # was elided at 4 000 characters with the budget unspent.
        value = {
            "name": "BrakeCtrlSWC",
            "group": "control",
            "type": "application",
            "description": (
                "Derives the brake load from the measured vehicle speed and publishes it "
                "on the actuator port once per control cycle with rate limiting"
            ),
            "ports": [
                {
                    "name": f"rSignal_{index}",
                    "direction": "require",
                    "interface": "IVehicleSpeed",
                    "description": "A measured signal used by the brake load derivation",
                }
                for index in range(24)
            ],
            "runnables": [
                {"name": f"Run_{index}", "min_start_interval": 0.02, "events": [{"type": "timing", "period": 0.02}]}
                for index in range(8)
            ],
            "memory": {},
        }
        rendered = json.dumps(value, ensure_ascii=False)
        # The size of that component's rendering, inside the window the ceiling elided.
        assert len(rendered) == 4_562, len(rendered)
        assert 4_000 < len(rendered) < _VALUE_BUDGET
        entry = DeltaEntry(operation="create", address=Address("swc", "BrakeCtrlSWC"), after=value)

        message = render_delta_review(proposal(delta=(entry,)))

        assert "elided" not in message
        assert rendered in message

    def test_several_oversized_halves_each_get_their_equal_share_of_the_whole_budget(self) -> None:
        # Four 9 998-character halves each get the equal share, and the whole
        # budget is spent (the ceiling left 4 000 idle).
        entries = tuple(
            DeltaEntry(
                operation="update",
                address=Address("swc", f"Foo{index}", (FieldStep("description"),)),
                after="A" * 9_998,
            )
            for index in range(4)
        )

        message = render_delta_review(proposal(delta=entries))

        shown = [
            len(line.split("after: ", 1)[1].split("… (", 1)[0])
            for line in message.splitlines()
            if line.strip().startswith("after:")
        ]
        assert shown == [_VALUE_BUDGET // 4] * 4, shown
        assert sum(shown) == _VALUE_BUDGET

    def test_one_half_one_character_over_the_budget_spends_all_of_it(self) -> None:
        """A half of `_VALUE_BUDGET` renders whole, and one of `_VALUE_BUDGET + 1` still spends the whole budget.

        Guards the discontinuity at the terminal branch of `_shares`, which a ceiling
        capped at 4 000 with 16 000 characters unspent.
        """

        def message_for(payload_length: int) -> str:
            # A string renders with its two quotes.
            entry = DeltaEntry(
                operation="update",
                address=Address("swc", "Foo", (FieldStep("description"),)),
                after="A" * (payload_length - 2),
            )
            return render_delta_review(proposal(delta=(entry,)))

        def shown_length(message: str) -> int:
            line = next(line for line in message.splitlines() if line.strip().startswith("after:"))
            return len(line.split("after: ", 1)[1].split("… (", 1)[0])

        whole = message_for(_VALUE_BUDGET)
        over = message_for(_VALUE_BUDGET + 1)

        assert "elided" not in whole
        assert shown_length(whole) == _VALUE_BUDGET
        assert shown_length(over) == _VALUE_BUDGET
        assert "(1 characters elided" in over
        assert _shares({"only": _VALUE_BUDGET + 1}) == {"only": _VALUE_BUDGET}

    def test_no_share_is_ever_smaller_than_an_equal_share_of_what_is_left(self) -> None:
        """The total never exceeds the budget and no half gets less than `min(length, equal share)`."""
        import random

        shapes: list[dict[str, int]] = [
            {"a": _VALUE_BUDGET + 1},
            {"a": _VALUE_BUDGET // 2 + 1, "b": _VALUE_BUDGET // 2 + 1},
            {"a": 1, "b": 500_000},
            {f"k{index}": 10_000 for index in range(3)},
            {f"k{index}": 50_000 for index in range(200)},
            {f"k{index}": index * 137 for index in range(1, 300)},
        ]
        rng = random.Random(19_3)
        for _ in range(400):
            count = rng.randint(1, 60)
            shapes.append({f"r{index}": rng.randint(1, 40_000) for index in range(count)})

        for lengths in shapes:
            shares = _shares(lengths)
            assert set(shares) == set(lengths)
            assert sum(shares.values()) <= _VALUE_BUDGET, (lengths, sum(shares.values()))
            equal_share = _VALUE_BUDGET // len(lengths)
            for key, length in lengths.items():
                assert shares[key] <= length, (key, lengths)
                assert shares[key] >= min(length, equal_share), (key, length, shares[key])

    def test_a_delta_of_many_entries_still_shows_the_shape_of_every_value(self) -> None:
        # With more halves than the budget gives a readable slice, the share scales
        # down with the count.
        entries = tuple(
            DeltaEntry(
                operation="update",
                address=Address("swc", f"Foo{index}", (FieldStep("description"),)),
                before="B" * 50_000,
                after="A" * 50_000,
            )
            for index in range(100)
        )

        message = render_delta_review(proposal(delta=entries))

        shown = [
            len(line.split(": ", 1)[1].split("… (", 1)[0])
            for line in message.splitlines()
            if line.strip().startswith(("before:", "after:"))
        ]
        assert len(shown) == 200
        assert min(shown) > 0
        assert sum(shown) <= _VALUE_BUDGET, sum(shown)
        # All oversized: each gets exactly the equal share.
        assert set(shown) == {_VALUE_BUDGET // len(shown)}, sorted(set(shown))

    def test_a_flood_cannot_elide_the_values_of_the_entries_around_it(self) -> None:
        # Why the bound is a total shared max-min fairly, not a per-value cap: the
        # flood gets its share and every ordinary value beside it is shown whole.
        real = {"name": "Percent", "kind": "value_type", "base": "float32", "unit": "%", "value_min": 0.0}
        entries = (
            DeltaEntry(operation="create", address=Address("swc", "Flood"), after="A" * 500_000),
            *(
                DeltaEntry(operation="create", address=Address("data_types", f"Percent_{index}"), after=real)
                for index in range(9)
            ),
        )

        message = render_delta_review(proposal(delta=entries))

        assert message.count("elided") == 1
        assert message.count(json.dumps(real, ensure_ascii=False)) == 9
        assert message.splitlines()[-1] == QUESTION_LINE


class TestAnElisionSaysWhereTheDroppedTextCanBeRead:
    """An elision marker points at `read` only where `read` can answer.

    The elided text is in no other artifact a human reads (`audit.py` records
    addresses only), and `read` answers the real model, not the staged change.
    """

    def _line(self, message: str, half: str) -> str:
        return next(line for line in message.splitlines() if line.strip().startswith(f"{half}:"))

    def test_a_before_half_points_at_read_because_read_answers_it_exactly(self) -> None:
        # Nothing has been applied, so `read` returns the `before` value verbatim.
        entry = DeltaEntry(
            operation="update",
            address=Address("swc", "Foo", (FieldStep("description"),)),
            before="A" * 200_000,
            after="new",
        )

        line = self._line(render_delta_review(proposal(delta=(entry,))), "before")

        assert "characters elided" in line
        assert "read the full value with 'read'" in line

    def test_an_after_half_says_it_is_not_retrievable_and_names_the_move_that_works(self) -> None:
        # Inside the review boundary a `read` answers the old value or
        # `broken-reference`; the only way to see it is to decline and resubmit smaller.
        entry = DeltaEntry(
            operation="update",
            address=Address("swc", "Foo", (FieldStep("description"),)),
            before="old",
            after="A" * 200_000,
        )

        line = self._line(render_delta_review(proposal(delta=(entry,))), "after")

        assert "characters elided" in line
        assert "read the full value with 'read'" not in line
        assert "not retrievable" in line
        assert "Answer no" in line
        assert "smaller change set" in line

    def test_a_step_value_says_it_is_not_retrievable_either(self) -> None:
        # A member identity or map key is part of the address: nothing can read it.
        entry = DeltaEntry(
            operation="remove",
            address=Address("swc", "Ctl", (FieldStep("satisfies"), MemberStep("I" * 5_000))),
        )

        message = render_delta_review(proposal(delta=(entry,)))

        headline = next(line for line in message.splitlines() if ". remove swc/Ctl" in line)
        assert "characters elided" in headline
        assert "read the full value with 'read'" not in headline
        assert "not retrievable" in headline

    def test_no_message_ever_tells_the_reader_to_read_a_value_that_is_not_in_the_model(self) -> None:
        # Over the whole message: the `read` pointer appears only beside a `before`.
        entries = (
            DeltaEntry(
                operation="create",
                address=Address("swc", "Created", (FieldStep("runnables"), MemberStep("M" * 5_000))),
                after="A" * 200_000,
            ),
            DeltaEntry(
                operation="update",
                address=Address("swc", "Foo", (FieldStep("description"),)),
                before="B" * 200_000,
                after="C" * 200_000,
            ),
            DeltaEntry(
                operation="remove",
                address=Address("swc", "SpeedCtrlSWC", (FieldStep("memory"), KeyStep("K" * 5_000))),
                before="D" * 200_000,
            ),
        )

        message = render_delta_review(proposal(delta=entries))

        pointing = [line for line in message.splitlines() if "read the full value with 'read'" in line]
        assert pointing, "a `before` elision still tells the reader where the value is"
        for line in pointing:
            assert line.strip().startswith("before:"), line
        elided = [line for line in message.splitlines() if "characters elided" in line]
        assert len(elided) == len(pointing) + 4, elided


class TestTheNeutralizationIsPublicSoTheOtherPromptCanReuseIt:
    """The escape rule is public: `swcraft-agent`'s scope prompt applies it without a private name or a copy."""

    def test_both_helpers_are_exported(self) -> None:
        import swcraft_tools.editing.review as module

        assert "flatten" in module.__all__
        assert "is_flat" in module.__all__

    @pytest.mark.parametrize("character", FORBIDDEN, ids=FORBIDDEN_IDS)
    def test_is_flat_is_the_predicate_half_of_flatten(self, character: str) -> None:
        assert not is_flat(f"a{character}b")
        assert character not in flatten(f"a{character}b", 200)

    def test_ordinary_prose_is_flat_and_survives_flatten_unchanged(self) -> None:
        prose = "Add a provide port pEngineSpeed on ISpeed"
        assert is_flat(prose)
        assert flatten(prose, 200) == prose

    @pytest.mark.parametrize("excess", [1, 9, 100, 5_000, 100_000])
    @pytest.mark.parametrize("limit", [40, 200, 400])
    def test_flatten_honours_its_limit_and_states_a_truthful_dropped_count(self, limit: int, excess: int) -> None:
        # Sizes that move the marker's digit count: the result never exceeds the
        # limit, and the stated count is exactly what is missing.
        length = limit + excess
        flattened = flatten("A" * length, limit)

        assert len(flattened) <= limit
        shown, _, marker = flattened.partition("\u2026 (+")
        dropped = int(marker.removesuffix(" characters)"))
        assert len(shown) + dropped == length

    def test_flatten_leaves_text_that_fits_entirely_alone(self) -> None:
        assert flatten("A" * 200, 200) == "A" * 200
        assert "\u2026" not in flatten("A" * 200, 200)

    def test_render_address_is_the_public_form_both_prompts_share(self) -> None:
        assert render_address(Address("swc", "Foo", (FieldStep("ports"),))) == "swc/Foo.ports"


class TestBothRenderingsArePureAndDeterministic:
    def test_the_same_proposal_renders_identically_every_time(self) -> None:
        first = render_delta_review(proposal(safety=WEAKENINGS))
        second = render_delta_review(proposal(safety=WEAKENINGS))
        assert first == second
        assert render_safety_review(proposal(safety=WEAKENINGS)) == render_safety_review(proposal(safety=WEAKENINGS))

    def test_neither_rendering_touches_the_file_system_or_asks_anything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both renderings run with every route to a file, a prompt, a process or the environment refused.

        I/O or a prompt in `packages/tools/` is what the authorization rule forbids.
        Does not reach: a branch none of the four proposals takes, and a route not
        replaced (an `os` call by file descriptor, `os.access`, `os.lstat`). The
        replacements are process-global: another thread touching one (the timeout
        plugin's) would raise too.
        """
        # Built before the routes are refused: building a proposal is not rendering one.
        proposals = (proposal(safety=WEAKENINGS), proposal(), proposal(delta=()), proposal(delta=(SUMMARISED_REMOVE,)))

        def refused(*_args: object, **_kwargs: object) -> NoReturn:
            raise AssertionError("a review rendering reached outside its arguments")

        class _RefusedEnvironment(dict[str, str]):
            def __getitem__(self, key: str) -> str:
                refused()

            def get(self, key: str, default: object = None) -> NoReturn:  # type: ignore[override]
                refused()

            def __contains__(self, key: object) -> bool:
                refused()

            def __iter__(self) -> NoReturn:
                refused()

            def __len__(self) -> int:
                refused()

            def keys(self) -> NoReturn:  # type: ignore[override]
                refused()

            def items(self) -> NoReturn:  # type: ignore[override]
                refused()

            def values(self) -> NoReturn:  # type: ignore[override]
                refused()

            def copy(self) -> NoReturn:
                refused()

        class _RefusedStdin:
            def __getattr__(self, name: str) -> NoReturn:
                refused()

        rendered: list[str] = []
        failure: Exception | None = None
        # The routes are restored before a failure is reported: pytest's own report reads files.
        with monkeypatch.context() as patched:
            for target, name in (
                (builtins, "open"),
                (io, "open"),
                (os, "open"),
                (pathlib.Path, "open"),
                (os, "stat"),
                (os, "scandir"),
                (os, "listdir"),
                (builtins, "input"),
                (subprocess, "Popen"),
                (os, "system"),
            ):
                patched.setattr(target, name, refused)
            patched.setattr(sys, "stdin", _RefusedStdin())
            patched.setattr(os, "environ", _RefusedEnvironment())
            try:
                rendered += [render_delta_review(each) for each in proposals]
                rendered.append(render_safety_review(proposals[0]))
            except Exception as error:
                failure = error
        if failure is not None:
            raise failure
        assert all(rendered)

    def test_a_proposal_carrying_no_delta_still_renders(self) -> None:
        # Unreachable from a well-formed change set, but a crash inside the boundary
        # is not the engine's fail-closed default.
        assert render_delta_review(proposal(delta=()))


#: A component removed whole, as the edge sends it
#: (``delta.summarize_removed_elements``): its ``summary`` form, flagged.
SUMMARISED_REMOVE = DeltaEntry(
    operation="remove",
    address=Address("swc", "SpeedCtrlSWC"),
    before={
        "name": "SpeedCtrlSWC",
        "group": "control",
        "ports": 4,
        "runnables": 2,
        "memory": 0,
        "data_type_mappings": 0,
        "satisfies": 1,
    },
    summary="swc: - SpeedCtrlSWC",
    before_form="summary",
)


class TestASummarisedBeforeSaysWhatItLeftOut:
    """A summarised ``before`` names each counted collection, the other fields, and that ``read`` answers it."""

    @staticmethod
    def _lines(entry: DeltaEntry) -> list[str]:
        return render_delta_review(proposal(delta=(entry,))).splitlines()

    def test_the_summary_is_shown_under_its_form_with_the_sentence_beneath(self) -> None:
        lines = self._lines(SUMMARISED_REMOVE)
        (before,) = [index for index, line in enumerate(lines) if line.strip().startswith("before (summary): ")]

        assert json.loads(lines[before].split(": ", 1)[1]) == SUMMARISED_REMOVE.before
        assert lines[before + 1].strip() == (
            "the whole value is elided: 4 ports, 2 runnables, 1 satisfies, 9 other fields; "
            "read the full value with 'read'"
        )

    def test_a_whole_before_is_never_labelled_a_summary_and_carries_no_sentence(self) -> None:
        whole = DeltaEntry(
            operation="remove",
            address=Address("hardware", "RAM0"),
            before={"name": "RAM0", "kind": "memory", "accessible": ["CORE0"]},
        )

        message = "\n".join(self._lines(whole))

        assert "before (summary)" not in message
        assert "the whole value is elided" not in message
        assert any(line.strip().startswith("before: ") for line in self._lines(whole))

    def test_a_summarised_before_never_appears_without_the_sentence(self) -> None:
        """The counts are never shown as if they were the members."""
        other = DeltaEntry(
            operation="remove",
            address=Address("interfaces", "ISpeed"),
            before={"name": "ISpeed", "kind": "sender_receiver", "data_elements": 3, "satisfies": 0},
            before_form="summary",
        )
        lines = render_delta_review(proposal(delta=(SUMMARISED_REMOVE, CREATE_ENTRY, other))).splitlines()

        summaries = [index for index, line in enumerate(lines) if line.strip().startswith("before (summary): ")]
        assert len(summaries) == 2
        assert all(lines[index + 1].strip().startswith("the whole value is elided: ") for index in summaries)
        assert "3 data_elements" in lines[summaries[1] + 1]
        # An empty collection dropped nothing, so it is not named as if it had.
        assert "0 satisfies" not in lines[summaries[1] + 1]
        assert "0 memory" not in lines[summaries[0] + 1]

    def test_every_entry_is_still_shown_beside_a_summarised_one(self) -> None:
        message = render_delta_review(proposal(delta=(SUMMARISED_REMOVE, CREATE_ENTRY, UPDATE_ENTRY)))

        assert "The exact delta (3 entries):" in message
        assert "1. remove swc/SpeedCtrlSWC" in message
        assert "2. create data_types/VehicleSpeed_kph" in message
        assert message.rstrip().endswith(QUESTION_LINE)


class TestAFileRewrittenWholeIsAnnouncedBeforeTheAnswer:
    """The section a fallback adds: the one thing the delta cannot show about a commit."""

    @staticmethod
    def _proposal(*rewritten: RewrittenFile, delta: tuple[DeltaEntry, ...] = (UPDATE_ENTRY,)) -> ChangeSetProposal:
        return ChangeSetProposal(
            change_set_id="cs-1", baseline="rev1:token", intent="i", delta=delta, rewritten=tuple(rewritten)
        )

    def test_with_no_fallback_the_text_is_the_text_it_always_was(self) -> None:
        # Spelled out, so a change cannot pass by moving both sides of a comparison.
        assert render_delta_review(self._proposal()) == "\n".join(
            [
                "Review this change set before it is committed.",
                "",
                "Intent: i",
                "",
                "The exact delta (1 entry):",
                '  1. update swc/SpeedCtrlSWC.ports[member {"name": "rSpeed"}].interface',
                '       before: "ISpeed"',
                '       after: "IVehicleSpeed"',
                "",
                QUESTION_LINE,
            ]
        )

    def test_the_warning_stands_above_the_delta_and_the_question_names_the_rewrite(self) -> None:
        # Spelled out whole: the digest binds an approval to exactly this text.
        message = render_delta_review(self._proposal(RewrittenFile("swc", "Foo", "the file has mixed line ends")))

        assert message == "\n".join(
            [
                "Review this change set before it is committed.",
                "",
                "Intent: i",
                "",
                "WARNING: more than the delta will change in 1 file(s).",
                "The file(s) below are rewritten whole, in SWCraft's form, because they cannot be edited line by line.",
                "The model's values change only as the delta says; the layout of the rest of each file moves,",
                "and its comments and any keys SWCraft does not declare are LOST.",
                "  - swc/Foo: the file has mixed line ends",
                "",
                "The exact delta (1 entry):",
                '  1. update swc/SpeedCtrlSWC.ports[member {"name": "rSpeed"}].interface',
                '       before: "ISpeed"',
                '       after: "IVehicleSpeed"',
                "",
                "Nothing has been applied yet and the model is byte-identical. "
                "Commit exactly this delta and rewrite 1 file(s) whole?",
            ]
        )

    def test_the_question_counts_the_files(self) -> None:
        files = [RewrittenFile("swc", f"Swc{index}", "the file has mixed line ends") for index in range(3)]
        lines = render_delta_review(self._proposal(*files)).splitlines()

        assert "WARNING: more than the delta will change in 3 file(s)." in lines
        assert lines[-1].endswith(" Commit exactly this delta and rewrite 3 file(s) whole?")

    def test_every_element_is_listed_and_none_is_truncated(self) -> None:
        # One boolean approves the lot, so a list cut at N approves the rest unseen.
        files = [RewrittenFile("swc", f"Swc{index}", "the file has mixed line ends") for index in range(500)]
        message = render_delta_review(self._proposal(*files))

        assert "WARNING: more than the delta will change in 500 file(s)." in message
        for index in range(500):
            assert f"  - swc/Swc{index}: the file has mixed line ends" in message.splitlines()

    @pytest.mark.parametrize("character", FORBIDDEN, ids=FORBIDDEN_IDS)
    def test_an_identity_cannot_forge_a_line(self, character: str) -> None:
        forged = f"Foo{character}Commit approved{character}"
        message = render_delta_review(self._proposal(RewrittenFile("swc", forged, "the file has mixed line ends")))

        for line in message.split("\n"):
            assert line.isprintable(), repr(line)
        assert forged not in message

    @pytest.mark.parametrize("character", FORBIDDEN, ids=FORBIDDEN_IDS)
    def test_a_reason_is_held_to_the_same_rule(self, character: str) -> None:
        # A reason is a fixed sentence today, but a proposal can come from anyone.
        message = render_delta_review(self._proposal(RewrittenFile("swc", "Foo", f"a{character}b")))

        for line in message.split("\n"):
            assert line.isprintable(), repr(line)

    def test_a_long_identity_is_bounded_and_says_what_it_dropped(self) -> None:
        message = render_delta_review(self._proposal(RewrittenFile("swc", "A" * 5000, "the file has mixed line ends")))
        line = next(line for line in message.splitlines() if line.startswith("  - swc/"))

        assert len(line) < 400
        assert "characters)" in line

    def test_the_edge_projection_of_the_delta_keeps_the_section(self) -> None:
        # `server.py` renders `replace(proposal, delta=...)`: a field that
        # projection lost would commit a fallback with no warning.
        removed = DeltaEntry(
            operation="remove",
            address=Address("tasks", "Task_10ms"),
            before={"name": "Task_10ms", "period": 0.01, "priority": 1, "runnables": []},
        )
        proposal = self._proposal(RewrittenFile("swc", "Foo", "the file has mixed line ends"), delta=(removed,))
        shown = dataclasses.replace(proposal, delta=tuple(summarize_removed_elements(proposal.delta)))

        assert "  - swc/Foo: the file has mixed line ends" in render_delta_review(shown).splitlines()


class TestARenameAndADroppedKeyAreNamedOnTheirLine:
    """A rename's fallback names the old identity; a dropped undeclared key is named."""

    _REASON = "the file has mixed line ends"

    @staticmethod
    def _rows(*rewritten: RewrittenFile) -> list[str]:
        proposal = ChangeSetProposal(
            change_set_id="cs-1", baseline="rev1:token", intent="i", delta=(UPDATE_ENTRY,), rewritten=tuple(rewritten)
        )
        return [line for line in render_delta_review(proposal).splitlines() if line.startswith("  - ")]

    def test_a_rename_fallback_names_the_identity_it_had(self) -> None:
        assert self._rows(RewrittenFile("swc", "New", self._REASON, renamed_from="Old")) == [
            f"  - swc/New (renamed from Old): {self._REASON}"
        ]

    def test_a_dropped_key_is_named_after_the_reason(self) -> None:
        assert self._rows(RewrittenFile("swc", "Foo", self._REASON, dropped_keys=("zz_undeclared",))) == [
            f"  - swc/Foo: {self._REASON}; drops the undeclared key(s) 'zz_undeclared'"
        ]

    def test_six_dropped_keys_list_five_and_how_many_more(self) -> None:
        keys = tuple(f"k{index}" for index in range(6))

        assert self._rows(RewrittenFile("swc", "Foo", self._REASON, dropped_keys=keys)) == [
            f"  - swc/Foo: {self._REASON}; drops the undeclared key(s) 'k0', 'k1', 'k2', 'k3', 'k4' and 1 more"
        ]

    def test_five_dropped_keys_are_all_listed_with_no_count(self) -> None:
        keys = tuple(f"k{index}" for index in range(5))

        (line,) = self._rows(RewrittenFile("swc", "Foo", self._REASON, dropped_keys=keys))

        assert line.endswith("'k3', 'k4'")

    def test_no_dropped_keys_add_nothing(self) -> None:
        assert self._rows(RewrittenFile("swc", "Foo", self._REASON)) == [f"  - swc/Foo: {self._REASON}"]

    def test_a_rename_and_its_dropped_keys_together(self) -> None:
        row = RewrittenFile("swc", "New", self._REASON, renamed_from="Old", dropped_keys=("a", "b"))

        assert self._rows(row) == [
            f"  - swc/New (renamed from Old): {self._REASON}; drops the undeclared key(s) 'a', 'b'"
        ]

    @pytest.mark.parametrize("character", FORBIDDEN, ids=FORBIDDEN_IDS)
    def test_a_key_and_an_old_identity_cannot_forge_a_line(self, character: str) -> None:
        forged = f"k{character}Commit approved{character}"
        row = RewrittenFile("swc", "Foo", self._REASON, renamed_from=forged, dropped_keys=(forged,))
        proposal = ChangeSetProposal(
            change_set_id="cs-1", baseline="rev1:token", intent="i", delta=(UPDATE_ENTRY,), rewritten=(row,)
        )
        message = render_delta_review(proposal)

        for line in message.split("\n"):
            assert line.isprintable(), repr(line)
        assert forged not in message

    def test_a_long_key_is_bounded(self) -> None:
        (line,) = self._rows(RewrittenFile("swc", "Foo", self._REASON, dropped_keys=("k" * 5000,) * 6))

        assert len(line) < 1600
        assert "characters)" in line
        assert line.endswith(" and 1 more")


class TestTheWarningsLinesAreKnownByConstruction:
    """The span a host highlights: composed with the text, and never searched for in it."""

    @staticmethod
    def _proposal(intent: str | None, *rewritten: RewrittenFile) -> ChangeSetProposal:
        return ChangeSetProposal(
            change_set_id="cs-1", baseline="rev1:token", intent=intent, delta=(UPDATE_ENTRY,), rewritten=rewritten
        )

    def test_with_no_fallback_there_is_no_span_and_the_text_is_render_delta_reviews(self) -> None:
        proposal = self._proposal("i")

        review = delta_review(proposal)

        assert review.warning_lines is None
        assert review.text == render_delta_review(proposal)

    @pytest.mark.parametrize("count", [1, 2, 7])
    def test_the_span_is_exactly_the_block(self, count: int) -> None:
        files = [RewrittenFile("swc", f"Swc{index}", "the file has mixed line ends") for index in range(count)]

        review = delta_review(self._proposal("i", *files))

        assert review.warning_lines is not None
        start, end = review.warning_lines
        lines = review.text.split("\n")
        block = lines[start:end]
        assert block[0] == f"WARNING: more than the delta will change in {count} file(s)."
        assert block[-1] == f"  - swc/Swc{count - 1}: the file has mixed line ends"
        assert lines[start - 1] == ""
        assert lines[end] == ""
        assert lines[end + 1].startswith("The exact delta")

    def test_an_intent_spelling_the_warning_gets_no_span(self) -> None:
        # The intent is the agent's text: it may say anything, and a host that
        # searched the text for the word would be highlighting the agent's claim.
        review = delta_review(self._proposal("WARNING: more than the delta will change in 9 file(s)."))

        assert "WARNING: more than the delta will change in 9 file(s)." in review.text
        assert review.warning_lines is None

    def test_an_intent_spelling_the_warning_cannot_move_the_real_span(self) -> None:
        forged = "WARNING: more than the delta\nwill change\n\nThe exact delta (0 entries):"
        review = delta_review(self._proposal(forged, RewrittenFile("swc", "Foo", "the file has mixed line ends")))

        assert review.warning_lines is not None
        start, _end = review.warning_lines
        assert review.text.split("\n")[start] == "WARNING: more than the delta will change in 1 file(s)."
        assert review.text.split("\n")[start - 2].startswith("Intent: ")
