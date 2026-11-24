"""Immutable semantic model of a generated project."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypeAlias

from .errors import GenerationError
from .names import validate_identifier, validate_unique_identifiers
from .preset import ASIL, Preset


@dataclass(frozen=True)
class Port:
    name: str
    direction: str
    interface: str


@dataclass(frozen=True)
class Runnable:
    """A runnable; ``period_us`` ``None`` is a runnable with no event (a specimen's)."""

    name: str
    period_us: int | None
    writes: tuple[tuple[str, str], ...] = ()
    reads: tuple[tuple[str, str], ...] = ()
    calls: tuple[tuple[str, str], ...] = ()
    wcet_us: int = 0


@dataclass(frozen=True)
class Component:
    name: str
    group: str
    asil: ASIL
    ports: tuple[Port, ...]
    runnables: tuple[Runnable, ...]
    memory: tuple[tuple[str, int], ...]
    description: str = ""


@dataclass(frozen=True)
class Domain:
    name: str
    components: tuple[str, ...]
    identifier: str = ""
    title: str = ""
    token: str = ""
    description: str = ""


@dataclass(frozen=True)
class Processor:
    name: str


@dataclass(frozen=True)
class Core:
    name: str
    threshold: Decimal
    capability: ASIL


@dataclass(frozen=True)
class Memory:
    name: str
    size_bytes: int
    threshold: Decimal
    accessible: tuple[str, ...]


@dataclass(frozen=True)
class Partition:
    """An explicit partition on ``core``; ``asil`` ``ABSENT`` is unrated (a specimen's)."""

    name: str
    core: str
    asil: ASIL = ASIL.ABSENT
    #: The argument for running components of several levels (a specimen's); empty is not written.
    coexistence: str = ""


@dataclass(frozen=True)
class BaseType:
    name: str
    size_bits: int
    encoding: str
    native_declaration: str


@dataclass(frozen=True)
class ValueType:
    name: str
    base: str


@dataclass(frozen=True)
class ArrayType:
    name: str
    element_type: str
    length: int


@dataclass(frozen=True)
class RecordField:
    name: str
    type: str


@dataclass(frozen=True)
class RecordType:
    name: str
    fields: tuple[RecordField, ...]


DataType: TypeAlias = BaseType | ValueType | ArrayType | RecordType


@dataclass(frozen=True)
class DataElement:
    name: str
    type: str


@dataclass(frozen=True)
class Argument:
    name: str
    direction: str
    type: str


@dataclass(frozen=True)
class Operation:
    name: str
    arguments: tuple[Argument, ...]


@dataclass(frozen=True)
class Interface:
    name: str
    kind: str
    data_elements: tuple[DataElement, ...] = ()
    operations: tuple[Operation, ...] = ()


@dataclass(frozen=True)
class ExternalInterface:
    """A stub for an interface declared outside the project (SWCraft's ``external_interface``).

    It carries the AUTOSAR path and the ``DEST`` of the reference and no member names, so no
    runnable reads, writes or calls through a port naming it.
    """

    name: str
    external_ref: str
    external_dest: str

    @property
    def kind(self) -> str:
        return "external_interface"


AnyInterface: TypeAlias = Interface | ExternalInterface


@dataclass(frozen=True)
class Unit:
    """A unit (``data_types/units/``); an empty ``display_name`` is not written."""

    name: str
    display_name: str = ""


@dataclass(frozen=True)
class LinearCompuMethod:
    """A ``linear`` compu method: physical = stored x ``factor`` + ``offset``, in ``unit``."""

    name: str
    unit: str
    factor: int = 1
    offset: int = 0


@dataclass(frozen=True)
class TextTableCompuMethod:
    """A ``texttable`` compu method: its ``(name, value)`` entries, in order, none repeated."""

    name: str
    entries: tuple[tuple[str, int], ...]


CompuMethod: TypeAlias = LinearCompuMethod | TextTableCompuMethod


@dataclass(frozen=True)
class DataConstr:
    """A data constraint (``data_types/constraints/``); a ``None`` limit is not written."""

    name: str
    min: int | None = None
    max: int | None = None


@dataclass(frozen=True)
class ApplicationType:
    """An ``application_type`` naming its unit, linear compu method and constraint by reference.

    Nothing inline: only the inline forms generate a ``<name>_Unit``, ``_Compu`` or ``_Range``
    element; a ``None`` reference is not written.
    """

    name: str
    unit_ref: str | None = None
    compu_method_ref: str | None = None
    data_constr_ref: str | None = None


@dataclass(frozen=True)
class MappingSet:
    """A data type mapping set: its ``(application, implementation)`` rows, in order."""

    name: str
    maps: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ConnectorEndpoint:
    swc: str | None
    port: str
    composition: str | None = None


@dataclass(frozen=True)
class Connector:
    name: str
    kind: str
    source: ConnectorEndpoint
    target: ConnectorEndpoint
    composition: str | None = None
    description: str = ""


#: The seven site kinds a requirement link may name (SWCraft's `satisfies` sites), in `LINK_KINDS`
#: in the order `passport._link_sites` reads them.
SWC_SITE = "swc"
PORT_SITE = "port"
RUNNABLE_SITE = "runnable"
INTERFACE_SITE = "interface"
COMPOSITION_SITE = "composition"
TASK_SITE = "task"
HARDWARE_SITE = "hardware"
LINK_KINDS = (
    SWC_SITE,
    PORT_SITE,
    RUNNABLE_SITE,
    INTERFACE_SITE,
    COMPOSITION_SITE,
    TASK_SITE,
    HARDWARE_SITE,
)


@dataclass(frozen=True)
class RequirementLink:
    """One link of a requirement on one site.

    ``kind`` is one of ``LINK_KINDS``; ``element`` names the component (for ``swc``, ``port`` and
    ``runnable``), the interface, the composition, the task or the hardware element, and
    ``member`` the port or the runnable of that component (``None`` for every other kind).
    ``version`` is the requirement version the link states (``<id>@<version>``).
    """

    kind: str
    element: str
    member: str | None = None
    version: int = 1


@dataclass(frozen=True)
class Requirement:
    """A requirement; ``asil`` ``ABSENT`` is not written (a specimen's).

    ``links`` are its links on any of the seven site kinds, each at its own version: a WATERS
    preset's are ``swc`` links at version 1 (``component_links``), a specimen's any kind.
    """

    id: str
    title: str
    description: str
    asil: ASIL
    links: tuple[RequirementLink, ...]
    version: int = 1


def component_links(components: Iterable[str]) -> tuple[RequirementLink, ...]:
    """A WATERS requirement's links: one ``swc`` link at version 1 per component named."""
    return tuple(RequirementLink(SWC_SITE, component) for component in components)


@dataclass(frozen=True)
class Task:
    """A task on ``core``; ``partition`` ``None`` names the core's derived partition (WATERS).

    A specimen's task names its explicit partition, on that partition's core, and may carry a
    ``coexistence`` text; empty is not written.
    """

    name: str
    period_us: int
    priority: int
    core: str
    wcet_us: int
    runnables: tuple[tuple[str, str], ...]
    asil: ASIL = ASIL.QM
    partition: str | None = None
    coexistence: str = ""


@dataclass(frozen=True)
class TaskSequence:
    """A sequence of ``task``: its steps, each a ``(component, runnable)`` the task runs.

    The title and every step's note are written empty."""

    name: str
    task: str
    steps: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CompositionMember:
    name: str
    type: str | None = None
    composition: str | None = None
    description: str = ""


@dataclass(frozen=True)
class CompositionPort:
    name: str
    direction: str
    interface: str
    description: str = ""


@dataclass(frozen=True)
class Delegation:
    name: str
    port: str
    member: str
    member_port: str
    description: str = ""


@dataclass(frozen=True)
class Composition:
    name: str
    title: str
    root: bool
    members: tuple[CompositionMember, ...]
    description: str = ""
    ports: tuple[CompositionPort, ...] = ()
    delegations: tuple[Delegation, ...] = ()


@dataclass(frozen=True)
class Project:
    name: str
    processor: Processor
    cores: tuple[Core, ...]
    memories: tuple[Memory, ...]
    domains: tuple[Domain, ...]
    components: tuple[Component, ...]
    data_types: tuple[DataType, ...]
    interfaces: tuple[AnyInterface, ...]
    connectors: tuple[Connector, ...]
    requirements: tuple[Requirement, ...]
    tasks: tuple[Task, ...]
    compositions: tuple[Composition, ...]
    emit_runnable_wcet: bool = False
    #: The explicit partitions (a specimen's); ``None`` derives one unrated partition per core.
    partitions: tuple[Partition, ...] | None = None
    #: The five type layers (a specimen's), in emitted order.
    units: tuple[Unit, ...] = ()
    compu_methods: tuple[CompuMethod, ...] = ()
    data_constraints: tuple[DataConstr, ...] = ()
    application_types: tuple[ApplicationType, ...] = ()
    mapping_sets: tuple[MappingSet, ...] = ()
    #: The sequences (a specimen's).
    sequences: tuple[TaskSequence, ...] = ()


def build_tiny_project(preset: Preset) -> Project:
    """Build the fixed two-component tiny project from a validated preset."""
    if (preset.components, preset.domains, len(preset.cores)) != (2, 1, 2):
        raise GenerationError("the tiny builder supports exactly components=2, domains=1, cores=2")
    if any(core.capability is ASIL.ABSENT for core in preset.cores):
        raise GenerationError("the tiny builder requires ASIL capability on both cores")
    if preset.runnables_per_component.minimum > 1 or preset.runnables_per_component.maximum < 1:
        raise GenerationError("the tiny builder requires one runnable per component")
    if len(preset.periods_us) != 1:
        raise GenerationError("the tiny builder requires exactly one period")

    period_us = preset.periods_us[0][0]
    wcet = max(1, int(preset.wcet_us_by_period[0][1].average_us))
    c0, c1 = "Dom01_Swc001", "Dom01_Swc002"
    core0, core1 = preset.cores[0].name, preset.cores[1].name
    shared, private0, private1 = "RAM_SHARED", f"RAM_{core0}", f"RAM_{core1}"
    components = (
        Component(
            c0,
            "dom01",
            ASIL.B,
            (
                Port("provide_data", "provide", "IfData"),
                Port("provide_service", "provide", "IfService"),
            ),
            (Runnable("write", period_us, (("provide_data", "Data"),)),),
            ((shared, 64), (private0, 16), (private1, 0)),
        ),
        Component(
            c1,
            "dom01",
            ASIL.B,
            (
                Port("require_data", "require", "IfData"),
                Port("require_service", "require", "IfService"),
            ),
            (
                Runnable("read", period_us, reads=(("require_data", "Data"),)),
                Runnable("call", period_us, calls=(("require_service", "exchange"),)),
            ),
            ((shared, 64), (private0, 0), (private1, 16)),
        ),
    )
    types: tuple[DataType, ...] = (
        BaseType("u8_base", 8, "NONE", "uint8_t"),
        ValueType("u8", "u8_base"),
        ArrayType("payload_array", "u8", 4),
        RecordType("payload_record", (RecordField("value", "u8"), RecordField("payload", "payload_array"))),
    )
    interfaces = (
        Interface("IfData", "sender_receiver", data_elements=(DataElement("Data", "u8"),)),
        Interface(
            "IfService",
            "client_server",
            operations=(
                Operation(
                    "exchange",
                    (
                        Argument("request", "in", "u8"),
                        Argument("response", "out", "payload_record"),
                        Argument("state", "inout", "payload_array"),
                    ),
                ),
            ),
        ),
    )
    return Project(
        preset.name,
        Processor("CPU"),
        tuple(Core(core.name, preset.core_threshold, core.capability) for core in preset.cores),
        (
            Memory(
                shared,
                preset.memories.shared.size_bytes,
                preset.memories.shared.threshold,
                (core0, core1),
            ),
            Memory(
                private0,
                preset.memories.private_per_core.size_bytes,
                preset.memories.private_per_core.threshold,
                (core0,),
            ),
            Memory(
                private1,
                preset.memories.private_per_core.size_bytes,
                preset.memories.private_per_core.threshold,
                (core1,),
            ),
        ),
        (Domain("Dom01", (c0, c1)),),
        components,
        types,
        interfaces,
        (
            Connector(
                "DataConnector",
                "sender_receiver",
                ConnectorEndpoint(c0, "provide_data"),
                ConnectorEndpoint(c1, "require_data"),
            ),
            Connector(
                "ServiceConnector",
                "client_server",
                ConnectorEndpoint(c0, "provide_service"),
                ConnectorEndpoint(c1, "require_service"),
            ),
        ),
        (
            Requirement(
                "REQ_ASIL_B_001",
                "Tiny slice safety requirement",
                "The tiny slice shall exchange data safely.",
                ASIL.B,
                component_links((c0, c1)),
            ),
        ),
        (
            Task(
                "Task_CORE0_B",
                period_us,
                10,
                core0,
                preset.task_overhead_us + wcet,
                ((c0, "write"),),
            ),
            Task(
                "Task_CORE1_B",
                period_us,
                10,
                core1,
                preset.task_overhead_us + (wcet * 2),
                ((c1, "read"), (c1, "call")),
            ),
        ),
        (
            Composition(
                "Root",
                "Specimen generator tiny root",
                True,
                (CompositionMember(c0, c0), CompositionMember(c1, c1)),
            ),
        ),
    )


def partition_name(core: str) -> str:
    """Return the name of the one partition on ``core``."""
    return f"Partition_{core}"


def partitions(project: Project) -> tuple[Partition, ...]:
    """Return the project's partitions in emitted order.

    The explicit ``Project.partitions`` when present (a specimen's, which may be empty and may be
    rated). Otherwise one unrated partition per core, in core order: every core runs components of
    several levels, so no partition carries an ``asil``. The derived partitions are never stored in
    ``Project`` and consume no PRNG draw.
    """
    if project.partitions is not None:
        return project.partitions
    return tuple(Partition(partition_name(core.name), core.name) for core in project.cores)


def task_partition(task: Task) -> str:
    """Return the partition ``task`` names: its own, else its core's derived one."""
    return task.partition if task.partition is not None else partition_name(task.core)


def hardware_names(project: Project) -> tuple[str, ...]:
    """Return every hardware element's name in emitted order: one file each in ``model/hardware/``.

    The processor, the cores, the memories, and the partitions (``partitions``).
    """
    return (
        project.processor.name,
        *(core.name for core in project.cores),
        *(memory.name for memory in project.memories),
        *(partition.name for partition in partitions(project)),
    )


#: The five type layers, each keyed as the passport keys it and as `Project` names it, with the
#: name of one element as a check message calls it.
_TYPE_LAYER_NAMES: dict[str, str] = {
    "application_types": "application type name",
    "mapping_sets": "mapping set name",
    "units": "unit name",
    "compu_methods": "compu method name",
    "data_constraints": "data constraint name",
}


def _type_layer_names(project: Project) -> list[tuple[str, str, list[str]]]:
    """Each type layer's key, its element's name as a message calls it, and its names."""
    return [
        (key, label, [element.name for element in getattr(project, key)]) for key, label in _TYPE_LAYER_NAMES.items()
    ]


def _limited_names(project: Project) -> list[tuple[str, str, list[str]]]:
    """Each key of a per-layer identifier limit table with its context and names.

    The keys are the passport identifier layers, the interfaces split by kind, the external
    interface stubs and the five type layers; one passport layer's names are unique ignoring case."""
    stubs = [i for i in project.interfaces if isinstance(i, ExternalInterface)]
    named = [i for i in project.interfaces if not isinstance(i, ExternalInterface)]
    return [
        ("swc", "component name", [c.name for c in project.components]),
        (
            "interfaces.sender_receiver",
            "sender-receiver interface name",
            [i.name for i in named if i.kind == "sender_receiver"],
        ),
        (
            "interfaces.client_server",
            "client-server interface name",
            [i.name for i in named if i.kind != "sender_receiver"],
        ),
        (
            "interfaces.external",
            "external interface name",
            [i.name for i in stubs],
        ),
        ("connectors", "connector name", [c.name for c in project.connectors]),
        (
            "implementation_types",
            "implementation type name",
            [t.name for t in project.data_types if not isinstance(t, BaseType)],
        ),
        (
            "base_types",
            "base type name",
            [t.name for t in project.data_types if isinstance(t, BaseType)],
        ),
        ("hardware", "hardware name", list(hardware_names(project))),
        ("compositions", "composition name", [c.name for c in project.compositions]),
        *_type_layer_names(project),
        ("requirements", "requirement id", [r.id for r in project.requirements]),
        ("tasks", "task name", [t.name for t in project.tasks]),
        ("sequences", "sequence name", [s.name for s in project.sequences]),
    ]


def _validate_limited_names(project: Project, limits: Mapping[str, int]) -> None:
    limited = _limited_names(project)
    keys = sorted(key for key, _, _ in limited)
    if sorted(limits) != keys:
        raise GenerationError(f"identifier limit table must name {keys}, not {sorted(limits)}")
    for key, context, names in limited:
        for name in names:
            validate_identifier(name, context, limit=limits[key])
    validate_unique_identifiers((i.name for i in project.interfaces), "interface names")
    for key, _, names in limited:
        if not key.startswith("interfaces."):
            validate_unique_identifiers(names, f"{key} names")


def _validate_type_layers(project: Project) -> None:
    """Check that every type-layer name is unique and every reference names an admitted element.

    Every name of a type layer is unique in its layer, and every reference names a declared
    element of the kind SWCraft admits there.
    """
    try:
        for _, label, names in _type_layer_names(project):
            validate_unique_identifiers(names, f"{label}s")
    except ValueError as exc:
        raise GenerationError(str(exc)) from None
    units = {u.name for u in project.units}
    # An application type names a linear method only: a texttable is SWCraft's
    # ``compu-method-wrong-category`` there.
    linear = {m.name for m in project.compu_methods if isinstance(m, LinearCompuMethod)}
    constraints = {c.name for c in project.data_constraints}
    for method in project.compu_methods:
        if isinstance(method, LinearCompuMethod) and method.unit not in units:
            raise GenerationError(f"compu method {method.name} names an undeclared unit {method.unit!r}")
        if isinstance(method, TextTableCompuMethod):
            entry_names = [name for name, _ in method.entries]
            if not entry_names or len(set(entry_names)) != len(entry_names):
                raise GenerationError(f"compu method {method.name} needs entries, none repeated: {entry_names}")
    for application in project.application_types:
        for field, value, declared, what in (
            ("unit_ref", application.unit_ref, units, "unit"),
            ("compu_method_ref", application.compu_method_ref, linear, "linear compu method"),
            ("data_constr_ref", application.data_constr_ref, constraints, "data constraint"),
        ):
            if value is not None and value not in declared:
                raise GenerationError(
                    f"application type {application.name} {field} names an undeclared {what} {value!r}"
                )
    applications = {a.name for a in project.application_types}
    # An application type pairs with a value type (the generator writes no other pairing).
    value_types = {t.name for t in project.data_types if isinstance(t, ValueType)}
    for mapping_set in project.mapping_sets:
        mapped: set[str] = set()
        for application_name, implementation in mapping_set.maps:
            if application_name not in applications:
                raise GenerationError(
                    f"mapping set {mapping_set.name} names an undeclared application type {application_name!r}"
                )
            if implementation not in value_types:
                raise GenerationError(
                    f"mapping set {mapping_set.name} maps {application_name} to "
                    f"{implementation!r}, which is no declared value type"
                )
            if application_name in mapped:
                raise GenerationError(f"mapping set {mapping_set.name} maps {application_name} twice")
            mapped.add(application_name)


def _is_chain(project: Project, legacy_flat: bool) -> bool:
    """Whether the project takes the specimen chain's shape.

    Not flat, one root, no composition named as a domain (the domain shape's positive signal:
    its compositions carry their domains' names, the chain's are ``Root`` and ``Cmp...``), and
    every composition holding at most one composition member. ``_validate_chain`` checks the
    rest; a malformed domain-shaped project keeps the domain shape's messages."""
    if legacy_flat or sum(c.root for c in project.compositions) != 1:
        return False
    domains = {d.name for d in project.domains}
    return not any(c.name in domains for c in project.compositions) and all(
        sum(m.composition is not None for m in c.members) <= 1 for c in project.compositions
    )


def _validate_chain(project: Project) -> None:
    """Check the specimen chain of compositions, its members and its connectors.

    The root, then each composition's one composition member down to the deepest, which holds
    none; every composition on it; every component a member of exactly one composition; every
    connector owned by the root (``composition`` unset) with both endpoints root members.
    """
    compositions = {c.name: c for c in project.compositions}
    current = next(c for c in project.compositions if c.root)
    root = current
    chain = [current.name]
    on_chain = {current.name}
    while True:
        child = next((m.composition for m in current.members if m.composition), None)
        if child is None:
            break
        if child not in compositions:
            raise GenerationError(f"unresolved composition reference: {child}")
        if child in on_chain or compositions[child].root:
            raise GenerationError("composition cycle detected")
        chain.append(child)
        on_chain.add(child)
        current = compositions[child]
    outside = [c.name for c in project.compositions if c.name not in on_chain]
    if outside:
        raise GenerationError(f"composition outside the chain: {', '.join(outside)}")
    members = [m.type for c in project.compositions for m in c.members if m.type is not None]
    component_names = [c.name for c in project.components]
    if sorted(members) != sorted(component_names):
        raise GenerationError("each component must be a member of exactly one composition")
    root_components = {m.type for m in root.members if m.type is not None}
    root_compositions = {m.composition for m in root.members if m.composition is not None}
    for connector in project.connectors:
        if connector.composition is not None:
            raise GenerationError(f"connector owned by a nested composition: {connector.name}")
        for endpoint in (connector.source, connector.target):
            if not (endpoint.swc in root_components or endpoint.composition in root_compositions):
                raise GenerationError(f"connector endpoint outside the root: {connector.name}")


def _validate_sequences(project: Project) -> None:
    """Every sequence names a declared task, and its steps are distinct runnables of that task."""
    runnables_of = {task.name: set(task.runnables) for task in project.tasks}
    for sequence in project.sequences:
        if sequence.task not in runnables_of:
            raise GenerationError(f"sequence {sequence.name} names an undeclared task {sequence.task!r}")
        if not sequence.steps:
            raise GenerationError(f"sequence {sequence.name} has no step")
        seen: set[tuple[str, str]] = set()
        for step in sequence.steps:
            component, runnable = step
            if step not in runnables_of[sequence.task]:
                raise GenerationError(
                    f"sequence {sequence.name} step {component}.{runnable} is not a runnable of "
                    f"its task {sequence.task}"
                )
            if step in seen:
                raise GenerationError(f"sequence {sequence.name} names the step {component}.{runnable} twice")
            seen.add(step)


def link_sites(project: Project) -> dict[str, set[tuple[str, str | None]]]:
    """Every site a ``RequirementLink`` may name, per kind, as its ``(element, member)``."""
    return {
        SWC_SITE: {(c.name, None) for c in project.components},
        PORT_SITE: {(c.name, p.name) for c in project.components for p in c.ports},
        RUNNABLE_SITE: {(c.name, r.name) for c in project.components for r in c.runnables},
        INTERFACE_SITE: {(i.name, None) for i in project.interfaces},
        COMPOSITION_SITE: {(c.name, None) for c in project.compositions},
        TASK_SITE: {(t.name, None) for t in project.tasks},
        HARDWARE_SITE: {(name, None) for name in hardware_names(project)},
    }


def _validate_requirement_links(project: Project) -> None:
    """Check the requirement versions and the sites and versions every link names.

    Every requirement states a version of at least 1, and every ``RequirementLink`` names a
    declared site at a version of its requirement.
    """
    sites = link_sites(project)
    for requirement in project.requirements:
        if requirement.version < 1:
            raise GenerationError(
                f"requirement {requirement.id} states version {requirement.version}; a version is at least 1"
            )
        for link in requirement.links:
            if (link.element, link.member) not in sites.get(link.kind, set()):
                shown = link.element if link.member is None else f"{link.element}.{link.member}"
                raise GenerationError(f"unresolved requirement link: {requirement.id} {link.kind} {shown}")
            if not 1 <= link.version <= requirement.version:
                raise GenerationError(
                    f"requirement link {requirement.id}@{link.version} names no version of "
                    f"{requirement.id} (1 to {requirement.version})"
                )


@dataclass(frozen=True)
class _ProjectIndex:
    """The lookups ``validate_project``'s later checks share, built once by ``_index_project``."""

    interfaces: dict[str, AnyInterface]
    ports: dict[tuple[str, str], Port]
    runnable_ids: set[tuple[str, str]]
    compositions: dict[str, Composition]
    root: Composition
    domain_by_component: dict[str, str]


#: The three composition shapes ``validate_project`` admits; exactly one holds for a project.
_Shape: TypeAlias = Literal["flat", "chain", "domain"]


def _project_shape(project: Project) -> _Shape:
    """Return the composition shape of ``project``.

    Raise ``GenerationError`` unless it has a core and exactly one root composition (and, in the
    domain shape, one composition per domain beside the root). A project neither flat nor a chain
    is the domain shape.
    """
    legacy_flat = len(project.compositions) == 1 and all(
        m.type in {c.name for c in project.components} and m.composition is None
        for m in project.compositions[0].members
    )
    chain = _is_chain(project, legacy_flat)
    # Neither flat nor a chain: the domain shape.
    domain_shape = not legacy_flat and not chain
    if (
        not project.cores
        or (domain_shape and len(project.compositions) != len(project.domains) + 1)
        or sum(c.root for c in project.compositions) != 1
    ):
        raise GenerationError("project requires at least one core and exactly one root composition")
    if legacy_flat:
        return "flat"
    return "chain" if chain else "domain"


def _validate_component_names(project: Project, limits: Mapping[str, int] | None) -> list[str]:
    """Check the component and hardware names and return the component names in project order.

    The names are unique, valid identifiers, held to ``limits`` when given; raise
    ``GenerationError`` on the first defect.
    """
    names = [c.name for c in project.components]
    try:
        validate_unique_identifiers(names, "component names")
        hardware = list(hardware_names(project))
        if limits is None:
            for name in names + hardware:
                validate_identifier(name, "generated identity", generated=True)
        else:
            _validate_limited_names(project, limits)
        # Every hardware element is one file in model/hardware/, so a derived partition name
        # must not collide with a processor, core or memory name, ignoring case.
        validate_unique_identifiers(hardware, "hardware names")
    except ValueError as exc:
        raise GenerationError(str(exc)) from None
    return names


def _index_project(project: Project) -> _ProjectIndex:
    """Build the project's ``_ProjectIndex``.

    Raise ``GenerationError`` when two compositions share a name, which the index's composition
    lookup cannot hold.
    """
    interfaces = {i.name: i for i in project.interfaces}
    ports = {(c.name, p.name): p for c in project.components for p in c.ports}
    runnable_ids = {(c.name, r.name) for c in project.components for r in c.runnables}
    compositions = {c.name: c for c in project.compositions}
    if len(compositions) != len(project.compositions):
        raise GenerationError("duplicate composition identity")
    roots = [c for c in project.compositions if c.root]
    return _ProjectIndex(
        interfaces=interfaces,
        ports=ports,
        runnable_ids=runnable_ids,
        compositions=compositions,
        root=roots[0],
        domain_by_component={name: d.name for d in project.domains for name in d.components},
    )


def _validate_domain_membership(project: Project, index: _ProjectIndex, names: list[str], shape: _Shape) -> None:
    """In the domain shape, check the root, the domains and their components; other shapes pass.

    The root holds exactly the domain compositions, every component of ``names`` sits in one
    domain and each domain composition holds exactly its domain's components; raise
    ``GenerationError`` on the first defect.
    """
    if shape != "domain":
        return
    root = index.root
    if {m.composition for m in root.members} != {d.name for d in project.domains} or any(
        m.type is not None for m in root.members
    ):
        raise GenerationError("root composition must contain only domain compositions")
    component_names = set(names)
    domain_by_component = index.domain_by_component
    if set(domain_by_component) != component_names or len(domain_by_component) != len(component_names):
        raise GenerationError("each component must belong to exactly one domain")
    for domain in project.domains:
        composition = index.compositions.get(domain.name)
        if composition is None or composition.root:
            raise GenerationError(f"missing non-root composition for domain {domain.name}")
        if {m.type for m in composition.members} != set(domain.components) or any(
            m.composition is not None for m in composition.members
        ):
            raise GenerationError(f"domain composition membership invalid: {domain.name}")


def _visit_composition(name: str, graph: Mapping[str, set[str]], active: set[str], done: set[str]) -> None:
    """Walk ``graph`` (composition name to its composition members) depth-first from ``name``.

    Raise ``GenerationError`` on a cycle or a member naming no composition; ``active`` holds the
    current path and ``done`` the names already finished (pass two empty sets).
    """
    if name in active:
        raise GenerationError("composition cycle detected")
    if name in done:
        return
    active.add(name)
    for child in graph[name]:
        if child not in graph:
            raise GenerationError(f"unresolved composition reference: {child}")
        _visit_composition(child, graph, active, done)
    active.remove(name)
    done.add(name)


def _validate_composition_graph(project: Project, root: Composition, shape: _Shape) -> None:
    """Check the composition graph for the project's shape; raise ``GenerationError`` on a defect.

    In the domain shape the graph is acyclic and fully resolved from ``root``; the chain shape
    runs ``_validate_chain``.
    """
    graph = {c.name: {m.composition for m in c.members if m.composition} for c in project.compositions}
    if shape == "domain":
        _visit_composition(root.name, graph, set(), set())
    if shape == "chain":
        _validate_chain(project)


def _validate_task_mapping(project: Project, runnable_ids: set[tuple[str, str]]) -> None:
    """Check the runnable-to-task mapping, the explicit partitions' cores and each task's partition.

    Raise ``GenerationError`` on the first defect.
    """
    task_refs = [ref for task in project.tasks for ref in task.runnables]
    # With a busy task present every runnable is in exactly one task. A specimen (explicit
    # partitions) with no task or with idle tasks only maps none; a WATERS project (derived
    # partitions) with tasks always maps every runnable, so an empty ``task_refs`` there is a defect.
    mapped = bool(task_refs) or (bool(project.tasks) and project.partitions is None)
    if mapped and (set(task_refs) != runnable_ids or len(task_refs) != len(set(task_refs))):
        raise GenerationError("every runnable must be assigned to exactly one task")
    core_names = {c.name for c in project.cores}
    if project.partitions is not None:
        if any(partition.core not in core_names for partition in project.partitions):
            raise GenerationError("an explicit partition names no core of the project")
    core_by_partition = {p.name: p.core for p in partitions(project)}
    for task in project.tasks:
        named = task_partition(task)
        if named not in core_by_partition:
            raise GenerationError(f"task {task.name} names an undeclared partition {named!r}")
        if core_by_partition[named] != task.core:
            raise GenerationError(
                f"task {task.name} names partition {named} on core {core_by_partition[named]}, not its core {task.core}"
            )


def _validate_tasks_and_component_ports(project: Project, interfaces: Mapping[str, AnyInterface]) -> None:
    """Check the tasks, the components' memory budgets, their ports and the members they reach.

    Each task's core, WCET, period and ASIL, then each component's memory budget set, its ports'
    interfaces and the port members its runnables reach; raise ``GenerationError`` on the first
    defect.
    """
    core_names = {c.name for c in project.cores}
    for task in project.tasks:
        if task.core not in core_names or task.wcet_us <= 0 or task.period_us <= 0 or task.asil is ASIL.ABSENT:
            raise GenerationError(f"invalid task assignment or WCET: {task.name}")
    for component in project.components:
        if {name for name, _ in component.memory} != {m.name for m in project.memories}:
            raise GenerationError(f"component {component.name} lacks a strict memory budget")
        for port in component.ports:
            if port.interface not in interfaces:
                raise GenerationError(f"unresolved interface: {port.interface}")
        for runnable in component.runnables:
            for port_name, member in (*runnable.reads, *runnable.writes, *runnable.calls):
                runnable_port = next((p for p in component.ports if p.name == port_name), None)
                if runnable_port is None:
                    raise GenerationError(f"unresolved runnable port: {component.name}.{port_name}")
                interface = interfaces[runnable_port.interface]
                # A stub lists no member names: nothing is reached through it.
                members = (
                    set()
                    if isinstance(interface, ExternalInterface)
                    else {e.name for e in interface.data_elements} | {o.name for o in interface.operations}
                )
                if member not in members:
                    raise GenerationError(f"unresolved runnable member: {runnable_port.interface}.{member}")


def _validate_memory_budgets(project: Project) -> None:
    """Check the memory budgets; raise ``GenerationError`` on a defect.

    No memory budget is negative, and every memory a component budgets is accessible from each
    core its mapped runnables run on.
    """
    task_by_runnable = {ref: task.core for task in project.tasks for ref in task.runnables}
    for component in project.components:
        for memory_name, amount in component.memory:
            if amount < 0:
                raise GenerationError(f"negative memory budget: {component.name}.{memory_name}")
            if amount:
                # An unmapped runnable (idle tasks only) runs on no core.
                cores = {
                    task_by_runnable[(component.name, r.name)]
                    for r in component.runnables
                    if (component.name, r.name) in task_by_runnable
                }
                memory = next(m for m in project.memories if m.name == memory_name)
                if not cores <= set(memory.accessible):
                    raise GenerationError(f"memory {memory_name} is inaccessible to {component.name}")


def _endpoint_port(
    endpoint: ConnectorEndpoint, index: _ProjectIndex, domain_shape: bool
) -> tuple[Port | CompositionPort, str | None]:
    """Return a checked endpoint's port and the domain (or composition) it sits in.

    A component's domain is `None` outside the domain shape, which has no domains.
    """
    if endpoint.swc is not None:
        domain = index.domain_by_component[endpoint.swc] if domain_shape else None
        return index.ports[(endpoint.swc, endpoint.port)], domain
    assert endpoint.composition is not None
    composition = index.compositions[endpoint.composition]
    return next(p for p in composition.ports if p.name == endpoint.port), endpoint.composition


def _validate_connector_endpoints(connector: Connector, index: _ProjectIndex) -> None:
    """Check one connector's endpoints and owner; raise ``GenerationError`` on the first defect.

    Each endpoint of ``connector`` selects exactly one of a component and a composition and
    resolves to a declared port, and its owner is a declared composition.
    """
    for endpoint in (connector.source, connector.target):
        if endpoint.swc is not None and endpoint.composition is not None:
            raise GenerationError(f"connector endpoint selects both swc and composition: {connector.name}")
        if endpoint.swc is None and endpoint.composition is None:
            raise GenerationError(f"connector endpoint selects neither swc nor composition: {connector.name}")
        if endpoint.swc is not None and (endpoint.swc, endpoint.port) not in index.ports:
            raise GenerationError(f"unresolved connector endpoint: {endpoint.swc}.{endpoint.port}")
        if endpoint.composition is not None and (
            endpoint.composition not in index.compositions
            or endpoint.port not in {p.name for p in index.compositions[endpoint.composition].ports}
        ):
            raise GenerationError(f"unresolved composition connector endpoint: {endpoint.composition}.{endpoint.port}")
    if connector.composition is not None and connector.composition not in index.compositions:
        raise GenerationError(f"unresolved connector owner: {connector.composition}")


def _validate_connectors(project: Project, index: _ProjectIndex, shape: _Shape) -> None:
    """Check every connector in project order; raise ``GenerationError`` on the first defect.

    Its endpoints and owner, its provide-to-require direction and, in the domain shape, that an
    intra-domain connector belongs to its domain and an inter-domain one to the root.
    """
    domain_shape = shape == "domain"
    for connector in project.connectors:
        _validate_connector_endpoints(connector, index)
        source_port, source_domain = _endpoint_port(connector.source, index, domain_shape)
        target_port, target_domain = _endpoint_port(connector.target, index, domain_shape)
        if source_port.direction != "provide" or target_port.direction != "require":
            raise GenerationError(f"invalid connector directions: {connector.name}")
        if (
            domain_shape
            and connector.source.swc is not None
            and connector.target.swc is not None
            and source_domain == target_domain
            and connector.composition != source_domain
        ):
            raise GenerationError(f"intra-domain connector owner mismatch: {connector.name}")
        if domain_shape and source_domain != target_domain and connector.composition is not None:
            raise GenerationError(f"inter-domain connector must belong to root: {connector.name}")


def _validate_delegations(project: Project, compositions: dict[str, Composition]) -> None:
    """Check each composition's names and delegations; raise ``GenerationError`` on the first defect.

    Member and boundary-port names are unique, and each delegation joins a boundary port to a
    member's port of the same direction and interface.
    """
    # Each element's ports by name, built once: a specimen's delegations number thousands.
    component_ports = {c.name: {p.name: p for p in c.ports} for c in project.components}
    boundary_ports = {c.name: {p.name: p for p in c.ports} for c in project.compositions}
    for composition in project.compositions:
        members_by_name = {m.name: m for m in composition.members}
        if len(members_by_name) != len(composition.members):
            raise GenerationError(f"duplicate members in {composition.name}")
        if len({p.name.casefold() for p in composition.ports}) != len(composition.ports):
            raise GenerationError(f"duplicate boundary ports in {composition.name}")
        own_ports = boundary_ports[composition.name]
        for delegation in composition.delegations:
            if delegation.port not in own_ports or delegation.member not in members_by_name:
                raise GenerationError(f"unresolved delegation: {composition.name}.{delegation.name}")
            delegated = members_by_name[delegation.member]
            inner: Mapping[str, Port | CompositionPort] = {}
            if delegated.type is not None:
                inner = component_ports.get(delegated.type, {})
            elif delegated.composition in compositions:
                inner = boundary_ports[delegated.composition]
            target = inner.get(delegation.member_port)
            outer = own_ports[delegation.port]
            if target is None or target.direction != outer.direction or target.interface != outer.interface:
                raise GenerationError(f"delegation direction/interface mismatch: {composition.name}.{delegation.name}")


def validate_project(project: Project, limits: Mapping[str, int] | None = None) -> None:
    """Check hierarchy, references, wiring and resource-facing invariants.

    Raise ``GenerationError`` at the first violation; return ``None`` when the project is sound.

    Three composition shapes are admitted: the flat one (one composition whose members are all
    components), the domain shape (the root holds one composition per domain) and the specimen
    chain (``_validate_chain``). Delegations are checked in every shape, a composition member's
    boundary port resolved like a component's port.

    ``limits`` is a per-layer identifier limit table keyed as ``_limited_names`` keys it (a
    specimen's); without it a component's and a hardware element's name is held to 60 characters.

    The checks run in a fixed order, which decides the message a project with several defects
    gets: shape, component names, the index with the composition identity check, domain
    membership, the composition graph, task mapping and partitions, sequences, tasks and component ports,
    the type layers, requirement links, memory budgets, connectors, delegations.
    """
    shape = _project_shape(project)
    names = _validate_component_names(project, limits)
    index = _index_project(project)
    _validate_domain_membership(project, index, names, shape)
    _validate_composition_graph(project, index.root, shape)
    _validate_task_mapping(project, index.runnable_ids)
    _validate_sequences(project)
    _validate_tasks_and_component_ports(project, index.interfaces)
    _validate_type_layers(project)
    _validate_requirement_links(project)
    _validate_memory_budgets(project)
    _validate_connectors(project, index, shape)
    _validate_delegations(project, index.compositions)
