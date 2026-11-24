"""The allocation experiment's configuration file and projections (``swcraft_tools.allocation``).

Every model is synthetic (``carma_models.py``); the projections are checked one class, one reason and
one status at a time. The search over them is ``test_allocation_search.py``'s.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from carma_models import (
    CALL_PORT,
    DATA_ELEMENT,
    DATA_PORT,
    SERVER_OPERATION,
    SERVER_PORT,
    THREE_CORES_LAYOUT,
    CarmaModel,
    asynchronous_call,
    call_returned_runnable,
    config_document,
    data_runnable,
    init_runnable,
    runnable,
    server_runnable,
    sr_ports,
    three_cores,
    with_unmapped_server,
)
from swcraft_tools.allocation import (
    ADMISSIBLE,
    CLASS_HARD,
    CLASS_LOCKED,
    CLASS_REMOVABLE,
    CLASS_SAFETY,
    DEFAULT_NAME,
    DEFAULT_TIME_LIMIT_S,
    FREQUENCY_NOT_ESTABLISHED,
    NOT_FREE_EXTERNAL,
    NOT_FREE_NO_RUNNABLE,
    NOT_FREE_NO_TASKED_RUNNABLE,
    NOT_FREE_PLACED_NOWHERE,
    NOT_FREE_TWO_PARTITIONS,
    NOT_FREE_UNDEFINED_WALK,
    UNSIZED_EXTERNAL_INTERFACE,
    UNSIZED_NO_MAPPING,
    UNSIZED_SHAPE,
    UNSIZED_UNDECLARED,
    AllocationConfig,
    ExperimentError,
    Projection,
    load_config,
    model_digest,
    parse_config,
    read_projection,
    runnable_kind,
)
from swcraft_tools.execution_budget import parse_budget
from swcraft_tools.resource_analysis import analyze_resource_budgets, runnable_utilization
from swcraft_tools.schema import Runnable
from swcraft_tools.validator import check_cross_layer_consistency


def _experiment(**overrides: object):
    return parse_config(config_document(name="relieve", **overrides))


def _project(model: CarmaModel, tmp_path: Path, **experiment: object) -> Projection:
    root = model.write(tmp_path / "model")
    return read_projection(root, _experiment(**experiment))


def _rules(projection: Projection, component: str, partition: str) -> list[str | None]:
    return [reason.rule for reason in projection.row(component).target(partition).reasons]


def _classes(projection: Projection, component: str, partition: str) -> list[str]:
    return [reason.klass for reason in projection.row(component).target(partition).reasons]


# Fixture (a)


class TestThreeCores:
    def test_the_starting_model_passes_the_gate(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "model")
        result = check_cross_layer_consistency(str(root))
        assert result.consistent, [record.message for record in result.records]

    def test_every_component_is_free_in_its_partition_at_its_partitions_level(self, tmp_path: Path) -> None:
        projection = _project(three_cores(), tmp_path)
        for partition, components in THREE_CORES_LAYOUT.items():
            for name in components:
                placement = projection.placements[name]
                assert placement.free, placement.reasons
                assert placement.partition == partition
                assert placement.core == f"C{partition[1]}"
                assert placement.level == ("QM" if partition == "P1" else "B")

    def test_b_moves_between_p2_and_p3_and_qm_moves_to_p3_only(self, tmp_path: Path) -> None:
        projection = _project(three_cores(), tmp_path)
        assert projection.row("B1").target("P3").klass == ADMISSIBLE
        assert projection.row("B5").target("P2").klass == ADMISSIBLE
        assert projection.row("Q1").target("P3").klass == ADMISSIBLE
        # B -> P1: the partition is rated below the component.
        assert projection.row("B1").target("P1").klass == CLASS_SAFETY
        assert "rule:safety.component-above-partition" in _rules(projection, "B1", "P1")
        # QM -> P2: rated above it without the partition's coexistence.
        assert projection.row("Q1").target("P2").klass == CLASS_SAFETY
        assert "rule:safety.component-below-partition" in _rules(projection, "Q1", "P2")

    def test_the_domain_is_the_current_partition_then_the_admissible_ones(self, tmp_path: Path) -> None:
        projection = _project(three_cores(), tmp_path)
        assert projection.row("B1").domain == ("P2", "P3")
        assert projection.row("Q1").domain == ("P1", "P3")

    def test_the_target_task_is_the_task_of_the_runnables_period(self, tmp_path: Path) -> None:
        projection = _project(three_cores(), tmp_path)
        assert dict(projection.row("B1").target("P3").target_tasks) == {"B1_Run": "T_P3_10ms"}
        assert dict(projection.row("B3").target("P3").target_tasks) == {"B3_Run": "T_P3_20ms"}

    @pytest.mark.parametrize(
        ("drop", "rule"),
        [
            ("partition", "rule:safety.component-below-partition"),
            ("task", "rule:safety.task-mixed-asil"),
        ],
    )
    def test_qm_into_p3_needs_both_of_p3s_arguments(self, tmp_path: Path, drop: str, rule: str) -> None:
        model = three_cores()
        if drop == "partition":
            del model.get("hardware/P3.yaml")["coexistence"]
        else:
            del model.get("tasks/T_P3_10ms.yaml")["coexistence"]
        projection = _project(model, tmp_path)
        row = projection.row("Q1").target("P3")
        assert row.klass == CLASS_SAFETY
        assert [reason.rule for reason in row.reasons] == [rule]

    def test_a_component_nothing_rates_is_judged_as_qm(self, tmp_path: Path) -> None:
        model = three_cores()
        del model.get("swc/carma/Q1.yaml")["asil"]
        projection = _project(model, tmp_path)
        assert projection.placements["Q1"].level == "QM"
        assert projection.row("Q1").target("P3").klass == ADMISSIBLE
        assert "rule:safety.component-below-partition" in _rules(projection, "Q1", "P2")


# Fixture (b): one per class and reason


def _with_fourth_partition(
    *, core_capability: str | None, asil: str | None, enforcement: str = "strict", slow_budget: bool = True
) -> CarmaModel:
    """Fixture (a) plus ``P4`` on a core ``C4`` of its own, with an empty 10 ms and 20 ms task."""
    model = three_cores()
    model.core("C4", asil_capability=core_capability, enforcement=enforcement)
    model.partition("P4", "C4", asil=asil, coexistence="Memory partitioning argument (synthetic).")
    model.task("T_P4_10ms", "P4", 0.01, runnables=[], wcet="500us")
    model.task("T_P4_20ms", "P4", 0.02, runnables=[], wcet="500us" if slow_budget else None)
    return model


class TestTheFreedomTable:
    def test_hard_an_unrated_partition_on_a_core_below_the_component(self, tmp_path: Path) -> None:
        projection = _project(_with_fourth_partition(core_capability="QM", asil=None), tmp_path)
        row = projection.row("B1").target("P4")
        assert row.klass == CLASS_HARD
        assert [(reason.klass, reason.rule) for reason in row.reasons] == [(CLASS_HARD, None)]
        assert "core 'C4' is declared able to host ASIL QM, below the component's ASIL B" in row.reasons[0].text

    def test_an_unrated_target_is_admissible_and_listed_as_an_assumption(self, tmp_path: Path) -> None:
        projection = _project(_with_fourth_partition(core_capability="QM", asil=None), tmp_path)
        row = projection.row("Q1").target("P4")
        assert row.admissible and row.unrated
        assert "moving 'Q1' into partition 'P4' is admissible: an unrated partition" in projection.data_gaps
        assert "P4" in projection.row("Q1").domain

    def test_hard_a_budgeted_memory_the_target_core_cannot_reach(self, tmp_path: Path) -> None:
        model = three_cores()
        model.memory("M1", accessible=["C2"])
        model.get("swc/carma/B1.yaml")["memory"] = {"M1": 64}
        projection = _project(model, tmp_path)
        row = projection.row("B1").target("P3")
        assert row.klass == CLASS_HARD
        assert [reason.rule for reason in row.reasons] == ["rule:hardware.unreachable-memory"]

    def test_a_zero_budget_is_no_use_of_the_memory(self, tmp_path: Path) -> None:
        model = three_cores()
        model.memory("M1", accessible=["C2"])
        model.get("swc/carma/B1.yaml")["memory"] = {"M1": 0}
        projection = _project(model, tmp_path)
        assert projection.row("B1").target("P3").admissible

    def test_safety_rated_below_the_component(self, tmp_path: Path) -> None:
        projection = _project(three_cores(), tmp_path)
        assert _rules(projection, "B5", "P1")[0] == "rule:safety.component-above-partition"
        assert set(_classes(projection, "B5", "P1")) == {CLASS_SAFETY}

    def test_safety_rated_above_without_the_partitions_coexistence(self, tmp_path: Path) -> None:
        model = three_cores()
        model.get("tasks/T_P2_10ms.yaml")["coexistence"] = "Execution-time argument (synthetic)."
        projection = _project(model, tmp_path)
        assert _rules(projection, "Q1", "P2") == ["rule:safety.component-below-partition"]

    def test_safety_a_target_task_that_would_mix_levels(self, tmp_path: Path) -> None:
        model = three_cores()
        del model.get("tasks/T_P3_10ms.yaml")["coexistence"]
        projection = _project(model, tmp_path)
        (reason,) = projection.row("Q1").target("P3").reasons
        assert reason.rule == "rule:safety.task-mixed-asil"
        assert "task 'T_P3_10ms' runs 'B5' (ASIL B), 'B6' (ASIL B)" in reason.text
        # The 20 ms task keeps its argument, so a 20 ms QM runnable is admissible.
        assert projection.row("Q3").target("P3").admissible

    def test_safety_a_budgeted_memory_below_the_targets_level(self, tmp_path: Path) -> None:
        model = three_cores()
        model.memory("M2", accessible=["C1", "C2", "C3"], asil_capability="QM")
        model.get("swc/carma/Q1.yaml")["memory"] = {"M2": 64}
        projection = _project(model, tmp_path)
        row = projection.row("Q1").target("P3")
        assert row.klass == CLASS_SAFETY
        assert [reason.rule for reason in row.reasons] == ["rule:safety.partition-memory-below-level"]

    def test_removable_no_task_of_the_period(self, tmp_path: Path) -> None:
        model = _with_fourth_partition(core_capability="B", asil="B")
        model.drop("tasks/T_P4_20ms.yaml")
        projection = _project(model, tmp_path)
        row = projection.row("B3").target("P4")
        assert row.klass == CLASS_REMOVABLE
        assert row.reasons[0].text == "partition 'P4' has no task of period 0.02 s declaring a budget"
        assert projection.row("B1").target("P4").admissible

    def test_removable_a_task_of_the_period_without_a_budget(self, tmp_path: Path) -> None:
        model = _with_fourth_partition(core_capability="B", asil="B", enforcement="advisory", slow_budget=False)
        projection = _project(model, tmp_path)
        assert projection.row("B3").target("P4").klass == CLASS_REMOVABLE
        assert dict(projection.row("B1").target("P4").target_tasks) == {"B1_Run": "T_P4_10ms"}

    def test_the_target_task_is_the_first_by_name_of_several(self, tmp_path: Path) -> None:
        model = three_cores()
        model.task("T_P3_10ms_a", "P3", 0.01, runnables=[], wcet="500us", coexistence="Argument (synthetic).")
        projection = _project(model, tmp_path)
        assert dict(projection.row("B1").target("P3").target_tasks) == {"B1_Run": "T_P3_10ms"}

    def test_locked_by_name(self, tmp_path: Path) -> None:
        projection = _project(three_cores(), tmp_path, locked=["B1"])
        row = projection.row("B1").target("P3")
        assert row.klass == CLASS_LOCKED
        assert projection.row("B1").domain == ("P2",)

    def test_locked_outside_allowed_partitions(self, tmp_path: Path) -> None:
        projection = _project(three_cores(), tmp_path, allowed_partitions={"B5": ["P1"]})
        assert projection.row("B5").target("P2").klass == CLASS_LOCKED
        assert projection.row("B5").domain == ("P3",)

    def test_a_row_with_two_classes_lists_both_and_takes_the_higher(self, tmp_path: Path) -> None:
        projection = _project(three_cores(), tmp_path, locked=["B1"])
        row = projection.row("B1").target("P1")
        assert CLASS_SAFETY in _classes(projection, "B1", "P1")
        assert CLASS_LOCKED in _classes(projection, "B1", "P1")
        assert row.klass == CLASS_SAFETY

    def test_hard_outranks_every_other_class(self, tmp_path: Path) -> None:
        projection = _project(_with_fourth_partition(core_capability="QM", asil="QM"), tmp_path, locked=["B1"])
        classes = _classes(projection, "B1", "P4")
        assert {CLASS_HARD, CLASS_SAFETY, CLASS_LOCKED} <= set(classes)
        assert projection.row("B1").target("P4").klass == CLASS_HARD


# Fixture (c): one per *not free* status


def _one_partition(*components: tuple[str, list[dict], int], task_runs: list[tuple[str, str]], **task) -> CarmaModel:
    model = CarmaModel()
    model.core("C1")
    model.partition("P1", "C1")
    model.partition("P2", "C1")
    for name, runnables, placed in components:
        model.component(name, runnables=runnables, placed=placed)
    model.task("T_P1_10ms", "P1", 0.01, runnables=task_runs, **task)
    model.task("T_P2_10ms", "P2", 0.01, runnables=[], wcet="500us")
    return model


class TestNotFree:
    def test_a_type_placed_twice(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run")], 2), task_runs=[("A", "A_Run")])
        placement = _project(model, tmp_path).placements["A"]
        assert not placement.free and placement.partition is None
        assert placement.reasons == ("placed 2 times (a move takes a component placed exactly once)",)

    def test_a_type_placed_nowhere(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run")], 0), task_runs=[("A", "A_Run")])
        assert _project(model, tmp_path).placements["A"].reasons == (NOT_FREE_PLACED_NOWHERE,)

    def test_a_runnable_without_a_cost_is_a_data_gap(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run", wcet=None)], 1), task_runs=[("A", "A_Run")])
        projection = _project(model, tmp_path)
        placement = projection.placements["A"]
        assert placement.reasons == ("data gap: runnable 'A_Run' declares no wcet",)
        # Its partition is still established: the link graph can read it.
        assert placement.partition == "P1"
        assert "component 'A' is not free: data gap: runnable 'A_Run' declares no wcet" in projection.data_gaps

    def test_a_runnable_in_no_task(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run")], 1), task_runs=[])
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == ("runnable 'A_Run' runs in no task",)
        assert placement.partition is None

    def test_a_runnable_with_no_event_in_no_task(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run", period=None)], 1), task_runs=[])
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == ("runnable 'A_Run' runs in no task",)
        assert placement.partition is None

    def test_a_runnable_with_no_event_in_a_task_has_no_timing_event(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run", period=None)], 1), task_runs=[("A", "A_Run")])
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == ("runnable 'A_Run' has no timing event, so its period is not defined",)
        assert placement.partition == "P1"

    def test_runnables_in_tasks_of_two_partitions(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_One"), runnable("A_Two")], 1), task_runs=[("A", "A_One")])
        model.get("tasks/T_P2_10ms.yaml")["runnables"] = [{"swc": "A", "runnable": "A_Two"}]
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (NOT_FREE_TWO_PARTITIONS,)
        assert placement.partition is None

    def test_a_task_without_a_budget_on_an_advisory_core(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run")], 1), task_runs=[("A", "A_Run")], wcet=None)
        model.core("C1", enforcement="advisory")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == ("data gap: task 'T_P1_10ms' declares no budget (wcet)",)

    def test_an_undefined_composition_walk(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run")], 1), task_runs=[("A", "A_Run")])
        root = model.write(tmp_path / "model")
        composition = root / "compositions" / "Root.yaml"
        composition.write_text(
            composition.read_text(encoding="utf-8").replace("root: true", "root: false"), encoding="utf-8", newline="\n"
        )
        placement = read_projection(root, _experiment()).placements["A"]
        assert placement.reasons[0] == NOT_FREE_UNDEFINED_WALK

    def test_a_component_with_no_runnable(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [], 1), task_runs=[])
        assert _project(model, tmp_path).placements["A"].reasons == (NOT_FREE_NO_RUNNABLE,)

    def test_an_external_stub(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run")], 1), task_runs=[("A", "A_Run")])
        model.component("Ext", runnables=[])
        model.get("swc/carma/Ext.yaml")["external"] = True
        placement = _project(model, tmp_path).placements["Ext"]
        assert placement.reasons == (NOT_FREE_EXTERNAL,)
        assert placement.partition is None

    def test_a_not_free_component_has_no_target_rows(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run", wcet=None)], 1), task_runs=[("A", "A_Run")])
        projection = _project(model, tmp_path)
        assert projection.row("A").targets == ()
        assert projection.row("A").domain == ()


#: The assumption lines for an initialization and a server runnable in no task, for the tests below.
_INIT_ASSUMPTION = (
    "initialization runnable 'A_Init' of 'A' runs in no task: activated once at start "
    "(`Rte_Init` or an initialization task), outside the steady-state load"
)
_SERVER_IN_NO_TASK = (
    "server runnable 'A_Srv' runs in its callers' tasks (a direct call), whose cost the experiment does not model"
)


def _init_in_task(runnable_name: str, task: str) -> str:
    return (
        f"initialization runnable '{runnable_name}' runs in task '{task}'; "
        "moving an initialization mapping is not modelled"
    )


def _server_in_task(runnable_name: str, task: str) -> str:
    return (
        f"server runnable '{runnable_name}' runs in task '{task}' at its callers' rate, "
        "which the experiment does not model"
    )


class TestInitializationAndServerRunnables:
    def test_an_unmapped_initialization_runnable_beside_a_mapped_periodic_one(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [runnable("A_Run"), init_runnable("A_Init", wcet=None)], 1), task_runs=[("A", "A_Run")]
        )
        projection = _project(model, tmp_path)
        placement = projection.placements["A"]
        assert placement.free and placement.reasons == ()
        assert placement.partition == "P1"
        assert placement.assumptions == (_INIT_ASSUMPTION,)
        assert [each.runnable for each in placement.runnables] == ["A_Run"]
        assert projection.data_gaps.count(_INIT_ASSUMPTION) == 1
        assert not any("declares no wcet" in gap for gap in projection.data_gaps)

    def test_the_assumption_follows_the_components_reasons(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [runnable("A_Run", wcet=None), init_runnable("A_Init")], 1), task_runs=[("A", "A_Run")]
        )
        projection = _project(model, tmp_path)
        assert projection.placements["A"].reasons == ("data gap: runnable 'A_Run' declares no wcet",)
        index = projection.data_gaps.index("component 'A' is not free: data gap: runnable 'A_Run' declares no wcet")
        assert projection.data_gaps[index + 1] == _INIT_ASSUMPTION

    def test_an_unmapped_server_runnable_beside_a_mapped_periodic_one(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [runnable("A_Run"), server_runnable("A_Srv", wcet=None)], 1), task_runs=[("A", "A_Run")]
        ).provide_server_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_SERVER_IN_NO_TASK,)
        assert not placement.free
        assert placement.partition == "P1"
        assert placement.assumptions == ()
        assert [each.runnable for each in placement.runnables] == ["A_Run"]

    def test_a_mapped_server_runnable(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [runnable("A_Run"), server_runnable("A_Srv", wcet=None)], 1),
            task_runs=[("A", "A_Run"), ("A", "A_Srv")],
        ).provide_server_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_server_in_task("A_Srv", "T_P1_10ms"),)
        assert placement.partition == "P1"
        assert [each.runnable for each in placement.runnables] == ["A_Run"]

    def test_a_server_runnable_with_an_init_event_too_is_a_server(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [runnable("A_Run"), server_runnable("A_Srv", init=True)], 1), task_runs=[("A", "A_Run")]
        ).provide_server_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_SERVER_IN_NO_TASK,)
        assert placement.assumptions == ()

    def test_a_mapped_initialization_runnable_gives_no_data_gap(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [init_runnable("A_Init", wcet=None)], 1), task_runs=[("A", "A_Init")])
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_init_in_task("A_Init", "T_P1_10ms"),)
        assert placement.partition == "P1"
        assert placement.runnables == ()

    def test_a_mapped_initialization_runnable_in_a_task_without_a_budget(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [init_runnable("A_Init", wcet=None)], 1), task_runs=[("A", "A_Init")], wcet=None)
        model.core("C1", enforcement="advisory")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (
            _init_in_task("A_Init", "T_P1_10ms"),
            "data gap: task 'T_P1_10ms' declares no budget (wcet)",
        )

    def test_only_unmapped_initialization_and_server_runnables(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [init_runnable("A_Init"), server_runnable("A_Srv")], 1), task_runs=[]
        ).provide_server_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_SERVER_IN_NO_TASK, NOT_FREE_NO_TASKED_RUNNABLE)
        assert placement.assumptions == (_INIT_ASSUMPTION,)
        assert placement.partition is None

    def test_only_an_unmapped_server_runnable(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [server_runnable("A_Srv")], 1), task_runs=[]).provide_server_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_SERVER_IN_NO_TASK, NOT_FREE_NO_TASKED_RUNNABLE)
        assert placement.partition is None

    def test_only_an_unmapped_initialization_runnable(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [init_runnable("A_Init")], 1), task_runs=[])
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (NOT_FREE_NO_TASKED_RUNNABLE,)
        assert placement.assumptions == (_INIT_ASSUMPTION,)
        assert placement.partition is None

    def test_an_unmapped_initialization_runnable_beside_an_unmapped_periodic_one(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run"), init_runnable("A_Init")], 1), task_runs=[])
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == ("runnable 'A_Run' runs in no task",)
        assert placement.assumptions == (_INIT_ASSUMPTION,)
        assert placement.partition is None

    def test_a_mapped_initialization_runnable_in_another_partitions_task(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [runnable("A_Run"), init_runnable("A_Init")], 1), task_runs=[("A", "A_Run")])
        model.get("tasks/T_P2_10ms.yaml")["runnables"] = [{"swc": "A", "runnable": "A_Init"}]
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_init_in_task("A_Init", "T_P2_10ms"), NOT_FREE_TWO_PARTITIONS)
        assert placement.partition is None

    def test_a_mapped_server_runnable_in_a_task_without_a_budget(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [server_runnable("A_Srv", wcet=None)], 1), task_runs=[("A", "A_Srv")], wcet=None
        ).provide_server_port("A")
        model.core("C1", enforcement="advisory")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (
            _server_in_task("A_Srv", "T_P1_10ms"),
            "data gap: task 'T_P1_10ms' declares no budget (wcet)",
        )
        assert placement.runnables == ()

    @pytest.mark.parametrize(
        "other",
        [{"type": "operation", "port": SERVER_PORT, "operation": SERVER_OPERATION}, {"type": "init"}],
        ids=["an operation event", "an init event"],
    )
    def test_a_timing_event_beside_another_makes_a_periodic_runnable(
        self, tmp_path: Path, other: dict[str, Any]
    ) -> None:
        events = [{"type": "timing", "period": 0.01}, other]
        mapped = _one_partition(
            ("A", [runnable("A_Run", events=events)], 1), task_runs=[("A", "A_Run")]
        ).provide_server_port("A")
        placement = _project(mapped, tmp_path / "mapped").placements["A"]
        assert placement.free and placement.reasons == () and placement.assumptions == ()
        (placed,) = placement.runnables
        assert placed.runnable == "A_Run" and placed.share == runnable_utilization(parse_budget("100us"), 0.01)
        unmapped = _one_partition(("A", [runnable("A_Run", events=events)], 1), task_runs=[]).provide_server_port("A")
        placement = _project(unmapped, tmp_path / "unmapped").placements["A"]
        assert placement.reasons == ("runnable 'A_Run' runs in no task",)
        assert placement.assumptions == ()
        assert placement.partition is None

    def test_an_external_stub_with_an_unmapped_initialization_runnable_gets_only_the_external_reason(
        self, tmp_path: Path
    ) -> None:
        """The no-tasked-runnable reason is never appended for an external stub; the assumption line is."""
        model = _one_partition(("A", [runnable("A_Run")], 1), task_runs=[("A", "A_Run")])
        model.component("Ext", runnables=[init_runnable("Ext_Init")])
        model.get("swc/carma/Ext.yaml")["external"] = True
        placement = _project(model, tmp_path).placements["Ext"]
        assert placement.reasons == (NOT_FREE_EXTERNAL,)
        assert placement.assumptions == (
            "initialization runnable 'Ext_Init' of 'Ext' runs in no task: activated once at start "
            "(`Rte_Init` or an initialization task), outside the steady-state load",
        )
        assert placement.partition is None

    def test_no_runnable_and_an_external_stub_keep_their_reasons(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [], 1), task_runs=[])
        model.component("Ext", runnables=[])
        model.get("swc/carma/Ext.yaml")["external"] = True
        placements = _project(model, tmp_path).placements
        assert placements["A"].reasons == (NOT_FREE_NO_RUNNABLE,)
        assert placements["Ext"].reasons == (NOT_FREE_EXTERNAL,)


def _data_in_task(runnable_name: str, task: str) -> str:
    return (
        f"data-received runnable '{runnable_name}' runs in task '{task}' at its data's arrival rate, "
        "which the model does not hold"
    )


_TIMING = {"type": "timing", "period": 0.01}
_OPERATION = {"type": "operation", "port": SERVER_PORT, "operation": SERVER_OPERATION}
_DATA = {"type": "data", "port": DATA_PORT, "element": DATA_ELEMENT}


class TestDataReceivedRunnables:
    """The ``data-received`` class: never moved and never costed."""

    @pytest.mark.parametrize(
        ("events", "kind"),
        [
            ([_DATA], "data-received"),
            ([_DATA, _DATA], "data-received"),
            ([{"type": "init"}, _DATA], "data-received"),
            ([_DATA, _TIMING], "periodic"),
            ([_TIMING, _DATA, _OPERATION], "periodic"),
            ([_DATA, _OPERATION], "server"),
            ([_OPERATION, _DATA], "server"),
            ([{"type": "init"}], "initialization"),
            ([], "event-less"),
        ],
        ids=[
            "data only",
            "two data events",
            "an init event beside",
            "a timing event beside",
            "timing, data and operation",
            "an operation event after",
            "an operation event before",
            "init only, unchanged",
            "no event, unchanged",
        ],
    )
    def test_the_class_read_from_the_events(self, events: list[dict[str, Any]], kind: str) -> None:
        assert runnable_kind(Runnable.model_validate({"name": "R", "events": events})) == kind

    def test_a_mapped_data_received_runnable_beside_a_mapped_periodic_one(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [runnable("A_Run"), data_runnable("A_Data", wcet=None)], 1),
            task_runs=[("A", "A_Run"), ("A", "A_Data")],
        ).require_data_port("A")
        projection = _project(model, tmp_path)
        placement = projection.placements["A"]
        # Never costed: no wcet data gap for it, and the share counts the periodic runnable alone.
        assert placement.reasons == (_data_in_task("A_Data", "T_P1_10ms"),)
        assert not placement.free
        assert placement.partition == "P1"
        assert placement.assumptions == ()
        assert [each.runnable for each in placement.runnables] == ["A_Run"]
        assert projection.row("A").targets == ()

    def test_a_mapped_data_received_runnable_alone_is_never_placed(self, tmp_path: Path) -> None:
        model = _one_partition(("A", [data_runnable("A_Data")], 1), task_runs=[("A", "A_Data")]).require_data_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_data_in_task("A_Data", "T_P1_10ms"),)
        assert not placement.free
        assert placement.partition == "P1"
        assert placement.runnables == ()

    def test_a_mapped_data_received_runnable_in_a_task_without_a_budget(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [data_runnable("A_Data", wcet=None)], 1), task_runs=[("A", "A_Data")], wcet=None
        ).require_data_port("A")
        model.core("C1", enforcement="advisory")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (
            _data_in_task("A_Data", "T_P1_10ms"),
            "data gap: task 'T_P1_10ms' declares no budget (wcet)",
        )

    def test_an_unmapped_data_received_runnable_runs_in_no_task(self, tmp_path: Path) -> None:
        # Unlike a server it needs a task: nobody calls it from another task.
        model = _one_partition(
            ("A", [runnable("A_Run"), data_runnable("A_Data")], 1), task_runs=[("A", "A_Run")]
        ).require_data_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == ("runnable 'A_Data' runs in no task",)
        assert not placement.free
        assert placement.partition is None
        assert placement.assumptions == ()

    def test_an_unmapped_data_received_runnable_alone_is_no_initialization_or_server_case(self, tmp_path: Path) -> None:
        # runs_in_tasks is false, so NOT_FREE_NO_TASKED_RUNNABLE's "(initialization and server runnables only)"
        # stays true: it is not appended.
        model = _one_partition(("A", [data_runnable("A_Data")], 1), task_runs=[]).require_data_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == ("runnable 'A_Data' runs in no task",)
        assert NOT_FREE_NO_TASKED_RUNNABLE not in placement.reasons
        assert placement.partition is None

    def test_an_unmapped_data_received_runnable_beside_an_unmapped_server(self, tmp_path: Path) -> None:
        model = (
            _one_partition(("A", [server_runnable("A_Srv"), data_runnable("A_Data")], 1), task_runs=[])
            .provide_server_port("A")
            .require_data_port("A")
        )
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (
            "server runnable 'A_Srv' runs in its callers' tasks (a direct call), "
            "whose cost the experiment does not model",
            "runnable 'A_Data' runs in no task",
        )
        assert placement.partition is None

    def test_a_timing_event_beside_keeps_it_periodic_placed_and_costed(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [data_runnable("A_Run", also=[_TIMING])], 1), task_runs=[("A", "A_Run")]
        ).require_data_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.free and placement.reasons == ()
        (placed,) = placement.runnables
        assert placed.runnable == "A_Run" and placed.share == runnable_utilization(parse_budget("100us"), 0.01)

    def test_an_operation_event_without_a_timing_event_keeps_it_a_server(self, tmp_path: Path) -> None:
        model = (
            _one_partition(
                ("A", [runnable("A_Run"), data_runnable("A_Srv", also=[_OPERATION])], 1),
                task_runs=[("A", "A_Run"), ("A", "A_Srv")],
            )
            .provide_server_port("A")
            .require_data_port("A")
        )
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_server_in_task("A_Srv", "T_P1_10ms"),)


def _returned_in_task(runnable_name: str, task: str) -> str:
    return (
        f"call-returned runnable '{runnable_name}' runs in task '{task}' when an asynchronous call returns, "
        "at a rate the model does not hold"
    )


_RETURNED = {"type": "call_returned", "port": CALL_PORT, "operation": SERVER_OPERATION}


def _caller(name: str, **keys: Any) -> dict[str, Any]:
    """A periodic runnable that calls the pair a ``call_returned`` event answers, asynchronously."""
    return {**runnable(name, **keys), "calls": [asynchronous_call()]}


class TestCallReturnedRunnables:
    """The ``call-returned`` class: never moved and never costed."""

    @pytest.mark.parametrize(
        ("events", "kind"),
        [
            ([_RETURNED], "call-returned"),
            ([_RETURNED, _RETURNED], "call-returned"),
            ([{"type": "init"}, _RETURNED], "call-returned"),
            ([_RETURNED, _TIMING], "periodic"),
            ([_TIMING, _RETURNED, _OPERATION, _DATA], "periodic"),
            ([_RETURNED, _OPERATION], "server"),
            ([_OPERATION, _RETURNED], "server"),
            ([_RETURNED, _DATA], "data-received"),
            ([_DATA, _RETURNED], "data-received"),
            ([{"type": "init"}], "initialization"),
            ([], "event-less"),
        ],
        ids=[
            "call_returned only",
            "two call_returned events",
            "an init event beside",
            "a timing event beside",
            "timing, call_returned, operation and data",
            "an operation event after",
            "an operation event before",
            "a data event after",
            "a data event before",
            "init only, unchanged",
            "no event, unchanged",
        ],
    )
    def test_the_class_read_from_the_events(self, events: list[dict[str, Any]], kind: str) -> None:
        assert runnable_kind(Runnable.model_validate({"name": "R", "events": events})) == kind

    def test_the_fixture_is_a_model_check_model_accepts(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [_caller("A_Run"), call_returned_runnable("A_Back", wcet=None)], 1),
            task_runs=[("A", "A_Run"), ("A", "A_Back")],
        ).require_call_port("A")
        result = check_cross_layer_consistency(str(model.write(tmp_path / "model")))
        assert result.consistent, [record.message for record in result.records]

    def test_a_mapped_call_returned_runnable_beside_a_mapped_periodic_one(self, tmp_path: Path) -> None:
        model = _one_partition(
            ("A", [_caller("A_Run"), call_returned_runnable("A_Back", wcet=None)], 1),
            task_runs=[("A", "A_Run"), ("A", "A_Back")],
        ).require_call_port("A")
        projection = _project(model, tmp_path)
        placement = projection.placements["A"]
        # Never costed: no wcet data gap for it, and the share counts the periodic runnable alone.
        assert placement.reasons == (_returned_in_task("A_Back", "T_P1_10ms"),)
        assert not placement.free
        assert placement.partition == "P1"
        assert placement.assumptions == ()
        assert [each.runnable for each in placement.runnables] == ["A_Run"]
        assert projection.row("A").targets == ()

    def test_a_mapped_call_returned_runnable_alone_is_never_placed(self, tmp_path: Path) -> None:
        # The runnable calls the pair itself, so the model stays one check-model accepts.
        back = {**call_returned_runnable("A_Back"), "calls": [asynchronous_call()]}
        model = _one_partition(("A", [back], 1), task_runs=[("A", "A_Back")]).require_call_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_returned_in_task("A_Back", "T_P1_10ms"),)
        assert not placement.free
        assert placement.partition == "P1"
        assert placement.runnables == ()

    def test_a_mapped_call_returned_runnable_in_a_task_without_a_budget(self, tmp_path: Path) -> None:
        back = {**call_returned_runnable("A_Back", wcet=None), "calls": [asynchronous_call()]}
        model = _one_partition(("A", [back], 1), task_runs=[("A", "A_Back")], wcet=None).require_call_port("A")
        model.core("C1", enforcement="advisory")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (
            _returned_in_task("A_Back", "T_P1_10ms"),
            "data gap: task 'T_P1_10ms' declares no budget (wcet)",
        )

    def test_an_unmapped_call_returned_runnable_runs_in_no_task(self, tmp_path: Path) -> None:
        # Unlike a server it needs a task: nobody calls it.
        model = _one_partition(
            ("A", [_caller("A_Run"), call_returned_runnable("A_Back")], 1), task_runs=[("A", "A_Run")]
        ).require_call_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == ("runnable 'A_Back' runs in no task",)
        assert not placement.free
        assert placement.partition is None
        assert placement.assumptions == ()

    def test_an_unmapped_call_returned_runnable_alone_is_no_initialization_or_server_case(self, tmp_path: Path) -> None:
        back = {**call_returned_runnable("A_Back"), "calls": [asynchronous_call()]}
        model = _one_partition(("A", [back], 1), task_runs=[]).require_call_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == ("runnable 'A_Back' runs in no task",)
        assert NOT_FREE_NO_TASKED_RUNNABLE not in placement.reasons
        assert placement.partition is None

    def test_a_timing_event_beside_keeps_it_periodic_placed_and_costed(self, tmp_path: Path) -> None:
        back = {**call_returned_runnable("A_Run", also=[_TIMING]), "calls": [asynchronous_call()]}
        model = _one_partition(("A", [back], 1), task_runs=[("A", "A_Run")]).require_call_port("A")
        placement = _project(model, tmp_path).placements["A"]
        assert placement.free and placement.reasons == ()
        (placed,) = placement.runnables
        assert placed.runnable == "A_Run" and placed.share == runnable_utilization(parse_budget("100us"), 0.01)

    def test_a_data_event_beside_keeps_it_data_received(self, tmp_path: Path) -> None:
        back = {**call_returned_runnable("A_Back", also=[_DATA]), "calls": [asynchronous_call()]}
        model = (
            _one_partition(("A", [runnable("A_Run"), back], 1), task_runs=[("A", "A_Run"), ("A", "A_Back")])
            .require_call_port("A")
            .require_data_port("A")
        )
        placement = _project(model, tmp_path).placements["A"]
        assert placement.reasons == (_data_in_task("A_Back", "T_P1_10ms"),)


# Fixture (d): the links


def _sizes_model() -> CarmaModel:
    """A sender ``S`` in ``P1`` and a receiver ``R`` in ``P2``, one interface and port pair per case."""
    model = CarmaModel()
    model.core("C1")
    model.core("C2")
    model.partition("P1", "C1")
    model.partition("P2", "C2")
    model.base_type("u12_base", 12)
    model.base_type("u4_base", 4)
    model.value_type("u12", "u12_base")
    model.value_type("u4", "u4_base")
    model.implementation_type({"name": "ref_one", "kind": "reference_type", "base": "u12"})
    model.implementation_type({"name": "ref_two", "kind": "reference_type", "base": "ref_one"})
    model.implementation_type(
        {
            "name": "rec16",
            "kind": "implementation_record",
            "elements": [{"name": "a", "type": "u12"}, {"name": "b", "type": "u4"}],
        }
    )
    model.implementation_type(
        {"name": "arr3", "kind": "implementation_array", "element_type": "u12", "length": 3, "element_name": "e"}
    )
    model.implementation_type({"name": "ptr", "kind": "pointer", "target": "uint8"})
    model.implementation_type({"name": "ext", "kind": "external_type"})
    model.application_type({"name": "AppMapped", "kind": "application_type"})
    model.application_type({"name": "AppUnmapped", "kind": "application_type"})
    model.application_type({"name": "EnumMapped", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
    model.application_type({"name": "EnumUnmapped", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})
    model.application_type({"name": "RecMapped", "kind": "record", "fields": [{"name": "f", "type": "AppMapped"}]})
    model.mapping_set("MS", [("AppMapped", "uint32"), ("EnumMapped", "uint8"), ("RecMapped", "rec16")])
    cases = {
        "u8": {"implementation_type": "uint8"},
        "u32": {"implementation_type": "uint32"},
        "u12": {"implementation_type": "u12"},
        "ref2": {"implementation_type": "ref_two"},
        "rec": {"implementation_type": "rec16"},
        "arr": {"implementation_type": "arr3"},
        "ptr": {"implementation_type": "ptr"},
        "ext": {"implementation_type": "ext"},
        "app": {"application_type": "AppMapped"},
        "app_unmapped": {"application_type": "AppUnmapped"},
        "enum": {"application_type": "EnumMapped"},
        "enum_unmapped": {"application_type": "EnumUnmapped"},
        "record": {"application_type": "RecMapped"},
    }
    ports_s, ports_r = [], []
    for case, typing in cases.items():
        model.sr_interface(f"I_{case}", {case: typing})
        ports_s.append({"name": f"p_{case}", "direction": "provide", "interface": f"I_{case}"})
        ports_r.append({"name": f"r_{case}", "direction": "require", "interface": f"I_{case}"})
        model.connector(f"L_{case}", ("S", f"p_{case}"), ("R", f"r_{case}"))
    writes = [(f"p_{case}", case) for case in cases]
    model.component("S", ports=ports_s, runnables=[runnable("S_Run", writes=writes)], mappings=["MS"])
    model.component("R", ports=ports_r, runnables=[runnable("R_Run")])
    model.task("T_P1_10ms", "P1", 0.01, runnables=[("S", "S_Run")])
    model.task("T_P2_10ms", "P2", 0.01, runnables=[("R", "R_Run")])
    return model


class TestTheLinkSizes:
    @pytest.fixture
    def links(self, tmp_path: Path) -> dict:
        return {link.element: link for link in _project(_sizes_model(), tmp_path).links}

    @pytest.mark.parametrize(
        ("element", "size"),
        [
            ("u8", Fraction(1)),
            ("u32", Fraction(4)),
            ("u12", Fraction(3, 2)),
            ("ref2", Fraction(3, 2)),
            ("rec", Fraction(2)),
            ("arr", Fraction(9, 2)),
            ("app", Fraction(4)),
            ("enum", Fraction(1)),
            ("record", Fraction(2)),
        ],
    )
    def test_the_sized_shapes_in_bits_over_eight(self, links: dict, element: str, size: Fraction) -> None:
        link = links[element]
        assert link.costed, link.exclusions
        assert link.size == size
        assert link.cost == size * 100

    @pytest.mark.parametrize(
        ("element", "reason"),
        [
            ("ptr", UNSIZED_SHAPE),
            ("ext", UNSIZED_SHAPE),
            ("app_unmapped", UNSIZED_NO_MAPPING),
            ("enum_unmapped", UNSIZED_NO_MAPPING),
        ],
    )
    def test_the_unsized_are_excluded_with_their_reason(self, links: dict, element: str, reason: str) -> None:
        link = links[element]
        assert link.exclusions == (reason,)
        assert link.cost == 0

    def test_the_exclusions_are_listed_as_data_gaps(self, tmp_path: Path) -> None:
        projection = _project(_sizes_model(), tmp_path)
        assert f"link 'S.p_ptr' -> 'R.r_ptr' (ptr) is excluded: {UNSIZED_SHAPE}" in projection.data_gaps

    def test_two_sets_mapping_one_application_type_differently_leave_it_unsized(self, tmp_path: Path) -> None:
        model = _sizes_model()
        model.mapping_set("MS2", [("AppMapped", "uint8")])
        model.get("swc/carma/S.yaml")["data_type_mappings"] = ["MS", "MS2"]
        links = {link.element: link for link in _project(model, tmp_path).links}
        assert links["app"].exclusions == (UNSIZED_NO_MAPPING,)


def _pair(sender_runnables: list[dict], *, receiver_task: bool = True, placed: int = 1) -> CarmaModel:
    """``S`` (in ``P1``) sends ``ISig.value`` (``uint32``) to ``R`` (in ``P2``)."""
    model = CarmaModel()
    model.core("C1")
    model.core("C2")
    model.partition("P1", "C1")
    model.partition("P2", "C2")
    model.sr_interface("ISig", {"value": {"implementation_type": "uint32"}})
    model.component("S", ports=sr_ports(["out"]), runnables=sender_runnables, placed=placed)
    model.component("R", ports=sr_ports(requires=["in"]), runnables=[runnable("R_Run")])
    model.connector("L", ("S", "out"), ("R", "in"))
    model.task("T_P1_10ms", "P1", 0.01, runnables=[("S", r["name"]) for r in sender_runnables])
    model.task("T_P2_10ms", "P2", 0.01, runnables=[("R", "R_Run")] if receiver_task else [])
    return model


class TestTheLinkFrequencies:
    def test_one_writer_gives_the_period(self, tmp_path: Path) -> None:
        (link,) = _project(_pair([runnable("W", writes=[("out", "value")])]), tmp_path).links
        assert link.period == 0.01 and not link.estimated
        assert link.frequency == 100
        assert link.cost == 400

    def test_a_writer_with_two_timing_events_takes_the_shorter(self, tmp_path: Path) -> None:
        events = [{"type": "timing", "period": 0.02}, {"type": "timing", "period": 0.01}]
        (link,) = _project(_pair([runnable("W", events=events, writes=[("out", "value")])]), tmp_path).links
        assert link.period == 0.01

    def test_of_two_writers_the_shorter_period_wins(self, tmp_path: Path) -> None:
        writers = [
            runnable("Slow", period=0.02, writes=[("out", "value")]),
            runnable("Fast", period=0.01, writes=[("out", "value")]),
        ]
        model = _pair(writers)
        model.task("T_P1_20ms", "P1", 0.02, runnables=[("S", "Slow")])
        model.get("tasks/T_P1_10ms.yaml")["runnables"] = [{"swc": "S", "runnable": "Fast"}]
        (link,) = _project(model, tmp_path).links
        assert link.period == 0.01

    def test_no_writer_estimates_from_the_senders_shortest_timing_event(self, tmp_path: Path) -> None:
        projection = _project(_pair([runnable("W")]), tmp_path)
        (link,) = projection.links
        assert link.costed and link.estimated and link.period == 0.01
        assert any("frequency estimated" in gap for gap in projection.data_gaps)

    def test_no_writer_and_no_timing_event_excludes_the_link(self, tmp_path: Path) -> None:
        projection = _project(_pair([runnable("W", events=[{"type": "init"}])]), tmp_path)
        (link,) = projection.links
        assert link.exclusions == (FREQUENCY_NOT_ESTABLISHED,)
        assert projection.placements["S"].reasons == (
            "initialization runnable 'W' runs in task 'T_P1_10ms'; moving an initialization mapping is not modelled",
        )


class TestTheLinkEnds:
    def test_an_end_placed_more_than_once_is_excluded(self, tmp_path: Path) -> None:
        (link,) = _project(_pair([runnable("W", writes=[("out", "value")])], placed=2), tmp_path).links
        assert link.exclusions == (
            "the partition of 'S' is not established (placed 2 times (a move takes a component placed exactly once))",
        )

    def test_an_end_whose_runnables_are_in_no_task_is_excluded(self, tmp_path: Path) -> None:
        (link,) = _project(_pair([runnable("W", writes=[("out", "value")])], receiver_task=False), tmp_path).links
        assert link.exclusions == ("the partition of 'R' is not established (runnable 'R_Run' runs in no task)",)

    def test_an_end_whose_only_unmapped_runnable_is_an_initialization_runnable_is_counted(self, tmp_path: Path) -> None:
        model = _pair([runnable("W", writes=[("out", "value")])])
        model.get("swc/carma/R.yaml")["runnables"].append(init_runnable("R_Init"))
        projection = _project(model, tmp_path)
        (link,) = projection.links
        assert link.costed and link.exclusions == ()
        assert projection.placements["R"].partition == "P2" and projection.placements["R"].free
        assert projection.current_volume == 400

    def test_a_call_to_a_server_whose_server_runnable_is_unmapped_is_counted(self, tmp_path: Path) -> None:
        projection = _project(with_unmapped_server(), tmp_path)
        (call,) = projection.calls
        assert call.counted
        assert projection.current_calls == 1
        assert projection.placements["B1"].partition == "P2"
        assert projection.placements["B1"].reasons == (
            "server runnable 'B1_Srv' runs in its callers' tasks (a direct call), "
            "whose cost the experiment does not model",
        )
        # The links through B1 are counted too.
        assert projection.current_volume == 1200

    def test_an_end_whose_only_runnable_is_an_unmapped_initialization_runnable_is_excluded(
        self, tmp_path: Path
    ) -> None:
        model = _pair([runnable("W", writes=[("out", "value")])], receiver_task=False)
        model.get("swc/carma/R.yaml")["runnables"] = [init_runnable("R_Init")]
        projection = _project(model, tmp_path)
        (link,) = projection.links
        assert link.exclusions == (
            "the partition of 'R' is not established (no runnable runs in a task, so its partition is not "
            "established (initialization and server runnables only))",
        )
        assert projection.current_volume == 0

    def test_a_call_to_a_server_whose_only_runnable_is_an_unmapped_server_runnable_is_not_counted(
        self, tmp_path: Path
    ) -> None:
        model = with_unmapped_server()
        model.get("swc/carma/B1.yaml")["runnables"] = [server_runnable("B1_Srv")]
        task = model.get("tasks/T_P2_10ms.yaml")
        task["runnables"] = [entry for entry in task["runnables"] if entry["runnable"] != "B1_Run"]
        projection = _project(model, tmp_path)
        (call,) = projection.calls
        assert not call.counted
        assert call.exclusions == (
            "the partition of 'B1' is not established (server runnable 'B1_Srv' runs in its callers' tasks "
            "(a direct call), whose cost the experiment does not model; no runnable runs in a task, so its "
            "partition is not established (initialization and server runnables only))",
        )
        assert projection.current_calls == 0

    def test_a_link_with_a_data_gap_end_is_still_costed(self, tmp_path: Path) -> None:
        (link,) = _project(_pair([runnable("W", wcet=None, writes=[("out", "value")])]), tmp_path).links
        assert link.costed


# The current state


def _graph_pair() -> CarmaModel:
    """:func:`_pair` with one writer, ``S`` -> ``R`` over ``L``."""
    return _pair([runnable("W", writes=[("out", "value")])])


class TestTheLinkGraphBranches:
    @pytest.mark.parametrize(
        ("typing", "extra"),
        [
            ({"implementation_type": "nosuch"}, []),
            ({"implementation_type": "orphan"}, [{"name": "orphan", "kind": "value_type", "base": "nobase"}]),
            (
                {"implementation_type": "loop"},
                [{"name": "loop", "kind": "implementation_record", "elements": [{"name": "a", "type": "loop"}]}],
            ),
        ],
        ids=["undeclared type", "undeclared base", "a record naming itself"],
    )
    def test_an_undeclared_type_is_unsized(self, tmp_path: Path, typing: dict, extra: list[dict]) -> None:
        model = _graph_pair()
        model.sr_interface("ISig", {"value": typing})
        for document in extra:
            model.implementation_type(document)
        (link,) = _project(model, tmp_path).links
        assert link.exclusions == (UNSIZED_UNDECLARED,)

    def test_an_external_interface_stub_is_unsized(self, tmp_path: Path) -> None:
        model = _graph_pair()
        model.drop("interfaces/ISig.yaml")
        model.put(
            "interfaces/ISig.yaml",
            {
                "name": "ISig",
                "kind": "external_interface",
                "external_dest": "SENDER-RECEIVER-INTERFACE",
                "data_elements": ["value"],
            },
        )
        (link,) = _project(model, tmp_path).links
        assert (link.element, link.exclusions) == ("value", (UNSIZED_EXTERNAL_INTERFACE,))

    def test_a_connector_reaches_a_component_through_a_nested_composition(self, tmp_path: Path) -> None:
        """``S`` is a member of ``Inner``, placed in the root; the connector names ``Inner``'s boundary port."""
        model = _graph_pair()
        del model.root_members["S"]
        model.root_members["inner"] = {"name": "inner", "composition": "Inner"}
        model.put(
            "compositions/Inner.yaml",
            {
                "name": "Inner",
                "title": "Inner",
                "ports": [{"name": "out", "direction": "provide", "interface": "ISig"}],
                "members": [{"name": "s", "type": "S"}],
                "delegations": [{"name": "d_out", "port": "out", "member": "s", "member_port": "out"}],
            },
        )
        model.put(
            "connectors/L.yaml",
            {"name": "L", "from": {"composition": "Inner", "port": "out"}, "to": {"swc": "R", "port": "in"}},
        )
        projection = _project(model, tmp_path)
        assert projection.placements["S"].partition == "P1"
        (link,) = projection.links
        assert (link.sender, link.receiver, link.costed, link.cost) == ("S", "R", True, 400)

    def test_two_ends_of_one_direction_make_no_link(self, tmp_path: Path) -> None:
        model = _graph_pair()
        model.get("swc/carma/R.yaml")["ports"] = sr_ports(["in"])
        assert _project(model, tmp_path).links == ()

    def test_a_call_with_an_end_not_established_is_listed_and_not_counted(self, tmp_path: Path) -> None:
        model = three_cores()
        model.cs_interface("IOp", ["Do"])
        model.get("swc/carma/Q1.yaml")["ports"].append({"name": "cOp", "direction": "require", "interface": "IOp"})
        model.get("swc/carma/Q1.yaml")["runnables"][0]["calls"] = [{"port": "cOp", "operation": "Do"}]
        model.get("swc/carma/B1.yaml")["ports"].append({"name": "sOp", "direction": "provide", "interface": "IOp"})
        model.connector("LOp", ("B1", "sOp"), ("Q1", "cOp"))
        model.root_members["B1_2"] = {"name": "B1_2", "type": "B1"}  # B1 placed twice: its partition is not established
        projection = _project(model, tmp_path)
        (call,) = projection.calls
        assert not call.counted
        assert call.exclusions == (
            "the partition of 'B1' is not established (placed 2 times (a move takes a component placed exactly once))",
        )
        assert projection.current_calls == 0
        assert any(gap.startswith("call 'Q1.Q1_Run' -> 'B1' (Do) is not counted: ") for gap in projection.data_gaps)


class TestTheCurrentState:
    def test_the_loads_are_the_analysis_figures(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "model")
        projection = read_projection(root, _experiment())
        analysis = {usage.name: usage for usage in analyze_resource_budgets(str(root)).resources}
        for load in projection.loads:
            assert load.load == analysis[load.core].consumption
            assert load.threshold == analysis[load.core].limit
        assert [load.core for load in projection.loads] == ["C1", "C2", "C3"]

    def test_the_volume_counts_the_crossing_links(self, tmp_path: Path) -> None:
        # B1->B5, B2->B6 and Q1->B7 cross (400 each); Q2->Q3 and B3->B4 do not.
        assert _project(three_cores(), tmp_path).current_volume == 1200

    def test_the_calls_are_counted_never_costed(self, tmp_path: Path) -> None:
        model = three_cores()
        model.cs_interface("IOp", ["Do"])
        model.get("swc/carma/Q1.yaml")["ports"].append({"name": "cOp", "direction": "require", "interface": "IOp"})
        model.get("swc/carma/Q1.yaml")["runnables"][0]["calls"] = [{"port": "cOp", "operation": "Do"}]
        model.get("swc/carma/B1.yaml")["ports"].append({"name": "sOp", "direction": "provide", "interface": "IOp"})
        model.connector("LOp", ("B1", "sOp"), ("Q1", "cOp"))
        projection = _project(model, tmp_path)
        (call,) = projection.calls
        assert (call.client, call.server, call.operation) == ("Q1", "B1", "Do")
        assert projection.current_calls == 1
        assert projection.current_volume == 1200


class TestRunnableUtilization:
    def test_it_equals_the_analysis_figure_for_a_one_runnable_task(self, tmp_path: Path) -> None:
        model = CarmaModel()
        model.core("C1")
        model.partition("P1", "C1")
        model.component("A", runnables=[runnable("A_Run", wcet="1000us")])
        model.task("T_P1_10ms", "P1", 0.01, runnables=[("A", "A_Run")], wcet="1000us")
        root = model.write(tmp_path / "model")
        (usage,) = [u for u in analyze_resource_budgets(str(root)).resources if u.name == "C1"]
        assert runnable_utilization(parse_budget("1000us"), 0.01) == usage.consumption == Fraction(1, 10)
        assert read_projection(root, _experiment()).placements["A"].share == Fraction(1, 10)

    def test_a_share_is_exact_in_every_unit(self) -> None:
        # One duration in two spellings is one share; a sub-microsecond budget is exact too.
        assert runnable_utilization(parse_budget("1500us"), 0.01) == Fraction(3, 20)
        assert runnable_utilization(parse_budget("1500000ns"), 0.01) == Fraction(3, 20)
        assert runnable_utilization(parse_budget("850ns"), 0.001) == Fraction(17, 20000)


# The configuration file


def _write_bytes(tmp_path: Path, data: bytes, name: str = "relieve.yaml") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


class TestTheConfigurationFile:
    def test_the_defaults_are_one_configuration_whatever_spells_them(self, tmp_path: Path) -> None:
        no_file = parse_config(None)
        empty, _digest = load_config(_write_bytes(tmp_path, b"", "empty.yaml"))
        comments, _digest = load_config(_write_bytes(tmp_path, b"# only a comment\n\n# and another\n", "c.yaml"))
        assert no_file == parse_config({}) == empty == comments == AllocationConfig()
        assert no_file.name == DEFAULT_NAME == "allocation"
        assert no_file.output is None
        assert (no_file.search.max_moves, no_file.search.candidates, no_file.search.candidates_at) == (3, 3, 3)
        assert no_file.search.tolerance == 0.0
        assert no_file.search.time_limit_s == DEFAULT_TIME_LIMIT_S == 10.0
        assert no_file.constraints.locked == [] and no_file.constraints.allowed_partitions == {}
        assert _experiment(max_moves=5).search.candidates_at == 5

    def test_the_kind_is_the_class_and_never_a_field(self) -> None:
        assert AllocationConfig.kind == DEFAULT_NAME and parse_config(None).kind == "allocation"
        assert "kind" not in AllocationConfig.model_fields
        assert set(AllocationConfig.model_fields) == {"name", "output", "search", "constraints"}

    def test_each_key_is_read_from_its_section(self, tmp_path: Path) -> None:
        path = tmp_path / "rebalance.yaml"
        path.write_text(
            "name: rebalance\n"
            "output: dist/experiments/rebalance\n"
            "search:\n"
            "  max_moves: 4\n"
            "  candidates: 2\n"
            "  candidates_at: 3\n"
            "  tolerance: 0.05\n"
            "  time_limit_s: 30\n"
            "constraints:\n"
            "  locked: [B1]\n"
            "  allowed_partitions:\n"
            "    B5: [P1, P3]\n",
            encoding="utf-8",
            newline="\n",
        )
        experiment, _digest = load_config(path)
        assert experiment.name == "rebalance" and experiment.output == "dist/experiments/rebalance"
        assert (experiment.search.max_moves, experiment.search.candidates, experiment.search.candidates_at) == (4, 2, 3)
        assert experiment.search.tolerance == 0.05 and experiment.search.time_limit_s == 30
        assert experiment.constraints.locked == ["B1"]
        assert experiment.constraints.allowed_partitions == {"B5": ["P1", "P3"]}

    def test_the_digest_is_of_the_bytes_as_read(self, tmp_path: Path) -> None:
        text = "name: relieve\nsearch:\n  max_moves: 2\n"
        lf = _write_bytes(tmp_path, text.encode("utf-8"), "lf.yaml")
        crlf = _write_bytes(tmp_path, text.replace("\n", "\r\n").encode("utf-8"), "crlf.yaml")
        lf_config, lf_digest = load_config(lf)
        crlf_config, crlf_digest = load_config(crlf)
        assert lf_digest == "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert crlf_digest != lf_digest
        assert lf_config == crlf_config

    def test_a_byte_order_mark_is_accepted_and_digested_as_read(self, tmp_path: Path) -> None:
        """A UTF-8 byte order mark (Windows Notepad writes one) parses as the plain file; the digest keeps it."""
        bom = _write_bytes(tmp_path, b"\xef\xbb\xbfname: relieve\n", "bom.yaml")
        plain = _write_bytes(tmp_path, b"name: relieve\n", "plain.yaml")
        bom_config, bom_digest = load_config(bom)
        plain_config, plain_digest = load_config(plain)
        assert bom_config == plain_config and bom_config.name == "relieve"
        assert bom_digest == "sha256:" + hashlib.sha256(b"\xef\xbb\xbfname: relieve\n").hexdigest()
        assert bom_digest != plain_digest

    def test_the_accepted_edges(self) -> None:
        experiment = _experiment(tolerance=1, max_moves=2, candidates_at=2)
        assert experiment.search.tolerance == 1
        assert experiment.search.candidates_at == experiment.search.max_moves == 2


class TestAConfigurationRefusal:
    @pytest.mark.parametrize(
        ("document", "key"),
        [
            (
                {"search": {"max_moves": 2, "candidates_at": 3}},
                "'search.candidates_at': candidates_at (3) must be at most max_moves (2)",
            ),
            ({"search": {"candidates_at": 0}}, "'search.candidates_at'"),
            ({"search": {"candidates_at": -1}}, "'search.candidates_at'"),
            ({"search": {"time_limit_s": float("inf")}}, "'search.time_limit_s': Input should be a finite number"),
            ({"search": {"time_limit_s": 0}}, "'search.time_limit_s': Input should be greater than 0"),
            ({"search": {"tolerance": 1.5}}, "'search.tolerance'"),
            ({"search": {"tolerance": -0.1}}, "'search.tolerance': Input should be greater than or equal to 0"),
            ({"search": {"tolerance": float("nan")}}, "'search.tolerance': Input should be a finite number"),
            ({"search": {"max_moves": "3"}}, "'search.max_moves'"),
            ({"search": {"max_moves": 0}}, "'search.max_moves': Input should be greater than or equal to 1"),
            ({"search": {"candidates": 0}}, "'search.candidates': Input should be greater than or equal to 1"),
            ({"search": {"max_moves": True}}, "'search.max_moves': Input should be a valid integer"),
            ({"search": {"candidates": False}}, "'search.candidates': Input should be a valid integer"),
            ({"constraints": {"locked": "B1"}}, "'constraints.locked'"),
            ({"constraints": {"allowed_partitions": ["P1"]}}, "'constraints.allowed_partitions'"),
            ({"output": 3}, "'output': Input should be a valid string"),
            ({"name": None}, "'name': Input should be a valid string"),
        ],
    )
    def test_a_value_out_of_range_names_its_dotted_key(self, document: dict, key: str) -> None:
        with pytest.raises(ExperimentError) as caught:
            parse_config(document)
        assert str(caught.value).startswith("the configuration file is refused: ")
        assert key in str(caught.value)

    def test_an_invalid_max_moves_is_the_only_refusal(self) -> None:
        with pytest.raises(ExperimentError) as caught:
            parse_config({"search": {"max_moves": 0, "candidates_at": 4}})
        assert "'search.max_moves'" in str(caught.value) and "candidates_at" not in str(caught.value)

    @pytest.mark.parametrize(
        ("document", "key"),
        [
            ({"max_move": 4}, "'max_move': Extra inputs are not permitted"),
            ({"search": {"max_move": 4}}, "'search.max_move': Extra inputs are not permitted"),
            ({"constraints": {"lock": ["B1"]}}, "'constraints.lock': Extra inputs are not permitted"),
            ({"kind": "allocation"}, "'kind': Extra inputs are not permitted"),
            ({"constraints": {"max_moves": 2}}, "'constraints.max_moves': Extra inputs are not permitted"),
            ({"search": {"locked": ["B1"]}}, "'search.locked': Extra inputs are not permitted"),
        ],
        ids=["top", "search", "constraints", "kind", "search key under constraints", "constraints key under search"],
    )
    def test_an_unknown_key_is_refused_by_its_dotted_name(self, document: dict, key: str) -> None:
        with pytest.raises(ExperimentError) as caught:
            parse_config(document)
        assert str(caught.value) == f"the configuration file is refused: {key}"

    @pytest.mark.parametrize("section", ["search", "constraints"])
    def test_a_section_that_is_not_a_mapping_is_refused(self, section: str) -> None:
        with pytest.raises(ExperimentError) as caught:
            parse_config({section: [1, 2]})
        assert f"'{section}': Input should be a valid dictionary" in str(caught.value)

    @pytest.mark.parametrize(
        ("overrides", "key"),
        [
            ({"name": "CON"}, "'name': name 'CON' names a device Windows reserves (CON)"),
            ({"name": "a/b"}, "'name'"),
            ({"name": "relieve."}, "'name': name 'relieve.' ends in a space or a dot"),
            ({"name": "relieve "}, "'name': name 'relieve ' ends in a space or a dot"),
        ],
    )
    def test_the_name_rules_are_unchanged(self, overrides: dict, key: str) -> None:
        with pytest.raises(ExperimentError) as caught:
            parse_config(overrides)
        assert key in str(caught.value)

    def test_a_file_refusal_is_read_through_the_yaml_leaf_and_names_no_path(self, tmp_path: Path) -> None:
        path = tmp_path / "relieve.yaml"
        path.write_text("search:\n  time_limit_s: .inf\n", encoding="utf-8", newline="\n")
        with pytest.raises(ExperimentError) as caught:
            load_config(path)
        assert "'search.time_limit_s'" in str(caught.value)
        assert str(tmp_path) not in str(caught.value) and "relieve.yaml" not in str(caught.value)

    @pytest.mark.parametrize(
        ("text", "sentence"),
        [
            ("- a\n- b\n", "the configuration file must be a mapping of keys to values"),
            ("just a string\n", "the configuration file must be a mapping of keys to values"),
            ("name: [unclosed\n", "the configuration file is not valid YAML: "),
        ],
        ids=["a list", "a scalar", "not YAML"],
    )
    def test_a_document_that_is_not_a_mapping_is_refused(self, tmp_path: Path, text: str, sentence: str) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(text, encoding="utf-8", newline="\n")
        with pytest.raises(ExperimentError) as caught:
            load_config(path)
        assert str(caught.value).startswith(sentence)

    def test_bytes_that_are_not_utf8_are_the_callers_to_name(self, tmp_path: Path) -> None:
        with pytest.raises(UnicodeDecodeError):
            load_config(_write_bytes(tmp_path, b"name: r\xe9lieve\n"))

    def test_an_absent_file_is_the_callers_to_name(self, tmp_path: Path) -> None:
        with pytest.raises(OSError):
            load_config(tmp_path / "absent.yaml")

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            (
                {"allowed_partitions": {"B5": ["P9"]}},
                "'constraints.allowed_partitions' of 'B5' names partition 'P9', which the model does not declare",
            ),
            (
                {"allowed_partitions": {"X9": ["P1"]}},
                "'constraints.allowed_partitions' names component 'X9', which the model does not declare",
            ),
            ({"locked": ["X9"]}, "'constraints.locked' names component 'X9', which the model does not declare"),
        ],
    )
    def test_a_name_the_model_does_not_declare_is_refused(self, tmp_path: Path, overrides: dict, message: str) -> None:
        root = three_cores().write(tmp_path / "model")
        with pytest.raises(ExperimentError) as caught:
            read_projection(root, _experiment(**overrides))
        assert str(caught.value) == message

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"locked": ["X8", "X9"]}, "'constraints.locked' names component 'X8', which the model does not declare"),
            (
                {"allowed_partitions": {"X7": ["P1"]}, "locked": ["X9"]},
                "'constraints.locked' names component 'X9', which the model does not declare",
            ),
        ],
        ids=["the first of one key", "locked before allowed_partitions"],
    )
    def test_of_two_undeclared_names_the_first_is_named(self, tmp_path: Path, overrides: dict, message: str) -> None:
        root = three_cores().write(tmp_path / "model")
        with pytest.raises(ExperimentError) as caught:
            read_projection(root, _experiment(**overrides))
        assert str(caught.value) == message


# The model digest


class TestTheModelDigest:
    def test_it_counts_the_layer_files(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "model")
        _digest, files = model_digest(root)
        assert files == sum(1 for path in root.rglob("*.yaml"))

    def test_a_content_change_or_a_rename_changes_it(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "model")
        before, _files = model_digest(root)
        task = root / "tasks" / "T_P1_10ms.yaml"
        text = task.read_text(encoding="utf-8")
        task.write_text(text.replace("wcet: 1000us", "wcet: 1001us"), encoding="utf-8", newline="\n")
        changed, _files = model_digest(root)
        assert changed != before
        task.write_text(text, encoding="utf-8", newline="\n")
        assert model_digest(root)[0] == before
        task.rename(task.with_name("T_P1_10ms_renamed.yaml"))
        assert model_digest(root)[0] not in (before, changed)

    def test_a_file_beside_the_model_is_not_read(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "model")
        before = model_digest(root)
        (root / "notes.txt").write_text("not a layer file\n", encoding="utf-8", newline="\n")
        (root / "tasks" / "README.md").write_text("neither\n", encoding="utf-8", newline="\n")
        assert model_digest(root) == before
