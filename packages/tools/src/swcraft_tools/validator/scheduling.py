"""The task and sequence rules, and the event identity.

Holds :func:`check_task_consistency` (the ``task.*`` rules),
:func:`check_sequence_consistency` (the ``sequence.*`` rules) and
:func:`event_identity`, the whole-value identity of an event that a finding's
subject carries. Both checks read each entry's resolved scheduling identity from
:mod:`swcraft_tools.scheduling_identity`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from swcraft_tools.diagnostics import Subject, SubjectStep, field_step, member_step
from swcraft_tools.scheduling_identity import (
    EntryTriple,
    Identity,
    Resolution,
    SchedulingIdentities,
    identities_from,
    identity_sort_key,
    written_selector,
)
from swcraft_tools.schema import (
    CallReturnedEvent,
    DataEvent,
    DisabledMode,
    InitEvent,
    OperationEvent,
    Runnable,
    SWComponent,
    Task,
    TaskRunnableRef,
    TimingEvent,
    hardware_by_name,
    partition_of,
)
from swcraft_tools.validator.model_view import ModelInput, ModelView, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result

# ---------------------------------------------------------------------------
# check_task_consistency
# ---------------------------------------------------------------------------


# Relative tolerance for the "runnable period is an integer multiple of the
# task period" float comparison.
_TASK_PERIOD_RELATIVE_TOLERANCE = 1e-9


def entry_step(swc: str | None, instance: str | None, runnable: str) -> SubjectStep:
    """A task entry's or a step's own member step: its ``(swc, instance, runnable)`` triple.

    The composite identity the registry publishes for both collections, the absent
    selector spelled ``None``.
    """
    return member_step({"swc": swc, "instance": instance, "runnable": runnable})


def _refusal_message(subject: str, swc: str | None, instance: str | None, resolution: Resolution) -> str:
    """The message of one of the six resolver refusals (``task.instance-…`` / ``sequence.step-instance-…``)."""
    if resolution.refusal == "instance-ambiguous":
        shown = ", ".join(f"'{path}'" for path in resolution.paths)
        more = "" if resolution.count <= len(resolution.paths) else f" (the first {len(resolution.paths)} shown)"
        return (
            f"{subject} names SWC '{swc}' by `swc`, but that type is placed {resolution.count} times below the root "
            f"composition{more}: {shown}. Name the occurrence by `instance`."
        )
    if resolution.refusal == "instance-not-a-component":
        return (
            f"{subject} names instance '{instance}', which ends on a composition member that instantiates a "
            "composition, not a component."
        )
    return (
        f"{subject} names instance '{instance}', which does not walk from the root composition to a member: a "
        "segment names no member, or descends into a component or an undeclared composition."
    )


def _refused_field(resolution: Resolution) -> str:
    """The selector field a resolver refusal is reported at: ``swc`` for an ambiguous type, else ``instance``."""
    return "swc" if resolution.refusal == "instance-ambiguous" else "instance"


def _written_differently(
    groups: Mapping[tuple[Identity, str], list[EntryTriple]],
) -> list[tuple[str, list[EntryTriple], str]]:
    """The resolved runnables reached by two or more entries written differently.

    One ``(runnable name, distinct triples, written text)`` per such group, the
    triples in :func:`identity_sort_key` order.
    """
    found: list[tuple[str, list[EntryTriple], str]] = []
    for (_identity, runnable_name), triples in groups.items():
        distinct = sorted(set(triples), key=identity_sort_key)
        if len(distinct) < 2:
            continue
        written = ", ".join(f"'{written_selector(swc, instance)}.{runnable}'" for swc, instance, runnable in distinct)
        found.append((runnable_name, distinct, written))
    return found


def check_task_consistency(model_root: ModelInput) -> ConsistencyResult:
    """Check task model consistency.

    Each entry names its component by exactly one of ``swc`` and ``instance``; every check reads the entry's
    **resolved scheduling identity** (:mod:`swcraft_tools.scheduling_identity`) or the component type it resolves
    to. When the composition walk is undefined (no root, several, a reachable cycle) the identity is the entry's
    syntactic triple, the resolver refusals and check 7 are silent, and an ``instance`` entry has no type.

    Checks performed:
    1. A task name declared in several files (``task.duplicate-name``).
    2. Every ``swc`` selector names an existing SWC (``task.mapping-unknown-swc``; an unknown path is
       ``task.instance-unknown``).
    3. Every entry resolves (``task.instance-ambiguous`` / ``-unknown`` / ``-not-a-component``); a refused entry is
       judged no further.
    4. Every runnable exists on the resolved type (``task.mapping-unknown-runnable``).
    5. One runnable of one resolved identity is mapped by at most one task (``task.runnable-mapped-to-multiple-tasks``),
       and at most once by one task (``task.resolved-duplicate-runnable``; the same entry written twice is refused at
       load).
    6. A mapped runnable's timing event period is a positive integer multiple of the task's period
       (``task.runnable-period-not-multiple``, relative tolerance ``1e-9``); init events carry no period constraint.
    7. ``task.instance-in-multiple-partitions``: a resolved instance run by tasks of two or more declared partitions
       (:func:`_split_identities`).

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when all checks pass. A model without a ``tasks/`` directory is
        always consistent: ``read_tasks`` returns an empty list in that case.
    """
    view = as_view(model_root)
    tasks = view.tasks
    swcs = view.swcs
    swc_map = {swc.name: swc for swc in swcs}

    records = _duplicate_task_records(tasks)
    if not tasks:
        return consistency_result(records)
    identities = identities_from(view.compositions)
    entry_records, mapped = _task_entry_records(tasks, identities, swc_map)
    records += entry_records
    records += _multiple_task_records(mapped.owners)
    records += _partition_split_records(mapped.resolved_runs, identities, view)
    return consistency_result(records)


@dataclass(frozen=True)
class _TaskMappings:
    """What :func:`_task_entry_records` hands the two later phases of :func:`check_task_consistency`.

    Attributes:
        owners: ``(identity, runnable)`` -> every ``(task name, entry triple)`` that maps it, per task
            OBJECT, so two files of one task name are two owners (check 5).
        resolved_runs: identity -> ``(task, component type, entry triple)`` for every entry checks 2-4
            resolved, in reader order (check 7). A refused entry, or one naming an undeclared component
            or runnable, is those checks' finding and not counted.
    """

    owners: Mapping[tuple[Identity, str], Sequence[tuple[str, EntryTriple]]]
    resolved_runs: Mapping[Identity, Sequence[tuple[Task, str, EntryTriple]]]


def _duplicate_task_records(tasks: Sequence[Task]) -> list[ConsistencyIssue]:
    """Check 1 of :func:`check_task_consistency`: one task name declared in several files."""
    records: list[ConsistencyIssue] = []
    _seen: dict[str, int] = {}
    for task in tasks:
        _seen[task.name] = _seen.get(task.name, 0) + 1
    for task_name, count in _seen.items():
        if count > 1:
            records.append(
                ConsistencyIssue(
                    rule="task.duplicate-name",
                    message=f"Duplicate task name '{task_name}' declared in multiple files.",
                    subjects=(Subject("tasks", task_name),),
                )
            )
    return records


def _task_entry_records(
    tasks: Sequence[Task], identities: SchedulingIdentities, swc_map: Mapping[str, SWComponent]
) -> tuple[list[ConsistencyIssue], _TaskMappings]:
    """Checks 2-4 and 6 per entry, and check 5 within one task, of :func:`check_task_consistency`."""
    records: list[ConsistencyIssue] = []
    mapping_owners: dict[tuple[Identity, str], list[tuple[str, EntryTriple]]] = {}
    resolved_runs: dict[Identity, list[tuple[Task, str, EntryTriple]]] = {}
    for task in tasks:
        element = Subject("tasks", task.name)
        # (identity, runnable) -> this task's entry triples that reach it (check 5).
        within: dict[tuple[Identity, str], list[EntryTriple]] = {}
        for ref in task.runnables:
            triple = (ref.swc, ref.instance, ref.runnable)
            resolution = identities.resolve(ref.swc, ref.instance)
            # A refused entry is judged once, by check 3, and claims nothing for check 5.
            if resolution.refusal is None:
                key = (resolution.identity, ref.runnable)
                mapping_owners.setdefault(key, []).append((task.name, triple))
                within.setdefault(key, []).append(triple)
            found, component = _task_entry_findings(task, ref, resolution, swc_map)
            records += found
            if component is not None:
                resolved_runs.setdefault(resolution.identity, []).append((task, component, triple))

        # Check 5, within one task: one resolved runnable reached by two entries
        # written differently (the same entry written twice is refused at load).
        for runnable_name, distinct, written in _written_differently(within):
            records.append(
                ConsistencyIssue(
                    rule="task.resolved-duplicate-runnable",
                    message=(
                        f"Task '{task.name}' maps runnable '{runnable_name}' of one component instance twice, "
                        f"through entries written differently: {written}."
                    ),
                    subjects=tuple(element.at(field_step("runnables"), entry_step(*entry)) for entry in distinct),
                )
            )
    return records, _TaskMappings(mapping_owners, resolved_runs)


def _task_entry_findings(
    task: Task, ref: TaskRunnableRef, resolution: Resolution, swc_map: Mapping[str, SWComponent]
) -> tuple[list[ConsistencyIssue], str | None]:
    """Checks 2-4 and 6 of :func:`check_task_consistency` on one entry of *task*.

    Returns the entry's findings and the component type it resolved to, ``None``
    when checks 2-4 refused it or its walk is undefined.
    """
    element = Subject("tasks", task.name)
    mapping = entry_step(ref.swc, ref.instance, ref.runnable)
    label = written_selector(ref.swc, ref.instance)

    # Check 2
    if ref.swc is not None and ref.swc not in swc_map:
        return [
            ConsistencyIssue(
                rule="task.mapping-unknown-swc",
                message=f"Task '{task.name}' references unknown SWC '{ref.swc}'.",
                subjects=(element.at(field_step("runnables"), mapping, field_step("swc")),),
                target=ref.swc,
            )
        ], None

    # Check 3
    if resolution.refusal is not None:
        return [
            ConsistencyIssue(
                rule=(
                    "task.instance-ambiguous"
                    if resolution.refusal == "instance-ambiguous"
                    else "task.instance-not-a-component"
                    if resolution.refusal == "instance-not-a-component"
                    else "task.instance-unknown"
                ),
                message=_refusal_message(
                    f"Task '{task.name}' entry '{label}.{ref.runnable}'", ref.swc, ref.instance, resolution
                ),
                subjects=(element.at(field_step("runnables"), mapping, field_step(_refused_field(resolution))),),
            )
        ], None

    # An `instance` entry under an undefined walk has no type, and one
    # resolving to an undeclared type is the composition rules' finding.
    swc = swc_map.get(resolution.type) if resolution.type is not None else None
    if swc is None:
        return [], None

    # Check 4
    runnable = next((r for r in swc.runnables if r.name == ref.runnable), None)
    if runnable is None:
        return [
            ConsistencyIssue(
                rule="task.mapping-unknown-runnable",
                message=(f"Task '{task.name}' references unknown runnable '{ref.runnable}' on SWC '{swc.name}'."),
                subjects=(element.at(field_step("runnables"), mapping, field_step("runnable")),),
                target=f"{swc.name}.{ref.runnable}",
            )
        ], None
    return _period_records(task, ref, label, swc, runnable), swc.name


def _period_records(
    task: Task, ref: TaskRunnableRef, label: str, swc: SWComponent, runnable: Runnable
) -> list[ConsistencyIssue]:
    """Check 6 of :func:`check_task_consistency`: each timing event of a mapped runnable against the task period."""
    records: list[ConsistencyIssue] = []
    element = Subject("tasks", task.name)
    for event in runnable.events:
        if not isinstance(event, TimingEvent):
            continue
        ratio = event.period / task.period
        nearest = round(ratio)
        if nearest < 1 or abs(ratio - nearest) > _TASK_PERIOD_RELATIVE_TOLERANCE:
            records.append(
                ConsistencyIssue(
                    rule="task.runnable-period-not-multiple",
                    message=(
                        f"Task '{task.name}' (period {task.period}s) runnable "
                        f"'{label}.{ref.runnable}' has timing period "
                        f"{event.period}s, which is not a positive integer "
                        f"multiple of the task period."
                    ),
                    # Both ends of the arithmetic: the task's period and the event's.
                    subjects=(
                        element.at(field_step("period")),
                        Subject(
                            "swc",
                            swc.name,
                            (
                                field_step("runnables"),
                                member_step(ref.runnable),
                                field_step("events"),
                                member_step(event_identity(event)),
                            ),
                        ),
                    ),
                )
            )
    return records


def _multiple_task_records(
    mapping_owners: Mapping[tuple[Identity, str], Sequence[tuple[str, EntryTriple]]],
) -> list[ConsistencyIssue]:
    """Check 5 of :func:`check_task_consistency` across tasks: one resolved runnable mapped by two tasks."""
    records: list[ConsistencyIssue] = []
    # At-most-one-task rule (hand-edited YAML can bypass the writer guard).
    for (_identity, runnable_name), owners in mapping_owners.items():
        unique_owners = sorted({owner for owner, _triple in owners})
        if len(unique_owners) > 1:
            claims = sorted(set(owners), key=lambda claim: (claim[0], identity_sort_key(claim[1])))
            first_swc, first_instance, _runnable = claims[0][1]
            records.append(
                ConsistencyIssue(
                    rule="task.runnable-mapped-to-multiple-tasks",
                    message=(
                        f"Runnable '{written_selector(first_swc, first_instance)}.{runnable_name}' is mapped to "
                        f"multiple tasks: {', '.join(unique_owners)}."
                    ),
                    # Every claiming mapping: the repair drops all but one, so each must be addressable.
                    subjects=tuple(
                        Subject("tasks", owner, (field_step("runnables"), entry_step(*triple)))
                        for owner, triple in claims
                    ),
                )
            )
    return records


def _partition_split_records(
    resolved_runs: Mapping[Identity, Sequence[tuple[Task, str, EntryTriple]]],
    identities: SchedulingIdentities,
    view: ModelView,
) -> list[ConsistencyIssue]:
    """Check 7 of :func:`check_task_consistency`: one resolved instance run by tasks of two partitions."""
    records: list[ConsistencyIssue] = []
    placements = identities.placements
    if placements is not None:
        for split in _split_identities(resolved_runs, view):
            tasks_text = ", ".join(f"'{name}'" for name in split.tasks)
            partitions_text = ", ".join(f"'{name}'" for name in split.partitions)
            if split.identity[0] == "unplaced-type":
                where = f"SWC '{split.component}' is placed nowhere below the root composition, and its runnables are"
            elif placements[split.component].count == 1:
                where = (
                    f"SWC '{split.component}' is placed once below the root composition, at '{split.identity[1]}', "
                    "and its runnables are"
                )
            else:
                where = (
                    f"SWC '{split.component}' at instance '{split.identity[1]}' below the root composition has its "
                    "runnables"
                )
            records.append(
                ConsistencyIssue(
                    rule="task.instance-in-multiple-partitions",
                    message=(
                        f"{where} run by tasks {tasks_text} of partitions {partitions_text}: one instance would run "
                        "in more than one partition."
                    ),
                    # The repairs: re-point a task to another partition, or move a
                    # mapping to a task of the partition the others run in.
                    subjects=tuple(Subject("tasks", task_name, steps) for task_name, steps in split.addresses),
                )
            )
    return records


class _SplitIdentity(NamedTuple):
    """One resolved instance :func:`_split_identities` finds run in two partitions."""

    #: ``('instance', path)`` or ``('unplaced-type', type)``.
    identity: Identity
    #: The component type the identity resolves to.
    component: str
    #: The distinct declared partitions running it, in name order.
    partitions: tuple[str, ...]
    #: ``(task name, steps)`` per subject: each counted task's ``partition`` and
    #: each of its ``runnables`` entries reaching the identity, the tasks in name order.
    addresses: tuple[tuple[str, tuple[SubjectStep, ...]], ...]

    @property
    def tasks(self) -> tuple[str, ...]:
        """The counted tasks running it, in name order -- read off :attr:`addresses`."""
        return tuple(dict.fromkeys(task_name for task_name, _steps in self.addresses))


def _split_identities(
    resolved_runs: Mapping[Identity, Sequence[tuple[Task, str, EntryTriple]]],
    model_root: ModelInput,
) -> list[_SplitIdentity]:
    """The resolved instances ``task.instance-in-multiple-partitions`` reports, in identity order.

    An identity is reported when the tasks running its runnables name **two or
    more distinct declared partitions**. Two occurrences of one type
    (``left/worker``, ``right/worker``) are judged apart; a type placed nowhere
    below the root is one identity, ``('unplaced-type', A)``. A task whose
    ``partition`` resolves to no ``Partition`` is not counted: the topology pass
    reports it.

    **What it does not reach:** a model without exactly one root, or with a cycle
    the root reaches, is not judged; a refused entry (``task.instance-…``) is not
    counted.
    """
    view = as_view(model_root)
    if not resolved_runs:
        return []
    by_name = hardware_by_name(view.hardware)
    found: list[_SplitIdentity] = []
    for identity in sorted(resolved_runs, key=identity_sort_key):
        runs = resolved_runs[identity]
        by_partition: dict[str, list[Task]] = {}
        # Two runnables of one task are one owner; two files of one task name are two.
        seen: set[int] = set()
        for task, _component, _triple in runs:
            partition = partition_of(task, by_name)
            if partition is not None and id(task) not in seen:
                seen.add(id(task))
                by_partition.setdefault(partition.name, []).append(task)
        if len(by_partition) < 2:
            continue
        counted = {id(task) for owners in by_partition.values() for task in owners}
        # Two files of one task name address one element, so each triple is kept once per name.
        entries: dict[str, dict[EntryTriple, None]] = {}
        for task, _component, triple in runs:
            if id(task) in counted:
                entries.setdefault(task.name, {})[triple] = None
        addresses: list[tuple[str, tuple[SubjectStep, ...]]] = []
        for task_name in sorted(entries):
            addresses.append((task_name, (field_step("partition"),)))
            addresses += [(task_name, (field_step("runnables"), entry_step(*triple))) for triple in entries[task_name]]
        found.append(_SplitIdentity(identity, runs[0][1], tuple(sorted(by_partition)), tuple(addresses)))
    return found


# ---------------------------------------------------------------------------
# check_sequence_consistency
# ---------------------------------------------------------------------------


def check_sequence_consistency(model_root: ModelInput) -> ConsistencyResult:
    """Check execution-sequence model consistency.

    A step is matched to its task's entries by the **resolved scheduling identity** and the runnable, so
    ``swc: A`` in the task and ``instance: A`` in the step are one entry when ``A`` is placed once. Under an
    undefined composition walk the identity is the syntactic triple.

    Checks performed:
    1. A sequence name declared in several files (``sequence.duplicate-name``).
    2. Every sequence's ``task`` reference names an existing ``Task`` (``sequence.unknown-task``).
    3. Every step resolves (``sequence.step-instance-ambiguous`` / ``-unknown`` / ``-not-a-component``); a refused
       step is judged no further.
    4. One resolved runnable appears at most once per sequence (``sequence.resolved-duplicate-step``, two steps
       written differently).
    5. Every step's resolved identity and runnable is present in the referenced task's ``runnables`` list
       (``sequence.step-not-mapped``).
    6. Every step resolves to a real SWC (``sequence.step-unknown-swc``) and a real runnable on the resolved type
       (``sequence.step-unknown-runnable``).
    7. Step order is a valid subsequence of the referenced task's runnable order (``sequence.step-order``).

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when all checks pass. A model without a ``sequences/`` directory
        is always consistent: ``read_sequences`` returns an empty list in that case.
    """
    view = as_view(model_root)
    sequences = view.sequences
    tasks = view.tasks
    task_map = {task.name: task for task in tasks}
    swcs = view.swcs
    swc_map = {swc.name: swc for swc in swcs}

    records: list[ConsistencyIssue] = []

    # Check 1
    _seen: dict[str, int] = {}
    for seq in sequences:
        _seen[seq.name] = _seen.get(seq.name, 0) + 1
    for seq_name, count in _seen.items():
        if count > 1:
            records.append(
                ConsistencyIssue(
                    rule="sequence.duplicate-name",
                    message=f"Duplicate sequence name '{seq_name}' declared in multiple files.",
                    subjects=(Subject("sequences", seq_name),),
                )
            )
    if not sequences:
        return consistency_result(records)
    identities = identities_from(view.compositions)

    for seq in sequences:
        element = Subject("sequences", seq.name)
        # Check 2
        task = task_map.get(seq.task)
        if task is None:
            records.append(
                ConsistencyIssue(
                    rule="sequence.unknown-task",
                    message=f"Sequence '{seq.name}' references unknown task '{seq.task}'.",
                    subjects=(element.at(field_step("task")),),
                    target=seq.task,
                )
            )
            continue

        # First entry wins. A refused task entry keeps its syntactic identity,
        # so only a step written the same way meets it.
        task_positions: dict[tuple[Identity, str], int] = {}
        for idx, ref in enumerate(task.runnables):
            task_positions.setdefault((identities.resolve(ref.swc, ref.instance).identity, ref.runnable), idx)

        resolved_steps = [(step, identities.resolve(step.swc, step.instance)) for step in seq.steps]

        # Check 4
        shown: dict[tuple[Identity, str], list[EntryTriple]] = {}
        for step, resolution in resolved_steps:
            if resolution.refusal is None:
                shown.setdefault((resolution.identity, step.runnable), []).append(
                    (step.swc, step.instance, step.runnable)
                )
        for runnable_name, distinct, written in _written_differently(shown):
            records.append(
                ConsistencyIssue(
                    rule="sequence.resolved-duplicate-step",
                    message=(
                        f"Sequence '{seq.name}' shows runnable '{runnable_name}' of one component instance twice, "
                        f"through steps written differently: {written}."
                    ),
                    subjects=tuple(element.at(field_step("steps"), entry_step(*entry)) for entry in distinct),
                )
            )

        last_position = -1
        for step, resolution in resolved_steps:
            label = written_selector(step.swc, step.instance)
            member = entry_step(step.swc, step.instance, step.runnable)

            # Check 3. An `swc` selector naming an undeclared SWC is check 6's finding.
            if resolution.refusal is not None and (step.swc is None or step.swc in swc_map):
                records.append(
                    ConsistencyIssue(
                        rule=(
                            "sequence.step-instance-ambiguous"
                            if resolution.refusal == "instance-ambiguous"
                            else "sequence.step-instance-not-a-component"
                            if resolution.refusal == "instance-not-a-component"
                            else "sequence.step-instance-unknown"
                        ),
                        message=_refusal_message(
                            f"Sequence '{seq.name}' step '{label}.{step.runnable}'", step.swc, step.instance, resolution
                        ),
                        subjects=(element.at(field_step("steps"), member, field_step(_refused_field(resolution))),),
                    )
                )
                continue

            # Check 5
            key = (resolution.identity, step.runnable)
            if key not in task_positions:
                records.append(
                    ConsistencyIssue(
                        rule="sequence.step-not-mapped",
                        message=(
                            f"Sequence '{seq.name}' step '{label}.{step.runnable}' is not mapped to task '{seq.task}'."
                        ),
                        subjects=(element.at(field_step("steps"), member),),
                        target=f"{seq.task}: {label}.{step.runnable}",
                    )
                )
                continue

            # Check 6
            if step.swc is not None and step.swc not in swc_map:
                records.append(
                    ConsistencyIssue(
                        rule="sequence.step-unknown-swc",
                        message=f"Sequence '{seq.name}' step references unknown SWC '{step.swc}'.",
                        subjects=(element.at(field_step("steps"), member, field_step("swc")),),
                        target=step.swc,
                    )
                )
                continue
            swc = swc_map.get(resolution.type) if resolution.type is not None else None
            if swc is not None:
                runnable = next((r for r in swc.runnables if r.name == step.runnable), None)
                if runnable is None:
                    records.append(
                        ConsistencyIssue(
                            rule="sequence.step-unknown-runnable",
                            message=(
                                f"Sequence '{seq.name}' step references unknown runnable '{step.runnable}' on SWC "
                                f"'{swc.name}'."
                            ),
                            subjects=(element.at(field_step("steps"), member, field_step("runnable")),),
                            target=f"{swc.name}.{step.runnable}",
                        )
                    )
                    continue

            # Check 7
            position = task_positions[key]
            if position < last_position:
                records.append(
                    ConsistencyIssue(
                        rule="sequence.step-order",
                        message=(
                            f"Sequence '{seq.name}' step order violates task "
                            f"'{seq.task}' runnable order: '{label}.{step.runnable}' "
                            "appears out of order."
                        ),
                        # The whole collection: the protocol has no positional move.
                        subjects=(element.at(field_step("steps")),),
                    )
                )
                continue
            last_position = position

    return consistency_result(records)


# ---------------------------------------------------------------------------
# event_identity
# ---------------------------------------------------------------------------


def _disabled_mode_identity(modes: Sequence[DisabledMode]) -> list[dict[str, str]]:
    """An event's ``disabled_modes`` as the identity carries them: each mode's three references, in declared order."""
    return [{"port": mode.port, "mode_group_ref": mode.mode_group_ref, "mode_ref": mode.mode_ref} for mode in modes]


def event_identity(
    event: TimingEvent | InitEvent | OperationEvent | DataEvent | CallReturnedEvent,
) -> dict[str, object]:
    """Any event as the whole-value identity the registry publishes for it.

    The kind's own fields and a stated ``name``; a field at its contract default
    is no part of the identity.

    Raises:
        TypeError: for an event kind not taught here.
    """
    identity: dict[str, object] = {"type": event.type}
    if isinstance(event, TimingEvent):
        identity["period"] = event.period
        if event.disabled_modes:
            identity["disabled_modes"] = _disabled_mode_identity(event.disabled_modes)
    elif isinstance(event, OperationEvent):
        identity["port"] = event.port
        identity["operation"] = event.operation
    elif isinstance(event, DataEvent):
        identity["port"] = event.port
        identity["element"] = event.element
        if event.disabled_modes:
            identity["disabled_modes"] = _disabled_mode_identity(event.disabled_modes)
    elif isinstance(event, CallReturnedEvent):
        identity["port"] = event.port
        identity["operation"] = event.operation
        if event.result is not None:
            identity["result"] = event.result
    elif not isinstance(event, InitEvent):
        raise TypeError(f"an event kind the event identity has not been taught: {type(event).__name__}")
    if event.name is not None:
        identity["name"] = event.name
    return identity
