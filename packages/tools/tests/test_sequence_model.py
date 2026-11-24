"""Tests for the Sequence model (schema, reader, writer, validator)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from swcraft_tools.auditor import AuditLog
from swcraft_tools.reader import find_sequence_file, read_sequences
from swcraft_tools.schema import Sequence, SequenceStep
from swcraft_tools.validator import check_sequence_consistency
from swcraft_tools.writer import (
    add_runnable,
    create_sequence,
    create_swc,
    create_task,
    map_runnable_to_task,
)


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


VALID_SEQUENCE = {
    "name": "task10ms_speed_cycle",
    "task": "Task_10ms",
    "title": "Speed control cycle",
    "steps": [
        {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
        {
            "swc": "SpeedCtrlSWC",
            "runnable": "TorqueCalc_Run",
            "note": "uses filtered speed from the previous step",
        },
    ],
}


# ---------------------------------------------------------------------------
# SequenceStep
# ---------------------------------------------------------------------------


def test_sequence_step_valid():
    step = SequenceStep(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")
    assert step.swc == "SpeedCtrlSWC"
    assert step.runnable == "SpeedCtrl_Run"
    assert step.note == ""


def test_sequence_step_note_optional_default_empty():
    step = SequenceStep(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")
    assert step.note == ""


def test_sequence_step_note_set():
    step = SequenceStep(swc="SpeedCtrlSWC", runnable="TorqueCalc_Run", note="uses filtered speed")
    assert step.note == "uses filtered speed"


def test_sequence_step_empty_swc_raises():
    with pytest.raises(ValidationError):
        SequenceStep(swc="", runnable="SpeedCtrl_Run")


def test_sequence_step_empty_runnable_raises():
    with pytest.raises(ValidationError):
        SequenceStep(swc="SpeedCtrlSWC", runnable="")


def test_sequence_step_whitespace_swc_raises():
    with pytest.raises(ValidationError):
        SequenceStep(swc="   ", runnable="SpeedCtrl_Run")


def test_sequence_step_whitespace_runnable_raises():
    with pytest.raises(ValidationError):
        SequenceStep(swc="SpeedCtrlSWC", runnable="   ")


class TestTheSelector:
    """A step names its component by exactly one of ``swc`` and ``instance``."""

    def test_both_selectors_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of 'swc' and 'instance'"):
            SequenceStep(swc="A", instance="a", runnable="Run")

    def test_neither_selector_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of 'swc' and 'instance'"):
            SequenceStep(runnable="Run")

    def test_an_explicit_null_swc_is_the_exactly_one_refusal(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of 'swc' and 'instance'"):
            SequenceStep.model_validate({"swc": None, "runnable": "Run"})

    def test_instance_alone_is_accepted(self) -> None:
        step = SequenceStep(instance="left/worker", runnable="Run")
        assert (step.swc, step.instance) == (None, "left/worker")

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_instance_is_refused(self, blank: str) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            SequenceStep(instance=blank, runnable="Run")

    def test_the_triple_written_twice_is_refused(self) -> None:
        step = {"instance": "left/worker", "runnable": "Run"}
        with pytest.raises(ValidationError, match="duplicate"):
            Sequence(name="s", task="T", steps=[step, step])

    def test_the_same_runnable_through_two_selectors_loads(self) -> None:
        sequence = Sequence(
            name="s", task="T", steps=[{"swc": "A", "runnable": "Run"}, {"instance": "A", "runnable": "Run"}]
        )
        assert len(sequence.steps) == 2


# ---------------------------------------------------------------------------
# Sequence
# ---------------------------------------------------------------------------


def test_sequence_valid():
    seq = Sequence(**VALID_SEQUENCE)
    assert seq.name == "task10ms_speed_cycle"
    assert seq.task == "Task_10ms"
    assert seq.title == "Speed control cycle"
    assert len(seq.steps) == 2
    assert seq.steps[0].swc == "SpeedCtrlSWC"
    assert seq.steps[0].runnable == "SpeedCtrl_Run"
    assert seq.steps[0].note == ""
    assert seq.steps[1].note == "uses filtered speed from the previous step"


def test_sequence_title_defaults_empty():
    data = {**VALID_SEQUENCE, "title": None}
    del data["title"]
    seq = Sequence(**data)
    assert seq.title == ""


def test_sequence_empty_name_raises():
    data = {**VALID_SEQUENCE, "name": ""}
    with pytest.raises(ValidationError):
        Sequence(**data)


def test_sequence_whitespace_name_raises():
    data = {**VALID_SEQUENCE, "name": "   "}
    with pytest.raises(ValidationError):
        Sequence(**data)


def test_sequence_empty_task_raises():
    data = {**VALID_SEQUENCE, "task": ""}
    with pytest.raises(ValidationError):
        Sequence(**data)


def test_sequence_whitespace_task_raises():
    data = {**VALID_SEQUENCE, "task": "   "}
    with pytest.raises(ValidationError):
        Sequence(**data)


def test_sequence_empty_steps_list_raises():
    data = {**VALID_SEQUENCE, "steps": []}
    with pytest.raises(ValidationError):
        Sequence(**data)


def test_sequence_duplicate_step_pair_raises():
    step = {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}
    data = {**VALID_SEQUENCE, "steps": [step, dict(step)]}
    with pytest.raises(ValidationError):
        Sequence(**data)


@pytest.mark.parametrize(
    "bad_name",
    [
        "has space",
        "has-hyphen",
        "1leading_digit",
        "trailing/slash",
        "with.dot",
    ],
)
def test_sequence_non_identifier_name_raises(bad_name):
    data = {**VALID_SEQUENCE, "name": bad_name}
    with pytest.raises(ValidationError):
        Sequence(**data)


@pytest.mark.parametrize(
    "good_name",
    ["task10ms_speed_cycle", "_leading_underscore", "CamelCase", "a", "a1b2c3"],
)
def test_sequence_identifier_shaped_name_passes(good_name):
    data = {**VALID_SEQUENCE, "name": good_name}
    seq = Sequence(**data)
    assert seq.name == good_name


# ---------------------------------------------------------------------------
# read_sequences
# ---------------------------------------------------------------------------


class TestReadSequences:
    def test_reads_all_sequence_yaml_under_sequences_dir(self, tmp_path):
        _write_yaml(tmp_path / "sequences" / "task10ms_speed_cycle.yaml", VALID_SEQUENCE)
        other = {
            **VALID_SEQUENCE,
            "name": "task10ms_other_cycle",
            "steps": [{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
        }
        _write_yaml(tmp_path / "sequences" / "task10ms_other_cycle.yaml", other)
        sequences = read_sequences(str(tmp_path))
        assert len(sequences) == 2
        names = {s.name for s in sequences}
        assert names == {"task10ms_speed_cycle", "task10ms_other_cycle"}

    def test_missing_sequences_dir_returns_empty_list(self, tmp_path):
        sequences = read_sequences(str(tmp_path))
        assert sequences == []

    def test_invalid_sequence_yaml_raises_with_offending_filename(self, tmp_path):
        invalid = {"name": "", "task": "Task_10ms", "steps": []}
        bad_path = tmp_path / "sequences" / "Bad.yaml"
        _write_yaml(bad_path, invalid)
        with pytest.raises(ValueError, match="Bad.yaml"):
            read_sequences(str(tmp_path))

    def test_malformed_yaml_syntax_raises_with_offending_filename(self, tmp_path):
        # Syntactically-broken YAML (unclosed bracket) — a yaml.YAMLError, not
        # a pydantic ValidationError, must still surface the filename.
        broken_path = tmp_path / "sequences" / "Broken.yaml"
        broken_path.parent.mkdir(parents=True, exist_ok=True)
        broken_path.write_text("name: task10ms_speed_cycle\nsteps: [swc: Foo, runnable: Bar\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Broken.yaml"):
            read_sequences(str(tmp_path))


# ---------------------------------------------------------------------------
# find_sequence_file
# ---------------------------------------------------------------------------


class TestFindSequenceFile:
    def test_finds_existing_sequence_returns_path(self, tmp_path):
        _write_yaml(tmp_path / "sequences" / "task10ms_speed_cycle.yaml", VALID_SEQUENCE)
        result = find_sequence_file(str(tmp_path), "task10ms_speed_cycle")
        assert result is not None
        assert "task10ms_speed_cycle.yaml" in result

    def test_nonexistent_sequence_returns_none(self, tmp_path):
        (tmp_path / "sequences").mkdir()
        result = find_sequence_file(str(tmp_path), "missing")
        assert result is None

    def test_missing_sequences_dir_returns_none(self, tmp_path):
        result = find_sequence_file(str(tmp_path), "missing")
        assert result is None


# ---------------------------------------------------------------------------
# create_sequence
# ---------------------------------------------------------------------------


class TestCreateSequence:
    def _make_task_with_runnables(self, tmp_path, task_name="Task_10ms", period=0.01, priority=5, mappings=None):
        """Create SWCs/runnables and map them onto a task in the given order.

        mappings: list of (swc, runnable) tuples; each SWC is created once and
        each runnable mapped onto the task in list order via
        map_runnable_to_task (so task.runnables reflects the intended order).
        """
        mappings = mappings or [
            ("SpeedCtrlSWC", "SpeedCtrl_Run"),
            ("SpeedCtrlSWC", "TorqueCalc_Run"),
        ]
        create_task(str(tmp_path), task_name, period=period, priority=priority, partition="PARTITION0")
        created_swcs: set[str] = set()
        for swc, runnable in mappings:
            if swc not in created_swcs:
                create_swc(str(tmp_path), swc, group="body")
                created_swcs.add(swc)
            add_runnable(str(tmp_path), swc, runnable)
            map_runnable_to_task(str(tmp_path), task_name, swc, runnable)
        return mappings

    def test_creates_valid_yaml_and_audits(self, tmp_path):
        self._make_task_with_runnables(tmp_path)
        auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))

        path = create_sequence(
            str(tmp_path),
            "task10ms_speed_cycle",
            "Task_10ms",
            steps=[
                {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                {
                    "swc": "SpeedCtrlSWC",
                    "runnable": "TorqueCalc_Run",
                    "note": "uses filtered speed from the previous step",
                },
            ],
            title="Speed control cycle",
            auditor=auditor,
        )

        expected = tmp_path / "sequences" / "task10ms_speed_cycle.yaml"
        assert expected.exists()
        assert path == str(expected)

        sequences = read_sequences(str(tmp_path))
        assert len(sequences) == 1
        seq = sequences[0]
        assert seq.name == "task10ms_speed_cycle"
        assert seq.task == "Task_10ms"
        assert seq.title == "Speed control cycle"
        assert len(seq.steps) == 2
        assert seq.steps[1].note == "uses filtered speed from the previous step"

        entries = auditor.get_entries()
        assert len(entries) == 1
        assert entries[0]["operation"] == "create_sequence"

    def test_duplicate_sequence_name_raises(self, tmp_path):
        self._make_task_with_runnables(tmp_path)
        create_sequence(
            str(tmp_path),
            "task10ms_speed_cycle",
            "Task_10ms",
            steps=[{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
        )
        with pytest.raises(ValueError, match="task10ms_speed_cycle"):
            create_sequence(
                str(tmp_path),
                "task10ms_speed_cycle",
                "Task_10ms",
                steps=[{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
            )

    def test_a_file_name_another_sequence_holds_takes_a_collision_suffix(self, tmp_path):
        """A file name another sequence holds takes the nearest free collision suffix.

        A file already sitting at sequences/<name>.yaml whose internal YAML `name:` field differs from <name> is
        invisible to the content-based scan (find_sequence_file), and it is no refusal either: the store keeps both
        names, so the new sequence's file takes the suffix and the existing file is never overwritten."""
        self._make_task_with_runnables(tmp_path)
        occupant = tmp_path / "sequences" / "task10ms_speed_cycle.yaml"
        _write_yaml(
            occupant,
            {
                "name": "some_other_internal_name",
                "task": "Task_10ms",
                "steps": [{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
            },
        )
        before = occupant.read_bytes()

        # Content-based scan finds no match (internal name differs).
        assert find_sequence_file(str(tmp_path), "task10ms_speed_cycle") is None

        result = create_sequence(
            str(tmp_path),
            "task10ms_speed_cycle",
            "Task_10ms",
            steps=[{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
        )

        assert Path(result).name == "task10ms_speed_cycle~1.yaml"
        assert find_sequence_file(str(tmp_path), "task10ms_speed_cycle") == result
        assert occupant.read_bytes() == before

    @pytest.mark.parametrize("bad_name", ["a/b", "a\\b", "..", "../evil"])
    def test_path_traversal_name_raises(self, tmp_path, bad_name):
        self._make_task_with_runnables(tmp_path)
        with pytest.raises(ValueError):
            create_sequence(
                str(tmp_path),
                bad_name,
                "Task_10ms",
                steps=[{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
            )

    def test_unknown_task_raises(self, tmp_path):
        self._make_task_with_runnables(tmp_path)
        with pytest.raises(ValueError, match="Missing"):
            create_sequence(
                str(tmp_path),
                "task10ms_speed_cycle",
                "Missing",
                steps=[{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
            )

    def test_step_pair_not_in_task_runnables_raises(self, tmp_path):
        self._make_task_with_runnables(tmp_path)
        create_swc(str(tmp_path), "OtherSWC", group="body")
        add_runnable(str(tmp_path), "OtherSWC", "OtherRun")
        with pytest.raises(ValueError, match="OtherSWC"):
            create_sequence(
                str(tmp_path),
                "task10ms_speed_cycle",
                "Task_10ms",
                steps=[{"swc": "OtherSWC", "runnable": "OtherRun"}],
            )

    def test_out_of_order_steps_raise(self, tmp_path):
        self._make_task_with_runnables(tmp_path)
        with pytest.raises(ValueError, match="order"):
            create_sequence(
                str(tmp_path),
                "task10ms_speed_cycle",
                "Task_10ms",
                steps=[
                    {"swc": "SpeedCtrlSWC", "runnable": "TorqueCalc_Run"},
                    {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                ],
            )

    def test_repeated_step_pair_raises(self, tmp_path):
        self._make_task_with_runnables(tmp_path)
        with pytest.raises(ValueError):
            create_sequence(
                str(tmp_path),
                "task10ms_speed_cycle",
                "Task_10ms",
                steps=[
                    {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                    {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                ],
            )

    def test_valid_subset_preserving_order_accepted(self, tmp_path):
        self._make_task_with_runnables(
            tmp_path,
            mappings=[
                ("SpeedCtrlSWC", "SpeedCtrl_Run"),
                ("SpeedCtrlSWC", "TorqueCalc_Run"),
                ("BrakeSWC", "Brake_Run"),
            ],
        )
        create_sequence(
            str(tmp_path),
            "task10ms_speed_cycle",
            "Task_10ms",
            steps=[
                {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                {"swc": "BrakeSWC", "runnable": "Brake_Run"},
            ],
        )
        assert (tmp_path / "sequences" / "task10ms_speed_cycle.yaml").exists()
        seq = read_sequences(str(tmp_path))[0]
        assert [(s.swc, s.runnable) for s in seq.steps] == [
            ("SpeedCtrlSWC", "SpeedCtrl_Run"),
            ("BrakeSWC", "Brake_Run"),
        ]

    def test_full_task_runnables_in_order_accepted(self, tmp_path):
        mappings = [
            ("SpeedCtrlSWC", "SpeedCtrl_Run"),
            ("SpeedCtrlSWC", "TorqueCalc_Run"),
        ]
        self._make_task_with_runnables(tmp_path, mappings=mappings)
        create_sequence(
            str(tmp_path),
            "task10ms_speed_cycle",
            "Task_10ms",
            steps=[{"swc": swc, "runnable": runnable} for swc, runnable in mappings],
        )
        seq = read_sequences(str(tmp_path))[0]
        assert [(s.swc, s.runnable) for s in seq.steps] == mappings

    def test_stale_task_mapping_unknown_swc_raises(self, tmp_path):
        """create_sequence resolves the SWC of a task entry against the real model.

        A hand-edited task.yaml lists {swc, runnable} whose SWC no longer exists — create_sequence must not just trust
        task.runnables membership."""
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        _write_yaml(
            tmp_path / "tasks" / "Task_10ms.yaml",
            {
                "name": "Task_10ms",
                "period": 0.01,
                "priority": 5,
                "partition": "PARTITION0",
                "runnables": [{"swc": "GhostSWC", "runnable": "GhostRun"}],
            },
        )
        with pytest.raises(ValueError, match="GhostSWC"):
            create_sequence(
                str(tmp_path),
                "task10ms_speed_cycle",
                "Task_10ms",
                steps=[{"swc": "GhostSWC", "runnable": "GhostRun"}],
            )

    def test_stale_task_mapping_unknown_runnable_raises(self, tmp_path):
        """Same as above but the SWC exists, only the runnable doesn't.

        A hand-edited task.yaml mapping onto a real SWC but a runnable name that was never added / has since been
        removed."""
        create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        _write_yaml(
            tmp_path / "tasks" / "Task_10ms.yaml",
            {
                "name": "Task_10ms",
                "period": 0.01,
                "priority": 5,
                "partition": "PARTITION0",
                "runnables": [{"swc": "SpeedCtrlSWC", "runnable": "GhostRun"}],
            },
        )
        with pytest.raises(ValueError, match="GhostRun"):
            create_sequence(
                str(tmp_path),
                "task10ms_speed_cycle",
                "Task_10ms",
                steps=[{"swc": "SpeedCtrlSWC", "runnable": "GhostRun"}],
            )


# ---------------------------------------------------------------------------
# check_sequence_consistency
# ---------------------------------------------------------------------------


def _make_task_with_runnables(tmp_path, task_name="Task_10ms", period=0.01, priority=5, mappings=None):
    """Create SWCs/runnables and map them onto a task in the given order.

    mappings: list of (swc, runnable) tuples; each SWC is created once and
    each runnable mapped onto the task in list order via
    map_runnable_to_task (so task.runnables reflects the intended order).
    """
    mappings = mappings or [
        ("SpeedCtrlSWC", "SpeedCtrl_Run"),
        ("SpeedCtrlSWC", "TorqueCalc_Run"),
    ]
    create_task(str(tmp_path), task_name, period=period, priority=priority, partition="PARTITION0")
    created_swcs: set[str] = set()
    for swc, runnable in mappings:
        if swc not in created_swcs:
            create_swc(str(tmp_path), swc, group="body")
            created_swcs.add(swc)
        add_runnable(str(tmp_path), swc, runnable)
        map_runnable_to_task(str(tmp_path), task_name, swc, runnable)
    return mappings


class TestCheckSequenceConsistency:
    def test_no_sequences_dir_is_consistent(self, tmp_path):
        _make_task_with_runnables(tmp_path)
        result = check_sequence_consistency(str(tmp_path))
        assert result.consistent is True
        assert result.issues == []

    def test_valid_sequence_is_consistent(self, tmp_path):
        _make_task_with_runnables(tmp_path)
        create_sequence(
            str(tmp_path),
            "task10ms_speed_cycle",
            "Task_10ms",
            steps=[
                {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                {"swc": "SpeedCtrlSWC", "runnable": "TorqueCalc_Run"},
            ],
        )
        result = check_sequence_consistency(str(tmp_path))
        assert result.consistent is True
        assert result.issues == []

    def test_valid_ordered_subset_is_consistent(self, tmp_path):
        _make_task_with_runnables(
            tmp_path,
            mappings=[
                ("SpeedCtrlSWC", "SpeedCtrl_Run"),
                ("SpeedCtrlSWC", "TorqueCalc_Run"),
                ("BrakeSWC", "Brake_Run"),
            ],
        )
        create_sequence(
            str(tmp_path),
            "task10ms_speed_cycle",
            "Task_10ms",
            steps=[
                {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                {"swc": "BrakeSWC", "runnable": "Brake_Run"},
            ],
        )
        result = check_sequence_consistency(str(tmp_path))
        assert result.consistent is True
        assert result.issues == []

    def test_duplicate_sequence_name_across_files_raises(self, tmp_path):
        _make_task_with_runnables(tmp_path)
        create_sequence(
            str(tmp_path),
            "task10ms_speed_cycle",
            "Task_10ms",
            steps=[{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
        )
        # Hand-write a second file declaring the same sequence name
        # (bypassing create_sequence's writer-level duplicate guard).
        _write_yaml(
            tmp_path / "sequences" / "task10ms_speed_cycle_dup.yaml",
            {
                "name": "task10ms_speed_cycle",
                "task": "Task_10ms",
                "steps": [{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
            },
        )
        result = check_sequence_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("task10ms_speed_cycle" in issue for issue in result.issues)

    def test_unknown_task_reference_raises(self, tmp_path):
        _make_task_with_runnables(tmp_path)
        _write_yaml(
            tmp_path / "sequences" / "orphan.yaml",
            {
                "name": "orphan",
                "task": "GhostTask",
                "steps": [{"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"}],
            },
        )
        result = check_sequence_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("GhostTask" in issue for issue in result.issues)

    def test_step_not_mapped_to_task_raises(self, tmp_path):
        _make_task_with_runnables(tmp_path)
        create_swc(str(tmp_path), "OtherSWC", group="body")
        add_runnable(str(tmp_path), "OtherSWC", "OtherRun")
        _write_yaml(
            tmp_path / "sequences" / "bad_step.yaml",
            {
                "name": "bad_step",
                "task": "Task_10ms",
                "steps": [{"swc": "OtherSWC", "runnable": "OtherRun"}],
            },
        )
        result = check_sequence_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("OtherSWC" in issue for issue in result.issues)

    def test_stale_task_mapping_unknown_swc_raises(self, tmp_path):
        """check_sequence_consistency resolves the SWC of a task entry against the real model.

        A hand-edited task.yaml lists {swc, runnable} whose SWC no longer exists — check_sequence_consistency must not
        just trust task.runnables membership."""
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        _write_yaml(
            tmp_path / "tasks" / "Task_10ms.yaml",
            {
                "name": "Task_10ms",
                "period": 0.01,
                "priority": 5,
                "partition": "PARTITION0",
                "runnables": [{"swc": "GhostSWC", "runnable": "GhostRun"}],
            },
        )
        _write_yaml(
            tmp_path / "sequences" / "stale.yaml",
            {
                "name": "stale",
                "task": "Task_10ms",
                "steps": [{"swc": "GhostSWC", "runnable": "GhostRun"}],
            },
        )
        result = check_sequence_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("GhostSWC" in issue for issue in result.issues)

    def test_stale_task_mapping_unknown_runnable_raises(self, tmp_path):
        create_swc(str(tmp_path), "SpeedCtrlSWC", group="body")
        create_task(str(tmp_path), "Task_10ms", period=0.01, priority=5, partition="PARTITION0")
        _write_yaml(
            tmp_path / "tasks" / "Task_10ms.yaml",
            {
                "name": "Task_10ms",
                "period": 0.01,
                "priority": 5,
                "partition": "PARTITION0",
                "runnables": [{"swc": "SpeedCtrlSWC", "runnable": "GhostRun"}],
            },
        )
        _write_yaml(
            tmp_path / "sequences" / "stale.yaml",
            {
                "name": "stale",
                "task": "Task_10ms",
                "steps": [{"swc": "SpeedCtrlSWC", "runnable": "GhostRun"}],
            },
        )
        result = check_sequence_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("GhostRun" in issue for issue in result.issues)

    def test_out_of_order_steps_raise(self, tmp_path):
        _make_task_with_runnables(tmp_path)
        _write_yaml(
            tmp_path / "sequences" / "reordered.yaml",
            {
                "name": "reordered",
                "task": "Task_10ms",
                "steps": [
                    {"swc": "SpeedCtrlSWC", "runnable": "TorqueCalc_Run"},
                    {"swc": "SpeedCtrlSWC", "runnable": "SpeedCtrl_Run"},
                ],
            },
        )
        result = check_sequence_consistency(str(tmp_path))
        assert result.consistent is False
        assert any("order" in issue for issue in result.issues)


# ---------------------------------------------------------------------------
# A step is matched by its resolved identity
# ---------------------------------------------------------------------------


class TestTheInstanceIdentity:
    def _model(self, tmp_path, task_runnables: list[dict], *, root: bool = True) -> None:
        from conftest import _core, _root_composition, _swc, _task, _write, element_path

        _core(tmp_path, "CORE0")
        _swc(tmp_path, "Worker", runnables=[{"name": "Run"}, {"name": "Other"}])
        _swc(tmp_path, "Solo", runnables=[{"name": "Run"}, {"name": "Second"}])
        if root:
            _root_composition(
                tmp_path,
                [
                    {"name": "left", "composition": "Pair"},
                    {"name": "right", "composition": "Pair"},
                    {"name": "solo", "type": "Solo"},
                ],
            )
        _write(
            element_path(tmp_path, "compositions", "Pair"),
            {"name": "Pair", "title": "Pair", "members": [{"name": "worker", "type": "Worker"}]},
        )
        _task(tmp_path, "Task_A", runnables=task_runnables)

    def _sequence(self, tmp_path, steps: list[dict]) -> list:
        _write_yaml(tmp_path / "sequences" / "cycle.yaml", {"name": "cycle", "task": "Task_A", "steps": steps})
        return list(check_sequence_consistency(str(tmp_path)).records)

    def test_a_step_is_matched_to_its_task_entry_across_selectors(self, tmp_path):
        self._model(tmp_path, [{"swc": "Solo", "runnable": "Run"}, {"instance": "solo", "runnable": "Second"}])
        assert (
            self._sequence(tmp_path, [{"instance": "solo", "runnable": "Run"}, {"swc": "Solo", "runnable": "Second"}])
            == []
        )

    def test_the_order_is_judged_on_the_resolved_identity(self, tmp_path):
        self._model(tmp_path, [{"swc": "Solo", "runnable": "Run"}, {"swc": "Solo", "runnable": "Second"}])
        records = self._sequence(
            tmp_path, [{"instance": "solo", "runnable": "Second"}, {"instance": "solo", "runnable": "Run"}]
        )
        assert [r.rule for r in records] == ["sequence.step-order"]

    def test_one_runnable_shown_twice_through_two_spellings_is_a_resolved_duplicate(self, tmp_path):
        self._model(tmp_path, [{"swc": "Solo", "runnable": "Run"}])
        records = self._sequence(
            tmp_path, [{"swc": "Solo", "runnable": "Run"}, {"instance": "solo", "runnable": "Run"}]
        )
        assert [r.rule for r in records] == ["sequence.resolved-duplicate-step"]
        assert len(records[0].subjects) == 2

    def test_left_and_right_worker_are_two_steps(self, tmp_path):
        self._model(
            tmp_path, [{"instance": "left/worker", "runnable": "Run"}, {"instance": "right/worker", "runnable": "Run"}]
        )
        steps = [{"instance": "left/worker", "runnable": "Run"}, {"instance": "right/worker", "runnable": "Run"}]
        assert self._sequence(tmp_path, steps) == []

    def test_a_step_of_another_occurrence_is_not_mapped(self, tmp_path):
        self._model(tmp_path, [{"instance": "left/worker", "runnable": "Run"}])
        records = self._sequence(tmp_path, [{"instance": "right/worker", "runnable": "Run"}])
        assert [r.rule for r in records] == ["sequence.step-not-mapped"]
        assert records[0].subjects[0].path[-1].value == {"swc": None, "instance": "right/worker", "runnable": "Run"}

    def test_the_runnable_is_checked_on_the_resolved_type(self, tmp_path):
        self._model(tmp_path, [{"instance": "left/worker", "runnable": "Missing"}])
        records = self._sequence(tmp_path, [{"instance": "left/worker", "runnable": "Missing"}])
        assert [r.rule for r in records] == ["sequence.step-unknown-runnable"]
        assert "on SWC 'Worker'" in records[0].message

    @pytest.mark.parametrize(
        ("step", "rule", "field", "message"),
        [
            (
                {"swc": "Worker", "runnable": "Run"},
                "sequence.step-instance-ambiguous",
                "swc",
                "Sequence 'cycle' step 'Worker.Run' names SWC 'Worker' by `swc`, but that type is placed 2 times "
                "below the root composition: 'left/worker', 'right/worker'. Name the occurrence by `instance`.",
            ),
            (
                {"instance": "left/nope", "runnable": "Run"},
                "sequence.step-instance-unknown",
                "instance",
                "Sequence 'cycle' step 'left/nope.Run' names instance 'left/nope', which does not walk from the root "
                "composition to a member: a segment names no member, or descends into a component or an undeclared "
                "composition.",
            ),
            (
                {"instance": "left", "runnable": "Run"},
                "sequence.step-instance-not-a-component",
                "instance",
                "Sequence 'cycle' step 'left.Run' names instance 'left', which ends on a composition member that "
                "instantiates a composition, not a component.",
            ),
        ],
    )
    def test_each_refusal(self, tmp_path, step, rule, field, message):
        from swcraft_tools.diagnostics import Subject, field_step, member_step

        self._model(tmp_path, [{"instance": "left/worker", "runnable": "Run"}])
        (record,) = self._sequence(tmp_path, [step])
        assert record.rule == rule
        assert record.message == message
        triple = {"swc": step.get("swc"), "instance": step.get("instance"), "runnable": "Run"}
        assert record.subjects == (
            Subject("sequences", "cycle", (field_step("steps"), member_step(triple), field_step(field))),
        )

    def test_an_undeclared_swc_placed_twice_is_the_unknown_swc_finding_and_not_an_ambiguity(self, tmp_path):
        """An `swc` naming an undeclared component is `sequence.step-unknown-swc`'s.

        The order between the two findings: this holds although the resolver would refuse it as placed twice."""
        from conftest import _root_composition

        self._model(tmp_path, [{"swc": "Ghost", "runnable": "Run"}])
        _root_composition(tmp_path, [{"name": "ghost_1", "type": "Ghost"}, {"name": "ghost_2", "type": "Ghost"}])
        records = self._sequence(tmp_path, [{"swc": "Ghost", "runnable": "Run"}])
        assert [r.rule for r in records] == ["sequence.step-unknown-swc"]

    def test_a_type_placed_more_than_64_times_lists_the_first_64_paths_and_the_exact_count(self, tmp_path):
        from conftest import _root_composition

        self._model(tmp_path, [{"instance": "w0", "runnable": "Run"}])
        _root_composition(tmp_path, [{"name": f"w{index}", "type": "Worker"} for index in range(70)])
        (record,) = self._sequence(tmp_path, [{"swc": "Worker", "runnable": "Run"}])
        assert record.rule == "sequence.step-instance-ambiguous"
        assert "placed 70 times below the root composition (the first 64 shown): 'w0', 'w1'," in record.message
        assert record.message.count("'w") == 64
        assert "'w63'" in record.message and "'w64'" not in record.message

    def test_under_an_undefined_walk_the_identity_is_syntactic(self, tmp_path):
        self._model(tmp_path, [{"swc": "Solo", "runnable": "Run"}], root=False)
        records = self._sequence(tmp_path, [{"instance": "solo", "runnable": "Run"}])
        # The step's syntactic triple is not the task entry's, and nothing is refused.
        assert [r.rule for r in records] == ["sequence.step-not-mapped"]
