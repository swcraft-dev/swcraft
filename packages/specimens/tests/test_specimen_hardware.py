"""Hardware, the per-core RAM, tasks and requirements at zero.

The hardware leaves are met by construction (one processor, the cores, the partitions round-robin
over the cores with the first `rated_partitions` rated QM, memory j accessible from core j mod cores
alone) and judged by the passport pass over the rendered tree, never by the builder's own lists.
Each `hardware-topology` vector fires with one refusal and passes with one acceptance. The budgets
sit on the first memory of each component's core, by `workload.py`'s formula, and size each memory
for about 60 % under its 0.8 threshold.
"""

from __future__ import annotations

import copy
import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_specimens.contract import validate_contract
from swcraft_specimens.emit import build_file_map
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import (
    BaseType,
    Component,
    Composition,
    CompositionMember,
    Connector,
    ConnectorEndpoint,
    Core,
    DataElement,
    Domain,
    Interface,
    Partition,
    Port,
    Processor,
    Project,
    Task,
    ValueType,
    hardware_names,
    validate_project,
)
from swcraft_specimens.passport import compute_passport, read_passport
from swcraft_specimens.preset import ASIL
from swcraft_specimens.specimen.build import build_project, build_specimen_files
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.hardware import (
    PARTITION_COEXISTENCE,
    HardwarePlan,
    build_hardware,
    component_budget,
    memory_budgets,
    memory_size,
    rate_partitions,
)
from swcraft_specimens.specimen.identifiers import (
    hardware_names_by_kind,
    identifier_counts,
    identifier_names,
)
from swcraft_specimens.specimen.leaves import LEAVES, stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.shape import Shape, specimen_shape
from swcraft_specimens.specimen.vocabulary import CEILING, HARDWARE_TOPOLOGY, LATER_SHAPE, MET
from swcraft_specimens.validate import validate_file_map
from tests.oracle.specimen_helpers import neutral_passport, refused_before_construction

ROOT = Path(__file__).parents[1]


def hardware_of(passport: dict[str, Any]) -> HardwarePlan:
    """The hardware of a feasible passport, named by the identifier plan at the least lengths."""
    leaves = stated_leaves(passport)
    shape = specimen_shape(leaves)
    assert isinstance(shape, Shape)
    unnamed = {path: value for path, value in leaves.items() if not path.startswith("identifiers.")}
    counts = identifier_counts(unnamed, shape.connectors)
    names = identifier_names(unnamed, counts)
    return build_hardware(leaves, hardware_names_by_kind(names, counts))


def passport_with(cores: int, partitions: int, memories: int, rated: int, layer: int | None = None) -> dict[str, Any]:
    """The neutral passport with these hardware leaves; `layers.hardware` follows them unless stated."""
    passport = copy.deepcopy(neutral_passport())
    passport["hardware"] = {
        "cores": cores,
        "partitions": partitions,
        "memories": memories,
        "rated_partitions": rated,
    }
    passport["layers"]["hardware"] = 1 + cores + partitions + memories if layer is None else layer
    return passport


def hardware_refusals(passport: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (r.leaf, r.refusal_class)
        for r in check_feasibility(passport)
        if r.leaf.startswith("hardware.") or r.leaf == "layers.hardware"
    ]


def specimen_project(plan: HardwarePlan) -> Project:
    """Two components with one connector, no task, no requirement, on the built hardware."""
    group = "specimen"
    first, second = "Swc0001", "Swc0002"
    budgets, _ = memory_budgets(plan, (None, None), 0)
    return Project(
        DEFAULT_NAME,
        plan.processor,
        plan.cores,
        plan.memories,
        (Domain(group, (first, second)),),
        (
            Component(first, group, ASIL.ABSENT, (Port("P1", "provide", "If0001"),), (), budgets[0]),
            Component(second, group, ASIL.ABSENT, (Port("P1", "require", "If0001"),), (), budgets[1]),
        ),
        (BaseType("Bt0001", 8, "NONE", "uint8_t"), ValueType("Vt0001", "Bt0001")),
        (Interface("If0001", "sender_receiver", data_elements=(DataElement("D1", "Vt0001"),)),),
        (
            Connector(
                "Conn0001",
                "sender_receiver",
                ConnectorEndpoint(first, "P1"),
                ConnectorEndpoint(second, "P1"),
            ),
        ),
        (),
        (),
        (
            Composition(
                "Comp0001",
                "Comp0001",
                True,
                (CompositionMember("M0001", first), CompositionMember("M0002", second)),
            ),
        ),
        partitions=plan.partitions,
    )


def rendered(project: Project) -> tuple[dict[str, bytes], dict[str, Any]]:
    """The checked file map and the passport the pass computes over it."""
    validate_project(project)
    file_map = build_file_map(project)
    validate_contract(file_map)
    documents = validate_file_map(file_map, project, task_overhead_us=1)
    return file_map, compute_passport(documents)


# --- the leaves are met -------------------------------------------------------------------------


class TestHardwareIsMet:
    @pytest.mark.parametrize(
        ("cores", "partitions", "memories", "rated"),
        [(1, 0, 0, 0), (1, 1, 1, 0), (2, 5, 3, 2), (3, 7, 2, 7), (4, 2, 0, 1)],
    )
    def test_the_hardware_leaves_are_met(self, cores: int, partitions: int, memories: int, rated: int) -> None:
        passport = passport_with(cores, partitions, memories, rated)
        assert hardware_refusals(passport) == []
        plan = hardware_of(passport)
        _, computed = rendered(specimen_project(plan))
        assert computed["hardware"] == passport["hardware"]
        assert computed["layers"]["hardware"] == passport["layers"]["hardware"]

    def test_partitions_go_round_robin_over_the_cores(self) -> None:
        plan = hardware_of(passport_with(3, 7, 0, 0))
        cores = [core.name for core in plan.cores]
        assert [p.core for p in plan.partitions] == [cores[i % 3] for i in range(7)]

    def test_the_first_rated_partitions_are_qm_and_the_rest_unrated(self) -> None:
        plan = hardware_of(passport_with(2, 5, 0, 2))
        assert [p.asil for p in plan.partitions] == [ASIL.QM] * 2 + [ASIL.ABSENT] * 3

    def test_cores_are_strict_d_cores_at_0_7(self) -> None:
        plan = hardware_of(passport_with(2, 0, 0, 0))
        assert all(core.capability is ASIL.D for core in plan.cores)
        assert all(core.threshold == Decimal("0.7") for core in plan.cores)

    def test_memory_j_is_accessible_from_core_j_alone(self) -> None:
        plan = hardware_of(passport_with(3, 0, 3, 0))
        cores = [core.name for core in plan.cores]
        assert [memory.accessible for memory in plan.memories] == [(core,) for core in cores]

    def test_memories_beyond_the_cores_go_round_robin(self) -> None:
        plan = hardware_of(passport_with(2, 0, 5, 0))
        cores = [core.name for core in plan.cores]
        assert [memory.accessible for memory in plan.memories] == [(cores[j % 2],) for j in range(5)]

    def test_memories_are_strict_at_0_8(self) -> None:
        plan = hardware_of(passport_with(2, 0, 3, 0))
        assert all(memory.threshold == Decimal("0.8") for memory in plan.memories)

    def test_hardware_names_are_unique_ignoring_case(self) -> None:
        plan = hardware_of(passport_with(4, 9, 3, 1))
        names = hardware_names(specimen_project(plan))
        assert len({name.casefold() for name in names}) == len(names) == 1 + 4 + 9 + 3

    def test_the_hardware_leaves_are_built(self) -> None:
        hardware_leaves = frozenset(
            {
                "layers.hardware",
                "hardware.cores",
                "hardware.partitions",
                "hardware.memories",
                "hardware.rated_partitions",
            }
        )
        assert all(LEAVES[leaf].leaf_class == MET for leaf in hardware_leaves)
        refusals = check_feasibility(passport_with(2, 3, 1, 1))
        assert not hardware_leaves & {r.leaf for r in refusals}


class TestMemoryBudgets:
    """`workload.py`'s budgets by component order, on the first memory of each component's core.

    Each memory is sized for about 60 % under its 0.8 threshold.
    """

    def test_the_budget_formula(self) -> None:
        # No hub: component 0 alone takes the hub budget; the rest 512 x (1 + 7i mod 8).
        assert [component_budget(i, 0) for i in range(5)] == [16_384, 4_096, 3_584, 3_072, 2_560]
        # Three hubs: 16,384 >> i for each, then the ordinary rule.
        assert [component_budget(i, 3) for i in range(5)] == [16_384, 8_192, 4_096, 3_072, 2_560]
        # A hub budget is at least 512.
        assert [component_budget(i, 8) for i in (5, 6, 7)] == [512, 512, 512]

    def test_each_size_leaves_the_budgets_about_60_percent(self) -> None:
        assert memory_size(0) == 4_096
        assert memory_size(2_457) == 4_096
        assert memory_size(2_458) == 8_192
        assert memory_size(111_616) == 188_416

    def test_no_component_in_a_task_declares_zero_on_every_memory(self) -> None:
        plan = hardware_of(passport_with(2, 0, 3, 0))
        budgets, memories = memory_budgets(plan, (None, None), 0)
        zero = tuple((memory.name, 0) for memory in plan.memories)
        assert budgets == (zero, zero)
        assert [memory.size_bytes for memory in memories] == [4_096] * 3
        file_map, _ = rendered(specimen_project(plan))
        for name in ("Swc0001", "Swc0002"):
            document = yaml.safe_load(file_map[f"model/swc/specimen/{name}.yaml"])
            assert document["memory"] == {memory.name: 0 for memory in plan.memories}

    def test_no_task_gives_zero_budgets_and_the_least_size_when_built(self) -> None:
        passport = passport_with(2, 3, 2, 2)
        del passport["identifiers"]["hardware"]
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        assert project.tasks == ()
        assert all(amount == 0 for c in project.components for _, amount in c.memory)
        assert [m.size_bytes for m in project.memories] == [4_096, 4_096]
        assert [m.threshold for m in project.memories] == [Decimal("0.8")] * 2

    def test_no_memory_gives_an_empty_budget_map(self) -> None:
        plan = hardware_of(passport_with(1, 0, 0, 0))
        assert memory_budgets(plan, (None,), 0) == (((),), ())
        project = build_project(SpecimenPreset(DEFAULT_NAME, tasks_passport(1, 1, 0)))
        assert project.memories == ()
        assert all(component.memory == () for component in project.components)

    def test_each_budget_sits_on_its_cores_memory(self) -> None:
        project = built_with_tasks(tasks_passport(2, 2, 2))
        cores = core_of_component(project)
        memory_of_core = first_memory_of_core(project)
        assert sorted(set(cores.values())) == [core.name for core in project.cores]
        for index, component in enumerate(project.components):
            own = memory_of_core[cores[component.name]]
            assert dict(component.memory) == {
                m.name: component_budget(index, 0) if m.name == own else 0 for m in project.memories
            }
        for memory in project.memories:
            consumed = sum(dict(c.memory)[memory.name] for c in project.components)
            assert memory.size_bytes == memory_size(consumed)

    def test_two_cores_and_one_memory(self) -> None:
        # Memory 0 is core 0's alone; a component of core 1, which has no memory, budgets 0.
        project = built_with_tasks(tasks_passport(2, 2, 1))
        cores = core_of_component(project)
        first_core, second_core = (core.name for core in project.cores)
        (memory,) = project.memories
        assert memory.accessible == (first_core,)
        first, second = project.components
        assert (cores[first.name], cores[second.name]) == (first_core, second_core)
        assert first.memory == ((memory.name, 16_384),)
        assert second.memory == ((memory.name, 0),)
        assert memory.size_bytes == memory_size(16_384) == 28_672

    def test_memories_beyond_the_cores_budget_on_each_core_s_first_memory(self) -> None:
        # One core and three memories: all three are core 0's (j mod 1), the budgets sit on the
        # first alone, and the other two declare 0 and take the least size.
        project = built_with_tasks(tasks_passport(1, 1, 3))
        first, *others = project.memories
        assert [memory.accessible for memory in project.memories] == [(project.cores[0].name,)] * 3
        assert [dict(c.memory) for c in project.components] == [
            {first.name: component_budget(index, 0), **{m.name: 0 for m in others}}
            for index in range(len(project.components))
        ]
        assert first.size_bytes == memory_size(16_384 + 4_096)
        assert [memory.size_bytes for memory in others] == [4_096, 4_096]

    def test_a_component_with_no_runnable_budgets_zero_beside_busy_tasks(self) -> None:
        # Rn = 1 < n = 2 with one busy task: the second component holds no runnable, runs in no
        # task and budgets 0 everywhere; component 0 is still the unique largest.
        passport = tasks_passport(1, 1, 1)
        passport["components"]["runnables"] = 1
        passport["layers"]["tasks"] = 1
        project = built_with_tasks(passport)
        first, second = project.components
        assert len(second.runnables) == 0
        assert second.name not in core_of_component(project)
        (memory,) = project.memories
        assert first.memory == ((memory.name, 16_384),)
        assert second.memory == ((memory.name, 0),)

    def test_large_bench_has_a_unique_largest_consumer(self) -> None:
        text = (ROOT / "tests/fixtures/specimen-acceptance/large-bench.json").read_text(encoding="utf-8")
        project = build_project(SpecimenPreset(DEFAULT_NAME, read_passport(text)))
        totals = [sum(amount for _, amount in c.memory) for c in project.components]
        assert totals[0] == 16_384
        assert max(totals[1:]) == 8_192
        assert all(total > 0 for total in totals)
        cores = core_of_component(project)
        memory_of_core = first_memory_of_core(project)
        assert len(memory_of_core) == len(project.memories) == len(project.cores) == 4
        for component in project.components:
            positive = [name for name, amount in component.memory if amount]
            assert positive == [memory_of_core[cores[component.name]]]
        for memory in project.memories:
            consumed = sum(dict(c.memory)[memory.name] for c in project.components)
            assert memory.size_bytes == memory_size(consumed)
            assert consumed < memory.threshold * memory.size_bytes


def tasks_passport(cores: int, partitions: int, memories: int) -> dict[str, Any]:
    """The neutral passport with one runnable per component, two busy tasks and these hardware counts.

    It has two components; the hardware, task and sequence identifier lengths are unstated.
    """
    passport = passport_with(cores, partitions, memories, 0)
    for layer in ("hardware", "tasks", "sequences"):
        del passport["identifiers"][layer]
    passport["components"]["runnables"] = 2
    passport["layers"]["tasks"] = 2
    passport["layers"]["sequences"] = 0
    return passport


def built_with_tasks(passport: dict[str, Any]) -> Project:
    """The project of a feasible passport, built whole (the self-check included)."""
    preset = SpecimenPreset(DEFAULT_NAME, passport)
    assert check_feasibility(passport) == []
    build_specimen_files(preset)
    return build_project(preset)


def first_memory_of_core(project: Project) -> dict[str, str]:
    """Each core's first memory, the one its components' budgets sit on."""
    first: dict[str, str] = {}
    for memory in project.memories:
        first.setdefault(memory.accessible[0], memory.name)
    return first


def core_of_component(project: Project) -> dict[str, str]:
    """Each component that runs in a task, by the core of that task."""
    return {component: task.core for task in project.tasks for component, _ in task.runnables}


# --- the hardware-topology refusals -------------------------------------------------------------


class TestHardwareTopology:
    def test_no_core_with_hardware_is_refused(self) -> None:
        passport = passport_with(0, 0, 1, 0)
        assert hardware_refusals(passport) == [("hardware.cores", HARDWARE_TOPOLOGY)]

    def test_one_core_is_accepted(self) -> None:
        assert hardware_refusals(passport_with(1, 0, 1, 0)) == []

    def test_partitions_without_a_core_are_refused(self) -> None:
        passport = passport_with(0, 2, 0, 0)
        assert ("hardware.partitions", HARDWARE_TOPOLOGY) in hardware_refusals(passport)

    def test_partitions_with_a_core_are_accepted(self) -> None:
        assert hardware_refusals(passport_with(1, 2, 0, 0)) == []

    def test_more_rated_than_partitions_is_refused(self) -> None:
        passport = passport_with(1, 2, 0, 3)
        assert hardware_refusals(passport) == [("hardware.rated_partitions", HARDWARE_TOPOLOGY)]

    def test_every_partition_rated_is_accepted(self) -> None:
        assert hardware_refusals(passport_with(1, 2, 0, 2)) == []

    @pytest.mark.parametrize("layer", [3, 5])
    def test_a_layer_total_off_the_sum_is_refused(self, layer: int) -> None:
        passport = passport_with(1, 1, 1, 0, layer=layer)
        assert hardware_refusals(passport) == [("layers.hardware", HARDWARE_TOPOLOGY)]

    def test_a_layer_total_at_the_sum_is_accepted(self) -> None:
        assert hardware_refusals(passport_with(1, 1, 1, 0, layer=4)) == []

    def test_an_unstated_layer_total_is_accepted(self) -> None:
        passport = passport_with(2, 1, 1, 0)
        del passport["layers"]["hardware"]
        assert hardware_refusals(passport) == []
        plan = hardware_of(passport)
        assert len(hardware_names(specimen_project(plan))) == 5

    def test_no_hardware_at_all_is_later_shape(self) -> None:
        passport = passport_with(0, 0, 0, 0, layer=0)
        assert hardware_refusals(passport) == [("layers.hardware", LATER_SHAPE)]

    def test_the_refusal_sentences_name_the_counts(self) -> None:
        passport = passport_with(1, 2, 0, 3, layer=9)
        sentences = {r.leaf: r.sentence for r in check_feasibility(passport) if r.refusal_class == HARDWARE_TOPOLOGY}
        assert sentences == {
            "layers.hardware": (
                "states 9 hardware elements; one processor, 1 cores, 2 partitions and 0 memories are 4"
            ),
            "hardware.rated_partitions": "states 3 rated partitions of 2 partitions",
        }


def with_components(passport: dict[str, Any], components: int) -> dict[str, Any]:
    """The passport with `components` one-port components wired in pairs through one interface.

    `layers.hardware` is unstated.
    """
    passport["layers"]["swc"] = components
    passport["components"]["ports"] = {"total": components}
    passport["layers"]["connectors"] = passport["connectors"]["assembly"] = components // 2
    passport["connectors"]["links"] = 0
    passport["interfaces"]["fan_out"] = {"max": components}
    del passport["layers"]["hardware"]
    return passport


class TestTheHardwareCeiling:
    """Every count at or below the ceiling, but the construction multiplies them.

    A budget per component per memory and 1 + cores + partitions + memories elements with
    `layers.hardware` unstated (each memory is one core's).
    """

    def test_ten_thousand_components_and_memories_are_refused_before_any_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # This vector once passed the check and began 100 million budget entries. It trips two
        # ceiling checks, the hardware elements and the budgets; the next test's trips the budgets
        # alone, so each check refuses before any construction runs.
        external = with_components(passport_with(1, 0, 10_000, 0), 10_000)
        budgets_only = with_components(passport_with(1, 0, 101, 0), 100)
        refusals, budget_refusals = refused_before_construction(monkeypatch, [external, budgets_only])
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("hardware.memories", CEILING)]
        assert "10002 hardware elements" in refusals[0].sentence
        assert [(r.leaf, r.refusal_class) for r in budget_refusals] == [("hardware.memories", CEILING)]
        assert "10100 in all" in budget_refusals[0].sentence

    def test_budgets_above_the_ceiling_are_refused(self) -> None:
        passport = with_components(passport_with(1, 0, 101, 0), 100)
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("hardware.memories", CEILING)]
        assert "100 components declares a budget on every memory, 10100 in all" in (refusals[0].sentence)

    def test_budgets_at_the_ceiling_are_not(self) -> None:
        passport = with_components(passport_with(1, 0, 100, 0), 100)
        assert hardware_refusals(passport) == []

    def test_memory_access_no_longer_multiplies_by_the_cores(self) -> None:
        # Each memory is accessible from one core, so memories x cores is no construction.
        assert hardware_refusals(passport_with(3, 0, 3334, 0)) == []

    def test_hardware_elements_above_the_ceiling_are_refused_at_the_largest_count(self) -> None:
        passport = passport_with(1, 9_999, 0, 0)
        del passport["layers"]["hardware"]
        refusals = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("hardware.partitions", CEILING)]
        assert "10001 hardware elements" in refusals[0].sentence

    def test_hardware_elements_at_the_ceiling_are_not(self) -> None:
        passport = passport_with(1, 9_998, 0, 0)
        del passport["layers"]["hardware"]
        assert hardware_refusals(passport) == []


# --- tasks and requirements at zero; the partition in the contract and the oracle ---------------


class TestTheRenderedTree:
    def test_zero_tasks_and_requirements_give_empty_layer_directories(self) -> None:
        plan = hardware_of(passport_with(2, 2, 1, 1))
        file_map, computed = rendered(specimen_project(plan))
        for layer in ("tasks", "requirements"):
            assert file_map[f"model/{layer}/.gitkeep"] == b""
            assert not [p for p in file_map if p.startswith(f"model/{layer}/") and p.endswith("l")]
        assert computed["layers"]["tasks"] == computed["layers"]["requirements"] == 0

    def test_a_rated_partition_passes_the_contract_and_the_oracle(self) -> None:
        plan = hardware_of(passport_with(1, 2, 0, 1))
        file_map, _ = rendered(specimen_project(plan))
        rated, unrated = (yaml.safe_load(file_map[f"model/hardware/{p.name}.yaml"]) for p in plan.partitions)
        assert rated == {
            "name": plan.partitions[0].name,
            "kind": "partition",
            "core": plan.cores[0].name,
            "asil": "QM",
        }
        assert "asil" not in unrated

    def test_the_oracle_refuses_a_partition_rated_off_the_model(self) -> None:
        plan = hardware_of(passport_with(1, 1, 0, 1))
        project = specimen_project(plan)
        file_map = build_file_map(project)
        path = f"model/hardware/{plan.partitions[0].name}.yaml"
        file_map[path] = file_map[path].replace(b"asil: QM", b"asil: B")
        with pytest.raises(GenerationError, match="partition"):
            validate_file_map(file_map, project, task_overhead_us=1)

    def test_the_oracle_refuses_an_unrated_partition_that_turns_rated(self) -> None:
        plan = hardware_of(passport_with(1, 1, 0, 0))
        project = specimen_project(plan)
        file_map = build_file_map(project)
        path = f"model/hardware/{plan.partitions[0].name}.yaml"
        file_map[path] = file_map[path] + b"asil: QM\n"
        with pytest.raises(GenerationError, match="partition"):
            validate_file_map(file_map, project, task_overhead_us=1)

    def test_an_explicit_partition_on_an_unknown_core_is_refused(self) -> None:
        plan = hardware_of(passport_with(1, 1, 0, 0))
        project = specimen_project(plan)
        broken = dataclasses.replace(project, partitions=(Partition(plan.partitions[0].name, "Nowhere", ASIL.ABSENT),))
        with pytest.raises(GenerationError, match="partition"):
            validate_project(broken)

    def test_a_task_beside_explicit_partitions_names_one_of_them(self) -> None:
        # An explicit partition carries tasks; a task naming none (its core's undeclared derived
        # partition) is refused.
        plan = hardware_of(passport_with(1, 1, 0, 0))
        project = specimen_project(plan)
        core = plan.cores[0].name
        unnamed = Task("Task0001", 1000, 1, core, 10, ())
        with pytest.raises(GenerationError, match="names an undeclared partition"):
            validate_project(dataclasses.replace(project, tasks=(unnamed,)))
        named = dataclasses.replace(unnamed, partition=plan.partitions[0].name)
        validate_project(dataclasses.replace(project, tasks=(named,)))

    def test_the_explicit_partition_tuple_may_be_empty(self) -> None:
        plan = hardware_of(passport_with(2, 0, 0, 0))
        assert plan.partitions == ()
        file_map, computed = rendered(specimen_project(plan))
        assert computed["hardware"]["partitions"] == 0
        assert not [p for p in file_map if "Partition_" in p]


def test_the_processor_is_one() -> None:
    plan = hardware_of(passport_with(2, 0, 0, 0))
    assert isinstance(plan.processor, Processor)
    assert isinstance(plan.cores[0], Core)


# --- partition levels ---------------------------------------------------------------------------


class TestPartitionLevels:
    def partitions(self) -> tuple[Partition, ...]:
        return (
            Partition("Pa1", "Co1", ASIL.QM),
            Partition("Pa2", "Co1", ASIL.QM),
            Partition("Pa3", "Co1", ASIL.ABSENT),
        )

    def test_a_rated_partition_takes_the_highest_level_it_runs(self) -> None:
        rated = rate_partitions(self.partitions(), {"Pa1": (ASIL.QM, ASIL.D, ASIL.B)})
        assert rated[0] == Partition("Pa1", "Co1", ASIL.D, PARTITION_COEXISTENCE)
        assert PARTITION_COEXISTENCE == ("Specimen: components of several levels share this partition by construction.")

    def test_one_level_writes_no_coexistence(self) -> None:
        rated = rate_partitions(self.partitions(), {"Pa2": (ASIL.C,)})
        assert rated[1] == Partition("Pa2", "Co1", ASIL.C)

    def test_a_rated_partition_running_nothing_stays_qm(self) -> None:
        assert rate_partitions(self.partitions(), {}) == self.partitions()

    def test_an_unrated_partition_stays_unrated_whatever_it_runs(self) -> None:
        rated = rate_partitions(self.partitions(), {"Pa3": (ASIL.QM, ASIL.D)})
        assert rated[2] == Partition("Pa3", "Co1", ASIL.ABSENT)

    def test_with_no_task_every_rated_partition_is_qm_as_before(self) -> None:
        passport = passport_with(2, 3, 1, 2)
        del passport["identifiers"]["hardware"]
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        assert project.tasks == ()
        assert [(p.asil, p.coexistence) for p in project.partitions or ()] == [
            (ASIL.QM, ""),
            (ASIL.QM, ""),
            (ASIL.ABSENT, ""),
        ]

    def test_a_built_rated_partition_runs_two_levels_with_coexistence(self) -> None:
        passport = passport_with(1, 1, 0, 1)
        del passport["identifiers"]["hardware"]
        for layer in ("tasks", "sequences"):
            del passport["identifiers"][layer]
        passport["components"]["by_declared_level"].update(A=1, D=1)
        passport["layers"]["tasks"] = 1
        passport["layers"]["sequences"] = 1
        preset = SpecimenPreset(DEFAULT_NAME, passport)
        assert check_feasibility(passport) == []
        build_specimen_files(preset)
        project = build_project(preset)
        assert [(p.asil, p.coexistence) for p in project.partitions or ()] == [(ASIL.D, PARTITION_COEXISTENCE)]
        assert project.tasks[0].coexistence != ""
