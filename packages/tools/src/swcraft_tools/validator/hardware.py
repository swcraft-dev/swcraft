"""The hardware topology, resource budget, runnable cost and hardware capability rules.

:func:`check_hardware_topology` reports the ``hardware.*`` shape and reference
rules, ending with ``hardware.unreachable-memory``;
:func:`check_resource_budgets` maps ``resource_analysis``'s findings onto
``hardware.missing-budget``, ``hardware.threshold-breach`` and
``hardware.rate-monotonic-violation``; :func:`check_runnable_costs` reports
``task.runnable-cost-exceeds-task``; :func:`check_hardware_capability`
reports the seven ``safety.*`` placement and trust rules. The module also
holds :data:`RESERVED_IMPLEMENTATION_SUFFIX`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, NamedTuple

from swcraft_tools.diagnostics import Subject, field_step, key_step, member_step
from swcraft_tools.execution_budget import Budget, finest, format_exact, parse_budget
from swcraft_tools.resource_analysis import (
    FINDING_MISSING_BUDGET,
    FINDING_RATE_MONOTONIC_VIOLATION,
    FINDING_THRESHOLD_BREACH,
    ResourceAnalysis,
    analyze_resources,
    has_resources,
)
from swcraft_tools.safety import asil_rank, max_asil
from swcraft_tools.scheduling_identity import (
    EntryTriple,
    Identity,
    SchedulingIdentities,
    identities_from,
    written_selector,
)
from swcraft_tools.schema import (
    Composition,
    Core,
    Hardware,
    Memory,
    Partition,
    Processor,
    Requirement,
    SWComponent,
    Task,
    core_of,
    core_of_partition,
    hardware_by_name,
)
from swcraft_tools.validator.asil import (
    asil_allocation_index,
    components_by_name,
    derived_asil_levels,
    effective_level_of,
    partitions_among,
    rated_partitions,
    resolves,
)
from swcraft_tools.validator.description_elements import duplicates
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result
from swcraft_tools.validator.scheduling import entry_step

# ---------------------------------------------------------------------------
# check_hardware_topology
# ---------------------------------------------------------------------------

#: The layer the ``scan_layer`` probe reads. The ``Subject``s below spell it as a literal: a subject's layer is a
#: protocol address, not the reader's argument.
_HARDWARE_LAYER = "hardware"


def _hardware_names(elements: Iterable[Hardware], model: type[Processor | Core | Memory | Partition]) -> list[str]:
    """The names of *elements* that are instances of *model*, in reader order."""
    return [element.name for element in elements if isinstance(element, model)]


def _declared(names: Sequence[str]) -> str:
    """*names* sorted and de-duplicated, as the message's ``(declared …: …)`` list, or ``none``.

    An empty parenthesis would read as a truncated message. Sorted and de-duplicated because the writer answers the same
    question that way, and two answers differing in order would read as two answers.
    """
    return ", ".join(sorted(set(names))) if names else "none"


def check_hardware_topology(model_root: ModelInput) -> ConsistencyResult:
    """Check the ``hardware/`` layer's own shape and everything that names it.

    The layer is mandatory and carries exactly one ``processor``. A task's ``partition``, a partition's ``core``, the
    keys of an SWC's ``memory`` map and a memory's ``accessible`` entries are identity references into it, and the
    kind each must name is enforced here. Checks performed, each reporting the rule of the same name:

    1. ``hardware.layer-absent`` — no ``hardware/`` directory. Reported once, and the pass then returns.
    2. ``hardware.no-processor`` / ``hardware.multiple-processors`` — exactly one ``kind: processor`` element.
    3. ``hardware.duplicate-name`` — one identity namespace across the four kinds.
    4. ``hardware.undeclared-partition`` / ``hardware.partition-not-a-partition`` — a task's ``partition`` names a
       declared partition; ``hardware.partition-undeclared-core`` / ``hardware.partition-core-not-a-core`` — a
       partition's ``core`` names a declared core.
    5. ``hardware.undeclared-memory`` / ``hardware.memory-not-a-memory`` — each key of an SWC's ``memory`` map names a
       declared memory.
    6. ``hardware.accessible-undeclared-core`` / ``hardware.accessible-not-a-core`` — each entry of a memory's
       ``accessible`` list names a declared core.
    7. ``hardware.unreachable-memory`` — for every task whose partition resolves to a core, every memory an SWC it runs
       budgets a **non-zero** consumption in lists that core in its ``accessible``. A case another rule already reports
       is skipped. One finding per ``(task, SWC, memory)``.

    Reachability is a declaration check, not a placement check. A component no task runs and a zero-byte budget are
    not reachability-checked. Budget arithmetic is :func:`check_resource_budgets`'s; hardware names are not checked
    against the AUTOSAR ``Identifier`` rule.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when all checks pass.
    """
    view = as_view(model_root)

    if not view.layer_present(_HARDWARE_LAYER):
        return consistency_result(
            [
                ConsistencyIssue(
                    rule="hardware.layer-absent",
                    message=(
                        "Project has no hardware/ layer; it is mandatory (a processor, its cores and its memories)."
                    ),
                    # The layer as a whole: there is no element to point at.
                    subjects=(Subject("hardware"),),
                )
            ]
        )

    index = _build_hardware_index(view.hardware)
    records = _hardware_identity_records(index)
    # Read once: checks 4, 5 and 7 all walk these two layers.
    tasks = view.tasks
    swcs = view.swcs
    records.extend(_partition_reference_records(index, tasks))
    records.extend(_memory_key_records(index, swcs))
    records.extend(_accessible_records(index))
    if tasks:
        swc_by_name = {swc.name: swc for swc in swcs}
        records.extend(_unreachable_memory_records(index, tasks, swc_by_name, identities_from(view.compositions)))
    return consistency_result(records)


# ---------------------------------------------------------------------------
# check_hardware_topology's phases
# ---------------------------------------------------------------------------
#
# Each phase holds the literal of every rule it emits: `binding.json` binds each rule to the phase that mints it.


@dataclass(frozen=True)
class _HardwareIndex:
    """The ``hardware`` layer as :func:`check_hardware_topology`'s phases resolve it.

    Attributes:
        elements: every hardware element, in reader order.
        by_name: the elements by name, first file wins; check 3 reports the duplicate.
        core_names: the names of the cores, in reader order.
        memory_names: the names of the memories, in reader order.
        processor_names: the names of the processors, in reader order.
        partition_names: the names of the partitions, in reader order.
    """

    elements: Sequence[Hardware]
    by_name: Mapping[str, Hardware]
    core_names: Sequence[str]
    memory_names: Sequence[str]
    processor_names: Sequence[str]
    partition_names: Sequence[str]


def _build_hardware_index(elements: Sequence[Hardware]) -> _HardwareIndex:
    """Index the ``hardware`` layer's *elements* by name (first wins) and by kind."""
    return _HardwareIndex(
        elements=elements,
        by_name=hardware_by_name(elements),
        core_names=_hardware_names(elements, Core),
        memory_names=_hardware_names(elements, Memory),
        processor_names=_hardware_names(elements, Processor),
        partition_names=_hardware_names(elements, Partition),
    )


def _hardware_identity_records(index: _HardwareIndex) -> list[ConsistencyIssue]:
    """Checks 2 and 3: exactly one processor, and one identity namespace across every kind."""
    records: list[ConsistencyIssue] = []
    processor_names = index.processor_names
    if not processor_names:
        records.append(
            ConsistencyIssue(
                rule="hardware.no-processor",
                message="hardware/ declares no 'kind: processor' element; exactly one is required.",
                subjects=(Subject("hardware"),),
            )
        )
    elif len(processor_names) > 1:
        records.append(
            ConsistencyIssue(
                rule="hardware.multiple-processors",
                message=(
                    f"hardware/ declares {len(processor_names)} 'kind: processor' elements "
                    f"({', '.join(processor_names)}); exactly one is allowed."
                ),
                # Every processor: the repair deletes all but one, and the client chooses which.
                subjects=tuple(Subject("hardware", name) for name in processor_names),
            )
        )

    for name in duplicates([element.name for element in index.elements]):
        records.append(
            ConsistencyIssue(
                rule="hardware.duplicate-name",
                message=f"Duplicate hardware element name '{name}' declared in multiple files.",
                subjects=(Subject("hardware", name),),
            )
        )
    return records


def _partition_reference_records(index: _HardwareIndex, tasks: Sequence[Task]) -> list[ConsistencyIssue]:
    """Check 4: every task runs in a declared partition, and every partition is on a declared core."""
    records: list[ConsistencyIssue] = []
    for task in tasks:
        partition_target = index.by_name.get(task.partition)
        if partition_target is None:
            records.append(
                ConsistencyIssue(
                    rule="hardware.undeclared-partition",
                    message=(
                        f"Task '{task.name}' runs in partition '{task.partition}', which no hardware element "
                        f"declares (declared partitions: {_declared(index.partition_names)})."
                    ),
                    # The referring field, and the layer where declaring the partition is the other repair.
                    subjects=(Subject("tasks", task.name, (field_step("partition"),)), Subject("hardware")),
                    target=task.partition,
                )
            )
        elif not isinstance(partition_target, Partition):
            records.append(
                ConsistencyIssue(
                    rule="hardware.partition-not-a-partition",
                    message=(
                        f"Task '{task.name}' runs in '{task.partition}', which is a {partition_target.kind}, "
                        "not a partition."
                    ),
                    subjects=(Subject("tasks", task.name, (field_step("partition"),)),),
                    target=task.partition,
                )
            )
    for partition in partitions_among(index.elements):
        core_target = index.by_name.get(partition.core)
        if core_target is None:
            records.append(
                ConsistencyIssue(
                    rule="hardware.partition-undeclared-core",
                    message=(
                        f"Partition '{partition.name}' is on core '{partition.core}', which no hardware element "
                        f"declares (declared cores: {_declared(index.core_names)})."
                    ),
                    # `hardware.undeclared-partition`'s two repairs.
                    subjects=(Subject("hardware", partition.name, (field_step("core"),)), Subject("hardware")),
                    target=partition.core,
                )
            )
        elif not isinstance(core_target, Core):
            records.append(
                ConsistencyIssue(
                    rule="hardware.partition-core-not-a-core",
                    message=(
                        f"Partition '{partition.name}' is on '{partition.core}', which is a {core_target.kind}, "
                        "not a core."
                    ),
                    subjects=(Subject("hardware", partition.name, (field_step("core"),)),),
                    target=partition.core,
                )
            )
    return records


def _memory_key_records(index: _HardwareIndex, swcs: Sequence[SWComponent]) -> list[ConsistencyIssue]:
    """Check 5: every SWC memory budget is keyed by a declared memory."""
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        for key in swc.memory:
            memory_target = index.by_name.get(key)
            if memory_target is None:
                records.append(
                    ConsistencyIssue(
                        rule="hardware.undeclared-memory",
                        message=(
                            f"SWC '{swc.name}' budgets memory '{key}', which no hardware element "
                            f"declares (declared memories: {_declared(index.memory_names)})."
                        ),
                        subjects=(
                            Subject("swc", swc.name, (field_step("memory"), key_step(key))),
                            Subject("hardware"),
                        ),
                        target=key,
                    )
                )
            elif not isinstance(memory_target, Memory):
                records.append(
                    ConsistencyIssue(
                        rule="hardware.memory-not-a-memory",
                        message=(f"SWC '{swc.name}' budgets '{key}', which is a {memory_target.kind}, not a memory."),
                        subjects=(Subject("swc", swc.name, (field_step("memory"), key_step(key))),),
                        target=key,
                    )
                )
    return records


def _accessible_records(index: _HardwareIndex) -> list[ConsistencyIssue]:
    """Check 6: every ``accessible`` entry of a memory names a declared core."""
    records: list[ConsistencyIssue] = []
    for memory in index.elements:
        if not isinstance(memory, Memory):
            continue
        for entry in memory.accessible:
            entry_target = index.by_name.get(entry)
            if entry_target is None:
                records.append(
                    ConsistencyIssue(
                        rule="hardware.accessible-undeclared-core",
                        message=(
                            f"Memory '{memory.name}' is accessible from '{entry}', which no hardware "
                            f"element declares (declared cores: {_declared(index.core_names)})."
                        ),
                        # `accessible` is a whole_value list, so the member is its identity.
                        subjects=(Subject("hardware", memory.name, (field_step("accessible"), member_step(entry))),),
                        target=entry,
                    )
                )
            elif not isinstance(entry_target, Core):
                records.append(
                    ConsistencyIssue(
                        rule="hardware.accessible-not-a-core",
                        message=(
                            f"Memory '{memory.name}' is accessible from '{entry}', which is a "
                            f"{entry_target.kind}, not a core."
                        ),
                        subjects=(Subject("hardware", memory.name, (field_step("accessible"), member_step(entry))),),
                        target=entry,
                    )
                )
    return records


def _unreachable_memory_records(
    index: _HardwareIndex,
    tasks: Sequence[Task],
    swc_by_name: Mapping[str, SWComponent],
    identities: SchedulingIdentities,
) -> list[ConsistencyIssue]:
    """Check 7: every memory an SWC charges a non-zero budget to is reachable from its task's core.

    Each ``continue`` on a reference skips a case checks 4–6 or ``check_task_consistency`` already report. The
    ``isinstance(memory, Memory)`` guard keeps ``.accessible`` from being read off an element that is no memory.
    *identities* resolves the component type each entry names, so an ``instance`` entry raises every finding the same
    entry written with ``swc`` raises.
    """
    records: list[ConsistencyIssue] = []
    reported: set[tuple[str, str, str]] = set()
    for task in tasks:
        core = core_of(task, index.by_name)
        if core is None:
            # Check 4 owns an unresolved partition or a partition on no core.
            continue
        for ref in task.runnables:
            swc = identities.component_of(ref.swc, ref.instance, swc_by_name)
            if swc is None:
                # `task.mapping-unknown-swc`'s, or the resolver's (`task.instance-…`)
                continue
            if not any(runnable.name == ref.runnable for runnable in swc.runnables):
                # `task.mapping-unknown-runnable` owns a broken entry; a topology repair cascaded off it would be wrong
                # advice.
                continue
            for memory_name, budget in swc.memory.items():
                memory = index.by_name.get(memory_name)
                if not isinstance(memory, Memory):
                    continue  # hardware.undeclared-memory / memory-not-a-memory
                if budget == 0:
                    # `0` declares no consumption: under `enforcement: strict` a missing entry is already a finding, so
                    # reading `0` as a use would make `hardware.missing-budget` and this rule unsatisfiable together.
                    continue
                if core.name in memory.accessible:
                    continue
                # One finding per (task, SWC, memory): a second SWC is a violation of its own.
                if (task.name, swc.name, memory.name) in reported:
                    continue
                reported.add((task.name, swc.name, memory.name))
                records.append(
                    ConsistencyIssue(
                        rule="hardware.unreachable-memory",
                        message=(
                            f"Task '{task.name}' runs in partition '{task.partition}' on core '{core.name}', "
                            f"but SWC '{swc.name}' budgets memory '{memory.name}', which is not accessible "
                            f"from '{core.name}'."
                        ),
                        # The legal repairs. The last subject is the field: the member the repair adds does not exist
                        # yet.
                        subjects=(
                            Subject("tasks", task.name, (field_step("partition"),)),
                            Subject("hardware", task.partition, (field_step("core"),)),
                            Subject("hardware", memory.name, (field_step("accessible"),)),
                        ),
                    )
                )
    return records


# ---------------------------------------------------------------------------
# check_resource_budgets
# ---------------------------------------------------------------------------

#: ``resource_analysis``'s finding codes, mapped onto this module's rules. A table rather than a string transformation,
#: so a new finding code is a ``KeyError`` rather than an undeclared rule id.
_RESOURCE_FINDING_RULES: dict[str, str] = {
    FINDING_MISSING_BUDGET: "hardware.missing-budget",
    FINDING_THRESHOLD_BREACH: "hardware.threshold-breach",
    FINDING_RATE_MONOTONIC_VIOLATION: "hardware.rate-monotonic-violation",
}


def check_resource_budgets(model_root: ModelInput) -> ConsistencyResult:
    """Check the budgets the ``hardware/`` layer's cores and memories declare.

    A thin mapper over :func:`resource_analysis.analyze_resources`, which owns all budget arithmetic and lists the
    checks. Only ``error`` findings become issues, so a resource declaring ``enforcement: advisory`` never fails
    validation; its ``info`` findings show only in ``resource-report``.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when all checks pass; always consistent when ``hardware/`` declares
        no core and no memory.
    """
    view = as_view(model_root)
    hardware = view.hardware
    analysis = analyze_resources(hardware, view.tasks, view.swcs) if has_resources(hardware) else ResourceAnalysis()
    records = [
        ConsistencyIssue(
            rule=_RESOURCE_FINDING_RULES[finding.code],
            message=finding.message,
            subjects=finding.subjects,
            target=finding.target,
        )
        for finding in analysis.error_findings()
    ]
    return consistency_result(records)


def check_runnable_costs(model_root: ModelInput) -> ConsistencyResult:
    """Check that the declared budgets of a task's runnables fit the task's own.

    ``task.runnable-cost-exceeds-task``: a task whose ``wcet`` is declared and whose mapped runnables' ``wcet`` budgets
    sum to **more** than it; equality is silent. Budgets are compared as exact seconds, so ``1ms`` and ``1000us`` are
    one duration; the message quotes each budget as written and the sum in the finest unit involved. Each entry is
    resolved through :mod:`swcraft_tools.scheduling_identity`: one runnable of one resolved identity counts once per
    task, and two occurrences of one type count twice, because the task runs the runnable once for each.

    **What it does not reach.** Judged only when the task **and** at least one of its runnables declare a budget; a
    runnable without ``wcet`` adds nothing. ``gross`` and ``net`` budgets are summed alike: the message says how many
    are ``net`` and reconciles nothing. An entry the resolver refuses or resolves to no type, and one naming an
    undeclared component or runnable, add nothing (``check_task_consistency``'s findings). Two files of one task name
    are judged as two tasks. The finding is an error whatever the core's ``enforcement``, and the budgets get no
    per-core scaling.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when no task is overrun.
    """
    view = as_view(model_root)
    tasks = view.tasks
    if not any(task.wcet is not None for task in tasks):
        return consistency_result([])
    swc_map = {swc.name: swc for swc in view.swcs}
    identities = identities_from(view.compositions)
    records: list[ConsistencyIssue] = []
    for task in tasks:
        if task.wcet is None:
            continue
        budget = parse_budget(task.wcet)
        element = Subject("tasks", task.name)
        # (identity, runnable) -> its costed runnable, in mapping order.
        costed: dict[tuple[Identity, str], _Costed] = {}
        for ref in task.runnables:
            resolution = identities.resolve(ref.swc, ref.instance)
            if resolution.refusal is not None or resolution.type is None:
                continue
            swc = swc_map.get(resolution.type)
            if swc is None:
                continue
            runnable = next((r for r in swc.runnables if r.name == ref.runnable), None)
            if runnable is None or runnable.wcet is None:
                continue
            costed.setdefault(
                (resolution.identity, ref.runnable),
                _Costed(
                    swc.name,
                    runnable.name,
                    parse_budget(runnable.wcet),
                    runnable.wcet_type,
                    (ref.swc, ref.instance, ref.runnable),
                ),
            )
        costs = [each.budget for each in costed.values()]
        total = sum((cost.seconds for cost in costs), Fraction(0))
        if not costed or total <= budget.seconds:
            continue
        listed = ", ".join(
            f"'{written_selector(each.triple[0], each.triple[1])}.{each.runnable}' ({each.budget.text})"
            for each in costed.values()
        )
        unit = finest([budget.unit, *(cost.unit for cost in costs)])
        net = sum(1 for each in costed.values() if each.wcet_type == "net")
        net_clause = ""
        if net == 1:
            net_clause = " 1 of them is declared net, so its external calls are not counted in the sum."
        elif net > 1:
            net_clause = f" {net} of them are declared net, so their external calls are not counted in the sum."
        # The repairs: the task's budget, one of its entries, or a runnable's budget, each runnable once.
        runnables = dict.fromkeys((each.component, each.runnable) for each in costed.values())
        subjects = [element.at(field_step("wcet"))]
        subjects += [element.at(field_step("runnables"), entry_step(*each.triple)) for each in costed.values()]
        subjects += [
            Subject("swc", component, (field_step("runnables"), member_step(name), field_step("wcet")))
            for component, name in runnables
        ]
        records.append(
            ConsistencyIssue(
                rule="task.runnable-cost-exceeds-task",
                message=(
                    f"Task '{task.name}' declares a budget of {task.wcet}, but the declared budgets of its "
                    f"runnables sum to {format_exact(total, unit)}: {listed}.{net_clause}"
                ),
                subjects=tuple(subjects),
            )
        )
    return consistency_result(records)


# ---------------------------------------------------------------------------
# check_name_uniqueness
# ---------------------------------------------------------------------------

#: The component ``SHORT-NAME`` suffix the ARXML writer generates for every non-external component, and therefore
#: reserves. Restated rather than imported: ``writer`` imports this package. No composition name is reserved: the writer
#: emits the compositions the model declares.
RESERVED_IMPLEMENTATION_SUFFIX = "_Implementation"


# ---------------------------------------------------------------------------
# check_hardware_capability
# ---------------------------------------------------------------------------


def check_hardware_capability(model_root: ModelInput) -> ConsistencyResult:
    """Report the placement findings, from one read of four layers.

    A partition is RATED like a component (``asil``); a core or a memory is declared ABLE to host a level
    (``asil_capability``). An element declaring no level is compared against nothing; a core or a memory declaring
    ``QM`` hosts no ASIL.

    1. ``safety.partition-on-unqualified-core`` — a rated partition on a core whose declared capability is **below** the
       partition's level. One finding per partition, whether or not a task runs in it.
    2. ``safety.memory-below-component`` — a component charging a **non-zero** budget to a memory whose declared
       capability is below the component's effective level. One finding per ``(component, memory)``; a component no task
       runs is reached too.
    3. ``safety.component-above-partition`` — a component the partition's tasks run whose effective level is **above**
       the partition's. One finding per ``(partition, component)``, and nothing closes it.
    4. ``safety.component-below-partition`` — a component the partition runs rated **below** its level, ``QM`` included,
       where the partition declares no ``coexistence`` argument. One finding per ``(partition, component)``.
    5. ``safety.partition-memory-below-level`` — a memory a component the partition runs budgets **non-zero**, declaring
       a capability below the partition's level. One finding per ``(partition, memory)``, naming every contributing
       component.
    6. ``safety.trusted-partition-below-processor-level`` — a ``trusted`` partition declaring a level **below** the
       highest level any partition of the processor declares (the OS protects private data from **non-trusted**
       OS-Applications only: ``[SWS_Os_00207]``, ``[SWS_Os_00355]``, ``[SWS_Os_00356]``). One finding per such
       partition, naming the first partition declaring the highest level. A written ``QM`` is rated.
    7. ``safety.trusted-partition-unrated`` — a ``trusted`` partition declaring **no** level while that highest level is
       **above** ``QM``.

    Rules 6 and 7 judge only ``trusted``, never ``trusted_with_protection`` (``[SWS_Os_00795]``). Rules 3–5 judge the
    partition a task's name resolves to, so an empty partition mints none of them. Names resolve first wins; an
    undeclared core, partition, component, runnable or memory is another ``hardware.*`` or ``task.*`` rule's. All seven
    are declaration checks: no rule says the hardware enforces anything, and a model that passes is not thereby safe.
    """
    view = as_view(model_root)
    return consistency_result(
        hardware_capability_records(
            view.hardware,
            view.tasks,
            view.swcs,
            view.requirements,
            view.compositions,
        )
    )


def hardware_capability_records(
    hardware: Sequence[Hardware],
    tasks: Sequence[Task],
    swcs: Sequence[SWComponent],
    requirements: Sequence[Requirement],
    compositions: Sequence[Composition],
) -> list[ConsistencyIssue]:
    """:func:`check_hardware_capability`'s rules, over five layers already read.

    *compositions* is required: rules 3–5 read each task entry as the type it resolves to.
    """
    allocation_index = asil_allocation_index(swcs, requirements)
    derived = derived_asil_levels(swcs, allocation_index)
    # First wins over every element, whatever its kind, as `check_hardware_topology` resolves: a capability-filtered
    # comprehension would be last-wins.
    by_hw_name = hardware_by_name(hardware)

    records = _partition_core_capability_records(hardware, by_hw_name)
    records.extend(_memory_below_component_records(swcs, by_hw_name, derived, allocation_index))

    # Rules 3-5, per rated partition a task's name resolves to, on a core that resolves. A duplicate partition file runs
    # nothing here: `hardware.duplicate-name` reports it.
    by_swc_name = components_by_name(swcs)
    identities = identities_from(compositions)
    rated = rated_partitions(
        hardware,
        by_hw_name,
        tasks,
        by_swc_name,
        lambda swc: effective_level_of(swc, derived),
        identities,
    )
    records.extend(_component_above_partition_records(rated, by_swc_name, allocation_index))
    records.extend(_component_below_partition_records(rated))
    records.extend(_partition_memory_records(rated, by_swc_name, by_hw_name))
    records.extend(_trusted_partition_records(hardware, by_hw_name, rated))
    return records


# ---------------------------------------------------------------------------
# hardware_capability_records' phases
# ---------------------------------------------------------------------------
#
# Each phase holds the literal of every rule it emits: `binding.json` binds each rule to the phase that mints it.

#: One rated partition :func:`rated_partitions` judges: the partition, its
#: declared level, and the effective level of each component its tasks run.
_RatedPartition = tuple[Partition, str, dict[str, str]]


def _derived_level_repairs(
    swc: SWComponent, capability: str, allocation_index: Mapping[str, Sequence[Requirement]]
) -> tuple[Requirement, ...]:
    """The requirements a DERIVED level is repaired at: those above *capability*.

    Empty for a declared level; lowering one at or below the capability closes nothing. Not named `_allocating`: the
    conformance scan refuses two functions of one name in an emitting module.
    """
    if swc.asil is not None:
        return ()
    index = allocation_index[swc.name]
    return tuple(req for req in index if req.asil is not None and asil_rank(req.asil) > asil_rank(capability))


def _derived_from(repairs: tuple[Requirement, ...]) -> str:
    """``, derived from 'R1' (ASIL B), …`` for a message, or ``""`` when *repairs* is empty."""
    named = ", ".join(f"'{req.id}' (ASIL {req.asil})" for req in repairs)
    return f", derived from {named}" if named else ""


def _partition_core_capability_records(
    hardware: Sequence[Hardware], by_hw_name: Mapping[str, Hardware]
) -> list[ConsistencyIssue]:
    """Rule 1, ``safety.partition-on-unqualified-core``: per partition OBJECT, in reader order."""
    records: list[ConsistencyIssue] = []
    for partition in partitions_among(hardware):
        if partition.asil is None:
            continue  # unrated: compared against nothing
        core = core_of_partition(partition, by_hw_name)
        if core is None or core.asil_capability is None:
            # The topology pass's, or unrated.
            continue
        if asil_rank(partition.asil) <= asil_rank(core.asil_capability):
            continue
        records.append(
            ConsistencyIssue(
                rule="safety.partition-on-unqualified-core",
                message=(
                    f"Partition '{partition.name}' (ASIL {partition.asil}) is on core '{core.name}' "
                    f"(ASIL capability {core.asil_capability}): a partition placed on a core not declared able "
                    "to host its level."
                ),
                # The legal repairs; the rule does not choose.
                subjects=(
                    Subject("hardware", partition.name, (field_step("asil"),)),
                    Subject("hardware", core.name, (field_step("asil_capability"),)),
                    Subject("hardware", partition.name, (field_step("core"),)),
                ),
            )
        )
    return records


def _memory_below_component_records(
    swcs: Sequence[SWComponent],
    by_hw_name: Mapping[str, Hardware],
    derived: Mapping[str, str],
    allocation_index: Mapping[str, Sequence[Requirement]],
) -> list[ConsistencyIssue]:
    """Rule 2, ``safety.memory-below-component``: per component OBJECT, per memory key."""
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        level = effective_level_of(swc, derived)
        for memory_name, budget in swc.memory.items():
            memory = by_hw_name.get(memory_name)
            if not isinstance(memory, Memory) or memory.asil_capability is None:
                continue  # the topology pass's, or unrated
            if budget == 0:
                # `0` declares no use, as for `hardware.unreachable-memory`.
                continue
            if asil_rank(level) <= asil_rank(memory.asil_capability):
                continue
            # One tuple for the message and the subjects, so both name the same requirements.
            repairs = _derived_level_repairs(swc, memory.asil_capability, allocation_index)
            records.append(
                ConsistencyIssue(
                    rule="safety.memory-below-component",
                    message=(
                        f"SWC '{swc.name}' (ASIL {level}) budgets {budget} bytes of memory '{memory.name}' "
                        f"(ASIL capability {memory.asil_capability}){_derived_from(repairs)}: "
                        "a component charging a memory not declared able to host its level."
                    ),
                    subjects=(
                        Subject("swc", swc.name, (field_step("asil"),)),
                        Subject("swc", swc.name, (field_step("memory"), key_step(memory.name))),
                        Subject("hardware", memory.name, (field_step("asil_capability"),)),
                        *(Subject("requirements", req.id, (field_step("asil"),)) for req in repairs),
                    ),
                )
            )
    return records


def _component_above_partition_records(
    rated: Sequence[_RatedPartition],
    by_swc_name: Mapping[str, SWComponent],
    allocation_index: Mapping[str, Sequence[Requirement]],
) -> list[ConsistencyIssue]:
    """Rule 3, ``safety.component-above-partition``: per (partition, component), no exemption."""
    records: list[ConsistencyIssue] = []
    for partition, declared, runs in rated:
        for name in sorted(runs):
            level = runs[name]
            if asil_rank(level) <= asil_rank(declared):
                continue
            repairs = _derived_level_repairs(by_swc_name[name], declared, allocation_index)
            records.append(
                ConsistencyIssue(
                    rule="safety.component-above-partition",
                    message=(
                        f"SWC '{name}' (ASIL {level}) is run by partition '{partition.name}' "
                        f"(ASIL {declared}){_derived_from(repairs)}: a component rated above the partition "
                        "that runs it."
                    ),
                    # No argument closes it.
                    subjects=(
                        Subject("hardware", partition.name, (field_step("asil"),)),
                        Subject("swc", name, (field_step("asil"),)),
                        *(Subject("requirements", req.id, (field_step("asil"),)) for req in repairs),
                    ),
                )
            )
    return records


def _component_below_partition_records(rated: Sequence[_RatedPartition]) -> list[ConsistencyIssue]:
    """Rule 4, ``safety.component-below-partition``: closed by the partition's ``coexistence``."""
    records: list[ConsistencyIssue] = []
    for partition, declared, runs in rated:
        if partition.coexistence:
            continue
        for name in sorted(runs):
            level = runs[name]
            if asil_rank(level) >= asil_rank(declared):
                continue
            records.append(
                ConsistencyIssue(
                    rule="safety.component-below-partition",
                    message=(
                        f"SWC '{name}' (ASIL {level}) is run by partition '{partition.name}' "
                        f"(ASIL {declared}), which declares no coexistence argument: a component rated "
                        "below the partition that runs it, sharing its memory protection."
                    ),
                    subjects=(
                        Subject("hardware", partition.name, (field_step("coexistence"),)),
                        Subject("hardware", partition.name, (field_step("asil"),)),
                        Subject("swc", name, (field_step("asil"),)),
                    ),
                )
            )
    return records


def _partition_memory_records(
    rated: Sequence[_RatedPartition],
    by_swc_name: Mapping[str, SWComponent],
    by_hw_name: Mapping[str, Hardware],
) -> list[ConsistencyIssue]:
    """Rule 5, ``safety.partition-memory-below-level``: per (partition, memory), deduplicated over its components."""
    records: list[ConsistencyIssue] = []
    for partition, declared, runs in rated:
        charged: dict[str, tuple[Memory, list[str]]] = {}
        for name in sorted(runs):
            for memory_name, budget in by_swc_name[name].memory.items():
                if budget == 0:
                    continue  # rule 2's decision: `0` declares no use
                memory = by_hw_name.get(memory_name)
                if not isinstance(memory, Memory) or memory.asil_capability is None:
                    continue  # the topology pass's, or unrated
                if asil_rank(memory.asil_capability) >= asil_rank(declared):
                    continue
                charged.setdefault(memory.name, (memory, []))[1].append(name)
        for memory_name in sorted(charged):
            memory, components = charged[memory_name]
            named = ", ".join(f"'{name}'" for name in components)
            records.append(
                ConsistencyIssue(
                    rule="safety.partition-memory-below-level",
                    message=(
                        f"Partition '{partition.name}' (ASIL {declared}) runs {named}, budgeting memory "
                        f"'{memory.name}' (ASIL capability {memory.asil_capability}): a partition whose "
                        "components charge a memory not declared able to host its level."
                    ),
                    subjects=(
                        Subject("hardware", memory.name, (field_step("asil_capability"),)),
                        Subject("hardware", partition.name, (field_step("asil"),)),
                        *(Subject("swc", name, (field_step("memory"), key_step(memory.name))) for name in components),
                    ),
                )
            )
    return records


def _trusted_partition_records(
    hardware: Sequence[Hardware], by_hw_name: Mapping[str, Hardware], rated: Sequence[_RatedPartition]
) -> list[ConsistencyIssue]:
    """Rules 6 and 7, the trust rules: per ``trusted`` partition FILE against the highest rated level.

    A file is judged when it is the first-wins resolution of its name, on a core that resolves (``resolves``, as
    ``rated_partitions``). ``top`` is ``max_asil``'s, so its ``(Y)`` follows the tie rule, never reader order; ``peak``
    is the first partition carrying it.
    """
    if not rated:
        return []
    records: list[ConsistencyIssue] = []
    top = max_asil(level for _, level, _ in rated)
    peak = next(partition for partition, level, _ in rated if level == top)
    for partition in partitions_among(hardware):
        if partition.trust != "trusted":
            continue  # `non_trusted` and `trusted_with_protection` are confined for memory
        if not resolves(partition, by_hw_name):
            continue  # the duplicate and the unresolved core are the topology pass's
        if partition.asil is not None:
            # Rule 6: the highest, or one tied with it, fires nothing.
            if asil_rank(partition.asil) >= asil_rank(top):
                continue
            records.append(
                ConsistencyIssue(
                    rule="safety.trusted-partition-below-processor-level",
                    message=(
                        f"Partition '{partition.name}' (trusted, ASIL {partition.asil}) shares the processor "
                        f"with partition '{peak.name}' (ASIL {top}): a trusted partition, unrestricted for "
                        "memory, rated below a partition whose memory it may write."
                    ),
                    # The legal repairs; the rule does not choose.
                    subjects=(
                        Subject("hardware", partition.name, (field_step("trust"),)),
                        Subject("hardware", partition.name, (field_step("asil"),)),
                        Subject("hardware", peak.name, (field_step("asil"),)),
                    ),
                )
            )
        elif asil_rank(top) > asil_rank("QM"):
            # Rule 7: beside `QM` alone nothing claims an ASIL.
            records.append(
                ConsistencyIssue(
                    rule="safety.trusted-partition-unrated",
                    message=(
                        f"Partition '{partition.name}' (trusted, unrated) shares the processor with partition "
                        f"'{peak.name}' (ASIL {top}): a trusted partition of unknown assurance, unrestricted "
                        "for memory, beside a partition rated above QM."
                    ),
                    subjects=(
                        Subject("hardware", partition.name, (field_step("trust"),)),
                        Subject("hardware", partition.name, (field_step("asil"),)),
                    ),
                )
            )
    return records


class _Costed(NamedTuple):
    """One runnable :func:`check_runnable_costs` sums: its component, its name, its budget, its type and entry."""

    component: str
    runnable: str
    budget: Budget
    wcet_type: Literal["gross", "net"] | None
    triple: EntryTriple
