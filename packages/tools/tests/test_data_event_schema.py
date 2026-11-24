"""The data-received event, at the declaration, the loader and the derived views.

A runnable may be started by ``{type: data, port, element}`` -- the reception of a data element
through one of the component's own ``require`` ports, AUTOSAR's ``DataReceivedEvent``
(``[TPS_SWCT_01337]``) -- with the timing event's optional ``disabled_modes`` and a stated ``name``.

This module asks five questions, and only these:

* **what the contract declares** -- the member kind and its fields, the widened ``events`` union,
  ``disabled_mode``'s second collection, the cross-element rules with their bindings and codes, the
  element-local rule, and the kept rules widened by addition;
* **what is refused at load** -- a blank port or element;
* **what is written out** -- ``disabled_modes`` and ``name`` are left out at their default, so a
  component without the kind dumps exactly as before;
* **how the whole value is addressed** -- the protocol address, the ``diff`` key, the editor-schema
  path and the ``references.json`` member-reference edges;
* **that the generated prefix is restated** in the validator, beside the operation event's.

**Not reached:** no cross-element rule is exercised (``check-model``'s tests); nothing here emits
ARXML, maps an event at import, compares a component export or writes through
``writer.yaml_documents._event_to_dict``; the merge's widened comparison is
``test_arxml_import_merges.py``'s.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from model_contract_fixtures import load_contract
from pydantic import ValidationError
from swcraft_tools import editor_schema, validator
from swcraft_tools.differ import collection_rule
from swcraft_tools.editing import address as addr
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.diagnosis import BROKEN_REFERENCE, CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.editing.resolve import REFERENCE_POOLS
from swcraft_tools.model_registry import production_registry
from swcraft_tools.schema import DataEvent, DisabledMode, Runnable, SWComponent, TimingEvent
from swcraft_tools.writer.elements import element_to_dict

BINDING = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools" / "model_contract" / "binding.json"

#: The four cross-element identifiers, under a prefix of their own so that the ``swc.access.*`` and
#: ``swc.operation-event.*`` families are untouched, each with the code and the group pass D maps it
#: to (a rule with no repair group is grouped by its own id).
CROSS_ELEMENT = {
    "swc.data-event.unknown-port": (BROKEN_REFERENCE, "data-event-port"),
    "swc.data-event.direction-mismatch": (CONSISTENCY_VIOLATION, "swc.data-event.direction-mismatch"),
    "swc.data-event.interface-kind-mismatch": (CONSISTENCY_VIOLATION, "swc.data-event.interface-kind-mismatch"),
    "swc.data-event.unknown-element": (BROKEN_REFERENCE, "data-event-element"),
}

#: Each kept rule this contract widens, and the identifiers it gains.
WIDENED = {
    "rule:swc.timing_event.disabled-mode-port": {"field:swc.data_event.disabled_modes"},
    "rule:identifier.generated": {"field:swc.data_event.name"},
    "rule:identifier.declared": {"field:swc.data_event.name"},
    "rule:name.runnable-event-collision": {
        "field:swc.data_event.type",
        "field:swc.data_event.port",
        "field:swc.data_event.element",
        "field:swc.data_event.name",
    },
    "rule:name.duplicate-event": {
        "field:swc.data_event.type",
        "field:swc.data_event.port",
        "field:swc.data_event.element",
        "field:swc.data_event.name",
    },
    "rule:name.name-space-collision": {
        "field:swc.data_event.type",
        "field:swc.data_event.port",
        "field:swc.data_event.element",
        "field:swc.data_event.name",
    },
}

MODE = {
    "port": "R_EcuMode",
    "mode_group_ref": "/ModeDeclarationGroups/EcuMode",
    "mode_ref": "/ModeDeclarationGroups/EcuMode/Sleep",
}
PLAIN = {"type": "data", "port": "R_WheelSpeed", "element": "FrontLeft"}


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


def _component() -> dict[str, Any]:
    """A component with every event kind, as a file's mapping."""
    return {
        "name": "SpeedEstimator",
        "ports": [
            {"name": "R_WheelSpeed", "direction": "require", "interface": "WheelSpeedIf"},
            {"name": "R_EcuMode", "direction": "require", "interface": "EcuModeIf"},
        ],
        "runnables": [
            {
                "name": "FilterSpeed",
                "events": [dict(PLAIN)],
                "reads": [{"port": "R_WheelSpeed", "element": "FrontLeft"}],
            },
            {
                "name": "MonitorSpeed",
                "events": [
                    {"type": "timing", "period": 0.1},
                    {"type": "data", "port": "R_WheelSpeed", "element": "FrontRight", "disabled_modes": [dict(MODE)]},
                ],
            },
        ],
    }


# The declaration


class TestTheContractDeclaresTheDataEvent:
    def test_the_contract_version_is_the_minor_that_admits_it(self) -> None:
        """The contract version this module was checked against; any bump moves it."""
        assert load_contract()["contractVersion"] == "12.7.1"

    def test_the_kind_is_a_member_of_the_runnables_events(self) -> None:
        kind = _kinds()["kind:swc.data_event"]

        assert (kind["kind"], kind["memberOnly"]) == ("data_event", True)
        assert kind["memberOf"] == [{"kind": "runnable", "field": "events"}]

    def test_the_kind_declares_its_five_immutable_fields_in_order(self) -> None:
        fields = _fields("kind:swc.data_event")

        assert list(fields) == ["type", "port", "element", "disabled_modes", "name"]
        for name, field in fields.items():
            assert field["id"] == f"field:swc.data_event.{name}"
            assert field["immutable"] is True, name
        for name in ("type", "port", "element"):
            assert (fields[name]["type"], fields[name]["required"]) == ("string", True), name
        assert fields["type"]["enum"] == ["data"]
        modes = fields["disabled_modes"]
        assert (modes["type"], modes["required"], modes["default"], modes["omitAtDefault"]) == (
            "list[disabled_mode]",
            False,
            [],
            True,
        )
        assert modes["collection"] == {
            "kind": "list",
            "memberIdentity": "whole_value",
            "memberKinds": ["disabled_mode"],
        }
        stated = fields["name"]
        assert (stated["required"], stated["default"]) == (False, None)
        assert "DRT_{port}_{element}" in stated["description"]

    def test_port_and_element_copy_the_reads_member_references(self) -> None:
        """The pair a read access already names, resolved the same way."""
        event = _fields("kind:swc.data_event")
        read = _fields("kind:swc.data_read")

        assert event["port"]["memberReference"]["collections"] == read["port"]["memberReference"]["collections"]
        assert event["element"]["memberReference"]["collections"] == read["element"]["memberReference"]["collections"]
        assert event["element"]["memberReference"]["via"] == "port"
        assert "via" not in event["port"]["memberReference"]

    def test_the_events_union_widens_by_the_new_member_and_stays_whole_value(self) -> None:
        events = _fields("kind:swc.runnable")["events"]

        assert events["type"] == "list[timing_event | init_event | operation_event | data_event | call_returned_event]"
        assert events["collection"] == {
            "kind": "list",
            "memberIdentity": "whole_value",
            "memberKinds": ["timing_event", "init_event", "operation_event", "data_event", "call_returned_event"],
            "discriminatorField": "type",
        }

    def test_the_disabled_mode_is_a_member_of_both_events_lists(self) -> None:
        assert _kinds()["kind:swc.disabled_mode"]["memberOf"] == [
            {"kind": "timing_event", "field": "disabled_modes"},
            {"kind": "data_event", "field": "disabled_modes"},
        ]

    def test_the_three_descriptions_that_named_three_kinds_name_the_fourth(self) -> None:
        events = _fields("kind:swc.runnable")["events"]["description"]
        runnable = _kinds()["kind:swc.runnable"]["description"]
        mode = _kinds()["kind:swc.disabled_mode"]["description"]

        assert "`data` events" in events
        assert "reception" in runnable
        assert "data-received event" in mode


class TestTheContractDeclaresTheRules:
    def test_the_element_local_rule_guards_both_references(self) -> None:
        rule = _rules()["rule:swc.data_event.reference-not-blank"]

        assert rule["family"] == "element-local"
        assert rule["appliesTo"] == ["field:swc.data_event.port", "field:swc.data_event.element"]
        assert _binding()["rules"]["rule:swc.data_event.reference-not-blank"] == {
            "kind": "kind:swc.data_event",
            "method": "ref_fields_must_not_be_empty",
        }

    def test_the_kind_is_bound_to_its_class(self) -> None:
        assert _binding()["kinds"]["kind:swc.data_event"] == {"module": "swcraft_tools.schema", "class": "DataEvent"}

    @pytest.mark.parametrize("rule_id", sorted(CROSS_ELEMENT))
    def test_each_cross_element_rule_is_declared_summarised_bound_and_coded(self, rule_id: str) -> None:
        declared = _rules()[f"rule:{rule_id}"]
        code, group = CROSS_ELEMENT[rule_id]

        assert declared["family"] == "cross-element"
        assert "severity" not in declared
        assert {"field:swc.runnable.events", "field:swc.data_event.port"} <= set(declared["appliesTo"])
        assert declared["summary"] == validator.CONSISTENCY_RULES[rule_id]
        assert rule_id not in validator.WARNING_RULES
        assert _binding()["rules"][f"rule:{rule_id}"]["module"].rpartition(".")[0] == "swcraft_tools.validator"
        assert (RULE_CODES[rule_id].code, RULE_CODES[rule_id].group) == (code, group)

    def test_the_prefix_holds_exactly_the_four(self) -> None:
        declared = sorted(rule for rule in _rules() if rule.startswith("rule:swc.data-event."))

        assert declared == sorted(f"rule:{rule_id}" for rule_id in CROSS_ELEMENT)

    def test_the_unknown_element_rule_reaches_what_the_member_reference_names(self) -> None:
        """The unknown-element rule reaches what each of the two new member references names."""
        port = set(_rules()["rule:swc.data-event.unknown-port"]["appliesTo"])
        element = set(_rules()["rule:swc.data-event.unknown-element"]["appliesTo"])

        assert {"field:swc.swc.runnables", "field:swc.port.name"} <= port
        assert {
            "field:swc.data_event.element",
            "field:swc.port.interface",
            "field:interfaces.sender_receiver.data_elements",
            "field:interfaces.data_element.name",
            "kind:interfaces.external_interface",
            "field:interfaces.external_interface.data_elements",
        } <= element

    def test_no_warning_no_queue_rule_and_no_mixing_rule_exist(self) -> None:
        """The queued split of ``[SWS_Rte_07403]`` is not checked, and mixing event kinds is not judged."""
        added = [rule for rule in _rules() if "data-event" in rule or "data_event" in rule]
        assert all(_rules()[rule].get("severity") is None for rule in added)
        assert not [rule for rule in _rules() if "data" in rule and ("queue" in rule.split(".")[-1] or "mixed" in rule)]

    def test_the_two_repair_pools_name_the_ports_and_the_data_elements(self) -> None:
        assert REFERENCE_POOLS["data-event-port"] == ((), (("swc", "ports"),))
        assert REFERENCE_POOLS["data-event-element"] == ((), (("interfaces", "data_elements"),))

    @pytest.mark.parametrize("rule_id", sorted(WIDENED))
    def test_each_widened_rule_claims_the_new_fields(self, rule_id: str) -> None:
        declared = _rules()[rule_id]

        assert WIDENED[rule_id] <= set(declared["appliesTo"])
        assert declared["summary"] == validator.CONSISTENCY_RULES[rule_id.removeprefix("rule:")]

    def test_the_disabled_mode_rule_keeps_its_id_and_speaks_of_an_event(self) -> None:
        """A rename is a major; the id stays, the summary says *an event's*."""
        assert _rules()["rule:swc.timing_event.disabled-mode-port"]["summary"].startswith("An event's disabled mode")

    def test_the_generated_prefix_is_restated(self) -> None:
        assert validator.GENERATED_DATA_EVENT_PREFIX == "DRT_"


# What loads, and what is refused at load


class TestADataEventLoads:
    def test_a_data_event_loads_as_its_own_class(self) -> None:
        runnable = Runnable.model_validate({"name": "FilterSpeed", "events": [dict(PLAIN)]})

        (event,) = runnable.events
        assert isinstance(event, DataEvent)
        assert (event.type, event.port, event.element) == ("data", "R_WheelSpeed", "FrontLeft")
        assert (event.disabled_modes, event.name) == ([], None)

    def test_the_component_of_the_solution_overview_loads(self) -> None:
        component = SWComponent.model_validate(_component())
        monitor = component.runnables[1]

        assert [type(event) for event in monitor.events] == [TimingEvent, DataEvent]
        assert monitor.events[1].disabled_modes == [DisabledMode(**MODE)]

    @pytest.mark.parametrize(
        "event",
        [
            {"type": "data", "port": "", "element": "FrontLeft"},
            {"type": "data", "port": "R_WheelSpeed", "element": "  "},
        ],
    )
    def test_a_blank_reference_is_refused(self, event: dict[str, str]) -> None:
        with pytest.raises(ValidationError, match="data event ref fields must not be empty"):
            Runnable.model_validate({"name": "R", "events": [event]})

    @pytest.mark.parametrize(
        "event", [{"type": "data", "port": "R_WheelSpeed"}, {"type": "data", "element": "FrontLeft"}]
    )
    def test_a_missing_reference_is_refused(self, event: dict[str, str]) -> None:
        with pytest.raises(ValidationError):
            Runnable.model_validate({"name": "R", "events": [event]})

    def test_a_disabled_mode_keeps_its_element_local_rule(self) -> None:
        with pytest.raises(ValidationError, match="must be an absolute path"):
            DataEvent.model_validate({**PLAIN, "disabled_modes": [{**MODE, "mode_ref": "Sleep"}]})

    def test_one_event_repeated_on_one_runnable_loads(self) -> None:
        """Silence: ``events`` is a multiset."""
        runnable = Runnable.model_validate({"name": "R", "events": [dict(PLAIN), dict(PLAIN)]})
        assert runnable.events[0] == runnable.events[1]

    def test_the_event_is_a_whole_value(self) -> None:
        one = DataEvent(**PLAIN)

        assert one == DataEvent(**PLAIN)
        assert one != DataEvent(**{**PLAIN, "element": "FrontRight"})
        assert one != DataEvent(**PLAIN, disabled_modes=[DisabledMode(**MODE)])


# What is written out


class TestTheGenericSerializer:
    def test_a_component_without_the_kind_dumps_exactly_as_before(self) -> None:
        component = SWComponent(
            name="A",
            handle_termination_and_restart="no_support",
            runnables=[Runnable(name="R", events=[TimingEvent(type="timing", period=0.01)])],
        )

        # The same mapping a component without the kind always wrote.
        assert element_to_dict(component) == {
            "name": "A",
            "type": "application",
            "ports": [],
            "runnables": [{"name": "R", "events": [{"type": "timing", "period": 0.01}]}],
            "handle_termination_and_restart": "no_support",
        }

    def test_disabled_modes_and_name_are_left_out_at_their_default(self) -> None:
        dumped = element_to_dict(SWComponent.model_validate(_component()))

        assert dumped["runnables"][0]["events"] == [PLAIN]
        assert dumped["runnables"][1]["events"][1] == {
            "type": "data",
            "port": "R_WheelSpeed",
            "element": "FrontRight",
            "disabled_modes": [MODE],
        }

    def test_a_stated_name_is_written(self) -> None:
        dumped = element_to_dict(
            SWComponent.model_validate(
                {**_component(), "runnables": [{"name": "FilterSpeed", "events": [{**PLAIN, "name": "OnFrontLeft"}]}]}
            )
        )

        assert dumped["runnables"][0]["events"] == [{**PLAIN, "name": "OnFrontLeft"}]


# The whole value, addressed


class TestTheWholeValueIsAddressed:
    @staticmethod
    def _snapshot(component: SWComponent) -> Any:
        """A stand-in answering ``state`` the way ``ModelSnapshot`` does, with one layer populated."""
        registry = production_registry()

        class _Snapshot:
            state = {layer.name: [] for layer in registry.layers} | {"swc": [component]}

        return _Snapshot()

    def test_the_identity_without_disabled_modes_is_type_port_and_element(self) -> None:
        assert addr.reduce_whole_value(DataEvent(**PLAIN), _events()) == PLAIN
        assert addr.reduce_whole_value({**PLAIN, "disabled_modes": [], "name": None}, _events()) == PLAIN
        stated = {**PLAIN, "disabled_modes": [MODE]}
        assert addr.reduce_whole_value(stated, _events()) == stated

    def test_the_defaults_are_the_contracts(self) -> None:
        assert dict(dict(_events().member_defaults)["data"]) == {"disabled_modes": [], "name": None}

    def test_an_event_is_addressed_by_its_whole_value(self) -> None:
        component = SWComponent.model_validate(_component())
        address = addr.Address.from_json(
            {
                "layer": "swc",
                "element": "SpeedEstimator",
                "path": [{"field": "runnables"}, {"member": "FilterSpeed"}, {"field": "events"}, {"member": PLAIN}],
            }
        )

        result = addr.resolve(address, self._snapshot(component), placement={"group": "chassis"})

        assert result.found, result
        assert result.value == component.runnables[0].events[0]
        assert result.kind is not None and result.kind.kind == "data_event"

    def test_an_address_naming_another_element_is_not_found(self) -> None:
        component = SWComponent.model_validate(_component())
        other = {**PLAIN, "element": "FrontRight"}
        address = addr.Address.from_json(
            {
                "layer": "swc",
                "element": "SpeedEstimator",
                "path": [{"field": "runnables"}, {"member": "FilterSpeed"}, {"field": "events"}, {"member": other}],
            }
        )

        assert not addr.resolve(address, self._snapshot(component), placement={"group": "chassis"}).found

    def test_the_diff_key_is_the_whole_value(self) -> None:
        """A changed element or a stated disabled mode is another event, never an edit in place."""
        rule = collection_rule(Runnable, "events")

        assert rule is not None
        key = rule.key(dict(PLAIN))
        assert key == rule.key(dict(PLAIN))
        assert key != rule.key({**PLAIN, "element": "FrontRight"})
        assert key != rule.key({**PLAIN, "disabled_modes": [MODE]})
        assert key != rule.key({"type": "operation", "port": "R_WheelSpeed", "operation": "FrontLeft"})

    def test_the_editor_schema_reaches_the_event_through_the_runnable(self) -> None:
        definitions = editor_schema.layer_schema(production_registry().layer("swc"))["definitions"]

        assert {"$ref": "#/definitions/data_event"} in definitions["runnable"]["properties"]["events"]["items"]["oneOf"]
        declared = definitions["data_event"]
        assert declared["required"] == ["type", "port", "element"]
        assert declared["properties"]["type"]["enum"] == ["data"]
        assert declared["properties"]["disabled_modes"]["items"] == {"$ref": "#/definitions/disabled_mode"}

    @pytest.mark.parametrize(
        ("field", "collections"),
        [
            ("port", [["ports"]]),
            ("element", [["data_elements"], ["data_elements"]]),
        ],
    )
    def test_navigation_resolves_port_and_element(self, field: str, collections: list[list[str]]) -> None:
        references = json.loads(editor_schema.references_artifact())
        (entry,) = [ref for ref in references["memberReferences"] if ref["field"] == f"field:swc.data_event.{field}"]

        assert entry["paths"] == [{"topKind": "swc", "path": ["runnables", "*", "events", "*", field]}]
        assert [collection["path"] for collection in entry["collections"]] == collections
        assert entry["via"] == (None if field == "port" else "port")
