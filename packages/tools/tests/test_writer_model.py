"""Tests for writer/mutations.py: create_requirement, add_trace_link, create_cs_interface."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError
from swcraft_tools.auditor import AuditLog
from swcraft_tools.reader import read_interfaces, read_requirements, read_swc_model
from swcraft_tools.schema import CSInterface
from swcraft_tools.writer import (
    add_trace_link,
    create_cs_interface,
    create_interface,
    create_requirement,
    create_swc,
)

# ---------------------------------------------------------------------------
# create_requirement
# ---------------------------------------------------------------------------


def test_create_requirement_creates_yaml(tmp_path):
    path = create_requirement(str(tmp_path), "REQ_001", "Speed signal", "The system shall...")
    expected = tmp_path / "requirements" / "REQ_001.yaml"
    assert expected.exists(), f"Expected {expected} to exist"
    assert path == str(expected)

    reqs = read_requirements(str(tmp_path))
    assert len(reqs) == 1
    assert reqs[0].id == "REQ_001"
    assert reqs[0].title == "Speed signal"
    assert reqs[0].description == "The system shall..."
    assert reqs[0].req_type == "req"
    # `version` and `source` are required.
    assert reqs[0].version == 1
    assert reqs[0].source == "architecture"
    assert reqs[0].derived_from == []


def test_create_requirement_writes_version_and_source_always(tmp_path):
    """``version`` and ``source`` are written even at their defaults; ``derived_from`` is omitted empty."""
    create_requirement(str(tmp_path), "REQ_001", "Speed signal", "The system shall...")
    create_requirement(str(tmp_path), "REQ_002", "Brake", "The system shall brake.", version=3, source="external")

    first = yaml.safe_load((tmp_path / "requirements" / "REQ_001.yaml").read_text(encoding="utf-8"))
    second = yaml.safe_load((tmp_path / "requirements" / "REQ_002.yaml").read_text(encoding="utf-8"))

    assert (first["version"], first["source"]) == (1, "architecture")
    assert (second["version"], second["source"]) == (3, "external")
    assert "derived_from" not in first
    assert "links" not in first and "status" not in first


def test_create_requirement_duplicate_raises(tmp_path):
    create_requirement(str(tmp_path), "REQ_001", "Speed signal", "The system shall...")
    with pytest.raises(ValueError, match="REQ_001"):
        create_requirement(str(tmp_path), "REQ_001", "Another title", "Another desc")


def test_create_requirement_empty_id_raises(tmp_path):
    with pytest.raises(ValueError):
        create_requirement(str(tmp_path), "", "Speed signal", "The system shall...")


def test_create_requirement_spec_type(tmp_path):
    create_requirement(str(tmp_path), "SPEC_001", "Spec title", "Spec description", req_type="spec")
    data = yaml.safe_load((tmp_path / "requirements" / "SPEC_001.yaml").read_text(encoding="utf-8"))
    assert data["req_type"] == "spec"


def test_create_requirement_with_auditor(tmp_path):
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    create_requirement(str(tmp_path), "REQ_001", "Speed signal", "The system shall...", auditor=auditor)
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "create_requirement"


# ---------------------------------------------------------------------------
# add_trace_link -- it writes the COMPONENT's `satisfies`
# ---------------------------------------------------------------------------


def _make_requirement(tmp_path, req_id="REQ_001", **extra):
    """Helper: create a requirement and return its path."""
    return create_requirement(str(tmp_path), req_id, "Speed signal", "The system shall...", **extra)


def _satisfies(tmp_path, swc_name):
    return next(swc for swc in read_swc_model(str(tmp_path)) if swc.name == swc_name).satisfies


def test_add_trace_link_updates_yaml(tmp_path):
    _make_requirement(tmp_path)
    create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
    result = add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC")

    assert _satisfies(tmp_path, "SpeedCtrlSWC") == ["REQ_001@1"]
    # The component file is what is written and returned; the requirement is untouched.
    assert result == str(tmp_path / "swc" / "body" / "SpeedCtrlSWC.yaml")
    requirement = yaml.safe_load((tmp_path / "requirements" / "REQ_001.yaml").read_text(encoding="utf-8"))
    assert "links" not in requirement


def test_add_trace_link_writes_the_requirements_current_version(tmp_path):
    _make_requirement(tmp_path, version=4)
    create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
    add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC")

    assert _satisfies(tmp_path, "SpeedCtrlSWC") == ["REQ_001@4"]


def test_add_trace_link_nonexistent_req_raises(tmp_path):
    create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
    with pytest.raises(ValueError, match="REQ_999"):
        add_trace_link(str(tmp_path), "REQ_999", "SpeedCtrlSWC")


def test_add_trace_link_nonexistent_component_raises(tmp_path):
    _make_requirement(tmp_path)
    with pytest.raises(ValueError, match="GhostSWC"):
        add_trace_link(str(tmp_path), "REQ_001", "GhostSWC")


def test_add_trace_link_refuses_an_id_two_requirement_files_declare(tmp_path):
    """There is no one version to write; the model is ``name.duplicate-requirement``'s finding."""
    _make_requirement(tmp_path)
    twin = tmp_path / "requirements" / "REQ_001_twin.yaml"
    twin.write_text(
        (tmp_path / "requirements" / "REQ_001.yaml").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")

    with pytest.raises(ValueError, match="declared by 2 files"):
        add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC")
    assert _satisfies(tmp_path, "SpeedCtrlSWC") == []


def test_add_trace_link_idempotent(tmp_path):
    """Calling add_trace_link twice with the same SWC must not create a duplicate."""
    _make_requirement(tmp_path)
    create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
    add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC")
    add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC")

    assert _satisfies(tmp_path, "SpeedCtrlSWC") == ["REQ_001@1"]


def test_add_trace_link_leaves_a_stale_version_alone(tmp_path):
    """Idempotent **by id**: a member naming the id at another version is not confirmed in silence."""
    _make_requirement(tmp_path, version=2)
    create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
    path = tmp_path / "swc" / "body" / "SpeedCtrlSWC.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["satisfies"] = ["REQ_001@1"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC", auditor=auditor)

    assert _satisfies(tmp_path, "SpeedCtrlSWC") == ["REQ_001@1"]
    # The audit records the link the file holds, never a confirmation at version 2.
    assert [entry["args"]["link"] for entry in auditor.get_entries()] == ["REQ_001@1"]


def test_add_trace_link_audits_the_link_it_appends(tmp_path):
    _make_requirement(tmp_path, version=3)
    create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC", auditor=auditor)

    assert [entry["args"]["link"] for entry in auditor.get_entries()] == ["REQ_001@3"]


def test_add_trace_link_refuses_a_version_too_wide_for_decimal_digits(tmp_path):
    """A hexadecimal ``version: 0x…`` loads as an integer no decimal link can spell."""
    _make_requirement(tmp_path)
    path = tmp_path / "requirements" / "REQ_001.yaml"
    text = path.read_text(encoding="utf-8").replace("version: 1", "version: 0x" + "f" * 4000)
    path.write_text(text, encoding="utf-8", newline="\n")
    create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")

    with pytest.raises(ValueError, match="too wide to write as decimal digits"):
        add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC")
    assert _satisfies(tmp_path, "SpeedCtrlSWC") == []


def test_add_trace_link_two_different_swcs(tmp_path):
    """Two components linked to one requirement each carry the link in their own file."""
    _make_requirement(tmp_path)
    create_swc(str(tmp_path), "SWC1", group="body")
    create_swc(str(tmp_path), "SWC2", group="body")
    add_trace_link(str(tmp_path), "REQ_001", "SWC1")
    add_trace_link(str(tmp_path), "REQ_001", "SWC2")

    assert _satisfies(tmp_path, "SWC1") == ["REQ_001@1"]
    assert _satisfies(tmp_path, "SWC2") == ["REQ_001@1"]


def test_add_trace_link_two_requirements_on_one_swc_keep_their_order(tmp_path):
    _make_requirement(tmp_path, "REQ_B")
    _make_requirement(tmp_path, "REQ_A")
    create_swc(str(tmp_path), "SWC1", group="body")
    add_trace_link(str(tmp_path), "REQ_B", "SWC1")
    add_trace_link(str(tmp_path), "REQ_A", "SWC1")

    assert _satisfies(tmp_path, "SWC1") == ["REQ_B@1", "REQ_A@1"]


def test_add_trace_link_with_auditor(tmp_path):
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    _make_requirement(tmp_path)
    create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
    add_trace_link(str(tmp_path), "REQ_001", "SpeedCtrlSWC", auditor=auditor)
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "add_trace_link"
    assert entries[0]["args"]["link"] == "REQ_001@1"
    assert entries[0]["args"]["swc_name"] == "SpeedCtrlSWC"


# ---------------------------------------------------------------------------
# create_cs_interface
# ---------------------------------------------------------------------------

_SAMPLE_OPERATIONS = [
    {
        "name": "ReadDtc",
        "arguments": [
            {"name": "request", "direction": "in", "implementation_type": "uint8"},
            {"name": "response", "direction": "out", "implementation_type": "uint8"},
        ],
    }
]


def test_create_cs_interface_creates_yaml(tmp_path):
    """create_cs_interface writes a valid YAML file at interfaces/{name}.yaml."""
    path = create_cs_interface(str(tmp_path), "IDiagnostic", _SAMPLE_OPERATIONS)
    expected = tmp_path / "interfaces" / "IDiagnostic.yaml"
    assert expected.exists(), f"Expected {expected} to exist"
    assert path == str(expected)

    ifaces = read_interfaces(str(tmp_path))
    assert len(ifaces) == 1
    assert isinstance(ifaces[0], CSInterface)
    cs = ifaces[0]
    assert cs.name == "IDiagnostic"
    assert cs.kind == "client_server"
    assert len(cs.operations) == 1
    assert cs.operations[0].name == "ReadDtc"
    assert len(cs.operations[0].arguments) == 2
    assert cs.operations[0].arguments[0].name == "request"
    assert cs.operations[0].arguments[0].direction == "in"
    assert cs.operations[0].arguments[0].implementation_type == "uint8"


def test_create_cs_interface_yaml_format(tmp_path):
    """YAML output uses default_flow_style=False and sort_keys=False."""
    create_cs_interface(str(tmp_path), "IDiagnostic", _SAMPLE_OPERATIONS)
    raw = (tmp_path / "interfaces" / "IDiagnostic.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert data["name"] == "IDiagnostic"
    assert data["kind"] == "client_server"
    assert isinstance(data["operations"], list)
    assert "{" not in raw  # safe_dump with default_flow_style=False never uses { for block-style


def test_create_cs_interface_duplicate_raises(tmp_path):
    """Creating a CS interface with the same name as an existing interface raises ValueError."""
    create_cs_interface(str(tmp_path), "IDiagnostic", _SAMPLE_OPERATIONS)
    with pytest.raises(ValueError, match="IDiagnostic"):
        create_cs_interface(str(tmp_path), "IDiagnostic", _SAMPLE_OPERATIONS)


def test_create_cs_interface_with_description_is_emitted(tmp_path):
    """CSInterface.description round-trips through the YAML."""
    create_cs_interface(
        str(tmp_path),
        "IDiagnostic",
        _SAMPLE_OPERATIONS,
        description="Diagnostic service interface.",
    )
    raw = (tmp_path / "interfaces" / "IDiagnostic.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert data["description"] == "Diagnostic service interface."

    ifaces = read_interfaces(str(tmp_path))
    assert ifaces[0].description == "Diagnostic service interface."


def test_create_cs_interface_without_description_omits_key(tmp_path):
    """No description -> the key is absent from the YAML, and a file without it loads (Pydantic default)."""
    create_cs_interface(str(tmp_path), "IDiagnostic", _SAMPLE_OPERATIONS)
    data = yaml.safe_load((tmp_path / "interfaces" / "IDiagnostic.yaml").read_text(encoding="utf-8"))
    assert "description" not in data

    ifaces = read_interfaces(str(tmp_path))
    assert ifaces[0].description == ""


def test_create_cs_interface_duplicate_sr_name_raises(tmp_path):
    """Creating a CS interface with the same name as an existing SR interface raises ValueError."""
    create_interface(str(tmp_path), "IFoo", [{"name": "speed", "implementation_type": "uint8"}])
    with pytest.raises(ValueError, match="IFoo"):
        create_cs_interface(str(tmp_path), "IFoo", _SAMPLE_OPERATIONS)


def test_create_cs_interface_path_traversal_slash_raises(tmp_path):
    """Name containing '/' raises ValueError."""
    with pytest.raises(ValueError, match="path separators"):
        create_cs_interface(str(tmp_path), "a/b", _SAMPLE_OPERATIONS)


def test_create_cs_interface_path_traversal_backslash_raises(tmp_path):
    """Name containing '\\' raises ValueError."""
    with pytest.raises(ValueError, match="path separators"):
        create_cs_interface(str(tmp_path), "a\\b", _SAMPLE_OPERATIONS)


def test_create_cs_interface_path_traversal_dotdot_raises(tmp_path):
    """Name containing '..' raises ValueError."""
    with pytest.raises(ValueError, match="path separators"):
        create_cs_interface(str(tmp_path), "a..b", _SAMPLE_OPERATIONS)


def test_create_cs_interface_empty_name_raises(tmp_path):
    """Empty name raises ValueError."""
    with pytest.raises(ValueError, match="name must not be empty"):
        create_cs_interface(str(tmp_path), "", _SAMPLE_OPERATIONS)


def test_create_cs_interface_empty_operations_raises(tmp_path):
    """Empty operations list raises ValidationError (via Pydantic)."""
    with pytest.raises(ValidationError):
        create_cs_interface(str(tmp_path), "IDiagnostic", [])


def test_create_cs_interface_invalid_direction_raises(tmp_path):
    """Invalid argument direction raises ValidationError (via Pydantic)."""
    bad_ops = [
        {
            "name": "ReadDtc",
            "arguments": [{"name": "req", "direction": "bad_dir", "implementation_type": "uint8"}],
        }
    ]
    with pytest.raises(ValidationError):
        create_cs_interface(str(tmp_path), "IDiagnostic", bad_ops)


def test_create_cs_interface_audit_log_entry(tmp_path):
    """Audit log entry is written with operation='create_cs_interface'."""
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    create_cs_interface(str(tmp_path), "IDiagnostic", _SAMPLE_OPERATIONS, auditor=auditor)
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "create_cs_interface"
    assert entries[0]["args"]["name"] == "IDiagnostic"


def test_create_cs_interface_no_arguments(tmp_path):
    """Operation with no arguments is valid (arguments list is optional)."""
    ops = [{"name": "Reset", "arguments": []}]
    create_cs_interface(str(tmp_path), "IControl", ops)
    ifaces = read_interfaces(str(tmp_path))
    assert len(ifaces) == 1
    cs = ifaces[0]
    assert isinstance(cs, CSInterface)
    assert cs.operations[0].name == "Reset"
    assert cs.operations[0].arguments == []


def test_create_cs_interface_none_operations_raises(tmp_path):
    """Passing None for operations raises ValueError, not TypeError.

    The MCP wrapper catches (ValueError, ValidationError) but not TypeError, so
    a bare None must be caught before the list comprehension iterates it.
    """
    with pytest.raises(ValueError, match="operations must be a list"):
        create_cs_interface(str(tmp_path), "IDiagnostic", None)  # type: ignore[arg-type]
