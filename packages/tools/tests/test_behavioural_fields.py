"""The behavioural fields: declared, loaded, refused and written back unchanged.

``is_service`` on both interface kinds, a data element's ``sw_impl_policy`` and
``sw_calibration_access``, a port's ``com_specs`` and a timing event's ``disabled_modes``:
the contract declares them, each loads and defaults, is written back last in its kind and
left out while unset, survives every mutation that rewrites its component, and the
element-local rules refuse what they name.

Does not reach the ARXML side (``test_behavioural_import.py``,
``test_behavioural_emission.py``) or the cross-element rules (``test_behavioural_rules.py``);
only the rule count is asserted here.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
import yaml
from model_contract_fixtures import load_binding, load_contract
from pydantic import ValidationError
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import (
    CSInterface,
    DataElement,
    DisabledMode,
    Port,
    PortComSpec,
    SRInterface,
    SWComponent,
    TimingEvent,
)
from swcraft_tools.writer import (
    add_init_event,
    add_port,
    add_runnable,
    add_timing_event,
    set_swc_memory_budget,
    unset_swc_memory_budget,
    write_element,
)
from swcraft_tools.writer.yaml_documents import _event_to_dict, _port_com_spec_to_dict, swc_to_dict

# The contract


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


def _fields(kind_id: str) -> dict[str, dict[str, Any]]:
    return {field["name"]: field for field in _kinds()[kind_id]["fields"]}


#: ``field id -> (owning kind, the field's declared default, omitAtDefault)``.
ADDED_FIELDS: dict[str, tuple[str, Any, bool]] = {
    "field:interfaces.sender_receiver.is_service": ("kind:interfaces.sender_receiver", False, True),
    "field:interfaces.client_server.is_service": ("kind:interfaces.client_server", False, True),
    "field:interfaces.data_element.sw_impl_policy": ("kind:interfaces.data_element", None, False),
    "field:interfaces.data_element.sw_calibration_access": ("kind:interfaces.data_element", None, False),
    "field:swc.port.com_specs": ("kind:swc.port", [], True),
    "field:swc.timing_event.disabled_modes": ("kind:swc.timing_event", [], True),
}


class TestTheContractDeclaresTheFields:
    def test_the_version_and_the_counts_are_the_minor_s(self) -> None:
        contract = load_contract()
        kinds = [kind for layer in contract["layers"] for kind in layer["kinds"]]
        fields = [field for kind in kinds for field in kind["fields"]]
        # A minor or a major of the contract moves the version.
        assert contract["contractVersion"] == "12.7.1"
        # Layers, kinds and fields the contract declares; a kind or field added or removed moves them.
        assert (len(contract["layers"]), len(kinds), len(fields)) == (15, 67, 359)
        # Every rule the contract declares, by family; a rule added or removed moves them.
        assert len(contract["rules"]) == 352
        families = [rule["family"] for rule in contract["rules"]]
        assert (families.count("cross-element"), families.count("element-local")) == (207, 144)

    @pytest.mark.parametrize("field_id", sorted(ADDED_FIELDS))
    def test_each_is_optional_and_last_in_its_kind(self, field_id: str) -> None:
        kind_id, default, omitted = ADDED_FIELDS[field_id]
        # The timing event's `name` and every kind's `satisfies` follow these fields; left out.
        fields = [
            field
            for field in _kinds()[kind_id]["fields"]
            if field["id"] != "field:swc.timing_event.name" and field["name"] != "satisfies"
        ]
        added = [field for field in fields if field["id"] in ADDED_FIELDS]
        assert fields[-len(added) :] == added
        declared = next(field for field in fields if field["id"] == field_id)
        assert declared["required"] is False
        assert declared["default"] == default
        assert declared.get("omitAtDefault", False) is omitted

    def test_the_two_member_kinds_and_their_bindings(self) -> None:
        kinds = _kinds()
        binding = load_binding()["kinds"]
        port_com_spec = kinds["kind:swc.port_com_spec"]
        disabled_mode = kinds["kind:swc.disabled_mode"]
        assert port_com_spec["memberOf"] == [{"kind": "port", "field": "com_specs"}]
        # The data-received event holds the same list.
        assert disabled_mode["memberOf"] == [
            {"kind": "timing_event", "field": "disabled_modes"},
            {"kind": "data_event", "field": "disabled_modes"},
        ]
        assert [field["name"] for field in port_com_spec["fields"]] == [
            "data_element",
            "enable_update",
            "alive_timeout",
            "handle_never_received",
            "handle_timeout_type",
            "handle_out_of_range",
            "uses_end_to_end_protection",
            "queue_length",
            # The port's own init value.
            "init",
        ]
        assert [(field["name"], field["required"]) for field in port_com_spec["fields"]][0] == ("data_element", True)
        assert all(not field["required"] for field in port_com_spec["fields"][1:])
        assert [(field["name"], field["required"], field["immutable"]) for field in disabled_mode["fields"]] == [
            ("port", True, True),
            ("mode_group_ref", True, True),
            ("mode_ref", True, True),
        ]
        assert binding["kind:swc.port_com_spec"]["class"] == "PortComSpec"
        assert binding["kind:swc.disabled_mode"]["class"] == "DisabledMode"

    def test_the_collections_are_the_list_shapes_the_plan_decided(self) -> None:
        com_specs = _fields("kind:swc.port")["com_specs"]["collection"]
        disabled_modes = _fields("kind:swc.timing_event")["disabled_modes"]["collection"]
        assert com_specs == {
            "kind": "list",
            "memberIdentity": "field",
            "memberKinds": ["port_com_spec"],
            "identityField": "data_element",
        }
        assert disabled_modes == {"kind": "list", "memberIdentity": "whole_value", "memberKinds": ["disabled_mode"]}
        assert _fields("kind:swc.timing_event")["disabled_modes"]["immutable"] is True

    def test_the_enum_spellings_are_snake_case(self) -> None:
        element = _fields("kind:interfaces.data_element")
        com_spec = _fields("kind:swc.port_com_spec")
        assert element["sw_impl_policy"]["enum"] == ["standard", "queued"]
        assert element["sw_calibration_access"]["enum"] == ["not_accessible", "read_only", "read_write"]
        assert com_spec["handle_timeout_type"]["enum"] == ["none", "replace"]
        assert com_spec["handle_out_of_range"]["enum"] == ["none", "default", "ignore", "saturate"]
        assert com_spec["alive_timeout"]["unit"] == "seconds"

    def test_float_finite_governs_the_alive_timeout(self) -> None:
        rule = next(rule for rule in load_contract()["rules"] if rule["id"] == "rule:model.float-finite")
        assert "field:swc.port_com_spec.alive_timeout" in rule["appliesTo"]


# Loading and defaults


class TestEachFieldLoadsAndDefaults:
    def test_the_defaults(self) -> None:
        assert (
            SRInterface(
                name="I", kind="sender_receiver", data_elements=[{"name": "e", "implementation_type": "t"}]
            ).is_service
            is False
        )
        assert CSInterface(name="I", kind="client_server", operations=[{"name": "Op"}]).is_service is False
        element = DataElement(name="e", implementation_type="t")
        assert (element.sw_impl_policy, element.sw_calibration_access) == (None, None)
        assert Port(name="p", direction="require", interface="I").com_specs == []
        assert TimingEvent(type="timing", period=0.01).disabled_modes == []
        entry = PortComSpec(data_element="e")
        assert [getattr(entry, name) for name in PortComSpec.model_fields if name != "data_element"] == [None] * 8

    def test_a_stated_value_loads(self) -> None:
        port = Port.model_validate(
            {
                "name": "rIn",
                "direction": "require",
                "interface": "I",
                "com_specs": [
                    {
                        "data_element": "speed",
                        "enable_update": True,
                        "alive_timeout": 0.5,
                        "handle_never_received": True,
                        "handle_timeout_type": "replace",
                        "handle_out_of_range": "saturate",
                        "uses_end_to_end_protection": True,
                    },
                    {"data_element": "samples", "queue_length": 20},
                ],
            }
        )
        assert port.com_specs[0].alive_timeout == 0.5 and port.com_specs[1].queue_length == 20
        event = TimingEvent.model_validate(
            {
                "type": "timing",
                "period": 0.01,
                "disabled_modes": [{"port": "rMode", "mode_group_ref": "/Ifs/IMode/mode", "mode_ref": "/Modes/M/OFF"}],
            }
        )
        assert event.disabled_modes[0].mode_ref == "/Modes/M/OFF"


# The element-local rules


class TestTheElementLocalRulesRefuse:
    @pytest.mark.parametrize("value", [-0.001, math.inf, math.nan])
    def test_an_alive_timeout_that_is_negative_or_not_finite(self, value: float) -> None:
        with pytest.raises(ValidationError):
            PortComSpec(data_element="e", alive_timeout=value)

    def test_a_zero_alive_timeout_is_legal(self) -> None:
        assert PortComSpec(data_element="e", alive_timeout=0).alive_timeout == 0

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_queue_length_below_one(self, value: int) -> None:
        with pytest.raises(ValidationError, match="queue_length must be >= 1"):
            PortComSpec(data_element="e", queue_length=value)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("mode_group_ref", "Ifs/IMode/mode"),
            ("mode_group_ref", ""),
            ("mode_ref", "/Modes/M/OFF STATE"),
            ("mode_ref", "/Modes//OFF"),
            ("mode_ref", "/1Modes/OFF"),
        ],
    )
    def test_a_malformed_disabled_mode_reference(self, field: str, value: str) -> None:
        document = {"port": "rMode", "mode_group_ref": "/Ifs/IMode/mode", "mode_ref": "/Modes/M/OFF"}
        with pytest.raises(ValidationError, match="absolute path of AUTOSAR identifiers"):
            DisabledMode.model_validate({**document, field: value})

    def test_two_entries_of_one_data_element(self) -> None:
        with pytest.raises(ValidationError, match="duplicate com_specs entry for data element 'speed'"):
            Port.model_validate(
                {
                    "name": "rIn",
                    "direction": "require",
                    "interface": "I",
                    "com_specs": [{"data_element": "speed"}, {"data_element": "speed", "enable_update": True}],
                }
            )

    @pytest.mark.parametrize("access", ["read_only", "read_write"])
    def test_a_queued_element_with_a_calibration_access_it_does_not_support(self, access: str) -> None:
        with pytest.raises(ValidationError, match="supports not_accessible only"):
            DataElement(name="e", implementation_type="t", sw_impl_policy="queued", sw_calibration_access=access)

    @pytest.mark.parametrize(
        ("policy", "access"),
        [("queued", "not_accessible"), ("queued", None), ("standard", "read_write"), (None, "read_only")],
    )
    def test_every_supported_combination_loads(self, policy: str | None, access: str | None) -> None:
        DataElement(name="e", implementation_type="t", sw_impl_policy=policy, sw_calibration_access=access)


# The model writers

_COM_SPECS = [{"data_element": "value", "enable_update": True, "alive_timeout": 0.25}, {"data_element": "other"}]
_DISABLED = [{"port": "rMode", "mode_group_ref": "/Ifs/IMode/mode", "mode_ref": "/Modes/M/OFF"}]


def _component(**port: Any) -> SWComponent:
    return SWComponent.model_validate(
        {
            "name": "Carrier",
            "handle_termination_and_restart": "no_support",
            "ports": [{"name": "rIn", "direction": "require", "interface": "IExample", **port}],
            "runnables": [{"name": "Run", "events": [{"type": "timing", "period": 0.01, "disabled_modes": _DISABLED}]}],
        }
    )


class TestTheWritersWriteEachFieldLastAndOmitItUnset:
    def test_the_generic_writer(self, tmp_path: Path) -> None:
        write_element(str(tmp_path), "swc/g/Carrier.yaml", _component(com_specs=_COM_SPECS), create=True)
        document = yaml.safe_load((tmp_path / "swc/g/Carrier.yaml").read_text(encoding="utf-8"))
        port = document["ports"][0]
        assert list(port)[-1] == "com_specs"
        assert port["com_specs"] == [
            {"data_element": "value", "enable_update": True, "alive_timeout": 0.25},
            {"data_element": "other"},
        ]
        event = document["runnables"][0]["events"][0]
        assert event == {"type": "timing", "period": 0.01, "disabled_modes": _DISABLED}

    def test_the_generic_writer_leaves_every_unset_field_out(self, tmp_path: Path) -> None:
        plain = SWComponent.model_validate(
            {
                "name": "Plain",
                "handle_termination_and_restart": "no_support",
                "ports": [{"name": "rIn", "direction": "require", "interface": "IExample"}],
                "runnables": [{"name": "Run", "events": [{"type": "timing", "period": 0.01}]}],
            }
        )
        write_element(str(tmp_path), "swc/g/Plain.yaml", plain, create=True)
        text = (tmp_path / "swc/g/Plain.yaml").read_text(encoding="utf-8")
        assert "com_specs" not in text and "disabled_modes" not in text
        interface = SRInterface(
            name="IPlain", kind="sender_receiver", data_elements=[{"name": "e", "implementation_type": "t"}]
        )
        write_element(str(tmp_path), "interfaces/IPlain.yaml", interface, create=True)
        text = (tmp_path / "interfaces/IPlain.yaml").read_text(encoding="utf-8")
        assert "is_service" not in text and "sw_impl_policy" not in text and "sw_calibration_access" not in text

    def test_the_hand_written_component_dump_equals_the_generic_one(self, tmp_path: Path) -> None:
        component = _component(com_specs=_COM_SPECS)
        write_element(str(tmp_path), "swc/g/Carrier.yaml", component, create=True)
        generic = (tmp_path / "swc/g/Carrier.yaml").read_text(encoding="utf-8")
        hand = yaml.safe_dump(swc_to_dict(component), default_flow_style=False, sort_keys=False)
        assert hand == generic

    def test_the_com_spec_and_event_dumps_write_only_what_is_stated(self) -> None:
        full = PortComSpec(
            data_element="e",
            enable_update=False,
            alive_timeout=0.0,
            handle_never_received=False,
            handle_timeout_type="none",
            handle_out_of_range="none",
            uses_end_to_end_protection=False,
            queue_length=3,
            init=0,
        )
        # A stated `false`, `0` or `none` is a statement and is written; only
        # `None` -- the model saying nothing -- is left out.
        assert _port_com_spec_to_dict(full) == full.model_dump()
        assert _port_com_spec_to_dict(PortComSpec(data_element="e")) == {"data_element": "e"}
        assert _event_to_dict(TimingEvent(type="timing", period=0.01)) == {"type": "timing", "period": 0.01}


def _seed(project_model: Path) -> None:
    """Put a component carrying both lists into a copy of the editing fixture's model."""
    target = project_model / "swc" / "example" / "Carrier.yaml"
    component = _component(com_specs=_COM_SPECS)
    target.write_text(
        yaml.safe_dump(swc_to_dict(component), default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    import editing_vector_fixtures as fixtures

    project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
    root = project / "model"
    _seed(root)
    return root


def _carrier(model_root: Path) -> SWComponent:
    return next(swc for swc in read_swc_model(str(model_root)) if swc.name == "Carrier")


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda root: add_port(str(root), "Carrier", "pExtra", "provide", "IExample"), id="add_port"),
        pytest.param(lambda root: add_runnable(str(root), "Carrier", "Extra"), id="add_runnable"),
        pytest.param(
            lambda root: (
                add_runnable(str(root), "Carrier", "Extra"),
                add_timing_event(str(root), "Carrier", "Extra", 0.02),
            ),
            id="add_timing_event",
        ),
        pytest.param(lambda root: add_init_event(str(root), "Carrier", "Run"), id="add_init_event"),
        pytest.param(lambda root: set_swc_memory_budget(str(root), "Carrier", "RAM0", 64), id="set_memory"),
        pytest.param(
            lambda root: (
                set_swc_memory_budget(str(root), "Carrier", "RAM0", 64),
                unset_swc_memory_budget(str(root), "Carrier", "RAM0"),
            ),
            id="unset_memory",
        ),
    ],
)
def test_every_mutation_rewriting_a_component_keeps_what_it_does_not_touch(model_root: Path, mutation: Any) -> None:
    mutation(model_root)
    carrier = _carrier(model_root)
    port = next(port for port in carrier.ports if port.name == "rIn")
    assert [entry.model_dump(exclude_none=True) for entry in port.com_specs] == _COM_SPECS
    run = next(runnable for runnable in carrier.runnables if runnable.name == "Run")
    timing = [event for event in run.events if isinstance(event, TimingEvent) and event.period == 0.01]
    assert [mode.model_dump() for mode in timing[0].disabled_modes] == _DISABLED
