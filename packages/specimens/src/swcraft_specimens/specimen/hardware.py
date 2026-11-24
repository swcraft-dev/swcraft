"""Hardware, the partitions' levels and the per-core RAM.

The per-core RAM is restated from SWCraft's benchmark generator (`PROVENANCE.md`); nothing is
imported from SWCraft.

One processor; `hardware.cores` cores (capability `D`, enforcement `strict`, threshold 0.7);
`hardware.partitions` partitions, partition i on core i mod cores, the first
`hardware.rated_partitions` rated; `hardware.memories` RAM memories (`strict`, threshold 0.8),
memory j accessible from core j mod cores alone. Names are a stem and an index (`Cp1`, `Co1`,
`Me1`, `Pa1`, ...), padded by `identifiers.py`.

:func:`memory_budgets`: component i budgets `component_budget(i, k)` on the first memory of the core
its tasks run on and 0 on every other memory, so component 0 is the unique largest consumer; every
component keeps an entry per memory, since SWCraft reports `hardware.missing-budget` otherwise. Each
memory is sized about 60 % used. :func:`rate_partitions`: a rated partition takes the highest
effective level among the components its tasks run (`QM` when none), and writes
`PARTITION_COEXISTENCE` with two or more levels; an unrated one stays unrated.

The `hardware-topology` refusals before generation, each at its leaf:

- `hardware.cores`: no core while the hardware is not empty;
- `hardware.partitions`: partitions with no core to sit on;
- `hardware.rated_partitions`: more rated partitions than partitions;
- `layers.hardware`: a stated count other than 1 + cores + partitions + memories (one processor);
  a stated 0 is `later-shape` at that leaf.

The `ceiling` refusals (`hardware_ceiling_refusals`): the components x memories budget entries and,
with `layers.hardware` unstated, the hardware elements, each above `common.COUNT_CEILING`. A real
model's hardware matches its specimen's in the four counts alone.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..errors import GenerationError
from ..model import Core, Memory, Partition, Processor
from ..passport import Refusal
from ..placement import ASIL_ORDER
from ..preset import ASIL
from .common import CEILING_SENTENCE, COUNT_CEILING, ceil_div
from .vocabulary import (
    CEILING,
    CORES,
    HARDWARE_TOPOLOGY,
    LATER_SHAPE,
    LAYER_HARDWARE,
    LAYER_SWC,
    MEMORIES,
    PARTITIONS,
    RATED_PARTITIONS,
)

#: The counts the hardware layer's element count comes from.
HARDWARE_COUNTS = frozenset({CORES, PARTITIONS, MEMORIES})

#: Every core's threshold and capability.
THRESHOLD = Decimal("0.7")
CORE_CAPABILITY = ASIL.D
#: Every memory's threshold and its least size, also the page its size is rounded up to.
MEMORY_THRESHOLD = Decimal("0.8")
MEMORY_LEAST_BYTES = 4_096
#: The share of a memory its budgets use, about 60 %: USE_NUMERATOR / USE_DENOMINATOR.
USE_NUMERATOR, USE_DENOMINATOR = 6, 10
#: The largest component's budget and the step of the ordinary budgets.
HUB_BUDGET_BYTES = 16_384
BUDGET_STEP_BYTES = 512
#: The level of a rated partition before `rate_partitions` rates it by what it runs.
RATED_LEVEL = ASIL.QM
#: A rated partition's argument for running components of several effective levels.
PARTITION_COEXISTENCE = "Specimen: components of several levels share this partition by construction."


@dataclass(frozen=True)
class HardwareSpec:
    """The stated hardware leaves; `layer` is `layers.hardware`, or `None` when unstated."""

    cores: int
    partitions: int
    memories: int
    rated: int
    layer: int | None

    @property
    def elements(self) -> int:
        """The hardware elements the four counts build: one processor and the rest."""
        return 1 + self.cores + self.partitions + self.memories


@dataclass(frozen=True)
class HardwareNames:
    """The identifier plan's hardware names, per kind (`identifiers.hardware_names_by_kind`)."""

    processor: str
    cores: tuple[str, ...]
    memories: tuple[str, ...]
    partitions: tuple[str, ...]


@dataclass(frozen=True)
class HardwarePlan:
    """The processor, the cores, the memories and the explicit partitions, in emitted order."""

    processor: Processor
    cores: tuple[Core, ...]
    memories: tuple[Memory, ...]
    partitions: tuple[Partition, ...]


def hardware_spec(stated: Mapping[str, Any]) -> HardwareSpec:
    """The hardware leaves of a stated passport (dotted paths).

    The schema requires the four counts, and an unstated `layers.hardware` follows them.
    """
    layer = stated.get(LAYER_HARDWARE)
    return HardwareSpec(
        cores=int(stated[CORES]),
        partitions=int(stated[PARTITIONS]),
        memories=int(stated[MEMORIES]),
        rated=int(stated[RATED_PARTITIONS]),
        layer=None if layer is None else int(layer),
    )


def hardware_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    """The `hardware-topology` refusals and the `later-shape` refusal of an empty hardware."""
    spec = hardware_spec(stated)
    refusals: list[Refusal] = []
    if spec.layer == 0:
        refusals.append(
            Refusal(
                LAYER_HARDWARE,
                LATER_SHAPE,
                "states no hardware; this version builds one processor and at least one core",
            )
        )
    elif spec.layer is not None and spec.layer != spec.elements:
        refusals.append(
            Refusal(
                LAYER_HARDWARE,
                HARDWARE_TOPOLOGY,
                f"states {spec.layer} hardware elements; one processor, {spec.cores} cores, "
                f"{spec.partitions} partitions and {spec.memories} memories are {spec.elements}",
            )
        )
    if spec.cores < 1 and spec.layer != 0:
        refusals.append(Refusal(CORES, HARDWARE_TOPOLOGY, "states no core; a project needs one core"))
    if spec.partitions > 0 and spec.cores < 1:
        refusals.append(
            Refusal(
                PARTITIONS,
                HARDWARE_TOPOLOGY,
                f"states {spec.partitions} partitions and no core for them to sit on",
            )
        )
    if spec.rated > spec.partitions:
        refusals.append(
            Refusal(
                RATED_PARTITIONS,
                HARDWARE_TOPOLOGY,
                f"states {spec.rated} rated partitions of {spec.partitions} partitions",
            )
        )
    return refusals


def hardware_ceiling_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    """The `ceiling` refusals of what the hardware construction multiplies or adds up from counts.

    Each count is at or below `COUNT_CEILING` (the caller has refused any above it). With
    `layers.hardware` unstated, the 1 + cores + partitions + memories elements, at the largest of
    the three counts; the budget every component declares on every memory (components x
    memories), at `hardware.memories`. Every factor is a stated leaf (`layers.swc` and the
    hardware counts are required), so the stated leaves alone decide each refusal and its class
    is `ceiling`.
    """
    spec = hardware_spec(stated)
    ceiling = CEILING_SENTENCE
    refusals: list[Refusal] = []
    if spec.layer is None and spec.elements > COUNT_CEILING:
        largest = max((CORES, PARTITIONS, MEMORIES), key=lambda leaf: int(stated[leaf]))
        refusals.append(
            Refusal(
                largest,
                CEILING,
                f"states {int(stated[largest])}; one processor, {spec.cores} cores, "
                f"{spec.partitions} partitions and {spec.memories} memories are "
                f"{spec.elements} hardware elements, and {ceiling}",
            )
        )
    components = int(stated[LAYER_SWC])
    if components * spec.memories > COUNT_CEILING:
        refusals.append(
            Refusal(
                MEMORIES,
                CEILING,
                f"states {spec.memories}; each of the {components} components declares a budget "
                f"on every memory, {components * spec.memories} in all, and {ceiling}",
            )
        )
    return refusals


def build_hardware(stated: Mapping[str, Any], names: HardwareNames) -> HardwarePlan:
    """The hardware of a feasible passport, named by the identifier plan per kind.

    Memory j is accessible from core j mod cores alone and sized for no budget (`memory_budgets`
    sizes it).
    """
    spec = hardware_spec(stated)
    core_names, memory_names = names.cores, names.memories
    partition_names = names.partitions
    if (len(core_names), len(memory_names), len(partition_names)) != (
        spec.cores,
        spec.memories,
        spec.partitions,
    ):
        raise GenerationError("the identifier plan's hardware names differ from the counts")
    cores = tuple(Core(name, THRESHOLD, CORE_CAPABILITY) for name in core_names)
    memories = tuple(
        Memory(name, MEMORY_LEAST_BYTES, MEMORY_THRESHOLD, (core_names[j % len(core_names)],))
        for j, name in enumerate(memory_names)
    )
    partitions = tuple(
        Partition(
            name,
            core_names[index % len(core_names)],
            RATED_LEVEL if index < spec.rated else ASIL.ABSENT,
        )
        for index, name in enumerate(partition_names)
    )
    return HardwarePlan(Processor(names.processor), cores, memories, partitions)


def rate_partitions(partitions: Sequence[Partition], levels: Mapping[str, Sequence[ASIL]]) -> tuple[Partition, ...]:
    """Each rated partition at the highest effective level its tasks run; an unrated one unchanged.

    `levels` is by partition name, distinct as `tasks.TaskPlan.partition_levels` holds them; `QM`
    for one that runs none; with `PARTITION_COEXISTENCE` when they run two or more.
    """
    rated: list[Partition] = []
    for partition in partitions:
        runs = levels.get(partition.name, ())
        if partition.asil is ASIL.ABSENT or not runs:
            rated.append(partition)
            continue
        highest = max(runs, key=ASIL_ORDER.__getitem__)
        coexistence = PARTITION_COEXISTENCE if len(runs) > 1 else ""
        rated.append(dataclasses.replace(partition, asil=highest, coexistence=coexistence))
    return tuple(rated)


def component_budget(index: int, hubs: int) -> int:
    """Component `index`'s budget in component order, `hubs` the hubs.

    16,384 >> i, at least 512, for i < max(hubs, 1), else 512 x (1 + 7i mod 8), at most 4,096.
    """
    if index < max(hubs, 1):
        return max(BUDGET_STEP_BYTES, HUB_BUDGET_BYTES >> index)
    return BUDGET_STEP_BYTES * (1 + (7 * index) % 8)


def memory_size(consumed: int) -> int:
    """A memory's size for budgets summing to `consumed`, so they use about 60 % of it.

    max(4,096, ceil(consumed x 10 / 6 / 4,096) x 4,096) bytes.
    """
    needed = ceil_div(consumed * USE_DENOMINATOR, USE_NUMERATOR)
    return max(MEMORY_LEAST_BYTES, ceil_div(needed, MEMORY_LEAST_BYTES) * MEMORY_LEAST_BYTES)


def memory_budgets(
    plan: HardwarePlan, cores: Sequence[str | None], hubs: int
) -> tuple[tuple[tuple[tuple[str, int], ...], ...], tuple[Memory, ...]]:
    """Every component's memory map, in memory order, and the memories sized for them.

    `cores` holds, per component in component order, the core its tasks run on, `None` for one
    in no task; `hubs` the hubs. A component's budget sits on the first memory of its core
    (memory j is core j mod cores'), 0 on every other memory; with no core, or a core with no
    memory, it is 0 everywhere."""
    first_memory: dict[str, str] = {}
    for memory in plan.memories:
        first_memory.setdefault(memory.accessible[0], memory.name)
    maps: list[tuple[tuple[str, int], ...]] = []
    consumed = dict.fromkeys((memory.name for memory in plan.memories), 0)
    for index, core in enumerate(cores):
        own = first_memory.get(core) if core is not None else None
        budget = component_budget(index, hubs) if own is not None else 0
        maps.append(tuple((memory.name, budget if memory.name == own else 0) for memory in plan.memories))
        if own is not None:
            consumed[own] += budget
    memories = tuple(
        dataclasses.replace(memory, size_bytes=memory_size(consumed[memory.name])) for memory in plan.memories
    )
    return tuple(maps), memories
