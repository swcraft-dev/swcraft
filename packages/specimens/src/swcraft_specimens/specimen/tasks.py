"""Tasks and sequences, restated from SWCraft's benchmark workload generator (`PROVENANCE.md`).

Nothing is imported from SWCraft. Notation: n components, Rn = `components.runnables`,
T = `layers.tasks`, I = `tasks.idle`, B = T - I the **busy** tasks, Q = `layers.sequences`,
K = `hardware.partitions`. A component's **effective level** is its declared level, `QM` when none.

The runnables, in component order, are split into B contiguous chunks whose sizes differ by at most
one (`chunk_sizes`); busy task k runs chunk k every 10, 20, 30, 40 or 50 ms (k mod 5), idle task j
every 100 ms, and each mapped runnable gains one `timing` event at its task's period. A component's
runnables never run in two partitions: the busy tasks are laid in contiguous blocks cut only at
component boundaries, block b naming partition b; idle task j names partition j mod K. Priorities
are rate-monotonic on every core. Every task writes `wcet: 1us`; a task whose components have two
or more effective levels writes `TASK_COEXISTENCE`. Sequence j steps through busy task j mod B,
its first min(3, chunk size) runnables; an unstated Q is B.

The refusals before generation (`task_refusals`, `task_ceiling_refusals`), each at its leaf:

- `sum` at `tasks.idle`: I > T (the other task refusals are then not looked for);
- `tasks` at `layers.tasks`: B above a stated Rn; T > 0 with K = 0; B > 0 with n = 0 and Rn
  unstated;
- `tasks` at `layers.sequences`: Q > 0 with B = 0;
- `ceiling` at `layers.sequences`: the sequence steps above `COUNT_CEILING`.

An unstated Rn is max(n, B) when n >= 1 and 0 when n = 0. The periods, priorities and partition
blocks are fixed choices no passport leaf states, so a real model's tasks match its specimen's in
the counts alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..model import Partition, Task, TaskSequence
from ..passport import Refusal
from ..preset import ASIL
from .common import CEILING_SENTENCE, COUNT_CEILING, stated_count
from .vocabulary import (
    CEILING,
    LAYER_SEQUENCES,
    LAYER_SWC,
    LAYER_TASKS,
    PARTITIONS,
    RUNNABLES,
    SUM,
    TASKS_CLASS,
    TASKS_IDLE,
)

#: The busy tasks' periods, by k mod 5 (`workload.py`'s `period_options_s`), in microseconds.
BUSY_PERIODS_US = (10_000, 20_000, 30_000, 40_000, 50_000)
#: An idle task's period.
IDLE_PERIOD_US = 100_000
#: Every task's budget.
TASK_WCET_US = 1
#: The most steps a sequence takes.
STEPS_PER_SEQUENCE = 3
#: A task's argument for running components of several effective levels.
TASK_COEXISTENCE = "Specimen: components of several levels share this task by construction."
#: The level of a component that declares none.
EFFECTIVE_DEFAULT = ASIL.QM


@dataclass(frozen=True)
class TaskComponent:
    """What the tasks read of one component: its name, its runnable count and its declared level."""

    name: str
    runnables: int
    level: ASIL


@dataclass(frozen=True)
class TaskPlan:
    """The task plan: the tasks, the sequences, the runnables' periods and the partitions' levels.

    The tasks are busy, then idle; the periods are each mapped runnable's, the levels each running
    partition's distinct effective levels.
    """

    tasks: tuple[Task, ...]
    sequences: tuple[TaskSequence, ...]
    periods: dict[tuple[str, str], int]
    partition_levels: dict[str, tuple[ASIL, ...]]


def effective_level(level: ASIL) -> ASIL:
    """A component's effective level: its declared one, `QM` when it declares none."""
    return EFFECTIVE_DEFAULT if level is ASIL.ABSENT else level


def busy_tasks(stated: Mapping[str, Any]) -> int:
    """B = T - I, or 0 when I > T (refused `sum`)."""
    return max(0, stated_count(stated, LAYER_TASKS) - stated_count(stated, TASKS_IDLE))


def runnable_count(stated: Mapping[str, Any]) -> int:
    """Rn: the stated count, else max(n, B) with n >= 1 and 0 with n = 0."""
    if RUNNABLES in stated:
        return int(stated[RUNNABLES])
    n = stated_count(stated, LAYER_SWC)
    return max(n, busy_tasks(stated)) if n else 0


def sequence_count(stated: Mapping[str, Any]) -> int:
    """Q: the stated count, else B."""
    if LAYER_SEQUENCES in stated:
        return int(stated[LAYER_SEQUENCES])
    return busy_tasks(stated)


def chunk_sizes(runnables: int, busy: int) -> list[int]:
    """The busy tasks' chunk sizes, larger first.

    `runnables` split into `busy` contiguous chunks differing by at most one.
    """
    if busy <= 0:
        return []
    per, extra = divmod(runnables, busy)
    return [per + 1 if index < extra else per for index in range(busy)]


def sequence_step_count(runnables: int, busy: int, sequences: int) -> int:
    """The steps Q sequences take over B chunks of Rn runnables, as the builder slices them.

    Q is a stated count at most `COUNT_CEILING` (a larger one is refused first) or B, so the sum is short.
    """
    sizes = chunk_sizes(runnables, busy)
    if not sizes:
        return 0
    return sum(min(STEPS_PER_SEQUENCE, sizes[j % busy]) for j in range(sequences))


def task_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    """The `sum` and `tasks` refusals of the tasks and sequences, at most one per leaf."""
    tasks, idle = stated_count(stated, LAYER_TASKS), stated_count(stated, TASKS_IDLE)
    if idle > tasks:
        return [Refusal(TASKS_IDLE, SUM, f"states {idle} idle tasks; layers.tasks states {tasks} tasks")]
    busy = tasks - idle
    refusals: list[Refusal] = []
    if RUNNABLES in stated and busy > int(stated[RUNNABLES]):
        runnables = int(stated[RUNNABLES])
        refusals.append(
            Refusal(
                LAYER_TASKS,
                TASKS_CLASS,
                f"states {tasks} tasks with {idle} idle; the {runnables} runnables fill at most {runnables} busy tasks",
            )
        )
    elif tasks > 0 and stated_count(stated, PARTITIONS) == 0:
        refusals.append(
            Refusal(
                LAYER_TASKS,
                TASKS_CLASS,
                f"a task runs in a partition; {PARTITIONS} states 0",
            )
        )
    elif busy > 0 and stated_count(stated, LAYER_SWC) == 0 and RUNNABLES not in stated:
        refusals.append(
            Refusal(
                LAYER_TASKS,
                TASKS_CLASS,
                f"states {busy} busy tasks; no component holds a runnable",
            )
        )
    sequences = sequence_count(stated)
    if sequences > 0 and busy == 0:
        refusals.append(
            Refusal(
                LAYER_SEQUENCES,
                TASKS_CLASS,
                f"states {sequences}; a sequence steps through a task's runnables; no task runs one",
            )
        )
    return refusals


def task_ceiling_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    """The `ceiling` refusal of the sequences' steps: each factor is within the ceiling, the sum need not be."""
    tasks, idle = stated_count(stated, LAYER_TASKS), stated_count(stated, TASKS_IDLE)
    if idle > tasks:
        return []
    steps = sequence_step_count(runnable_count(stated), tasks - idle, sequence_count(stated))
    if steps <= COUNT_CEILING:
        return []
    return [
        Refusal(
            LAYER_SEQUENCES,
            CEILING,
            f"writes {steps} steps; {CEILING_SENTENCE}",
        )
    ]


def _cut_blocks(sizes: Sequence[int], components: Sequence[TaskComponent], blocks: int) -> list[int]:
    """Each busy task's block (its partition's index).

    K contiguous blocks cut only where a chunk boundary is also a component boundary, the cuts
    chosen evenly.
    """
    bounds: set[int] = set()
    position = 0
    for component in components:
        position += component.runnables
        bounds.add(position)
    cuts: list[int] = []
    position = 0
    for index, size in enumerate(sizes[:-1]):
        position += size
        if position in bounds:
            cuts.append(index + 1)
    chosen_count = min(blocks, len(cuts) + 1) - 1
    chosen = [cuts[((2 * j + 1) * len(cuts)) // (2 * chosen_count)] for j in range(chosen_count)]
    result: list[int] = []
    block = 0
    for task in range(len(sizes)):
        if block < len(chosen) and task == chosen[block]:
            block += 1
        result.append(block)
    return result


def build_tasks(
    components: Sequence[TaskComponent],
    busy: int,
    idle: int,
    partitions: Sequence[Partition],
    task_names: Sequence[str],
    sequence_names: Sequence[str],
) -> TaskPlan:
    """The tasks and sequences over these components and partitions, in their order."""
    runnables = [
        (component.name, f"R{index}", effective_level(component.level))
        for component in components
        for index in range(1, component.runnables + 1)
    ]
    sizes = chunk_sizes(len(runnables), busy)
    blocks = _cut_blocks(sizes, components, len(partitions)) if busy else []
    chunks: list[list[tuple[str, str, ASIL]]] = []
    start = 0
    for size in sizes:
        chunks.append(runnables[start : start + size])
        start += size
    periods = [BUSY_PERIODS_US[k % len(BUSY_PERIODS_US)] for k in range(busy)]
    periods += [IDLE_PERIOD_US] * idle
    ranked = sorted(range(busy + idle), key=lambda index: (-periods[index], index))
    priorities = {index: rank + 1 for rank, index in enumerate(ranked)}
    named = [partitions[block] for block in blocks]
    named += [partitions[j % len(partitions)] for j in range(idle)]
    tasks: list[Task] = []
    mapped: dict[tuple[str, str], int] = {}
    partition_levels: dict[str, list[ASIL]] = {}
    for index, name in enumerate(task_names):
        chunk = chunks[index] if index < busy else []
        levels: list[ASIL] = []
        for component, runnable, level in chunk:
            mapped[(component, runnable)] = periods[index]
            if level not in levels:
                levels.append(level)
        partition = named[index]
        if chunk:
            seen = partition_levels.setdefault(partition.name, [])
            seen.extend(level for level in levels if level not in seen)
        tasks.append(
            Task(
                name,
                periods[index],
                priorities[index],
                partition.core,
                TASK_WCET_US,
                tuple((component, runnable) for component, runnable, _ in chunk),
                partition=partition.name,
                coexistence=TASK_COEXISTENCE if len(levels) > 1 else "",
            )
        )
    sequences = tuple(
        TaskSequence(
            name,
            tasks[j % busy].name,
            tasks[j % busy].runnables[:STEPS_PER_SEQUENCE],
        )
        for j, name in enumerate(sequence_names)
    )
    return TaskPlan(
        tuple(tasks),
        sequences,
        mapped,
        {name: tuple(levels) for name, levels in partition_levels.items()},
    )
