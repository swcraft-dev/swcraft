"""Tests for the hardware layer writer: ``create_hardware_element``, the budget setters and the update paths.

``swc_to_dict`` / ``task_to_dict`` rewrite the whole file from the parsed object, so the budget-preservation regressions
guard against a serializer that stops emitting a budget. Pinned here:

* the kinds share **one identity namespace**, so a duplicate name is refused whichever two kinds carry it;
* a field belonging to another kind is **refused, not ignored**;
* the model-wide half of the layer's rules is deliberately **not** here: a memory naming an undeclared core in
  ``accessible`` is written and ``check-model`` reports it. A single-file writer cannot be right about a model it has
  half read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from conftest import partition_on
from pydantic import ValidationError
from swcraft_tools.auditor import AuditLog
from swcraft_tools.model_registry import production_registry
from swcraft_tools.reader import read_hardware, read_tasks
from swcraft_tools.resource_analysis import analyze_resource_budgets
from swcraft_tools.schema import Core, Memory, Partition, Processor
from swcraft_tools.validator import check_hardware_topology
from swcraft_tools.validator.hardware import _declared
from swcraft_tools.writer import (
    add_init_event,
    add_port,
    add_runnable,
    add_timing_event,
    create_hardware_element,
    create_swc,
    create_task,
    map_runnable_to_task,
    set_swc_memory_budget,
    set_task_partition,
    set_task_priority,
    set_task_wcet,
    unset_swc_memory_budget,
    update_resource,
)
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import hardware_to_dict


def _auditor(tmp_path: Path) -> AuditLog:
    return AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))


def _audit_entries(tmp_path: Path) -> list[dict]:
    log_path = tmp_path / "dist" / "audit.jsonl"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hardware_data(tmp_path: Path, name: str) -> dict:
    return yaml.safe_load((tmp_path / "hardware" / f"{name}.yaml").read_text(encoding="utf-8"))


def _create(tmp_path: Path, name: str, kind: str, **kwargs) -> str:
    """One hardware element of any kind, with that kind's required fields filled."""
    if kind == "processor":
        return create_hardware_element(str(tmp_path), name, "processor", **kwargs)
    if kind == "core":
        kwargs.setdefault("threshold", 0.7)
        return create_hardware_element(str(tmp_path), name, "core", **kwargs)
    return _memory(tmp_path, name, **kwargs)


def _memory(tmp_path: Path, name: str, **kwargs) -> str:
    """One `kind: memory`, with the four required fields defaulted."""
    kwargs.setdefault("memory_type", "RAM")
    kwargs.setdefault("size", 1024)
    kwargs.setdefault("accessible", ["CORE0"])
    kwargs.setdefault("threshold", 0.8)
    return create_hardware_element(str(tmp_path), name, "memory", **kwargs)


def _core(tmp_path: Path, name: str, *, partition: bool = True, **kwargs) -> str:
    """Return one ``kind: core`` with ``threshold`` defaulted.

    Unless *partition* is false, it carries the one unrated partition ``conftest.partition_on`` names (a task runs in a
    partition).
    """
    kwargs.setdefault("threshold", 0.7)
    result = create_hardware_element(str(tmp_path), name, "core", **kwargs)
    if partition:
        create_hardware_element(str(tmp_path), partition_on(name), "partition", core=name)
    return result


# Happy path


class TestCreateHardwareProcessor:
    def test_creates_file_and_returns_absolute_path(self, tmp_path):
        result = create_hardware_element(
            str(tmp_path),
            "CPU",
            "processor",
            description="Concept document, table 4.1",
            auditor=_auditor(tmp_path),
        )

        target = tmp_path / "hardware" / "CPU.yaml"
        assert result == str(target)
        assert Path(result).is_absolute()
        assert target.exists()

    def test_written_yaml_round_trips_through_the_reader(self, tmp_path):
        create_hardware_element(str(tmp_path), "CPU", "processor", auditor=_auditor(tmp_path))

        elements = read_hardware(str(tmp_path))
        assert len(elements) == 1
        element = elements[0]
        assert isinstance(element, Processor)
        assert element.name == "CPU"
        assert element.kind == "processor"
        assert element.description == ""

    def test_no_budget_field_is_emitted(self, tmp_path):
        """A processor has no threshold or enforcement: deleting one weakens no check only if its file has no budget."""
        create_hardware_element(str(tmp_path), "CPU", "processor")

        assert _hardware_data(tmp_path, "CPU") == {"name": "CPU", "kind": "processor"}

    def test_audit_entry_is_appended(self, tmp_path):
        create_hardware_element(str(tmp_path), "CPU", "processor", auditor=_auditor(tmp_path))

        entries = _audit_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0]["operation"] == "create_hardware_element"
        assert entries[0]["args"] == {"name": "CPU", "kind": "processor"}
        assert entries[0]["result"].endswith("CPU.yaml")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("threshold", 0.7),
            ("hw_id", 0),
            ("memory_type", "RAM"),
            ("size", 1024),
            ("accessible", ["CORE0"]),
            # The ``enforcement`` argument defaults to ``None`` (no truthy default), so ``_refuse_foreign_fields``'
            # ``value is None`` skip reaches it and a processor never drops the argument in silence.
            ("enforcement", "advisory"),
        ],
    )
    def test_every_field_a_processor_does_not_carry_is_refused(self, tmp_path, field, value):
        with pytest.raises(ValueError, match="is not valid for a processor"):
            create_hardware_element(str(tmp_path), "CPU", "processor", **{field: value})
        assert not (tmp_path / "hardware" / "CPU.yaml").exists()


class TestCreateHardwarePartition:
    """The fourth kind through the writer."""

    def test_writes_a_partition_with_its_fields_in_declaration_order(self, tmp_path):
        result = create_hardware_element(
            str(tmp_path),
            "P_Brake",
            "partition",
            core="CORE1",
            asil="B",
            coexistence="Separate memory regions.",
            description="The brake functions.",
            auditor=_auditor(tmp_path),
        )

        assert result == str(tmp_path / "hardware" / "P_Brake.yaml")
        assert (tmp_path / "hardware" / "P_Brake.yaml").read_text(encoding="utf-8") == (
            "name: P_Brake\nkind: partition\ncore: CORE1\nasil: B\ncoexistence: Separate memory regions.\n"
            "description: The brake functions.\n"
        )
        (element,) = read_hardware(str(tmp_path))
        assert isinstance(element, Partition)
        assert (element.core, element.asil, element.coexistence) == ("CORE1", "B", "Separate memory regions.")

    def test_an_unrated_unargued_partition_omits_both_fields(self, tmp_path):
        create_hardware_element(str(tmp_path), "PARTITION0", "partition", core="CORE0")

        assert _hardware_data(tmp_path, "PARTITION0") == {"name": "PARTITION0", "kind": "partition", "core": "CORE0"}

    def test_the_audit_entry_names_the_core_and_the_level(self, tmp_path):
        create_hardware_element(str(tmp_path), "P0", "partition", core="CORE0", asil="D", auditor=_auditor(tmp_path))

        entry = _audit_entries(tmp_path)[-1]
        assert entry["operation"] == "create_hardware_element"
        assert entry["args"] == {"name": "P0", "kind": "partition", "core": "CORE0", "asil": "D"}

    def test_core_is_required(self, tmp_path):
        with pytest.raises(ValueError, match="'core' is required for a partition"):
            create_hardware_element(str(tmp_path), "P0", "partition")
        assert not (tmp_path / "hardware").exists() or not list((tmp_path / "hardware").iterdir())

    @pytest.mark.parametrize(
        ("field", "value"),
        [("threshold", 0.7), ("enforcement", "strict"), ("hw_id", 0), ("size", 1024), ("accessible", ["CORE0"])],
    )
    def test_a_budget_field_is_refused_on_a_partition(self, tmp_path, field, value):
        with pytest.raises(ValueError, match=f"'{field}' is not valid for a partition"):
            create_hardware_element(str(tmp_path), "P0", "partition", core="CORE0", **{field: value})

    @pytest.mark.parametrize("kind", ["processor", "core", "memory"])
    @pytest.mark.parametrize(
        ("field", "value"), [("core", "CORE0"), ("asil", "B"), ("trust", "trusted"), ("coexistence", "text")]
    )
    def test_a_partition_field_is_refused_on_another_kind(self, tmp_path, kind, field, value):
        with pytest.raises(ValueError, match=f"'{field}' is not valid for a {kind} \\(it is a partition field\\)"):
            _create(tmp_path, "X", kind, **{field: value})

    def test_an_illegal_level_is_refused(self, tmp_path):
        with pytest.raises(ValidationError):
            create_hardware_element(str(tmp_path), "P0", "partition", core="CORE0", asil="E")

    def test_trust_is_written_between_the_level_and_the_argument_and_audited(self, tmp_path):
        """``trust``: the keyword, the dump and the audit argument."""
        create_hardware_element(
            str(tmp_path),
            "P_Brake",
            "partition",
            core="CORE1",
            asil="D",
            trust="trusted",
            coexistence="Separate memory regions.",
            auditor=_auditor(tmp_path),
        )

        assert (tmp_path / "hardware" / "P_Brake.yaml").read_text(encoding="utf-8") == (
            "name: P_Brake\nkind: partition\ncore: CORE1\nasil: D\ntrust: trusted\n"
            "coexistence: Separate memory regions.\n"
        )
        (element,) = read_hardware(str(tmp_path))
        assert isinstance(element, Partition)
        assert element.trust == "trusted"
        assert _audit_entries(tmp_path)[-1]["args"] == {
            "name": "P_Brake",
            "kind": "partition",
            "core": "CORE1",
            "asil": "D",
            "trust": "trusted",
            "coexistence": "Separate memory regions.",
        }

    def test_an_undeclared_trust_is_neither_written_nor_audited(self, tmp_path):
        create_hardware_element(str(tmp_path), "P0", "partition", core="CORE0", auditor=_auditor(tmp_path))

        assert "trust" not in _hardware_data(tmp_path, "P0")
        assert "trust" not in _audit_entries(tmp_path)[-1]["args"]

    def test_the_default_class_passed_explicitly_is_still_left_out_of_the_file(self, tmp_path):
        """`non_trusted` is the default the contract omits, however it arrived."""
        create_hardware_element(str(tmp_path), "P0", "partition", core="CORE0", trust="non_trusted")

        assert _hardware_data(tmp_path, "P0") == {"name": "P0", "kind": "partition", "core": "CORE0"}

    def test_a_partition_file_declaring_no_trust_round_trips_byte_identically(self, tmp_path):
        """A partition file declaring no trust comes back with the same bytes, through both writers."""
        text = "name: P_Body\nkind: partition\ncore: CORE0\nasil: B\ncoexistence: MPU regions.\n"
        hardware = tmp_path / "hardware"
        hardware.mkdir()
        (hardware / "P_Body.yaml").write_text(text, encoding="utf-8", newline="\n")
        (element,) = read_hardware(str(tmp_path))

        dumped = yaml.safe_dump(hardware_to_dict(element), default_flow_style=False, sort_keys=False)
        derived = yaml.safe_dump(element_to_dict(element), default_flow_style=False, sort_keys=False)
        assert dumped == derived == text

        update_resource(str(tmp_path), "P_Body", description="")
        assert (hardware / "P_Body.yaml").read_text(encoding="utf-8") == text

    def test_an_illegal_trust_is_refused_and_nothing_is_written(self, tmp_path):
        with pytest.raises(ValidationError):
            create_hardware_element(str(tmp_path), "P0", "partition", core="CORE0", trust="untrusted")
        assert not (tmp_path / "hardware" / "P0.yaml").exists()

    def test_update_resource_keeps_a_declared_trust(self, tmp_path):
        create_hardware_element(str(tmp_path), "P0", "partition", core="CORE0", trust="trusted")

        update_resource(str(tmp_path), "P0", description="Now described.")
        assert _hardware_data(tmp_path, "P0")["trust"] == "trusted"

    def test_update_resource_refuses_a_budget_on_a_partition(self, tmp_path):
        create_hardware_element(str(tmp_path), "P0", "partition", core="CORE0")

        with pytest.raises(
            ValueError, match=r"'threshold' is not valid for partition 'P0' \(a partition carries no budget\)"
        ):
            update_resource(str(tmp_path), "P0", threshold=0.5)
        update_resource(str(tmp_path), "P0", description="Now described.")
        assert _hardware_data(tmp_path, "P0") == {
            "name": "P0",
            "kind": "partition",
            "core": "CORE0",
            "description": "Now described.",
        }


class TestCreateHardwareCore:
    def test_creates_file_and_returns_absolute_path(self, tmp_path):
        result = create_hardware_element(
            str(tmp_path),
            "CORE2",
            "core",
            threshold=0.7,
            description="Concept document, table 4.2",
            auditor=_auditor(tmp_path),
        )

        target = tmp_path / "hardware" / "CORE2.yaml"
        assert result == str(target)
        assert Path(result).is_absolute()
        assert target.exists()

    def test_written_yaml_round_trips_through_the_reader(self, tmp_path):
        create_hardware_element(str(tmp_path), "CORE2", "core", threshold=0.7, auditor=_auditor(tmp_path))

        elements = read_hardware(str(tmp_path))
        assert len(elements) == 1
        element = elements[0]
        assert isinstance(element, Core)
        assert element.name == "CORE2"
        assert element.kind == "core"
        assert element.threshold == 0.7
        assert element.enforcement == "strict"
        assert element.hw_id is None

    def test_advisory_enforcement_is_persisted(self, tmp_path):
        create_hardware_element(
            str(tmp_path),
            "CORE0",
            "core",
            threshold=0.9,
            enforcement="advisory",
            auditor=_auditor(tmp_path),
        )
        assert read_hardware(str(tmp_path))[0].enforcement == "advisory"

    def test_hw_id_is_persisted_when_supplied_and_omitted_otherwise(self, tmp_path):
        """``hw_id`` is validated by nothing, so the one possible defect is a ``hw_id: null`` line in every core."""
        create_hardware_element(str(tmp_path), "CORE0", "core", threshold=0.7, hw_id=4)
        create_hardware_element(str(tmp_path), "CORE1", "core", threshold=0.7)

        assert _hardware_data(tmp_path, "CORE0")["hw_id"] == 4
        assert "hw_id" not in _hardware_data(tmp_path, "CORE1")
        assert read_hardware(str(tmp_path))[0].hw_id == 4

    def test_a_float_hw_id_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="must be an int"):
            create_hardware_element(str(tmp_path), "CORE0", "core", threshold=0.7, hw_id=4.0)
        assert not (tmp_path / "hardware" / "CORE0.yaml").exists()

    def test_audit_entry_is_appended(self, tmp_path):
        create_hardware_element(str(tmp_path), "CORE2", "core", threshold=0.7, auditor=_auditor(tmp_path))

        entries = _audit_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0]["operation"] == "create_hardware_element"
        assert entries[0]["args"] == {"name": "CORE2", "kind": "core", "threshold": 0.7}
        assert entries[0]["result"].endswith("CORE2.yaml")

    def test_missing_threshold_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="'threshold' is required for a core"):
            create_hardware_element(str(tmp_path), "CORE2", "core", auditor=_auditor(tmp_path))
        assert not (tmp_path / "hardware" / "CORE2.yaml").exists()

    def test_out_of_range_threshold_is_rejected_by_pydantic(self, tmp_path):
        with pytest.raises(ValidationError):
            create_hardware_element(str(tmp_path), "CORE2", "core", threshold=1.5, auditor=_auditor(tmp_path))

    @pytest.mark.parametrize(
        ("field", "value"),
        [("memory_type", "RAM"), ("size", 1024), ("accessible", ["CORE0"])],
    )
    def test_every_memory_field_is_refused_on_a_core(self, tmp_path, field, value):
        with pytest.raises(ValueError, match="is not valid for a core"):
            create_hardware_element(str(tmp_path), "CORE2", "core", threshold=0.7, **{field: value})
        assert not (tmp_path / "hardware" / "CORE2.yaml").exists()


class TestCreateHardwareMemory:
    def test_creates_file_and_returns_absolute_path(self, tmp_path):
        result = create_hardware_element(
            str(tmp_path),
            "RAM0",
            "memory",
            memory_type="RAM",
            size=262144,
            accessible=["CORE0"],
            threshold=0.8,
            auditor=_auditor(tmp_path),
        )

        target = tmp_path / "hardware" / "RAM0.yaml"
        assert result == str(target)
        assert target.exists()

    def test_written_yaml_round_trips_through_the_reader(self, tmp_path):
        _memory(tmp_path, "RAM0", size=262144, auditor=_auditor(tmp_path))

        element = read_hardware(str(tmp_path))[0]
        assert isinstance(element, Memory)
        assert element.name == "RAM0"
        assert element.kind == "memory"
        assert element.type == "RAM"
        assert element.size == 262144
        assert element.accessible == ["CORE0"]
        assert element.threshold == 0.8
        assert element.enforcement == "strict"

    @pytest.mark.parametrize("memory_type", ["RAM", "ROM", "EEPROM", "Flash"])
    def test_all_four_declared_types_are_accepted(self, tmp_path, memory_type):
        _memory(tmp_path, "M", memory_type=memory_type)

        assert read_hardware(str(tmp_path))[0].type == memory_type

    def test_an_undeclared_type_is_rejected_by_pydantic(self, tmp_path):
        with pytest.raises(ValidationError):
            _memory(tmp_path, "M", memory_type="MRAM")

    def test_audit_entry_is_appended(self, tmp_path):
        _memory(tmp_path, "RAM0", size=262144, auditor=_auditor(tmp_path))

        entries = _audit_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0]["operation"] == "create_hardware_element"
        assert entries[0]["args"] == {
            "name": "RAM0",
            "kind": "memory",
            "threshold": 0.8,
            "type": "RAM",
            "size": 262144,
            "accessible": ["CORE0"],
        }

    @pytest.mark.parametrize("field", ["memory_type", "size", "accessible", "threshold"])
    def test_every_required_field_is_reported_by_name(self, tmp_path, field):
        kwargs = {"memory_type": "RAM", "size": 1024, "accessible": ["CORE0"], "threshold": 0.8}
        del kwargs[field]

        with pytest.raises(ValueError, match=f"'{field}' is required for a memory"):
            create_hardware_element(str(tmp_path), "RAM0", "memory", **kwargs)
        assert not (tmp_path / "hardware" / "RAM0.yaml").exists()

    def test_zero_size_is_rejected_by_pydantic(self, tmp_path):
        with pytest.raises(ValidationError):
            _memory(tmp_path, "RAM0", size=0)

    def test_an_empty_accessible_list_is_rejected_by_pydantic(self, tmp_path):
        """A memory no core may use has no meaning: ``schema.Memory.accessible_must_name_distinct_cores`` refuses it."""
        with pytest.raises(ValidationError):
            _memory(tmp_path, "RAM0", accessible=[])

    def test_a_repeated_core_in_accessible_is_rejected_by_pydantic(self, tmp_path):
        with pytest.raises(ValidationError):
            _memory(tmp_path, "RAM0", accessible=["CORE0", "CORE0"])

    def test_hw_id_is_refused_on_a_memory(self, tmp_path):
        with pytest.raises(ValueError, match="is not valid for a memory"):
            _memory(tmp_path, "RAM0", hw_id=1)
        assert not (tmp_path / "hardware" / "RAM0.yaml").exists()

    def test_an_undeclared_accessible_core_is_not_this_functions_business(self, tmp_path):
        """The single-file writer accepts it; ``check-model``'s ``hardware.accessible-undeclared-core`` refuses it."""
        _memory(tmp_path, "RAM0", accessible=["CORE_NOT_DECLARED"])

        assert read_hardware(str(tmp_path))[0].accessible == ["CORE_NOT_DECLARED"]


# Guards


class TestCreateHardwareElementGuards:
    def test_empty_name_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="name must not be empty"):
            _memory(tmp_path, "", auditor=_auditor(tmp_path))

    def test_whitespace_only_name_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="name must not be empty"):
            _memory(tmp_path, "   ", auditor=_auditor(tmp_path))

    @pytest.mark.parametrize("bad_name", ["../escape", "sub/Core", "sub\\Core", ".."])
    def test_path_traversal_name_is_rejected(self, tmp_path, bad_name):
        with pytest.raises(ValueError, match="must not contain path separators"):
            _memory(tmp_path, bad_name, auditor=_auditor(tmp_path))
        assert not (tmp_path / "hardware").exists()

    def test_duplicate_name_is_rejected(self, tmp_path):
        _memory(tmp_path, "RAM0", size=1024)
        with pytest.raises(ValueError, match="already exists"):
            _memory(tmp_path, "RAM0", size=2048)
        assert read_hardware(str(tmp_path))[0].size == 1024

    @pytest.mark.parametrize(
        ("first", "second"),
        [("processor", "core"), ("core", "memory"), ("memory", "processor")],
    )
    def test_duplicate_name_across_kinds_is_rejected(self, tmp_path, first, second):
        """One namespace spans the kinds: it makes placement references and ``SWComponent.memory`` keys resolvable."""
        _create(tmp_path, "Shared", first)
        with pytest.raises(ValueError, match="already exists"):
            _create(tmp_path, "Shared", second)

    def test_existing_file_with_different_content_name_is_kept_and_the_new_element_takes_a_suffix(self, tmp_path):
        # A file whose YAML ``name`` differs from its file name is never overwritten and does not refuse the create: the
        # new file takes the nearest free collision suffix, with exactly the identity asked for.
        hardware_dir = tmp_path / "hardware"
        hardware_dir.mkdir(parents=True)
        occupant = yaml.safe_dump({"name": "Other", "kind": "processor"})
        (hardware_dir / "RAM0.yaml").write_text(occupant, encoding="utf-8")

        result = _memory(tmp_path, "RAM0", auditor=_auditor(tmp_path))

        assert Path(result).name == "RAM0~1.yaml"
        assert yaml.safe_load(Path(result).read_text(encoding="utf-8"))["name"] == "RAM0"
        assert (hardware_dir / "RAM0.yaml").read_text(encoding="utf-8") == occupant

    def test_unknown_kind_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown hardware kind"):
            create_hardware_element(str(tmp_path), "Flash", "storage", threshold=0.8, auditor=_auditor(tmp_path))
        assert not (tmp_path / "hardware" / "Flash.yaml").exists()

    def test_invalid_enforcement_is_rejected_by_pydantic(self, tmp_path):
        with pytest.raises(ValidationError):
            _memory(tmp_path, "RAM0", enforcement="mandatory", auditor=_auditor(tmp_path))

    def test_no_audit_entry_on_a_rejected_create(self, tmp_path):
        with pytest.raises(ValueError):
            create_hardware_element(str(tmp_path), "Flash", "storage", threshold=0.8, auditor=_auditor(tmp_path))
        assert _audit_entries(tmp_path) == []

    def test_works_without_an_auditor(self, tmp_path):
        assert Path(_memory(tmp_path, "RAM0")).exists()


# Budget setters


def _task_data(tmp_path: Path, name: str = "Task_10ms") -> dict:
    return yaml.safe_load((tmp_path / "tasks" / f"{name}.yaml").read_text(encoding="utf-8"))


def _swc_data(tmp_path: Path, group: str = "body", name: str = "MySwc") -> dict:
    return yaml.safe_load((tmp_path / "swc" / group / f"{name}.yaml").read_text(encoding="utf-8"))


class TestSetTaskWcet:
    def test_updates_an_existing_task_file(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        result = set_task_wcet(str(tmp_path), "Task_10ms", "1500us", auditor=_auditor(tmp_path))

        assert result == str(tmp_path / "tasks" / "Task_10ms.yaml")
        assert _task_data(tmp_path)["wcet"] == "1500us"
        assert read_tasks(str(tmp_path))[0].wcet == "1500us"

    def test_overwrites_a_previous_value(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, wcet="400us", partition="PARTITION0")

        set_task_wcet(str(tmp_path), "Task_10ms", "900us", auditor=_auditor(tmp_path))

        assert read_tasks(str(tmp_path))[0].wcet == "900us"

    def test_preserves_every_other_task_field(self, tmp_path):
        create_swc(str(tmp_path), "MySwc", "body")
        add_runnable(str(tmp_path), "MySwc", "Run")
        create_task(
            str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION2", description="Engine loop"
        )
        map_runnable_to_task(str(tmp_path), "Task_10ms", "MySwc", "Run")

        set_task_wcet(str(tmp_path), "Task_10ms", "400us", auditor=_auditor(tmp_path))

        task = read_tasks(str(tmp_path))[0]
        assert (task.period, task.priority, task.partition, task.description) == (
            0.01,
            5,
            "PARTITION2",
            "Engine loop",
        )
        assert [(r.swc, r.runnable) for r in task.runnables] == [("MySwc", "Run")]

    def test_audit_entry_uses_the_writer_function_name(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        set_task_wcet(str(tmp_path), "Task_10ms", "400us", auditor=_auditor(tmp_path))

        entry = _audit_entries(tmp_path)[-1]
        assert entry["operation"] == "set_task_wcet"
        assert entry["args"] == {"task_name": "Task_10ms", "wcet": "400us"}
        assert entry["result"].endswith("Task_10ms.yaml")

    def test_unknown_task_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            set_task_wcet(str(tmp_path), "Nope", "400us", auditor=_auditor(tmp_path))
        assert _audit_entries(tmp_path) == []

    @pytest.mark.parametrize("bad", ["0us", "0ns"])
    def test_non_positive_value_is_rejected(self, tmp_path, bad):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        with pytest.raises(ValueError, match="wcet must be > 0"):
            set_task_wcet(str(tmp_path), "Task_10ms", bad, auditor=_auditor(tmp_path))
        assert "wcet" not in _task_data(tmp_path)

    @pytest.mark.parametrize(
        ("bad", "sentence"),
        [
            (400.5, "wcet 400.5 is not a budget: write a whole number with one of ns, us, ms, s, e.g. '1500us'"),
            (400, "wcet 400 names no unit: write it with one of ns, us, ms, s, e.g. '400us'"),
            ("-1us", "wcet '-1us' is not a whole number followed by one of ns, us, ms, s"),
        ],
    )
    def test_a_value_that_is_not_a_budget_is_rejected(self, tmp_path, bad, sentence):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        with pytest.raises(ValueError, match=re.escape(sentence)):
            set_task_wcet(str(tmp_path), "Task_10ms", bad, auditor=_auditor(tmp_path))
        assert "wcet" not in _task_data(tmp_path)

    def test_works_without_an_auditor(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        assert set_task_wcet(str(tmp_path), "Task_10ms", "400us").endswith("Task_10ms.yaml")


class TestCreateTaskWithWcet:
    def test_wcet_is_persisted_when_supplied(self, tmp_path):
        create_task(str(tmp_path), "Task_1ms", period=0.001, priority=30, partition="PARTITION2", wcet="400us")

        assert _task_data(tmp_path, "Task_1ms")["wcet"] == "400us"
        assert read_tasks(str(tmp_path))[0].wcet == "400us"

    def test_omitted_wcet_is_not_emitted_at_all(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        assert "wcet" not in _task_data(tmp_path)
        assert read_tasks(str(tmp_path))[0].wcet is None

    def test_wcet_is_audited_only_when_supplied(self, tmp_path):
        create_task(
            str(tmp_path),
            "Task_1ms",
            period=0.001,
            priority=30,
            wcet="400us",
            auditor=_auditor(tmp_path),
            partition="PARTITION0",
        )
        create_task(
            str(tmp_path), "Task_10ms", period=0.01, priority=5, auditor=_auditor(tmp_path), partition="PARTITION0"
        )

        entries = _audit_entries(tmp_path)
        assert entries[0]["args"]["wcet"] == "400us"
        assert "wcet" not in entries[1]["args"]

    def test_non_positive_wcet_is_rejected_by_pydantic(self, tmp_path):
        with pytest.raises(ValidationError):
            create_task(str(tmp_path), "Task_1ms", period=0.001, priority=30, wcet="0us", partition="PARTITION0")
        assert not (tmp_path / "tasks" / "Task_1ms.yaml").exists()


class TestCreateSwcWithMemory:
    """A conformant SWC in ONE call.

    Under a ``strict`` memory pool an SWC created without a budget is a ``missing_budget`` error, so a scripted flow
    with a fixed tool sequence could not otherwise get there. ``memory`` mirrors ``create_task(..., wcet=...)``,
    declared after ``auditor`` for the same positional-call reason.
    """

    def _pools(self, tmp_path) -> None:
        _memory(tmp_path, "RAM", size=262144)
        _memory(tmp_path, "ROM", memory_type="ROM", size=1048576, threshold=0.9)

    def test_memory_is_persisted_when_supplied(self, tmp_path):
        self._pools(tmp_path)

        create_swc(str(tmp_path), "MySwc", "body", memory={"RAM": 2048, "ROM": 8192})

        assert _swc_data(tmp_path)["memory"] == {"RAM": 2048, "ROM": 8192}

    def test_omitted_memory_is_not_emitted_at_all(self, tmp_path):
        self._pools(tmp_path)

        create_swc(str(tmp_path), "MySwc", "body")

        assert "memory" not in _swc_data(tmp_path)

    def test_empty_memory_map_is_not_emitted_either(self, tmp_path):
        self._pools(tmp_path)

        create_swc(str(tmp_path), "MySwc", "body", memory={})

        assert "memory" not in _swc_data(tmp_path)

    def test_undeclared_pool_is_rejected_and_nothing_is_written(self, tmp_path):
        self._pools(tmp_path)

        with pytest.raises(ValueError, match="not declared as a 'kind: memory' element"):
            create_swc(str(tmp_path), "MySwc", "body", memory={"FLASH": 1024})

        assert not (tmp_path / "swc" / "body" / "MySwc.yaml").exists()

    def test_a_core_is_not_a_memory(self, tmp_path):
        _core(tmp_path, "CORE0")

        with pytest.raises(ValueError, match="not declared as a 'kind: memory' element"):
            create_swc(str(tmp_path), "MySwc", "body", memory={"CORE0": 1024})

    def test_negative_budget_is_rejected_by_pydantic(self, tmp_path):
        self._pools(tmp_path)

        with pytest.raises(ValidationError):
            create_swc(str(tmp_path), "MySwc", "body", memory={"RAM": -1})
        assert not (tmp_path / "swc" / "body" / "MySwc.yaml").exists()

    def test_non_int_budget_is_rejected(self, tmp_path):
        self._pools(tmp_path)

        with pytest.raises(ValueError, match="must be an int"):
            create_swc(str(tmp_path), "MySwc", "body", memory={"RAM": 1024.5})
        assert not (tmp_path / "swc" / "body" / "MySwc.yaml").exists()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_pool_name_reports_the_blank_name_not_the_lookup(self, tmp_path, blank):
        # Both entry points share ``_require_declared_memory_pools``, whose blank-name check runs first: otherwise a
        # blank key reports the far less useful "is not declared" message.
        self._pools(tmp_path)
        create_swc(str(tmp_path), "Existing", "body")

        with pytest.raises(ValueError, match="pool must not be empty"):
            create_swc(str(tmp_path), "MySwc", "body", memory={blank: 1024})
        assert not (tmp_path / "swc" / "body" / "MySwc.yaml").exists()

        with pytest.raises(ValueError, match="pool must not be empty"):
            set_swc_memory_budget(str(tmp_path), "Existing", blank, 1024)
        assert "memory" not in _swc_data(tmp_path, name="Existing")

    def test_memory_is_audited_only_when_supplied(self, tmp_path):
        self._pools(tmp_path)

        create_swc(str(tmp_path), "WithBudget", "body", memory={"RAM": 2048}, auditor=_auditor(tmp_path))
        create_swc(str(tmp_path), "WithoutBudget", "body", auditor=_auditor(tmp_path))

        entries = _audit_entries(tmp_path)
        assert entries[0]["args"]["memory"] == {"RAM": 2048}
        assert "memory" not in entries[1]["args"]

    def test_the_swc_reads_back_conformant_under_a_strict_memory(self, tmp_path):
        self._pools(tmp_path)

        create_swc(str(tmp_path), "MySwc", "body", memory={"RAM": 2048, "ROM": 8192})

        # Both directions: the write went through the declared-memory guard, and the analysis finds the budget within
        # the limit it computes for each memory.
        declared = {e.name: e for e in read_hardware(str(tmp_path))}
        assert _swc_data(tmp_path)["memory"] == {"RAM": 2048, "ROM": 8192}
        assert declared["RAM"].size * declared["RAM"].threshold > 2048
        assert declared["ROM"].size * declared["ROM"].threshold > 8192

        usages = {u.name: u for u in analyze_resource_budgets(str(tmp_path)).resources}
        assert usages["RAM"].findings == []
        assert usages["ROM"].findings == []
        assert usages["RAM"].consumption == 2048


class TestSetSwcMemoryBudget:
    def _project(self, tmp_path) -> None:
        _memory(tmp_path, "RAM", size=262144)
        _memory(tmp_path, "ROM", memory_type="ROM", size=1048576, threshold=0.9)
        create_swc(str(tmp_path), "MySwc", "body")

    def test_writes_the_pool_entry(self, tmp_path):
        self._project(tmp_path)

        result = set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 2048, auditor=_auditor(tmp_path))

        assert result == str(tmp_path / "swc" / "body" / "MySwc.yaml")
        assert _swc_data(tmp_path)["memory"] == {"RAM": 2048}

    def test_setting_one_pool_preserves_the_others(self, tmp_path):
        self._project(tmp_path)

        set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 2048, auditor=_auditor(tmp_path))
        set_swc_memory_budget(str(tmp_path), "MySwc", "ROM", 8192, auditor=_auditor(tmp_path))
        set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 4096, auditor=_auditor(tmp_path))

        assert _swc_data(tmp_path)["memory"] == {"RAM": 4096, "ROM": 8192}

    def test_zero_bytes_is_accepted(self, tmp_path):
        self._project(tmp_path)

        set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 0, auditor=_auditor(tmp_path))

        assert _swc_data(tmp_path)["memory"] == {"RAM": 0}

    def test_preserves_ports_and_runnables(self, tmp_path):
        self._project(tmp_path)
        add_runnable(str(tmp_path), "MySwc", "Run")
        add_port(str(tmp_path), "MySwc", "pOut", "provide", "ISpeed")

        set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 2048, auditor=_auditor(tmp_path))

        data = _swc_data(tmp_path)
        assert [p["name"] for p in data["ports"]] == ["pOut"]
        assert [r["name"] for r in data["runnables"]] == ["Run"]

    def test_audit_entry_uses_the_writer_function_name(self, tmp_path):
        self._project(tmp_path)

        set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 2048, auditor=_auditor(tmp_path))

        entry = _audit_entries(tmp_path)[-1]
        assert entry["operation"] == "set_swc_memory_budget"
        assert entry["args"] == {"swc_name": "MySwc", "pool": "RAM", "size_bytes": 2048}
        assert entry["result"].endswith("MySwc.yaml")

    def test_unknown_swc_is_rejected(self, tmp_path):
        self._project(tmp_path)

        with pytest.raises(ValueError, match="SWC 'Nope' not found"):
            set_swc_memory_budget(str(tmp_path), "Nope", "RAM", 2048, auditor=_auditor(tmp_path))

    def test_undeclared_pool_is_rejected(self, tmp_path):
        self._project(tmp_path)

        with pytest.raises(ValueError, match="not declared as a 'kind: memory' element"):
            set_swc_memory_budget(str(tmp_path), "MySwc", "Flash", 2048, auditor=_auditor(tmp_path))
        assert "memory" not in _swc_data(tmp_path)

    def test_a_core_is_not_a_valid_pool(self, tmp_path):
        self._project(tmp_path)
        _core(tmp_path, "CORE0")

        with pytest.raises(ValueError, match="not declared as a 'kind: memory' element"):
            set_swc_memory_budget(str(tmp_path), "MySwc", "CORE0", 2048, auditor=_auditor(tmp_path))

    def test_pool_is_rejected_when_no_memory_is_declared_at_all(self, tmp_path):
        create_swc(str(tmp_path), "MySwc", "body")

        with pytest.raises(ValueError, match="not declared as a 'kind: memory' element"):
            set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 2048, auditor=_auditor(tmp_path))

    def test_negative_size_is_rejected(self, tmp_path):
        self._project(tmp_path)

        # The rule lives in ``SWComponent.memory_budgets_must_be_named_and_non_negative`` only: the writer re-validates
        # through the model.
        with pytest.raises(ValueError, match="must be >= 0"):
            set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", -1, auditor=_auditor(tmp_path))

    def test_float_size_is_rejected(self, tmp_path):
        self._project(tmp_path)

        with pytest.raises(ValueError, match="must be an int"):
            set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 2048.5, auditor=_auditor(tmp_path))

    def test_empty_pool_name_is_rejected(self, tmp_path):
        self._project(tmp_path)

        with pytest.raises(ValueError, match="pool must not be empty"):
            set_swc_memory_budget(str(tmp_path), "MySwc", "   ", 2048, auditor=_auditor(tmp_path))

    def test_works_without_an_auditor(self, tmp_path):
        self._project(tmp_path)
        assert set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 2048).endswith("MySwc.yaml")


class TestUnsetSwcMemoryBudget:
    """The inverse of ``set_swc_memory_budget``.

    Without it a budget recorded against a since-deleted pool could not be removed by any tool, stranding the model in a
    failing ``validate``.
    """

    def _project(self, tmp_path) -> None:
        _memory(tmp_path, "RAM", size=262144)
        _memory(tmp_path, "ROM", memory_type="ROM", size=1048576, threshold=0.9)
        create_swc(str(tmp_path), "MySwc", "body")
        set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 2048)
        set_swc_memory_budget(str(tmp_path), "MySwc", "ROM", 8192)

    def test_removes_one_pool_and_preserves_the_others(self, tmp_path):
        self._project(tmp_path)

        result = unset_swc_memory_budget(str(tmp_path), "MySwc", "RAM", auditor=_auditor(tmp_path))

        assert result == str(tmp_path / "swc" / "body" / "MySwc.yaml")
        assert _swc_data(tmp_path)["memory"] == {"ROM": 8192}

    def test_removing_the_last_pool_drops_the_memory_key_entirely(self, tmp_path):
        self._project(tmp_path)

        unset_swc_memory_budget(str(tmp_path), "MySwc", "RAM")
        unset_swc_memory_budget(str(tmp_path), "MySwc", "ROM")

        assert "memory" not in _swc_data(tmp_path)

    def test_preserves_ports_and_runnables(self, tmp_path):
        self._project(tmp_path)
        add_runnable(str(tmp_path), "MySwc", "Run")
        add_port(str(tmp_path), "MySwc", "pOut", "provide", "ISpeed")

        unset_swc_memory_budget(str(tmp_path), "MySwc", "RAM", auditor=_auditor(tmp_path))

        data = _swc_data(tmp_path)
        assert [p["name"] for p in data["ports"]] == ["pOut"]
        assert [r["name"] for r in data["runnables"]] == ["Run"]

    def test_works_after_the_pool_resource_was_deleted(self, tmp_path):
        # The pool need not still be declared, or the cleanup path for a deleted resource would not exist.
        self._project(tmp_path)
        (tmp_path / "hardware" / "RAM.yaml").unlink()

        unset_swc_memory_budget(str(tmp_path), "MySwc", "RAM", auditor=_auditor(tmp_path))

        assert _swc_data(tmp_path)["memory"] == {"ROM": 8192}

    def test_audit_entry_uses_the_writer_function_name(self, tmp_path):
        self._project(tmp_path)

        unset_swc_memory_budget(str(tmp_path), "MySwc", "RAM", auditor=_auditor(tmp_path))

        entry = _audit_entries(tmp_path)[-1]
        assert entry["operation"] == "unset_swc_memory_budget"
        assert entry["args"] == {"swc_name": "MySwc", "pool": "RAM"}
        assert entry["result"].endswith("MySwc.yaml")

    def test_unknown_swc_is_rejected(self, tmp_path):
        self._project(tmp_path)

        with pytest.raises(ValueError, match="SWC 'Nope' not found"):
            unset_swc_memory_budget(str(tmp_path), "Nope", "RAM", auditor=_auditor(tmp_path))

    def test_pool_the_swc_does_not_budget_is_rejected(self, tmp_path):
        # Not a silent no-op: a typo'd pool name must not read as success.
        self._project(tmp_path)

        with pytest.raises(ValueError, match="declares no memory budget for pool 'Flash'"):
            unset_swc_memory_budget(str(tmp_path), "MySwc", "Flash", auditor=_auditor(tmp_path))
        assert _swc_data(tmp_path)["memory"] == {"RAM": 2048, "ROM": 8192}

    def test_empty_pool_name_is_rejected(self, tmp_path):
        self._project(tmp_path)

        with pytest.raises(ValueError, match="pool must not be empty"):
            unset_swc_memory_budget(str(tmp_path), "MySwc", "   ", auditor=_auditor(tmp_path))

    def test_no_audit_entry_on_a_rejected_unset(self, tmp_path):
        self._project(tmp_path)

        with pytest.raises(ValueError):
            unset_swc_memory_budget(str(tmp_path), "MySwc", "Flash", auditor=_auditor(tmp_path))

        assert [e for e in _audit_entries(tmp_path) if e["operation"] == "unset_swc_memory_budget"] == []

    def test_works_without_an_auditor(self, tmp_path):
        self._project(tmp_path)
        assert unset_swc_memory_budget(str(tmp_path), "MySwc", "RAM").endswith("MySwc.yaml")


# Budget-preservation regressions: every mutation through ``swc_to_dict`` / ``task_to_dict`` rewrites the whole file, so
# a serializer that did not emit a budget would erase it. One test per calling mutation.


class TestBudgetSurvivesUnrelatedMutations:
    def _swc_with_budget(self, tmp_path) -> None:
        _memory(tmp_path, "RAM", size=262144)
        _memory(tmp_path, "ROM", memory_type="ROM", size=1048576, threshold=0.9)
        create_swc(str(tmp_path), "MySwc", "body")
        set_swc_memory_budget(str(tmp_path), "MySwc", "RAM", 2048)
        set_swc_memory_budget(str(tmp_path), "MySwc", "ROM", 8192)

    def test_add_port_preserves_swc_memory(self, tmp_path):
        self._swc_with_budget(tmp_path)

        add_port(str(tmp_path), "MySwc", "pOut", "provide", "ISpeed", auditor=_auditor(tmp_path))

        assert _swc_data(tmp_path)["memory"] == {"RAM": 2048, "ROM": 8192}

    def test_add_runnable_preserves_swc_memory(self, tmp_path):
        self._swc_with_budget(tmp_path)

        add_runnable(str(tmp_path), "MySwc", "Run", auditor=_auditor(tmp_path))

        assert _swc_data(tmp_path)["memory"] == {"RAM": 2048, "ROM": 8192}

    def test_add_timing_event_preserves_swc_memory(self, tmp_path):
        self._swc_with_budget(tmp_path)
        add_runnable(str(tmp_path), "MySwc", "Run")

        add_timing_event(str(tmp_path), "MySwc", "Run", 0.01, auditor=_auditor(tmp_path))

        assert _swc_data(tmp_path)["memory"] == {"RAM": 2048, "ROM": 8192}

    def test_add_init_event_preserves_swc_memory(self, tmp_path):
        self._swc_with_budget(tmp_path)
        add_runnable(str(tmp_path), "MySwc", "Run")

        add_init_event(str(tmp_path), "MySwc", "Run", auditor=_auditor(tmp_path))

        assert _swc_data(tmp_path)["memory"] == {"RAM": 2048, "ROM": 8192}

    def test_map_runnable_to_task_preserves_task_wcet(self, tmp_path):
        create_swc(str(tmp_path), "MySwc", "body")
        add_runnable(str(tmp_path), "MySwc", "Run")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        set_task_wcet(str(tmp_path), "Task_10ms", "1500us")

        map_runnable_to_task(str(tmp_path), "Task_10ms", "MySwc", "Run", auditor=_auditor(tmp_path))

        assert _task_data(tmp_path)["wcet"] == "1500us"

    def test_map_runnable_to_task_still_omits_an_unset_wcet(self, tmp_path):
        create_swc(str(tmp_path), "MySwc", "body")
        add_runnable(str(tmp_path), "MySwc", "Run")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        map_runnable_to_task(str(tmp_path), "Task_10ms", "MySwc", "Run", auditor=_auditor(tmp_path))

        assert "wcet" not in _task_data(tmp_path)

    def test_swc_mutations_still_omit_an_unset_memory_map(self, tmp_path):
        create_swc(str(tmp_path), "MySwc", "body")

        add_runnable(str(tmp_path), "MySwc", "Run", auditor=_auditor(tmp_path))

        assert "memory" not in _swc_data(tmp_path)


# update_resource / set_task_partition: ``create_hardware_element`` has no in-place inverse, so a typo'd threshold would
# be permanent. ``update_resource`` keeps its name because ``authorization.classify_safety_act`` dispatches on it, a
# wire fact in the audit log and the approval prompt.


class TestUpdateResource:
    def test_corrects_a_typod_threshold_in_place(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.0001)

        result = update_resource(str(tmp_path), "CORE0", threshold=0.7, auditor=_auditor(tmp_path))

        assert result == str(tmp_path / "hardware" / "CORE0.yaml")
        assert read_hardware(str(tmp_path))[0].threshold == 0.7

    def test_flips_enforcement_to_advisory(self, tmp_path):
        _memory(tmp_path, "RAM0")

        update_resource(str(tmp_path), "RAM0", enforcement="advisory", auditor=_auditor(tmp_path))

        assert read_hardware(str(tmp_path))[0].enforcement == "advisory"

    def test_corrects_a_memory_size(self, tmp_path):
        _memory(tmp_path, "RAM0", size=1024)

        update_resource(str(tmp_path), "RAM0", size=262144, auditor=_auditor(tmp_path))

        element = read_hardware(str(tmp_path))[0]
        assert isinstance(element, Memory)
        assert element.size == 262144

    def test_preserves_every_field_left_unset(self, tmp_path):
        _core(tmp_path, "CORE3", threshold=0.5, hw_id=3, description="Table 4.2")

        update_resource(str(tmp_path), "CORE3", threshold=0.9, auditor=_auditor(tmp_path))

        element = read_hardware(str(tmp_path))[0]
        assert isinstance(element, Core)
        assert (element.name, element.hw_id, element.enforcement, element.description) == (
            "CORE3",
            3,
            "strict",
            "Table 4.2",
        )

    def test_a_memorys_type_and_accessible_survive_a_budget_correction(self, tmp_path):
        """A memory's ``type`` and ``accessible`` have no update path: a correction to a limit must not lose them."""
        _memory(tmp_path, "SHARED", memory_type="Flash", accessible=["CORE0", "CORE1"])

        update_resource(str(tmp_path), "SHARED", size=4096)

        element = read_hardware(str(tmp_path))[0]
        assert isinstance(element, Memory)
        assert (element.type, element.accessible, element.size) == ("Flash", ["CORE0", "CORE1"], 4096)

    @pytest.mark.parametrize(
        ("kind", "correction"),
        [
            ("core", {"threshold": 0.9}),
            ("core", {"enforcement": "advisory"}),
            ("core", {"description": "Table 4.3"}),
            ("memory", {"size": 8192}),
            ("memory", {"threshold": 0.5}),
            ("memory", {"enforcement": "advisory"}),
            ("memory", {"description": "Table 4.3"}),
        ],
    )
    @pytest.mark.parametrize("capability", ["QM", "B", "D"])
    def test_a_declared_asil_capability_survives_every_correction(self, tmp_path, kind, correction, capability):
        """A budget correction must not quietly make the element unrated.

        ``update_resource`` writes through the hand-written ``hardware_to_dict``, so a field it does not name is erased,
        and an unrated element is compared against nothing. The file is seeded by hand: ``create_hardware_element``
        takes no capability.
        """
        _create(tmp_path, "ELEM", kind)
        seeded = _hardware_data(tmp_path, "ELEM") | {"asil_capability": capability}
        (tmp_path / "hardware" / "ELEM.yaml").write_text(
            yaml.safe_dump(seeded, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
        )

        update_resource(str(tmp_path), "ELEM", **correction)

        element = read_hardware(str(tmp_path))[0]
        assert isinstance(element, Core | Memory)
        assert element.asil_capability == capability
        for field_name, value in correction.items():
            assert getattr(element, field_name) == value

    @pytest.mark.parametrize("kind", ["core", "memory"])
    def test_an_unrated_element_is_still_written_without_the_key(self, tmp_path, kind):
        """The other half: an element that declares no capability gains no key."""
        _create(tmp_path, "ELEM", kind)
        before = _hardware_data(tmp_path, "ELEM")

        update_resource(str(tmp_path), "ELEM", description="Table 4.3")

        assert _hardware_data(tmp_path, "ELEM") == before | {"description": "Table 4.3"}
        assert "asil_capability" not in (tmp_path / "hardware" / "ELEM.yaml").read_text(encoding="utf-8")

    def test_empty_description_clears_it(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5, description="Table 4.2")

        update_resource(str(tmp_path), "CORE0", description="", auditor=_auditor(tmp_path))

        assert "description" not in _hardware_data(tmp_path, "CORE0")

    def test_a_processors_description_is_the_one_field_it_can_update(self, tmp_path):
        create_hardware_element(str(tmp_path), "CPU", "processor", description="TC397")

        update_resource(str(tmp_path), "CPU", description="TC399", auditor=_auditor(tmp_path))

        assert read_hardware(str(tmp_path))[0].description == "TC399"

    @pytest.mark.parametrize("field", ["threshold", "enforcement"])
    def test_a_budget_field_is_refused_on_a_processor(self, tmp_path, field):
        create_hardware_element(str(tmp_path), "CPU", "processor")
        value = 0.7 if field == "threshold" else "advisory"

        with pytest.raises(ValueError, match="is not valid for processor 'CPU'"):
            update_resource(str(tmp_path), "CPU", **{field: value})
        assert _hardware_data(tmp_path, "CPU") == {"name": "CPU", "kind": "processor"}

    def test_audit_entry_uses_the_writer_function_name(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5)

        update_resource(str(tmp_path), "CORE0", threshold=0.7, auditor=_auditor(tmp_path))

        entry = _audit_entries(tmp_path)[-1]
        assert entry["operation"] == "update_resource"
        assert entry["args"] == {"name": "CORE0", "threshold": 0.7}
        assert entry["result"].endswith("CORE0.yaml")

    def test_unknown_element_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            update_resource(str(tmp_path), "Nope", threshold=0.7, auditor=_auditor(tmp_path))
        assert _audit_entries(tmp_path) == []

    def test_no_field_to_update_is_rejected(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5)

        with pytest.raises(ValueError, match="at least one of"):
            update_resource(str(tmp_path), "CORE0", auditor=_auditor(tmp_path))

    def test_size_on_a_core_is_rejected(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5)

        with pytest.raises(ValueError, match="'size' is not valid for core 'CORE0'"):
            update_resource(str(tmp_path), "CORE0", size=1024, auditor=_auditor(tmp_path))
        assert "size" not in _hardware_data(tmp_path, "CORE0")

    @pytest.mark.parametrize("bad", [0.0, 1.5, -0.1])
    def test_out_of_range_threshold_is_rejected_and_nothing_is_written(self, tmp_path, bad):
        _core(tmp_path, "CORE0", threshold=0.5)

        with pytest.raises((ValueError, ValidationError)):
            update_resource(str(tmp_path), "CORE0", threshold=bad, auditor=_auditor(tmp_path))
        assert _hardware_data(tmp_path, "CORE0")["threshold"] == 0.5

    def test_unknown_enforcement_is_rejected(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5)

        with pytest.raises((ValueError, ValidationError)):
            update_resource(str(tmp_path), "CORE0", enforcement="lenient", auditor=_auditor(tmp_path))
        assert _hardware_data(tmp_path, "CORE0")["enforcement"] == "strict"

    def test_works_without_an_auditor(self, tmp_path):
        _core(tmp_path, "CORE0", threshold=0.5)
        assert update_resource(str(tmp_path), "CORE0", threshold=0.6).endswith("CORE0.yaml")


def _populated_hardware() -> dict[str, Processor | Core | Memory | Partition]:
    """One element of each hardware kind with no field left at an omittable default."""
    return {
        # Every hardware kind carries ``satisfies``.
        "processor": Processor(name="CPU", kind="processor", description="TC397", satisfies=["REQ_HW@1"]),
        "core": Core(
            name="CORE0",
            kind="core",
            threshold=0.7,
            enforcement="advisory",
            asil_capability="B",
            hw_id=0,
            description="Table 4.2",
            satisfies=["REQ_HW@1"],
        ),
        "memory": Memory(
            name="RAM0",
            kind="memory",
            type="RAM",
            size=4096,
            accessible=["CORE0"],
            threshold=0.8,
            enforcement="advisory",
            asil_capability="QM",
            description="Table 4.2",
            satisfies=["REQ_HW@1", "REQ_MEM@2"],
        ),
        "partition": Partition(
            name="P_Brake",
            kind="partition",
            core="CORE0",
            asil="B",
            # Off its ``non_trusted`` default, or both serializers would drop it.
            trust="trusted_with_protection",
            coexistence="Separate memory regions under memory protection.",
            description="The brake functions' OS-Application.",
            satisfies=["REQ_PART@1"],
        ),
    }


class TestTheHandWrittenHardwareDumpAgreesWithTheDerivedOne:
    """``hardware_to_dict`` held to ``write_element``'s serializer, both halves.

    ``update_resource`` and ``create_hardware_element`` write through the hand-written dump, the editing protocol
    through ``element_to_dict``, which walks ``model_fields``; a field only the derived path writes would be erased by
    every budget correction. The next hardware field fails here by name.
    """

    @pytest.mark.parametrize("kind", ["processor", "core", "memory", "partition"])
    def test_the_two_serializers_produce_the_same_document(self, kind):
        element = _populated_hardware()[kind]

        assert hardware_to_dict(element) == element_to_dict(element)

    def test_the_populated_elements_really_populate_every_declared_field(self):
        """Every declared field of each kind is written, or the equality compares two documents both omitting it.

        **What this does not reach**: whether the value is meaningful, and a field in ``schema.py`` but not in the
        contract (the conformance gate's).
        """
        layer = next(facts for facts in production_registry().layers if facts.name == "hardware")
        populated = _populated_hardware()

        for kind in layer.kinds:
            declared = {field.name for field in kind.fields}
            missing = declared - set(element_to_dict(populated[kind.kind]))
            assert not missing, (
                f"`_populated_hardware()` leaves {sorted(missing)} of `{kind.id}` at a default, so the "
                "serializer equality above compares two documents that are both missing it. Populate it here."
            )


class TestUpdateResourceKeepsTheLinks:
    """``update_resource`` rewrites through ``hardware_to_dict`` and keeps ``satisfies``, one case per hardware kind."""

    @pytest.mark.parametrize(
        ("kind", "correction"),
        [
            ("processor", {"description": "TC399"}),
            ("core", {"threshold": 0.6}),
            ("memory", {"size": 8192}),
            ("partition", {"description": "Now described."}),
        ],
    )
    def test_update_resource_keeps_satisfies(self, tmp_path, kind, correction):
        element = _populated_hardware()[kind]
        path = tmp_path / "hardware" / f"{element.name}.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(hardware_to_dict(element), sort_keys=False), encoding="utf-8", newline="\n")

        update_resource(str(tmp_path), element.name, **correction)

        assert yaml.safe_load(path.read_text(encoding="utf-8"))["satisfies"] == element.satisfies


class TestSetTaskPartition:
    """The declared-partition guard is unconditional, so the writer is never stricter than validation.

    ``hardware.undeclared-partition`` refuses an unresolved name whatever the enforcement.
    """

    def test_moves_a_task_to_another_declared_partition(self, tmp_path):
        _core(tmp_path, "CORE0")
        _core(tmp_path, "CORE1")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION1")

        result = set_task_partition(str(tmp_path), "Task_10ms", "PARTITION0", auditor=_auditor(tmp_path))

        assert result == str(tmp_path / "tasks" / "Task_10ms.yaml")
        assert read_tasks(str(tmp_path))[0].partition == "PARTITION0"

    def test_an_undeclared_partition_is_rejected_and_the_declared_ones_are_named(self, tmp_path):
        _core(tmp_path, "CORE0")
        _core(tmp_path, "CORE1")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        with pytest.raises(ValueError, match=r"declared partitions are \['PARTITION0', 'PARTITION1'\]"):
            set_task_partition(str(tmp_path), "Task_10ms", "PARTITION7", auditor=_auditor(tmp_path))
        assert _task_data(tmp_path)["partition"] == "PARTITION0"

    def test_a_partition_on_an_advisory_core_is_still_a_declared_partition(self, tmp_path):
        """``advisory`` is an enforcement level, not a declaration level: an undeclared partition is refused anyway."""
        _core(tmp_path, "CORE0", enforcement="advisory")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        set_task_partition(str(tmp_path), "Task_10ms", "PARTITION0")

        with pytest.raises(ValueError, match="is not declared as a 'kind: partition' element"):
            set_task_partition(str(tmp_path), "Task_10ms", "PARTITION7")

    def test_a_project_declaring_no_partition_at_all_refuses_every_destination(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        with pytest.raises(ValueError, match=r"declared partitions are \[\]"):
            set_task_partition(str(tmp_path), "Task_10ms", "PARTITION3", auditor=_auditor(tmp_path))

    @pytest.mark.parametrize("kind", ["processor", "core", "memory"])
    def test_an_element_of_another_kind_is_not_a_partition(self, tmp_path, kind):
        _create(tmp_path, "OTHER", kind)
        _core(tmp_path, "CORE0")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        with pytest.raises(ValueError, match="is not declared as a 'kind: partition' element"):
            set_task_partition(str(tmp_path), "Task_10ms", "OTHER")

    def test_preserves_every_other_task_field(self, tmp_path):
        _core(tmp_path, "CORE0")
        _core(tmp_path, "CORE4")
        create_swc(str(tmp_path), "MySwc", "body")
        add_runnable(str(tmp_path), "MySwc", "Run")
        create_task(
            str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0", description="Engine loop"
        )
        map_runnable_to_task(str(tmp_path), "Task_10ms", "MySwc", "Run")
        set_task_wcet(str(tmp_path), "Task_10ms", "900us")

        set_task_partition(str(tmp_path), "Task_10ms", "PARTITION4", auditor=_auditor(tmp_path))

        task = read_tasks(str(tmp_path))[0]
        assert (task.period, task.priority, task.wcet, task.description) == (0.01, 5, "900us", "Engine loop")
        assert [(r.swc, r.runnable) for r in task.runnables] == [("MySwc", "Run")]
        assert task.partition == "PARTITION4"

    def test_audit_entry_uses_the_writer_function_name(self, tmp_path):
        _core(tmp_path, "CORE2")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        set_task_partition(str(tmp_path), "Task_10ms", "PARTITION2", auditor=_auditor(tmp_path))

        entry = _audit_entries(tmp_path)[-1]
        assert entry["operation"] == "set_task_partition"
        assert entry["args"] == {"task_name": "Task_10ms", "partition": "PARTITION2"}
        assert entry["result"].endswith("Task_10ms.yaml")

    def test_an_unknown_task_is_reported_before_the_partition_is_looked_at(self, tmp_path):
        """An unknown task reports as an unknown task, whichever partition it was moved to."""
        with pytest.raises(ValueError, match="not found"):
            set_task_partition(str(tmp_path), "Nope", "PARTITION7", auditor=_auditor(tmp_path))
        assert _audit_entries(tmp_path) == []

    def test_an_integer_partition_is_rejected(self, tmp_path):
        """It cannot match a declared name, so the declared-partition guard refuses it before Pydantic sees it."""
        _core(tmp_path, "CORE0")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        with pytest.raises(ValueError, match="is not declared as a 'kind: partition' element"):
            set_task_partition(str(tmp_path), "Task_10ms", 0, auditor=_auditor(tmp_path))
        assert _task_data(tmp_path)["partition"] == "PARTITION0"

    def test_works_without_an_auditor(self, tmp_path):
        _core(tmp_path, "CORE1")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        assert set_task_partition(str(tmp_path), "Task_10ms", "PARTITION1").endswith("Task_10ms.yaml")

    def test_the_old_name_is_gone(self):
        """The writer package exports no ``set_task_core`` alias."""
        import swcraft_tools.writer as writer_package

        assert not hasattr(writer_package, "set_task_core")


class TestSetTaskPriority:
    """The only writer for ``Task.priority``.

    Without it an inverted priority's ``rate_monotonic_violation`` could not be cleared by any tool call.
    """

    def test_sets_the_priority_and_returns_the_task_path(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        result = set_task_priority(str(tmp_path), "Task_10ms", 30, auditor=_auditor(tmp_path))

        assert result == str(tmp_path / "tasks" / "Task_10ms.yaml")
        assert read_tasks(str(tmp_path))[0].priority == 30

    def test_zero_is_a_legal_priority(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        set_task_priority(str(tmp_path), "Task_10ms", 0, auditor=_auditor(tmp_path))

        assert read_tasks(str(tmp_path))[0].priority == 0

    def test_preserves_every_other_task_field(self, tmp_path):
        create_swc(str(tmp_path), "MySwc", "body")
        add_runnable(str(tmp_path), "MySwc", "Run")
        create_task(
            str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION2", description="Engine loop"
        )
        map_runnable_to_task(str(tmp_path), "Task_10ms", "MySwc", "Run")
        set_task_wcet(str(tmp_path), "Task_10ms", "900us")

        set_task_priority(str(tmp_path), "Task_10ms", 40, auditor=_auditor(tmp_path))

        task = read_tasks(str(tmp_path))[0]
        assert (task.period, task.partition, task.wcet, task.description) == (
            0.01,
            "PARTITION2",
            "900us",
            "Engine loop",
        )
        assert [(r.swc, r.runnable) for r in task.runnables] == [("MySwc", "Run")]

    def test_audit_entry_uses_the_writer_function_name(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        set_task_priority(str(tmp_path), "Task_10ms", 30, auditor=_auditor(tmp_path))

        entry = _audit_entries(tmp_path)[-1]
        assert entry["operation"] == "set_task_priority"
        assert entry["args"] == {"task_name": "Task_10ms", "priority": 30}
        assert entry["result"].endswith("Task_10ms.yaml")

    def test_unknown_task_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            set_task_priority(str(tmp_path), "Nope", 1, auditor=_auditor(tmp_path))
        assert _audit_entries(tmp_path) == []

    def test_negative_priority_is_rejected(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        with pytest.raises((ValueError, ValidationError)):
            set_task_priority(str(tmp_path), "Task_10ms", -1, auditor=_auditor(tmp_path))
        assert _task_data(tmp_path)["priority"] == 5

    def test_float_priority_is_rejected(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        with pytest.raises(ValueError, match="must be an int"):
            set_task_priority(str(tmp_path), "Task_10ms", 5.0, auditor=_auditor(tmp_path))
        assert _task_data(tmp_path)["priority"] == 5

    def test_bool_priority_is_rejected(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        with pytest.raises(ValueError, match="must be an int"):
            set_task_priority(str(tmp_path), "Task_10ms", True, auditor=_auditor(tmp_path))

    def test_works_without_an_auditor(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        assert set_task_priority(str(tmp_path), "Task_10ms", 7).endswith("Task_10ms.yaml")


class TestIntBudgetsAreRejectedIdenticallyOnEveryEntryPoint:
    """One integer rule, one answer, whichever writer a caller reaches first.

    A whole-number float (``400.0``) coerces cleanly under Pydantic's lax mode; it is never accepted on create and
    refused on the next update. The task's ``wcet`` is out of the int guard: it is a budget text with its unit, refused
    alike by ``Task.wcet_is_written_as_a_budget`` (``test_the_task_budget_is_refused_alike_on_create_and_update``).
    """

    #: (create-with-value, update-with-value), both taking the model root and the offending value. The create side names
    #: a *new* element: every ``create_*`` resolves its duplicate check before the int guard.
    _PAIRS = [
        pytest.param(
            lambda root, bad: create_task(root, "Task_5ms", period=0.005, priority=bad, partition="PARTITION0"),
            lambda root, bad: set_task_priority(root, "Task_10ms", bad),
            id="task.priority",
        ),
        pytest.param(
            lambda root, bad: create_swc(root, "OtherSwc", "body", memory={"RAM": bad}),
            lambda root, bad: set_swc_memory_budget(root, "MySwc", "RAM", bad),
            id="swc.memory",
        ),
        pytest.param(
            lambda root, bad: create_hardware_element(
                root, "ROM", "memory", memory_type="ROM", accessible=["CORE0"], threshold=0.8, size=bad
            ),
            lambda root, bad: update_resource(root, "RAM", size=bad),
            id="memory.size",
        ),
    ]

    @staticmethod
    def _project(tmp_path: Path) -> str:
        """A model with the pool, the SWC and the task every pair updates."""
        root = str(tmp_path)
        create_hardware_element(
            root, "RAM", "memory", memory_type="RAM", accessible=["CORE0"], threshold=0.8, size=100000
        )
        create_swc(root, "MySwc", "body")
        create_task(root, "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        return root

    @pytest.mark.parametrize(("create_call", "update_call"), _PAIRS)
    @pytest.mark.parametrize("bad", [400.0, "400", True], ids=["float", "str", "bool"])
    def test_create_rejects_what_update_rejects(self, tmp_path, create_call, update_call, bad):
        root = self._project(tmp_path)

        with pytest.raises(ValueError, match="must be an int"):
            update_call(root, bad)
        with pytest.raises(ValueError, match="must be an int"):
            create_call(root, bad)

    @pytest.mark.parametrize(
        ("bad", "sentence"),
        [
            (400.0, "wcet 400.0 is not a budget: write a whole number with one of ns, us, ms, s, e.g. '1500us'"),
            ("400", "wcet '400' names no unit: write it with one of ns, us, ms, s, e.g. '400us'"),
            (True, "wcet True is not a budget: write a whole number with one of ns, us, ms, s, e.g. '400us'"),
        ],
        ids=["float", "str", "bool"],
    )
    def test_the_task_budget_is_refused_alike_on_create_and_update(self, tmp_path, bad, sentence):
        root = self._project(tmp_path)

        with pytest.raises(ValueError, match=re.escape(sentence)):
            set_task_wcet(root, "Task_10ms", bad)
        with pytest.raises(ValueError, match=re.escape(sentence)):
            create_task(root, "Task_5ms", period=0.005, priority=5, partition="PARTITION0", wcet=bad)
        assert not (tmp_path / "tasks" / "Task_5ms.yaml").exists()

    def test_hw_id_is_guarded_too(self, tmp_path):
        """``hw_id`` has no update path, so ``create_hardware_element`` is the only place its guard can live."""
        with pytest.raises(ValueError, match="must be an int"):
            _core(tmp_path, "CORE0", hw_id=0.0)
        assert not (tmp_path / "hardware" / "CORE0.yaml").exists()

    def test_nothing_is_written_when_a_create_is_refused(self, tmp_path):
        root = self._project(tmp_path)

        with pytest.raises(ValueError, match="must be an int"):
            create_task(root, "Task_5ms", period=0.005, priority=400.0, partition="PARTITION0")

        assert not (tmp_path / "tasks" / "Task_5ms.yaml").exists()
        assert [e["operation"] for e in _audit_entries(tmp_path)] == []

    def test_size_on_a_core_still_reports_the_kind_mismatch(self, tmp_path):
        """The int guard must not shadow the more useful message."""
        root = str(tmp_path)
        _core(tmp_path, "CORE0")

        with pytest.raises(ValueError, match="not valid for core"):
            update_resource(root, "CORE0", size=1024.0)
        with pytest.raises(ValueError, match="not valid for a core"):
            _core(tmp_path, "CORE1", size=1024.0)


# The writer's "declared ..." list and ``check-model``'s are one answer


class TestTheDeclaredListAgreesWithCheckModel:
    """One question, two entry points, one answer: content and order.

    ``validator.hardware._declared`` renders the declared list as ``sorted(set(names))``; the writer never prints a
    name two files claim twice. The duplicates are written as raw files, since ``create_hardware_element`` refuses them.

    What this does **not** claim: that the two *renderings* match (``", "`` join against a ``list`` repr); only content
    and order are shared.
    """

    @staticmethod
    def _duplicate(tmp_path: Path, name: str, kind: str, filename: str, **fields) -> None:
        document = {"name": name, "kind": kind, **fields}
        directory = tmp_path / "hardware"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def test_a_duplicated_partition_name_is_named_once(self, tmp_path):
        self._duplicate(tmp_path, "CORE0", "core", "CORE0.yaml", threshold=0.7, enforcement="strict")
        self._duplicate(tmp_path, "CORE0", "core", "Duplicate.yaml", threshold=0.7, enforcement="strict")
        _core(tmp_path, "CORE1")
        self._duplicate(tmp_path, "PARTITION0", "partition", "PARTITION0.yaml", core="CORE0")
        self._duplicate(tmp_path, "PARTITION0", "partition", "DuplicatePartition.yaml", core="CORE1")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION1")

        with pytest.raises(ValueError) as excinfo:
            set_task_partition(str(tmp_path), "Task_10ms", "PARTITION7")

        assert "declared partitions are ['PARTITION0', 'PARTITION1']" in str(excinfo.value)
        partitions = [element.name for element in read_hardware(str(tmp_path)) if element.kind == "partition"]
        assert _declared(partitions) == "PARTITION0, PARTITION1"

    def test_a_duplicated_memory_name_is_named_once(self, tmp_path):
        fields = {"type": "RAM", "size": 1024, "accessible": ["CORE0"], "threshold": 0.8}
        self._duplicate(tmp_path, "RAM0", "memory", "RAM0.yaml", **fields)
        self._duplicate(tmp_path, "RAM0", "memory", "Duplicate.yaml", **fields)

        with pytest.raises(ValueError) as excinfo:
            create_swc(str(tmp_path), "MySwc", "body", memory={"FLASH0": 512})

        assert "declared memories are ['RAM0']" in str(excinfo.value)
        assert _declared([element.name for element in read_hardware(str(tmp_path))]) == "RAM0"

    def test_the_duplicate_state_is_one_check_model_refuses_and_the_writer_does_not(self, tmp_path):
        """The duplicate state is reachable: ``check-model`` refuses it and the writer does not."""
        self._duplicate(tmp_path, "CORE0", "core", "CORE0.yaml", threshold=0.7, enforcement="strict")
        self._duplicate(tmp_path, "CORE0", "core", "Duplicate.yaml", threshold=0.7, enforcement="strict")
        self._duplicate(tmp_path, "PARTITION0", "partition", "PARTITION0.yaml", core="CORE0")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        set_task_partition(str(tmp_path), "Task_10ms", "PARTITION0")

        result = check_hardware_topology(str(tmp_path))
        assert not result.consistent
        assert any("Duplicate hardware element name 'CORE0'" in issue for issue in result.issues)
