"""Contract `12.7.0`: implicit access, asynchronous calls and the call-returned event, declared and loaded.

The forms: ``reads``/``writes`` ``by: implicit`` (AUTOSAR's ``dataReadAccess``/``dataWriteAccess``), a
call's ``asynchronous: true`` (``AsynchronousServerCallPoint``) with ``poll``, ``result`` and ``timeout``,
and the event ``{type: call_returned, port, operation}`` (``AsynchronousServerCallReturnsEvent``).

This module asks what the contract declares, what loads and is refused at load, what both serializers
write out (nothing new at its default; every form round-trips through the dumps and every ``add_*``
mutation), how the event and the call fields are addressed, and that no event-kind site raises on the
new kind.

What it does **not** reach: no cross-element rule is exercised (``test_async_call_validator.py``,
``test_implicit_access_validator.py``); nothing here emits ARXML (``test_access_forms_arxml.py``), maps the forms at
import or compares a component export; the result point's ``Result_`` budget is the validator's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from data_event_support import COMPONENT, COMPONENT_FILE, WHEEL_INTERFACE, WHEEL_PORT, data_event_model
from external_stubs_support import change, put
from model_contract_fixtures import load_contract
from pydantic import ValidationError
from swcraft_tools import editor_schema, validator
from swcraft_tools.differ import collection_rule
from swcraft_tools.editing import address as addr
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.apply import StagedModel
from swcraft_tools.editing.diagnosis import (
    BROKEN_REFERENCE,
    CONSISTENCY_VIOLATION,
    RULE_CODES,
    emitted_identifier_violations,
)
from swcraft_tools.editing.resolve import REFERENCE_POOLS, UNSUGGESTED_GROUPS
from swcraft_tools.model_registry import production_registry
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import (
    CallReturnedEvent,
    DataReadRef,
    DataWriteRef,
    OperationCallRef,
    Runnable,
    SWComponent,
    TimingEvent,
)
from swcraft_tools.validator.names import _event_phrase
from swcraft_tools.writer import arxml_generation, mutations
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.mutations import _check_event_identifier
from swcraft_tools.writer.yaml_documents import runnable_to_dict, swc_to_dict

BINDING = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools" / "model_contract" / "binding.json"

#: The nine cross-element identifiers and the code each maps to; a rule with no repair group is
#: grouped by its own id.
CROSS_ELEMENT = {
    "swc.access.queued-implicit-read": CONSISTENCY_VIOLATION,
    "swc.access.implicit-read-enable-update": CONSISTENCY_VIOLATION,
    "swc.call.mixed-synchrony": CONSISTENCY_VIOLATION,
    "swc.call.timeout-mismatch": CONSISTENCY_VIOLATION,
    "swc.call.async-without-result": CONSISTENCY_VIOLATION,
    "swc.call.several-results": CONSISTENCY_VIOLATION,
    "swc.call-returned-event.unknown-call": BROKEN_REFERENCE,
    "swc.call-returned-event.ambiguous-call": CONSISTENCY_VIOLATION,
    "swc.call.init-runnable-result": CONSISTENCY_VIOLATION,
}

#: The three element-local rules: ``(kind, bound method, appliesTo)``.
ELEMENT_LOCAL = {
    "rule:swc.call_returned_event.reference-not-blank": (
        "kind:swc.call_returned_event",
        "ref_fields_must_not_be_empty",
        ["field:swc.call_returned_event.port", "field:swc.call_returned_event.operation"],
    ),
    "rule:swc.operation_call.asynchronous-fields": (
        "kind:swc.operation_call",
        "asynchronous_fields",
        ["kind:swc.operation_call"],
    ),
    "rule:swc.operation_call.timeout.non-negative": (
        "kind:swc.operation_call",
        "timeout_non_negative",
        ["field:swc.operation_call.timeout"],
    ),
}

_EVENT = {
    "field:swc.call_returned_event.type",
    "field:swc.call_returned_event.port",
    "field:swc.call_returned_event.operation",
    "field:swc.call_returned_event.name",
}
_RESULTS = {"field:swc.operation_call.result", "field:swc.call_returned_event.result"}

#: Each kept rule this contract widens, and the identifiers it gains.
WIDENED = {
    "rule:identifier.generated": {"field:swc.call_returned_event.name"} | _RESULTS,
    "rule:identifier.declared": {"field:swc.call_returned_event.name"} | _RESULTS,
    "rule:name.runnable-event-collision": _EVENT,
    "rule:name.duplicate-event": _EVENT,
    "rule:name.name-space-collision": _EVENT | _RESULTS,
    "rule:swc.access.name-collision": _RESULTS
    | {
        "field:swc.operation_call.poll",
        "field:swc.call_returned_event.port",
        "field:swc.call_returned_event.operation",
    },
}

PLAIN = {"type": "call_returned", "port": "rNvStorage", "operation": "WriteBlock"}


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


def _fields(kind_id: str) -> dict[str, dict[str, Any]]:
    return {field["name"]: field for field in _kinds()[kind_id]["fields"]}


def _rules() -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in load_contract()["rules"]}


def _binding() -> dict[str, Any]:
    with BINDING.open(encoding="utf-8") as handle:
        return json.load(handle)


def _events() -> reg.CollectionDescriptor:
    collection = reg.REGISTRY.element_kind("runnable").get_field("events").collection
    assert collection is not None
    return collection


def _every_form() -> dict[str, Any]:
    """One runnable stating every new form, as a file's mapping."""
    return {
        "name": "ComputeBrakeTorque",
        "events": [{"type": "timing", "period": 0.01}],
        "reads": [
            {"port": "rWheelSpeed", "element": "speed", "by": "implicit"},
            {"port": "rPedal", "element": "position"},
            {"port": "rPedal", "element": "force", "by": "value"},
        ],
        "writes": [
            {"port": "pBrakeTorque", "element": "torque", "by": "implicit"},
            {"port": "pBrakeTorque", "element": "status"},
        ],
        "calls": [
            {"port": "rNvStorage", "operation": "WriteBlock", "asynchronous": True, "timeout": 0.5},
            {"port": "rCalibration", "operation": "ReadBlock", "asynchronous": True, "poll": True, "result": "Calib"},
            {"port": "rCalibration", "operation": "Check"},
        ],
    }


def _callback() -> dict[str, Any]:
    return {
        "name": "OnBlockWritten",
        "events": [dict(PLAIN), {**PLAIN, "operation": "EraseBlock", "result": "Done", "name": "ASRT_Erased"}],
    }


# The declaration


class TestTheContractDeclaresTheForms:
    def test_the_contract_version_is_the_minor_that_admits_them(self) -> None:
        """Nothing is removed or renamed, so the bump is a minor on ``12.6.0``."""
        assert load_contract()["contractVersion"] == "12.7.1"

    def test_the_read_s_by_widens_by_implicit(self) -> None:
        by = _fields("kind:swc.data_read")["by"]

        assert by["enum"] == ["value", "implicit"]
        assert (by["required"], by["default"], by["immutable"]) == (False, None, False)
        assert "dataReadAccess" in by["description"]

    def test_the_write_takes_a_by_of_its_own_after_element(self) -> None:
        fields = _fields("kind:swc.data_write")

        assert list(fields) == ["port", "element", "by", "name"]
        by = fields["by"]
        assert (by["type"], by["enum"], by["required"], by["default"], by["immutable"]) == (
            "string",
            ["implicit"],
            False,
            None,
            False,
        )
        assert "dataWriteAccess" in by["description"]

    def test_the_call_takes_the_synchrony_and_its_three_companions_in_order(self) -> None:
        fields = _fields("kind:swc.operation_call")

        assert list(fields) == ["port", "operation", "asynchronous", "poll", "result", "timeout", "name"]
        for name in ("asynchronous", "poll"):
            field = fields[name]
            assert (field["type"], field["default"], field["omitAtDefault"], field["immutable"]) == (
                "boolean",
                False,
                True,
                False,
            ), name
        assert (fields["result"]["type"], fields["result"]["default"], fields["result"]["immutable"]) == (
            "string",
            None,
            False,
        )
        assert "Result_{port}_{operation}" in fields["result"]["description"]
        timeout = fields["timeout"]
        assert (timeout["type"], timeout["unit"], timeout["default"], timeout["immutable"]) == (
            "number",
            "seconds",
            None,
            False,
        )
        assert "synchronously" not in _kinds()["kind:swc.operation_call"]["description"].split("and how")[0]

    def test_the_kind_is_a_member_of_the_runnable_s_events(self) -> None:
        kind = _kinds()["kind:swc.call_returned_event"]

        assert (kind["kind"], kind["memberOnly"]) == ("call_returned_event", True)
        assert kind["memberOf"] == [{"kind": "runnable", "field": "events"}]

    def test_the_kind_declares_its_five_immutable_fields_in_order(self) -> None:
        fields = _fields("kind:swc.call_returned_event")

        assert list(fields) == ["type", "port", "operation", "result", "name"]
        for name, field in fields.items():
            assert field["id"] == f"field:swc.call_returned_event.{name}"
            assert field["immutable"] is True, name
        for name in ("type", "port", "operation"):
            assert (fields[name]["type"], fields[name]["required"]) == ("string", True), name
        assert fields["type"]["enum"] == ["call_returned"]
        for name in ("result", "name"):
            assert (fields[name]["required"], fields[name]["default"]) == (False, None), name
        assert "Result_{port}_{operation}" in fields["result"]["description"]
        assert "ASRT_{port}_{operation}" in fields["name"]["description"]

    def test_port_and_operation_copy_the_call_s_member_references(self) -> None:
        event = _fields("kind:swc.call_returned_event")
        call = _fields("kind:swc.operation_call")

        assert event["port"]["memberReference"]["collections"] == call["port"]["memberReference"]["collections"]
        assert (
            event["operation"]["memberReference"]["collections"] == call["operation"]["memberReference"]["collections"]
        )
        assert event["operation"]["memberReference"]["via"] == "port"

    def test_the_events_union_widens_by_the_new_member_and_stays_whole_value(self) -> None:
        events = _fields("kind:swc.runnable")["events"]

        assert events["type"] == "list[timing_event | init_event | operation_event | data_event | call_returned_event]"
        assert events["collection"] == {
            "kind": "list",
            "memberIdentity": "whole_value",
            "memberKinds": ["timing_event", "init_event", "operation_event", "data_event", "call_returned_event"],
            "discriminatorField": "type",
        }
        assert "`call_returned` events" in events["description"]


class TestTheContractDeclaresTheRules:
    @pytest.mark.parametrize("rule_id", sorted(ELEMENT_LOCAL))
    def test_each_element_local_rule_is_declared_and_bound(self, rule_id: str) -> None:
        kind, method, applies_to = ELEMENT_LOCAL[rule_id]
        rule = _rules()[rule_id]

        assert (rule["family"], rule["appliesTo"]) == ("element-local", applies_to)
        assert _binding()["rules"][rule_id] == {"kind": kind, "method": method}

    def test_the_kind_is_bound_to_its_class(self) -> None:
        assert _binding()["kinds"]["kind:swc.call_returned_event"] == {
            "module": "swcraft_tools.schema",
            "class": "CallReturnedEvent",
        }

    @pytest.mark.parametrize("rule_id", sorted(CROSS_ELEMENT))
    def test_each_cross_element_rule_is_declared_summarised_bound_and_coded(self, rule_id: str) -> None:
        declared = _rules()[f"rule:{rule_id}"]

        assert declared["family"] == "cross-element"
        assert "severity" not in declared
        assert "field:swc.swc.runnables" in declared["appliesTo"]
        assert declared["summary"] == validator.CONSISTENCY_RULES[rule_id]
        assert rule_id not in validator.WARNING_RULES
        assert _binding()["rules"][f"rule:{rule_id}"]["module"].rpartition(".")[0] == "swcraft_tools.validator"
        assert RULE_CODES[rule_id].code == CROSS_ELEMENT[rule_id]

    def test_unknown_call_names_both_member_references_and_suggests_no_repair(self) -> None:
        """Every ``memberReference`` is held by a rule that names it, and no repair is suggested.

        No pool expresses the component's asynchronous calls, so the finding is unsuggested.
        """
        applies_to = set(_rules()["rule:swc.call-returned-event.unknown-call"]["appliesTo"])

        assert {"field:swc.call_returned_event.port", "field:swc.call_returned_event.operation"} <= applies_to
        group = RULE_CODES["swc.call-returned-event.unknown-call"].group
        assert group == "call-returned-event-call"
        assert group not in REFERENCE_POOLS and group in UNSUGGESTED_GROUPS

    def test_the_version_aware_rule_names_its_edition(self) -> None:
        assert "4.2.2" in _rules()["rule:swc.call.async-without-result"]["summary"]

    @pytest.mark.parametrize("rule_id", sorted(WIDENED))
    def test_each_widened_rule_claims_the_new_fields(self, rule_id: str) -> None:
        declared = _rules()[rule_id]

        assert WIDENED[rule_id] <= set(declared["appliesTo"])
        assert declared["summary"] == validator.CONSISTENCY_RULES[rule_id.removeprefix("rule:")]

    def test_no_swc_access_rule_but_the_two_implicit_ones_is_minted(self) -> None:
        """The event's port and operation are judged through ``unknown-call``."""
        assert not [rule for rule in _rules() if rule.startswith("rule:swc.call-returned-event.unknown-port")]

    @pytest.mark.parametrize(
        ("restated", "original", "spelled"),
        [
            (validator.GENERATED_RESULT_POINT_PREFIX, arxml_generation.RESULT_POINT_PREFIX, "Result_"),
            (validator.GENERATED_RETURNS_EVENT_PREFIX, arxml_generation.RETURNS_EVENT_PREFIX, "ASRT_"),
        ],
    )
    def test_the_two_generated_prefixes_are_restated(self, restated: str, original: str, spelled: str) -> None:
        assert restated == original == spelled

    def test_the_event_kind_word(self) -> None:
        assert validator.EVENT_KIND_WORDS["call_returned"] == "call-returned"


# What loads, and what is refused at load


class TestTheFormsLoad:
    def test_every_form_loads(self) -> None:
        runnable = Runnable.model_validate(_every_form())

        assert [read.by for read in runnable.reads] == ["implicit", None, "value"]
        assert [write.by for write in runnable.writes] == ["implicit", None]
        first, polled, plain = runnable.calls
        assert (first.asynchronous, first.poll, first.result, first.timeout) == (True, False, None, 0.5)
        assert (polled.asynchronous, polled.poll, polled.result, polled.timeout) == (True, True, "Calib", None)
        assert (plain.asynchronous, plain.poll, plain.result, plain.timeout) == (False, False, None, None)

    def test_the_event_loads_as_its_own_class(self) -> None:
        runnable = Runnable.model_validate(_callback())

        unnamed, named = runnable.events
        assert isinstance(unnamed, CallReturnedEvent) and isinstance(named, CallReturnedEvent)
        assert (unnamed.result, unnamed.name) == (None, None)
        assert (named.result, named.name) == ("Done", "ASRT_Erased")

    def test_a_poll_without_a_result_and_a_zero_timeout_load(self) -> None:
        call = OperationCallRef(port="p", operation="o", asynchronous=True, poll=True, timeout=0)
        assert (call.poll, call.result, call.timeout) == (True, None, 0)

    @pytest.mark.parametrize("value", ["explicit", "argument", "Implicit"])
    def test_an_unknown_read_form_is_refused(self, value: str) -> None:
        with pytest.raises(ValidationError):
            DataReadRef(port="p", element="e", by=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["value", "explicit"])
    def test_a_write_takes_no_form_but_implicit(self, value: str) -> None:
        with pytest.raises(ValidationError):
            DataWriteRef(port="p", element="e", by=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "event",
        [
            {"type": "call_returned", "port": "", "operation": "WriteBlock"},
            {"type": "call_returned", "port": "rNvStorage", "operation": "  "},
        ],
    )
    def test_a_blank_event_reference_is_refused(self, event: dict[str, str]) -> None:
        with pytest.raises(ValidationError, match="call-returned event ref fields must not be empty"):
            Runnable.model_validate({"name": "R", "events": [event]})

    @pytest.mark.parametrize(
        "event", [{"type": "call_returned", "port": "rNvStorage"}, {"type": "call_returned", "operation": "Op"}]
    )
    def test_a_missing_event_reference_is_refused(self, event: dict[str, str]) -> None:
        with pytest.raises(ValidationError):
            Runnable.model_validate({"name": "R", "events": [event]})

    @pytest.mark.parametrize("key", ["asynchronous", "poll"])
    def test_a_non_boolean_flag_is_refused(self, key: str) -> None:
        entry = {"port": "p", "operation": "o", "asynchronous": True, key: "sometimes"}
        with pytest.raises(ValidationError):
            OperationCallRef.model_validate(entry)

    def test_a_negative_timeout_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="timeout must be >= 0"):
            OperationCallRef(port="p", operation="o", asynchronous=True, timeout=-0.1)

    @pytest.mark.parametrize(
        ("stated", "named"),
        [
            ({"poll": True}, "poll"),
            ({"timeout": 0.5}, "timeout"),
            ({"poll": True, "result": "R"}, "poll, result"),
        ],
    )
    def test_a_companion_on_a_synchronous_call_is_refused(self, stated: dict[str, Any], named: str) -> None:
        with pytest.raises(ValidationError, match=f"states {named} without 'asynchronous: true'"):
            OperationCallRef.model_validate({"port": "p", "operation": "o", **stated})

    def test_a_result_without_a_poll_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="states a result without 'poll: true'"):
            OperationCallRef(port="p", operation="o", asynchronous=True, result="R")

    def test_the_event_is_a_whole_value(self) -> None:
        one = CallReturnedEvent(**PLAIN)

        assert one == CallReturnedEvent(**PLAIN)
        assert one != CallReturnedEvent(**{**PLAIN, "operation": "ReadBlock"})
        assert one != CallReturnedEvent(**PLAIN, result="Done")


# What is written out


class TestTheSerializers:
    def test_a_runnable_without_the_new_forms_dumps_exactly_as_before(self) -> None:
        runnable = Runnable.model_validate(
            {
                "name": "R",
                "events": [{"type": "timing", "period": 0.01}],
                "reads": [{"port": "p", "element": "e", "by": "value"}],
                "writes": [{"port": "q", "element": "e"}],
                "calls": [{"port": "c", "operation": "o", "name": "Call_Stated"}],
            }
        )
        # The mapping a runnable without the access forms dumps to, by both writers.
        expected = {
            "name": "R",
            "events": [{"type": "timing", "period": 0.01}],
            "reads": [{"port": "p", "element": "e", "by": "value"}],
            "writes": [{"port": "q", "element": "e"}],
            "calls": [{"port": "c", "operation": "o", "name": "Call_Stated"}],
        }

        assert runnable_to_dict(runnable) == expected
        assert element_to_dict(SWComponent(name="S", runnables=[runnable]))["runnables"] == [expected]

    @pytest.mark.parametrize("document", [_every_form(), _callback()], ids=["accesses-and-calls", "events"])
    def test_every_form_round_trips_and_the_two_serializers_agree(self, document: dict[str, Any]) -> None:
        runnable = Runnable.model_validate(document)
        hand_written = runnable_to_dict(runnable)
        (generic,) = element_to_dict(SWComponent(name="S", runnables=[runnable]))["runnables"]

        assert hand_written == generic == document
        assert json.dumps(hand_written) == json.dumps(generic)  # key order included
        assert Runnable.model_validate(yaml.safe_load(yaml.safe_dump(hand_written, sort_keys=False))) == runnable

    def test_an_event_kind_it_has_not_been_taught_is_still_refused(self) -> None:
        class Untaught:
            type = "untaught"

        runnable = Runnable.model_construct(name="R", min_start_interval=0.0, concurrent=False, events=[Untaught()])
        with pytest.raises(TypeError, match="Untaught"):
            runnable_to_dict(runnable)

    def test_the_event_identifier_guard_refuses_the_event_by_its_own_arm(self) -> None:
        """``add_timing_event``/``add_init_event``'s guard: a call-returned event is named for its pair."""
        with pytest.raises(TypeError, match=r"'call_returned'.*port and operation"):
            _check_event_identifier("R", "S", "call_returned")


#: A client-server interface the fixture copy gains, and the two ports the new forms go through.
NV_INTERFACE: dict[str, Any] = {
    "name": "NvStorageIf",
    "kind": "client_server",
    "operations": [{"name": "WriteBlock"}, {"name": "ReadBlock"}],
}
NEW_PORTS = [
    {"name": "rNvStorage", "direction": "require", "interface": "NvStorageIf"},
    {"name": "P_Speed", "direction": "provide", "interface": WHEEL_INTERFACE},
]
CALLER: dict[str, Any] = {
    "name": "StoreSpeed",
    "events": [{"type": "timing", "period": 0.1}],
    "reads": [{"port": WHEEL_PORT, "element": "FrontLeft", "by": "implicit"}],
    "writes": [{"port": "P_Speed", "element": "FrontLeft", "by": "implicit"}],
    "calls": [
        {"port": "rNvStorage", "operation": "WriteBlock", "asynchronous": True, "timeout": 0.25},
        {"port": "rNvStorage", "operation": "ReadBlock", "asynchronous": True, "poll": True, "result": "Fetched"},
    ],
}
CALLBACKS: list[dict[str, Any]] = [
    {"name": "OnStored", "events": [{"type": "call_returned", "port": "rNvStorage", "operation": "WriteBlock"}]},
    {
        "name": "OnStoredNamed",
        "events": [
            {
                "type": "call_returned",
                "port": "rNvStorage",
                "operation": "WriteBlock",
                "result": "Stored",
                "name": "ASRT_Stored",
            }
        ],
    },
]


def _forms_model(model_root: Path) -> Path:
    """The data-received event fixture's copy, its component given every new form."""
    data_event_model(model_root)
    put(model_root, "interfaces/NvStorageIf.yaml", NV_INTERFACE)

    def component(document: dict[str, Any]) -> None:
        document["ports"].extend(NEW_PORTS)
        document["runnables"].extend([CALLER, *CALLBACKS])

    change(model_root, COMPONENT_FILE, component)
    return model_root


def _runnables(model_root: Path) -> dict[str, dict[str, Any]]:
    document = yaml.safe_load((model_root / COMPONENT_FILE).read_text(encoding="utf-8"))
    return {runnable["name"]: runnable for runnable in document["runnables"]}


#: Every mutation that rewrites an existing component through `swc_to_dict`, but three that need
#: more of the model than the forms do.
REWRITING_MUTATIONS: dict[str, Any] = {
    "add_port": lambda root: mutations.add_port(root, COMPONENT, "R_Second", "require", WHEEL_INTERFACE),
    "add_runnable": lambda root: mutations.add_runnable(root, COMPONENT, "Other"),
    "add_timing_event": lambda root: (
        mutations.add_runnable(root, COMPONENT, "Other"),
        mutations.add_timing_event(root, COMPONENT, "Other", 0.02),
    ),
    "add_init_event": lambda root: (
        mutations.add_runnable(root, COMPONENT, "Other"),
        mutations.add_init_event(root, COMPONENT, "Other"),
    ),
}


class TestTheMutationsKeepTheForms:
    """A preservation test per calling mutation: a hand-written dump never erases a field."""

    def test_the_hand_written_serializer_writes_the_component_as_the_generic_one(self, tmp_path: Path) -> None:
        model = _forms_model(tmp_path / "model")
        (swc,) = (swc for swc in read_swc_model(str(model)) if swc.name == COMPONENT)

        assert swc_to_dict(swc) == element_to_dict(swc)

    @pytest.mark.parametrize("mutation", sorted(REWRITING_MUTATIONS))
    def test_it_keeps_every_new_form(self, tmp_path: Path, mutation: str) -> None:
        model = _forms_model(tmp_path / "model")
        before = _runnables(model)

        REWRITING_MUTATIONS[mutation](str(model))

        after = _runnables(model)
        for runnable in (CALLER, *CALLBACKS):
            assert after[runnable["name"]] == before[runnable["name"]] == runnable


# The whole value, addressed; the call's fields, updated in place


class TestTheEventIsAddressed:
    @staticmethod
    def _snapshot(component: SWComponent) -> Any:
        registry = production_registry()

        class _Snapshot:
            state = {layer.name: [] for layer in registry.layers} | {"swc": [component]}

        return _Snapshot()

    def test_the_identity_without_names_is_type_port_and_operation(self) -> None:
        assert addr.reduce_whole_value(CallReturnedEvent(**PLAIN), _events()) == PLAIN
        assert addr.reduce_whole_value({**PLAIN, "result": None, "name": None}, _events()) == PLAIN
        stated = {**PLAIN, "result": "Done"}
        assert addr.reduce_whole_value(stated, _events()) == stated

    def test_the_defaults_are_the_contracts(self) -> None:
        assert dict(dict(_events().member_defaults)["call_returned"]) == {"result": None, "name": None}

    def test_the_validator_and_pass_d_name_one_identity(self) -> None:
        event = CallReturnedEvent(**PLAIN, result="Done", name="ASRT_Done")

        assert validator.event_identity(event) == {**PLAIN, "result": "Done", "name": "ASRT_Done"}
        assert validator.event_identity(CallReturnedEvent(**PLAIN)) == PLAIN

    def test_an_event_is_addressed_by_its_whole_value(self) -> None:
        component = SWComponent(name="Client", runnables=[Runnable.model_validate(_callback())])
        address = addr.Address.from_json(
            {
                "layer": "swc",
                "element": "Client",
                "path": [{"field": "runnables"}, {"member": "OnBlockWritten"}, {"field": "events"}, {"member": PLAIN}],
            }
        )

        result = addr.resolve(address, self._snapshot(component), placement={"group": "chassis"})

        assert result.found, result
        assert result.value == component.runnables[0].events[0]
        assert result.kind is not None and result.kind.kind == "call_returned_event"

    def test_the_diff_key_is_the_whole_value(self) -> None:
        rule = collection_rule(Runnable, "events")

        assert rule is not None
        key = rule.key(dict(PLAIN))
        assert key == rule.key(dict(PLAIN))
        assert key != rule.key({**PLAIN, "operation": "ReadBlock"})
        assert key != rule.key({**PLAIN, "result": "Done"})
        assert key != rule.key({"type": "operation", "port": "rNvStorage", "operation": "WriteBlock"})

    def test_the_editor_schema_reaches_the_event_through_the_runnable(self) -> None:
        definitions = editor_schema.layer_schema(production_registry().layer("swc"))["definitions"]

        assert {"$ref": "#/definitions/call_returned_event"} in definitions["runnable"]["properties"]["events"][
            "items"
        ]["oneOf"]
        declared = definitions["call_returned_event"]
        assert declared["required"] == ["type", "port", "operation"]
        assert declared["properties"]["type"]["enum"] == ["call_returned"]
        assert [v for v in definitions["data_read"]["properties"]["by"]["enum"] if v is not None] == [
            "value",
            "implicit",
        ]
        assert [v for v in definitions["data_write"]["properties"]["by"]["enum"] if v is not None] == ["implicit"]
        assert set(definitions["operation_call"]["properties"]) >= {"asynchronous", "poll", "result", "timeout"}

    @pytest.mark.parametrize(
        ("field", "collections"),
        [("port", [["ports"]]), ("operation", [["operations"], ["operations"]])],
    )
    def test_navigation_resolves_port_and_operation(self, field: str, collections: list[list[str]]) -> None:
        references = json.loads(editor_schema.references_artifact())
        (entry,) = [
            ref for ref in references["memberReferences"] if ref["field"] == f"field:swc.call_returned_event.{field}"
        ]

        assert entry["paths"] == [{"topKind": "swc", "path": ["runnables", "*", "events", "*", field]}]
        assert [collection["path"] for collection in entry["collections"]] == collections
        assert entry["via"] == (None if field == "port" else "port")


class TestTheCallFieldsAreUpdatesOfAnIdentifiedMember:
    @pytest.mark.parametrize(
        ("kind", "field"),
        [
            ("data_read", "by"),
            ("data_write", "by"),
            ("operation_call", "asynchronous"),
            ("operation_call", "poll"),
            ("operation_call", "result"),
            ("operation_call", "timeout"),
        ],
    )
    def test_the_field_is_mutable(self, kind: str, field: str) -> None:
        assert reg.REGISTRY.element_kind(kind).get_field(field).immutable is False

    @pytest.mark.parametrize(
        "changed",
        [
            {"asynchronous": True},
            {"asynchronous": True, "poll": True},
            {"asynchronous": True, "poll": True, "result": "R"},
            {"asynchronous": True, "timeout": 1.0},
        ],
    )
    def test_the_diff_keys_a_call_by_its_pair_alone(self, changed: dict[str, Any]) -> None:
        rule = collection_rule(Runnable, "calls")

        assert rule is not None
        plain = {"port": "rNvStorage", "operation": "WriteBlock"}
        assert rule.key(plain) == rule.key({**plain, **changed})

    @pytest.mark.parametrize(("list_name", "member"), [("reads", "element"), ("writes", "element")])
    def test_the_diff_keys_an_access_by_its_pair_alone(self, list_name: str, member: str) -> None:
        rule = collection_rule(Runnable, list_name)

        assert rule is not None
        plain = {"port": "p", member: "e"}
        assert rule.key(plain) == rule.key({**plain, "by": "implicit"})


# No event-kind site raises on the new kind


class TestNoEventKindSiteRaises:
    def test_the_derived_and_emitted_names(self) -> None:
        swc = SWComponent(name="Client", runnables=[Runnable.model_validate(_callback())])

        assert validator.derived_event_names(swc) == ["ASRT_rNvStorage_WriteBlock", "ASRT_rNvStorage_EraseBlock"]
        assert validator.emitted_event_names(swc) == ["ASRT_rNvStorage_WriteBlock", "ASRT_Erased"]
        assert arxml_generation._event_base_name(swc.runnables[0], swc.runnables[0].events[0]) == (
            "ASRT_rNvStorage_WriteBlock"
        )
        assert arxml_generation.event_element(swc.runnables[0].events[0]) == "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT"

    def test_the_event_phrase(self) -> None:
        runnable = Runnable.model_validate(_callback())
        phrase = _event_phrase(runnable, runnable.events[0], "ASRT_rNvStorage_WriteBlock")

        assert phrase == (
            "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT 'ASRT_rNvStorage_WriteBlock' the writer generates for port "
            "'rNvStorage' operation 'WriteBlock' (on runnable 'OnBlockWritten')"
        )

    def test_the_materialization_walk_takes_a_legal_event(self) -> None:
        staged = StagedModel(registry=reg.REGISTRY, elements={"swc": [{"name": "Client", "runnables": [_callback()]}]})

        assert emitted_identifier_violations(staged) == ()

    def test_the_materialization_walk_charges_the_pair_s_budget(self) -> None:
        port = "r" + "x" * 120
        event = {"type": "call_returned", "port": port, "operation": "Op"}
        runnable = {"name": "OnDone", "events": [event]}
        staged = StagedModel(registry=reg.REGISTRY, elements={"swc": [{"name": "Client", "runnables": [runnable]}]})

        # The event's result point, `Result_{port}_Op`, is charged too: two names over the budget.
        event_name, result_point = emitted_identifier_violations(staged)

        assert event_name.error == result_point.error == "invalid-identifier"
        assert f"call-returned event '{port}'.'Op'" in event_name.message
        assert "result point" not in event_name.message and "result point" in result_point.message

    def test_the_materialization_walk_judges_a_stated_name_as_declared(self) -> None:
        event = {**PLAIN, "name": "on done"}
        staged = StagedModel(
            registry=reg.REGISTRY,
            elements={"swc": [{"name": "Client", "runnables": [{"name": "OnDone", "events": [event]}]}]},
        )

        (violation,) = emitted_identifier_violations(staged)
        assert "call-returned event" in violation.message

    def test_check_model_runs_over_a_named_and_an_unnamed_event(self, tmp_path: Path) -> None:
        model = _forms_model(tmp_path / "model")

        result = validator.check_cross_layer_consistency(str(model))

        # `WriteBlock` has a named and an unnamed returns event on purpose: two result points of one call point.
        assert [issue.rule for issue in result.records if "call" in issue.rule] == ["swc.call.several-results"]

    def test_check_model_charges_the_derived_returns_event_name_the_pair_s_budget(self, tmp_path: Path) -> None:
        model = _forms_model(tmp_path / "model")
        operation = "W" + "x" * (129 - len("ASRT_") - 1 - len("rNvStorage") - 1)

        def stretch(document: dict[str, Any]) -> None:
            document["operations"].append({"name": operation})

        change(model, "interfaces/NvStorageIf.yaml", stretch)

        def component(document: dict[str, Any]) -> None:
            for runnable in document["runnables"]:
                if runnable["name"] == "OnStored":
                    runnable["events"] = [{"type": "call_returned", "port": "rNvStorage", "operation": operation}]

        change(model, COMPONENT_FILE, component)

        issues = validator.check_cross_layer_consistency(str(model)).records

        # The event's result point, `Result_rNvStorage_{operation}`, is over too.
        generated = [
            issue for issue in issues if issue.rule == "identifier.generated" and "result point" not in issue.message
        ]
        assert len(generated) == 1, [issue.message for issue in issues]
        assert f"call-returned event 'rNvStorage'.'{operation}'" in generated[0].message


def test_the_plain_timing_event_stays_the_default_member() -> None:
    """A guard on the discriminator: a timing event still loads as itself beside the new kind."""
    runnable = Runnable.model_validate({"name": "R", "events": [{"type": "timing", "period": 0.1}, dict(PLAIN)]})

    assert [type(event) for event in runnable.events] == [TimingEvent, CallReturnedEvent]
