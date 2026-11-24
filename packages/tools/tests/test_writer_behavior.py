"""Tests for the runnable mutations, and that every mutation sharing a hand-written dump shape preserves each field."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from swcraft_tools.auditor import AuditLog
from swcraft_tools.reader import read_requirements, read_swc_model
from swcraft_tools.schema import Requirement, SWComponent, Task
from swcraft_tools.writer import (
    add_init_event,
    add_port,
    add_runnable,
    add_timing_event,
    add_trace_link,
    create_hardware_element,
    create_interface,
    create_requirement,
    create_sequence,
    create_swc,
    create_task,
    map_runnable_to_task,
    set_swc_memory_budget,
    set_task_partition,
    set_task_priority,
    set_task_wcet,
    unset_swc_memory_budget,
)
from swcraft_tools.writer.elements import write_element
from swcraft_tools.writer.yaml_documents import requirement_to_dict, swc_to_dict, task_to_dict
from swcraft_tools.yaml_load import load_yaml_document

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_swc(tmp_path, name: str = "FooSWC", group: str = "body") -> str:
    """Create a SWC and return its path."""
    return create_swc(str(tmp_path), name, group=group)


# ---------------------------------------------------------------------------
# add_runnable
# ---------------------------------------------------------------------------


def test_add_runnable_updates_yaml(tmp_path):
    _make_swc(tmp_path)
    result = add_runnable(str(tmp_path), "FooSWC", "Run1")
    swcs = read_swc_model(str(tmp_path))
    assert len(swcs) == 1
    assert len(swcs[0].runnables) == 1
    r = swcs[0].runnables[0]
    assert r.name == "Run1"
    assert r.min_start_interval == 0.0
    assert result is not None


def test_add_runnable_with_min_start_interval(tmp_path):
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1", min_start_interval=0.005)
    swcs = read_swc_model(str(tmp_path))
    assert swcs[0].runnables[0].min_start_interval == 0.005


def test_add_runnable_duplicate_raises(tmp_path):
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    with pytest.raises(ValueError, match="Run1"):
        add_runnable(str(tmp_path), "FooSWC", "Run1")


def test_add_runnable_nonexistent_swc_raises(tmp_path):
    with pytest.raises(ValueError, match="NonExistent"):
        add_runnable(str(tmp_path), "NonExistent", "Run1")


def test_add_runnable_with_auditor(tmp_path):
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1", auditor=auditor)
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "add_runnable"


def test_add_runnable_multiple_runnables(tmp_path):
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    add_runnable(str(tmp_path), "FooSWC", "Run2")
    swcs = read_swc_model(str(tmp_path))
    assert len(swcs[0].runnables) == 2


# ---------------------------------------------------------------------------
# add_timing_event
# ---------------------------------------------------------------------------


def test_add_timing_event_updates_runnable(tmp_path):
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    result = add_timing_event(str(tmp_path), "FooSWC", "Run1", period=0.01)
    swcs = read_swc_model(str(tmp_path))
    r = swcs[0].runnables[0]
    assert len(r.events) == 1
    assert r.events[0].type == "timing"
    assert r.events[0].period == pytest.approx(0.01)
    assert result is not None


def test_add_timing_event_zero_period_raises(tmp_path):
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    with pytest.raises(ValueError):
        add_timing_event(str(tmp_path), "FooSWC", "Run1", period=0.0)


def test_add_timing_event_negative_period_raises(tmp_path):
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    with pytest.raises(ValueError):
        add_timing_event(str(tmp_path), "FooSWC", "Run1", period=-0.01)


def test_add_timing_event_nonexistent_runnable_raises(tmp_path):
    _make_swc(tmp_path)
    with pytest.raises(ValueError, match="NonExistentRunnable"):
        add_timing_event(str(tmp_path), "FooSWC", "NonExistentRunnable", period=0.01)


def test_add_timing_event_nonexistent_swc_raises(tmp_path):
    with pytest.raises(ValueError, match="NonExistent"):
        add_timing_event(str(tmp_path), "NonExistent", "Run1", period=0.01)


def test_add_timing_event_with_auditor(tmp_path):
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    add_timing_event(str(tmp_path), "FooSWC", "Run1", period=0.01, auditor=auditor)
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "add_timing_event"


# ---------------------------------------------------------------------------
# add_init_event
# ---------------------------------------------------------------------------


def test_add_init_event_updates_runnable(tmp_path):
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    result = add_init_event(str(tmp_path), "FooSWC", "Run1")
    swcs = read_swc_model(str(tmp_path))
    r = swcs[0].runnables[0]
    assert len(r.events) == 1
    assert r.events[0].type == "init"
    assert result is not None


def test_add_init_event_nonexistent_runnable_raises(tmp_path):
    _make_swc(tmp_path)
    with pytest.raises(ValueError, match="NonExistentRunnable"):
        add_init_event(str(tmp_path), "FooSWC", "NonExistentRunnable")


def test_add_init_event_nonexistent_swc_raises(tmp_path):
    with pytest.raises(ValueError, match="NonExistent"):
        add_init_event(str(tmp_path), "NonExistent", "Run1")


def test_add_init_event_with_auditor(tmp_path):
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    add_init_event(str(tmp_path), "FooSWC", "Run1", auditor=auditor)
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "add_init_event"


def test_runnable_can_have_both_timing_and_init_events(tmp_path):
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    add_timing_event(str(tmp_path), "FooSWC", "Run1", period=0.01)
    add_init_event(str(tmp_path), "FooSWC", "Run1")
    swcs = read_swc_model(str(tmp_path))
    r = swcs[0].runnables[0]
    assert len(r.events) == 2
    event_types = {e.type for e in r.events}
    assert "timing" in event_types
    assert "init" in event_types


def test_add_timing_event_duplicate_raises(tmp_path):
    """Adding a second timing event to the same runnable raises ValueError."""
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    add_timing_event(str(tmp_path), "FooSWC", "Run1", period=0.01)
    with pytest.raises(ValueError, match="Timing event already exists"):
        add_timing_event(str(tmp_path), "FooSWC", "Run1", period=0.02)


def test_add_init_event_duplicate_raises(tmp_path):
    """Adding a second init event to the same runnable raises ValueError."""
    _make_swc(tmp_path)
    add_runnable(str(tmp_path), "FooSWC", "Run1")
    add_init_event(str(tmp_path), "FooSWC", "Run1")
    with pytest.raises(ValueError, match="Init event already exists"):
        add_init_event(str(tmp_path), "FooSWC", "Run1")


# ---------------------------------------------------------------------------
# The three access lists through the mutations.
#
# `yaml_documents.swc_to_dict` is hand-written and shared: a field it drops is
# silently erased by every mutation that rewrites the SWC file, so each calling
# mutation (`grep -n 'swc_to_dict(' writer/mutations.py`) gets a preservation
# case. `map_runnable_to_task` reads the runnable and rewrites the task file
# only, so its case asserts the SWC file is untouched. The fixture is written by
# hand, so the cases do not depend on `add_runnable`'s list parameters.
# ---------------------------------------------------------------------------

_ACCESS_SWC_YAML = (
    "name: Control\n"
    "type: application\n"
    "ports:\n"
    "- name: rSpeed\n"
    "  direction: require\n"
    "  interface: ISpeed\n"
    "- name: pTorque\n"
    "  direction: provide\n"
    "  interface: ITorque\n"
    "- name: rDiag\n"
    "  direction: require\n"
    "  interface: IDiag\n"
    "runnables:\n"
    "- name: ControlRun\n"
    "  events:\n"
    "  - type: timing\n"
    "    period: 0.01\n"
    "  reads:\n"
    "  - port: rSpeed\n"
    "    element: value\n"
    "  - port: rSpeed\n"
    "    element: valid\n"
    "    by: value\n"
    "  writes:\n"
    "  - port: pTorque\n"
    "    element: valid\n"
    "  - port: pTorque\n"
    "    element: value\n"
    "  calls:\n"
    "  - port: rDiag\n"
    "    operation: ReadDtc\n"
    "- name: Idle\n"
    "  events: []\n"
)

#: The `ControlRun` block of the fixture -- everything from its `- name:` line
#: up to the next runnable -- the bytes every mutation must carry verbatim.
_CONTROL_RUN_BLOCK = _ACCESS_SWC_YAML[
    _ACCESS_SWC_YAML.index("- name: ControlRun") : _ACCESS_SWC_YAML.index("- name: Idle")
]


def _write_access_swc(tmp_path: Path) -> Path:
    swc_file = tmp_path / "swc" / "body" / "Control.yaml"
    swc_file.parent.mkdir(parents=True)
    swc_file.write_text(_ACCESS_SWC_YAML, encoding="utf-8", newline="\n")
    return swc_file


def _control_run_dict(swc_file: Path) -> dict:
    return yaml.safe_load(swc_file.read_text(encoding="utf-8"))["runnables"][0]


def _declare_memory(tmp_path: Path, name: str) -> None:
    create_hardware_element(
        str(tmp_path), name, "memory", memory_type="RAM", size=4096, accessible=["CORE0"], threshold=0.8
    )


class TestTheAccessListsSurviveEveryMutationSharingSwcToDict:
    """One case per caller of `swc_to_dict` that rewrites an existing file, plus `map_runnable_to_task`.

    Each asserts the runnable's block byte for byte (catches a reorder or re-key) and
    the parsed runnable (explains a failure).

    What it does not reach: `write_element`, which cannot drop a field by construction
    (`test_writer_elements.py`)."""

    def test_add_port_keeps_the_lists(self, tmp_path):
        swc_file = _write_access_swc(tmp_path)
        before = _control_run_dict(swc_file)
        add_port(str(tmp_path), "Control", "pStatus", "provide", "IStatus")
        text = swc_file.read_text(encoding="utf-8")
        assert _CONTROL_RUN_BLOCK in text
        assert _control_run_dict(swc_file) == before
        assert text.endswith(_CONTROL_RUN_BLOCK + "- name: Idle\n  events: []\n")

    def test_add_runnable_keeps_the_lists_of_the_existing_runnable(self, tmp_path):
        swc_file = _write_access_swc(tmp_path)
        before = _control_run_dict(swc_file)
        add_runnable(str(tmp_path), "Control", "Later")
        text = swc_file.read_text(encoding="utf-8")
        assert _CONTROL_RUN_BLOCK in text
        assert _control_run_dict(swc_file) == before
        assert text == _ACCESS_SWC_YAML + "- name: Later\n  events: []\n"

    def test_add_timing_event_on_another_runnable_keeps_the_lists(self, tmp_path):
        swc_file = _write_access_swc(tmp_path)
        before = _control_run_dict(swc_file)
        add_timing_event(str(tmp_path), "Control", "Idle", period=0.1)
        text = swc_file.read_text(encoding="utf-8")
        assert _CONTROL_RUN_BLOCK in text
        assert _control_run_dict(swc_file) == before
        assert text.endswith("- name: Idle\n  events:\n  - type: timing\n    period: 0.1\n")

    def test_add_init_event_on_the_accessing_runnable_keeps_its_lists_after_the_event(self, tmp_path):
        """An event added to the accessing runnable lands before its lists, which stay unchanged."""
        swc_file = _write_access_swc(tmp_path)
        add_init_event(str(tmp_path), "Control", "ControlRun")
        text = swc_file.read_text(encoding="utf-8")
        lists = _CONTROL_RUN_BLOCK[_CONTROL_RUN_BLOCK.index("  reads:") :]
        assert lists in text
        assert text.index("  - type: init\n") < text.index("  reads:\n")
        assert _control_run_dict(swc_file)["reads"] == [
            {"port": "rSpeed", "element": "value"},
            {"port": "rSpeed", "element": "valid", "by": "value"},
        ]

    def test_add_init_event_on_another_runnable_keeps_the_lists(self, tmp_path):
        swc_file = _write_access_swc(tmp_path)
        before = _control_run_dict(swc_file)
        add_init_event(str(tmp_path), "Control", "Idle")
        text = swc_file.read_text(encoding="utf-8")
        assert _CONTROL_RUN_BLOCK in text
        assert _control_run_dict(swc_file) == before
        assert text.endswith("- name: Idle\n  events:\n  - type: init\n")

    def test_map_runnable_to_task_does_not_touch_the_swc_file(self, tmp_path):
        swc_file = _write_access_swc(tmp_path)
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        map_runnable_to_task(str(tmp_path), "Task_10ms", "Control", "ControlRun")
        assert swc_file.read_text(encoding="utf-8") == _ACCESS_SWC_YAML
        task = yaml.safe_load((tmp_path / "tasks" / "Task_10ms.yaml").read_text(encoding="utf-8"))
        assert task["runnables"] == [{"swc": "Control", "runnable": "ControlRun"}]

    def test_set_swc_memory_budget_keeps_the_lists(self, tmp_path):
        swc_file = _write_access_swc(tmp_path)
        _declare_memory(tmp_path, "RAM")
        before = _control_run_dict(swc_file)
        set_swc_memory_budget(str(tmp_path), "Control", "RAM", 2048)
        text = swc_file.read_text(encoding="utf-8")
        assert _CONTROL_RUN_BLOCK in text
        assert _control_run_dict(swc_file) == before
        assert text == _ACCESS_SWC_YAML + "memory:\n  RAM: 2048\n"

    def test_unset_swc_memory_budget_keeps_the_lists(self, tmp_path):
        swc_file = _write_access_swc(tmp_path)
        _declare_memory(tmp_path, "RAM")
        set_swc_memory_budget(str(tmp_path), "Control", "RAM", 2048)
        unset_swc_memory_budget(str(tmp_path), "Control", "RAM")
        assert swc_file.read_text(encoding="utf-8") == _ACCESS_SWC_YAML


class TestAddRunnableCarriesTheLists:
    """`add_runnable`'s `reads`, `writes`, `calls`, validated by the `Runnable` model as the loader validates a file.

    What it does not reach: the cross-element access rules (an undeclared port, an
    element the interface does not carry) -- those are `check-model`'s."""

    def _swc_with_ports(self, tmp_path) -> Path:
        create_swc(str(tmp_path), "Control", group="body")
        add_port(str(tmp_path), "Control", "rSpeed", "require", "ISpeed")
        add_port(str(tmp_path), "Control", "pTorque", "provide", "ITorque")
        add_port(str(tmp_path), "Control", "rDiag", "require", "IDiag")
        return tmp_path / "swc" / "body" / "Control.yaml"

    def test_the_lists_land_in_the_written_bytes_in_declaration_order(self, tmp_path):
        swc_file = self._swc_with_ports(tmp_path)
        add_runnable(
            str(tmp_path),
            "Control",
            "ControlRun",
            reads=[{"port": "rSpeed", "element": "value"}, {"port": "rSpeed", "element": "valid", "by": "value"}],
            writes=[{"port": "pTorque", "element": "valid"}, {"port": "pTorque", "element": "value"}],
            calls=[{"port": "rDiag", "operation": "ReadDtc"}],
        )
        add_timing_event(str(tmp_path), "Control", "ControlRun", period=0.01)
        add_runnable(str(tmp_path), "Control", "Idle")
        assert swc_file.read_text(encoding="utf-8") == _ACCESS_SWC_YAML

    def test_the_default_writes_the_runnable_exactly_as_before(self, tmp_path):
        swc_file = self._swc_with_ports(tmp_path)
        add_runnable(str(tmp_path), "Control", "ControlRun")
        text = swc_file.read_text(encoding="utf-8")
        assert text.endswith("runnables:\n- name: ControlRun\n  events: []\n")
        assert "reads" not in text and "writes" not in text and "calls" not in text

    def test_one_list_alone_is_written_alone(self, tmp_path):
        swc_file = self._swc_with_ports(tmp_path)
        add_runnable(str(tmp_path), "Control", "Writer", writes=[{"port": "pTorque", "element": "value"}])
        text = swc_file.read_text(encoding="utf-8")
        assert text.endswith("- name: Writer\n  events: []\n  writes:\n  - port: pTorque\n    element: value\n")
        assert "reads" not in text and "calls" not in text

    def test_the_reader_reads_back_what_was_written(self, tmp_path):
        self._swc_with_ports(tmp_path)
        add_runnable(
            str(tmp_path),
            "Control",
            "ControlRun",
            reads=[{"port": "rSpeed", "element": "valid", "by": "value"}],
            calls=[{"port": "rDiag", "operation": "ReadDtc"}],
        )
        runnable = read_swc_model(str(tmp_path))[0].runnables[0]
        assert [(r.port, r.element, r.by) for r in runnable.reads] == [("rSpeed", "valid", "value")]
        assert runnable.writes == []
        assert [(c.port, c.operation) for c in runnable.calls] == [("rDiag", "ReadDtc")]

    def test_a_duplicate_entry_is_refused_before_writing(self, tmp_path):
        swc_file = self._swc_with_ports(tmp_path)
        before = swc_file.read_text(encoding="utf-8")
        auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
        with pytest.raises(ValueError, match="duplicate reads entry in runnable: rSpeed.value"):
            add_runnable(
                str(tmp_path),
                "Control",
                "ControlRun",
                reads=[{"port": "rSpeed", "element": "value"}, {"port": "rSpeed", "element": "value", "by": "value"}],
                auditor=auditor,
            )
        assert swc_file.read_text(encoding="utf-8") == before
        assert auditor.get_entries() == []

    def test_a_write_carries_its_form_only_when_implicit(self, tmp_path):
        """A write is `{port, element}`, or with `by: implicit` after `element`; `by: value` is refused on a write."""
        swc_file = self._swc_with_ports(tmp_path)
        add_runnable(str(tmp_path), "Control", "ControlRun", writes=[{"port": "pTorque", "element": "v"}])
        text = swc_file.read_text(encoding="utf-8")
        assert text.endswith("  writes:\n  - port: pTorque\n    element: v\n")
        assert "by" not in text
        add_runnable(
            str(tmp_path), "Control", "ImplicitRun", writes=[{"port": "pTorque", "element": "v", "by": "implicit"}]
        )
        assert swc_file.read_text(encoding="utf-8").endswith(
            "  writes:\n  - port: pTorque\n    element: v\n    by: implicit\n"
        )
        with pytest.raises(ValidationError):
            add_runnable(
                str(tmp_path), "Control", "ValueRun", writes=[{"port": "pTorque", "element": "v", "by": "value"}]
            )

    def test_the_audit_entry_carries_the_lists(self, tmp_path):
        self._swc_with_ports(tmp_path)
        auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
        add_runnable(
            str(tmp_path),
            "Control",
            "ControlRun",
            reads=[{"port": "rSpeed", "element": "valid", "by": "value"}],
            auditor=auditor,
        )
        (entry,) = auditor.get_entries()
        assert entry["operation"] == "add_runnable"
        assert entry["args"]["reads"] == [{"port": "rSpeed", "element": "valid", "by": "value"}]
        assert entry["args"]["writes"] == []
        assert entry["args"]["calls"] == []


# ---------------------------------------------------------------------------
# The declared safety integrity level through the mutations.
#
# `yaml_documents.swc_to_dict` and `requirement_to_dict` are hand-written, so each
# calling mutation (`grep -n 'swc_to_dict(' writer/mutations.py`, the same for the
# requirement) gets a preservation case. `create_swc` and `create_requirement`
# write a new file and take no level, so they must write no `asil` key at all.
# The fixtures are written by hand, never through a mutation.
# ---------------------------------------------------------------------------

_ASIL_SWC_YAML = (
    "name: Guard\n"
    "type: application\n"
    "ports:\n"
    "- name: rSpeed\n"
    "  direction: require\n"
    "  interface: ISpeed\n"
    # A PORT field: an erasure one level deeper than `asil`, witnessed by every caller case.
    "  measures:\n"
    "    speed: End-to-end protection profile 4.\n"
    # The trace link on the port, the runnable and the component, each a field `swc_to_dict` must keep.
    "  satisfies:\n"
    "  - REQ_PORT@1\n"
    "runnables:\n"
    "- name: GuardRun\n"
    "  events:\n"
    "  - type: timing\n"
    "    period: 0.01\n"
    "- name: Idle\n"
    "  events: []\n"
    "  satisfies:\n"
    "  - REQ_RUN@2\n"
    "asil: D\n"
    "satisfies:\n"
    "- REQ_001@1\n"
)

_ASIL_REQUIREMENT_YAML = (
    "id: REQ_001\n"
    "title: The guard limits torque\n"
    "description: The system shall limit torque.\n"
    "req_type: req\n"
    "version: 1\n"
    "source: architecture\n"
    "derived_from:\n"
    "- REQ_PARENT@3\n"
    "asil: D\n"
)

#: The same requirement with its provenance after `asil`: `revision` a string the
#: dumper quotes because it would read as a number.
_PROVENANCE_REQUIREMENT_YAML = _ASIL_REQUIREMENT_YAML + "document: SRS-Brakes\nbaseline: BL-7\nrevision: '1.3'\n"

#: The same requirement as a stub: `stub` after the provenance, written only when true.
_STUB_REQUIREMENT_YAML = _PROVENANCE_REQUIREMENT_YAML + "stub: true\n"


def _write_asil_swc(tmp_path: Path) -> Path:
    swc_file = tmp_path / "swc" / "body" / "Guard.yaml"
    swc_file.parent.mkdir(parents=True)
    swc_file.write_text(_ASIL_SWC_YAML, encoding="utf-8", newline="\n")
    return swc_file


def _write_asil_requirement(tmp_path: Path) -> Path:
    req_file = tmp_path / "requirements" / "REQ_001.yaml"
    req_file.parent.mkdir(parents=True)
    req_file.write_text(_ASIL_REQUIREMENT_YAML, encoding="utf-8", newline="\n")
    return req_file


def _asil_of(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("asil", "<<absent>>")


def _satisfies_of(path: Path) -> tuple[object, object, object]:
    """The component's, the ``rSpeed`` port's and the ``Idle`` runnable's ``satisfies``, or a marker."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    port = next((p for p in document.get("ports", []) if p["name"] == "rSpeed"), {})
    runnable = next((r for r in document.get("runnables", []) if r["name"] == "Idle"), {})
    return (
        document.get("satisfies", "<<absent>>"),
        port.get("satisfies", "<<absent>>"),
        runnable.get("satisfies", "<<absent>>"),
    )


_GUARD_LINKS = (["REQ_001@1"], ["REQ_PORT@1"], ["REQ_RUN@2"])


def _measures_of(path: Path, port: str = "rSpeed"):
    """The named port's `measures` map, or a marker if the key was erased."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for declared in document["ports"]:
        if declared["name"] == port:
            return declared.get("measures", "<<absent>>")
    return "<<no such port>>"


class TestTheDeclaredLevelSurvivesEveryMutationSharingTheDumpShapes:
    """One case per caller that rewrites an existing file, plus the two creators.

    The level is a **declaration**: dropping it leaves the element valid, so no other
    gate would report the erasure.
    """

    def test_add_port_keeps_the_level(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        add_port(str(tmp_path), "Guard", "pTorque", "provide", "ITorque")
        assert _asil_of(swc_file) == "D"

    def test_add_runnable_keeps_the_level(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        add_runnable(str(tmp_path), "Guard", "Later")
        assert _asil_of(swc_file) == "D"

    def test_add_timing_event_keeps_the_level(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        add_timing_event(str(tmp_path), "Guard", "Idle", period=0.1)
        assert _asil_of(swc_file) == "D"

    def test_add_init_event_keeps_the_level(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        add_init_event(str(tmp_path), "Guard", "GuardRun")
        assert _asil_of(swc_file) == "D"

    def test_set_swc_memory_budget_keeps_the_level(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        create_hardware_element(
            str(tmp_path), "RAM", "memory", memory_type="RAM", size=4096, accessible=["CORE0"], threshold=0.8
        )
        set_swc_memory_budget(str(tmp_path), "Guard", "RAM", 2048)
        assert _asil_of(swc_file) == "D"

    def test_unset_swc_memory_budget_keeps_the_level(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        create_hardware_element(
            str(tmp_path), "RAM", "memory", memory_type="RAM", size=4096, accessible=["CORE0"], threshold=0.8
        )
        set_swc_memory_budget(str(tmp_path), "Guard", "RAM", 2048)
        unset_swc_memory_budget(str(tmp_path), "Guard", "RAM")
        assert swc_file.read_text(encoding="utf-8") == _ASIL_SWC_YAML

    def test_every_component_rewrite_keeps_the_three_links(self, tmp_path):
        """Each caller of ``swc_to_dict`` keeps the component's, a port's and a runnable's link."""
        create_hardware_element(
            str(tmp_path), "RAM", "memory", memory_type="RAM", size=4096, accessible=["CORE0"], threshold=0.8
        )
        rewrites = (
            lambda root: add_port(root, "Guard", "pTorque", "provide", "ITorque"),
            lambda root: add_runnable(root, "Guard", "Later"),
            lambda root: add_timing_event(root, "Guard", "Idle", period=0.1),
            lambda root: add_init_event(root, "Guard", "GuardRun"),
            lambda root: set_swc_memory_budget(root, "Guard", "RAM", 2048),
            lambda root: unset_swc_memory_budget(root, "Guard", "RAM"),
        )
        swc_file = _write_asil_swc(tmp_path)
        for rewrite in rewrites:
            rewrite(str(tmp_path))
            assert _satisfies_of(swc_file) == _GUARD_LINKS, rewrite

    def test_create_swc_writes_no_level_key_at_all(self, tmp_path):
        create_swc(str(tmp_path), "Plain", group="body")
        document = yaml.safe_load((tmp_path / "swc" / "body" / "Plain.yaml").read_text(encoding="utf-8"))
        assert "asil" not in document

    def test_add_trace_link_keeps_the_requirements_level(self, tmp_path):
        """``add_trace_link`` writes the component, and leaves the requirement byte-identical."""
        req_file = _write_asil_requirement(tmp_path)
        create_swc(str(tmp_path), "Second", group="body")
        add_trace_link(str(tmp_path), "REQ_001", "Second")
        assert _asil_of(req_file) == "D"
        assert req_file.read_text(encoding="utf-8") == _ASIL_REQUIREMENT_YAML

    def test_add_trace_link_keeps_the_components_level_and_links(self, tmp_path):
        """The component it rewrites keeps its level, its measures and every link it held."""
        swc_file = _write_asil_swc(tmp_path)
        _write_asil_requirement(tmp_path)
        (tmp_path / "requirements" / "REQ_NEW.yaml").write_text(
            _ASIL_REQUIREMENT_YAML.replace("REQ_001", "REQ_NEW"), encoding="utf-8", newline="\n"
        )
        add_trace_link(str(tmp_path), "REQ_NEW", "Guard")
        assert _asil_of(swc_file) == "D"
        assert _satisfies_of(swc_file) == (["REQ_001@1", "REQ_NEW@1"], ["REQ_PORT@1"], ["REQ_RUN@2"])

    def test_create_requirement_writes_no_level_key_at_all(self, tmp_path):
        create_requirement(str(tmp_path), "REQ_002", "T", "The system shall do nothing.")
        document = yaml.safe_load((tmp_path / "requirements" / "REQ_002.yaml").read_text(encoding="utf-8"))
        assert "asil" not in document

    def test_the_two_dump_shapes_agree_with_the_generic_writer(self, tmp_path):
        """Both dump shapes write a fully populated element exactly as `write_element` does.

        **Compared as text**: with `sort_keys=False` the key order is the file, and dict
        equality ignores order.
        """
        swc = SWComponent.model_validate(yaml.safe_load(_ASIL_SWC_YAML))
        requirement = Requirement.model_validate(yaml.safe_load(_STUB_REQUIREMENT_YAML))
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "swc/body/Guard.yaml", swc)
        write_element(str(root), "requirements/REQ_001.yaml", requirement)

        assert (root / "swc" / "body" / "Guard.yaml").read_text(encoding="utf-8") == yaml.safe_dump(
            swc_to_dict(swc), sort_keys=False, allow_unicode=True
        )
        assert (root / "requirements" / "REQ_001.yaml").read_text(encoding="utf-8") == yaml.safe_dump(
            requirement_to_dict(requirement), sort_keys=False, allow_unicode=True
        )


class TestTheRequirementsProvenance:
    """`document`, `baseline` and `revision`, three optional strings.

    Both write paths carry them when set, in `model_fields` order after `asil`,
    and write the bytes of before when they are not: a requirement that records
    no provenance is byte-identical on disk. `revision` is a string, so an
    unquoted YAML integer is refused rather than coerced -- the source tool's
    `1.3` and `B` are revisions as much as `7` is.
    """

    def test_a_requirement_with_all_three_loads_them_as_written(self):
        requirement = Requirement.model_validate(yaml.safe_load(_PROVENANCE_REQUIREMENT_YAML))
        assert (requirement.document, requirement.baseline, requirement.revision) == ("SRS-Brakes", "BL-7", "1.3")

    @pytest.mark.parametrize("text", [_PROVENANCE_REQUIREMENT_YAML, _ASIL_REQUIREMENT_YAML])
    def test_both_write_paths_write_the_file_back_byte_for_byte(self, tmp_path, text):
        """With the three, `revision: '1.3'` stays quoted; without them, the bytes are unchanged."""
        requirement = Requirement.model_validate(yaml.safe_load(text))
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "requirements/REQ_001.yaml", requirement)
        assert (root / "requirements" / "REQ_001.yaml").read_text(encoding="utf-8") == text
        assert yaml.safe_dump(requirement_to_dict(requirement), sort_keys=False, allow_unicode=True) == text

    def test_an_unset_provenance_writes_no_key(self):
        requirement = Requirement.model_validate(yaml.safe_load(_ASIL_REQUIREMENT_YAML))
        assert {"document", "baseline", "revision"}.isdisjoint(requirement_to_dict(requirement))

    @pytest.mark.parametrize(
        ("field", "value"),
        [("revision", 7), ("revision", 1.3), ("document", 12), ("baseline", True), ("baseline", ["BL-7"])],
    )
    def test_a_non_string_value_is_refused(self, field, value):
        """`revision: 7` unquoted is an integer to YAML, and a string field refuses it."""
        document = yaml.safe_load(_ASIL_REQUIREMENT_YAML)
        document[field] = value
        with pytest.raises(ValidationError, match=field):
            Requirement.model_validate(document)

    def test_an_empty_provenance_string_is_kept_and_both_write_paths_agree(self, tmp_path):
        """`document: ''` is a string like any other: loaded, kept, and written by both paths alike."""
        text = _ASIL_REQUIREMENT_YAML + "document: ''\n"
        requirement = Requirement.model_validate(yaml.safe_load(text))
        assert requirement.document == ""
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "requirements/REQ_001.yaml", requirement)
        assert (root / "requirements" / "REQ_001.yaml").read_text(encoding="utf-8") == text
        assert yaml.safe_dump(requirement_to_dict(requirement), sort_keys=False, allow_unicode=True) == text


class TestTheStubRequirement:
    """`stub`, a boolean written only when true (`omitAtDefault`).

    A stub is the placeholder the import creates for a parent `derived_from`
    names and nothing holds. Both write paths write `stub: true` after the
    provenance, and a requirement whose `stub` is `false` -- stated or absent --
    writes the bytes of before, so no real requirement gains the key on a
    rewrite. The field is strict: a quoted `'true'` or an integer is refused, so a
    rewrite never turns `stub: 1` into `stub: true` or drops `stub: 0`. An unquoted
    `yes`, `on`, `no` or `off` is a boolean to YAML 1.1 itself and loads; the two
    reader-level tests pin both halves from YAML text.
    """

    def test_a_stub_loads_and_both_write_paths_write_it_back_byte_for_byte(self, tmp_path):
        requirement = Requirement.model_validate(yaml.safe_load(_STUB_REQUIREMENT_YAML))
        assert requirement.stub is True
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "requirements/REQ_001.yaml", requirement)
        assert (root / "requirements" / "REQ_001.yaml").read_text(encoding="utf-8") == _STUB_REQUIREMENT_YAML
        assert (
            yaml.safe_dump(requirement_to_dict(requirement), sort_keys=False, allow_unicode=True)
            == _STUB_REQUIREMENT_YAML
        )

    @pytest.mark.parametrize("suffix", ["", "stub: false\n"], ids=["absent", "false"])
    def test_a_false_or_absent_stub_writes_the_bytes_of_before(self, tmp_path, suffix):
        requirement = Requirement.model_validate(yaml.safe_load(_PROVENANCE_REQUIREMENT_YAML + suffix))
        assert requirement.stub is False
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "requirements/REQ_001.yaml", requirement)
        assert (root / "requirements" / "REQ_001.yaml").read_text(encoding="utf-8") == _PROVENANCE_REQUIREMENT_YAML
        assert "stub" not in requirement_to_dict(requirement)

    @pytest.mark.parametrize("value", ["maybe", ["true"], {"stub": True}, 2, "true", "yes", "on", 1, "1", 0, "off"])
    def test_a_non_boolean_stub_is_refused(self, value):
        """Strict, like `version`: a spelling lax pydantic would read as a boolean is refused, not normalised."""
        document = yaml.safe_load(_PROVENANCE_REQUIREMENT_YAML)
        document["stub"] = value
        with pytest.raises(ValidationError, match="stub"):
            Requirement.model_validate(document)

    @pytest.mark.parametrize(
        ("spelling", "expected"),
        [("true", True), ("yes", True), ("on", True), ("false", False), ("no", False), ("off", False)],
    )
    def test_an_unquoted_yaml_boolean_spelling_loads_through_the_reader(self, tmp_path, spelling, expected):
        """What strict does not reach: libyaml (YAML 1.1) reads these as booleans before the model sees them."""
        assert load_yaml_document(f"stub: {spelling}\n") == {"stub": expected}
        root = _write_stub_requirement_text(tmp_path, f"stub: {spelling}\n")
        (requirement,) = read_requirements(str(root))
        assert requirement.stub is expected

    @pytest.mark.parametrize("spelling", ["'true'", "'yes'", '"on"', "'false'", "1", "0", "'1'"])
    def test_a_quoted_or_integer_stub_is_refused_through_the_reader(self, tmp_path, spelling):
        root = _write_stub_requirement_text(tmp_path, f"stub: {spelling}\n")
        with pytest.raises(ValueError, match="stub"):
            read_requirements(str(root))


def _write_stub_requirement_text(tmp_path: Path, stub_line: str) -> Path:
    """A model root whose one requirement is the provenance requirement plus *stub_line*, as YAML text."""
    root = tmp_path / "model"
    req_file = root / "requirements" / "REQ_001.yaml"
    req_file.parent.mkdir(parents=True)
    req_file.write_text(_PROVENANCE_REQUIREMENT_YAML + stub_line, encoding="utf-8", newline="\n")
    return root


# ---------------------------------------------------------------------------
# `measures`, `coexistence` and a data element's `asil`, on the same terms as `asil` above
#
# `measures` lives on a port, rendered inline by `swc_to_dict`, so its erasure is
# invisible to assertions about the component's own keys. A data element's `asil`
# goes through `data_element_to_dict`, a REBUILD as well as a dump:
# `create_interface` reconstructs each element from the caller's dict.
# ---------------------------------------------------------------------------

_COEXISTENCE = "The two components run in separate partitions with timing protection."


class TestThePortMeasuresSurviveEveryMutationThatRewritesTheComponent:
    """One case per `swc_to_dict` caller that rewrites an existing file.

    The two budget mutations round-trip the component through
    `SWComponent.model_validate({**swc_to_dict(swc), ...})`, so a key the dump forgets
    is gone after any budget edit.
    """

    def test_add_port_keeps_the_map(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        add_port(str(tmp_path), "Guard", "pTorque", "provide", "ITorque")
        assert _measures_of(swc_file) == {"speed": "End-to-end protection profile 4."}

    def test_add_runnable_keeps_the_map(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        add_runnable(str(tmp_path), "Guard", "Later")
        assert _measures_of(swc_file) == {"speed": "End-to-end protection profile 4."}

    def test_add_timing_event_keeps_the_map(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        add_timing_event(str(tmp_path), "Guard", "Idle", period=0.1)
        assert _measures_of(swc_file) == {"speed": "End-to-end protection profile 4."}

    def test_add_init_event_keeps_the_map(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        add_init_event(str(tmp_path), "Guard", "GuardRun")
        assert _measures_of(swc_file) == {"speed": "End-to-end protection profile 4."}

    def test_set_swc_memory_budget_keeps_the_map(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        create_hardware_element(
            str(tmp_path), "RAM", "memory", memory_type="RAM", size=4096, accessible=["CORE0"], threshold=0.8
        )
        set_swc_memory_budget(str(tmp_path), "Guard", "RAM", 2048)
        assert _measures_of(swc_file) == {"speed": "End-to-end protection profile 4."}

    def test_unset_swc_memory_budget_keeps_the_map(self, tmp_path):
        swc_file = _write_asil_swc(tmp_path)
        create_hardware_element(
            str(tmp_path), "RAM", "memory", memory_type="RAM", size=4096, accessible=["CORE0"], threshold=0.8
        )
        set_swc_memory_budget(str(tmp_path), "Guard", "RAM", 2048)
        unset_swc_memory_budget(str(tmp_path), "Guard", "RAM")
        assert swc_file.read_text(encoding="utf-8") == _ASIL_SWC_YAML

    def test_create_swc_writes_no_measures_key_at_all(self, tmp_path):
        create_swc(str(tmp_path), "Plain", group="body")
        document = yaml.safe_load((tmp_path / "swc" / "body" / "Plain.yaml").read_text(encoding="utf-8"))
        assert document["ports"] == []

    def test_add_port_writes_no_measures_key_on_the_new_port(self, tmp_path):
        """An omitted field, not a written empty map: the file a project keeps is unchanged."""
        swc_file = _write_asil_swc(tmp_path)
        add_port(str(tmp_path), "Guard", "pTorque", "provide", "ITorque")
        assert _measures_of(swc_file, "pTorque") == "<<absent>>"


class TestTheTaskArgumentAndTheElementLevelSurviveTheirOwnDumps:
    def test_set_task_wcet_keeps_the_coexistence_argument(self, tmp_path):
        create_task(str(tmp_path), "T", period=0.01, priority=5, partition="PARTITION0")
        task_file = tmp_path / "tasks" / "T.yaml"
        document = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        document["coexistence"] = _COEXISTENCE
        task_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        set_task_wcet(str(tmp_path), "T", "500us")

        assert yaml.safe_load(task_file.read_text(encoding="utf-8"))["coexistence"] == _COEXISTENCE

    def test_map_runnable_to_task_keeps_the_coexistence_argument(self, tmp_path):
        create_swc(str(tmp_path), "Guard", group="body")
        add_runnable(str(tmp_path), "Guard", "GuardRun")
        create_task(str(tmp_path), "T", period=0.01, priority=5, partition="PARTITION0")
        task_file = tmp_path / "tasks" / "T.yaml"
        document = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        document["coexistence"] = _COEXISTENCE
        task_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        map_runnable_to_task(str(tmp_path), "T", "Guard", "GuardRun")

        assert yaml.safe_load(task_file.read_text(encoding="utf-8"))["coexistence"] == _COEXISTENCE

    @pytest.mark.parametrize(
        "rewrite",
        ["set_task_wcet", "set_task_priority", "set_task_partition", "map_runnable_to_task"],
    )
    def test_every_task_rewrite_keeps_the_tasks_links(self, tmp_path, rewrite):
        """Each caller of ``task_to_dict`` that rewrites an existing task keeps ``satisfies``."""
        create_swc(str(tmp_path), "Guard", group="body")
        add_runnable(str(tmp_path), "Guard", "GuardRun")
        (tmp_path / "hardware").mkdir()
        for name in ("PARTITION0", "PARTITION1"):
            (tmp_path / "hardware" / f"{name}.yaml").write_text(
                f"name: {name}\nkind: partition\ncore: C0\n", encoding="utf-8", newline="\n"
            )
        create_task(str(tmp_path), "T", period=0.01, priority=5, partition="PARTITION0")
        task_file = tmp_path / "tasks" / "T.yaml"
        document = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        document["satisfies"] = ["REQ_TIMING@1", "REQ_SAFE@2"]
        task_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        calls = {
            "set_task_wcet": lambda root: set_task_wcet(root, "T", "500us"),
            "set_task_priority": lambda root: set_task_priority(root, "T", 7),
            "set_task_partition": lambda root: set_task_partition(root, "T", "PARTITION1"),
            "map_runnable_to_task": lambda root: map_runnable_to_task(root, "T", "Guard", "GuardRun"),
        }
        calls[rewrite](str(tmp_path))

        assert yaml.safe_load(task_file.read_text(encoding="utf-8"))["satisfies"] == ["REQ_TIMING@1", "REQ_SAFE@2"]

    def test_create_task_writes_no_coexistence_key_at_all(self, tmp_path):
        create_task(str(tmp_path), "T", period=0.01, priority=5, partition="PARTITION0")
        document = yaml.safe_load((tmp_path / "tasks" / "T.yaml").read_text(encoding="utf-8"))
        assert "coexistence" not in document

    def test_create_interface_carries_a_declared_element_level(self, tmp_path):
        """`create_interface` rebuilds each element from the caller's dict and keeps its declared level."""
        create_interface(
            str(tmp_path),
            "ISig",
            [
                {"name": "rated", "implementation_type": "uint8", "asil": "D"},
                {"name": "bare", "implementation_type": "uint8"},
            ],
        )
        elements = yaml.safe_load((tmp_path / "interfaces" / "ISig.yaml").read_text(encoding="utf-8"))["data_elements"]

        assert elements[0]["asil"] == "D"
        assert "asil" not in elements[1]

    def test_the_task_dump_agrees_with_the_generic_writer(self, tmp_path):
        """`task_to_dict` against `write_element`, over a fully populated task."""
        task = Task.model_validate(
            {
                "name": "T",
                "period": 0.01,
                "priority": 5,
                "partition": "PARTITION0",
                "wcet": "500us",
                "description": "d",
                "runnables": [{"swc": "Guard", "runnable": "GuardRun"}],
                "coexistence": _COEXISTENCE,
                "satisfies": ["REQ_TIMING@1"],
            }
        )
        root = tmp_path / "model"
        root.mkdir()
        write_element(str(root), "tasks/T.yaml", task)

        # As TEXT: `sort_keys=False` makes the key order the file, and a parsed
        # comparison cannot see a field that moved.
        assert (root / "tasks" / "T.yaml").read_text(encoding="utf-8") == yaml.safe_dump(
            task_to_dict(task), sort_keys=False, allow_unicode=True
        )


# ---------------------------------------------------------------------------
# The instance selector
# ---------------------------------------------------------------------------


class TestTheInstanceSelectorSurvivesTheTaskAndSequenceWriters:
    """`task_to_dict` / `sequence_to_dict` write the one selector an entry states.

    The writer guards compare the triple ``(swc, instance, runnable)`` syntactically, and an ``instance`` step's
    runnable is found on the type the path resolves to through `scheduling_identity`.

    What this does not reach: the resolved rules (an ``instance`` entry and a
    ``swc`` entry naming one occurrence) are `check-model`'s, tested in
    ``test_scheduling_identity.py`` and the validator suites -- the writer
    guards are syntactic by design.
    """

    @staticmethod
    def _model(tmp_path: Path) -> Path:
        """`Guard` placed twice under the root (`left`, `right`), one task `T`, and a second partition.

        `T` holds a `swc` entry of `Other` and an `instance` entry `left`; the second partition is where it moves."""
        create_swc(str(tmp_path), "Guard", group="body")
        add_runnable(str(tmp_path), "Guard", "GuardRun")
        create_swc(str(tmp_path), "Other", group="body")
        add_runnable(str(tmp_path), "Other", "OtherRun")
        (tmp_path / "compositions").mkdir()
        (tmp_path / "compositions" / "Root.yaml").write_text(
            "name: Root\ntitle: Root\nroot: true\nmembers:\n"
            "  - name: left\n    type: Guard\n  - name: right\n    type: Guard\n"
            "  - name: other\n    type: Other\n",
            encoding="utf-8",
            newline="\n",
        )
        (tmp_path / "hardware").mkdir()
        for name in ("P0", "P1"):
            (tmp_path / "hardware" / f"{name}.yaml").write_text(
                f"name: {name}\nkind: partition\ncore: C0\n", encoding="utf-8", newline="\n"
            )
        create_task(str(tmp_path), "T", period=0.01, priority=5, partition="P0")
        map_runnable_to_task(str(tmp_path), "T", "Other", "OtherRun")
        task_file = tmp_path / "tasks" / "T.yaml"
        document = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        document["runnables"].append({"instance": "left", "runnable": "GuardRun"})
        task_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
        return task_file

    @pytest.mark.parametrize(
        ("setter", "value"),
        [(set_task_partition, "P1"), (set_task_wcet, "500us"), (set_task_priority, 7)],
        ids=["set_task_partition", "set_task_wcet", "set_task_priority"],
    )
    def test_an_instance_entry_survives_every_task_setter_unchanged(self, tmp_path, setter, value):
        task_file = self._model(tmp_path)

        setter(str(tmp_path), "T", value)

        runnables = yaml.safe_load(task_file.read_text(encoding="utf-8"))["runnables"]
        assert runnables == [
            {"swc": "Other", "runnable": "OtherRun"},
            {"instance": "left", "runnable": "GuardRun"},
        ]

    def test_a_swc_only_task_file_round_trips_byte_identically(self, tmp_path):
        create_swc(str(tmp_path), "Guard", group="body")
        add_runnable(str(tmp_path), "Guard", "GuardRun")
        create_task(str(tmp_path), "T", period=0.01, priority=5, partition="PARTITION0")
        map_runnable_to_task(str(tmp_path), "T", "Guard", "GuardRun")
        task_file = tmp_path / "tasks" / "T.yaml"
        before = task_file.read_bytes()

        set_task_priority(str(tmp_path), "T", 5)

        assert task_file.read_bytes() == before
        assert b"instance" not in before

    def test_the_task_dump_writes_the_selector_the_entry_states_and_no_null(self):
        task = Task.model_validate(
            {
                "name": "T",
                "period": 0.01,
                "priority": 5,
                "partition": "P0",
                "runnables": [{"swc": "A", "runnable": "r"}, {"instance": "x/a", "runnable": "r"}],
            }
        )
        assert task_to_dict(task)["runnables"] == [
            {"swc": "A", "runnable": "r"},
            {"instance": "x/a", "runnable": "r"},
        ]

    def test_map_runnable_to_task_refuses_the_triple_already_mapped(self, tmp_path):
        self._model(tmp_path)
        create_task(str(tmp_path), "U", period=0.01, priority=5, partition="P0")

        with pytest.raises(ValueError, match="'Other.OtherRun' is already mapped to task 'T'"):
            map_runnable_to_task(str(tmp_path), "U", "Other", "OtherRun")

    def test_map_runnable_to_task_compares_the_triple_not_the_resolved_occurrence(self, tmp_path):
        """The syntactic guard writes another triple, and `check-model` judges the two.

        `T` maps `instance: left`; `(Guard, None, GuardRun)` is another triple."""
        task_file = self._model(tmp_path)

        map_runnable_to_task(str(tmp_path), "T", "Guard", "GuardRun")

        runnables = yaml.safe_load(task_file.read_text(encoding="utf-8"))["runnables"]
        assert runnables[-2:] == [
            {"instance": "left", "runnable": "GuardRun"},
            {"swc": "Guard", "runnable": "GuardRun"},
        ]

    def test_create_sequence_keys_an_instance_step_on_its_triple_and_writes_it(self, tmp_path):
        self._model(tmp_path)

        path = create_sequence(
            str(tmp_path),
            "S",
            "T",
            [{"swc": "Other", "runnable": "OtherRun"}, {"instance": "left", "runnable": "GuardRun", "note": "n"}],
        )

        steps = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["steps"]
        assert steps == [
            {"swc": "Other", "runnable": "OtherRun"},
            {"instance": "left", "runnable": "GuardRun", "note": "n"},
        ]

    def test_create_sequence_refuses_an_instance_step_the_task_does_not_write(self, tmp_path):
        """`right` is an occurrence of the same type, and a different triple."""
        self._model(tmp_path)

        with pytest.raises(ValueError, match="Step 'right.GuardRun' is not mapped to task 'T'"):
            create_sequence(str(tmp_path), "S", "T", [{"instance": "right", "runnable": "GuardRun"}])

    def test_create_sequence_resolves_an_instance_steps_runnable_on_its_type(self, tmp_path):
        task_file = self._model(tmp_path)
        document = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        document["runnables"] += [
            {"instance": "right", "runnable": "Missing"},
            {"instance": "nowhere", "runnable": "GuardRun"},
        ]
        task_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match="unknown runnable 'Missing' on SWC 'Guard'"):
            create_sequence(str(tmp_path), "S", "T", [{"instance": "right", "runnable": "Missing"}])
        with pytest.raises(
            ValueError,
            match="Sequence step 'nowhere.GuardRun' names instance 'nowhere', which resolves to no component",
        ):
            create_sequence(str(tmp_path), "S2", "T", [{"instance": "nowhere", "runnable": "GuardRun"}])

    def test_create_sequence_says_the_walk_is_undefined_when_no_root_resolves_an_instance_step(self, tmp_path):
        """Without a root composition no path resolves: the writer refuses the step and says why.

        ``check-model`` leaves the finding to ``composition.no-root`` (the docstring's limit)."""
        self._model(tmp_path)
        root = tmp_path / "compositions" / "Root.yaml"
        root.write_text(root.read_text(encoding="utf-8").replace("root: true\n", ""), encoding="utf-8", newline="\n")

        with pytest.raises(
            ValueError,
            match=(
                "Sequence step 'left.GuardRun' names instance 'left', which is not resolved: "
                "the composition walk is undefined"
            ),
        ):
            create_sequence(str(tmp_path), "S", "T", [{"instance": "left", "runnable": "GuardRun"}])
