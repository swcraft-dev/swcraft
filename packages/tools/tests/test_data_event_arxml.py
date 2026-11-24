"""The ARXML writer's half of the data-received event: ``DATA-RECEIVED-EVENT``.

A data-received event ``{type: data, port, element}`` is emitted as one ``DATA-RECEIVED-EVENT`` in
its runnable's component ``EVENTS``, in the runnable's own declaration order beside its other events:

* its ``SHORT-NAME`` is ``DRT_{port}_{element}``, passed through the same retroactive
  ``_unique_event_names`` as every event, or the stated ``name`` written as it is;
* its children are ``SHORT-NAME``, ``DISABLED-MODE-IREFS`` (only when stated),
  ``START-ON-EVENT-REF`` (``DEST="RUNNABLE-ENTITY"``) and ``DATA-IREF`` -- the ``REFERRABLE`` →
  ``RTE-EVENT`` → ``DATA-RECEIVED-EVENT`` order, one at 4.2.2 and 4.3.0;
* ``DATA-IREF`` holds ``CONTEXT-R-PORT-REF`` (``DEST="R-PORT-PROTOTYPE"``) naming the component's
  **own** port, then ``TARGET-DATA-ELEMENT-REF`` (``DEST="VARIABLE-DATA-PROTOTYPE"``) naming the
  interface's element, under a stub's ``external_ref`` for an external interface stub;
* nothing else: no ``DESC``, ``CATEGORY``, ``ACTIVATION-REASON-REPRESENTATION-REF`` or
  ``VARIATION-POINT`` (no field holds them, and the standard gives none a default to add).

**The writer refuses, with nothing written, every class ``check-model`` reports as an error about
the event, through the same predicates** (``validator.data_event_records``). Each negative class is
generated into a fresh directory, which stays empty, and ``check-model`` is asked the same model to
show the two agree.

**Not reached:** XSD conformance (``test_arxml_xsd_conformance.py``'s
``TestTheDataEventShapesConform``, licensed schemas); the component export's marker and the
importer; an RTE's behaviour; any model shape outside the ones below -- example-based evidence, not
a proof over every model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from data_event_support import (
    COMPONENT,
    COMPONENT_FILE,
    FILTER,
    FRONT_LEFT,
    FRONT_RIGHT,
    MODE_PORT,
    MONITOR,
    REAR_LEFT,
    REAR_PORT,
    SLEEP_MODE,
    TRACK_REAR,
    WHEEL_INTERFACE,
    WHEEL_INTERFACE_FILE,
    WHEEL_PORT,
    data_event,
    data_event_model,
    tasks_of_runnables_started_by,
)
from external_stubs_support import change, put
from lxml import etree
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.validator import WARNING_RULES, check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import (
    DATA_EVENT_PREFIX,
    INTERNAL_BEHAVIOR_SUFFIX,
    _event_base_name,
    event_element,
    generate_arxml,
)

NS = {"ar": "http://autosar.org/schema/r4.0"}
VERSIONS = ("4.2.2", "4.3.0")
BEHAVIOUR_PATH = f"/ComponentTypes/{COMPONENT}/{COMPONENT}{INTERNAL_BEHAVIOR_SUFFIX}"
COMPONENT_PATH = f"/ComponentTypes/{COMPONENT}"
REAR_STUB_REF = "/Foreign/PortInterfaces/RearSpeedIf"

#: The derived names of the fixture's three data-received events.
FRONT_LEFT_NAME = f"DRT_{WHEEL_PORT}_{FRONT_LEFT}"
FRONT_RIGHT_NAME = f"DRT_{WHEEL_PORT}_{FRONT_RIGHT}"
REAR_LEFT_NAME = f"DRT_{REAR_PORT}_{REAR_LEFT}"

#: A require port on ``WheelSpeedIf`` states the queue of its queued element.
QUEUE_SPEC = [{"data_element": FRONT_RIGHT, "queue_length": 4}]


# Helpers


@pytest.fixture
def model(tmp_path: Path) -> Path:
    """A copy of the shared fixture, with its root composition: clean."""
    return data_event_model(tmp_path / "model")


def _component(model_root: Path, mutate: Any) -> None:
    change(model_root, COMPONENT_FILE, mutate)


def _runnable_entry(document: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [runnable for runnable in document["runnables"] if runnable["name"] == name]
    return found


def _add_runnable(model_root: Path, name: str, *events: dict[str, Any]) -> None:
    _component(model_root, lambda d: d["runnables"].append({"name": name, "events": list(events)}))


def _add_port(model_root: Path, name: str, direction: str, interface: str) -> None:
    port: dict[str, Any] = {"name": name, "direction": direction, "interface": interface}
    if direction == "require" and interface == WHEEL_INTERFACE:
        port["com_specs"] = QUEUE_SPEC
    _component(model_root, lambda d: d["ports"].append(port))


def _generate(model_root: Path, output: Path, version: str = "4.2.2") -> etree._Element:
    generate_arxml(str(model_root), str(output), version=version)
    return etree.parse(str(output)).getroot()


def _tag(node: etree._Element) -> str:
    return etree.QName(node).localname


def _children(node: etree._Element) -> list[str]:
    return [_tag(child) for child in node]


def _short_name(node: etree._Element) -> str:
    return node.findtext("ar:SHORT-NAME", namespaces=NS)


def _events(document: etree._Element) -> list[etree._Element]:
    (events,) = document.xpath(
        "//ar:APPLICATION-SW-COMPONENT-TYPE[ar:SHORT-NAME=$name]//ar:SWC-INTERNAL-BEHAVIOR/ar:EVENTS",
        namespaces=NS,
        name=COMPONENT,
    )
    return list(events)


def _data_events(document: etree._Element) -> list[etree._Element]:
    return [event for event in _events(document) if _tag(event) == "DATA-RECEIVED-EVENT"]


def _event(document: etree._Element, name: str) -> etree._Element:
    (node,) = [event for event in _events(document) if _short_name(event) == name]
    return node


def _reference(node: etree._Element, path: str) -> tuple[str, str]:
    (reference,) = node.xpath(path, namespaces=NS)
    return reference.text, reference.get("DEST")


# Emission


class TestTheEventsOfTheFixture:
    def test_every_event_is_emitted_in_declared_order(self, model: Path, tmp_path: Path) -> None:
        """Runnables in declared order, each runnable's events in its own: the monitor's timing event first."""
        events = _events(_generate(model, tmp_path / "out.arxml"))

        assert [(_tag(event), _short_name(event)) for event in events] == [
            ("DATA-RECEIVED-EVENT", FRONT_LEFT_NAME),
            ("TIMING-EVENT", f"TMT_{MONITOR}"),
            ("DATA-RECEIVED-EVENT", FRONT_RIGHT_NAME),
            ("DATA-RECEIVED-EVENT", REAR_LEFT_NAME),
        ]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_children_are_in_the_schema_order(self, model: Path, tmp_path: Path, version: str) -> None:
        document = _generate(model, tmp_path / "out.arxml", version)

        assert _children(_event(document, FRONT_LEFT_NAME)) == ["SHORT-NAME", "START-ON-EVENT-REF", "DATA-IREF"]
        for event in _data_events(document):
            assert _children(event.find("ar:DATA-IREF", namespaces=NS)) == [
                "CONTEXT-R-PORT-REF",
                "TARGET-DATA-ELEMENT-REF",
            ]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_disabled_modes_are_written_before_the_start_reference(
        self, model: Path, tmp_path: Path, version: str
    ) -> None:
        """``RTE-EVENT``'s order: ``DISABLED-MODE-IREFS`` before ``START-ON-EVENT-REF``, at both versions."""
        monitor_event = _event(_generate(model, tmp_path / "out.arxml", version), FRONT_RIGHT_NAME)

        assert _children(monitor_event) == ["SHORT-NAME", "DISABLED-MODE-IREFS", "START-ON-EVENT-REF", "DATA-IREF"]

    def test_the_disabled_mode_is_written_exactly(self, model: Path, tmp_path: Path) -> None:
        monitor_event = _event(_generate(model, tmp_path / "out.arxml"), FRONT_RIGHT_NAME)

        (iref,) = monitor_event.xpath("ar:DISABLED-MODE-IREFS/ar:DISABLED-MODE-IREF", namespaces=NS)
        assert [(_tag(child), child.text, child.get("DEST")) for child in iref] == [
            ("CONTEXT-PORT-REF", f"{COMPONENT_PATH}/{MODE_PORT}", "R-PORT-PROTOTYPE"),
            (
                "CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF",
                SLEEP_MODE["mode_group_ref"],
                "MODE-DECLARATION-GROUP-PROTOTYPE",
            ),
            ("TARGET-MODE-DECLARATION-REF", SLEEP_MODE["mode_ref"], "MODE-DECLARATION"),
        ]

    def test_an_event_with_no_disabled_mode_writes_no_wrapper(self, model: Path, tmp_path: Path) -> None:
        document = _generate(model, tmp_path / "out.arxml")

        for name in (FRONT_LEFT_NAME, REAR_LEFT_NAME):
            assert _event(document, name).find("ar:DISABLED-MODE-IREFS", namespaces=NS) is None

    def test_it_starts_its_own_runnable(self, model: Path, tmp_path: Path) -> None:
        document = _generate(model, tmp_path / "out.arxml")

        starts = {
            name: _reference(_event(document, name), "ar:START-ON-EVENT-REF")
            for name in (FRONT_LEFT_NAME, FRONT_RIGHT_NAME, REAR_LEFT_NAME)
        }
        assert starts == {
            FRONT_LEFT_NAME: (f"{BEHAVIOUR_PATH}/{FILTER}", "RUNNABLE-ENTITY"),
            FRONT_RIGHT_NAME: (f"{BEHAVIOUR_PATH}/{MONITOR}", "RUNNABLE-ENTITY"),
            REAR_LEFT_NAME: (f"{BEHAVIOUR_PATH}/{TRACK_REAR}", "RUNNABLE-ENTITY"),
        }

    def test_the_iref_names_the_components_own_require_port_and_the_interfaces_element(
        self, model: Path, tmp_path: Path
    ) -> None:
        event = _event(_generate(model, tmp_path / "out.arxml"), FRONT_LEFT_NAME)

        assert _reference(event, "ar:DATA-IREF/ar:CONTEXT-R-PORT-REF") == (
            f"{COMPONENT_PATH}/{WHEEL_PORT}",
            "R-PORT-PROTOTYPE",
        )
        assert _reference(event, "ar:DATA-IREF/ar:TARGET-DATA-ELEMENT-REF") == (
            f"/PortInterfaces/{WHEEL_INTERFACE}/{FRONT_LEFT}",
            "VARIABLE-DATA-PROTOTYPE",
        )

    def test_a_stub_ports_element_is_named_under_the_stubs_external_ref(self, model: Path, tmp_path: Path) -> None:
        event = _event(_generate(model, tmp_path / "out.arxml"), REAR_LEFT_NAME)

        assert _reference(event, "ar:DATA-IREF/ar:CONTEXT-R-PORT-REF") == (
            f"{COMPONENT_PATH}/{REAR_PORT}",
            "R-PORT-PROTOTYPE",
        )
        assert _reference(event, "ar:DATA-IREF/ar:TARGET-DATA-ELEMENT-REF") == (
            f"{REAR_STUB_REF}/{REAR_LEFT}",
            "VARIABLE-DATA-PROTOTYPE",
        )

    def test_both_references_resolve_inside_the_document(self, model: Path, tmp_path: Path) -> None:
        """The port and the element are elements the same document emits, by their SHORT-NAMEs."""
        document = _generate(model, tmp_path / "out.arxml")

        ports = document.xpath(
            "//ar:APPLICATION-SW-COMPONENT-TYPE[ar:SHORT-NAME=$swc]/ar:PORTS/ar:R-PORT-PROTOTYPE/ar:SHORT-NAME/text()",
            namespaces=NS,
            swc=COMPONENT,
        )
        elements = document.xpath(
            "//ar:SENDER-RECEIVER-INTERFACE[ar:SHORT-NAME=$iface]/ar:DATA-ELEMENTS/ar:VARIABLE-DATA-PROTOTYPE"
            "/ar:SHORT-NAME/text()",
            namespaces=NS,
            iface=WHEEL_INTERFACE,
        )

        assert WHEEL_PORT in ports
        assert {FRONT_LEFT, FRONT_RIGHT} <= set(elements)

    def test_the_paths_follow_a_stated_package(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: d.update(package="Chassis"))
        change(model, WHEEL_INTERFACE_FILE, lambda d: d.update(package="Wheels"))

        event = _event(_generate(model, tmp_path / "out.arxml"), FRONT_LEFT_NAME)

        assert _reference(event, "ar:DATA-IREF/ar:CONTEXT-R-PORT-REF")[0] == (
            f"/ComponentTypes/Chassis/{COMPONENT}/{WHEEL_PORT}"
        )
        assert _reference(event, "ar:DATA-IREF/ar:TARGET-DATA-ELEMENT-REF")[0] == (
            f"/PortInterfaces/Wheels/{WHEEL_INTERFACE}/{FRONT_LEFT}"
        )
        assert _reference(event, "ar:START-ON-EVENT-REF")[0] == (
            f"/ComponentTypes/Chassis/{COMPONENT}/{COMPONENT}{INTERNAL_BEHAVIOR_SUFFIX}/{FILTER}"
        )

    def test_no_other_event_kind_is_emitted_in_its_place(self, model: Path, tmp_path: Path) -> None:
        """No ``else`` names the kind an init, an operation or a timing event."""
        _component(model, lambda d: d["runnables"].pop(1))  # the monitor, the one timing event
        output = tmp_path / "out.arxml"

        generate_arxml(str(model), str(output))

        text = output.read_text(encoding="utf-8")
        for absent in ("INIT-EVENT", "OPERATION-INVOKED-EVENT", "TIMING-EVENT", "IT_", "OIT_", "TMT_"):
            assert absent not in text, absent


class TestTheName:
    def test_the_prefix_is_the_validators(self) -> None:
        from swcraft_tools.validator import GENERATED_DATA_EVENT_PREFIX

        assert DATA_EVENT_PREFIX == GENERATED_DATA_EVENT_PREFIX == "DRT_"

    def test_the_kind_has_its_own_arms(self) -> None:
        from swcraft_tools.schema import DataEvent, Runnable

        event = DataEvent(type="data", port="P", element="E")

        assert _event_base_name(Runnable(name="R"), event) == "DRT_P_E"
        assert event_element(event) == "DATA-RECEIVED-EVENT"

    def test_an_untaught_kind_still_raises(self) -> None:
        from swcraft_tools.schema import Runnable

        class Unknown:
            name = None

        with pytest.raises(TypeError, match="Unknown"):
            event_element(Unknown())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="Unknown"):
            _event_base_name(Runnable(name="R"), Unknown())  # type: ignore[arg-type]

    def test_a_stated_name_is_written_as_stated(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: _runnable_entry(d, FILTER)["events"][0].update(name="OnFrontLeft"))

        names = [_short_name(event) for event in _events(_generate(model, tmp_path / "out.arxml"))]

        assert names == ["OnFrontLeft", f"TMT_{MONITOR}", FRONT_RIGHT_NAME, REAR_LEFT_NAME]

    def test_one_event_repeated_on_one_runnable_is_suffixed_by_the_shared_counter(
        self, model: Path, tmp_path: Path
    ) -> None:
        """``events`` is a multiset: the retroactive counter names the two ``_0`` / ``_1``."""
        _component(model, lambda d: _runnable_entry(d, FILTER)["events"].append(data_event(WHEEL_PORT, FRONT_LEFT)))
        assert check_cross_layer_consistency(str(model)).records == ()

        names = [_short_name(event) for event in _events(_generate(model, tmp_path / "out.arxml"))]

        assert names[:2] == [f"{FRONT_LEFT_NAME}_0", f"{FRONT_LEFT_NAME}_1"]
        assert FRONT_LEFT_NAME not in names

    def test_two_pairs_deriving_one_base_are_suffixed_by_the_shared_counter(self, model: Path, tmp_path: Path) -> None:
        """``(R, WheelSpeed_FrontLeft)`` and ``(R_WheelSpeed, FrontLeft)`` both derive one base."""
        put(
            model,
            "interfaces/WheelIf.yaml",
            {
                "name": "WheelIf",
                "kind": "sender_receiver",
                "data_elements": [{"name": "WheelSpeed_FrontLeft", "implementation_type": "uint16"}],
            },
        )
        _add_port(model, "R", "require", "WheelIf")
        _add_runnable(model, "Extra", data_event("R", "WheelSpeed_FrontLeft"))
        assert check_cross_layer_consistency(str(model)).records == ()

        names = [_short_name(event) for event in _events(_generate(model, tmp_path / "out.arxml"))]

        assert names.count(f"{FRONT_LEFT_NAME}_0") == 1
        assert names.count(f"{FRONT_LEFT_NAME}_1") == 1
        assert len(names) == len(set(names))

    def test_a_pair_on_the_joint_budget_emits_exactly_the_limit(self, model: Path, tmp_path: Path) -> None:
        port = "R" + "x" * (MAX_IDENTIFIER_LENGTH - len(DATA_EVENT_PREFIX) - 1 - len(FRONT_LEFT) - 1)
        _add_port(model, port, "require", WHEEL_INTERFACE)
        _add_runnable(model, "Extra", data_event(port, FRONT_LEFT))

        names = [_short_name(event) for event in _events(_generate(model, tmp_path / "out.arxml"))]

        boundary = f"DRT_{port}_{FRONT_LEFT}"
        assert len(boundary) == MAX_IDENTIFIER_LENGTH
        assert boundary in names


class TestNothingElseIsWritten:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_no_child_the_model_has_no_field_for(self, model: Path, tmp_path: Path, version: str) -> None:
        document = _generate(model, tmp_path / "out.arxml", version)

        for event in _data_events(document):
            for absent in ("DESC", "CATEGORY", "ACTIVATION-REASON-REPRESENTATION-REF", "VARIATION-POINT", "ADMIN-DATA"):
                assert event.find(f"ar:{absent}", namespaces=NS) is None, (_short_name(event), absent)


# The refusal matrix


def _unknown_port(root: Path) -> None:
    _add_runnable(root, "Extra", data_event("R_Nope", FRONT_LEFT))


def _a_port_of_another_component(root: Path) -> None:
    put(
        root,
        "swc/chassis/Other.yaml",
        {
            "name": "Other",
            "type": "application",
            "ports": [
                {"name": "R_Elsewhere", "direction": "require", "interface": WHEEL_INTERFACE, "com_specs": QUEUE_SPEC}
            ],
            "handle_termination_and_restart": "no_support",
        },
    )
    change(root, "compositions/Root.yaml", lambda d: d["members"].append({"name": "Other", "type": "Other"}))
    _add_runnable(root, "Extra", data_event("R_Elsewhere", FRONT_LEFT))


def _a_provide_port(root: Path) -> None:
    _add_port(root, "P_WheelSpeed", "provide", WHEEL_INTERFACE)
    _add_runnable(root, "Extra", data_event("P_WheelSpeed", FRONT_LEFT))


def _a_client_server_port(root: Path) -> None:
    put(
        root,
        "interfaces/SpeedServiceIf.yaml",
        {"name": "SpeedServiceIf", "kind": "client_server", "operations": [{"name": "Get"}]},
    )
    _add_port(root, "R_SpeedService", "require", "SpeedServiceIf")
    _add_runnable(root, "Extra", data_event("R_SpeedService", FRONT_LEFT))


def _a_mode_switch_stub_port(root: Path) -> None:
    _add_runnable(root, "Extra", data_event(MODE_PORT, "Sleep"))


def _an_unknown_element(root: Path) -> None:
    _add_runnable(root, "Extra", data_event(WHEEL_PORT, "Nope"))


def _an_element_a_stub_does_not_list(root: Path) -> None:
    _add_runnable(root, "Extra", data_event(REAR_PORT, "RearRight"))


def _an_over_budget_pair(root: Path) -> None:
    port = "R" + "x" * (MAX_IDENTIFIER_LENGTH + 1 - len(DATA_EVENT_PREFIX) - 1 - len(FRONT_LEFT) - 1)
    _add_port(root, port, "require", WHEEL_INTERFACE)
    _add_runnable(root, "Extra", data_event(port, FRONT_LEFT))


def _a_bad_disabled_mode(root: Path) -> None:
    bad = {**SLEEP_MODE, "port": WHEEL_PORT}
    _add_runnable(root, "Extra", data_event(WHEEL_PORT, FRONT_LEFT, disabled_modes=[bad]))


def _a_stated_name_that_is_no_identifier(root: Path) -> None:
    _component(root, lambda d: _runnable_entry(d, FILTER)["events"][0].update(name="on front left"))


def _a_variable_named_like_a_derived_name(root: Path) -> None:
    _component(
        root,
        lambda d: d.update(
            variables=[{"name": FRONT_LEFT_NAME.lower(), "access": "explicit", "implementation_type": "uint16"}]
        ),
    )


def _a_runnable_named_like_a_derived_name(root: Path) -> None:
    _component(root, lambda d: d["runnables"].append({"name": FRONT_LEFT_NAME}))


def _a_duplicate_stated_name(root: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        _runnable_entry(document, FILTER)["events"][0]["name"] = "OnSpeed"
        _runnable_entry(document, TRACK_REAR)["events"][0]["name"] = "OnSpeed"

    _component(root, mutate)


#: Each negative class, the change that produces it and the one rule ``check-model`` reports it under.
REFUSED: dict[str, tuple[Any, str]] = {
    "unknown-port": (_unknown_port, "swc.data-event.unknown-port"),
    "a-port-of-another-component": (_a_port_of_another_component, "swc.data-event.unknown-port"),
    "direction-mismatch": (_a_provide_port, "swc.data-event.direction-mismatch"),
    "interface-kind-mismatch-client-server": (_a_client_server_port, "swc.data-event.interface-kind-mismatch"),
    "interface-kind-mismatch-mode-switch-stub": (_a_mode_switch_stub_port, "swc.data-event.interface-kind-mismatch"),
    "unknown-element": (_an_unknown_element, "swc.data-event.unknown-element"),
    "unknown-element-through-a-stub": (_an_element_a_stub_does_not_list, "swc.data-event.unknown-element"),
    "over-budget-pair": (_an_over_budget_pair, "identifier.generated"),
    "bad-disabled-mode": (_a_bad_disabled_mode, "swc.timing_event.disabled-mode-port"),
    "stated-name-no-identifier": (_a_stated_name_that_is_no_identifier, "identifier.declared"),
    "named-like-a-variable": (_a_variable_named_like_a_derived_name, "name.name-space-collision"),
    "named-like-a-runnable": (_a_runnable_named_like_a_derived_name, "name.runnable-event-collision"),
    "duplicate-stated-name": (_a_duplicate_stated_name, "name.duplicate-event"),
}

#: The classes the writer refuses in the validator's words: its message carries every finding verbatim.
SHARED_PREDICATE = frozenset(
    {
        "unknown-port",
        "a-port-of-another-component",
        "direction-mismatch",
        "interface-kind-mismatch-client-server",
        "interface-kind-mismatch-mode-switch-stub",
        "unknown-element",
        "unknown-element-through-a-stub",
        "bad-disabled-mode",
        "named-like-a-variable",
    }
)


def _refusal(root: Path, tmp_path: Path) -> str:
    """Generate into a fresh directory, which must stay empty; return the refusal's text."""
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ValueError) as refusal:
        generate_arxml(str(root), str(out / "model.arxml"))
    assert sorted(entry.name for entry in out.iterdir()) == [], "a refused model must leave no output"
    return str(refusal.value)


class TestTheRefusalMatrix:
    @pytest.mark.parametrize("case", sorted(REFUSED))
    def test_check_model_reports_the_class_as_its_one_finding(self, case: str, model: Path) -> None:
        mutate, rule = REFUSED[case]
        mutate(model)

        result = check_cross_layer_consistency(str(model))

        assert [record.rule for record in (*result.records, *result.warnings)] == [rule]
        assert rule not in WARNING_RULES

    @pytest.mark.parametrize("case", sorted(REFUSED))
    def test_generate_arxml_refuses_it_and_writes_nothing(self, case: str, model: Path, tmp_path: Path) -> None:
        mutate, rule = REFUSED[case]
        mutate(model)

        message = _refusal(model, tmp_path)

        if case in SHARED_PREDICATE:
            findings = [record for record in check_cross_layer_consistency(str(model)).records if record.rule == rule]
            assert findings, "the case must produce a finding, or the loop below proves nothing"
            for finding in findings:
                assert finding.message in message

    def test_the_four_resolution_classes_name_their_rule(self, model: Path, tmp_path: Path) -> None:
        _unknown_port(model)

        assert "rule:swc.data-event.unknown-port: " in _refusal(model, tmp_path)

    def test_the_over_budget_refusal_states_the_joint_budget(self, model: Path, tmp_path: Path) -> None:
        _an_over_budget_pair(model)

        message = _refusal(model, tmp_path)

        assert "DATA-RECEIVED-EVENT" in message
        assert "123 characters between them" in message
        assert "data-received event" in message

    def test_the_overhead_is_read_off_the_suffixed_name(self, model: Path, tmp_path: Path) -> None:
        """Two events of one pair at the boundary: each suffixed name is 130, refused at 121 between them."""
        port = "R" + "x" * (MAX_IDENTIFIER_LENGTH - len(DATA_EVENT_PREFIX) - 1 - len(FRONT_LEFT) - 1)
        _add_port(model, port, "require", WHEEL_INTERFACE)
        _add_runnable(model, "First", data_event(port, FRONT_LEFT))
        _add_runnable(model, "Second", data_event(port, FRONT_LEFT))
        base = f"DRT_{port}_{FRONT_LEFT}"

        message = _refusal(model, tmp_path)

        assert f"'{base}_0'" in message
        assert f"'{base}_1'" in message
        assert "121 characters between them" in message

    def test_a_stated_name_takes_the_derived_form_out_of_the_budget(self, model: Path, tmp_path: Path) -> None:
        _an_over_budget_pair(model)
        _component(model, lambda d: _runnable_entry(d, "Extra")["events"][0].update(name="OnLongPort"))
        assert check_cross_layer_consistency(str(model)).records == ()

        assert "OnLongPort" in [_short_name(event) for event in _events(_generate(model, tmp_path / "out.arxml"))]

    @pytest.mark.parametrize("interface", ["NoSuchIf", None], ids=["undeclared-interface", "no-interface"])
    def test_an_event_through_a_port_whose_interface_does_not_resolve_is_refused(
        self, interface: str | None, model: Path, tmp_path: Path
    ) -> None:
        """The writer refuses an event through a port whose interface does not resolve.

        ``port.undeclared-interface`` owns the port and the event check skips it, but the writer must
        never write an element path naming no interface."""
        port: dict[str, Any] = {"name": "R_Lost", "direction": "require"}
        if interface is not None:
            port["interface"] = interface
        _component(model, lambda d: d["ports"].append(port))
        _add_runnable(model, "Extra", data_event("R_Lost", FRONT_LEFT))

        assert "R_Lost" in _refusal(model, tmp_path)

    def test_every_finding_is_named_in_one_refusal(self, model: Path, tmp_path: Path) -> None:
        """Two classes at once: the refusal batches them, as check-model reports both."""
        _add_runnable(model, "Extra", data_event("R_Nope", FRONT_LEFT), data_event(WHEEL_PORT, "Nope"))

        message = _refusal(model, tmp_path)

        assert "port 'R_Nope'" in message
        assert "element 'Nope'" in message


class TestSilenceGenerates:
    """The silence cases: no finding, so the writer writes them."""

    def test_an_event_beside_a_timing_event(self, model: Path, tmp_path: Path) -> None:
        """The fixture's monitor: the standard's own example (§ 7.2.5)."""
        assert check_cross_layer_consistency(str(model)).records == ()

        events = _events(_generate(model, tmp_path / "out.arxml"))

        monitor = [
            event for event in events if event.findtext("ar:START-ON-EVENT-REF", namespaces=NS).endswith(MONITOR)
        ]
        assert [_tag(event) for event in monitor] == ["TIMING-EVENT", "DATA-RECEIVED-EVENT"]

    def test_an_event_beside_an_init_event(self, model: Path, tmp_path: Path) -> None:
        _component(model, lambda d: _runnable_entry(d, FILTER)["events"].append({"type": "init"}))
        assert check_cross_layer_consistency(str(model)).records == ()

        tags = [_tag(event) for event in _events(_generate(model, tmp_path / "out.arxml"))]

        assert tags[:2] == ["DATA-RECEIVED-EVENT", "INIT-EVENT"]

    def test_an_event_on_an_element_the_runnable_does_not_read(self, model: Path, tmp_path: Path) -> None:
        """The rear tracker reads nothing; its event is written all the same."""
        assert REAR_LEFT_NAME in [_short_name(event) for event in _data_events(_generate(model, tmp_path / "o.arxml"))]

    def test_two_runnables_started_by_one_element(self, model: Path, tmp_path: Path) -> None:
        _add_runnable(model, "Extra", data_event(WHEEL_PORT, FRONT_LEFT))
        assert check_cross_layer_consistency(str(model)).records == ()

        names = [_short_name(event) for event in _data_events(_generate(model, tmp_path / "out.arxml"))]

        assert names.count(f"{FRONT_LEFT_NAME}_0") == 1
        assert names.count(f"{FRONT_LEFT_NAME}_1") == 1

    def test_two_runnables_started_by_one_queued_element_in_two_tasks(self, model: Path, tmp_path: Path) -> None:
        """``[SWS_Rte_07403]``'s queued split is a stated limit, not a rule; written as declared."""
        _component(model, lambda d: _runnable_entry(d, FILTER)["events"].append(data_event(WHEEL_PORT, FRONT_RIGHT)))
        assert tasks_of_runnables_started_by(model, FRONT_RIGHT) == {FILTER: ["Task_10ms"], MONITOR: ["Task_100ms"]}
        assert check_cross_layer_consistency(str(model)).records == ()

        targets = [
            _reference(event, "ar:DATA-IREF/ar:TARGET-DATA-ELEMENT-REF")[0]
            for event in _data_events(_generate(model, tmp_path / "out.arxml"))
        ]

        assert targets.count(f"/PortInterfaces/{WHEEL_INTERFACE}/{FRONT_RIGHT}") == 2

    def test_an_event_on_a_port_no_connector_feeds(self, model: Path, tmp_path: Path) -> None:
        """The fixture has no connector at all: every trigger port is unconnected, and every event is written."""
        assert not (model / "connectors").exists() or not any((model / "connectors").iterdir())

        assert len(_data_events(_generate(model, tmp_path / "out.arxml"))) == 3
