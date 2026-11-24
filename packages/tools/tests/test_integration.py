"""Integration test: the writer -> reader -> validator -> generator chain, and the audit log of every write."""

from __future__ import annotations

import pathlib

from conftest import _root_composition, declare_no_support
from node_support import requires_node
from swcraft_tools.auditor import AuditLog
from swcraft_tools.reader import find_data_type_file, read_data_types
from swcraft_tools.validator import (
    check_cross_layer_consistency,
    check_data_type_cycles,
    check_data_type_references,
)
from swcraft_tools.writer import (
    add_port,
    add_runnable,
    add_timing_event,
    add_trace_link,
    create_connector,
    create_data_type,
    create_hardware_element,
    create_interface,
    create_requirement,
    create_swc,
    generate_arxml,
    generate_diagrams,
    generate_needs,
)

# The committed model; these smoke tests read it and never write to it.
_REAL_MODEL_ROOT = str(pathlib.Path(__file__).parents[3] / "examples" / "basic" / "model")


def test_full_model_lifecycle(tmp_path):
    """Build a complete model, generate all artifacts, verify consistency and audit log."""
    audit_log = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    model_root = str(tmp_path)

    # Write operations (12 audited)

    # The processor keeps `check_cross_layer_consistency` about this model, since `hardware/` is
    # mandatory. Written without an auditor, so the audit count covers the lifecycle's own calls.
    create_hardware_element(model_root, "CPU", "processor")
    # `create_swc` takes no `no_support`, which a component must declare; the line is added
    # to the file outside the audit.
    declare_no_support(create_swc(model_root, "SpeedSensorSWC", group="powertrain", auditor=audit_log))
    declare_no_support(create_swc(model_root, "SpeedCtrlSWC", group="powertrain", auditor=audit_log))
    # No data prototype may be typed by a base type, so `float32` is a value type over
    # `float32_base`, written without an auditor like the processor.
    create_data_type(
        model_root,
        "float32_base",
        "base_type",
        size=32,
        encoding="IEEE754",
        native_declaration="float",
    )
    create_data_type(model_root, "float32", "value_type", base="float32_base", auditor=audit_log)
    create_interface(
        model_root,
        "ISpeed",
        [{"name": "speed", "implementation_type": "float32"}],
        auditor=audit_log,
    )
    add_port(model_root, "SpeedSensorSWC", "pSpeed", "provide", "ISpeed", auditor=audit_log)
    add_port(model_root, "SpeedCtrlSWC", "rSpeed", "require", "ISpeed", auditor=audit_log)
    create_connector(
        model_root,
        "SpeedSensor_to_SpeedCtrl",
        "SpeedSensorSWC",
        "pSpeed",
        "SpeedCtrlSWC",
        "rSpeed",
        auditor=audit_log,
    )
    # The root the connector is drawn inside, written as a file: `create_composition` creates an
    # empty one, and adding a member is an edit of an existing element.
    _root_composition(model_root, ["SpeedSensorSWC", "SpeedCtrlSWC"])
    add_runnable(model_root, "SpeedCtrlSWC", "SpeedCtrl_Run", auditor=audit_log)
    add_timing_event(model_root, "SpeedCtrlSWC", "SpeedCtrl_Run", period=0.01, auditor=audit_log)
    create_requirement(
        model_root,
        "REQ_SPEED_001",
        "Speed signal",
        "The system shall provide the current vehicle speed.",
        auditor=audit_log,
    )
    add_trace_link(model_root, "REQ_SPEED_001", "SpeedSensorSWC", auditor=audit_log)
    add_trace_link(model_root, "REQ_SPEED_001", "SpeedCtrlSWC", auditor=audit_log)

    # Generate artifacts
    arxml_path = tmp_path / "dist" / "model.arxml"
    generate_arxml(model_root, str(arxml_path))
    assert arxml_path.exists(), "ARXML file was not created"
    arxml_content = arxml_path.read_text(encoding="utf-8")
    assert "<?xml" in arxml_content
    assert "SpeedCtrlSWC" in arxml_content
    assert "ASSEMBLY-SW-CONNECTOR" in arxml_content

    views_root = tmp_path / "views"
    diagrams = generate_diagrams(model_root, str(views_root), "integration", node=requires_node())
    # The root composition's internal block diagram names both placed components.
    assert "ibd-Root" in diagrams.views, diagrams.views
    ibd = (views_root / "ibd-Root.svg").read_text(encoding="utf-8")
    assert "SpeedSensorSWC" in ibd
    assert "SpeedCtrlSWC" in ibd

    needs_path = tmp_path / "dist" / "needs.json"
    generate_needs(model_root, str(needs_path), "integration")
    assert needs_path.exists(), "needs.json was not created"
    needs_content = needs_path.read_text(encoding="utf-8")
    assert "REQ_SPEED_001" in needs_content

    # Cross-layer consistency
    result = check_cross_layer_consistency(model_root)
    assert result.consistent, f"Consistency check failed: {result.issues}"

    # Audit log
    entries = audit_log.get_entries()
    assert len(entries) == 12, f"Expected 12 audit entries, got {len(entries)}: {entries}"
    operations = [e["operation"] for e in entries]
    assert "create_swc" in operations
    assert "create_data_type" in operations
    assert "create_interface" in operations
    assert "add_port" in operations
    assert "create_connector" in operations
    assert "add_runnable" in operations
    assert "add_timing_event" in operations
    assert "create_requirement" in operations
    assert "add_trace_link" in operations


# Smoke tests on the committed model


def test_read_data_types_returns_seed_primitives():
    """read_data_types loads the twelve seed types of examples/basic."""
    data_types = read_data_types(_REAL_MODEL_ROOT)
    names = {dt.name for dt in data_types}
    expected_primitives = {
        "float32",
        "float64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "int8",
        "int16",
        "int32",
        "int64",
        "bool",
        "boolean",
    }
    assert expected_primitives.issubset(names), f"Missing seed primitives: {expected_primitives - names}"


def test_create_data_type_round_trip(tmp_path):
    """create_data_type writes a file that read_data_types can parse back correctly."""
    model_root = str(tmp_path)
    path = create_data_type(
        model_root,
        "VehicleSpeed",
        "value_type",
        base="float32",
        unit="km_h",
        value_min=0.0,
        value_max=300.0,
        description="Vehicle longitudinal speed",
    )
    assert pathlib.Path(path).exists(), "create_data_type did not create the file"

    found = find_data_type_file(model_root, "VehicleSpeed")
    assert found is not None, "find_data_type_file returned None for a just-created type"
    assert found == path

    loaded = read_data_types(model_root)
    assert len(loaded) == 1
    dt = loaded[0]
    assert dt.name == "VehicleSpeed"
    assert dt.kind == "value_type"


def test_check_data_type_references_no_issues_on_real_model():
    """check_data_type_references must report no issues on the committed model."""
    result = check_data_type_references(_REAL_MODEL_ROOT)
    assert result.consistent, f"Unexpected reference issues in real model: {result.issues}"


def test_check_data_type_cycles_no_issues_on_seed_primitives():
    """check_data_type_cycles must report no cycles in the seed primitive types."""
    result = check_data_type_cycles(_REAL_MODEL_ROOT)
    assert result.consistent, f"Unexpected cycles in real model data types: {result.issues}"
