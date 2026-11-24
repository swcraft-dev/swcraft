"""The ASIL allocation and safety coexistence rules.

:func:`check_component_asil` reports ``safety.component-below-requirements``;
:func:`check_safety_coexistence` reports ``safety.provider-below-element``,
``safety.consumer-above-element``, ``safety.measure-unknown-element``,
``safety.measure-on-provide-port``, ``safety.task-mixed-asil`` and
``safety.measure-blank``. Each check reads the model and hands the layers to a
``*_records`` function that touches no disk. The module also holds the
name-keyed :func:`effective_asil_levels`, the editing guard's
:class:`SafetyReading`, and the partition readings the hardware capability
rules share.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field

from swcraft_tools.diagnostics import Subject, field_step, key_step, member_step
from swcraft_tools.safety import NO_ASIL, asil_rank, max_asil
from swcraft_tools.scheduling_identity import SchedulingIdentities, identities_from
from swcraft_tools.schema import (
    Composition,
    DataElement,
    ExternalInterface,
    Hardware,
    Interface,
    Partition,
    Port,
    Requirement,
    SRInterface,
    SWComponent,
    Task,
    core_of_partition,
    hardware_by_name,
)
from swcraft_tools.trace import allocated_requirement_ids
from swcraft_tools.validator.compositions import stub_member_miss, stub_member_names
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result


def partitions_among(elements: Iterable[Hardware]) -> list[Partition]:
    """The partitions among *elements*, in reader order."""
    return [element for element in elements if isinstance(element, Partition)]


# ---------------------------------------------------------------------------
# check_component_asil
# ---------------------------------------------------------------------------


def asil_allocation_index(
    swcs: Sequence[SWComponent],
    requirements: Sequence[Requirement],
) -> dict[str, list[Requirement]]:
    """The requirement-to-component allocation: component name to the **levelled** requirements it satisfies.

    ISO 26262-9:2018 § 5.2 puts the level on the requirement and reaches an
    architectural element by allocation. The derived level and a finding's
    ``above`` set both read this one index, so they cannot disagree.

    What a component satisfies is :func:`swcraft_tools.trace.allocated_requirement_ids`:
    its own ``satisfies`` and each port's and runnable's. Requirements are listed in
    reader order, each once per component however many sites name it.

    **What a ``satisfies`` member does and does not reach:** an unlevelled
    requirement, a member naming no declared requirement
    (``trace.link-unknown-requirement``'s finding) and a link on a composition,
    interface, task or hardware element contribute nothing; a stale member counts
    as current. Keyed by name: two component files of one name share one entry
    (``name.duplicate-swc``), and two requirement files of one id both contribute.
    """
    levelled = [requirement for requirement in requirements if requirement.asil is not None]
    return {
        name: [requirement for requirement in levelled if requirement.id in ids]
        for name, ids in allocated_requirement_ids(swcs).items()
    }


def effective_asil_levels(
    swcs: Sequence[SWComponent],
    requirements: Sequence[Requirement],
) -> dict[str, str]:
    """Every component's effective ASIL: its own declaration, else the maximum allocated to it.

    ISO 26262-9:2018 § 5.2 puts the level on the **requirement**, and a component
    inherits the highest allocated to it, so an absent declaration is never an
    exemption. A component nothing allocates a level to answers ``"QM"``.

    Keyed by name, so this is a view and not a rule input: no rule reads it (the
    coexistence and placement rules read :func:`effective_level_of` per object, and
    :func:`check_component_asil` the component's own ``asil``). **What "first wins" does
    and does not buy:** where two files of one name declare different levels, the
    **first** of the sequence given wins (reader order for production callers),
    the same file :func:`check_safety_coexistence` picks; the answer still depends
    on the sequence a direct caller composes. Such a model is
    ``name.duplicate-swc``'s finding.
    """
    allocation_index = asil_allocation_index(swcs, requirements)
    derived = derived_asil_levels(swcs, allocation_index)
    # First wins: the reading `check_safety_coexistence` makes of the same duplicate.
    return {name: effective_level_of(swc, derived) for name, swc in components_by_name(swcs).items()}


def derived_asil_levels(
    swcs: Sequence[SWComponent],
    allocation_index: Mapping[str, Sequence[Requirement]],
) -> dict[str, str]:
    """The maximum each component name has ALLOCATED to it, ``"QM"`` for none.

    Name-keyed: two component files of one name share one index entry, so the
    derived maximum is the same for both, while the effective level is read per
    object.
    """
    return {
        swc.name: max_asil(
            requirement.asil for requirement in allocation_index[swc.name] if requirement.asil is not None
        )
        for swc in swcs
    }


def check_component_asil(model_root: ModelInput) -> ConsistencyResult:
    """Report every component declaring an ASIL below the requirements allocated to it.

    ``[constr_6201]``'s direction: a component at or above the maximum of its
    requirements is legitimate; only one below them is the violation. One finding
    per component, naming its declared level and every requirement above it.

    A component declaring nothing derives the maximum and is never reported; a
    declared ``asil: QM`` is a declaration and is reported under a levelled
    requirement, ``[constr_6201]``'s own case. An external stub is judged like any
    other component: its ``asil`` is a value the project wrote. The comparison
    reads the component's own ``asil``, so two files of one name give the same
    finding in any reader order.

    **What it does not see:** a link on a composition, interface, task or hardware
    element allocates no level. It compares two of the project's declarations and
    says nothing about whether the allocation is right; a model that passes it is
    not thereby safe.

    The rule is :func:`component_asil_records`, which touches no disk, so the
    editing guard can ask it of the models it holds in memory.
    """
    view = as_view(model_root)
    return consistency_result(component_asil_records(view.swcs, view.requirements))


def component_asil_records(
    swcs: Sequence[SWComponent],
    requirements: Sequence[Requirement],
) -> list[ConsistencyIssue]:
    """:func:`check_component_asil`'s rule, over two layers already read; it touches no disk."""
    allocation_index = asil_allocation_index(swcs, requirements)

    records: list[ConsistencyIssue] = []
    for swc in swcs:
        # A component declaring nothing derives this very maximum: nothing is above it.
        if swc.asil is None:
            continue
        above = tuple(
            requirement
            for requirement in allocation_index[swc.name]
            if requirement.asil is not None and asil_rank(requirement.asil) > asil_rank(swc.asil)
        )
        if not above:
            continue
        maximum = max_asil(requirement.asil for requirement in above if requirement.asil is not None)
        named = ", ".join(f"'{requirement.id}' (ASIL {requirement.asil})" for requirement in above)
        records.append(
            ConsistencyIssue(
                rule="safety.component-below-requirements",
                message=(
                    f"SWC '{swc.name}' declares ASIL {swc.asil}, below the ASIL {maximum} of the "
                    f"requirement(s) allocated to it: {named}."
                ),
                # The two legal repairs: raise the component, or lower the requirements above it.
                subjects=(
                    Subject("swc", swc.name, (field_step("asil"),)),
                    *(Subject("requirements", req.id, (field_step("asil"),)) for req in above),
                ),
            )
        )

    return records


# ---------------------------------------------------------------------------
# check_safety_coexistence
# ---------------------------------------------------------------------------


def check_safety_coexistence(model_root: ModelInput) -> ConsistencyResult:
    """Report the six coexistence findings, from one read of four layers.

    1. ``safety.provider-below-element``: a ``provide`` port's component rated
       below an element it produces. No exemption.
    2. ``safety.consumer-above-element``: a ``require`` port's component rated
       above an element it consumes; closed by a ``measures`` key naming it.
    3. ``safety.measure-unknown-element``: a ``measures`` key on a ``require``
       port naming no data element of its interface (on a client-server port,
       every key).
    4. ``safety.measure-on-provide-port``: a ``measures`` map on a ``provide`` port.
    5. ``safety.task-mixed-asil``: a task scheduling components of different
       levels; closed by a ``coexistence`` text.
    6. ``safety.measure-blank``: a ``measures`` text on a ``require`` port that is
       empty or whitespace only, one finding per ``(port, key)``; a blank text
       under an unknown key gives both 3 and 6.

    Rules 3, 4 and 6 skip an external component stub; rules 1-3 skip a port whose
    interface is undeclared; a task entry resolving to no declared component
    contributes nothing to rule 5. They are findings, not load failures, so the
    read-only commands still read the project. An answered finding is not reported.
    A ``""`` measure still closes rule 2 and is rule 6's finding; on an external
    interface stub's elements rules 1 and 2 abstain.

    A model that passes is not thereby safe: no rule follows a connector, no
    separation (partition, OS-Application, memory protection) is modelled, a text
    is checked against nothing but blankness, an operation carries no level, and an
    unrated element is read by neither port rule. The rules are
    :func:`safety_coexistence_records`, which touches no disk.
    """
    view = as_view(model_root)
    return consistency_result(
        safety_coexistence_records(
            view.swcs,
            view.interfaces,
            view.tasks,
            view.requirements,
            view.compositions,
        )
    )


def effective_level_of(swc: SWComponent, derived: Mapping[str, str]) -> str:
    """This component OBJECT's effective level: its own declaration, else the derived maximum.

    Not a lookup in the name-keyed :func:`effective_asil_levels`: two files of one
    name share every name-keyed entry, so a rule would report a level the file in
    front of it never wrote.
    """
    return swc.asil if swc.asil is not None else derived[swc.name]


def components_by_name(swcs: Sequence[SWComponent]) -> dict[str, SWComponent]:
    """The component OBJECT each name resolves to, FIRST file wins.

    Every name-keyed reading here must resolve the same object (the duplicate is
    ``name.duplicate-swc``'s finding); a comprehension would be last-wins.
    """
    by_name: dict[str, SWComponent] = {}
    for candidate in swcs:
        by_name.setdefault(candidate.name, candidate)
    return by_name


def _sr_elements_by_interface(interfaces: Sequence[Interface]) -> dict[str, dict[str, DataElement]]:
    """Per sender-receiver interface, its data elements by name.

    A client-server interface is absent from this map ON PURPOSE:
    ``safety.measure-unknown-element`` reads the absence to mean "declares no
    data elements at all", which is exactly true of one.
    """
    return {
        iface.name: {element.name: element for element in iface.data_elements}
        for iface in interfaces
        if isinstance(iface, SRInterface)
    }


def _elements_below_level(
    interface: str,
    level: str,
    elements_of: Mapping[str, Mapping[str, DataElement]],
    cache: dict[tuple[str, str], frozenset[str]] | None = None,
) -> frozenset[str]:
    """The names *interface* declares that are rated strictly BELOW *level*.

    ``safety.consumer-above-element``'s comparison, answered as a set so that rule
    2 and :meth:`SafetyReading.answering` share it. An unrated element is below
    nothing. *cache* is a caller-owned map keyed ``(interface, level)``; ``None``
    recomputes.
    """
    key = (interface, level)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached
    rank = asil_rank(level)
    names = frozenset(
        name
        for name, element in elements_of.get(interface, {}).items()
        if element.asil is not None and asil_rank(element.asil) < rank
    )
    if cache is not None:
        cache[key] = names
    return names


def _consumed_below_level(
    port: Port,
    level: str,
    elements_of: Mapping[str, Mapping[str, DataElement]],
) -> Iterator[DataElement]:
    """``safety.consumer-above-element``'s condition, over one port, in reader order.

    Every data element a ``require`` *port* consumes that is rated below *level*.
    """
    if port.direction != "require" or port.interface is None:
        return  # a port naming no interface consumes no element
    declared = elements_of.get(port.interface, {})
    below = _elements_below_level(port.interface, level, elements_of)
    for name in declared:
        if name in below:
            yield declared[name]


def _task_component_levels(
    task: Task,
    by_swc_name: Mapping[str, SWComponent],
    level_of: Callable[[SWComponent], str],
    identities: SchedulingIdentities,
) -> dict[str, str]:
    """``safety.task-mixed-asil``'s reading of a task's runnables.

    The effective level of each component the task's ``runnables`` name, keyed by
    component name, so two runnables of one component count once. An entry is
    read as the component TYPE it resolves to; one resolving to none contributes
    nothing (``task.mapping-unknown-swc``'s or a ``task.instance-…`` refusal's
    finding, or an ``instance`` under an undefined walk).
    """
    components = (identities.component_of(ref.swc, ref.instance, by_swc_name) for ref in task.runnables)
    return {swc.name: level_of(swc) for swc in components if swc is not None}


def partition_runs(
    partition_name: str,
    tasks: Sequence[Task],
    by_swc_name: Mapping[str, SWComponent],
    level_of: Callable[[SWComponent], str],
    identities: SchedulingIdentities,
) -> dict[str, str]:
    """A partition's reading of what it runs.

    The effective level of each component run by a task OBJECT whose
    ``partition`` names *partition_name*, keyed by component name. Unlike
    :func:`_task_component_levels`, an entry naming a runnable its component does
    not declare (``task.mapping-unknown-runnable``) contributes nothing, so one
    mistyped runnable is not reported twice. An entry resolving to no declared
    component contributes nothing either. The caller resolves the partition.

    **What it does not reach:** it reads component TYPES, so two instances of one
    type cannot be told apart.
    """
    runs: dict[str, str] = {}
    for task in tasks:
        if task.partition != partition_name:
            continue
        for ref in task.runnables:
            swc = identities.component_of(ref.swc, ref.instance, by_swc_name)
            if swc is not None and any(runnable.name == ref.runnable for runnable in swc.runnables):
                runs[swc.name] = level_of(swc)
    return runs


def resolves(partition: Partition, by_hw_name: Mapping[str, Hardware]) -> bool:
    """Whether *partition* is judged at all.

    It must be the first-wins resolution of its own name over every hardware
    element, and its ``core`` must resolve to a :class:`Core` (an unresolved core
    is the topology pass's finding).
    """
    return by_hw_name.get(partition.name) is partition and core_of_partition(partition, by_hw_name) is not None


def rated_partitions(
    hardware: Sequence[Hardware],
    by_hw_name: Mapping[str, Hardware],
    tasks: Sequence[Task],
    by_swc_name: Mapping[str, SWComponent],
    level_of: Callable[[SWComponent], str],
    identities: SchedulingIdentities,
) -> list[tuple[Partition, str, dict[str, str]]]:
    """The partitions the three per-type rules judge, each with its level and what it runs.

    A partition is judged when it is rated and :func:`resolves`. Shared by the
    rules and :func:`safety_reading`, so the editing guard cannot disagree with
    them. In reader order; *runs* is :func:`partition_runs`.
    """
    rated: list[tuple[Partition, str, dict[str, str]]] = []
    for partition in partitions_among(hardware):
        if partition.asil is None:
            continue  # unrated: compared against nothing
        if not resolves(partition, by_hw_name):
            continue
        rated.append(
            (partition, partition.asil, partition_runs(partition.name, tasks, by_swc_name, level_of, identities))
        )
    return rated


@dataclass(frozen=True)
class SafetyReading:
    """What the answerable safety rules read off ONE model, asked per OWNER.

    The editing guard asks whether *this* owner lost protection; a model-wide
    count can be cancelled by an unrelated element carrying the same key, so
    every member is keyed by the owner a question is asked about:

    - :attr:`effective` and :attr:`allocated` — the component: its effective level
      and the maximum allocated to it. Both, because a component declaring its own
      level keeps its effective one when the allocation under it drops;
    - :attr:`elements` — per interface, its data elements by name;
    - :meth:`readable` and :meth:`answering` — the ``(component, port)`` pair that
      holds a ``measures`` map, computed on demand for one port;
    - :attr:`coexistence` and :attr:`mixed_tasks` — the task (rule 5);
    - :attr:`partition_levels`, :attr:`partition_coexistence` and
      :attr:`mixed_partitions` — the partition, judged as :func:`rated_partitions` judges it;
    - :attr:`partition_trust` — the partition's declared class, which is no
      rule's condition.

    Nothing here builds a record: materialising findings is costly on a large model.

    **What this does not reach.** Beyond the answerable rules (``safety.consumer-above-element``,
    ``safety.task-mixed-asil``, ``safety.component-below-partition``), the maximum
    ``safety.component-below-requirements`` compares against, and the partitions' declared levels and
    trust classes, it reads nothing: ``safety.provider-below-element`` is read as a level rather than a
    finding, and ``safety.measure-unknown-element`` and ``safety.measure-on-provide-port`` are answered by
    no declared argument. It does not read the placement rules' cores or memories. Which task runs in which
    partition it reads for :attr:`mixed_partitions` only, never as a placement change to classify: the guard
    does not ask about one (``classify_safety_act``). Keyed by name, first wins: two component files of one
    name share one owner (``name.duplicate-swc``'s finding).
    """

    #: Component name to its EFFECTIVE level — :func:`effective_asil_levels`.
    effective: Mapping[str, str]
    #: Component name to the maximum its requirements ALLOCATE, ``"QM"`` for none.
    allocated: Mapping[str, str]
    #: Per sender-receiver interface, its data elements by name; a client-server interface is absent.
    elements: Mapping[str, Mapping[str, DataElement]]
    #: ``(component, port)`` to the port's own non-empty ``measures`` map, by
    #: reference: a copy would be linear in the model's whole measure volume.
    measures: Mapping[tuple[str, str], Mapping[str, str]]
    #: Task name to its non-empty ``coexistence`` text.
    coexistence: Mapping[str, str]
    #: The tasks whose runnables span more than one effective level (rule 5's
    #: condition). A set, not a map to the levels: the model does not record
    #: which pair of levels an argument was written about.
    mixed_tasks: AbstractSet[str]
    #: Partition name to its DECLARED level, rated partitions only, first wins
    #: over every hardware element; an unrated partition is absent.
    partition_levels: Mapping[str, str]
    #: Partition name to its non-empty ``coexistence`` text, first wins, rated or not.
    partition_coexistence: Mapping[str, str]
    #: The rated partitions that run a component rated BELOW their declared
    #: level, before the argument is consulted; a set for :attr:`mixed_tasks`' reason.
    mixed_partitions: AbstractSet[str]
    #: Partition name to its declared ``trust`` class, first wins, only where it
    #: is not the default ``non_trusted``.
    partition_trust: Mapping[str, str]
    #: ``(component, port)`` to the port OBJECT, first wins: the input to the two methods.
    _ports: Mapping[tuple[str, str], Port] = field(default_factory=dict, repr=False, compare=False)
    #: :func:`_elements_below_level`'s cache, filled as owners are asked about.
    _below: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict, repr=False, compare=False)

    def readable(self, owner: tuple[str, str]) -> AbstractSet[str]:
        """The keys of this port's ``measures`` map that rule 2 can LOOK AT.

        On a ``require`` port, the keys its interface declares as data elements; a
        key naming none, a map on a ``provide`` port and a key an external
        interface stub lists are read by no level rule. The withdrawal side's
        reading: removing the port, the map or the direction takes the argument away.
        """
        port = self._ports.get(owner)
        if port is None or port.direction != "require" or port.interface is None:
            return frozenset()
        # Two key views intersected, so neither map is copied.
        return self.elements.get(port.interface, {}).keys() & port.measures.keys()

    def answering(self, owner: tuple[str, str]) -> AbstractSet[str]:
        """The keys of this port's ``measures`` map that answer a finding TODAY.

        :meth:`readable`, narrowed to elements rated strictly below the consuming
        component's effective level. The switch side's reading: a standing map
        goes live when its element is rated, the port turns ``require`` or the
        interface is re-pointed, none of which declares anything.
        """
        port = self._ports.get(owner)
        if port is None or port.direction != "require" or port.interface is None:
            return frozenset()
        level = self.effective.get(owner[0], NO_ASIL)
        return _elements_below_level(port.interface, level, self.elements, self._below) & port.measures.keys()


def safety_reading(
    swcs: Sequence[SWComponent],
    interfaces: Sequence[Interface],
    tasks: Sequence[Task],
    requirements: Sequence[Requirement],
    hardware: Sequence[Hardware],
    compositions: Sequence[Composition],
) -> SafetyReading:
    """:class:`SafetyReading` for one model held in memory.

    Every condition is read through the rules' own functions, so the guard and
    the findings cannot disagree. *hardware* and *compositions* are required, not
    defaulted: a forgotten layer would read every partition as absent, or misread
    every ``instance`` task entry.
    """
    identities = identities_from(compositions)
    allocation_index = asil_allocation_index(swcs, requirements)
    derived = derived_asil_levels(swcs, allocation_index)
    by_swc_name = components_by_name(swcs)
    elements_of = _sr_elements_by_interface(interfaces)

    effective = effective_asil_levels(swcs, requirements)
    ports: dict[tuple[str, str], Port] = {}
    measures: dict[tuple[str, str], Mapping[str, str]] = {}
    for swc in by_swc_name.values():
        for port in swc.ports:
            owner = (swc.name, port.name)
            if owner in ports:
                # First wins, as rule 2 resolves it (`swc.duplicate-port-name`'s finding).
                continue
            ports[owner] = port
            if port.measures:
                measures[owner] = port.measures

    coexistence: dict[str, str] = {}
    mixed: set[str] = set()
    seen_tasks: set[str] = set()
    for task in tasks:
        # First wins: two task files of one name are one owner (`name.duplicate-task`).
        if task.name in seen_tasks:
            continue
        seen_tasks.add(task.name)
        mapped = _task_component_levels(task, by_swc_name, lambda swc: effective_level_of(swc, derived), identities)
        # Distinct by `X` (`asil_rank`): `B` beside `B(D)` is one level, as rule 5 counts it.
        if len({asil_rank(level) for level in mapped.values()}) >= 2:
            mixed.add(task.name)
        if task.coexistence:
            coexistence[task.name] = task.coexistence

    by_hw_name = hardware_by_name(hardware)
    partition_levels: dict[str, str] = {}
    partition_coexistence: dict[str, str] = {}
    partition_trust: dict[str, str] = {}
    for name, element in by_hw_name.items():
        if not isinstance(element, Partition):
            continue
        if element.asil is not None:
            partition_levels[name] = element.asil
        if element.coexistence:
            partition_coexistence[name] = element.coexistence
        if element.trust != "non_trusted":  # spelled: a model_fields read blinds the field oracle
            partition_trust[name] = element.trust
    mixed_partitions = frozenset(
        partition.name
        for partition, declared, runs in rated_partitions(
            hardware, by_hw_name, tasks, by_swc_name, lambda swc: effective_level_of(swc, derived), identities
        )
        if any(asil_rank(level) < asil_rank(declared) for level in runs.values())
    )

    return SafetyReading(
        effective=effective,
        allocated=derived,
        elements=elements_of,
        measures=measures,
        coexistence=coexistence,
        mixed_tasks=frozenset(mixed),
        partition_levels=partition_levels,
        partition_coexistence=partition_coexistence,
        mixed_partitions=mixed_partitions,
        partition_trust=partition_trust,
        _ports=ports,
    )


def safety_coexistence_records(
    swcs: Sequence[SWComponent],
    interfaces: Sequence[Interface],
    tasks: Sequence[Task],
    requirements: Sequence[Requirement],
    compositions: Sequence[Composition],
) -> list[ConsistencyIssue]:
    """:func:`check_safety_coexistence`'s six rules, over five layers already read; it touches no disk.

    *compositions* is required: rule 5 reads each task entry as the type it resolves to.
    """
    index = _build_coexistence_index(swcs, interfaces, requirements, compositions)
    records = _provider_below_element_records(swcs, index)
    records += _consumer_above_element_records(swcs, index)
    records += _measure_unknown_element_records(swcs, index)
    records += _measure_on_provide_port_records(swcs)
    records += _task_mixed_asil_records(tasks, index)
    records += _measure_blank_records(swcs)
    return records


@dataclass(frozen=True)
class _CoexistenceIndex:
    """What :func:`safety_coexistence_records` builds before its first finding, read by its phases.

    Attributes:
        identities: the compositions' scheduling identities.
        allocation_index: each component's allocated requirements.
        derived: each component's derived level, the maximum allocated to it.
        by_swc_name: the components by name, first wins.
        declared_interfaces: names only: rule 3 asks only whether the port's interface is declared.
        external_interfaces: the external interface stubs by name, first wins; rule 3 resolves a key
            through one against the names it lists, and the two level rules abstain.
        elements_of: per sender-receiver interface, its elements by name, the map ``SafetyReading`` reads.
    """

    identities: SchedulingIdentities
    allocation_index: Mapping[str, Sequence[Requirement]]
    derived: Mapping[str, str]
    by_swc_name: Mapping[str, SWComponent]
    declared_interfaces: frozenset[str]
    external_interfaces: Mapping[str, ExternalInterface]
    elements_of: Mapping[str, Mapping[str, DataElement]]


def _build_coexistence_index(
    swcs: Sequence[SWComponent],
    interfaces: Sequence[Interface],
    requirements: Sequence[Requirement],
    compositions: Sequence[Composition],
) -> _CoexistenceIndex:
    """The :class:`_CoexistenceIndex` over the layers :func:`safety_coexistence_records` was handed."""
    identities = identities_from(compositions)
    allocation_index = asil_allocation_index(swcs, requirements)
    derived = derived_asil_levels(swcs, allocation_index)
    external_interfaces: dict[str, ExternalInterface] = {}
    for iface in interfaces:
        if isinstance(iface, ExternalInterface):
            external_interfaces.setdefault(iface.name, iface)
    return _CoexistenceIndex(
        identities=identities,
        allocation_index=allocation_index,
        derived=derived,
        by_swc_name=components_by_name(swcs),
        declared_interfaces=frozenset(iface.name for iface in interfaces),
        external_interfaces=external_interfaces,
        elements_of=_sr_elements_by_interface(interfaces),
    )


def _allocating(swc: SWComponent, allocation_index: Mapping[str, Sequence[Requirement]]) -> tuple[Requirement, ...]:
    """The levelled requirements this component's level is DERIVED from.

    Empty when the component declares its own level: then no requirement is a
    legal repair for a finding about it.
    """
    return () if swc.asil is not None else tuple(allocation_index[swc.name])


def _provider_below_element_records(swcs: Sequence[SWComponent], index: _CoexistenceIndex) -> list[ConsistencyIssue]:
    """Rule 1 of :func:`safety_coexistence_records`: a component rated below an element it provides."""
    records: list[ConsistencyIssue] = []

    for swc in swcs:
        level = effective_level_of(swc, index.derived)
        for port in swc.ports:
            if port.direction != "provide" or port.interface is None:
                continue  # a port naming no interface provides no element
            for element in index.elements_of.get(port.interface, {}).values():
                if element.asil is None or asil_rank(element.asil) <= asil_rank(level):
                    continue
                records.append(
                    ConsistencyIssue(
                        rule="safety.provider-below-element",
                        message=(
                            f"SWC '{swc.name}' (ASIL {level}) provides '{element.name}' "
                            f"(ASIL {element.asil}) through port '{port.name}' of "
                            f"'{port.interface}': a component rated below an element it produces."
                        ),
                        # The three legal repairs: raise the component, lower the element,
                        # or change the requirements the component's level derives from.
                        subjects=(
                            Subject("swc", swc.name, (field_step("asil"),)),
                            Subject(
                                "swc",
                                swc.name,
                                (field_step("ports"), member_step(port.name), field_step("interface")),
                            ),
                            Subject(
                                "interfaces",
                                port.interface,
                                (
                                    field_step("data_elements"),
                                    member_step(element.name),
                                    field_step("asil"),
                                ),
                            ),
                            *(
                                Subject("requirements", req.id, (field_step("asil"),))
                                for req in _allocating(swc, index.allocation_index)
                            ),
                        ),
                    )
                )
    return records


def _consumer_above_element_records(swcs: Sequence[SWComponent], index: _CoexistenceIndex) -> list[ConsistencyIssue]:
    """Rule 2 of :func:`safety_coexistence_records`: a component rated above an element it consumes, with no measure."""
    records: list[ConsistencyIssue] = []

    for swc in swcs:
        level = effective_level_of(swc, index.derived)
        for port in swc.ports:
            for element in _consumed_below_level(port, level, index.elements_of):
                if element.name in port.measures:
                    continue
                records.append(
                    ConsistencyIssue(
                        rule="safety.consumer-above-element",
                        message=(
                            f"SWC '{swc.name}' (ASIL {level}) requires '{element.name}' "
                            f"(ASIL {element.asil}) through port '{port.name}' of "
                            f"'{port.interface}' and declares no measure for it: a component "
                            "rated above an element it consumes."
                        ),
                        # A fourth repair beside the provider rule's three: declare the measure.
                        subjects=(
                            Subject("swc", swc.name, (field_step("asil"),)),
                            Subject(
                                "swc",
                                swc.name,
                                (field_step("ports"), member_step(port.name), field_step("measures")),
                            ),
                            Subject(
                                "interfaces",
                                port.interface,
                                (
                                    field_step("data_elements"),
                                    member_step(element.name),
                                    field_step("asil"),
                                ),
                            ),
                            *(
                                Subject("requirements", req.id, (field_step("asil"),))
                                for req in _allocating(swc, index.allocation_index)
                            ),
                        ),
                    )
                )
    return records


def _measure_unknown_element_records(swcs: Sequence[SWComponent], index: _CoexistenceIndex) -> list[ConsistencyIssue]:
    """Rule 3 of :func:`safety_coexistence_records`: a measure naming no data element of the port's interface."""
    records: list[ConsistencyIssue] = []

    # Require ports only: a map on a provide port is rule 4's, whose one repair
    # (remove the map) decides the key question too. A stub component is skipped
    # for the same reason: its map is `swc.external-with-ports`' finding.
    for swc in swcs:
        if swc.external:
            continue
        for port in swc.ports:
            if port.direction != "require" or not port.measures or port.interface not in index.declared_interfaces:
                continue
            declared = index.elements_of.get(port.interface, {})
            stub = index.external_interfaces.get(port.interface)
            for key in port.measures:
                if key in declared:
                    continue
                # A key a stub lists is accepted; no level rule reads it.
                if stub is not None and key in stub_member_names(stub, "element"):
                    continue
                records.append(
                    ConsistencyIssue(
                        rule="safety.measure-unknown-element",
                        message=(
                            f"SWC '{swc.name}' port '{port.name}' declares a measure for "
                            f"'{key}', which interface '{port.interface}' does not declare as a "
                            "data element: an argument no rule can read."
                            if stub is None
                            else f"SWC '{swc.name}' port '{port.name}' declares a measure for "
                            f"'{key}', but {stub_member_miss(stub, 'element', key)}."
                        ),
                        # Ends in a KEY step: a mapping entry is addressed by its key, never as a member.
                        subjects=(
                            Subject(
                                "swc",
                                swc.name,
                                (
                                    field_step("ports"),
                                    member_step(port.name),
                                    field_step("measures"),
                                    key_step(key),
                                ),
                            ),
                        ),
                    )
                )
    return records


def _measure_on_provide_port_records(swcs: Sequence[SWComponent]) -> list[ConsistencyIssue]:
    """Rule 4 of :func:`safety_coexistence_records`: a measure on a provide port."""
    records: list[ConsistencyIssue] = []

    # A stub's port is `swc.external-with-ports`' finding, as in rule 3.
    for swc in swcs:
        if swc.external:
            continue
        for port in swc.ports:
            if port.direction != "provide" or not port.measures:
                continue
            named = ", ".join(f"'{key}'" for key in port.measures)
            records.append(
                ConsistencyIssue(
                    rule="safety.measure-on-provide-port",
                    message=(
                        f"SWC '{swc.name}' port '{port.name}' is a provide port and declares "
                        f"measures for {named}: no rule reads a measure on a provide port, and a "
                        "measure belongs on the consumer."
                    ),
                    # The one legal repair removes the map, so the subject is the field, not a key.
                    subjects=(
                        Subject(
                            "swc",
                            swc.name,
                            (field_step("ports"), member_step(port.name), field_step("measures")),
                        ),
                    ),
                )
            )
    return records


def _task_mixed_asil_records(tasks: Sequence[Task], index: _CoexistenceIndex) -> list[ConsistencyIssue]:
    """Rule 5 of :func:`safety_coexistence_records`: a task mixing levels with no coexistence argument."""
    records: list[ConsistencyIssue] = []

    for task in tasks:
        if task.coexistence:
            continue
        mapped = _task_component_levels(
            task, index.by_swc_name, lambda swc: effective_level_of(swc, index.derived), index.identities
        )
        # Distinct levels are counted by `X` (`asil_rank`): a `B` and a `B(D)`
        # component are one level; the message still names each as written.
        distinct = len({asil_rank(level) for level in mapped.values()})
        if distinct < 2:
            continue
        named = ", ".join(f"'{name}' (ASIL {mapped[name]})" for name in sorted(mapped))
        allocating: list[Requirement] = []
        for name in sorted(mapped):
            for req in _allocating(index.by_swc_name[name], index.allocation_index):
                if req not in allocating:
                    allocating.append(req)
        records.append(
            ConsistencyIssue(
                rule="safety.task-mixed-asil",
                message=(
                    f"Task '{task.name}' schedules components of {distinct} "
                    f"different ASILs and declares no coexistence argument: {named}."
                ),
                subjects=(
                    Subject("tasks", task.name, (field_step("coexistence"),)),
                    *(Subject("swc", name, (field_step("asil"),)) for name in sorted(mapped)),
                    *(Subject("requirements", req.id, (field_step("asil"),)) for req in allocating),
                ),
            )
        )
    return records


def _measure_blank_records(swcs: Sequence[SWComponent]) -> list[ConsistencyIssue]:
    """Rule 6 of :func:`safety_coexistence_records`: a blank measure on a require port."""
    records: list[ConsistencyIssue] = []

    # `str.strip` reaches Unicode whitespace such as U+00A0 and not a zero-width
    # space. Require ports of non-stub components only, for rule 3's reasons.
    # Beside rule 3 it is a second cause: renaming the key leaves the text blank.
    for swc in swcs:
        if swc.external:
            continue
        for port in swc.ports:
            if port.direction != "require" or not port.measures:
                continue
            for key, text in port.measures.items():
                if text.strip():
                    continue
                records.append(
                    ConsistencyIssue(
                        rule="safety.measure-blank",
                        message=(
                            f"SWC '{swc.name}' port '{port.name}' declares a blank measure for "
                            f"'{key}': a measure names the mechanism that answers a coexistence "
                            "finding, and an empty text names none. Write the mechanism, or delete "
                            "the entry."
                        ),
                        # Rule 3's address, so the finding lands on the entry's own line.
                        subjects=(
                            Subject(
                                "swc",
                                swc.name,
                                (
                                    field_step("ports"),
                                    member_step(port.name),
                                    field_step("measures"),
                                    key_step(key),
                                ),
                            ),
                        ),
                    )
                )
    return records
