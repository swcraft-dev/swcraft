"""Tests for the address-keyed scope proposal.

The proposal is a separate, tool-free structured LLM call whose payload is validated into
an immutable :class:`~swcraft_agent.scope_proposal.ScopeProposal` before it is shown to the
user: the closed envelope ``{summary, entries}``, each entry a ``$defs/scopeEntry``.

* **fail-closed.** Every failure (transport, non-JSON, an unknown layer or field, a malformed
  address) raises :class:`ScopeProposalError`; there is no permissive fallback. An empty
  ``entries`` list is the read-only, nothing-approved form, not a failure.
* **nothing is hand-written.** The schema's and the parser's vocabulary both come from
  :mod:`swcraft_tools.editing.registry` and ``CHANGE_OPERATION_NAMES``;
  :class:`TestTheVocabularyIsDerivedFromTheSchema` proves it with one new model field.
"""

from __future__ import annotations

import dataclasses

import pytest
from swcraft_agent.approval import SCOPE_QUESTION, render_scope_approval
from swcraft_agent.conversation import ConversationManager
from swcraft_agent.scope_proposal import (
    MAX_SCOPE_ENTRIES,
    PROPOSAL_INSTRUCTION,
    SCOPE_JSON_SCHEMA,
    ScopeProposal,
    ScopeProposalError,
    build_scope_json_schema,
    layer_field_vocabulary,
    max_addressable_depth,
    parse_proposal,
    propose_scope,
    render_scope,
)
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, ScopeEntry
from swcraft_tools.editing.protocol import CHANGE_OPERATION_NAMES
from swcraft_tools.editing.registry import REGISTRY
from swcraft_tools.schema import SWComponent

from tests.conftest import FakeLLMClient

#: The adversary set for the scope prompt, derived from **Python's Unicode database**
#: (`str.isprintable()`), never from the renderer's escape table, so the two cannot share an
#: omission. `test_editing_review.py` declares the identical sweep on purpose: independent
#: sets, and a single shared tuple would be a single place to narrow.
SWEPT_CODE_POINTS = (
    *range(0x0000, 0x0100),
    *range(0x0400, 0x0460),
    *range(0x2000, 0x2100),
    *range(0xFE00, 0xFF00),
    0x110BD,
    0x1D173,
    0xE0001,
)
NON_PRINTABLE = tuple(chr(code) for code in SWEPT_CODE_POINTS if not chr(code).isprintable())
NON_PRINTABLE_IDS = [f"U+{ord(character):04X}" for character in NON_PRINTABLE]

# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

_MUTATION_PAYLOAD = {
    "summary": "Add a provide port pEngineSpeed on ISpeed to EngineController",
    "entries": [
        {
            "operation": "update",
            "address": {"layer": "swc", "element": "EngineController", "path": [{"field": "ports"}]},
        }
    ],
}

_READ_ONLY_PAYLOAD = {
    "summary": "List the SWCs in the model",
    "entries": [],
}


def _llm(payload):
    return FakeLLMClient([], structured=[payload])


# ---------------------------------------------------------------------------
# The closed envelope
# ---------------------------------------------------------------------------


class TestTheClosedEnvelope:
    def test_a_mutation_payload_parses_into_a_scope_proposal(self) -> None:
        proposal = propose_scope(_llm(_MUTATION_PAYLOAD), [], "Add pEngineSpeed to EngineController.")

        assert isinstance(proposal, ScopeProposal)
        assert proposal.summary == _MUTATION_PAYLOAD["summary"]
        assert proposal.entries == (
            ScopeEntry(
                operation="update",
                address=Address(layer="swc", element="EngineController", path=(FieldStep("ports"),)),
            ),
        )
        assert proposal.is_read_only is False

    def test_every_entry_is_parsed_by_scope_entry_from_json(self) -> None:
        """The host does not re-implement ``$defs/scopeEntry``; it calls it."""
        payload = {
            "summary": "Rebudget the 10 ms task",
            "entries": [
                {
                    "operation": "update",
                    "address": {"layer": "tasks", "element": "Task10ms", "path": [{"field": "wcet"}]},
                },
                {"operation": "create", "address": {"layer": "swc", "element": "BrakeController"}},
                {"operation": "remove", "address": {"layer": "hardware"}},
            ],
        }
        proposal = parse_proposal(payload)

        assert proposal.entries == tuple(ScopeEntry.from_json(entry) for entry in payload["entries"])
        assert [entry.operation for entry in proposal.entries] == ["update", "create", "remove"]

    def test_an_empty_entries_list_is_the_read_only_nothing_approved_form(self) -> None:
        """Not a failure and not "no scope": the read-only form, an empty list."""
        proposal = parse_proposal(_READ_ONLY_PAYLOAD)

        assert proposal.entries == ()
        assert proposal.is_read_only is True
        assert proposal.to_json() == []

    def test_to_json_round_trips_the_entries(self) -> None:
        proposal = parse_proposal(_MUTATION_PAYLOAD)
        assert proposal.to_json() == _MUTATION_PAYLOAD["entries"]

    def test_the_proposal_is_immutable(self) -> None:
        proposal = parse_proposal(_MUTATION_PAYLOAD)
        with pytest.raises(dataclasses.FrozenInstanceError):
            proposal.summary = "something else"  # type: ignore[misc]
        assert isinstance(proposal.entries, tuple)

    def test_parse_proposal_is_the_same_entry_point_without_an_llm(self) -> None:
        assert parse_proposal(_MUTATION_PAYLOAD).summary == _MUTATION_PAYLOAD["summary"]


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------

_INVALID_PAYLOADS = {
    "not_a_mapping": ["summary", "entries"],
    "null": None,
    "empty_object": {},
    "missing_summary": {"entries": []},
    "missing_entries": {"summary": "s"},
    "empty_summary": {"summary": "", "entries": []},
    "blank_summary": {"summary": "   ", "entries": []},
    "non_string_summary": {"summary": 5, "entries": []},
    "unknown_envelope_key": {"summary": "s", "entries": [], "urgency": "high"},
    "retired_allow_tools_envelope": {
        "kind": "mutation",
        "summary": "s",
        "allow_tools": ["add_port"],
        "targets": [{"layer": "swcs", "name": "A", "fields": ["ports"]}],
    },
    "entries_not_a_list": {"summary": "s", "entries": {"operation": "create"}},
    "entries_a_string": {"summary": "s", "entries": "create swc"},
    "entry_not_an_object": {"summary": "s", "entries": ["create"]},
    "entry_missing_operation": {"summary": "s", "entries": [{"address": {"layer": "swc"}}]},
    "entry_missing_address": {"summary": "s", "entries": [{"operation": "create"}]},
    "entry_unknown_key": {
        "summary": "s",
        "entries": [{"operation": "create", "address": {"layer": "swc"}, "why": "because"}],
    },
    "unknown_operation": {"summary": "s", "entries": [{"operation": "rename", "address": {"layer": "swc"}}]},
    "address_is_a_storage_path": {"summary": "s", "entries": [{"operation": "create", "address": "model/swc/A.yaml"}]},
    "address_missing_layer": {"summary": "s", "entries": [{"operation": "create", "address": {"element": "A"}}]},
    "address_unknown_key": {
        "summary": "s",
        "entries": [{"operation": "create", "address": {"layer": "swc", "group": "powertrain"}}],
    },
    "explicit_null_element": {
        "summary": "s",
        "entries": [{"operation": "create", "address": {"layer": "swc", "element": None}}],
    },
    "path_without_element": {
        "summary": "s",
        "entries": [{"operation": "update", "address": {"layer": "swc", "path": [{"field": "ports"}]}}],
    },
    "traversal_element": {
        "summary": "s",
        "entries": [{"operation": "create", "address": {"layer": "swc", "element": "../etc"}}],
    },
    "unknown_layer": {"summary": "s", "entries": [{"operation": "create", "address": {"layer": "components"}}]},
    "undeclared_field": {
        "summary": "s",
        "entries": [
            {"operation": "update", "address": {"layer": "swc", "element": "A", "path": [{"field": "telemetry"}]}}
        ],
    },
    "field_of_another_layer": {
        "summary": "s",
        "entries": [
            # `priority` is a task's alone (a runnable declares `wcet` too).
            {"operation": "update", "address": {"layer": "swc", "element": "A", "path": [{"field": "priority"}]}}
        ],
    },
    "undeclared_nested_field": {
        "summary": "s",
        "entries": [
            {
                "operation": "update",
                "address": {
                    "layer": "swc",
                    "element": "A",
                    "path": [{"field": "ports"}, {"member": "pSpeed"}, {"field": "baud_rate"}],
                },
            }
        ],
    },
}


@pytest.mark.parametrize("payload", list(_INVALID_PAYLOADS.values()), ids=list(_INVALID_PAYLOADS))
def test_invalid_payload_raises_scope_proposal_error(payload) -> None:
    """No permissive fallback: an unusable proposal authorizes nothing."""
    with pytest.raises(ScopeProposalError):
        propose_scope(_llm(payload), [], "do something")


@pytest.mark.parametrize("payload", list(_INVALID_PAYLOADS.values()), ids=list(_INVALID_PAYLOADS))
def test_parse_proposal_rejects_the_same_payloads(payload) -> None:
    with pytest.raises(ScopeProposalError):
        parse_proposal(payload)


def test_an_unknown_envelope_key_refusal_states_the_condition_it_fired_on() -> None:
    """The refusal names the extra key and the accepted ones, never "carries no" (the opposite condition)."""
    with pytest.raises(ScopeProposalError) as failure:
        parse_proposal({"summary": "s", "entries": [], "urgency": "high"})

    message = str(failure.value)
    assert "urgency" in message
    assert "carries no" not in message
    assert "summary" in message
    assert "entries" in message


class TestTheRegistryDerivedVocabularyRefuses:
    def test_an_undeclared_layer_names_the_layer(self) -> None:
        with pytest.raises(ScopeProposalError, match="components"):
            parse_proposal({"summary": "s", "entries": [{"operation": "create", "address": {"layer": "components"}}]})

    def test_an_undeclared_field_names_the_field_and_the_layer(self) -> None:
        with pytest.raises(ScopeProposalError) as excinfo:
            parse_proposal(
                {
                    "summary": "s",
                    "entries": [
                        {
                            "operation": "update",
                            "address": {"layer": "swc", "element": "A", "path": [{"field": "telemetry"}]},
                        }
                    ],
                }
            )
        assert "telemetry" in str(excinfo.value)
        assert "swc" in str(excinfo.value)

    def test_a_declared_nested_field_is_accepted(self) -> None:
        """The False branch of the same check: a member's own declared field passes."""
        proposal = parse_proposal(
            {
                "summary": "Retarget a port's interface",
                "entries": [
                    {
                        "operation": "update",
                        "address": {
                            "layer": "swc",
                            "element": "EngineController",
                            "path": [{"field": "ports"}, {"member": "pSpeed"}, {"field": "interface"}],
                        },
                    }
                ],
            }
        )
        assert len(proposal.entries) == 1

    def test_a_key_step_needs_no_field_vocabulary(self) -> None:
        proposal = parse_proposal(
            {
                "summary": "Retune one memory consumer",
                "entries": [
                    {
                        "operation": "update",
                        "address": {"layer": "swc", "element": "A", "path": [{"field": "memory"}, {"key": "RAM0"}]},
                    }
                ],
            }
        )
        assert len(proposal.entries) == 1

    def test_every_declared_layer_is_accepted(self) -> None:
        for layer in REGISTRY.layer_names:
            proposal = parse_proposal(
                {"summary": "s", "entries": [{"operation": "create", "address": {"layer": layer}}]}
            )
            assert proposal.entries[0].address.layer == layer

    def test_every_declared_field_of_every_layer_is_accepted(self) -> None:
        vocabulary = layer_field_vocabulary()
        for layer, fields in vocabulary.items():
            for field in fields:
                proposal = parse_proposal(
                    {
                        "summary": "s",
                        "entries": [
                            {
                                "operation": "update",
                                "address": {"layer": layer, "element": "A", "path": [{"field": field}]},
                            }
                        ],
                    }
                )
                assert proposal.entries[0].address.path[0] == FieldStep(field)


def test_transport_failure_raises_scope_proposal_error() -> None:
    llm = FakeLLMClient([], structured=[ConnectionError("azure unreachable")])

    with pytest.raises(ScopeProposalError) as excinfo:
        propose_scope(llm, [], "do something")

    assert isinstance(excinfo.value.__cause__, ConnectionError)


def test_missing_structured_round_raises_scope_proposal_error() -> None:
    """Even the double's own "you forgot to queue one" failure fails closed."""
    with pytest.raises(ScopeProposalError):
        propose_scope(FakeLLMClient([]), [], "do something")


def test_scope_proposal_error_names_the_failure() -> None:
    with pytest.raises(ScopeProposalError, match="scope"):
        parse_proposal({})


# ---------------------------------------------------------------------------
# What the proposal call is given
# ---------------------------------------------------------------------------


def test_proposal_receives_the_history_the_user_message_and_nothing_else() -> None:
    """No separate project-context payload and no new discovery mechanism."""
    conversation = ConversationManager("SYSTEM PROMPT")
    conversation.add_user("Add a SWC called A.")
    conversation.add_assistant("Done.")
    history = conversation.messages

    llm = _llm(_MUTATION_PAYLOAD)
    propose_scope(llm, history, "Add pEngineSpeed to EngineController.")

    ((sent, schema),) = llm.structured_calls
    assert schema is SCOPE_JSON_SCHEMA
    assert sent == [
        *history,
        {"role": "user", "content": "Add pEngineSpeed to EngineController."},
        {"role": "system", "content": PROPOSAL_INSTRUCTION},
    ]


def test_proposal_does_not_mutate_the_caller_history() -> None:
    history = [{"role": "system", "content": "sys"}]
    propose_scope(_llm(_READ_ONLY_PAYLOAD), history, "hi")
    assert history == [{"role": "system", "content": "sys"}]


def test_proposal_instruction_is_a_fixed_constant() -> None:
    """The instruction carries no project data — it is the same string always."""
    first = _llm(_READ_ONLY_PAYLOAD)
    second = _llm(_READ_ONLY_PAYLOAD)
    propose_scope(first, [{"role": "user", "content": "project A"}], "a")
    propose_scope(second, [{"role": "user", "content": "project B"}], "b")

    assert first.structured_calls[0][0][-1] == second.structured_calls[0][0][-1]


# ---------------------------------------------------------------------------
# The JSON schema
# ---------------------------------------------------------------------------


def _entry_schema(schema: dict) -> dict:
    return schema["properties"]["entries"]["items"]


def _address_schema(schema: dict) -> dict:
    return _entry_schema(schema)["properties"]["address"]


class TestTheJsonSchema:
    def test_the_envelope_is_closed(self) -> None:
        assert SCOPE_JSON_SCHEMA["type"] == "object"
        assert SCOPE_JSON_SCHEMA["additionalProperties"] is False
        assert sorted(SCOPE_JSON_SCHEMA["required"]) == ["entries", "summary"]
        assert sorted(SCOPE_JSON_SCHEMA["properties"]) == ["entries", "summary"]

    def test_every_nested_object_is_closed_too(self) -> None:
        for nested in (_entry_schema(SCOPE_JSON_SCHEMA), _address_schema(SCOPE_JSON_SCHEMA)):
            assert nested["additionalProperties"] is False

    def test_the_operation_enum_is_the_protocol_mirror(self) -> None:
        enum = _entry_schema(SCOPE_JSON_SCHEMA)["properties"]["operation"]["enum"]
        assert enum == list(CHANGE_OPERATION_NAMES)

    def test_the_layer_enum_is_the_registry_layer_names(self) -> None:
        """The enum is the registry's layers; ``hardware`` present and ``resources`` absent rule out a stale literal."""
        enum = _address_schema(SCOPE_JSON_SCHEMA)["properties"]["layer"]["enum"]
        assert enum == list(REGISTRY.layer_names)
        assert "hardware" in enum
        assert "resources" not in enum

    def test_the_field_enum_is_every_declared_field(self) -> None:
        enum = _address_schema(SCOPE_JSON_SCHEMA)["properties"]["path"]["items"]["properties"]["field"]["enum"]
        declared = {field for fields in layer_field_vocabulary().values() for field in fields}
        assert set(enum) == declared
        assert enum == sorted(enum), "a generated enum is rendered in a stable order"

    def test_an_entries_array_may_be_empty(self) -> None:
        """The read-only form has to be expressible in the schema the LLM answers."""
        assert SCOPE_JSON_SCHEMA["properties"]["entries"].get("minItems", 0) == 0

    def test_the_per_layer_vocabulary_is_stated_in_the_description(self) -> None:
        description = SCOPE_JSON_SCHEMA["description"]
        for layer, fields in layer_field_vocabulary().items():
            assert layer in description
            for field in fields:
                assert field in description


class TestTheVocabularyIsDerivedFromTheSchema:
    """One new field declared in a schema class only reaches the LLM's and the parser's vocabulary.

    The extended registry starts from ``registry._PRODUCTION_TABLES``, so it differs from
    production in exactly one field.
    """

    NEW_FIELD = "telemetry_channel"

    @staticmethod
    def _extended_registry() -> reg.Registry:
        class ExtendedSWComponent(SWComponent):
            telemetry_channel: str | None = None

        tables = reg._PRODUCTION_TABLES
        specs = tuple(
            dataclasses.replace(spec, annotation=ExtendedSWComponent) if spec.name == "swc" else spec
            for spec in tables.layer_specs
        )
        kind_names = dict(tables.kind_names)
        kind_names[ExtendedSWComponent] = "swc"
        return reg.build_registry(layer_specs=specs, kind_names=kind_names)

    def test_the_new_field_is_absent_from_the_production_vocabulary(self) -> None:
        assert self.NEW_FIELD not in layer_field_vocabulary()["swc"]

    def test_the_new_field_reaches_the_vocabulary(self) -> None:
        extended = self._extended_registry()
        assert self.NEW_FIELD in layer_field_vocabulary(extended)["swc"]

    def test_the_new_field_reaches_the_json_schema(self) -> None:
        extended = self._extended_registry()
        schema = build_scope_json_schema(extended)
        enum = _address_schema(schema)["properties"]["path"]["items"]["properties"]["field"]["enum"]
        assert self.NEW_FIELD in enum
        assert self.NEW_FIELD in schema["description"]

    def test_the_new_field_is_accepted_by_the_parser_only_under_that_registry(self) -> None:
        payload = {
            "summary": "Set the telemetry channel",
            "entries": [
                {
                    "operation": "update",
                    "address": {"layer": "swc", "element": "A", "path": [{"field": self.NEW_FIELD}]},
                }
            ],
        }
        assert parse_proposal(payload, registry=self._extended_registry()).entries[0].address.path == (
            FieldStep(self.NEW_FIELD),
        )
        with pytest.raises(ScopeProposalError):
            parse_proposal(payload)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_is_stable(self) -> None:
        assert render_scope(parse_proposal(_MUTATION_PAYLOAD)) == render_scope(parse_proposal(_MUTATION_PAYLOAD))

    def test_names_the_summary_the_operation_and_the_address_of_every_entry(self) -> None:
        payload = {
            "summary": "Retime the 10 ms task and drop the spare memory",
            "entries": [
                {
                    "operation": "update",
                    "address": {"layer": "tasks", "element": "Task10ms", "path": [{"field": "wcet"}]},
                },
                {"operation": "create", "address": {"layer": "swc", "element": "BrakeController"}},
                {"operation": "remove", "address": {"layer": "hardware", "element": "RAM1"}},
            ],
        }
        text = render_scope(parse_proposal(payload))

        assert payload["summary"] in text
        for fragment in (
            "update",
            "tasks",
            "Task10ms",
            "wcet",
            "create",
            "BrakeController",
            "remove",
            "hardware",
            "RAM1",
        ):
            assert fragment in text, f"{fragment!r} missing from the rendered scope"

    def test_renders_every_step_form(self) -> None:
        payload = {
            "summary": "Retarget one port and one memory entry",
            "entries": [
                {
                    "operation": "update",
                    "address": {
                        "layer": "swc",
                        "element": "A",
                        "path": [{"field": "ports"}, {"member": "pSpeed"}, {"field": "interface"}],
                    },
                },
                {
                    "operation": "update",
                    "address": {"layer": "swc", "element": "A", "path": [{"field": "memory"}, {"key": "RAM0"}]},
                },
            ],
        }
        text = render_scope(parse_proposal(payload))
        for fragment in ("ports", "pSpeed", "interface", "memory", "RAM0"):
            assert fragment in text

    def test_an_address_reads_the_same_here_as_in_the_servers_delta_review(self) -> None:
        """The scope prompt spells an address as the server's delta review does, so the two can be compared."""
        from swcraft_tools.editing.review import render_address as review_render

        payload = {
            "summary": "Retarget one port and one memory entry",
            "entries": [
                {
                    "operation": "update",
                    "address": {
                        "layer": "swc",
                        "element": "A",
                        "path": [{"field": "ports"}, {"member": "pSpeed"}, {"field": "interface"}],
                    },
                },
                {
                    "operation": "update",
                    "address": {"layer": "swc", "element": "A", "path": [{"field": "memory"}, {"key": "RAM0"}]},
                },
            ],
        }
        proposal = parse_proposal(payload)
        text = render_scope(proposal)

        for entry in proposal.entries:
            assert review_render(entry.address) in text, review_render(entry.address)

    def test_a_layer_wide_entry_renders_without_an_element(self) -> None:
        text = render_scope(
            parse_proposal({"summary": "s", "entries": [{"operation": "create", "address": {"layer": "swc"}}]})
        )
        assert "swc" in text
        assert "None" not in text, "an omitted element must never render as a bare None"

    def test_the_read_only_form_says_nothing_may_change(self) -> None:
        text = render_scope(parse_proposal(_READ_ONLY_PAYLOAD))

        assert _READ_ONLY_PAYLOAD["summary"] in text
        assert "none" in text.lower()
        for operation in CHANGE_OPERATION_NAMES:
            assert operation not in text

    def test_entry_order_is_the_proposals_own(self) -> None:
        payload = {
            "summary": "s",
            "entries": [
                {"operation": "remove", "address": {"layer": "hardware", "element": "RAM1"}},
                {"operation": "create", "address": {"layer": "swc", "element": "A"}},
            ],
        }
        text = render_scope(parse_proposal(payload))
        assert text.index("hardware") < text.index("swc")

    def test_returns_a_block_a_prompt_can_print_as_is(self) -> None:
        text = render_scope(parse_proposal(_MUTATION_PAYLOAD))
        assert not text.startswith("\n")
        assert not text.endswith("\n")


class TestTheScopePromptCannotBeForgedByTheModel:
    """The scope block is the first and broadest prompt of a scoped turn; its LLM-written summary cannot forge it.

    Interpolated verbatim, a summary with newlines could print its own false scope block
    and question above the real one, so a user granted ``remove`` over a whole layer. The
    delta review's neutralization never reaches this prompt. Both halves are pinned: the
    parser refuses such a summary, and the renderer neutralizes one that reached it anyway.
    """

    #: The shape of the attack: a complete, plausible, false scope block ending
    #: in the real question, so the user answers that one instead.
    FORGERY = (
        "read the model\n  entries: none - no change to the model is authorized\n\n"
        f"{SCOPE_QUESTION}y\n\nProposed task scope\n  summary: read the model"
    )

    #: The widest grant the vocabulary can express: `remove`, layer-wide.
    WIDEST_ENTRY = {"operation": "remove", "address": {"layer": "swc"}}

    def _forged(self, summary: str) -> ScopeProposal:
        """A proposal carrying *summary*, built past the parser so the renderer is pinned on its own."""
        return ScopeProposal(summary=summary, entries=(ScopeEntry.from_json(self.WIDEST_ENTRY),))

    def test_the_parser_refuses_a_summary_that_carries_a_line_break(self) -> None:
        with pytest.raises(ScopeProposalError) as failure:
            parse_proposal({"summary": self.FORGERY, "entries": [self.WIDEST_ENTRY]})

        # The refusal names the code point, and calls only a line break a line break.
        assert "U+000A" in str(failure.value)
        assert "line break" in str(failure.value)

    def test_the_refusal_states_the_code_point_rather_than_reprinting_it(self) -> None:
        with pytest.raises(ScopeProposalError) as failure:
            parse_proposal({"summary": f"read the model{chr(0x9B)}2J", "entries": []})

        message = str(failure.value)
        # Printed to the approval prompt's terminal, so echoing the offender raw would hand back
        # the cursor control the refusal exists to stop.
        assert "U+009B" in message
        assert "line break" not in message
        assert message.isprintable()

    @pytest.mark.parametrize("character", NON_PRINTABLE, ids=NON_PRINTABLE_IDS)
    def test_the_parser_refuses_every_non_printable_code_point(self, character: str) -> None:
        with pytest.raises(ScopeProposalError) as failure:
            parse_proposal({"summary": f"read the model{character}", "entries": []})

        assert str(failure.value).isprintable()

    def test_a_forged_summary_that_reached_the_renderer_cannot_open_a_second_block(self) -> None:
        prompt = render_scope_approval(self._forged(self.FORGERY))
        benign = render_scope_approval(self._forged("read the model"))

        # The forged words survive, escaped on the summary's own line; no forged *line* does.
        lines = prompt.splitlines()
        assert len(lines) == len(benign.splitlines())
        assert [index for index, line in enumerate(lines) if line == SCOPE_QUESTION] == [len(lines) - 1]
        assert len([line for line in lines if line.startswith("Proposed task scope")]) == 1
        assert "  entries: none - no change to the model is authorized" not in lines
        assert "  - remove swc (any element)" in lines

    @pytest.mark.parametrize("character", NON_PRINTABLE, ids=NON_PRINTABLE_IDS)
    def test_no_code_point_the_renderer_escapes_survives_onto_a_line(self, character: str) -> None:
        prompt = render_scope_approval(self._forged(f"before{character}after"))

        # `str.splitlines` splits on NEL and the Unicode separators too, so every spelling is counted.
        assert len(prompt.splitlines()) == len(render_scope_approval(self._forged("x")).splitlines())
        for line in prompt.splitlines():
            assert line.isprintable() or line == "", repr(line)
        # Escaped, not dropped: a deleted character would hide that the model sent one.
        assert "beforeafter" not in prompt

    def test_an_over_long_summary_cannot_push_the_entries_off_the_screen(self) -> None:
        prompt = render_scope_approval(self._forged("A" * 5000))

        summary_line = next(line for line in prompt.splitlines() if line.startswith("  summary: "))
        assert len(summary_line) <= len("  summary: ") + 200
        # The truncation marker's count is checked against the input.
        dropped = int(summary_line.split("\u2026 (+", 1)[1].split(" characters)", 1)[0])
        assert len(summary_line.split("\u2026 (+", 1)[0]) - len("  summary: ") + dropped == 5000
        assert "  - remove swc (any element)" in prompt.splitlines()
        assert prompt.endswith(SCOPE_QUESTION)

    def test_the_parser_refuses_more_entries_than_a_person_can_be_asked_to_read(self) -> None:
        # Refused rather than truncated: a prompt showing part of a grant would take one `y` for all of it.
        payload = {
            "summary": "touch everything",
            "entries": [
                {"operation": "update", "address": {"layer": "swc", "element": f"E{index}"}}
                for index in range(MAX_SCOPE_ENTRIES + 1)
            ],
        }

        with pytest.raises(ScopeProposalError) as failure:
            parse_proposal(payload)

        assert str(MAX_SCOPE_ENTRIES) in str(failure.value)

    def test_the_parser_refuses_an_address_deeper_than_the_model_can_be_addressed(self) -> None:
        # `$defs/address.path` declares no maximum, so an unbounded path could scroll the real
        # grant off the screen; refused at parse time, like the entry count.
        payload = {
            "summary": "hide the real grant",
            "entries": [
                {"operation": "remove", "address": {"layer": "swc"}},
                {
                    "operation": "update",
                    "address": {
                        "layer": "swc",
                        "element": "Filler",
                        "path": [{"key": "K" * 5000} for _ in range(2000)],
                    },
                },
            ],
        }

        with pytest.raises(ScopeProposalError) as failure:
            parse_proposal(payload)

        assert "2000 path steps" in str(failure.value)

    def test_the_deepest_address_the_model_admits_is_still_accepted(self) -> None:
        # The bound is derived from the registry; a real address of that depth: an SWC's runnable's event.
        deepest = {
            "operation": "update",
            "address": {
                "layer": "swc",
                "element": "EngineController",
                "path": [
                    {"field": "runnables"},
                    {"member": "EngineRun"},
                    {"field": "events"},
                    {"member": {"type": "timing", "period": 0.01}},
                    {"field": "period"},
                ],
            },
        }

        parsed = parse_proposal({"summary": "retime the event", "entries": [deepest]})

        assert len(parsed.entries[0].address.path) == max_addressable_depth() == 5

    def test_the_maximum_depth_is_the_registry_s_own_and_not_a_constant(self) -> None:
        # An independent walk of the field graph: a field is one step, a collection field two
        # (the field and the member/key), and a member kind's fields keep going. A member of a
        # `whole_value` collection is a leaf: each of its fields is one step, its collections add none.
        kinds = {kind.kind: kind for layer in REGISTRY.layers for kind in layer.element_kinds}

        def deepest(kind: object) -> int:
            best = 0
            for field in kind.fields:  # type: ignore[attr-defined]
                steps = 1
                if field.collection is not None:
                    members = [kinds[member] for member in field.collection.member_kinds if member in kinds]
                    if field.collection.member_identity == "whole_value":
                        steps += 1 + (1 if any(member.fields for member in members) else 0)
                    else:
                        steps += 1 + max((deepest(member) for member in members), default=0)
                best = max(best, steps)
            return best

        expected = max(deepest(kind) for layer in REGISTRY.layers for kind in layer.top_level_kinds)
        assert max_addressable_depth() == expected

    def test_the_proposal_schema_declares_the_same_depth_it_enforces(self) -> None:
        # A well-behaved model is never refused for a bound it was not shown.
        schema = build_scope_json_schema()
        path_schema = schema["properties"]["entries"]["items"]["properties"]["address"]["properties"]["path"]

        assert path_schema["maxItems"] == max_addressable_depth()

    def test_a_proposal_at_the_limit_is_still_accepted(self) -> None:
        payload = {
            "summary": "touch a lot",
            "entries": [
                {"operation": "update", "address": {"layer": "swc", "element": f"E{index}"}}
                for index in range(MAX_SCOPE_ENTRIES)
            ],
        }

        assert len(parse_proposal(payload).entries) == MAX_SCOPE_ENTRIES

    def test_an_element_identity_is_neutralized_by_the_shared_renderer(self) -> None:
        # `render_address` delegates to `review.render_address`: one escape table for both prompts.
        entry = ScopeEntry.from_json({"operation": "remove", "address": {"layer": "requirements", "element": "REQ-1"}})
        prompt = render_scope_approval(ScopeProposal(summary="tidy", entries=(entry,)))

        assert "  - remove requirements/REQ-1" in prompt.splitlines()
        assert prompt.splitlines()[-1] == SCOPE_QUESTION
