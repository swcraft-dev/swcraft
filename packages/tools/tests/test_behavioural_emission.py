"""The behavioural values and a data element's calibration access on the writer's side.

* **``IS-SERVICE``** on both interface kinds, ``false`` when silent;
* **``SW-DATA-DEF-PROPS``** carries ``sw_calibration_access`` then ``sw_impl_policy`` in one
  conditional, before ``TYPE-TREF`` (XSD 4.2.2 ``:59340``/``:59437``, 4.3.0 ``:68576``/``:68673``);
* **a component port's ``com_specs``** replace the silent values, and a queued element takes the
  queued com-spec kinds (``[TPS_SWCT_01219]``); a composition's require port writes no com-spec
  for a queued element;
* **``DISABLED-MODE-IREFS``** precede ``START-ON-EVENT-REF`` on a timing event;
* **six of the seven behavioural rules are refused by the writer** with the validator's message
  and no file, by a whole build at 4.2.2 and 4.3.0, by a component export whose closure carries the
  offender, and by ``--all-components``, which lists it refused and exports the rest;
* **a SWCraft round trip** of each value.

Does not reach: the XSD validity of the new shapes (``test_arxml_xsd_conformance.py``'s
``TestTheBehaviouralValuesConform``), the goldens (no golden sets a value), ``check-model``'s own
verdicts (``test_behavioural_rules.py``), and the writer's refusal of the seventh rule,
``data-type.conversion-without-unit`` (``test_writer_defaults.py``).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from lxml import etree
from project_support import init_project
from reference_oracle import reference_problems, verbatim_references
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.type_mapping import _SW_CALIBRATION_ACCESS_VALUES, _SW_IMPL_POLICY_VALUES
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import export_all_components, export_component, import_component
from swcraft_tools.component_exchange.export import ClosureValidationError
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import (
    SW_CALIBRATION_ACCESS_LITERALS,
    SW_IMPL_POLICY_LITERALS,
    generate_arxml,
)

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"
VERSIONS = ("4.2.2", "4.3.0")

#: The mode-switch interface stub the disabled-mode cases type their port by: paths no model element holds.
MODE_INTERFACE = "/Ext/ModeInterfaces/EcuModeIf"
MODE_GROUP = f"{MODE_INTERFACE}/ecuMode"
MODES = ("/Ext/ModeGroups/EcuMode/STARTUP", "/Ext/ModeGroups/EcuMode/SHUTDOWN")

Patch = Callable[[Path], None]


# Models


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _change(model_root: Path, relative: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = model_root / relative
    document = _load(path)
    change(document)
    _dump(path, document)


def _port_of(document: dict[str, Any], name: str) -> dict[str, Any]:
    (port,) = [port for port in document["ports"] if port["name"] == name]
    return port


def set_interface(model_root: Path, name: str, **fields: Any) -> None:
    _change(model_root, f"interfaces/{name}.yaml", lambda d: d.update(fields))


def set_element(model_root: Path, interface: str, **fields: Any) -> None:
    _change(model_root, f"interfaces/{interface}.yaml", lambda d: d["data_elements"][0].update(fields))


def set_port(model_root: Path, swc: str, port: str, **fields: Any) -> None:
    relative = {"ActuatorSwc": "swc/actuation/ActuatorSwc.yaml", "SensorSwc": "swc/sensing/SensorSwc.yaml"}[swc]
    _change(model_root, relative, lambda d: _port_of(d, port).update(fields))


def add_mode_port(model_root: Path) -> None:
    """``ActuatorSwc``'s require port ``rMode`` on the mode-switch interface stub ``EcuModeIf``."""
    _dump(
        model_root / "interfaces" / "EcuModeIf.yaml",
        {
            "name": "EcuModeIf",
            "kind": "external_interface",
            "external_ref": MODE_INTERFACE,
            "external_dest": "MODE-SWITCH-INTERFACE",
        },
    )
    _change(
        model_root,
        "swc/actuation/ActuatorSwc.yaml",
        lambda d: d["ports"].append({"name": "rMode", "direction": "require", "interface": "EcuModeIf"}),
    )


def disable_in(model_root: Path, port: str = "rMode", modes: tuple[str, ...] = MODES) -> None:
    """``ActuatorSwc``'s timing event, disabled in each of *modes* through *port*."""

    def change(document: dict[str, Any]) -> None:
        (event,) = document["runnables"][0]["events"]
        event["disabled_modes"] = [{"port": port, "mode_group_ref": MODE_GROUP, "mode_ref": mode} for mode in modes]

    _change(model_root, "swc/actuation/ActuatorSwc.yaml", change)


def exchange_model(root: Path, *patches: Patch) -> Path:
    shutil.copytree(EXCHANGE_MODEL_ROOT, root)
    for patch in patches:
        patch(root)
    return root


#: Every value the four behavioural fields and the calibration access can state, on the
#: ``component_exchange`` tree: shared by the emission, round-trip and XSD cases.
RECEIVER_ENTRY = {
    "data_element": "speed",
    "enable_update": True,
    "alive_timeout": 0.0125,
    "handle_never_received": True,
    "handle_timeout_type": "replace",
    "handle_out_of_range": "saturate",
    "uses_end_to_end_protection": True,
}
SENDER_ENTRY = {"data_element": "speed", "handle_out_of_range": "ignore", "uses_end_to_end_protection": True}
QUEUED_ENTRY = {"data_element": "event", "handle_out_of_range": "default", "queue_length": 4}


def behavioural_model(root: Path) -> Path:
    """The ``component_exchange`` tree stating every behavioural value and a calibration access."""

    def patch(model_root: Path) -> None:
        set_interface(model_root, "IVehicleSpeed", is_service=True)
        set_interface(model_root, "ICalibration", is_service=True)
        set_element(model_root, "IVehicleSpeed", sw_calibration_access="read_only", sw_impl_policy="standard")
        _dump(
            model_root / "interfaces" / "IEvents.yaml",
            {
                "name": "IEvents",
                "kind": "sender_receiver",
                "data_elements": [
                    {
                        "name": "event",
                        "implementation_type": "uint8",
                        "sw_impl_policy": "queued",
                        "sw_calibration_access": "not_accessible",
                    }
                ],
            },
        )
        _change(
            model_root,
            "swc/sensing/SensorSwc.yaml",
            lambda d: d["ports"].append({"name": "pEvents", "direction": "provide", "interface": "IEvents"}),
        )
        _change(
            model_root,
            "swc/actuation/ActuatorSwc.yaml",
            lambda d: d["ports"].append(
                {"name": "rEvents", "direction": "require", "interface": "IEvents", "com_specs": [QUEUED_ENTRY]}
            ),
        )
        set_port(model_root, "ActuatorSwc", "rSpeed", com_specs=[RECEIVER_ENTRY])
        set_port(model_root, "SensorSwc", "pSpeed", com_specs=[SENDER_ENTRY])
        add_mode_port(model_root)
        disable_in(model_root)

    return exchange_model(root, patch)


# Reading the document


def _local(node: etree._Element) -> str:
    return etree.QName(node).localname


def _children(node: etree._Element) -> list[str]:
    return [_local(child) for child in node]


def _named(root: etree._Element, tag: str, name: str) -> etree._Element:
    matches = [node for node in root.iter(f"{NS}{tag}") if node.findtext(f"{NS}SHORT-NAME") == name]
    assert len(matches) == 1, (tag, name, len(matches))
    return matches[0]


def _generate(model_root: Path, out: Path, version: str = "4.2.2") -> etree._Element:
    generate_arxml(str(model_root), str(out), version=version)
    return etree.parse(str(out)).getroot()


def _com_specs(root: etree._Element, owner_tag: str, owner: str, port: str) -> list[etree._Element]:
    """The com-specs of *owner*'s port *port*, in document order (empty when it has no wrapper)."""
    required = port.startswith("r")
    port_node = _named(_named(root, owner_tag, owner), "R-PORT-PROTOTYPE" if required else "P-PORT-PROTOTYPE", port)
    wrapper = port_node.find(f"{NS}{'REQUIRED' if required else 'PROVIDED'}-COM-SPECS")
    return [] if wrapper is None else list(wrapper)


def _swc_com_specs(root: etree._Element, swc: str, port: str) -> list[etree._Element]:
    return _com_specs(root, "APPLICATION-SW-COMPONENT-TYPE", swc, port)


# IS-SERVICE


class TestIsServiceIsWritten:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(
        ("name", "tag"),
        [("IVehicleSpeed", "SENDER-RECEIVER-INTERFACE"), ("ICalibration", "CLIENT-SERVER-INTERFACE")],
        ids=["sender-receiver", "client-server"],
    )
    def test_a_service_interface_states_true(self, tmp_path: Path, version: str, name: str, tag: str) -> None:
        model_root = exchange_model(tmp_path / "model", lambda root: set_interface(root, name, is_service=True))
        root = _generate(model_root, tmp_path / "out.arxml", version)

        interface = _named(root, tag, name)
        assert interface.findtext(f"{NS}IS-SERVICE") == "true"
        assert _children(interface)[1] == "IS-SERVICE"

    def test_a_silent_interface_states_false(self, tmp_path: Path) -> None:
        root = _generate(EXCHANGE_MODEL_ROOT, tmp_path / "out.arxml")

        assert {node.text for node in root.iter(f"{NS}IS-SERVICE")} == {"false"}


# The data element's calibration access and policy


def _conditional(prototype: etree._Element) -> etree._Element:
    (conditional,) = prototype.iter(f"{NS}SW-DATA-DEF-PROPS-CONDITIONAL")
    return conditional


class TestTheDataElementProperties:
    def test_the_writer_literals_are_the_importers_inverted(self) -> None:
        """One spelling each way: the writer's table is the importer's, inverted."""
        assert {literal: value for value, literal in SW_IMPL_POLICY_LITERALS.items()} == _SW_IMPL_POLICY_VALUES
        assert {
            literal: value for value, literal in SW_CALIBRATION_ACCESS_LITERALS.items()
        } == _SW_CALIBRATION_ACCESS_VALUES

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(
        ("access", "literal"),
        [("not_accessible", "NOT-ACCESSIBLE"), ("read_only", "READ-ONLY"), ("read_write", "READ-WRITE")],
    )
    def test_each_calibration_access(self, tmp_path: Path, version: str, access: str, literal: str) -> None:
        model_root = exchange_model(
            tmp_path / "model", lambda root: set_element(root, "IVehicleSpeed", sw_calibration_access=access)
        )
        prototype = _named(_generate(model_root, tmp_path / "out.arxml", version), "VARIABLE-DATA-PROTOTYPE", "speed")

        assert _children(prototype) == ["SHORT-NAME", "SW-DATA-DEF-PROPS", "TYPE-TREF"]
        assert _children(_conditional(prototype)) == ["SW-CALIBRATION-ACCESS"]
        assert _conditional(prototype).findtext(f"{NS}SW-CALIBRATION-ACCESS") == literal

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("policy", "literal"), [("standard", "STANDARD"), ("queued", "QUEUED")])
    def test_each_policy(self, tmp_path: Path, version: str, policy: str, literal: str) -> None:
        def patch(root: Path) -> None:
            set_element(root, "IVehicleSpeed", sw_impl_policy=policy)
            if policy == "queued":
                set_port(root, "ActuatorSwc", "rSpeed", com_specs=[{"data_element": "speed", "queue_length": 1}])

        model_root = exchange_model(tmp_path / "model", patch)
        prototype = _named(_generate(model_root, tmp_path / "out.arxml", version), "VARIABLE-DATA-PROTOTYPE", "speed")

        assert _children(_conditional(prototype)) == ["SW-IMPL-POLICY"]
        assert _conditional(prototype).findtext(f"{NS}SW-IMPL-POLICY") == literal

    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_calibration_access_precedes_the_policy_after_the_description(
        self, tmp_path: Path, version: str
    ) -> None:
        def patch(root: Path) -> None:
            set_element(
                root,
                "IVehicleSpeed",
                description="The vehicle speed.",
                sw_calibration_access="read_write",
                sw_impl_policy="standard",
                init=0.0,
            )

        model_root = exchange_model(tmp_path / "model", patch)
        prototype = _named(_generate(model_root, tmp_path / "out.arxml", version), "VARIABLE-DATA-PROTOTYPE", "speed")

        assert _children(prototype) == ["SHORT-NAME", "DESC", "SW-DATA-DEF-PROPS", "TYPE-TREF", "INIT-VALUE"]
        props = prototype.find(f"{NS}SW-DATA-DEF-PROPS")
        assert _children(props) == ["SW-DATA-DEF-PROPS-VARIANTS"]
        assert _children(props[0]) == ["SW-DATA-DEF-PROPS-CONDITIONAL"]
        assert [(_local(node), node.text) for node in _conditional(prototype)] == [
            ("SW-CALIBRATION-ACCESS", "READ-WRITE"),
            ("SW-IMPL-POLICY", "STANDARD"),
        ]

    def test_a_silent_element_writes_no_props(self, tmp_path: Path) -> None:
        root = _generate(EXCHANGE_MODEL_ROOT, tmp_path / "out.arxml")

        prototypes = list(root.iter(f"{NS}VARIABLE-DATA-PROTOTYPE"))
        assert prototypes
        assert [p for p in prototypes if p.find(f"{NS}SW-DATA-DEF-PROPS") is not None] == []


# A component port's com_specs


class TestTheComSpecValues:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_receiver_states_every_value_in_the_xsds_order(self, tmp_path: Path, version: str) -> None:
        model_root = exchange_model(
            tmp_path / "model", lambda root: set_port(root, "ActuatorSwc", "rSpeed", com_specs=[RECEIVER_ENTRY])
        )
        (spec,) = _swc_com_specs(_generate(model_root, tmp_path / "out.arxml", version), "ActuatorSwc", "rSpeed")

        assert _local(spec) == "NONQUEUED-RECEIVER-COM-SPEC"
        assert [(_local(node), node.text) for node in spec][1:] == [
            ("HANDLE-OUT-OF-RANGE", "SATURATE"),
            ("USES-END-TO-END-PROTECTION", "true"),
            ("ALIVE-TIMEOUT", "0.0125"),
            ("ENABLE-UPDATE", "true"),
            ("HANDLE-NEVER-RECEIVED", "true"),
            ("HANDLE-TIMEOUT-TYPE", "REPLACE"),
        ]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_sender_states_its_two_values(self, tmp_path: Path, version: str) -> None:
        model_root = exchange_model(
            tmp_path / "model", lambda root: set_port(root, "SensorSwc", "pSpeed", com_specs=[SENDER_ENTRY])
        )
        (spec,) = _swc_com_specs(_generate(model_root, tmp_path / "out.arxml", version), "SensorSwc", "pSpeed")

        assert _local(spec) == "NONQUEUED-SENDER-COM-SPEC"
        assert [(_local(node), node.text) for node in spec][1:] == [
            ("HANDLE-OUT-OF-RANGE", "IGNORE"),
            ("USES-END-TO-END-PROTECTION", "true"),
        ]

    @pytest.mark.parametrize("handle", ["none", "default", "ignore", "saturate"])
    def test_each_out_of_range_literal(self, tmp_path: Path, handle: str) -> None:
        model_root = exchange_model(
            tmp_path / "model",
            lambda root: set_port(
                root, "ActuatorSwc", "rSpeed", com_specs=[{"data_element": "speed", "handle_out_of_range": handle}]
            ),
        )
        (spec,) = _swc_com_specs(_generate(model_root, tmp_path / "out.arxml"), "ActuatorSwc", "rSpeed")

        assert spec.findtext(f"{NS}HANDLE-OUT-OF-RANGE") == handle.upper()

    def test_an_attribute_the_entry_leaves_silent_keeps_the_silent_value(self, tmp_path: Path) -> None:
        model_root = exchange_model(
            tmp_path / "model",
            lambda root: set_port(
                root, "ActuatorSwc", "rSpeed", com_specs=[{"data_element": "speed", "enable_update": True}]
            ),
        )
        (spec,) = _swc_com_specs(_generate(model_root, tmp_path / "out.arxml"), "ActuatorSwc", "rSpeed")

        assert [(_local(node), node.text) for node in spec][1:] == [
            ("HANDLE-OUT-OF-RANGE", "NONE"),
            ("USES-END-TO-END-PROTECTION", "false"),
            ("ALIVE-TIMEOUT", "0"),
            ("ENABLE-UPDATE", "true"),
            ("HANDLE-NEVER-RECEIVED", "false"),
            ("HANDLE-TIMEOUT-TYPE", "NONE"),
        ]

    def test_a_stated_silent_value_writes_the_same_bytes_as_silence(self, tmp_path: Path) -> None:
        stated = {
            "data_element": "speed",
            "enable_update": False,
            "alive_timeout": 0.0,
            "handle_never_received": False,
            "handle_timeout_type": "none",
            "handle_out_of_range": "none",
            "uses_end_to_end_protection": False,
        }
        model_root = exchange_model(
            tmp_path / "model", lambda root: set_port(root, "ActuatorSwc", "rSpeed", com_specs=[stated])
        )
        generate_arxml(str(model_root), str(tmp_path / "stated.arxml"))
        generate_arxml(str(EXCHANGE_MODEL_ROOT), str(tmp_path / "silent.arxml"))

        assert (tmp_path / "stated.arxml").read_bytes() == (tmp_path / "silent.arxml").read_bytes()


# The queued com-specs


def _queued_speed(queue_length: int | None = 3) -> Patch:
    def patch(root: Path) -> None:
        set_element(root, "IVehicleSpeed", sw_impl_policy="queued", init=0.0)
        if queue_length is not None:
            set_port(root, "ActuatorSwc", "rSpeed", com_specs=[{"data_element": "speed", "queue_length": queue_length}])

    return patch


class TestTheQueuedComSpecs:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_queued_sender_and_receiver(self, tmp_path: Path, version: str) -> None:
        root = _generate(exchange_model(tmp_path / "model", _queued_speed()), tmp_path / "out.arxml", version)

        (sender,) = _swc_com_specs(root, "SensorSwc", "pSpeed")
        (receiver,) = _swc_com_specs(root, "ActuatorSwc", "rSpeed")
        assert _local(sender) == "QUEUED-SENDER-COM-SPEC"
        assert [(_local(node), node.text) for node in sender][1:] == [
            ("HANDLE-OUT-OF-RANGE", "NONE"),
            ("USES-END-TO-END-PROTECTION", "false"),
        ]
        assert _local(receiver) == "QUEUED-RECEIVER-COM-SPEC"
        assert [(_local(node), node.text) for node in receiver][1:] == [
            ("HANDLE-OUT-OF-RANGE", "NONE"),
            ("USES-END-TO-END-PROTECTION", "false"),
            ("QUEUE-LENGTH", "3"),
        ]
        for spec in (sender, receiver):
            assert spec.find(f"{NS}DATA-ELEMENT-REF").text == "/PortInterfaces/IVehicleSpeed/speed"
        # The element's init stays on the prototype; neither queued kind has an INIT-VALUE.
        prototype = _named(root, "VARIABLE-DATA-PROTOTYPE", "speed")
        assert _children(prototype)[-1] == "INIT-VALUE"

    def test_a_queued_receiver_states_its_other_values(self, tmp_path: Path) -> None:
        def patch(root: Path) -> None:
            _queued_speed(None)(root)
            set_port(
                root,
                "ActuatorSwc",
                "rSpeed",
                com_specs=[
                    {
                        "data_element": "speed",
                        "queue_length": 8,
                        "handle_out_of_range": "saturate",
                        "uses_end_to_end_protection": True,
                    }
                ],
            )

        root = _generate(exchange_model(tmp_path / "model", patch), tmp_path / "out.arxml")
        (receiver,) = _swc_com_specs(root, "ActuatorSwc", "rSpeed")

        assert [(_local(node), node.text) for node in receiver][1:] == [
            ("HANDLE-OUT-OF-RANGE", "SATURATE"),
            ("USES-END-TO-END-PROTECTION", "true"),
            ("QUEUE-LENGTH", "8"),
        ]

    def test_a_non_queued_element_beside_a_queued_one_keeps_its_kind(self, tmp_path: Path) -> None:
        root = _generate(behavioural_model(tmp_path / "model"), tmp_path / "out.arxml")

        assert [_local(spec) for spec in _swc_com_specs(root, "ActuatorSwc", "rEvents")] == ["QUEUED-RECEIVER-COM-SPEC"]
        assert [_local(spec) for spec in _swc_com_specs(root, "ActuatorSwc", "rSpeed")] == [
            "NONQUEUED-RECEIVER-COM-SPEC"
        ]
        assert [_local(spec) for spec in _swc_com_specs(root, "SensorSwc", "pEvents")] == ["QUEUED-SENDER-COM-SPEC"]


def _mixed_composition_ports(root: Path) -> None:
    """``IMixed`` (a queued ``a``, a standard ``b``) and ``IQueued`` (queued only), each on ``Root``'s ports."""
    _dump(
        root / "interfaces" / "IMixed.yaml",
        {
            "name": "IMixed",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "a", "implementation_type": "uint8", "sw_impl_policy": "queued"},
                {"name": "b", "implementation_type": "uint8"},
            ],
        },
    )
    _dump(
        root / "interfaces" / "IQueued.yaml",
        {
            "name": "IQueued",
            "kind": "sender_receiver",
            "data_elements": [{"name": "q", "implementation_type": "uint8", "sw_impl_policy": "queued"}],
        },
    )
    _change(
        root,
        "compositions/Root.yaml",
        lambda d: d.update(
            ports=[
                {"name": "rMixed", "direction": "require", "interface": "IMixed"},
                {"name": "rQueued", "direction": "require", "interface": "IQueued"},
                {"name": "pQueued", "direction": "provide", "interface": "IQueued"},
            ]
        ),
    )


class TestACompositionPortOnAQueuedElement:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_its_require_port_writes_no_com_spec_for_the_queued_element(self, tmp_path: Path, version: str) -> None:
        root = _generate(exchange_model(tmp_path / "model", _mixed_composition_ports), tmp_path / "out.arxml", version)

        (spec,) = _com_specs(root, "COMPOSITION-SW-COMPONENT-TYPE", "Root", "rMixed")
        assert _local(spec) == "NONQUEUED-RECEIVER-COM-SPEC"
        assert spec.findtext(f"{NS}DATA-ELEMENT-REF") == "/PortInterfaces/IMixed/b"

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_port_left_with_no_com_spec_writes_no_wrapper(self, tmp_path: Path, version: str) -> None:
        root = _generate(exchange_model(tmp_path / "model", _mixed_composition_ports), tmp_path / "out.arxml", version)

        port = _named(_named(root, "COMPOSITION-SW-COMPONENT-TYPE", "Root"), "R-PORT-PROTOTYPE", "rQueued")
        assert _children(port) == ["SHORT-NAME", "REQUIRED-INTERFACE-TREF"]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_its_provide_port_writes_the_queued_sender(self, tmp_path: Path, version: str) -> None:
        """``[TPS_SWCT_01219]`` (SWCT R4.2.2 ``:7734``, R4.3.0 ``:8221``): a queued element takes the queued kind.

        A queued sender needs no value a composition port lacks, so it is written.
        """
        root = _generate(exchange_model(tmp_path / "model", _mixed_composition_ports), tmp_path / "out.arxml", version)

        (spec,) = _com_specs(root, "COMPOSITION-SW-COMPONENT-TYPE", "Root", "pQueued")
        assert _local(spec) == "QUEUED-SENDER-COM-SPEC"
        assert [(_local(node), node.text) for node in spec] == [
            ("DATA-ELEMENT-REF", "/PortInterfaces/IQueued/q"),
            ("HANDLE-OUT-OF-RANGE", "NONE"),
            ("USES-END-TO-END-PROTECTION", "false"),
        ]

    def test_check_model_reports_nothing_about_it(self, tmp_path: Path) -> None:
        model_root = exchange_model(tmp_path / "model", _mixed_composition_ports)

        rules = {record.rule for record in check_cross_layer_consistency(str(model_root)).records}
        assert "swc.port.queued-receiver-without-queue-length" not in rules


# DISABLED-MODE-IREFS


class TestTheDisabledModes:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_each_mode_is_an_instance_reference_before_the_start_reference(self, tmp_path: Path, version: str) -> None:
        def patch(root: Path) -> None:
            add_mode_port(root)
            disable_in(root)

        root = _generate(exchange_model(tmp_path / "model", patch), tmp_path / "out.arxml", version)
        event = _named(root, "TIMING-EVENT", "TMT_ActuatorMain")

        assert _children(event) == ["SHORT-NAME", "DISABLED-MODE-IREFS", "START-ON-EVENT-REF", "PERIOD"]
        irefs = list(event.find(f"{NS}DISABLED-MODE-IREFS"))
        assert [_local(iref) for iref in irefs] == ["DISABLED-MODE-IREF", "DISABLED-MODE-IREF"]
        for iref, mode in zip(irefs, MODES):
            assert [(_local(node), node.get("DEST"), node.text) for node in iref] == [
                ("CONTEXT-PORT-REF", "R-PORT-PROTOTYPE", "/ComponentTypes/ActuatorSwc/rMode"),
                ("CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF", "MODE-DECLARATION-GROUP-PROTOTYPE", MODE_GROUP),
                ("TARGET-MODE-DECLARATION-REF", "MODE-DECLARATION", mode),
            ]

    def test_the_context_port_is_placed(self, tmp_path: Path) -> None:
        def patch(root: Path) -> None:
            add_mode_port(root)
            disable_in(root, modes=MODES[:1])
            _change(root, "swc/actuation/ActuatorSwc.yaml", lambda d: d.update(package="Actuation"))

        root = _generate(exchange_model(tmp_path / "model", patch), tmp_path / "out.arxml")

        (port_ref,) = _named(root, "TIMING-EVENT", "TMT_ActuatorMain").iter(f"{NS}CONTEXT-PORT-REF")
        assert port_ref.text == "/ComponentTypes/Actuation/ActuatorSwc/rMode"

    def test_only_the_mode_paths_the_model_holds_lie_outside_the_document(self, tmp_path: Path) -> None:
        """The reference oracle's exemption for a disabled mode is its two exact strings, and nothing more.

        A path below an exempted one, or the same tag naming another path, still fails.
        """
        model_root = behavioural_model(tmp_path / "model")
        out = tmp_path / "out.arxml"
        generate_arxml(str(model_root), str(out))
        stubs = frozenset({MODE_INTERFACE})
        mode_paths = frozenset({MODE_GROUP, *MODES})

        unexempted = reference_problems(out, external=stubs)
        assert unexempted == [f"TARGET-MODE-DECLARATION-REF {mode} names no element of the document" for mode in MODES]
        assert reference_problems(out, external=stubs, verbatim=mode_paths) == []
        assert [reference.tag for reference in verbatim_references(out, mode_paths)] == [
            "CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF",
            "TARGET-MODE-DECLARATION-REF",
        ] * 2

        text = out.read_text(encoding="utf-8")
        moved = text.replace(f">{MODES[0]}<", f">{MODES[0]}/below<").replace(f">{MODES[1]}<", ">/Ext/Other<")
        assert reference_problems(moved, external=stubs, verbatim=mode_paths) == [
            f"TARGET-MODE-DECLARATION-REF {MODES[0]}/below names no element of the document",
            "TARGET-MODE-DECLARATION-REF /Ext/Other names no element of the document",
        ]

    def test_an_event_with_none_writes_no_wrapper(self, tmp_path: Path) -> None:
        root = _generate(EXCHANGE_MODEL_ROOT, tmp_path / "out.arxml")

        assert list(root.iter(f"{NS}DISABLED-MODE-IREFS")) == []


# The writer refuses every rule check-model reports, with its message


def _unknown_element(root: Path) -> None:
    set_port(root, "ActuatorSwc", "rSpeed", com_specs=[{"data_element": "nothing"}])


def _inapplicable(root: Path) -> None:
    set_port(root, "SensorSwc", "pSpeed", com_specs=[{"data_element": "speed", "enable_update": True}])


def _without_queue_length(root: Path) -> None:
    _queued_speed(None)(root)


def _disabled_mode_port(root: Path) -> None:
    disable_in(root, port="rSpeed", modes=MODES[:1])


def _queued_read_by_value(root: Path) -> None:
    _queued_speed(1)(root)
    _change(
        root,
        "swc/actuation/ActuatorSwc.yaml",
        lambda d: d["runnables"][0].update(reads=[{"port": "rSpeed", "element": "speed", "by": "value"}]),
    )


def _service_composition_port(root: Path) -> None:
    set_interface(root, "IVehicleSpeed", is_service=True)
    _change(
        root,
        "compositions/Root.yaml",
        lambda d: d.update(ports=[{"name": "rSpeedIn", "direction": "require", "interface": "IVehicleSpeed"}]),
    )


#: Six of the seven behavioural rules the writer twin refuses (the seventh,
#: ``data-type.conversion-without-unit``, is ``test_writer_defaults.py``'s), the shape that breaks
#: each, and whether a component closure carries the offending element (a composition never is).
GUARDED = [
    pytest.param("swc.port.com-spec-unknown-element", _unknown_element, "ActuatorSwc", id="com-spec-unknown-element"),
    pytest.param("swc.port.com-spec-inapplicable", _inapplicable, "SensorSwc", id="com-spec-inapplicable"),
    pytest.param(
        "swc.port.queued-receiver-without-queue-length",
        _without_queue_length,
        "ActuatorSwc",
        id="queued-receiver-without-queue-length",
    ),
    pytest.param("swc.timing_event.disabled-mode-port", _disabled_mode_port, "ActuatorSwc", id="disabled-mode-port"),
    pytest.param("swc.access.queued-read-by-value", _queued_read_by_value, "ActuatorSwc", id="queued-read-by-value"),
    pytest.param("composition.port-service-interface", _service_composition_port, None, id="port-service-interface"),
]


def _reported(model_root: Path, rule: str) -> list[str]:
    messages = [
        record.message for record in check_cross_layer_consistency(str(model_root)).records if record.rule == rule
    ]
    assert messages, rule
    return messages


class TestTheWriterRefusesWhatCheckModelReports:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("rule", "patch", "swc"), GUARDED)
    def test_the_writer_refuses_with_the_validators_message_and_writes_nothing(
        self, tmp_path: Path, version: str, rule: str, patch: Patch, swc: str | None
    ) -> None:
        model_root = exchange_model(tmp_path / "model", patch)
        out = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model_root), str(out), version=version)

        for message in _reported(model_root, rule):
            assert f"rule:{rule}: {message}" in str(caught.value)
        assert not out.parent.exists()

    def test_the_guards_run_in_the_validators_order(self, tmp_path: Path) -> None:
        """Two offenders: the first guard in ``check_cross_layer_consistency``'s order refuses."""

        def patch(root: Path) -> None:
            _service_composition_port(root)
            _unknown_element(root)

        model_root = exchange_model(tmp_path / "model", patch)
        with pytest.raises(ValueError) as caught:
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

        assert "rule:swc.port.com-spec-unknown-element" in str(caught.value)
        assert "composition.port-service-interface" not in str(caught.value)

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("rule", "patch", "swc"), [p for p in GUARDED if p.values[2] is not None])
    def test_a_component_export_refuses_the_same(
        self, tmp_path: Path, version: str, rule: str, patch: Patch, swc: str
    ) -> None:
        model_root = exchange_model(tmp_path / "model", patch)
        out = tmp_path / f"{swc}.arxml"

        with pytest.raises(ClosureValidationError) as caught:
            export_component(str(model_root), swc, str(out), version=version)

        assert caught.value.issues == _reported(model_root, rule)
        assert not out.exists()

    def test_a_component_export_refuses_a_text_xml_cannot_carry_as_a_closure_refusal(self, tmp_path: Path) -> None:
        """Not the writer's bare ``ValueError`` in the middle of the emission."""
        rule = "text.xml-unrepresentable-character"
        model_root = exchange_model(
            tmp_path / "model", lambda root: set_port(root, "ActuatorSwc", "rSpeed", description="bell\x07")
        )
        out = tmp_path / "ActuatorSwc.arxml"

        with pytest.raises(ClosureValidationError) as caught:
            export_component(str(model_root), "ActuatorSwc", str(out))

        assert caught.value.issues == _reported(model_root, rule)
        assert len(caught.value.issues) == 1
        assert not out.exists()

    @pytest.mark.parametrize(("rule", "patch", "swc"), [p for p in GUARDED if p.values[2] is not None])
    def test_all_components_lists_the_refused_one_and_goes_on(
        self, tmp_path: Path, rule: str, patch: Patch, swc: str
    ) -> None:
        model_root = exchange_model(tmp_path / "model", patch)
        out_dir = tmp_path / "out"

        reports = {report.swc: report for report in export_all_components(str(model_root), str(out_dir))}

        assert reports[swc].status != "ok"
        for message in _reported(model_root, rule):
            assert message in (reports[swc].error or "")
        assert not (out_dir / f"{swc}.arxml").exists()
        other = "SensorSwc" if swc == "ActuatorSwc" else "ActuatorSwc"
        assert reports[other].status == "ok"

    def test_a_component_export_carries_no_composition_so_the_service_port_does_not_block_it(
        self, tmp_path: Path
    ) -> None:
        model_root = exchange_model(tmp_path / "model", _service_composition_port)
        out = tmp_path / "ActuatorSwc.arxml"

        export_component(str(model_root), "ActuatorSwc", str(out))

        assert out.exists()
        assert list(etree.parse(str(out)).getroot().iter(f"{NS}COMPOSITION-SW-COMPONENT-TYPE")) == []


# The SWCraft round trip: build, import, the same values


def _file_of(model_root: Path, layer: str, name: str) -> dict[str, Any]:
    (path,) = [path for path in (model_root / layer).rglob("*.yaml") if _load(path).get("name") == name]
    return _load(path)


@pytest.fixture(scope="module")
def round_trip(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """The behavioural model, and the model ``import model`` makes of its whole-model ARXML."""
    base = tmp_path_factory.mktemp("round-trip")
    model_root = behavioural_model(base / "model")
    out = base / "model.arxml"
    generate_arxml(str(model_root), str(out))
    project = init_project(base / "proj")
    run_import(str(out), str(project / "swcraft.yaml"), force=True)
    return model_root, project / "model"


class TestTheRoundTrip:
    @pytest.mark.parametrize("name", ["IVehicleSpeed", "ICalibration", "IActuatorStatus", "IPose"])
    def test_is_service(self, round_trip: tuple[Path, Path], name: str) -> None:
        source, imported = round_trip
        assert _file_of(imported, "interfaces", name).get("is_service", False) == _file_of(
            source, "interfaces", name
        ).get("is_service", False)

    @pytest.mark.parametrize("name", ["IVehicleSpeed", "IEvents", "IActuatorStatus"])
    def test_the_data_element_properties(self, round_trip: tuple[Path, Path], name: str) -> None:
        source, imported = round_trip
        keys = ("sw_impl_policy", "sw_calibration_access")
        (written,) = _file_of(source, "interfaces", name)["data_elements"]
        (read,) = _file_of(imported, "interfaces", name)["data_elements"]
        assert {key: read.get(key) for key in keys} == {key: written.get(key) for key in keys}

    @pytest.mark.parametrize(
        ("swc", "port"),
        [("ActuatorSwc", "rSpeed"), ("ActuatorSwc", "rEvents"), ("SensorSwc", "pSpeed"), ("SensorSwc", "pEvents")],
    )
    def test_the_com_specs(self, round_trip: tuple[Path, Path], swc: str, port: str) -> None:
        source, imported = round_trip
        assert _port_of(_file_of(imported, "swc", swc), port).get("com_specs", []) == _port_of(
            _file_of(source, "swc", swc), port
        ).get("com_specs", [])

    def test_the_disabled_modes(self, round_trip: tuple[Path, Path]) -> None:
        source, imported = round_trip
        (written,) = _file_of(source, "swc", "ActuatorSwc")["runnables"][0]["events"]
        (read,) = _file_of(imported, "swc", "ActuatorSwc")["runnables"][0]["events"]
        assert read == written
        assert _port_of(_file_of(imported, "swc", "ActuatorSwc"), "rMode")["direction"] == "require"

    def test_a_component_export_round_trips_the_com_specs(self, tmp_path: Path) -> None:
        """``export`` then ``import component`` into the tree it came from: unchanged, the values included."""

        def patch(root: Path) -> None:
            _queued_speed(5)(root)
            _change(
                root,
                "swc/actuation/ActuatorSwc.yaml",
                lambda d: _port_of(d, "rSpeed")["com_specs"][0].update(
                    handle_out_of_range="saturate", uses_end_to_end_protection=True
                ),
            )
            set_interface(root, "IVehicleSpeed", is_service=True)
            set_element(root, "IVehicleSpeed", sw_calibration_access="not_accessible")

        project = init_project(tmp_path / "target")
        shutil.rmtree(project / "model")
        model_root = exchange_model(project / "model", patch)
        out = tmp_path / "ActuatorSwc.arxml"
        export_component(str(model_root), "ActuatorSwc", str(out))

        report = import_component(str(out), str(model_root))

        assert report.swc_action == "unchanged"
