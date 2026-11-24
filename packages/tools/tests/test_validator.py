"""Tests for the validator's consistency checks.

`check_cross_layer_consistency` runs the hardware topology pass and `hardware/`
is a mandatory layer, so a fixture exercising a requirement or connector rule
declares a processor first (`_processor()`), or it fails on
`hardware.layer-absent` instead. The topology rules have their own suite
(`packages/tools/tests/test_hardware_topology.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import validator_package
import yaml
from conftest import _processor, _root_composition, declare_no_support, model_element_path
from swcraft_tools import validator
from swcraft_tools.reader import read_requirements, read_swc_model
from swcraft_tools.schema import Connector
from swcraft_tools.validator import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    CompatibilityResult,
    ConsistencyIssue,
    ConsistencyResult,
    check_connector_compatibility,
    check_connector_consistency,
    check_cross_layer_consistency,
    check_data_type_cycles,
    check_data_type_references,
    check_hardware_topology,
    check_name_uniqueness,
    check_port_compatibility,
    check_port_interface_references,
    severity_of,
)
from swcraft_tools.validator.asil import asil_allocation_index
from swcraft_tools.validator.results import consistency_result, deduplicated
from swcraft_tools.writer import (
    add_port,
    add_runnable,
    add_trace_link,
    create_requirement,
    create_swc,
    create_task,
    map_runnable_to_task,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_swc_with_port(
    tmp_path: Path,
    swc_name: str,
    port_name: str,
    direction: str,
    interface: str,
    group: str = "test",
) -> None:
    """Create an SWC with one port."""
    create_swc(str(tmp_path), swc_name, group=group)
    add_port(str(tmp_path), swc_name, port_name, direction, interface)


def _write_sr_interface(tmp_path: Path, name: str, *elements: str, type_name: str = "uint8") -> None:
    """Write one sender-receiver interface file carrying *elements*, all of one type.

    Where a case is about a missing element name the element types are left
    undeclared: the subset by name is decided before an unresolvable type abstains.
    """
    directory = tmp_path / "interfaces"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "kind": "sender_receiver",
                "data_elements": [{"name": element, "implementation_type": type_name} for element in elements],
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )


# ---------------------------------------------------------------------------
# check_port_compatibility
# ---------------------------------------------------------------------------


class TestCheckPortCompatibility:
    def test_compatible_provide_require_same_interface(self, tmp_path: Path) -> None:
        """provide port + require port on same interface → compatible."""
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "pIn", "require", "ISpeed")

        result = check_port_compatibility(str(tmp_path), "ProviderSWC", "pOut", "RequirerSWC", "pIn")

        assert isinstance(result, CompatibilityResult)
        assert result.compatible is True


# ---------------------------------------------------------------------------
# check_connector_compatibility / check_connector_consistency
# ---------------------------------------------------------------------------


class TestCheckConnectorCompatibility:
    def test_compatible_connector(self, tmp_path: Path) -> None:
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "rIn", "require", "ISpeed")
        connector = Connector.model_validate(
            {
                "name": "Provider_to_Requirer",
                "from": {"swc": "ProviderSWC", "port": "pOut"},
                "to": {"swc": "RequirerSWC", "port": "rIn"},
            }
        )

        result = check_connector_compatibility(str(tmp_path), connector)

        assert result.compatible is True

    def test_two_different_but_compatible_interfaces_are_compatible(self, tmp_path: Path) -> None:
        """Two interfaces whose required element has a provided counterpart are compatible.

        No map is handed in, so the function reads the interface layers itself.
        """
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "IStatusFull")
        _make_swc_with_port(tmp_path, "RequirerSWC", "rIn", "require", "IStatusBasic")
        _write_sr_interface(tmp_path, "IStatusFull", "speed", "level")
        _write_sr_interface(tmp_path, "IStatusBasic", "speed")
        connector = Connector.model_validate(
            {
                "name": "Provider_to_Requirer",
                "from": {"swc": "ProviderSWC", "port": "pOut"},
                "to": {"swc": "RequirerSWC", "port": "rIn"},
            }
        )

        result = check_connector_compatibility(str(tmp_path), connector)

        assert result.compatible is True

    def test_a_required_element_without_a_counterpart_is_incompatible(self, tmp_path: Path) -> None:
        """The other half: the same two names, and a required element nothing provides."""
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "IStatusFull")
        _make_swc_with_port(tmp_path, "RequirerSWC", "rIn", "require", "IStatusBasic")
        _write_sr_interface(tmp_path, "IStatusFull", "speed")
        _write_sr_interface(tmp_path, "IStatusBasic", "speed", "level")
        connector = Connector.model_validate(
            {
                "name": "Bad",
                "from": {"swc": "ProviderSWC", "port": "pOut"},
                "to": {"swc": "RequirerSWC", "port": "rIn"},
            }
        )

        result = check_connector_compatibility(str(tmp_path), connector)

        assert result.compatible is False
        assert result.rule == "connector.interface-mismatch"
        assert result.reason == (
            "Connector 'Bad' interface mismatch: required 'IStatusBasic' (RequirerSWC.rIn) is not "
            "satisfied by provided 'IStatusFull' (ProviderSWC.pOut): "
            "data element 'level' has no counterpart in 'IStatusFull'."
        )

    def test_connector_wrong_direction_is_incompatible(self, tmp_path: Path) -> None:
        _make_swc_with_port(tmp_path, "ProviderSWC", "rOut", "require", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "rIn", "require", "ISpeed")
        connector = Connector.model_validate(
            {
                "name": "Bad",
                "from": {"swc": "ProviderSWC", "port": "rOut"},
                "to": {"swc": "RequirerSWC", "port": "rIn"},
            }
        )

        result = check_connector_compatibility(str(tmp_path), connector)

        assert result.compatible is False
        assert "provide port" in result.reason

    def test_connector_consistency_reports_duplicate_links(self, tmp_path: Path) -> None:
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "rIn", "require", "ISpeed")
        connector_dir = tmp_path / "connectors"
        connector_dir.mkdir()
        for name in ("ConnA", "ConnB"):
            (connector_dir / f"{name}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "name": name,
                        "from": {"swc": "ProviderSWC", "port": "pOut"},
                        "to": {"swc": "RequirerSWC", "port": "rIn"},
                    },
                    default_flow_style=False,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

        result = check_connector_consistency(str(tmp_path))

        assert result.consistent is False
        assert any("Duplicate connector link" in issue for issue in result.issues)

    def test_incompatible_both_provide(self, tmp_path: Path) -> None:
        """Two provide ports → incompatible."""
        _make_swc_with_port(tmp_path, "SWC_A", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "SWC_B", "pOut2", "provide", "ISpeed")

        result = check_port_compatibility(str(tmp_path), "SWC_A", "pOut", "SWC_B", "pOut2")

        assert result.compatible is False
        assert result.reason != ""

    def test_incompatible_both_require(self, tmp_path: Path) -> None:
        """Two require ports → incompatible."""
        _make_swc_with_port(tmp_path, "SWC_A", "pIn", "require", "ISpeed")
        _make_swc_with_port(tmp_path, "SWC_B", "pIn2", "require", "ISpeed")

        result = check_port_compatibility(str(tmp_path), "SWC_A", "pIn", "SWC_B", "pIn2")

        assert result.compatible is False
        assert result.reason != ""

    def test_incompatible_different_interfaces(self, tmp_path: Path) -> None:
        """Two declared interfaces, the required one unsatisfied → the whole mismatch sentence.

        The interfaces are declared because the check resolves them: two undeclared
        names are an abstention.
        """
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "pIn", "require", "IThrottle")
        _write_sr_interface(tmp_path, "ISpeed", "speed")
        _write_sr_interface(tmp_path, "IThrottle", "throttle")

        result = check_port_compatibility(str(tmp_path), "ProviderSWC", "pOut", "RequirerSWC", "pIn")

        assert result.compatible is False
        assert result.reason == (
            "Interface mismatch: required 'IThrottle' (RequirerSWC.pIn) is not satisfied by "
            "provided 'ISpeed' (ProviderSWC.pOut): data element 'throttle' has no counterpart in 'ISpeed'."
        )

    def test_two_different_but_compatible_port_interfaces_are_compatible(self, tmp_path: Path) -> None:
        """The pre-flight accepts two different interfaces every other gate accepts; it once compared their names."""
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "IStatusFull")
        _make_swc_with_port(tmp_path, "RequirerSWC", "rIn", "require", "IStatusBasic")
        _write_sr_interface(tmp_path, "IStatusFull", "speed", "level")
        _write_sr_interface(tmp_path, "IStatusBasic", "speed")

        result = check_port_compatibility(str(tmp_path), "ProviderSWC", "pOut", "RequirerSWC", "rIn")

        assert result.compatible is True, result.reason
        assert result.reason == ""

    def test_the_two_ports_may_be_given_in_either_order(self, tmp_path: Path) -> None:
        """The roles are read off the resolved directions, not off the parameter names.

        The message names each port by the pair the caller gave for it.
        """
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "pIn", "require", "IThrottle")
        _write_sr_interface(tmp_path, "ISpeed", "speed")
        _write_sr_interface(tmp_path, "IThrottle", "throttle")

        result = check_port_compatibility(str(tmp_path), "RequirerSWC", "pIn", "ProviderSWC", "pOut")

        assert result.compatible is False
        assert result.reason == (
            "Interface mismatch: required 'IThrottle' (RequirerSWC.pIn) is not satisfied by "
            "provided 'ISpeed' (ProviderSWC.pOut): data element 'throttle' has no counterpart in 'ISpeed'."
        )

    def test_an_undeclared_interface_is_another_rules_finding(self, tmp_path: Path) -> None:
        """Two undeclared interfaces: this check abstains and `port.undeclared-interface` reports them."""
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "pIn", "require", "IThrottle")

        result = check_port_compatibility(str(tmp_path), "ProviderSWC", "pOut", "RequirerSWC", "pIn")

        assert result.compatible is True, result.reason
        assert [record.rule for record in check_port_interface_references(str(tmp_path)).records] == [
            "port.undeclared-interface",
            "port.undeclared-interface",
        ]

    def test_nonexistent_provider_swc_raises(self, tmp_path: Path) -> None:
        """Non-existent provider SWC → ValueError."""
        _make_swc_with_port(tmp_path, "RequirerSWC", "pIn", "require", "ISpeed")

        with pytest.raises(ValueError, match="NonExistentSWC"):
            check_port_compatibility(str(tmp_path), "NonExistentSWC", "pOut", "RequirerSWC", "pIn")

    def test_nonexistent_requirer_swc_raises(self, tmp_path: Path) -> None:
        """Non-existent requirer SWC → ValueError."""
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")

        with pytest.raises(ValueError, match="NonExistentSWC"):
            check_port_compatibility(str(tmp_path), "ProviderSWC", "pOut", "NonExistentSWC", "pIn")

    def test_nonexistent_provider_port_raises(self, tmp_path: Path) -> None:
        """Non-existent provider port → ValueError."""
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "pIn", "require", "ISpeed")

        with pytest.raises(ValueError, match="NoSuchPort"):
            check_port_compatibility(str(tmp_path), "ProviderSWC", "NoSuchPort", "RequirerSWC", "pIn")

    def test_nonexistent_requirer_port_raises(self, tmp_path: Path) -> None:
        """Non-existent requirer port → ValueError."""
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "pIn", "require", "ISpeed")

        with pytest.raises(ValueError, match="NoSuchPort"):
            check_port_compatibility(str(tmp_path), "ProviderSWC", "pOut", "RequirerSWC", "NoSuchPort")

    def test_require_provide_order_also_compatible(self, tmp_path: Path) -> None:
        """require as first arg, provide as second → also compatible (one provide, one require)."""
        _make_swc_with_port(tmp_path, "ProviderSWC", "pOut", "provide", "ISpeed")
        _make_swc_with_port(tmp_path, "RequirerSWC", "pIn", "require", "ISpeed")

        result = check_port_compatibility(str(tmp_path), "RequirerSWC", "pIn", "ProviderSWC", "pOut")

        assert result.compatible is True


# ---------------------------------------------------------------------------
# check_cross_layer_consistency
# ---------------------------------------------------------------------------


def _satisfies_by_hand(model_root: Path, swc: str, member: str) -> None:
    """Append *member* to the component's own ``satisfies``, past ``add_trace_link``'s refusal."""
    from swcraft_tools.reader import find_swc_file

    path = Path(find_swc_file(str(model_root), swc))
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["satisfies"] = [*document.get("satisfies", []), member]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


class TestCheckCrossLayerConsistency:
    def test_consistent_swc_with_requirement_link(self, tmp_path: Path) -> None:
        """SWC exists and is linked by a requirement → consistent."""
        _processor(tmp_path)
        _root_composition(tmp_path)
        declare_no_support(create_swc(str(tmp_path), "SpeedCtrlSWC", group="body"))
        create_requirement(str(tmp_path), "REQ_001", "Speed signal", "The system shall provide speed.")
        add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC")

        result = check_cross_layer_consistency(str(tmp_path))

        assert isinstance(result, ConsistencyResult)
        assert result.consistent is True
        assert result.issues == []

    def test_swc_with_no_requirement_link(self, tmp_path: Path) -> None:
        """A component satisfying no requirement, and no requirement declared → no finding at all.

        No rule reports an unlinked component, so the errors and the warnings are both empty.
        """
        _processor(tmp_path)
        _root_composition(tmp_path)
        declare_no_support(create_swc(str(tmp_path), "OrphanSWC", group="body"))

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []
        assert result.records == ()
        assert result.warnings == ()

    def test_an_unlinked_swc_beside_an_unallocated_requirement_is_the_requirements_warning_alone(
        self, tmp_path: Path
    ) -> None:
        """A component without a link beside an unsatisfied requirement → one requirement warning."""
        _processor(tmp_path)
        _root_composition(tmp_path)
        declare_no_support(create_swc(str(tmp_path), "OrphanSWC", group="body"))
        create_requirement(str(tmp_path), "REQ_ALONE", "Alone", "Nothing satisfies this requirement.")

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is True
        assert result.records == ()
        assert [
            (record.rule, [(subject.layer, subject.element) for subject in record.subjects])
            for record in result.warnings
        ] == [("trace.requirement-unallocated", [("requirements", "REQ_ALONE")])]
        assert "OrphanSWC" not in result.warnings[0].message

    def test_a_satisfies_member_naming_no_requirement(self, tmp_path: Path) -> None:
        """A `satisfies` member naming no requirement → one issue; a component satisfying nothing is none.

        The member is written by hand because ``add_trace_link`` refuses an
        undeclared requirement.
        """
        _processor(tmp_path)
        _root_composition(tmp_path)
        declare_no_support(create_swc(str(tmp_path), "LinkedSWC", group="body"))
        declare_no_support(create_swc(str(tmp_path), "OrphanSWC", group="body"))
        _satisfies_by_hand(tmp_path, "LinkedSWC", "REQ_GHOST@1")

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is False
        assert [record.rule for record in result.records] == ["trace.link-unknown-requirement"]
        assert "REQ_GHOST" in result.issues[0] and "LinkedSWC" in result.issues[0]
        assert result.warnings == ()

    def test_multiple_swcs_all_linked(self, tmp_path: Path) -> None:
        """Multiple SWCs each linked by at least one requirement → consistent."""
        _processor(tmp_path)
        _root_composition(tmp_path)
        declare_no_support(create_swc(str(tmp_path), "SWC_A", group="body"))
        declare_no_support(create_swc(str(tmp_path), "SWC_B", group="body"))
        create_requirement(str(tmp_path), "REQ_A", "A", "Desc A")
        create_requirement(str(tmp_path), "REQ_B", "B", "Desc B")
        add_trace_link(str(tmp_path), "REQ_A", "SWC_A")
        add_trace_link(str(tmp_path), "REQ_B", "SWC_B")

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is True

    def test_a_model_with_only_its_mandatory_layers_is_consistent(self, tmp_path: Path) -> None:
        """No SWCs and no requirements, only the mandatory processor and root composition → consistent."""
        _processor(tmp_path)
        _root_composition(tmp_path)

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []

    def test_a_wholly_empty_model_is_refused_for_its_two_missing_mandatory_layers(self, tmp_path: Path) -> None:
        """An empty model is refused for its two missing mandatory layers, in the composer's check order."""
        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is False
        assert [record.rule for record in result.records] == ["composition.no-root", "hardware.layer-absent"]

    def test_only_links_no_requirements(self, tmp_path: Path) -> None:
        """A component links to a requirement but no requirement file exists → not consistent."""
        create_swc(str(tmp_path), "Linker", group="body")
        _satisfies_by_hand(tmp_path, "Linker", "REQ_001@1")

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is False
        assert any("REQ_001" in issue and "Linker" in issue for issue in result.issues)

    def test_broken_task_yaml_surfaces_in_aggregate_result(self, tmp_path: Path) -> None:
        """A task referencing an unknown SWC makes the aggregate result inconsistent, not only the task check."""
        create_swc(str(tmp_path), "RealSWC", group="body")
        create_requirement(str(tmp_path), "REQ_001", "Speed signal", "The system shall provide speed.")
        add_trace_link(str(tmp_path), "REQ_001", "RealSWC")
        # SWC/requirement layers are consistent on their own; only the task
        # layer is broken (references a SWC that doesn't exist).
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "Broken_Task.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "Broken_Task",
                    "period": 0.01,
                    "priority": 5,
                    # `partition` is required: the file has to parse for the
                    # *mapping* to be the thing that breaks.
                    "partition": "PARTITION0",
                    "runnables": [{"swc": "MissingSWC", "runnable": "SomeRun"}],
                },
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is False
        assert any("MissingSWC" in issue for issue in result.issues)

    def test_broken_sequence_yaml_surfaces_in_aggregate_result(self, tmp_path: Path) -> None:
        """A sequence contradicting its task's runnable order makes the aggregate result inconsistent."""
        create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
        add_runnable(str(tmp_path), "SpeedCtrlSWC", "SpeedCtrl_Run")
        add_runnable(str(tmp_path), "SpeedCtrlSWC", "TorqueCalc_Run")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        map_runnable_to_task(str(tmp_path), "Task_10ms", "SpeedCtrlSWC", "SpeedCtrl_Run")
        map_runnable_to_task(str(tmp_path), "Task_10ms", "SpeedCtrlSWC", "TorqueCalc_Run")
        create_requirement(str(tmp_path), "REQ_001", "Speed signal", "The system shall provide speed.")
        add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC")
        # Only the sequence layer is broken: a hand-written sequence bypasses
        # create_sequence's order guard, as a hand edit would.
        sequences_dir = tmp_path / "sequences"
        sequences_dir.mkdir(parents=True, exist_ok=True)
        (sequences_dir / "Broken_Sequence.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "Broken_Sequence",
                    "task": "Task_10ms",
                    "steps": [
                        {"swc": "SpeedCtrlSWC", "runnable": "TorqueCalc_Run"},
                        {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                    ],
                },
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is False
        assert any("order" in issue for issue in result.issues)

    def test_interface_with_undeclared_type_inconsistent(self, tmp_path: Path) -> None:
        """Interface referencing an undeclared data type → cross-layer check fails."""
        create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
        create_requirement(str(tmp_path), "REQ_001", "Speed signal", "The system shall provide speed.")
        add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC")
        iface_dir = tmp_path / "interfaces"
        iface_dir.mkdir(exist_ok=True)
        (iface_dir / "ICustom.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "ICustom",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "value", "implementation_type": "UndeclaredType"}],
                },
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is False
        assert any("UndeclaredType" in issue for issue in result.issues)


# ---------------------------------------------------------------------------
# Shared helpers for data-type tests
# ---------------------------------------------------------------------------


def _write_cs_interface(
    tmp_path: Path,
    name: str,
    operation_name: str,
    arguments: list[dict],
) -> None:
    """Write a minimal client-server interface YAML."""
    iface_dir = tmp_path / "interfaces"
    iface_dir.mkdir(exist_ok=True)
    (iface_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "kind": "client_server",
                "operations": [
                    {"name": operation_name, "arguments": arguments},
                ],
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_data_type(tmp_path: Path, data: dict) -> None:
    """Write a data type YAML file into its layer's directory (a base type under data_types/base/)."""
    path = model_element_path(tmp_path, data["name"], kind=data["kind"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _primitive(name: str) -> dict:
    return {"name": name, "kind": "base_type", "size": 32, "encoding": "IEEE754"}


def _write_value_type(tmp_path: Path, name: str) -> None:
    """Write the value type *name* over a ``{name}_base`` base type.

    A data prototype may not name a base type alone.
    """
    _write_data_type(tmp_path, _primitive(f"{name}_base"))
    _write_data_type(tmp_path, {"name": name, "kind": "value_type", "base": f"{name}_base"})


# ---------------------------------------------------------------------------
# check_data_type_references — interface element refs
# ---------------------------------------------------------------------------


class TestCheckDataTypeReferencesInterfaceRefs:
    def test_all_declared_consistent(self, tmp_path: Path) -> None:
        """Interface element type is declared → consistent."""
        _write_value_type(tmp_path, "float32")
        _write_sr_interface(tmp_path, "ISpeed", "speed", type_name="float32")

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []

    def test_missing_interface_type_inconsistent(self, tmp_path: Path) -> None:
        """Interface element type not declared → inconsistent with that type in issues."""
        _write_sr_interface(tmp_path, "ISpeed", "speed", type_name="float32")
        # no data_types dir → float32 undeclared

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("float32" in issue for issue in result.issues)

    def test_empty_interfaces_dir_consistent(self, tmp_path: Path) -> None:
        """No interfaces → no interface refs to check → consistent."""
        _write_data_type(tmp_path, _primitive("float32"))

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []

    def test_empty_model_consistent(self, tmp_path: Path) -> None:
        """No interfaces AND no data types → vacuously consistent."""
        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []


# ---------------------------------------------------------------------------
# check_data_type_references — CS interface argument type refs
# ---------------------------------------------------------------------------


class TestCheckDataTypeReferencesCsInterfaces:
    def test_all_cs_argument_types_declared_consistent(self, tmp_path: Path) -> None:
        """All CS argument types declared → check_data_type_references returns consistent=True."""
        _write_value_type(tmp_path, "uint8")
        _write_value_type(tmp_path, "uint16")
        _write_cs_interface(
            tmp_path,
            "IDiagnosticService",
            "ReadDtc",
            [
                {"name": "request", "direction": "in", "implementation_type": "uint8"},
                {"name": "response", "direction": "out", "implementation_type": "uint16"},
            ],
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []

    def test_cs_argument_undeclared_type_inconsistent(self, tmp_path: Path) -> None:
        """One CS argument of an undeclared type → inconsistent, the issue naming the type and the operation."""
        _write_data_type(tmp_path, _primitive("uint8"))
        _write_cs_interface(
            tmp_path,
            "IDiagnosticService",
            "ReadDtc",
            [
                {"name": "request", "direction": "in", "implementation_type": "uint8"},
                {"name": "response", "direction": "out", "implementation_type": "DtcReadResponse"},
            ],
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("DtcReadResponse" in issue for issue in result.issues)
        assert any("ReadDtc" in issue for issue in result.issues)

    def test_sr_interfaces_not_affected_by_cs_change(self, tmp_path: Path) -> None:
        """SR interfaces with declared types remain consistent after CS check extension."""
        _write_value_type(tmp_path, "float32")
        _write_sr_interface(tmp_path, "ISpeed", "speed", type_name="float32")
        # Also add a valid CS interface to confirm both coexist cleanly
        _write_value_type(tmp_path, "uint8")
        _write_cs_interface(
            tmp_path,
            "IService",
            "Ping",
            [{"name": "code", "direction": "in", "implementation_type": "uint8"}],
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []


# ---------------------------------------------------------------------------
# check_port_compatibility — CS port regression
# ---------------------------------------------------------------------------


class TestCheckPortCompatibilityCSRegression:
    def test_cs_provide_require_same_interface_compatible(self, tmp_path: Path) -> None:
        """provide + require ports on a CS interface name → compatible (interface-type-agnostic)."""
        _make_swc_with_port(tmp_path, "ServerSWC", "sPort", "provide", "IDiagnosticService")
        _make_swc_with_port(tmp_path, "ClientSWC", "cPort", "require", "IDiagnosticService")

        result = check_port_compatibility(str(tmp_path), "ServerSWC", "sPort", "ClientSWC", "cPort")

        assert result.compatible is True
        assert result.reason == ""

    def test_cs_both_require_ports_incompatible(self, tmp_path: Path) -> None:
        """Two require ports on a CS interface → incompatible (direction check fires)."""
        _make_swc_with_port(tmp_path, "ClientA", "cPort", "require", "IDiagnosticService")
        _make_swc_with_port(tmp_path, "ClientB", "cPort2", "require", "IDiagnosticService")

        result = check_port_compatibility(str(tmp_path), "ClientA", "cPort", "ClientB", "cPort2")

        assert result.compatible is False
        assert result.reason != ""


# ---------------------------------------------------------------------------
# check_data_type_references — intra-type dependency refs
# ---------------------------------------------------------------------------


class TestCheckDataTypeReferencesIntraRefs:
    def test_value_type_base_missing_inconsistent(self, tmp_path: Path) -> None:
        """Value type whose base is undeclared → inconsistent."""
        _write_data_type(tmp_path, {"name": "VehicleSpeed", "kind": "value_type", "base": "float32"})
        # float32 not declared

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("float32" in issue for issue in result.issues)

    def test_value_type_base_declared_consistent(self, tmp_path: Path) -> None:
        """Value type whose base is a declared base type → consistent."""
        _write_data_type(tmp_path, _primitive("float32"))
        _write_data_type(tmp_path, {"name": "VehicleSpeed", "kind": "value_type", "base": "float32"})

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True

    def test_value_type_over_value_type_inconsistent(self, tmp_path: Path) -> None:
        """A value type whose base is another value type → `data-type.value-type-undeclared-base`.

        A value type's `base` resolves in `base_types` alone; the retired
        `data-type.value-type-base-not-base-type` is reported nowhere.
        """
        _write_data_type(tmp_path, _primitive("float32"))
        _write_data_type(tmp_path, {"name": "VehicleSpeed", "kind": "value_type", "base": "float32"})
        # AltSpeed rests on VehicleSpeed, which is itself a value type — not a base type
        _write_data_type(tmp_path, {"name": "AltSpeed", "kind": "value_type", "base": "VehicleSpeed"})

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("AltSpeed" in issue and "base_type" in issue for issue in result.issues)
        assert [record.rule for record in result.records] == ["data-type.value-type-undeclared-base"]
        assert "data-type.value-type-base-not-base-type" not in {record.rule for record in result.records}

    def test_record_field_type_missing_inconsistent(self, tmp_path: Path) -> None:
        """Record field type undeclared → inconsistent."""
        _write_data_type(
            tmp_path,
            {
                "name": "VehicleState",
                "kind": "record",
                "fields": [{"name": "speed", "type": "VehicleSpeed"}],
            },
        )
        # VehicleSpeed not declared

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("VehicleSpeed" in issue for issue in result.issues)

    def test_array_element_type_missing_inconsistent(self, tmp_path: Path) -> None:
        """Array element_type undeclared → inconsistent."""
        _write_data_type(
            tmp_path,
            {"name": "WheelSpeedArray", "kind": "array", "element_type": "float32", "length": 4},
        )
        # float32 not declared

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("float32" in issue for issue in result.issues)

    def test_all_intra_refs_declared_consistent(self, tmp_path: Path) -> None:
        """All intra-type references to declared types → consistent."""
        _write_data_type(tmp_path, _primitive("float32"))
        _write_data_type(tmp_path, {"name": "VehicleSpeed", "kind": "value_type", "base": "float32"})
        # A composite's members are the physical view, so the record field and
        # the array element name an application type.
        _write_data_type(tmp_path, {"name": "SpeedPhys", "kind": "application_type", "unit": "kph"})
        _write_data_type(
            tmp_path,
            {
                "name": "VehicleState",
                "kind": "record",
                "fields": [{"name": "speed", "type": "SpeedPhys"}],
            },
        )
        _write_data_type(
            tmp_path,
            # The array's element is the application type: neither a base type
            # nor an implementation type may be one.
            {"name": "WheelSpeedArray", "kind": "array", "element_type": "SpeedPhys", "length": 4},
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []


# ---------------------------------------------------------------------------
# check_data_type_cycles
# ---------------------------------------------------------------------------


class TestCheckDataTypeCycles:
    def test_no_types_consistent(self, tmp_path: Path) -> None:
        """No data types → no graph → no cycles."""
        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []

    def test_linear_value_type_chain_no_cycle(self, tmp_path: Path) -> None:
        """VehicleSpeed (value type→float32), float32 (base type) → no cycle."""
        _write_data_type(tmp_path, _primitive("float32"))
        _write_data_type(tmp_path, {"name": "VehicleSpeed", "kind": "value_type", "base": "float32"})

        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []

    def test_direct_self_ref_record_is_cycle(self, tmp_path: Path) -> None:
        """Record whose field references itself → direct cycle detected."""
        _write_data_type(
            tmp_path,
            {
                "name": "SelfRef",
                "kind": "record",
                "fields": [{"name": "x", "type": "SelfRef"}],
            },
        )

        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is False
        assert any("SelfRef" in issue for issue in result.issues)

    def test_two_value_types_naming_each_other_are_two_undeclared_bases(self, tmp_path: Path) -> None:
        """Two value types naming each other as base: no cycle, two undeclared bases.

        A value type's ``base`` resolves in ``base_types`` alone, so the cycle graph
        has no value type → base edge.
        """
        _write_data_type(tmp_path, {"name": "TypeA", "kind": "value_type", "base": "TypeB"})
        _write_data_type(tmp_path, {"name": "TypeB", "kind": "value_type", "base": "TypeA"})

        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []
        references = check_data_type_references(str(tmp_path))
        assert references.consistent is False
        assert sorted((record.rule, record.subjects[0].element) for record in references.records) == [
            ("data-type.value-type-undeclared-base", "TypeA"),
            ("data-type.value-type-undeclared-base", "TypeB"),
        ]

    def test_transitive_alias_ring_is_no_cycle(self, tmp_path: Path) -> None:
        """A→B→C→A over value-type bases: no cycle (no value type → base edge), three undeclared bases."""
        _write_data_type(tmp_path, {"name": "TypeA", "kind": "value_type", "base": "TypeB"})
        _write_data_type(tmp_path, {"name": "TypeB", "kind": "value_type", "base": "TypeC"})
        _write_data_type(tmp_path, {"name": "TypeC", "kind": "value_type", "base": "TypeA"})

        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []
        rules = [record.rule for record in check_data_type_references(str(tmp_path)).records]
        assert rules == ["data-type.value-type-undeclared-base"] * 3

    def test_transitive_record_cycle(self, tmp_path: Path) -> None:
        """A→B→C→A over record fields: the transitive cycle the graph still holds."""
        for name, target in (("TypeA", "TypeB"), ("TypeB", "TypeC"), ("TypeC", "TypeA")):
            _write_data_type(tmp_path, {"name": name, "kind": "record", "fields": [{"name": "f", "type": target}]})

        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is False
        # One finding for the one cycle, naming every member on the application
        # side: a record is an application type since contract `8.0.0`.
        (record,) = result.records
        assert record.rule == "data-type.cycle"
        assert sorted((subject.layer, subject.element) for subject in record.subjects) == [
            ("application_types", "TypeA"),
            ("application_types", "TypeB"),
            ("application_types", "TypeC"),
        ]

    def test_valid_dag_with_shared_dependency(self, tmp_path: Path) -> None:
        """TypeA and TypeB are value types over one base type, float32 → valid DAG, no cycles."""
        _write_data_type(tmp_path, _primitive("float32"))
        _write_data_type(tmp_path, {"name": "TypeA", "kind": "value_type", "base": "float32"})
        _write_data_type(tmp_path, {"name": "TypeB", "kind": "value_type", "base": "float32"})

        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is True
        assert result.issues == []

    def test_base_types_and_enums_have_no_edges(self, tmp_path: Path) -> None:
        """Base types and enums produce no graph edges → no cycles."""
        _write_data_type(tmp_path, _primitive("float32"))
        _write_data_type(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
            },
        )

        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is True

    def test_array_self_reference_cycle(self, tmp_path: Path) -> None:
        """Array whose element_type equals its own name → direct cycle detected."""
        _write_data_type(
            tmp_path,
            {"name": "SpeedArr", "kind": "array", "element_type": "SpeedArr", "length": 4},
        )

        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is False
        assert any("SpeedArr" in issue for issue in result.issues)


# ---------------------------------------------------------------------------
# Duplicate data type name detection
# ---------------------------------------------------------------------------


class TestDuplicateDataTypeNames:
    """A name declared in two files is flagged by check_data_type_references and check_data_type_cycles."""

    def _write_file(self, tmp_path: Path, filename: str, data: dict) -> None:
        """Write a data type file under an explicit filename, in its kind's layer directory."""
        dt_dir = model_element_path(tmp_path, data["name"], kind=data["kind"]).parent
        dt_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml

        (dt_dir / filename).write_text(
            _yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8"
        )

    def test_duplicate_name_in_check_references_is_inconsistent(self, tmp_path: Path) -> None:
        """Two YAML files declaring the same name → check_data_type_references reports it."""
        self._write_file(
            tmp_path, "float32.yaml", {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"}
        )
        self._write_file(
            tmp_path, "float32_dup.yaml", {"name": "float32", "kind": "base_type", "size": 64, "encoding": "NONE"}
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("float32" in issue and "Duplicate" in issue for issue in result.issues)

    def test_duplicate_name_in_check_cycles_is_inconsistent(self, tmp_path: Path) -> None:
        """Two YAML files declaring the same name → check_data_type_cycles reports it."""
        self._write_file(
            tmp_path, "TypeA.yaml", {"name": "TypeA", "kind": "base_type", "size": 32, "encoding": "IEEE754"}
        )
        self._write_file(
            tmp_path, "TypeA_dup.yaml", {"name": "TypeA", "kind": "base_type", "size": 64, "encoding": "NONE"}
        )

        result = check_data_type_cycles(str(tmp_path))

        assert result.consistent is False
        assert any("TypeA" in issue and "Duplicate" in issue for issue in result.issues)

    def test_no_duplicates_consistent(self, tmp_path: Path) -> None:
        """Unique names across files → no duplicate issue reported."""
        _write_data_type(tmp_path, _primitive("float32"))
        _write_data_type(tmp_path, _primitive("uint8"))

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert not any("Duplicate" in issue for issue in result.issues)

    def test_cross_layer_duplicate_reported_exactly_once(self, tmp_path: Path) -> None:
        """check_cross_layer_consistency reports a duplicate type name once, though both data-type checks detect it."""
        # Set up a valid SWC + requirement so the only issues are the duplicate type.
        from swcraft_tools.writer import add_trace_link, create_requirement, create_swc

        create_swc(str(tmp_path), "SomeSWC", group="body")
        create_requirement(str(tmp_path), "REQ_001", "Some req", "Desc")
        add_trace_link(str(tmp_path), "REQ_001", "SomeSWC")

        self._write_file(
            tmp_path,
            "float32.yaml",
            {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"},
        )
        self._write_file(
            tmp_path,
            "float32_dup.yaml",
            {"name": "float32", "kind": "base_type", "size": 64, "encoding": "NONE"},
        )

        result = check_cross_layer_consistency(str(tmp_path))

        assert result.consistent is False
        duplicate_messages = [issue for issue in result.issues if "float32" in issue and "Duplicate" in issue]
        assert len(duplicate_messages) == 1, (
            f"Expected exactly 1 duplicate-name message, got {len(duplicate_messages)}: {duplicate_messages}"
        )


# ---------------------------------------------------------------------------
# check_data_type_references — _impl suffix collision
# ---------------------------------------------------------------------------


class TestTheImplSuffixCollidesWithNothing:
    """A type named '{x}_impl' beside a declared '{x}' collides with nothing: no impl twin is generated."""

    def test_a_name_beside_a_base_type_is_no_collision(self, tmp_path: Path) -> None:
        """'float32_impl' beside base type 'float32' is no collision → consistent.

        This pair is the one widening the removal of ``data-type.impl-name-collision``
        buys; ``test_validator_data_types.py`` asserts it is the only one.
        """
        _write_data_type(tmp_path, _primitive("float32"))
        _write_data_type(
            tmp_path,
            {"name": "float32_impl", "kind": "base_type", "size": 32, "encoding": "IEEE754"},
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True, result.issues
        assert not any("conflicts with auto-generated" in issue for issue in result.issues)
        assert "data-type.impl-name-collision" not in {record.rule for record in result.records}

    def test_a_name_with_no_matching_type_is_no_collision(self, tmp_path: Path) -> None:
        """A type ending in '_impl' is fine when no matching type exists."""
        _write_data_type(
            tmp_path,
            {"name": "custom_impl", "kind": "base_type", "size": 16, "encoding": "NONE"},
        )
        # 'custom' is not declared, so no collision.

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert not any("conflicts with auto-generated" in issue for issue in result.issues)

    def test_a_name_beside_a_value_type_is_no_collision(self, tmp_path: Path) -> None:
        """'VehicleSpeed_impl' beside the value type 'VehicleSpeed' is no collision."""
        _write_data_type(tmp_path, _primitive("float32"))
        _write_data_type(tmp_path, {"name": "VehicleSpeed", "kind": "value_type", "base": "float32"})
        _write_data_type(
            tmp_path,
            {"name": "VehicleSpeed_impl", "kind": "base_type", "size": 32, "encoding": "IEEE754"},
        )

        result = check_data_type_references(str(tmp_path))

        assert not any("VehicleSpeed_impl" in issue and "conflicts" in issue for issue in result.issues), (
            f"Unexpected impl-collision beside a value type: {result.issues}"
        )


# ---------------------------------------------------------------------------
# check_data_type_references — _CompuMethod suffix collision
# ---------------------------------------------------------------------------


class TestCompuMethodSuffixCollision:
    """A type named '{x}_CompuMethod' beside enum '{x}' collides with the enum's generated COMPU-METHOD.

    The method lies in the enum's package, so only a declared type of that
    package collides with it.
    """

    def test_compu_method_suffix_collision_detected(self, tmp_path: Path) -> None:
        """'GearPosition_CompuMethod' declared alongside enum 'GearPosition' → inconsistent."""
        _write_data_type(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
            },
        )
        _write_data_type(tmp_path, {"name": "GearPosition_CompuMethod", "kind": "application_type"})

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("GearPosition_CompuMethod" in issue and "GearPosition" in issue for issue in result.issues), (
            f"Expected CompuMethod-collision message in issues, got: {result.issues}"
        )

    def test_a_base_type_under_the_methods_name_is_ok(self, tmp_path: Path) -> None:
        """A base type lies in /DataTypes/Base and the method in /DataTypes/Application."""
        _write_data_type(
            tmp_path,
            {"name": "GearPosition", "kind": "enum", "literals": [{"name": "PARK", "value": 0}]},
        )
        _write_data_type(
            tmp_path,
            {"name": "GearPosition_CompuMethod", "kind": "base_type", "size": 8, "encoding": "NONE"},
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True, result.issues

    def test_compu_method_suffix_no_matching_enum_is_ok(self, tmp_path: Path) -> None:
        """A type ending in '_CompuMethod' is fine when no matching enum exists."""
        _write_data_type(
            tmp_path,
            {
                "name": "custom_CompuMethod",
                "kind": "base_type",
                "size": 16,
                "encoding": "NONE",
            },
        )
        # 'custom' is not declared, so no collision.

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert not any("conflicts with auto-generated" in issue for issue in result.issues)


# ---------------------------------------------------------------------------
# check_data_type_references — duplicate record field names / enum literals
# ---------------------------------------------------------------------------


class TestDuplicateRecordFieldNames:
    """Two same-named record fields (sibling APPLICATION-RECORD-ELEMENTs of one SHORT-NAME) are caught statically."""

    def test_duplicate_field_name_detected(self, tmp_path: Path) -> None:
        _write_data_type(tmp_path, _primitive("uint8"))
        _write_data_type(
            tmp_path,
            {
                "name": "Point",
                "kind": "record",
                "fields": [
                    {"name": "x", "type": "uint8"},
                    {"name": "x", "type": "uint8"},
                ],
            },
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("Point" in issue and "x" in issue and "duplicate" in issue.lower() for issue in result.issues), (
            f"Expected duplicate-field-name message in issues, got: {result.issues}"
        )

    def test_distinct_field_names_is_ok(self, tmp_path: Path) -> None:
        _write_value_type(tmp_path, "uint8")
        # A record field names the physical view: an application type.
        _write_data_type(tmp_path, {"name": "Coord", "kind": "application_type"})
        _write_data_type(
            tmp_path,
            {
                "name": "Point",
                "kind": "record",
                "fields": [
                    {"name": "x", "type": "Coord"},
                    {"name": "y", "type": "Coord"},
                ],
            },
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert not any("duplicate" in issue.lower() for issue in result.issues)


class TestDuplicateEnumLiterals:
    """Two same-named or same-valued enum literals (duplicate SHORT-NAME / COMPU-SCALE value) are caught statically."""

    def test_duplicate_literal_name_detected(self, tmp_path: Path) -> None:
        _write_data_type(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [
                    {"name": "PARK", "value": 0},
                    {"name": "PARK", "value": 1},
                ],
            },
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any(
            "GearPosition" in issue and "PARK" in issue and "duplicate" in issue.lower() for issue in result.issues
        ), f"Expected duplicate-literal-name message in issues, got: {result.issues}"

    def test_duplicate_literal_value_detected(self, tmp_path: Path) -> None:
        _write_data_type(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [
                    {"name": "PARK", "value": 0},
                    {"name": "DRIVE", "value": 0},
                ],
            },
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is False
        assert any("GearPosition" in issue and "duplicate" in issue.lower() for issue in result.issues), (
            f"Expected duplicate-literal-value message in issues, got: {result.issues}"
        )

    def test_distinct_literal_names_and_values_is_ok(self, tmp_path: Path) -> None:
        _write_data_type(
            tmp_path,
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [
                    {"name": "PARK", "value": 0},
                    {"name": "DRIVE", "value": 1},
                ],
            },
        )

        result = check_data_type_references(str(tmp_path))

        assert result.consistent is True
        assert not any("duplicate" in issue.lower() for issue in result.issues)


# ---------------------------------------------------------------------------
# The class of a finding: the partition `consistency_result` applies
# ---------------------------------------------------------------------------

#: A shipped error rule, made a warning by `monkeypatch` for one test at a time,
#: so that these tests hold whatever `WARNING_RULES` ships — the shipped set is
#: the conformance gate's to hold against the contract, not this module's.
_SYNTHETIC_WARNING_RULE = "connector.duplicate-name"


def _finding(rule: str, message: str) -> ConsistencyIssue:
    return ConsistencyIssue(rule=rule, message=message)


class TestTheClassOfAFinding:
    """`severity_of`, the property, and `consistency_result`'s partition — the one place the class is applied.

    What the shipped set may name is not pinned here: its equality with the
    contract's `severity: warning` declarations, and so its membership of
    `CONSISTENCY_RULES`, is `test_model_contract_conformance.py`'s gate.
    """

    def test_a_rule_is_an_error_unless_warning_rules_names_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert severity_of(_SYNTHETIC_WARNING_RULE) == SEVERITY_ERROR
        assert _finding(_SYNTHETIC_WARNING_RULE, "m").severity == SEVERITY_ERROR

        validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", frozenset({_SYNTHETIC_WARNING_RULE}))

        assert severity_of(_SYNTHETIC_WARNING_RULE) == SEVERITY_WARNING
        assert _finding(_SYNTHETIC_WARNING_RULE, "m").severity == SEVERITY_WARNING
        assert severity_of("connector.duplicate-link") == SEVERITY_ERROR

    def test_result_partitions_by_the_rules_class_in_emission_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", frozenset({_SYNTHETIC_WARNING_RULE}))
        error_1 = _finding("connector.duplicate-link", "error one")
        warning_1 = _finding(_SYNTHETIC_WARNING_RULE, "warning one")
        error_2 = _finding("connector.duplicate-link", "error two")
        warning_2 = _finding(_SYNTHETIC_WARNING_RULE, "warning two")

        result = consistency_result([error_1, warning_1, error_2, warning_2])

        assert result.consistent is False
        assert result.issues == ["error one", "error two"]
        assert result.records == (error_1, error_2)
        assert result.warnings == (warning_1, warning_2)

    def test_warnings_alone_leave_the_result_consistent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`consistent` reads the error half: a warning decides nothing."""
        validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", frozenset({_SYNTHETIC_WARNING_RULE}))
        warning = _finding(_SYNTHETIC_WARNING_RULE, "warning one")

        result = consistency_result([warning])

        assert result.consistent is True
        assert result.issues == []
        assert result.records == ()
        assert result.warnings == (warning,)

    def test_with_the_shipped_set_every_finding_of_an_error_rule_is_an_error(self) -> None:
        """What every existing test of this module relies on, stated once."""
        error = _finding("connector.duplicate-link", "error one")

        result = consistency_result([error])

        assert result == ConsistencyResult(consistent=False, issues=["error one"], records=(error,), warnings=())

    def test_the_composer_carries_a_composed_checks_warnings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With `hardware.layer-absent` made a warning, the composer carries it beside the one error.

        An empty model root reports `composition.no-root` and `hardware.layer-absent`
        and nothing else.
        """
        validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", frozenset({"hardware.layer-absent"}))

        composed = check_hardware_topology(str(tmp_path))
        result = check_cross_layer_consistency(str(tmp_path))

        assert composed.consistent is True
        assert [record.rule for record in composed.warnings] == ["hardware.layer-absent"]
        assert result.consistent is False
        assert [record.rule for record in result.records] == ["composition.no-root"]
        assert result.issues == [result.records[0].message]
        assert result.warnings == composed.warnings

    def test_deduplication_runs_before_the_partition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One message is one finding whatever its class: the first occurrence keeps its rule's class."""
        validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", frozenset({_SYNTHETIC_WARNING_RULE}))
        as_warning = _finding(_SYNTHETIC_WARNING_RULE, "the same sentence")
        as_error = _finding("connector.duplicate-link", "the same sentence")

        warning_first = consistency_result(deduplicated([as_warning, as_error]))
        error_first = consistency_result(deduplicated([as_error, as_warning]))

        assert warning_first.consistent is True
        assert warning_first.records == ()
        assert warning_first.warnings == (as_warning,)
        assert error_first.consistent is False
        assert error_first.records == (as_error,)
        assert error_first.warnings == ()

    def test_errors_and_warnings_together_refuse_for_the_errors_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        validator_package.patch_everywhere(monkeypatch, "WARNING_RULES", frozenset({_SYNTHETIC_WARNING_RULE}))
        error = _finding("connector.duplicate-link", "error one")
        warning = _finding(_SYNTHETIC_WARNING_RULE, "warning one")

        result = consistency_result([warning, error])

        assert result.consistent is False
        assert result.issues == ["error one"]
        assert result.records == (error,)
        assert result.warnings == (warning,)

    def test_a_hand_built_result_carries_no_warning(self) -> None:
        """The field has a default, so every test that builds a result by hand keeps working."""
        result = ConsistencyResult(consistent=False, issues=["an error"])

        assert result.warnings == ()
        assert result.records == ()


# ---------------------------------------------------------------------------
# The trace link lives in the element
# ---------------------------------------------------------------------------


def _yaml(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _req(model_root: Path, req_id: str, version: int = 1, **extra: object) -> None:
    _yaml(
        model_root / "requirements" / f"{req_id}.yaml",
        {
            "id": req_id,
            "title": req_id,
            "description": f"The system shall satisfy {req_id}.",
            "version": version,
            "source": "architecture",
            **extra,
        },
    )


def _linked_component(model_root: Path, name: str = "Ctl", **links: object) -> None:
    """One component whose own, port's and runnable's ``satisfies`` are *links*' three keys."""
    port = {"name": "pIn", "direction": "require", "interface": "ISig"}
    runnable = {"name": "Run", "events": [{"type": "timing", "period": 0.01}]}
    if links.get("port"):
        port["satisfies"] = links["port"]
    if links.get("runnable"):
        runnable["satisfies"] = links["runnable"]
    _yaml(
        model_root / "swc" / "group" / f"{name}.yaml",
        {
            "name": name,
            "type": "application",
            "handle_termination_and_restart": "no_support",
            "ports": [port],
            "runnables": [runnable],
            **({"satisfies": links["own"]} if links.get("own") else {}),
        },
    )


@pytest.fixture
def trace_root(tmp_path: Path) -> Path:
    """A consistent model but for what each case writes: a processor, a root, an interface."""
    root = tmp_path / "model"
    _processor(root)
    _root_composition(root)
    _yaml(root / "data_types" / "base" / "u8.yaml", {"name": "u8", "kind": "base_type", "size": 8, "encoding": "NONE"})
    _yaml(
        root / "data_types" / "implementation" / "uint8.yaml",
        {"name": "uint8", "kind": "value_type", "base": "u8"},
    )
    _yaml(
        root / "interfaces" / "ISig.yaml",
        {"name": "ISig", "kind": "sender_receiver", "data_elements": [{"name": "v", "implementation_type": "uint8"}]},
    )
    return root


def _rules(model_root: Path) -> list[str]:
    return [record.rule for record in check_cross_layer_consistency(str(model_root)).records]


class TestTheIndexReadsTheElementsLinks:
    """`asil_allocation_index` over `trace.component_requirement_ids`."""

    def test_a_ports_and_a_runnables_link_count_for_the_component_once_per_requirement(self, trace_root: Path) -> None:
        _req(trace_root, "REQ_A", asil="B")
        _req(trace_root, "REQ_P", asil="C")
        _linked_component(trace_root, own=["REQ_A@1"], port=["REQ_A@1", "REQ_P@1"], runnable=["REQ_A@1"])

        swcs = read_swc_model(str(trace_root))
        requirements = read_requirements(str(trace_root))
        allocated = asil_allocation_index(swcs, requirements)

        assert [requirement.id for requirement in allocated["Ctl"]] == ["REQ_A", "REQ_P"]
        assert validator.effective_asil_levels(swcs, requirements) == {"Ctl": "C"}

    def test_a_stale_version_still_allocates(self, trace_root: Path) -> None:
        """The level is allocated by id: a link at an older `@N` never loses the allocation."""
        _req(trace_root, "REQ_A", version=3, asil="D")
        _linked_component(trace_root, own=["REQ_A@1"])

        swcs = read_swc_model(str(trace_root))
        assert validator.effective_asil_levels(swcs, read_requirements(str(trace_root))) == {"Ctl": "D"}
        assert "trace.link-unknown-requirement" not in _rules(trace_root)

    def test_a_dangling_id_is_reported_once(self, trace_root: Path) -> None:
        """A ghost is one `trace.link-unknown-requirement` error, and no warning stands beside it."""
        _linked_component(trace_root, own=["REQ_GHOST@1"])

        result = check_cross_layer_consistency(str(trace_root))

        assert [record.rule for record in result.records if record.rule.startswith("trace.")] == [
            "trace.link-unknown-requirement"
        ]
        assert result.warnings == ()

    def test_a_component_whose_only_member_holds_a_colon_is_reported_once(self, trace_root: Path) -> None:
        """A ``:`` member is no requirement reference: one `trace.link-unknown-requirement`, and nothing else."""
        _linked_component(trace_root, own=["hardware:CPU"])

        result = check_cross_layer_consistency(str(trace_root))

        assert [record.rule for record in result.records if record.rule.startswith("trace.")] == [
            "trace.link-unknown-requirement"
        ]
        assert result.warnings == ()


class TestALinkNamingNoRequirement:
    """`trace.link-unknown-requirement`: every site, the subject at the member, the grammar named."""

    @pytest.mark.parametrize(
        ("links", "steps"),
        [
            ({"own": ["REQ_GHOST@1"]}, ("satisfies", "REQ_GHOST@1")),
            ({"port": ["REQ_GHOST@1"]}, ("ports", "pIn", "satisfies", "REQ_GHOST@1")),
            ({"runnable": ["REQ_GHOST@1"]}, ("runnables", "Run", "satisfies", "REQ_GHOST@1")),
        ],
        ids=["component", "port", "runnable"],
    )
    def test_a_ghost_on_a_component_site_is_reported_at_the_member(
        self, trace_root: Path, links: dict, steps: tuple[str, ...]
    ) -> None:
        _linked_component(trace_root, **links)

        (record,) = [
            record
            for record in check_cross_layer_consistency(str(trace_root)).records
            if record.rule == "trace.link-unknown-requirement"
        ]

        (subject,) = record.subjects
        assert (subject.layer, subject.element) == ("swc", "Ctl")
        assert tuple(step.value for step in subject.path) == steps
        assert "no requirement declares 'REQ_GHOST'" in record.message

    def test_a_ghost_on_a_task_is_reported_at_the_tasks_member(self, trace_root: Path) -> None:
        _yaml(
            trace_root / "tasks" / "T.yaml",
            {"name": "T", "period": 0.01, "priority": 1, "partition": "P0", "satisfies": ["REQ_GHOST@1"]},
        )

        records = [
            record
            for record in check_cross_layer_consistency(str(trace_root)).records
            if record.rule == "trace.link-unknown-requirement"
        ]

        assert [(record.subjects[0].layer, record.subjects[0].element) for record in records] == [("tasks", "T")]
        assert "Task 'T' satisfies 'REQ_GHOST@1'" in records[0].message

    @pytest.mark.parametrize(
        ("member", "problem"),
        [
            ("REQ_A@x", "'x' after '@' is not a version"),
            ("REQ_A@0", "'0' after '@' is not a version"),
            ("REQ_A@01", "'01' after '@' is not a version"),
            ("hardware:CPU", "a member holding ':' names no requirement"),
        ],
        ids=["letters", "zero", "leading-zero", "element-form"],
    )
    def test_a_malformed_member_names_the_grammar(self, trace_root: Path, member: str, problem: str) -> None:
        _req(trace_root, "REQ_A")
        _linked_component(trace_root, own=[member])

        (record,) = [
            record
            for record in check_cross_layer_consistency(str(trace_root)).records
            if record.rule == "trace.link-unknown-requirement"
        ]

        assert problem in record.message
        assert "A member is '<id>@<version>', the version a positive integer, or the bare '<id>'." in record.message

    @pytest.mark.parametrize(
        ("member", "grammar"),
        [("REQ_GHOST@1", False), ("REQ_GHOST", False), ("REQ_GHOST@x", True)],
        ids=["versioned", "bare", "malformed-suffix"],
    )
    def test_a_ghost_names_the_grammar_only_when_it_breaks_it(
        self, trace_root: Path, member: str, grammar: bool
    ) -> None:
        """A well-formed member naming an absent requirement is told what is absent, and no grammar."""
        _linked_component(trace_root, own=[member])

        (record,) = [
            record
            for record in check_cross_layer_consistency(str(trace_root)).records
            if record.rule == "trace.link-unknown-requirement"
        ]

        assert record.message.endswith("no requirement declares 'REQ_GHOST'.") is not grammar
        assert ("A member is '<id>@<version>'" in record.message) is grammar

    def test_a_bare_id_and_a_well_formed_version_are_not_reported(self, trace_root: Path) -> None:
        _req(trace_root, "REQ_A")
        _linked_component(trace_root, own=["REQ_A"], port=["REQ_A@2"])

        assert "trace.link-unknown-requirement" not in _rules(trace_root)


class TestTwoFilesDeclaringOneRequirementId:
    """`name.duplicate-requirement`: one finding per id, at the requirement."""

    def test_two_files_of_one_id_are_reported_once(self, trace_root: Path) -> None:
        _req(trace_root, "REQ_A")
        _yaml(
            trace_root / "requirements" / "REQ_A~1.yaml",
            {"id": "REQ_A", "title": "t", "description": "d", "version": 1, "source": "external"},
        )
        _linked_component(trace_root, own=["REQ_A@1"])

        records = [
            record
            for record in check_name_uniqueness(str(trace_root)).records
            if record.rule == "name.duplicate-requirement"
        ]

        assert len(records) == 1
        assert "'REQ_A'" in records[0].message
        assert [(subject.layer, subject.element) for subject in records[0].subjects] == [("requirements", "REQ_A")]

    def test_one_file_is_not_a_finding(self, trace_root: Path) -> None:
        _req(trace_root, "REQ_A")
        _linked_component(trace_root, own=["REQ_A@1"])

        assert "name.duplicate-requirement" not in [
            record.rule for record in check_name_uniqueness(str(trace_root)).records
        ]
