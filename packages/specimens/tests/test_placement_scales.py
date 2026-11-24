from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from swcraft_specimens.architecture import Architecture, build_architecture
from swcraft_specimens.deterministic import SplitMix64
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import Task
from swcraft_specimens.packaged import preset_path
from swcraft_specimens.placement import (
    PlacementResult,
    build_placement,
    component_demand,
    placement_fingerprint,
    validate_placement,
)
from swcraft_specimens.preset import ASIL, Preset, load_preset
from swcraft_specimens.scale_presets import scale_preset
from tests.oracle.generator_start import generator_command
from tests.oracle.swcraft_gate import assert_swcraft_clean

PYTHON = sys.executable
ROOT = Path(__file__).parents[1]
SWCRAFT = os.environ.get("SWCRAFT_BIN")

SCALES = (
    (
        (15, 3, 3, 3),
        16690,
        43520,
        3,
        2,
        21,
        "864b7ecd99381a285590aab8fbb0a29a8b4e5defa082f3f7dd84fc5526acfc46",
    ),
    (
        (60, 6, 3, 8),
        403557,
        485453,
        8,
        6,
        27,
        "771b25656e3a04699a6ab9b90bfa57c1b079fc8abb7497f73f15baea7c3ffc87",
    ),
    (
        (160, 8, 3, 20),
        3410267,
        4079130,
        20,
        16,
        27,
        "2fb3178d2f2d4818cf40991e1c61d6c3632416039e28c7d8942b213a09698390",
    ),
)


def make(scale: tuple[int, int, int, int]) -> tuple[Preset, Architecture, PlacementResult]:
    preset = scale_preset(*scale)
    rng = SplitMix64(preset.seed)
    architecture = build_architecture(preset, rng)
    return preset, architecture, build_placement(architecture, preset, rng)


def reference_loads(architecture: Architecture, placement: dict[str, str], preset: Preset) -> dict[str, Fraction]:
    keys = {(r.period_us, c.asil) for c in architecture.components for r in c.runnables}
    loads = {
        core.name: sum((Fraction(preset.task_overhead_us, period) for period, _ in keys), Fraction())
        for core in preset.cores
    }
    for component in architecture.components:
        loads[placement[component.name]] += component_demand(component)
    return loads


def reference_task_wcet(task: Task, architecture: Architecture, overhead: int) -> int:
    return overhead + sum(
        r.wcet_us
        for component in architecture.components
        if component.name in {swc for swc, _ in task.runnables}
        for r in component.runnables
        if (component.name, r.name) in task.runnables
    )


def reference_cost(architecture: Architecture, mapping: dict[str, str]) -> tuple[Fraction, Fraction]:
    sender_receiver = Fraction()
    client_server = Fraction()
    for connector in architecture.connectors:
        if mapping[connector.provider] == mapping[connector.consumer]:
            continue
        if connector.kind == "sender_receiver":
            sender_receiver += Fraction(connector.payload_bytes * 1_000_000, connector.period_us)
        else:
            client_server += Fraction(
                (connector.request_bytes + connector.response_bytes) * 1_000_000,
                connector.period_us,
            )
    return sender_receiver, client_server


@pytest.mark.parametrize("scale,planted_cost,emitted_cost,moved,pinned,tasks,fingerprint", SCALES)
def test_the_placement_scale_gate(
    scale: tuple[int, int, int, int],
    planted_cost: int,
    emitted_cost: int,
    moved: int,
    pinned: int,
    tasks: int,
    fingerprint: str,
) -> None:
    preset, architecture, result = make(scale)
    second_preset, second_architecture, second = make(scale)
    assert preset == second_preset and architecture == second_architecture and result == second
    assert placement_fingerprint(result) == fingerprint
    assert placement_fingerprint(second) == fingerprint
    assert result.planted_cost.total == planted_cost
    assert result.emitted_cost.total == emitted_cost
    assert len(result.displaced) == moved
    assert len(result.pinned) == pinned
    assert len(result.planted_tasks.tasks) == tasks
    assert len(result.emitted_tasks.tasks) == tasks

    component_names = {component.name for component in architecture.components}
    assert {name for name, _ in result.planted.mapping} == component_names
    assert {name for name, _ in result.emitted.mapping} == component_names
    for domain in architecture.domains:
        assert len({result.planted.core_for(name) for name in domain.components}) == 1
    assert set(result.pinned).isdisjoint(result.displaced)
    assert (
        tuple(name for name, core in result.emitted.mapping if core != result.planted.core_for(name))
        == result.displaced
    )
    assert all(
        load <= Fraction(preset.core_threshold) for _, load in result.planted_core_loads + result.emitted_core_loads
    )
    assert result.emitted_cost.total > result.planted_cost.total

    capabilities = {core.name: core.capability for core in preset.cores}
    for component in architecture.components:
        for placement in (result.planted, result.emitted):
            capability = capabilities[placement.core_for(component.name)]
            assert component.asil is ASIL.QM or capability is not ASIL.ABSENT
            if component.asil not in (ASIL.QM, ASIL.ABSENT):
                asil_order = (ASIL.A, ASIL.B, ASIL.C, ASIL.D)
                assert asil_order.index(component.asil) <= asil_order.index(capability)

    memory_by_name = {memory.name: memory for memory in result.memories}
    budgets = {(budget.component, budget.memory): budget.bytes for budget in result.memory_budgets}
    assert all(
        result.planted.core_for(component) in memory_by_name[memory].accessible
        for (component, memory), amount in budgets.items()
        if amount and memory != "RAM_SHARED"
    )
    assert sum(amount for (component, memory), amount in budgets.items() if memory == "RAM_SHARED") <= int(
        preset.memories.shared.size_bytes * preset.memories.shared.threshold
    )
    for core in preset.cores:
        assert sum(amount for (component, memory), amount in budgets.items() if memory == f"RAM_{core.name}") <= int(
            preset.memories.private_per_core.size_bytes * preset.memories.private_per_core.threshold
        )

    expected = {
        (component.name, runnable.name) for component in architecture.components for runnable in component.runnables
    }
    for task_set, placement, loads in (
        (result.planted_tasks, result.planted, result.planted_core_loads),
        (result.emitted_tasks, result.emitted, result.emitted_core_loads),
    ):
        refs = [ref for task in task_set.tasks for ref in task.runnables]
        assert set(refs) == expected and len(refs) == len(set(refs))
        assert all(task.name == f"Task_{task.core}_{task.period_us}us_{task.asil.value}" for task in task_set.tasks)
        assert all(
            task.wcet_us == reference_task_wcet(task, architecture, preset.task_overhead_us) for task in task_set.tasks
        )
        derived = {core.name: Fraction() for core in preset.cores}
        for task in task_set.tasks:
            derived[task.core] += Fraction(task.wcet_us, task.period_us)
        assert tuple(derived[core] for core, _ in loads) == tuple(load for _, load in loads)
        assert tuple(sorted(reference_loads(architecture, dict(placement.mapping), preset).items())) == tuple(
            sorted(loads)
        )

    assert (
        result.planted_cost.sender_receiver,
        result.planted_cost.client_server,
    ) == reference_cost(architecture, dict(result.planted.mapping))
    assert (
        result.emitted_cost.sender_receiver,
        result.emitted_cost.client_server,
    ) == reference_cost(architecture, dict(result.emitted.mapping))


def test_the_placement_fingerprint_is_python_hash_seed_independent() -> None:
    code = (
        "from swcraft_specimens.scale_presets import scale_preset; "
        "from swcraft_specimens.architecture import build_architecture; "
        "from swcraft_specimens.deterministic import SplitMix64; "
        "from swcraft_specimens.placement import build_placement, placement_fingerprint; "
        "p=scale_preset(15,3,3,3); r=SplitMix64(p.seed); "
        "print(placement_fingerprint(build_placement(build_architecture(p,r),p,r)))"
    )
    outputs = []
    for hash_seed in ("1", "999"):
        outputs.append(
            subprocess.check_output(
                [PYTHON, "-c", code],
                cwd=ROOT,
                env=dict(os.environ, PYTHONHASHSEED=hash_seed),
                text=True,
            )
        )
    assert outputs[0] == outputs[1]


def assert_generation_error(callable_: Callable[[], object], text: str) -> None:
    with pytest.raises(GenerationError, match=text):
        callable_()


def test_the_generation_constraints_refuse() -> None:
    preset, architecture, result = make((15, 3, 3, 3))
    absent = replace(preset, cores=tuple(replace(core, capability=ASIL.ABSENT) for core in preset.cores))
    assert_generation_error(lambda: build_placement(architecture, absent, SplitMix64(1)), "Dom.*no capable core")
    assert_generation_error(
        lambda: build_placement(architecture, replace(preset, core_threshold=Decimal("0.0001")), SplitMix64(1)),
        "baseline CPU overhead",
    )
    demand = sum(
        (
            component_demand(next(c for c in architecture.components if c.name == name))
            for name in architecture.domains[0].components
        ),
        Fraction(),
    )
    low_fraction = Fraction(337, 40000) + demand / 2
    low = Decimal(low_fraction.numerator) / Decimal(low_fraction.denominator)
    assert demand > Fraction(low)
    assert_generation_error(
        lambda: build_placement(architecture, replace(preset, core_threshold=low), SplitMix64(1)),
        "domain .* demand",
    )
    assert_generation_error(
        lambda: build_placement(
            architecture,
            replace(
                preset,
                memories=replace(preset.memories, shared=replace(preset.memories.shared, size_bytes=1)),
            ),
            SplitMix64(1),
        ),
        "shared memory too small",
    )
    assert_generation_error(
        lambda: build_placement(
            architecture,
            replace(
                preset,
                memories=replace(
                    preset.memories,
                    private_per_core=replace(preset.memories.private_per_core, size_bytes=1),
                ),
            ),
            SplitMix64(1),
        ),
        "private memory",
    )
    assert_generation_error(
        lambda: build_placement(
            architecture,
            replace(
                preset,
                private_memory_share=Decimal("1"),
                memories=replace(
                    preset.memories,
                    private_per_core=replace(preset.memories.private_per_core, size_bytes=1),
                ),
            ),
            SplitMix64(1),
        ),
        "private memory",
    )
    assert_generation_error(
        lambda: build_placement(architecture, replace(preset, displaced_components=14), SplitMix64(1)),
        "eligible unpinned",
    )
    assert_generation_error(
        lambda: build_placement(
            replace(architecture, connectors=()),
            replace(preset, displaced_components=1),
            SplitMix64(1),
        ),
        "exchange cost",
    )


def test_the_corruption_constraints_refuse() -> None:
    preset, architecture, result = make((15, 3, 3, 3))
    pinned = result.pinned[0]
    new_core = next(core.name for core in preset.cores if core.name != result.planted.core_for(pinned))
    emitted_map = dict(result.emitted.mapping)
    emitted_map[pinned] = new_core
    moved = tuple(name for name, core in emitted_map.items() if core != result.planted.core_for(name))
    assert_generation_error(
        lambda: validate_placement(
            replace(result, emitted=type(result.emitted)(tuple(emitted_map.items())), displaced=moved),
            preset,
        ),
        "pinned component",
    )
    assert_generation_error(lambda: validate_placement(replace(result, displaced=()), preset), "displaced mapping")
    assert_generation_error(
        lambda: validate_placement(
            replace(
                result,
                emitted_tasks=replace(result.emitted_tasks, tasks=result.emitted_tasks.tasks[:-1]),
            ),
            preset,
        ),
        "topology",
    )
    duplicate = replace(
        result.emitted_tasks.tasks[0],
        runnables=result.emitted_tasks.tasks[0].runnables + result.emitted_tasks.tasks[0].runnables[:1],
    )
    assert_generation_error(
        lambda: validate_placement(
            replace(
                result,
                emitted_tasks=replace(result.emitted_tasks, tasks=(duplicate,) + result.emitted_tasks.tasks[1:]),
            ),
            preset,
        ),
        "topology",
    )
    bad_wcet = replace(result.emitted_tasks.tasks[0], wcet_us=result.emitted_tasks.tasks[0].wcet_us + 1)
    assert_generation_error(
        lambda: validate_placement(
            replace(
                result,
                emitted_tasks=replace(result.emitted_tasks, tasks=(bad_wcet,) + result.emitted_tasks.tasks[1:]),
            ),
            preset,
        ),
        "WCET mismatch",
    )
    bad_core = replace(result.emitted_tasks.tasks[0], core="CORE_NOT_THE_TASK_CORE")
    assert_generation_error(
        lambda: validate_placement(
            replace(
                result,
                emitted_tasks=replace(result.emitted_tasks, tasks=(bad_core,) + result.emitted_tasks.tasks[1:]),
            ),
            preset,
        ),
        "topology",
    )
    assert_generation_error(
        lambda: validate_placement(replace(result, memory_budgets=result.memory_budgets[:-1]), preset),
        "memory budget",
    )
    budgets = list(result.memory_budgets)
    positive = next(index for index, budget in enumerate(budgets) if budget.memory != "RAM_SHARED" and budget.bytes > 0)
    budget = budgets[positive]
    wrong_memory = next(
        memory.name
        for memory in result.memories
        if memory.name.startswith("RAM_") and memory.name != "RAM_SHARED" and memory.name != budget.memory
    )
    zero_index = next(
        index
        for index, candidate in enumerate(budgets)
        if candidate.component == budget.component and candidate.memory == wrong_memory
    )
    budgets[zero_index] = replace(budgets[zero_index], bytes=budget.bytes)
    assert_generation_error(
        lambda: validate_placement(replace(result, memory_budgets=tuple(budgets)), preset),
        "inaccessible",
    )
    loads = list(result.emitted_core_loads)
    loads[0] = (loads[0][0], loads[0][1] + 1)
    assert_generation_error(
        lambda: validate_placement(replace(result, emitted_core_loads=tuple(loads)), preset),
        "CPU load mismatch",
    )


@pytest.mark.parametrize("label", ["planted", "emitted"])
def test_the_capability_rule_refuses_a_component_on_a_core_below_its_level(label: str) -> None:
    # With one unrated partition per core, zero findings do not prove a component sits on a core able
    # to host its level; this rule proves it on the planted and on the emitted mapping.
    preset = load_preset(preset_path("small"))
    rng = SplitMix64(preset.seed)
    result = build_placement(build_architecture(preset, rng), preset, rng)
    validate_placement(result, preset)
    asil_order = (ASIL.QM, ASIL.A, ASIL.B, ASIL.C, ASIL.D)
    lowest = min(
        (core for core in preset.cores if core.capability is not ASIL.ABSENT),
        key=lambda core: asil_order.index(core.capability),
    )
    component = next(
        c
        for c in result.architecture.components
        if c.asil is not ASIL.QM
        and asil_order.index(c.asil) > asil_order.index(lowest.capability)
        and c.name not in result.pinned
    )
    placement = getattr(result, label)
    mapping = tuple((name, lowest.name if name == component.name else core) for name, core in placement.mapping)
    moved = replace(result, **{label: type(placement)(mapping)})
    assert_generation_error(
        lambda: validate_placement(moved, preset),
        rf"{label} placement: component {component.name} ASIL {component.asil.value} "
        rf"exceeds capability of {lowest.name}",
    )


def test_qm_is_not_an_ordinal_below_a() -> None:
    from swcraft_specimens.placement import _capable

    assert _capable(ASIL.D, ASIL.QM)
    assert not _capable(ASIL.ABSENT, ASIL.QM)
    assert not _capable(ASIL.QM, ASIL.A)


@pytest.mark.integration
def test_general_swcraft_gate(tmp_path: Path) -> None:
    if not SWCRAFT:
        pytest.skip("SWCRAFT_BIN is not set")
    output = tmp_path / "general"
    generated = subprocess.run(
        [
            *generator_command(),
            "generate",
            str(ROOT / "tests/fixtures/small_scale.yaml"),
            "--out",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert generated.returncode == 0, generated.stderr
    assert_swcraft_clean(
        SWCRAFT,
        output / "swcraft.yaml",
        [
            ("check-model", "--format", "json"),
            ("validate",),
            ("generate", "arxml"),
            ("generate", "diagrams"),
            ("generate", "needs"),
            ("resource-report",),
        ],
    )
    assert (output / "dist/needs.json").is_file()
