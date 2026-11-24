"""Tests for the Task model: schema, reader, writer and validator."""

from __future__ import annotations

import pytest
import validator_package
import yaml
from conftest import _core, _root_composition, _swc, _task, _write, element_path
from pydantic import ValidationError
from swcraft_tools.auditor import AuditLog
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.reader import find_task_file, read_tasks
from swcraft_tools.schema import Task, TaskRunnableRef, TimingEvent
from swcraft_tools.validator import CONSISTENCY_RULES, ConsistencyResult, check_task_consistency, scheduling
from swcraft_tools.writer import (
    add_init_event,
    add_runnable,
    add_timing_event,
    create_swc,
    create_task,
    map_runnable_to_task,
)


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


VALID_TASK = {
    "name": "Task_10ms",
    "period": 0.01,
    "priority": 5,
    "partition": "PARTITION0",
    "description": "10 ms application task",
    "runnables": [{"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}],
}


# ---------------------------------------------------------------------------
# TaskRunnableRef
# ---------------------------------------------------------------------------


def test_task_runnable_ref_valid():
    ref = TaskRunnableRef(swc="ExampleConsumerSWC", runnable="ExampleConsumerRun")
    assert ref.swc == "ExampleConsumerSWC"
    assert ref.runnable == "ExampleConsumerRun"


def test_task_runnable_ref_empty_swc_raises():
    with pytest.raises(ValidationError):
        TaskRunnableRef(swc="", runnable="ExampleConsumerRun")


def test_task_runnable_ref_empty_runnable_raises():
    with pytest.raises(ValidationError):
        TaskRunnableRef(swc="ExampleConsumerSWC", runnable="")


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


def test_task_valid():
    task = Task(
        name="Task_10ms",
        period=0.01,
        priority=5,
        partition="PARTITION0",
        description="10 ms application task",
        runnables=[TaskRunnableRef(swc="ExampleConsumerSWC", runnable="ExampleConsumerRun")],
    )
    assert task.name == "Task_10ms"
    assert task.period == pytest.approx(0.01)
    assert task.priority == 5
    assert task.partition == "PARTITION0"
    assert len(task.runnables) == 1


def test_task_defaults():
    task = Task(name="Task_10ms", period=0.01, priority=5, partition="PARTITION0")
    assert task.description == ""
    assert task.runnables == []


def test_task_partition_is_required_and_the_refusal_names_the_field():
    """Contract 9.0.0: `partition` names a hardware element and has no default.

    `hardware/` is mandatory and a default would have to name a partition the model invents, so a task
    file that omits `partition` does not parse at all -- and an 8.x task file, which says `core`, fails
    exactly so, naming the field (the symptom the release note's upgrade recipe names).
    """
    with pytest.raises(ValidationError) as raised:
        Task(name="Task_10ms", period=0.01, priority=5)
    assert [error["loc"] for error in raised.value.errors()] == [("partition",)]
    assert raised.value.errors()[0]["type"] == "missing"

    with pytest.raises(ValidationError) as raised:
        Task.model_validate({"name": "Task_10ms", "period": 0.01, "priority": 5, "core": "CORE0"})
    assert "partition\n  Field required" in str(raised.value)


def test_a_task_carrying_core_and_partition_loads_with_core_ignored():
    """No retired-field refusal: `core` is an unknown key like any other."""
    task = Task.model_validate(
        {"name": "Task_10ms", "period": 0.01, "priority": 5, "core": "CORE0", "partition": "PARTITION0"}
    )
    assert task.partition == "PARTITION0"
    assert "core" not in task.model_dump()
    assert not hasattr(task, "core")


def test_task_empty_name_raises():
    with pytest.raises(ValidationError):
        Task(name="", period=0.01, priority=5, partition="PARTITION0")


def test_task_whitespace_name_raises():
    with pytest.raises(ValidationError):
        Task(name="   ", period=0.01, priority=5, partition="PARTITION0")


def test_task_period_zero_raises():
    with pytest.raises(ValidationError):
        Task(name="Task_10ms", period=0.0, priority=5, partition="PARTITION0")


def test_task_period_negative_raises():
    with pytest.raises(ValidationError):
        Task(name="Task_10ms", period=-0.01, priority=5, partition="PARTITION0")


def test_task_period_nan_raises():
    with pytest.raises(ValidationError):
        Task(name="Task_10ms", period=float("nan"), priority=5, partition="PARTITION0")


def test_task_period_infinite_raises():
    with pytest.raises(ValidationError):
        Task(name="Task_10ms", period=float("inf"), priority=5, partition="PARTITION0")


def test_timing_event_period_nan_raises():
    with pytest.raises(ValidationError):
        TimingEvent(type="timing", period=float("nan"))


def test_timing_event_period_infinite_raises():
    with pytest.raises(ValidationError):
        TimingEvent(type="timing", period=float("inf"))


def test_task_negative_priority_raises():
    with pytest.raises(ValidationError):
        Task(name="Task_10ms", period=0.01, priority=-1, partition="PARTITION0")


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_task_blank_partition_raises(blank):
    """`rule:tasks.task.partition.not-blank`, refused at load and naming the field."""
    with pytest.raises(ValidationError) as raised:
        Task(name="Task_10ms", period=0.01, priority=5, partition=blank)
    assert [error["loc"] for error in raised.value.errors()] == [("partition",)]


def test_task_integer_partition_raises():
    """An integer is a hard parse failure, not a coercion: it must not resolve to the element named `"0"`."""
    with pytest.raises(ValidationError):
        Task(name="Task_10ms", period=0.01, priority=5, partition=0)


def test_task_duplicate_runnable_mapping_raises():
    ref = TaskRunnableRef(swc="ExampleConsumerSWC", runnable="ExampleConsumerRun")
    with pytest.raises(ValidationError):
        Task(name="Task_10ms", period=0.01, priority=5, partition="PARTITION0", runnables=[ref, ref])


def test_task_empty_runnables_allowed():
    task = Task(name="Task_10ms", period=0.01, priority=5, partition="PARTITION0", runnables=[])
    assert task.runnables == []


class TestTheSelector:
    """Exactly one of ``swc`` and ``instance`` names the component, judged at load inside one entry.

    Contract ``9.0.0``."""

    def test_both_selectors_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of 'swc' and 'instance'"):
            TaskRunnableRef(swc="A", instance="a", runnable="Run")

    def test_neither_selector_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of 'swc' and 'instance'"):
            TaskRunnableRef(runnable="Run")

    def test_an_explicit_null_swc_is_the_exactly_one_refusal_and_never_an_attribute_error(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of 'swc' and 'instance'"):
            TaskRunnableRef.model_validate({"swc": None, "runnable": "Run"})

    def test_instance_alone_is_accepted(self) -> None:
        ref = TaskRunnableRef(instance="left/worker", runnable="Run")
        assert (ref.swc, ref.instance, ref.runnable) == (None, "left/worker", "Run")

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_instance_is_refused(self, blank: str) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            TaskRunnableRef(instance=blank, runnable="Run")

    def test_the_triple_written_twice_is_refused(self) -> None:
        ref = TaskRunnableRef(instance="left/worker", runnable="Run")
        with pytest.raises(ValidationError, match="duplicate runnable mapping"):
            Task(name="T", period=0.01, priority=5, partition="P", runnables=[ref, ref])

    def test_the_same_runnable_through_two_selectors_loads(self) -> None:
        """The same runnable through two selectors loads.

        A load rule sees one file and no composition, so it cannot tell that ``swc: A`` and ``instance: A`` name one
        occurrence; that is ``task.resolved-duplicate-runnable``'s, at ``check-model``."""
        task = Task(
            name="T",
            period=0.01,
            priority=5,
            partition="P",
            runnables=[TaskRunnableRef(swc="A", runnable="Run"), TaskRunnableRef(instance="A", runnable="Run")],
        )
        assert len(task.runnables) == 2

    def test_a_dumped_entry_carries_the_absent_selector_as_none(self) -> None:
        assert TaskRunnableRef(swc="A", runnable="Run").model_dump() == {
            "swc": "A",
            "instance": None,
            "runnable": "Run",
        }


# ---------------------------------------------------------------------------
# read_tasks
# ---------------------------------------------------------------------------


class TestReadTasks:
    def test_reads_all_task_yaml_under_tasks_dir(self, tmp_path):
        _write_yaml(tmp_path / "tasks" / "Task_10ms.yaml", VALID_TASK)
        other = {**VALID_TASK, "name": "Task_20ms", "period": 0.02, "runnables": []}
        _write_yaml(tmp_path / "tasks" / "Task_20ms.yaml", other)
        tasks = read_tasks(str(tmp_path))
        assert len(tasks) == 2
        names = {t.name for t in tasks}
        assert names == {"Task_10ms", "Task_20ms"}

    def test_missing_tasks_dir_returns_empty_list(self, tmp_path):
        tasks = read_tasks(str(tmp_path))
        assert tasks == []

    def test_invalid_task_yaml_raises_with_offending_filename(self, tmp_path):
        invalid = {"name": "", "period": 0.01, "priority": 5}
        bad_path = tmp_path / "tasks" / "Bad.yaml"
        _write_yaml(bad_path, invalid)
        with pytest.raises(ValueError, match="Bad.yaml"):
            read_tasks(str(tmp_path))

    def test_malformed_yaml_syntax_raises_with_offending_filename(self, tmp_path):
        # Syntactically-broken YAML (unclosed bracket) — a yaml.YAMLError, not
        # a pydantic ValidationError, must still surface the filename.
        broken_path = tmp_path / "tasks" / "Broken.yaml"
        broken_path.parent.mkdir(parents=True, exist_ok=True)
        broken_path.write_text("name: Task_10ms\nrunnables: [swc: Foo, runnable: Bar\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Broken.yaml"):
            read_tasks(str(tmp_path))


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestFindTaskFile:
    def test_finds_existing_task_returns_path(self, tmp_path):
        _write_yaml(tmp_path / "tasks" / "Task_10ms.yaml", VALID_TASK)
        result = find_task_file(str(tmp_path), "Task_10ms")
        assert result is not None
        assert "Task_10ms.yaml" in result

    def test_nonexistent_task_returns_none(self, tmp_path):
        (tmp_path / "tasks").mkdir()
        result = find_task_file(str(tmp_path), "Missing")
        assert result is None

    def test_missing_tasks_dir_returns_none(self, tmp_path):
        result = find_task_file(str(tmp_path), "Missing")
        assert result is None


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


class TestCreateTask:
    def test_creates_valid_yaml_and_audits(self, tmp_path):
        auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
        path = create_task(
            str(tmp_path),
            "Task_10ms",
            period=0.01,
            priority=5,
            partition="PARTITION0",
            description="10 ms application task",
            auditor=auditor,
        )
        expected = tmp_path / "tasks" / "Task_10ms.yaml"
        assert expected.exists()
        assert path == str(expected)

        tasks = read_tasks(str(tmp_path))
        assert len(tasks) == 1
        assert tasks[0].name == "Task_10ms"
        assert tasks[0].period == pytest.approx(0.01)
        assert tasks[0].priority == 5
        assert tasks[0].partition == "PARTITION0"
        assert tasks[0].description == "10 ms application task"
        assert tasks[0].runnables == []

        entries = auditor.get_entries()
        assert len(entries) == 1
        assert entries[0]["operation"] == "create_task"

    def test_defaults(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        tasks = read_tasks(str(tmp_path))
        assert tasks[0].partition == "PARTITION0"
        assert tasks[0].description == ""

    def test_blank_partition_raises(self, tmp_path):
        with pytest.raises(ValidationError):
            create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="  ")

    def test_integer_partition_raises(self, tmp_path):
        """`create_task` refuses an integer as `schema.Task` does."""
        with pytest.raises(ValidationError):
            create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition=0)

    def test_duplicate_task_name_raises(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        with pytest.raises(ValueError, match="Task_10ms"):
            create_task(str(tmp_path), "Task_10ms", period=0.02, priority=1, partition="PARTITION0")

    def test_empty_name_raises(self, tmp_path):
        with pytest.raises(ValueError):
            create_task(str(tmp_path), "", period=0.01, priority=5, partition="PARTITION0")

    @pytest.mark.parametrize("bad_name", ["a/b", "a\\b", "..", "../evil"])
    def test_path_traversal_name_raises(self, tmp_path, bad_name):
        with pytest.raises(ValueError):
            create_task(str(tmp_path), bad_name, period=0.01, priority=5, partition="PARTITION0")

    def test_invalid_period_raises(self, tmp_path):
        with pytest.raises(ValidationError):
            create_task(str(tmp_path), "Task_10ms", period=0.0, priority=5, partition="PARTITION0")


# ---------------------------------------------------------------------------
# map_runnable_to_task
# ---------------------------------------------------------------------------


class TestMapRunnableToTask:
    def _make_swc_with_runnable(self, tmp_path, swc_name="ExampleConsumerSWC", runnable_name="ExampleConsumerRun"):
        create_swc(str(tmp_path), swc_name, group="body")
        add_runnable(str(tmp_path), swc_name, runnable_name)

    def test_appends_in_order_and_audits(self, tmp_path):
        self._make_swc_with_runnable(tmp_path, "SwcA", "RunA")
        self._make_swc_with_runnable(tmp_path, "SwcB", "RunB")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")

        auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
        map_runnable_to_task(str(tmp_path), "Task_10ms", "SwcA", "RunA", auditor=auditor)
        map_runnable_to_task(str(tmp_path), "Task_10ms", "SwcB", "RunB", auditor=auditor)

        tasks = read_tasks(str(tmp_path))
        assert len(tasks) == 1
        refs = tasks[0].runnables
        assert len(refs) == 2
        assert (refs[0].swc, refs[0].runnable) == ("SwcA", "RunA")
        assert (refs[1].swc, refs[1].runnable) == ("SwcB", "RunB")

        entries = auditor.get_entries()
        assert len(entries) == 2
        assert all(e["operation"] == "map_runnable_to_task" for e in entries)

    def test_unknown_task_raises(self, tmp_path):
        self._make_swc_with_runnable(tmp_path)
        with pytest.raises(ValueError, match="Missing"):
            map_runnable_to_task(str(tmp_path), "Missing", "ExampleConsumerSWC", "ExampleConsumerRun")

    def test_unknown_swc_raises(self, tmp_path):
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        with pytest.raises(ValueError, match="MissingSWC"):
            map_runnable_to_task(str(tmp_path), "Task_10ms", "MissingSWC", "SomeRun")

    def test_unknown_runnable_on_existing_swc_raises(self, tmp_path):
        create_swc(str(tmp_path), "ExampleConsumerSWC", group="body")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        with pytest.raises(ValueError, match="MissingRun"):
            map_runnable_to_task(str(tmp_path), "Task_10ms", "ExampleConsumerSWC", "MissingRun")

    def test_duplicate_mapping_in_same_task_raises(self, tmp_path):
        self._make_swc_with_runnable(tmp_path)
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        map_runnable_to_task(str(tmp_path), "Task_10ms", "ExampleConsumerSWC", "ExampleConsumerRun")
        with pytest.raises(ValueError, match="already mapped"):
            map_runnable_to_task(str(tmp_path), "Task_10ms", "ExampleConsumerSWC", "ExampleConsumerRun")

    def test_mapping_already_owned_by_another_task_raises(self, tmp_path):
        self._make_swc_with_runnable(tmp_path)
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        create_task(str(tmp_path), "Task_20ms", period=0.02, priority=1, partition="PARTITION0")
        map_runnable_to_task(str(tmp_path), "Task_10ms", "ExampleConsumerSWC", "ExampleConsumerRun")
        with pytest.raises(ValueError, match="already mapped"):
            map_runnable_to_task(str(tmp_path), "Task_20ms", "ExampleConsumerSWC", "ExampleConsumerRun")


# ---------------------------------------------------------------------------
# check_task_consistency
# ---------------------------------------------------------------------------


class TestCheckTaskConsistency:
    def _make_swc_with_timing_runnable(self, tmp_path, swc_name, runnable_name, period):
        create_swc(str(tmp_path), swc_name, group="body")
        add_runnable(str(tmp_path), swc_name, runnable_name)
        add_timing_event(str(tmp_path), swc_name, runnable_name, period)

    def _make_swc_with_init_runnable(self, tmp_path, swc_name, runnable_name):
        create_swc(str(tmp_path), swc_name, group="body")
        add_runnable(str(tmp_path), swc_name, runnable_name)
        add_init_event(str(tmp_path), swc_name, runnable_name)

    def test_model_without_tasks_dir_is_consistent(self, tmp_path):
        result = check_task_consistency(str(tmp_path))
        assert isinstance(result, ConsistencyResult)
        assert result.consistent is True
        assert result.issues == []

    def test_unknown_swc_in_mapping_is_inconsistent(self, tmp_path):
        _write_yaml(
            tmp_path / "tasks" / "Task_10ms.yaml",
            {
                "name": "Task_10ms",
                "period": 0.01,
                "priority": 5,
                "partition": "PARTITION0",
                "runnables": [{"swc": "MissingSWC", "runnable": "SomeRun"}],
            },
        )
        result = check_task_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("MissingSWC" in issue for issue in result.issues)

    def test_unknown_runnable_is_inconsistent(self, tmp_path):
        create_swc(str(tmp_path), "ExampleConsumerSWC", group="body")
        _write_yaml(
            tmp_path / "tasks" / "Task_10ms.yaml",
            {
                "name": "Task_10ms",
                "period": 0.01,
                "priority": 5,
                "partition": "PARTITION0",
                "runnables": [{"swc": "ExampleConsumerSWC", "runnable": "MissingRun"}],
            },
        )
        result = check_task_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("MissingRun" in issue for issue in result.issues)

    def test_runnable_mapped_in_two_tasks_is_inconsistent(self, tmp_path):
        self._make_swc_with_timing_runnable(tmp_path, "SwcA", "RunA", 0.01)
        _write_yaml(
            tmp_path / "tasks" / "Task_A.yaml",
            {
                "name": "Task_A",
                "period": 0.01,
                "priority": 5,
                "partition": "PARTITION0",
                "runnables": [{"swc": "SwcA", "runnable": "RunA"}],
            },
        )
        _write_yaml(
            tmp_path / "tasks" / "Task_B.yaml",
            {
                "name": "Task_B",
                "period": 0.01,
                "priority": 1,
                "partition": "PARTITION0",
                "runnables": [{"swc": "SwcA", "runnable": "RunA"}],
            },
        )
        result = check_task_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("SwcA.RunA" in issue and "Task_A" in issue and "Task_B" in issue for issue in result.issues)

    def test_duplicate_task_name_across_files_is_inconsistent(self, tmp_path):
        self._make_swc_with_timing_runnable(tmp_path, "SwcA", "RunA", 0.01)
        task_data = {
            "name": "Task_10ms",
            "period": 0.01,
            "priority": 5,
            "partition": "PARTITION0",
            "runnables": [],
        }
        _write_yaml(tmp_path / "tasks" / "one.yaml", task_data)
        _write_yaml(tmp_path / "tasks" / "two.yaml", task_data)
        result = check_task_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("Task_10ms" in issue and "uplicate" in issue for issue in result.issues)

    def test_runnable_period_not_integer_multiple_is_inconsistent(self, tmp_path):
        self._make_swc_with_timing_runnable(tmp_path, "SwcA", "RunA", 0.005)
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        map_runnable_to_task(str(tmp_path), "Task_10ms", "SwcA", "RunA")

        result = check_task_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("0.005" in issue or "period" in issue for issue in result.issues)

    def test_runnable_period_integer_multiple_is_consistent(self, tmp_path):
        self._make_swc_with_timing_runnable(tmp_path, "SwcA", "RunA", 0.02)
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        map_runnable_to_task(str(tmp_path), "Task_10ms", "SwcA", "RunA")

        result = check_task_consistency(str(tmp_path))
        assert result.consistent is True
        assert result.issues == []

    def test_init_event_only_runnable_has_no_period_constraint(self, tmp_path):
        self._make_swc_with_init_runnable(tmp_path, "SwcA", "RunA")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        map_runnable_to_task(str(tmp_path), "Task_10ms", "SwcA", "RunA")

        result = check_task_consistency(str(tmp_path))
        assert result.consistent is True
        assert result.issues == []


# ---------------------------------------------------------------------------
# rule:task.instance-in-multiple-partitions (contract 9.0.0)
# ---------------------------------------------------------------------------

SPLIT_RULE = "task.instance-in-multiple-partitions"


def _split_findings(model_root) -> list:
    return [record for record in check_task_consistency(str(model_root)).records if record.rule == SPLIT_RULE]


def _composition(model_root, name: str, members: list[dict]) -> None:
    """One non-root composition holding *members* verbatim."""
    _write(element_path(model_root, "compositions", name), {"name": name, "title": name, "members": members})


class TestTheSplitRule:
    """One placed instance whose runnables run in two partitions.

    Every model below declares ``CORE0`` with ``PARTITION0`` and ``CORE1`` with
    ``PARTITION1``, a component ``Split`` with two runnables, and a task in each
    partition running one of them; what moves is the composition hierarchy, or
    the partitions the two tasks name.
    """

    def _model(self, tmp_path, *, root_members=(), partitions=("PARTITION0", "PARTITION1"), root=True):
        _core(tmp_path, "CORE0")
        _core(tmp_path, "CORE1")
        _swc(tmp_path, "Split", runnables=[{"name": "RunA"}, {"name": "RunB"}])
        _task(tmp_path, "Task_A", partition=partitions[0], runnables=[{"swc": "Split", "runnable": "RunA"}])
        _task(tmp_path, "Task_B", partition=partitions[1], runnables=[{"swc": "Split", "runnable": "RunB"}])
        if root:
            _root_composition(tmp_path, root_members)

    def test_a_type_placed_once_in_two_partitions_fires(self, tmp_path):
        self._model(tmp_path, root_members=["Split"])
        (finding,) = _split_findings(tmp_path)
        # Judged per resolved instance: the one placement is the root member
        # `Split`, and the message names it.
        assert "SWC 'Split' is placed once below the root composition, at 'Split'," in finding.message
        assert "'Task_A', 'Task_B' of partitions 'PARTITION0', 'PARTITION1'" in finding.message
        assert "placed more than once" not in finding.message
        assert finding.subjects == (
            Subject("tasks", "Task_A", (field_step("partition"),)),
            Subject(
                "tasks",
                "Task_A",
                (field_step("runnables"), member_step({"swc": "Split", "instance": None, "runnable": "RunA"})),
            ),
            Subject("tasks", "Task_B", (field_step("partition"),)),
            Subject(
                "tasks",
                "Task_B",
                (field_step("runnables"), member_step({"swc": "Split", "instance": None, "runnable": "RunB"})),
            ),
        )
        assert check_task_consistency(str(tmp_path)).consistent is False

    def test_a_type_placed_nowhere_fires(self, tmp_path):
        # An unplaced type has one identity, so the instance form keeps the finding.
        self._model(tmp_path, root_members=[])
        (finding,) = _split_findings(tmp_path)
        assert "SWC 'Split' is placed nowhere" in finding.message

    def test_a_type_placed_once_through_a_nested_composition_fires(self, tmp_path):
        self._model(tmp_path, root_members=[{"name": "Inner_1", "composition": "Inner"}])
        _composition(tmp_path, "Inner", [{"name": "Split", "type": "Split"}])
        assert len(_split_findings(tmp_path)) == 1

    # A type placed more than once and named by `swc` does not reach the split
    # rule at all: each entry is refused as `task.instance-ambiguous` (judged
    # once, and nowhere else), so the full rule list is the assertion.
    AMBIGUOUS_TWICE = ["task.instance-ambiguous", "task.instance-ambiguous"]

    def test_a_type_placed_twice_as_two_root_members_is_refused_as_ambiguous(self, tmp_path):
        self._model(
            tmp_path,
            root_members=[{"name": "Split_1", "type": "Split"}, {"name": "Split_2", "type": "Split"}],
        )
        assert _rules(tmp_path) == self.AMBIGUOUS_TWICE

    def test_a_type_placed_twice_inside_a_composition_placed_once_is_refused_as_ambiguous(self, tmp_path):
        self._model(tmp_path, root_members=[{"name": "Inner_1", "composition": "Inner"}])
        _composition(tmp_path, "Inner", [{"name": "Split_1", "type": "Split"}, {"name": "Split_2", "type": "Split"}])
        assert _rules(tmp_path) == self.AMBIGUOUS_TWICE

    def test_a_composition_placed_twice_places_its_one_member_twice(self, tmp_path):
        self._model(
            tmp_path,
            root_members=[
                {"name": "Inner_1", "composition": "Inner"},
                {"name": "Inner_2", "composition": "Inner"},
            ],
        )
        _composition(tmp_path, "Inner", [{"name": "Split", "type": "Split"}])
        records = check_task_consistency(str(tmp_path)).records
        assert [record.rule for record in records] == self.AMBIGUOUS_TWICE
        assert "placed 2 times below the root composition: 'Inner_1/Split', 'Inner_2/Split'" in records[0].message

    def test_two_instances_of_one_type_split_across_partitions_only_where_one_instance_is(self, tmp_path):
        """The instance form of the rule.

        `Split_1` in both partitions is one finding, `Split_2` in one partition is none."""
        self._model(
            tmp_path,
            root_members=[{"name": "Split_1", "type": "Split"}, {"name": "Split_2", "type": "Split"}],
        )
        _task(
            tmp_path,
            "Task_A",
            partition="PARTITION0",
            runnables=[{"instance": "Split_1", "runnable": "RunA"}, {"instance": "Split_2", "runnable": "RunA"}],
        )
        _task(
            tmp_path,
            "Task_B",
            partition="PARTITION1",
            runnables=[{"instance": "Split_1", "runnable": "RunB"}],
        )
        (finding,) = _split_findings(tmp_path)
        assert _rules(tmp_path) == [SPLIT_RULE]
        assert "SWC 'Split' at instance 'Split_1' below the root composition" in finding.message
        _task(tmp_path, "Task_B", partition="PARTITION0", runnables=[{"instance": "Split_1", "runnable": "RunB"}])
        assert _rules(tmp_path) == []

    def test_a_composition_the_root_does_not_reach_places_nothing(self, tmp_path):
        self._model(tmp_path, root_members=["Split"])
        _composition(tmp_path, "Orphan", [{"name": "Split_1", "type": "Split"}, {"name": "Split_2", "type": "Split"}])
        assert len(_split_findings(tmp_path)) == 1

    def test_one_partition_is_silent(self, tmp_path):
        self._model(tmp_path, root_members=["Split"], partitions=("PARTITION0", "PARTITION0"))
        assert _split_findings(tmp_path) == []

    def test_no_root_abstains(self, tmp_path):
        self._model(tmp_path, root=False)
        assert _split_findings(tmp_path) == []

    def test_two_roots_abstain(self, tmp_path):
        self._model(tmp_path, root_members=["Split"])
        _root_composition(tmp_path, [], name="OtherRoot")
        assert _split_findings(tmp_path) == []

    def test_a_cycle_the_root_reaches_abstains(self, tmp_path):
        self._model(tmp_path, root_members=["Split", {"name": "Loop_1", "composition": "Loop"}])
        _composition(tmp_path, "Loop", [{"name": "Self", "composition": "Loop"}])
        assert _split_findings(tmp_path) == []

    def test_a_cycle_the_root_does_not_reach_changes_no_count(self, tmp_path):
        self._model(tmp_path, root_members=["Split"])
        _composition(tmp_path, "Loop", [{"name": "Self", "composition": "Loop"}])
        assert len(_split_findings(tmp_path)) == 1

    def test_a_member_naming_an_undeclared_composition_is_not_descended_into(self, tmp_path):
        # `composition.member-unknown-composition` reports the member; the walk
        # skips it rather than failing on the missing name.
        self._model(tmp_path, root_members=["Split", {"name": "Ghost_1", "composition": "Ghost"}])
        assert len(_split_findings(tmp_path)) == 1

    def test_two_files_of_one_task_name_address_each_subject_once(self, tmp_path):
        """Both files are counted (`task.duplicate-name` reports them), each address once."""
        self._model(tmp_path, root_members=["Split"])
        _write(
            tmp_path / "tasks" / "Task_B~1.yaml",
            {
                "name": "Task_B",
                "period": 0.01,
                "priority": 10,
                "partition": "PARTITION1",
                "runnables": [{"swc": "Split", "runnable": "RunB"}],
            },
        )
        (finding,) = _split_findings(tmp_path)
        assert finding.subjects == (
            Subject("tasks", "Task_A", (field_step("partition"),)),
            Subject(
                "tasks",
                "Task_A",
                (field_step("runnables"), member_step({"swc": "Split", "instance": None, "runnable": "RunA"})),
            ),
            Subject("tasks", "Task_B", (field_step("partition"),)),
            Subject(
                "tasks",
                "Task_B",
                (field_step("runnables"), member_step({"swc": "Split", "instance": None, "runnable": "RunB"})),
            ),
        )
        assert "'Task_A', 'Task_B' of partitions" in finding.message

    def test_a_task_on_an_undeclared_partition_is_not_counted(self, tmp_path):
        self._model(tmp_path, root_members=["Split"], partitions=("PARTITION0", "PARTITION9"))
        assert _split_findings(tmp_path) == []

    def test_a_task_naming_a_core_as_its_partition_is_not_counted(self, tmp_path):
        self._model(tmp_path, root_members=["Split"], partitions=("PARTITION0", "CORE1"))
        assert _split_findings(tmp_path) == []

    def test_an_uncounted_task_is_not_a_subject(self, tmp_path):
        self._model(tmp_path, root_members=["Split"])
        _swc(tmp_path, "Split", runnables=[{"name": "RunA"}, {"name": "RunB"}, {"name": "RunC"}])
        _task(tmp_path, "Task_C", partition="PARTITION9", runnables=[{"swc": "Split", "runnable": "RunC"}])
        (finding,) = _split_findings(tmp_path)
        assert {subject.element for subject in finding.subjects} == {"Task_A", "Task_B"}

    def test_an_unresolved_mapping_is_not_counted(self, tmp_path):
        # `task.mapping-unknown-runnable` reports it; the split rule does not.
        self._model(tmp_path, root_members=["Split"])
        _task(tmp_path, "Task_B", partition="PARTITION1", runnables=[{"swc": "Split", "runnable": "Missing"}])
        records = check_task_consistency(str(tmp_path)).records
        assert [record.rule for record in records] == ["task.mapping-unknown-runnable"]

    def test_the_placement_count_is_exact_through_a_deep_diamond(self, tmp_path):
        """The placement count is exact through a deep diamond.

        Forty levels, thirty-nine of them placing the next one twice: 2**39 paths reach ``Split``, so a walk that is not
        memoised per composition would not finish. The count is exact, the paths are capped at 64, and each `swc` entry
        is refused as ambiguous before the split rule is asked."""
        depth = 40
        self._model(tmp_path, root_members=[{"name": "L0", "composition": "Level0"}])
        for level in range(depth - 1):
            _composition(
                tmp_path,
                f"Level{level}",
                [
                    {"name": f"L{level + 1}_a", "composition": f"Level{level + 1}"},
                    {"name": f"L{level + 1}_b", "composition": f"Level{level + 1}"},
                ],
            )
        _composition(tmp_path, f"Level{depth - 1}", [{"name": "Split", "type": "Split"}])
        records = check_task_consistency(str(tmp_path)).records
        assert [record.rule for record in records] == self.AMBIGUOUS_TWICE
        message = records[0].message
        assert f"placed {2**39} times below the root composition (the first 64 shown): " in message
        assert message.count("'L0/") == 64
        assert "'L0/L1_a/L2_a/" in message

    def test_one_finding_per_component_in_name_order(self, tmp_path):
        self._model(tmp_path, root_members=["Split", "Another"])
        _swc(tmp_path, "Another", runnables=[{"name": "RunA"}, {"name": "RunB"}])
        _task(
            tmp_path,
            "Task_A",
            partition="PARTITION0",
            runnables=[{"swc": "Split", "runnable": "RunA"}, {"swc": "Another", "runnable": "RunA"}],
        )
        _task(
            tmp_path,
            "Task_B",
            partition="PARTITION1",
            runnables=[{"swc": "Split", "runnable": "RunB"}, {"swc": "Another", "runnable": "RunB"}],
        )
        assert [finding.message.split("'")[1] for finding in _split_findings(tmp_path)] == ["Another", "Split"]

    def test_two_runnables_in_one_task_are_two_subjects(self, tmp_path):
        self._model(tmp_path, root_members=["Split"])
        _swc(tmp_path, "Split", runnables=[{"name": "RunA"}, {"name": "RunB"}, {"name": "RunC"}])
        _task(
            tmp_path,
            "Task_A",
            partition="PARTITION0",
            runnables=[{"swc": "Split", "runnable": "RunA"}, {"swc": "Split", "runnable": "RunC"}],
        )
        (finding,) = _split_findings(tmp_path)
        assert finding.subjects[:3] == (
            Subject("tasks", "Task_A", (field_step("partition"),)),
            Subject(
                "tasks",
                "Task_A",
                (field_step("runnables"), member_step({"swc": "Split", "instance": None, "runnable": "RunA"})),
            ),
            Subject(
                "tasks",
                "Task_A",
                (field_step("runnables"), member_step({"swc": "Split", "instance": None, "runnable": "RunC"})),
            ),
        )


class TestTheSplitRuleIsDeclared:
    def test_the_contract_declares_it_and_states_the_abstention(self):
        (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == f"rule:{SPLIT_RULE}"]
        assert rule["family"] == "cross-element"
        # Judged per resolved instance; the one abstention left is the undefined walk.
        assert "judged per resolved instance" in rule["summary"]
        assert "placed more than once" not in rule["summary"]
        assert "Not judged without exactly one root composition or on a composition cycle." in rule["summary"]
        assert CONSISTENCY_RULES[SPLIT_RULE] == rule["summary"]

    def test_it_is_bound_to_the_phase_of_check_task_consistency_that_mints_it(self):
        assert load_binding()["rules"][f"rule:{SPLIT_RULE}"] == {
            "module": "swcraft_tools.validator.scheduling",
            "callable": "_partition_split_records",
        }
        assert "_partition_split_records" in validator_package.reached_functions(scheduling, "check_task_consistency")

    def test_the_protocol_answers_it_as_a_consistency_violation(self):
        assert RULE_CODES[SPLIT_RULE].code == CONSISTENCY_VIOLATION


# ---------------------------------------------------------------------------
# Contract 9.0.0: every check reads the resolved scheduling identity
# ---------------------------------------------------------------------------


def _rules(model_root) -> list[str]:
    return [record.rule for record in check_task_consistency(str(model_root)).records]


def _entry(selector: str, value: str, runnable: str) -> dict:
    return {selector: value, "runnable": runnable}


class TestTheInstanceIdentity:
    """Two partitions on two cores, a composition placed twice and a component placed once.

    A ``Pair`` composition placed twice as ``left`` and ``right``, each holding one ``worker`` of type ``Worker``; and a
    ``Solo`` component placed once as the root member ``solo``."""

    def _model(self, tmp_path, *, root: bool = True) -> None:
        _core(tmp_path, "CORE0")
        _core(tmp_path, "CORE1")
        _swc(tmp_path, "Worker", runnables=[{"name": "Run"}, {"name": "Other"}])
        _swc(tmp_path, "Solo", runnables=[{"name": "Run", "events": [{"type": "timing", "period": 0.01}]}])
        if root:
            _root_composition(
                tmp_path,
                [
                    {"name": "left", "composition": "Pair"},
                    {"name": "right", "composition": "Pair"},
                    {"name": "solo", "type": "Solo"},
                ],
            )
        _composition(tmp_path, "Pair", [{"name": "worker", "type": "Worker"}])

    def test_left_and_right_worker_in_two_partitions_are_two_instances_and_no_finding(self, tmp_path):
        self._model(tmp_path)
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("instance", "left/worker", "Run")])
        _task(tmp_path, "Task_B", partition="PARTITION1", runnables=[_entry("instance", "right/worker", "Run")])
        assert _rules(tmp_path) == []

    def test_left_worker_in_both_partitions_is_one_finding(self, tmp_path):
        self._model(tmp_path)
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("instance", "left/worker", "Run")])
        _task(tmp_path, "Task_B", partition="PARTITION1", runnables=[_entry("instance", "left/worker", "Other")])
        (finding,) = _split_findings(tmp_path)
        assert "SWC 'Worker' at instance 'left/worker' below the root composition" in finding.message
        assert finding.subjects[1] == Subject(
            "tasks",
            "Task_A",
            (field_step("runnables"), member_step({"swc": None, "instance": "left/worker", "runnable": "Run"})),
        )

    def test_swc_and_instance_of_one_occurrence_in_two_tasks_is_mapped_to_multiple_tasks(self, tmp_path):
        self._model(tmp_path)
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("swc", "Solo", "Run")])
        _task(tmp_path, "Task_B", partition="PARTITION0", runnables=[_entry("instance", "solo", "Run")])
        (finding,) = [
            r
            for r in check_task_consistency(str(tmp_path)).records
            if r.rule == "task.runnable-mapped-to-multiple-tasks"
        ]
        assert finding.message == "Runnable 'Solo.Run' is mapped to multiple tasks: Task_A, Task_B."
        assert finding.subjects == (
            Subject(
                "tasks",
                "Task_A",
                (field_step("runnables"), member_step({"swc": "Solo", "instance": None, "runnable": "Run"})),
            ),
            Subject(
                "tasks",
                "Task_B",
                (field_step("runnables"), member_step({"swc": None, "instance": "solo", "runnable": "Run"})),
            ),
        )

    def test_swc_and_instance_of_one_occurrence_in_one_task_is_a_resolved_duplicate(self, tmp_path):
        self._model(tmp_path)
        _task(
            tmp_path,
            "Task_A",
            partition="PARTITION0",
            runnables=[_entry("swc", "Solo", "Run"), _entry("instance", "solo", "Run")],
        )
        records = check_task_consistency(str(tmp_path)).records
        assert [r.rule for r in records] == ["task.resolved-duplicate-runnable"]
        assert "'solo.Run', 'Solo.Run'" in records[0].message  # identity_sort_key: None first
        assert len(records[0].subjects) == 2

    def test_the_runnable_is_checked_on_the_resolved_type(self, tmp_path):
        self._model(tmp_path)
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("instance", "left/worker", "Missing")])
        (finding,) = check_task_consistency(str(tmp_path)).records
        assert finding.rule == "task.mapping-unknown-runnable"
        assert finding.message == "Task 'Task_A' references unknown runnable 'Missing' on SWC 'Worker'."

    def test_the_period_is_checked_on_the_resolved_type(self, tmp_path):
        self._model(tmp_path)
        _task(tmp_path, "Task_A", period=0.02, partition="PARTITION0", runnables=[_entry("instance", "solo", "Run")])
        (finding,) = check_task_consistency(str(tmp_path)).records
        assert finding.rule == "task.runnable-period-not-multiple"
        assert "runnable 'solo.Run'" in finding.message
        assert finding.subjects[1].element == "Solo"

    def test_a_type_placed_twice_named_by_swc_is_ambiguous_with_its_paths(self, tmp_path):
        self._model(tmp_path)
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("swc", "Worker", "Run")])
        (finding,) = check_task_consistency(str(tmp_path)).records
        assert finding.rule == "task.instance-ambiguous"
        assert "placed 2 times below the root composition: 'left/worker', 'right/worker'" in finding.message
        assert finding.subjects[0].path[-1] == field_step("swc")

    def test_an_unknown_path_is_refused(self, tmp_path):
        self._model(tmp_path)
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("instance", "left/nope", "Run")])
        (finding,) = check_task_consistency(str(tmp_path)).records
        assert finding.rule == "task.instance-unknown"
        assert finding.subjects[0].path[-1] == field_step("instance")

    def test_a_path_ending_on_a_composition_is_refused(self, tmp_path):
        self._model(tmp_path)
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("instance", "left", "Run")])
        assert _rules(tmp_path) == ["task.instance-not-a-component"]

    def test_an_unknown_swc_is_still_the_swc_selectors_finding(self, tmp_path):
        self._model(tmp_path)
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("swc", "Ghost", "Run")])
        assert _rules(tmp_path) == ["task.mapping-unknown-swc"]

    def test_an_undeclared_swc_placed_twice_is_the_unknown_swc_finding_and_not_an_ambiguity(self, tmp_path):
        """An `swc` naming an undeclared component is `task.mapping-unknown-swc`'s, reported first.

        The order between the two: the resolver would refuse `Ghost` as placed twice."""
        self._model(tmp_path)
        _root_composition(
            tmp_path,
            [
                {"name": "ghost_1", "type": "Ghost"},
                {"name": "ghost_2", "type": "Ghost"},
                {"name": "solo", "type": "Solo"},
            ],
        )
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("swc", "Ghost", "Run")])
        assert _rules(tmp_path) == ["task.mapping-unknown-swc"]

    def test_a_type_placed_more_than_64_times_lists_the_first_64_paths_and_the_exact_count(self, tmp_path):
        self._model(tmp_path)
        _root_composition(tmp_path, [{"name": f"w{index}", "type": "Worker"} for index in range(70)])
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("swc", "Worker", "Run")])
        (record,) = check_task_consistency(str(tmp_path)).records
        assert record.rule == "task.instance-ambiguous"
        assert record.message.startswith(
            "Task 'Task_A' entry 'Worker.Run' names SWC 'Worker' by `swc`, but that type is placed 70 times below "
            "the root composition (the first 64 shown): 'w0', 'w1',"
        )
        assert record.message.count("'w") == 64
        assert "'w63'" in record.message and "'w64'" not in record.message

    def test_a_refused_entry_is_judged_once_and_never_also_mapped_to_multiple_tasks(self, tmp_path):
        """A refused entry is judged once.

        `swc: Worker` (placed twice) in two tasks is two `task.instance-ambiguous` findings and no
        `runnable-mapped-to-multiple-tasks`."""
        self._model(tmp_path)
        _task(tmp_path, "Task_A", partition="PARTITION0", runnables=[_entry("swc", "Worker", "Run")])
        _task(tmp_path, "Task_B", partition="PARTITION0", runnables=[_entry("swc", "Worker", "Run")])
        assert _rules(tmp_path) == ["task.instance-ambiguous", "task.instance-ambiguous"]

    def test_under_an_undefined_walk_the_identity_is_syntactic_and_the_resolver_is_silent(self, tmp_path):
        self._model(tmp_path, root=False)
        _task(
            tmp_path,
            "Task_A",
            partition="PARTITION0",
            runnables=[
                _entry("swc", "Solo", "Run"),
                _entry("instance", "solo", "Run"),
                _entry("instance", "x/y", "Run"),
            ],
        )
        # `Worker` runs in both partitions through one syntactic identity: the split
        # rule would judge it under a defined walk, and abstains under this one.
        _task(tmp_path, "Task_B", partition="PARTITION1", runnables=[_entry("swc", "Worker", "Run")])
        _task(tmp_path, "Task_C", partition="PARTITION0", runnables=[_entry("swc", "Worker", "Other")])
        # No refusal, no resolved duplicate (two triples are two identities), no
        # split finding; the composition rules own the missing root.
        assert _rules(tmp_path) == []
