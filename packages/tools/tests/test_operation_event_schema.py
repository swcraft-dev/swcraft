"""The operation event and a runnable's concurrency, at the declaration, the loader and the reader.

The ``events`` union holds ``{type: operation, port, operation}`` (a client's call of an operation the
component provides) beside ``timing`` and ``init``, and the runnable has ``concurrent``, AUTOSAR's
``canBeInvokedConcurrently``, omitted at its default ``false``. This module asks:

* **what the contract declares**: the member kind and its fields, the ``events`` union,
  ``concurrent``'s shape and position, the new rule identifiers with their families and bindings, and
  the summaries the validator repeats; that no rule judges mixed event kinds is asserted;
* **what loads**: an operation event, alone or beside a timing event, a blank reference refused
  element-locally, ``concurrent`` defaulting to ``false``;
* **what the reader captures**: both IREF halves at the event's own node,
  ``CAN-BE-INVOKED-CONCURRENTLY`` and each provided ``SERVER-COM-SPEC``'s ``QUEUE-LENGTH``, as the
  export spelled them.

What this module does **not** reach: no cross-element rule is exercised (the resolution family,
``duplicate-binding`` and ``incompatible-signatures`` are ``check-model``'s, tested with the
validator); nothing here serializes YAML, emits ARXML, maps an event at import or compares a component
export. The import merge's widened comparison is ``test_arxml_import_merges.py``'s.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import validator_package
import yaml
from model_contract_fixtures import load_contract
from pydantic import ValidationError
from swcraft_tools import reader
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.schema import InitEvent, OperationEvent, Runnable, TimingEvent
from swcraft_tools.validator import CONSISTENCY_RULES, GENERATED_OPERATION_EVENT_PREFIX, events

BINDING = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools" / "model_contract" / "binding.json"

#: The six cross-element identifiers, under a prefix of their own so that
#: ``test_validator_access.py``'s pin on the ``swc.access.*`` family is untouched.
CROSS_ELEMENT = (
    "swc.operation-event.unknown-port",
    "swc.operation-event.direction-mismatch",
    "swc.operation-event.interface-kind-mismatch",
    "swc.operation-event.unknown-operation",
    "swc.operation-event.duplicate-binding",
    "swc.operation-event.incompatible-signatures",
)

#: The phase of `operation_event_records` that mints each, the function `binding.json` names.
BOUND_PHASES: dict[str, str] = {
    "swc.operation-event.unknown-port": "_operation_event_port_records",
    "swc.operation-event.direction-mismatch": "_operation_event_walk_records",
    "swc.operation-event.interface-kind-mismatch": "_operation_event_walk_records",
    "swc.operation-event.unknown-operation": "_operation_event_walk_records",
    "swc.operation-event.duplicate-binding": "_operation_event_binding_records",
    "swc.operation-event.incompatible-signatures": "_operation_signature_records",
}


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


def _fields(kind_id: str) -> dict[str, dict[str, Any]]:
    return {field["name"]: field for field in _kinds()[kind_id]["fields"]}


def _rules() -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in load_contract()["rules"]}


def _binding() -> dict[str, Any]:
    with BINDING.open(encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


class TestTheContractDeclaresTheOperationEvent:
    def test_the_contract_version_is_the_minor_that_admits_it(self) -> None:
        """Adding a kind, fields and rules and widening one field's type is a minor (``7.7.0``).

        The assertion is on the shipped version."""
        # The shipped contract version: every contract bump moves it.
        assert load_contract()["contractVersion"] == "12.7.1"

    def test_the_kind_is_a_member_of_the_runnables_events(self) -> None:
        kind = _kinds()["kind:swc.operation_event"]

        assert (kind["kind"], kind["memberOnly"]) == ("operation_event", True)
        assert kind["memberOf"] == [{"kind": "runnable", "field": "events"}]

    def test_the_kind_declares_three_required_immutable_fields(self) -> None:
        fields = _fields("kind:swc.operation_event")

        # The optional stated `name` is immutable as every field of a whole-value member is
        # (`test_stated_names_and_stub_lists.py`).
        assert list(fields) == ["type", "port", "operation", "name"]
        stated = fields.pop("name")
        assert (stated["required"], stated["default"], stated["immutable"]) == (False, None, True)
        for name, field in fields.items():
            assert field["id"] == f"field:swc.operation_event.{name}"
            assert (field["type"], field["required"], field["immutable"]) == ("string", True, True), name
        assert fields["type"]["enum"] == ["operation"]
        assert "enum" not in fields["port"]
        assert "enum" not in fields["operation"]

    def test_neither_reference_field_is_a_declared_reference(self) -> None:
        """Neither name is a layer reference the traversal follows.

        Like ``operation_call``'s: the names resolve inside the component and its port's interface, which is
        ``check-model``'s resolution."""
        fields = _fields("kind:swc.operation_event")

        for name in ("port", "operation"):
            assert "reference" not in fields[name], name
            assert "relation" not in fields[name], name

    def test_the_events_union_widens_by_the_new_member_and_stays_whole_value(self) -> None:
        events = _fields("kind:swc.runnable")["events"]

        # The operation event keeps its third place (`test_data_event_schema.py` holds the data event).
        assert events["type"] == "list[timing_event | init_event | operation_event | data_event | call_returned_event]"
        assert events["collection"] == {
            "kind": "list",
            "memberIdentity": "whole_value",
            "memberKinds": ["timing_event", "init_event", "operation_event", "data_event", "call_returned_event"],
            "discriminatorField": "type",
        }

    def test_the_two_descriptions_that_named_two_kinds_name_three(self) -> None:
        events = _fields("kind:swc.runnable")["events"]["description"]
        runnable = _kinds()["kind:swc.runnable"]["description"]

        assert "`operation` events" in events
        assert "a client's call of an operation the component provides" in runnable

    def test_concurrent_is_an_optional_boolean_omitted_at_false(self) -> None:
        concurrent = _fields("kind:swc.runnable")["concurrent"]

        assert concurrent["id"] == "field:swc.runnable.concurrent"
        assert (
            concurrent["type"],
            concurrent["required"],
            concurrent["default"],
            concurrent["omitAtDefault"],
            concurrent["immutable"],
        ) == ("boolean", False, False, True, False)
        assert "canBeInvokedConcurrently" in concurrent["description"]

    def test_concurrent_follows_min_start_interval(self) -> None:
        """The position is the published read order and the YAML key order."""
        assert list(_fields("kind:swc.runnable")) == [
            "name",
            "min_start_interval",
            "concurrent",
            "events",
            "reads",
            "writes",
            "calls",
            # The two variable access lists, after the three port ones.
            "reads_variables",
            "writes_variables",
            # The two exclusive area lists, after the variable ones.
            "can_enter",
            "runs_inside",
            # Placed last, so every earlier position stands; each field below is appended after it.
            "description",
            "symbol",
            "wcet",
            "wcet_type",
            "satisfies",
        ]


class TestTheContractDeclaresTheRules:
    def test_the_element_local_rule_guards_both_references(self) -> None:
        rule = _rules()["rule:swc.operation_event.reference-not-blank"]

        assert rule["family"] == "element-local"
        assert rule["appliesTo"] == ["field:swc.operation_event.port", "field:swc.operation_event.operation"]

    @pytest.mark.parametrize("rule_id", CROSS_ELEMENT)
    def test_each_cross_element_rule_is_declared_under_its_own_prefix(self, rule_id: str) -> None:
        rule = _rules()[f"rule:{rule_id}"]

        assert rule["family"] == "cross-element"
        assert "field:swc.runnable.events" in rule["appliesTo"]
        assert "field:swc.operation_event.port" in rule["appliesTo"]

    @pytest.mark.parametrize("rule_id", CROSS_ELEMENT)
    def test_each_summary_is_the_validators(self, rule_id: str) -> None:
        assert _rules()[f"rule:{rule_id}"]["summary"] == CONSISTENCY_RULES[rule_id]

    def test_the_prefix_holds_exactly_the_six(self) -> None:
        declared = sorted(r for r in _rules() if r.startswith("rule:swc.operation-event."))

        assert declared == sorted(f"rule:{rule_id}" for rule_id in CROSS_ELEMENT)

    def test_no_rule_judges_mixed_event_kinds(self) -> None:
        """SWCraft does not judge mixed event kinds.

        The RTE specification describes mixing as unsupported in explanatory text, with no tagged requirement."""
        assert [r for r in _rules() if "event" in r and "mixed" in r] == []
        assert [r for r in CONSISTENCY_RULES if "event" in r and "mixed" in r] == []

    def test_the_incompatible_signatures_summary_states_what_it_does_not_judge(self) -> None:
        summary = CONSISTENCY_RULES["swc.operation-event.incompatible-signatures"]

        assert "Not judged:" in summary
        for unjudged in ("possible errors", "errorHandling", "serverArgumentImplPolicy", "PortDefinedArgumentValues"):
            assert unjudged in summary, unjudged

    def test_the_cross_element_rules_are_bound_to_the_phases_of_one_new_check(self) -> None:
        rules = _binding()["rules"]

        assert set(BOUND_PHASES) == set(CROSS_ELEMENT)
        for rule_id in CROSS_ELEMENT:
            assert rules[f"rule:{rule_id}"] == {
                "module": "swcraft_tools.validator.events",
                "callable": BOUND_PHASES[rule_id],
            }, rule_id
            assert BOUND_PHASES[rule_id] in validator_package.reached_functions(events, "operation_event_records")

    def test_the_kind_and_the_element_local_rule_are_bound_to_the_class(self) -> None:
        binding = _binding()

        assert binding["kinds"]["kind:swc.operation_event"] == {
            "module": "swcraft_tools.schema",
            "class": "OperationEvent",
        }
        assert binding["rules"]["rule:swc.operation_event.reference-not-blank"] == {
            "kind": "kind:swc.operation_event",
            "method": "ref_fields_must_not_be_empty",
        }

    def test_the_generated_prefix_is_restated(self) -> None:
        assert GENERATED_OPERATION_EVENT_PREFIX == "OIT_"


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


class TestAnOperationEventLoads:
    def test_an_operation_event_loads_as_its_own_class(self) -> None:
        runnable = Runnable.model_validate(
            {"name": "Serve", "events": [{"type": "operation", "port": "pServe", "operation": "Get"}]}
        )

        (event,) = runnable.events
        assert isinstance(event, OperationEvent)
        assert (event.type, event.port, event.operation) == ("operation", "pServe", "Get")

    def test_an_operation_event_beside_a_timing_and_an_init_event_loads(self) -> None:
        """Mixed event kinds are not judged: the loader keeps all three."""
        runnable = Runnable.model_validate(
            {
                "name": "Serve",
                "events": [
                    {"type": "timing", "period": 0.01},
                    {"type": "operation", "port": "pServe", "operation": "Get"},
                    {"type": "init"},
                ],
            }
        )

        assert [type(event) for event in runnable.events] == [TimingEvent, OperationEvent, InitEvent]

    @pytest.mark.parametrize(
        "event",
        [
            {"type": "operation", "port": "", "operation": "Get"},
            {"type": "operation", "port": "pServe", "operation": "  "},
        ],
    )
    def test_a_blank_reference_is_refused(self, event: dict[str, str]) -> None:
        with pytest.raises(ValidationError, match="operation event ref fields must not be empty"):
            Runnable.model_validate({"name": "Serve", "events": [event]})

    @pytest.mark.parametrize(
        "event",
        [
            {"type": "operation", "port": "pServe"},
            {"type": "operation", "operation": "Get"},
        ],
    )
    def test_a_missing_reference_is_refused(self, event: dict[str, str]) -> None:
        with pytest.raises(ValidationError):
            Runnable.model_validate({"name": "Serve", "events": [event]})

    def test_the_event_is_a_whole_value(self) -> None:
        one = OperationEvent(type="operation", port="pServe", operation="Get")

        assert one == OperationEvent(type="operation", port="pServe", operation="Get")
        assert one != OperationEvent(type="operation", port="pServe", operation="Set")


class TestConcurrentLoads:
    def test_concurrent_defaults_to_false(self) -> None:
        assert Runnable.model_validate({"name": "Run"}).concurrent is False

    def test_concurrent_true_loads(self) -> None:
        assert Runnable.model_validate({"name": "Run", "concurrent": True}).concurrent is True

    def test_concurrent_is_omitted_at_its_default_by_the_derived_rule(self) -> None:
        """``omitAtDefault``: a runnable at ``false`` carries no key under the registry's omission rule."""
        from swcraft_tools.model_registry import production_registry

        field = production_registry().kind("runnable").get_field("concurrent")
        assert field is not None
        assert field.omit_at_default is True

    def test_a_component_file_with_an_operation_event_loads_through_the_reader(self, tmp_path: Path) -> None:
        document = {
            "name": "Server",
            "type": "application",
            "ports": [{"name": "pServe", "direction": "provide", "interface": "IServe"}],
            "runnables": [
                {
                    "name": "Serve",
                    "concurrent": True,
                    "events": [{"type": "operation", "port": "pServe", "operation": "Get"}],
                }
            ],
        }
        path = tmp_path / "swc" / "app" / "Server.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        (loaded,) = reader.read_swc_model(str(tmp_path))

        (runnable,) = loaded.runnables
        assert runnable.concurrent is True
        assert runnable.events == [OperationEvent(type="operation", port="pServe", operation="Get")]


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def _server_document(tmp_path: Path, *, concurrency: str | None, queue_length: str = "1") -> Path:
    """One hand-built server component.

    A provide port with a ``SERVER-COM-SPEC``, a runnable and one event of each kind the reader recognizes."""
    concurrency_line = (
        f"                    <CAN-BE-INVOKED-CONCURRENTLY>{concurrency}</CAN-BE-INVOKED-CONCURRENTLY>\n"
        if concurrency is not None
        else ""
    )
    document = tmp_path / "server.arxml"
    document.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <APPLICATION-SW-COMPONENT-TYPE>\n"
        "          <SHORT-NAME>Server</SHORT-NAME>\n"
        "          <PORTS>\n"
        "            <P-PORT-PROTOTYPE>\n"
        "              <SHORT-NAME>pServe</SHORT-NAME>\n"
        "              <PROVIDED-COM-SPECS>\n"
        "                <SERVER-COM-SPEC>\n"
        '                  <OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/Pkg/IServe/Get</OPERATION-REF>\n'
        f"                  <QUEUE-LENGTH>{queue_length}</QUEUE-LENGTH>\n"
        "                </SERVER-COM-SPEC>\n"
        "                <SERVER-COM-SPEC>\n"
        '                  <OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/Pkg/IServe/Set</OPERATION-REF>\n'
        "                </SERVER-COM-SPEC>\n"
        "              </PROVIDED-COM-SPECS>\n"
        '              <PROVIDED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/Pkg/IServe</PROVIDED-INTERFACE-TREF>\n'
        "            </P-PORT-PROTOTYPE>\n"
        "          </PORTS>\n"
        "          <INTERNAL-BEHAVIORS>\n"
        "            <SWC-INTERNAL-BEHAVIOR>\n"
        "              <SHORT-NAME>Server_InternalBehavior</SHORT-NAME>\n"
        "              <EVENTS>\n"
        "                <OPERATION-INVOKED-EVENT>\n"
        "                  <SHORT-NAME>OIT_pServe_Get</SHORT-NAME>\n"
        '                  <START-ON-EVENT-REF DEST="RUNNABLE-ENTITY">'
        "/Pkg/Server/Server_InternalBehavior/Serve</START-ON-EVENT-REF>\n"
        "                  <OPERATION-IREF>\n"
        '                    <CONTEXT-P-PORT-REF DEST="P-PORT-PROTOTYPE">/Pkg/Server/pServe</CONTEXT-P-PORT-REF>\n'
        '                    <TARGET-PROVIDED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">'
        "/Pkg/IServe/Get</TARGET-PROVIDED-OPERATION-REF>\n"
        "                  </OPERATION-IREF>\n"
        "                </OPERATION-INVOKED-EVENT>\n"
        "                <TIMING-EVENT>\n"
        "                  <SHORT-NAME>TMT_Serve</SHORT-NAME>\n"
        '                  <START-ON-EVENT-REF DEST="RUNNABLE-ENTITY">'
        "/Pkg/Server/Server_InternalBehavior/Serve</START-ON-EVENT-REF>\n"
        "                  <PERIOD>0.01</PERIOD>\n"
        "                </TIMING-EVENT>\n"
        "              </EVENTS>\n"
        "              <RUNNABLES>\n"
        "                <RUNNABLE-ENTITY>\n"
        "                  <SHORT-NAME>Serve</SHORT-NAME>\n"
        f"{concurrency_line}"
        "                </RUNNABLE-ENTITY>\n"
        "              </RUNNABLES>\n"
        "            </SWC-INTERNAL-BEHAVIOR>\n"
        "          </INTERNAL-BEHAVIORS>\n"
        "        </APPLICATION-SW-COMPONENT-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n",
        encoding="utf-8",
        newline="\n",
    )
    return document


class TestTheReaderCapturesTheServerSide:
    def test_both_iref_halves_are_read_at_the_events_own_node(self, tmp_path: Path) -> None:
        (swc,) = read_arxml(str(_server_document(tmp_path, concurrency="true"))).swcs
        operation_event, timing_event = swc.events

        assert (operation_event.kind, operation_event.context_port_ref, operation_event.target_operation_ref) == (
            "OPERATION-INVOKED-EVENT",
            "/Pkg/Server/pServe",
            "/Pkg/IServe/Get",
        )
        assert operation_event.runnable_ref == "/Pkg/Server/Server_InternalBehavior/Serve"
        assert operation_event.period is None

    def test_another_event_kind_carries_no_iref(self, tmp_path: Path) -> None:
        (swc,) = read_arxml(str(_server_document(tmp_path, concurrency="true"))).swcs
        timing_event = swc.events[1]

        assert (timing_event.kind, timing_event.context_port_ref, timing_event.target_operation_ref) == (
            "TIMING-EVENT",
            None,
            None,
        )

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("true", True), ("1", True), ("false", False), ("0", False), (" true ", True), ("yes", None), (None, None)],
    )
    def test_concurrency_is_read_as_an_xsd_boolean(
        self, tmp_path: Path, text: str | None, expected: bool | None
    ) -> None:
        (swc,) = read_arxml(str(_server_document(tmp_path, concurrency=text))).swcs

        assert swc.runnables[0].can_be_invoked_concurrently is expected

    def test_each_provided_server_com_spec_is_read_with_its_queue_length(self, tmp_path: Path) -> None:
        (swc,) = read_arxml(str(_server_document(tmp_path, concurrency="false", queue_length=" 2 "))).swcs

        assert [(s.operation_ref, s.queue_length) for s in swc.ports[0].server_com_specs] == [
            ("/Pkg/IServe/Get", "2"),
            ("/Pkg/IServe/Set", None),
        ]

    def test_the_operation_ref_is_read_raw_as_every_reference_path_is(self, tmp_path: Path) -> None:
        """Only the queue length is stripped.

        A reference path is compared against the model's own strings, so the reader normalizes none of them
        (``_declared_text`` says why)."""
        document = _server_document(tmp_path, concurrency="false")
        text = document.read_text(encoding="utf-8")
        padded = text.replace(">/Pkg/IServe/Get</OPERATION-REF>", "> /Pkg/IServe/Get </OPERATION-REF>")
        assert padded != text
        document.write_text(padded, encoding="utf-8", newline="\n")

        (swc,) = read_arxml(str(document)).swcs

        assert swc.ports[0].server_com_specs[0].operation_ref == " /Pkg/IServe/Get "

    def test_the_document_reads_without_a_skip_of_its_own(self, tmp_path: Path) -> None:
        """Reading the event's details adds capture, never a new skip."""
        model = read_arxml(str(_server_document(tmp_path, concurrency="true")))

        assert [(entry.tag, entry.reason) for entry in model.skip_log if "EVENT" in entry.tag] == []
