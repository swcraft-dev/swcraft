"""Tasks and sequences.

The rules are restated from SWCraft's benchmark generator (`workload.py`): the busy tasks take the
runnables in component order in contiguous chunks whose sizes differ by at most one, larger first;
partitions take whole components; periods go by k mod 5 and the idle tasks run at 100 ms;
priorities are rate-monotonic; every task budgets 1 us; a task whose components have several
effective levels states `coexistence`; each mapped runnable gains one timing event at its task's
period; the sequences go round robin over the busy tasks with at most three steps. Every acceptance
is judged by the passport pass over the rendered tree.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from swcraft_specimens.model import Partition
from swcraft_specimens.preset import ASIL
from swcraft_specimens.specimen.build import build_project
from swcraft_specimens.specimen.common import COUNT_CEILING
from swcraft_specimens.specimen.components import build_components
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.leaves import stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.tasks import (
    BUSY_PERIODS_US,
    IDLE_PERIOD_US,
    TASK_COEXISTENCE,
    TASK_WCET_US,
    TaskComponent,
    build_tasks,
    chunk_sizes,
    runnable_count,
    sequence_count,
    sequence_step_count,
)
from tests.oracle.specimen_helpers import built_specimen, neutral_passport, no_components

PARTITION_SENTENCE = "a task runs in a partition; hardware.partitions states 0"


def with_tasks(
    tasks: int,
    idle: int,
    *,
    partitions: int = 1,
    rated: int = 0,
    cores: int = 1,
    runnables: int | None = 2,
    sequences: int | None = None,
) -> dict[str, Any]:
    """The neutral passport with tasks, partitions and runnables.

    The hardware, task and sequence identifier lengths are unstated (the overall maximum 8 is the
    connectors').
    """
    passport = copy.deepcopy(neutral_passport())
    passport["layers"]["tasks"] = tasks
    passport["tasks"]["idle"] = idle
    if sequences is None:
        del passport["layers"]["sequences"]
    else:
        passport["layers"]["sequences"] = sequences
    passport["hardware"].update(cores=cores, partitions=partitions, rated_partitions=rated)
    passport["layers"]["hardware"] = 1 + cores + partitions
    if runnables is None:
        del passport["components"]["runnables"]
    else:
        passport["components"]["runnables"] = runnables
    for layer in ("hardware", "tasks", "sequences"):
        del passport["identifiers"][layer]
    return passport


def refused(passport: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(r.leaf, r.refusal_class, r.sentence) for r in check_feasibility(passport)]


def computed(passport: dict[str, Any]) -> dict[str, Any]:
    return built_specimen(passport).passport


def partitions(count: int, cores: int = 1, rated: int = 0) -> tuple[Partition, ...]:
    return tuple(
        Partition(f"Pa{index}", f"Co{index % cores}", ASIL.QM if index < rated else ASIL.ABSENT)
        for index in range(count)
    )


def plan(
    components: list[tuple[int, ASIL]],
    busy: int,
    idle: int = 0,
    partition_count: int = 1,
    sequences: int | None = None,
    cores: int = 1,
) -> Any:
    """`build_tasks` over components named `S0`, `S1`, ... with these runnables and levels."""
    task_components = [
        TaskComponent(f"S{index}", runnables, level) for index, (runnables, level) in enumerate(components)
    ]
    sequence_total = busy if sequences is None else sequences
    return build_tasks(
        task_components,
        busy,
        idle,
        partitions(partition_count, cores),
        [f"T{index}" for index in range(busy + idle)],
        [f"Q{index}" for index in range(sequence_total)],
    )


# --- the even split -----------------------------------------------------------------------------


class TestTheEvenSplit:
    def test_150_runnables_over_30_busy_tasks_take_5_each(self) -> None:
        assert chunk_sizes(150, 30) == [5] * 30

    def test_8_runnables_over_2_busy_tasks_take_4_each(self) -> None:
        assert chunk_sizes(8, 2) == [4, 4]

    def test_an_uneven_split_is_larger_first_and_differs_by_at_most_one(self) -> None:
        assert chunk_sizes(7, 3) == [3, 2, 2]
        assert chunk_sizes(10, 4) == [3, 3, 2, 2]

    def test_the_chunks_are_contiguous_in_component_order(self) -> None:
        result = plan([(3, ASIL.ABSENT), (1, ASIL.ABSENT), (2, ASIL.ABSENT)], busy=2)
        assert [task.runnables for task in result.tasks] == [
            (("S0", "R1"), ("S0", "R2"), ("S0", "R3")),
            (("S1", "R1"), ("S2", "R1"), ("S2", "R2")),
        ]

    def test_no_busy_task_maps_nothing(self) -> None:
        result = plan([(2, ASIL.ABSENT), (1, ASIL.ABSENT)], busy=0, idle=2)
        assert [task.runnables for task in result.tasks] == [(), ()]
        assert result.periods == {}
        assert result.sequences == ()


# --- whole components per partition -------------------------------------------------------------


class TestWholeComponentsPerPartition:
    def test_a_component_of_three_runnables_over_two_chunks_stays_in_one_partition(self) -> None:
        # Chunks [2, 2, 1, 1]: the chunk boundaries at 2, 4 and 5 runnables; the component
        # boundaries at 3, 4 and 6. Only 4 is both, so tasks 0-1 take partition 0, 2-3 partition 1.
        result = plan([(3, ASIL.ABSENT), (1, ASIL.ABSENT), (2, ASIL.ABSENT)], busy=4, partition_count=2)
        assert [task.partition for task in result.tasks] == ["Pa0", "Pa0", "Pa1", "Pa1"]
        by_component: dict[str, set[str | None]] = {}
        for task in result.tasks:
            for component, _ in task.runnables:
                by_component.setdefault(component, set()).add(task.partition)
        assert all(len(named) == 1 for named in by_component.values())

    def test_the_cut_points_are_chosen_evenly_numbering_from_0(self) -> None:
        # Five one-runnable components over five tasks: four cut points, two chosen (K = 3).
        result = plan([(1, ASIL.ABSENT)] * 5, busy=5, partition_count=3)
        assert [task.partition for task in result.tasks] == ["Pa0", "Pa0", "Pa1", "Pa1", "Pa2"]

    def test_with_no_cut_point_every_busy_task_names_partition_0(self) -> None:
        result = plan([(4, ASIL.ABSENT)], busy=2, partition_count=3)
        assert [task.partition for task in result.tasks] == ["Pa0", "Pa0"]

    def test_a_task_runs_on_its_partition_s_core(self) -> None:
        result = plan([(1, ASIL.ABSENT)] * 4, busy=4, idle=3, partition_count=4, cores=2)
        for task in result.tasks:
            assert task.core == f"Co{int(str(task.partition)[2:]) % 2}"


# --- periods, idle tasks, priorities, budgets ---------------------------------------------------


class TestPeriodsAndPriorities:
    def test_busy_periods_go_by_k_mod_5(self) -> None:
        result = plan([(1, ASIL.ABSENT)] * 7, busy=7)
        assert [task.period_us for task in result.tasks] == [
            10_000,
            20_000,
            30_000,
            40_000,
            50_000,
            10_000,
            20_000,
        ]
        assert BUSY_PERIODS_US == (10_000, 20_000, 30_000, 40_000, 50_000)

    def test_idle_tasks_run_at_100_ms_round_robin_from_partition_0(self) -> None:
        result = plan([(1, ASIL.ABSENT)], busy=1, idle=5, partition_count=3)
        idle = result.tasks[1:]
        assert [task.period_us for task in idle] == [IDLE_PERIOD_US] * 5 == [100_000] * 5
        assert [task.partition for task in idle] == ["Pa0", "Pa1", "Pa2", "Pa0", "Pa1"]
        assert all(task.runnables == () for task in idle)

    def test_priorities_are_rate_monotonic_on_every_core(self) -> None:
        result = plan([(1, ASIL.ABSENT)] * 12, busy=12, idle=3, partition_count=4, cores=2)
        assert sorted(task.priority for task in result.tasks) == list(range(1, 16))
        for one in result.tasks:
            for other in result.tasks:
                if one.core == other.core and one.period_us < other.period_us:
                    assert one.priority > other.priority

    def test_busy_tasks_of_equal_period_rank_by_index(self) -> None:
        result = plan([(1, ASIL.ABSENT)] * 6, busy=6, idle=1)
        # Periods 10, 20, 30, 40, 50, 10, 100 ms: ranked longest first, then by index (tasks 0
        # and 5 tie at 10 ms). A busy task never ties an idle one: busy periods are at most 50.
        assert [task.priority for task in result.tasks] == [6, 5, 4, 3, 2, 7, 1]

    def test_idle_tasks_of_equal_period_rank_by_index(self) -> None:
        result = plan([(1, ASIL.ABSENT)] * 2, busy=0, idle=3)
        assert [(task.period_us, task.priority) for task in result.tasks] == [
            (100_000, 1),
            (100_000, 2),
            (100_000, 3),
        ]

    def test_every_task_budgets_1_us(self) -> None:
        result = plan([(1, ASIL.ABSENT)] * 3, busy=2, idle=2)
        assert TASK_WCET_US == 1
        assert all(task.wcet_us == 1 for task in result.tasks)


# --- coexistence and the back-patch --------------------------------------------------------------


class TestCoexistenceAndTheBackPatch:
    def test_coexistence_exactly_where_the_effective_levels_differ(self) -> None:
        result = plan(
            [(1, ASIL.ABSENT), (1, ASIL.QM), (1, ASIL.D), (1, ASIL.D), (1, ASIL.B)],
            busy=3,
        )
        # Chunks [2, 2, 1]: {QM (none declared), QM}, {D, D}, {B}.
        assert [task.coexistence for task in result.tasks] == ["", "", ""]
        mixed = plan([(1, ASIL.QM), (1, ASIL.D), (1, ASIL.D)], busy=2)
        assert [task.coexistence for task in mixed.tasks] == [TASK_COEXISTENCE, ""]
        assert TASK_COEXISTENCE == ("Specimen: components of several levels share this task by construction.")

    def test_each_mapped_runnable_gains_its_task_s_period(self) -> None:
        result = plan([(2, ASIL.ABSENT), (1, ASIL.ABSENT)], busy=2)
        assert result.periods == {
            ("S0", "R1"): 10_000,
            ("S0", "R2"): 10_000,
            ("S1", "R1"): 20_000,
        }

    def test_the_partition_levels_are_the_effective_levels_of_its_tasks(self) -> None:
        result = plan([(1, ASIL.ABSENT), (1, ASIL.D)], busy=2, idle=1, partition_count=2)
        assert {name: sorted(levels) for name, levels in result.partition_levels.items()} == {
            "Pa0": [ASIL.QM],
            "Pa1": [ASIL.D],
        }


# --- sequences ----------------------------------------------------------------------------------


class TestSequences:
    def test_sequences_go_round_robin_with_at_most_3_steps(self) -> None:
        result = plan([(5, ASIL.ABSENT), (1, ASIL.ABSENT)], busy=2, sequences=3)
        assert [(s.name, s.task) for s in result.sequences] == [
            ("Q0", "T0"),
            ("Q1", "T1"),
            ("Q2", "T0"),
        ]
        assert result.sequences[0].steps == (("S0", "R1"), ("S0", "R2"), ("S0", "R3"))
        assert result.sequences[1].steps == (("S0", "R4"), ("S0", "R5"), ("S1", "R1"))
        assert result.sequences[2].steps == result.sequences[0].steps
        short = plan([(1, ASIL.ABSENT), (1, ASIL.ABSENT)], busy=2)
        assert [s.steps for s in short.sequences] == [(("S0", "R1"),), (("S1", "R1"),)]

    def test_an_unstated_sequence_count_is_the_busy_tasks(self) -> None:
        assert sequence_count(stated_leaves(with_tasks(3, 1))) == 2
        assert sequence_count(stated_leaves(with_tasks(3, 1, sequences=5))) == 5

    def test_the_step_count_in_closed_form(self) -> None:
        # Chunks [2, 2, 1] of 5 runnables: one cycle is 2 + 2 + 1 steps.
        assert sequence_step_count(5, 3, 3) == 5
        assert sequence_step_count(5, 3, 7) == 5 + 5 + 2
        assert sequence_step_count(20, 2, 3) == 9
        assert sequence_step_count(5, 0, 0) == 0


# --- the derived runnables ----------------------------------------------------------------------


class TestTheDerivedRunnables:
    def test_unstated_runnables_are_max_n_b(self) -> None:
        stated = stated_leaves(with_tasks(5, 1, runnables=None))
        assert runnable_count(stated) == 4
        shapes = build_components(stated, [1, 1])
        assert sum(shape.runnables for shape in shapes) == 4

    def test_with_no_task_unstated_runnables_are_one_per_component(self) -> None:
        stated = stated_leaves(with_tasks(0, 0, runnables=None))
        assert runnable_count(stated) == 2

    def test_fewer_busy_tasks_than_components_derive_n(self) -> None:
        assert runnable_count(stated_leaves(with_tasks(1, 0, runnables=None))) == 2

    def test_unstated_runnables_with_four_busy_tasks_build(self) -> None:
        result = computed(with_tasks(5, 1, runnables=None))
        assert result["layers"]["tasks"] == 5
        assert result["tasks"]["idle"] == 1
        assert result["components"]["runnables"] == 4


# --- built and judged by the pass ---------------------------------------------------------------


class TestBuilt:
    def test_tasks_sequences_and_idle_are_met(self) -> None:
        result = computed(with_tasks(3, 1, partitions=2, runnables=4, sequences=3))
        assert result["layers"]["tasks"] == 3
        assert result["layers"]["sequences"] == 3
        assert result["tasks"]["idle"] == 1

    def test_the_built_project_carries_the_tasks(self) -> None:
        passport = with_tasks(3, 1, partitions=2, runnables=4)
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        assert len(project.tasks) == 3
        assert [len(task.runnables) for task in project.tasks] == [2, 2, 0]
        assert project.partitions is not None
        assert [partition.name for partition in project.partitions] == ["Pa1", "Pa2"]
        assert [task.partition for task in project.tasks] == ["Pa1", "Pa2", "Pa1"]
        mapped = [r for c in project.components for r in c.runnables]
        assert all(r.period_us is not None for r in mapped)
        assert [s.task for s in project.sequences] == [t.name for t in project.tasks[:2]]

    def test_no_task_builds_today_s_specimen(self) -> None:
        project = build_project(SpecimenPreset(DEFAULT_NAME, neutral_passport()))
        assert project.tasks == () and project.sequences == ()
        assert all(r.period_us is None for c in project.components for r in c.runnables)

    def test_idle_tasks_only_map_no_runnable(self) -> None:
        result = computed(with_tasks(2, 2, sequences=0))
        assert result["tasks"]["idle"] == 2


# --- the refusals -------------------------------------------------------------------------------


class TestRefusals:
    def test_more_idle_than_tasks_is_sum_at_tasks_idle(self) -> None:
        assert refused(with_tasks(3, 4)) == [
            ("tasks.idle", "sum", "states 4 idle tasks; layers.tasks states 3 tasks"),
        ]

    def test_more_busy_tasks_than_stated_runnables(self) -> None:
        assert refused(with_tasks(4, 1, runnables=2)) == [
            (
                "layers.tasks",
                "tasks",
                "states 4 tasks with 1 idle; the 2 runnables fill at most 2 busy tasks",
            ),
        ]

    def test_a_task_with_no_partition(self) -> None:
        assert refused(with_tasks(1, 1, partitions=0)) == [
            ("layers.tasks", "tasks", PARTITION_SENTENCE),
        ]

    def test_no_component_with_a_busy_task_and_unstated_runnables(self) -> None:
        passport = no_components(with_tasks(1, 0, runnables=None))
        assert refused(passport) == [
            ("layers.tasks", "tasks", "states 1 busy tasks; no component holds a runnable"),
        ]

    def test_no_component_with_stated_runnables_stays_the_components_sum(self) -> None:
        passport = no_components(with_tasks(1, 0, runnables=1))
        assert [(leaf, cls) for leaf, cls, _ in refused(passport)] == [
            ("components.runnables", "sum"),
        ]

    def test_sequences_with_no_busy_task(self) -> None:
        assert refused(with_tasks(1, 1, sequences=2)) == [
            (
                "layers.sequences",
                "tasks",
                "states 2; a sequence steps through a task's runnables; no task runs one",
            ),
        ]

    def test_the_steps_above_the_ceiling(self) -> None:
        # 10,000 runnables over 5,000 busy tasks: two each, so 5,001 sequences make 10,002 steps.
        passport = with_tasks(5_000, 0, runnables=COUNT_CEILING, sequences=5_001)
        assert refused(passport) == [
            (
                "layers.sequences",
                "ceiling",
                "writes 10002 steps; this version builds at most 10,000 of anything",
            ),
        ]

    def test_the_steps_at_the_ceiling_are_not_refused(self) -> None:
        passport = with_tasks(5_000, 0, runnables=COUNT_CEILING, sequences=5_000)
        assert ("layers.sequences", "ceiling") not in [(leaf, cls) for leaf, cls, _ in refused(passport)]

    @pytest.mark.parametrize(("tasks", "idle"), [(0, 0), (2, 2), (2, 0), (3, 1)], ids=["none", "idle", "busy", "mixed"])
    def test_a_feasible_task_layer_is_not_refused(self, tasks: int, idle: int) -> None:
        assert refused(with_tasks(tasks, idle, partitions=2)) == []
