"""Where a runnable reads and writes its port data implicitly, per task.

An implicit access (``by: implicit`` on a runnable's ``reads`` or ``writes`` entry) is one the RTE
serves from a copy, filled before the runnable starts and published after it ends.
:func:`implicit_accesses` answers, per task, how many implicit reads and writes the runnables it
maps **declare**. It marks and does not cost: when the RTE fills and publishes the copies is its
configuration (R22-11's ``[SWS_Rte_03956]``, ``[SWS_Rte_07020]``), copies may be shared, and the
model holds no data size. The answer is reported only: no ``check-model`` rule reads it, nothing is
written to ARXML and no YAML field holds it.

**Counted per occurrence.** Each task entry resolves through
:meth:`~swcraft_tools.scheduling_identity.SchedulingIdentities.resolve` to one runnable of one
root-reachable occurrence; one mapped twice by a task counts once, one mapped to two tasks counts in
each, and an entry resolving to no single occurrence maps nothing. The lines: the tasks with an
implicit access, shortest period first, then reader order; each runnable occurrence no task maps,
``in no task`` (or :data:`PLACED_NOWHERE`, named by the type), in instance-path and runnable order;
then each type placed more than :data:`~swcraft_tools.scheduling_identity.PATH_CAP` times, counting
the unwalked occurrences (:data:`PAST_THE_CAP`), in type order. Where the composition walk is
undefined, a ``swc`` entry maps its type's runnable, an ``instance`` entry nothing, and an
occurrence is named by its type.

What it does not reach: a task whose partition or core does not resolve reads
:data:`UNKNOWN_CORE`; an ``external`` component is passed over; the access form is counted as
declared (whether it is allowed is ``check-model``'s); ``connectors/`` is never read. An entry
naming an occurrence past the cap counts in its task, not again as ``in no task``.

A leaf: it imports only :mod:`swcraft_tools.schema`, :mod:`swcraft_tools.reader` and
:mod:`swcraft_tools.scheduling_identity`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from swcraft_tools.reader import ModelRootNotFoundError, read_hardware, read_swc_model, read_tasks
from swcraft_tools.scheduling_identity import PATH_CAP, SchedulingIdentities, identities_for
from swcraft_tools.schema import Hardware, Runnable, SWComponent, Task, core_of, hardware_by_name

#: The three kinds of line an :class:`ImplicitLine` is.
TASK = "task"
OCCURRENCE = "occurrence"
PAST_THE_CAP_LINE = "past-the-cap"

#: Why an occurrence line has no task: the two cases, each the text its line ends with.
IN_NO_TASK = "in no task"
PLACED_NOWHERE = "placed in no composition"

#: A type placed more than ``PATH_CAP`` times: the line's ``more`` counts the occurrences not walked.
PAST_THE_CAP = f"the occurrences past the first {PATH_CAP} are not walked"

#: What a task line names in place of a core its partition does not resolve to.
UNKNOWN_CORE = "core unknown"

#: The form a ``reads`` or ``writes`` entry states for an implicit access.
IMPLICIT = "implicit"


@dataclass(frozen=True)
class ImplicitLine:
    """One line of the section: a task's tally, an occurrence no task maps, or the occurrences past the cap.

    For :data:`TASK`: ``task``, ``core`` (``None`` when it does not resolve),
    ``reads``, ``writes`` and ``runnables``, the count of runnable occurrences
    that declare them. For :data:`OCCURRENCE`: ``occurrence`` (an instance path,
    or the type's name when no path names it), ``runnable``, ``reads``,
    ``writes`` and ``case`` (:data:`IN_NO_TASK` or :data:`PLACED_NOWHERE`). For
    :data:`PAST_THE_CAP_LINE`: ``occurrence`` (the type's name), ``case``
    (:data:`PAST_THE_CAP`) and ``more``, the occurrences not walked.
    """

    shape: str
    reads: int = 0
    writes: int = 0
    task: str | None = None
    core: str | None = None
    runnables: int = 0
    occurrence: str | None = None
    runnable: str | None = None
    case: str | None = None
    more: int = 0


def _tally(runnable: Runnable) -> tuple[int, int]:
    """The implicit reads and the implicit writes *runnable* declares."""
    reads = sum(1 for read in runnable.reads if read.by == IMPLICIT)
    writes = sum(1 for write in runnable.writes if write.by == IMPLICIT)
    return reads, writes


def _accessing(components: Sequence[SWComponent]) -> dict[str, dict[str, tuple[int, int]]]:
    """Type -> runnable -> its implicit (reads, writes), for every runnable declaring one; first file wins."""
    answer: dict[str, dict[str, tuple[int, int]]] = {}
    seen: set[str] = set()
    for component in components:
        if component.name in seen:
            continue
        seen.add(component.name)
        if component.external:
            continue
        tallies = {runnable.name: _tally(runnable) for runnable in component.runnables}
        declared = {name: tally for name, tally in tallies.items() if tally != (0, 0)}
        if declared:
            answer[component.name] = declared
    return answer


def _occurrence(identities: SchedulingIdentities, swc: str | None, instance: str | None) -> tuple[str, str] | None:
    """The ``(occurrence, type)`` an entry maps, or ``None`` when it maps no single occurrence of a known type."""
    resolution = identities.resolve(swc, instance)
    if resolution.type is None or resolution.refusal is not None:
        return None
    kind, *rest = resolution.identity
    if kind == "instance" and isinstance(rest[0], str):
        return rest[0], resolution.type
    if not identities.defined:
        return resolution.type, resolution.type  # the syntactic identity: an occurrence is named by its type
    return None  # a type placed nowhere


def _task_lines(
    identities: SchedulingIdentities,
    tasks: Sequence[Task],
    accessing: Mapping[str, Mapping[str, tuple[int, int]]],
    hardware: Mapping[str, Hardware],
) -> tuple[list[ImplicitLine], set[tuple[str, str]]]:
    """One line per task mapping an implicit access, and every ``(occurrence, runnable)`` some task maps."""
    lines: list[tuple[float, int, ImplicitLine]] = []
    mapped: set[tuple[str, str]] = set()
    for order, task in enumerate(tasks):
        counted: dict[tuple[str, str], tuple[int, int]] = {}
        for entry in task.runnables:
            found = _occurrence(identities, entry.swc, entry.instance)
            if found is None:
                continue
            occurrence, component = found
            mapped.add((occurrence, entry.runnable))
            tally = accessing.get(component, {}).get(entry.runnable)
            if tally is not None:
                counted[(occurrence, entry.runnable)] = tally
        if not counted:
            continue
        core = core_of(task, hardware)
        line = ImplicitLine(
            TASK,
            reads=sum(reads for reads, _ in counted.values()),
            writes=sum(writes for _, writes in counted.values()),
            task=task.name,
            core=core.name if core is not None else None,
            runnables=len(counted),
        )
        lines.append((task.period, order, line))
    return [line for _, _, line in sorted(lines, key=lambda each: (each[0], each[1]))], mapped


def _occurrence_lines(
    identities: SchedulingIdentities,
    accessing: Mapping[str, Mapping[str, tuple[int, int]]],
    mapped: set[tuple[str, str]],
) -> list[ImplicitLine]:
    """The runnable occurrences with an implicit access no task maps, then the occurrences past the cap."""
    unmapped: list[ImplicitLine] = []
    capped: list[ImplicitLine] = []
    for component, runnables in accessing.items():
        if not identities.defined:
            paths: tuple[str, ...] = (component,)
            case = IN_NO_TASK
        else:
            assert identities.placements is not None  # a defined walk has its placements
            placement = identities.placements.get(component)
            if placement is None:
                paths, case = (component,), PLACED_NOWHERE
            else:
                paths, case = placement.paths, IN_NO_TASK
                if placement.count > len(placement.paths):
                    more = placement.count - len(placement.paths)
                    capped.append(ImplicitLine(PAST_THE_CAP_LINE, occurrence=component, case=PAST_THE_CAP, more=more))
        for path in paths:
            for runnable, (reads, writes) in runnables.items():
                if case == IN_NO_TASK and (path, runnable) in mapped:
                    continue
                unmapped.append(ImplicitLine(OCCURRENCE, reads, writes, occurrence=path, runnable=runnable, case=case))
    unmapped.sort(key=lambda line: (line.occurrence or "", line.runnable or ""))
    capped.sort(key=lambda line: line.occurrence or "")
    return unmapped + capped


def implicit_accesses(model_root: str) -> tuple[ImplicitLine, ...]:
    """Every task's implicit reads and writes, then the occurrences no task maps.

    The component layer is read first; with no implicit access -- or no model
    root at all -- the answer is ``()`` and no other layer is read. Otherwise
    the tasks, the hardware and the compositions are read once each.

    Raises:
        ValueError: if a file of a layer it reads is malformed (the reader's).
    """
    try:
        components = read_swc_model(model_root)
    except ModelRootNotFoundError:
        return ()
    accessing = _accessing(components)
    if not accessing:
        return ()
    tasks = read_tasks(model_root)
    hardware = hardware_by_name(read_hardware(model_root))
    identities = identities_for(model_root)
    task_lines, mapped = _task_lines(identities, tasks, accessing, hardware)
    return tuple(task_lines + _occurrence_lines(identities, accessing, mapped))
