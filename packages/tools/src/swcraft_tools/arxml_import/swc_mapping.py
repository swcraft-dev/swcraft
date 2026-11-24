"""SWC/port/runnable/event mapping.

The per-element policies of the component mapping; ``swc_mapper.map_swcs()``
applies them, mapping every APPLICATION-SW-COMPONENT-TYPE to a ``SWComponent``
paired with its ``group`` (``ImportedSwc``). A port's interface ref resolves
through the already-mapped ``interfaces`` (``build_interface_path_index``);
``min_start_interval``/``period`` are copied as float seconds. A
PR-PORT-PROTOTYPE has no SWCraft equivalent and is skipped with a reason; an
OPERATION-INVOKED-EVENT is bound by exact path or skipped with a reason.

Depends on ``merge`` and ``unresolved`` (the two import policies), not on
``type_mapping``.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Literal, NamedTuple, cast

from pydantic import ValidationError

from swcraft_tools.arxml_import.merge import (
    COM_SPEC_KIND_FIELDS,
    CONFLICT,
    EXCLUDED,
    FILTER_COM_SPEC_KIND,
    LAYER_IMPLEMENTATION_TYPES,
    LAYER_INTERFACES,
    MergeDecisions,
    chained_reason,
    com_spec_values,
    read_com_spec_children,
    silent_values_without_place,
    unread_com_spec_children,
)
from swcraft_tools.arxml_import.registry import (
    CarriedValueLine,
    PortInitKey,
    PortInitOutcome,
    Represented,
    VariableTypeOutcome,
    init_radix_line,
)
from swcraft_tools.arxml_import.unresolved import (
    CAUSE_ABSENT,
    UnresolvedReferences,
    UnresolvedReferrer,
    direct_member,
)
from swcraft_tools.arxml_reader import (
    ABORT,
    EXECUTION_TIME_CONSTRAINT,
    MAPPED_PORT_KINDS,
    REPORT,
    ComSpecInfo,
    CSInterfaceInfo,
    DataElementComSpecInfo,
    DisabledModeInfo,
    ImportModel,
    PortApiOptionInfo,
    SkipCategory,
    SkipEffect,
    SkipEntry,
    SkipLog,
    SkipReason,
    SRInterfaceInfo,
    StandardRule,
    SwcAreaRefInfo,
    SwcEventInfo,
    SwcInfo,
    SwcLocalAccessInfo,
    SwcPortInfo,
    SwcRunnableInfo,
    SwcTimingInfo,
    TimingConstraintInfo,
    blank_short_name,
    duplicate_name_reason,
    not_supported,
    parse_int_literal,
)
from swcraft_tools.arxml_version import HANDLE_TERMINATION_AND_RESTART_LITERALS
from swcraft_tools.execution_budget import (
    CSE_CODE,
    MAX_FACTOR_DIGITS,
    Budget,
    CseNotABudget,
    CseOverTheCap,
    from_cse,
)
from swcraft_tools.guarantee import required, required_instance
from swcraft_tools.identifier import C_IDENTIFIER_PATTERN, check_c_identifier, check_identifier, short_name_key
from swcraft_tools.schema import (
    INTERFACE_KIND_PROSE,
    CallReturnedEvent,
    CanEnterArea,
    CSInterface,
    DataEvent,
    DataReadRef,
    DataWriteRef,
    DisabledMode,
    ExclusiveArea,
    ExternalInterface,
    Interface,
    OperationCallRef,
    OperationEvent,
    Port,
    PortComSpec,
    Runnable,
    RunsInsideArea,
    SRInterface,
    SWComponent,
    Variable,
    VariableReadRef,
    VariableWriteRef,
)
from swcraft_tools.validator import (
    derived_event_names,
    derived_result_point_name,
    emitted_access_point_names,
    emitted_behavior_name,
    emitted_variable_access_names,
    result_point_entries,
)
from swcraft_tools.writer.arxml_generation import (
    COM_SPEC_SILENT_VALUES,
    SERVER_QUEUE_LENGTH,
)

#: The XML literal of ``HANDLE-TERMINATION-AND-RESTART`` -> the model's value,
#: the inverse of the writer's table.
HANDLE_TERMINATION_AND_RESTART_VALUES = cast(
    # The writer's table is keyed by the schema field's literals; its annotation says `str`.
    "dict[str, Literal['no_support', 'can_be_terminated', 'can_be_terminated_and_restarted']]",
    {literal: value for value, literal in HANDLE_TERMINATION_AND_RESTART_LITERALS.items()},
)


@dataclass
class ImportedSwc:
    """One mapped SWC paired with its SWCraft ``group`` and the facts its mapping produced.

    ``group`` is a write-time directory parameter (``swc/{group}/``), not a
    field of ``SWComponent``, so it travels beside the model.

    ``path`` is the APPLICATION-SW-COMPONENT-TYPE's own ARXML path (not a
    prototype's): connector resolution keys kept SWCs by path, so a connector to
    a skipped duplicate-named declaration never binds to the surviving one. An
    external stub's ``path`` is the unresolvable ``TYPE-TREF`` itself.

    ``access_collapses``: one lossy-transformation line per access point
    collapsed into an earlier entry of the same ``(list, port, member)``; never
    a skip entry.

    ``queue_length_losses``: one line per kept provide port's ``SERVER-COM-SPEC``
    whose source ``QUEUE-LENGTH`` is not 1, which the export does not carry.
    """

    swc: SWComponent
    group: str
    path: str
    access_collapses: list[str] = field(default_factory=list)
    queue_length_losses: list[str] = field(default_factory=list)
    #: Where each generated element (event, access point) came from, addressed by
    #: runnable, list and index, so a later rename of the component leaves it correct.
    generated_sources: list[GeneratedSource] = field(default_factory=list)
    #: The structured twin of :attr:`queue_length_losses`, one
    #: ``({port}/PROVIDED-COM-SPECS, QUEUE-LENGTH)`` key per line, in the same order.
    queue_length_keys: list[tuple[str, str]] = field(default_factory=list)
    #: One :class:`PortInitRadix` per own init value a kept port carries from a
    #: hexadecimal spelling; carried beside the component so a port in no written
    #: file claims nothing.
    port_init_radix: list[PortInitRadix] = field(default_factory=list)
    #: One :class:`PortInitEquivalent` per own init value a kept port carries by
    #: equivalence with its element's. This holds only while the element keeps its
    #: ``init``; where a later pass drops it, each is named at its key so the port's
    #: source value is never lost without a line.
    port_init_equivalents: list[PortInitEquivalent] = field(default_factory=list)
    #: One :class:`PortInitLabels` per own init value whose ``SHORT-LABEL``s are
    #: lost -- held, not recorded, because the line is true only while the output
    #: holds an ``INIT-VALUE`` at that com-spec (:func:`report_port_init_labels`).
    port_init_labels: list[PortInitLabels] = field(default_factory=list)
    #: Every event, read, write and call through a port whose interface the export
    #: declares nowhere, and every such port's com-spec naming a member, held until
    #: the stub decision or ``import component``'s late pass decides the port
    #: (:func:`finish_stub_members`, :func:`admit_held_records`). Empty once finished.
    held_stub_records: list[HeldStubRecord] = field(default_factory=list)
    held_stub_com_specs: list[HeldStubComSpecs] = field(default_factory=list)
    #: Every kept port whose interface the file declares nowhere, under a policy
    #: that resolves late (``import component``). Its com-specs are held whole,
    #: because the late pass may answer the path with the target project's
    #: declared interface. Empty on ``import model``.
    late_ports: list[LatePort] = field(default_factory=list)
    #: Every com-spec of a kept port naming a member its interface does not
    #: declare, under a late-resolving policy -- a dependency verdict of the late
    #: pass. Empty on ``import model``.
    com_spec_member_misses: list[ComSpecMemberMiss] = field(default_factory=list)
    #: Every event, read, write and call naming a member the file's own interface
    #: at the port's path does not declare, under a late-resolving policy -- a
    #: verdict of the late pass, never an abort. Empty on ``import model``, where
    #: the same shape keeps its abort line.
    behaviour_member_misses: list[UnansweredRecord] = field(default_factory=list)
    #: Where each budget carried onto a runnable came from, one per carried
    #: constraint; the report names the source names and placement the writer
    #: does not keep.
    budget_sources: list[BudgetSource] = field(default_factory=list)
    #: One *Represented in another form* line per budget carried from a CSE code
    #: other than its unit's; carried beside the component so a component in no
    #: written file claims nothing.
    budget_normalizations: list[Represented] = field(default_factory=list)
    #: The radix line of every kept inter-runnable variable whose ``init`` the
    #: source spelled in hexadecimal, octal or binary, by variable name, so the
    #: stub pass can drop the line of an ``init`` it drops.
    variable_init_radix: list[tuple[str, str]] = field(default_factory=list)
    #: Each kept inter-runnable variable's ARXML path, by its name.
    variable_paths: dict[str, str] = field(default_factory=dict)
    #: Every call a result point of the file references, as ``(runnable, port,
    #: operation)``, whether or not the import carried that result point. Tells a
    #: call the file leaves without a result (``[constr_2006]`` at R4.2.2) from one
    #: whose result point the import did not carry.
    stated_result_calls: frozenset[tuple[str, str, str]] = frozenset()


@dataclass(frozen=True)
class BudgetSource:
    """One carried budget's source.

    ``runnable_name`` is the runnable the budget was carried onto; ``timing_path``
    and ``timing_name`` the ``SWC-TIMING`` it sat in and that element's
    ``SHORT-NAME``; ``constraint_path`` and ``constraint_name`` the
    ``EXECUTION-TIME-CONSTRAINT``'s. The writer derives both names
    (``<SWC>_Timing``, ``<runnable>_Budget``) and places the ``SWC-TIMING`` beside
    the component, so each of the three is a report line where the source differs.
    """

    runnable_name: str
    timing_path: str
    timing_name: str
    constraint_path: str
    constraint_name: str


@dataclass(frozen=True)
class LatePort:
    """One kept port whose interface the file declares nowhere, decided after the target is read.

    ``raw`` is the source port, whose ``interface_ref`` is the path the late pass
    answers; ``direction`` the port's, as mapped.
    """

    port_name: str
    raw: SwcPortInfo
    direction: str

    @property
    def interface_path(self) -> str:
        """The interface path the late pass answers (the source's ``interface_ref``, blank when absent)."""
        return self.raw.interface_ref or ""


@dataclass(frozen=True)
class HeldStubRecord:
    """An event, a read, a write or a call through a port on a target that may become an interface stub.

    Held because only the stub decision says whether its member is listed;
    :func:`finish_stub_members` then inserts it into the runnable's
    *collection* after ``position`` kept entries, or records its line.

    ``kind`` is the stub list the member would join. ``build`` makes the model
    entry from the **listed** member name only once the stub lists it, so a
    blank or illegal last segment is a line, never an exception; it takes the
    admitting pass's skip log, because a data-received event's disabled modes
    are read as it is built and the mapper's log may be spent by then.
    ``path``, ``tag``, ``name`` are the source element's; ``misdirected`` is
    the line for a port of the wrong direction, or ``None``; ``reason`` and
    ``once_stubbed`` the line when the member is not listed.
    """

    target: str
    kind: str
    member_ref: str
    runnable_name: str
    collection: str
    position: int
    port: str
    build: Callable[[str, SkipLog], OperationEvent | DataEvent | DataReadRef | DataWriteRef | OperationCallRef]
    path: str
    tag: str
    name: str
    misdirected: str | None
    reason: str
    once_stubbed: Callable[[str], str]
    #: A read in the by-value container, whose queued-element combination the
    #: late pass refuses (:func:`admit_held_records`).
    by_value: bool = False
    #: An implicit read, whose queued-element combination (``[constr_2020]``)
    #: the late pass names as the by-value one's.
    implicit_read: bool = False


class HeldComSpec(NamedTuple):
    """One com-spec of a kept port on a target that may become an interface stub, held for its decision."""

    shape: ComSpecInfo
    #: The stub list the member would join: ``operations`` or ``data_elements``.
    kind: str
    member_ref: str


@dataclass(frozen=True)
class HeldStubComSpecs:
    """The com-specs of one kept port on a target that may become an interface stub, each naming a member.

    ``held`` is one :class:`HeldComSpec` per com-spec the derivation would write
    back for a listed member -- a server com-spec on a provide port, a client
    com-spec on a require port, a non-queued sender or receiver com-spec in its
    side's block, each the first naming its member. Every other com-spec of the
    port got its loss line while it was mapped.
    """

    port_name: str
    raw: SwcPortInfo
    direction: str
    held: tuple[HeldComSpec, ...]

    @property
    def interface_path(self) -> str:
        """The port's interface path (the source's ``interface_ref``, blank when absent)."""
        return self.raw.interface_ref or ""


class PortInitLabels(NamedTuple):
    """One held ``SHORT-LABEL`` line of a kept port's own init value."""

    port_name: str
    data_element_name: str
    key: str
    lost: int


class PortInitRadix(NamedTuple):
    """The radix line of one own init value a kept port carries from a hexadecimal spelling."""

    port_name: str
    data_element_name: str
    line: str


class PortInitEquivalent(NamedTuple):
    """One own init value a kept port carries by equivalence with its element's, at its report key."""

    port_name: str
    data_element_name: str
    key: str


@dataclass(frozen=True)
class GeneratedSource:
    """One source element the writer derives a name for: where it went in the model, and what the export called it.

    ``collection`` is ``events`` or one of the three access lists; ``runnable_name``
    and ``index`` address the member in the mapped component; ``path``, ``tag``
    and ``name`` are the source element's path, tag and ``SHORT-NAME``.
    """

    runnable_name: str
    collection: str
    index: int
    path: str
    tag: str
    name: str


def parent_package_name(path: str) -> str:
    """Immediate parent AR-PACKAGE short-name of `path`: the SWC's `group`."""
    parts = path.strip("/").split("/")
    return parts[-2] if len(parts) >= 2 else parts[-1]


def build_interface_path_index(
    model: ImportModel,
    interfaces: list[Interface],
    iface_skip_log: SkipLog,
    merges: MergeDecisions | None = None,
) -> dict[str, str]:
    """ARXML path -> emitted SWCraft interface name, for the declarations `map_interfaces()` kept.

    Interface names are never renamed, so the index decides only WHICH
    declaration of a bare name survived; a port referencing a skipped
    declaration's path finds no entry. The survivor is not necessarily the
    first: a declaration that failed for content reasons does not claim its
    name. `iface_skip_log` holds one entry per raw declaration that was not
    kept, so a mapped name's surviving path is the one without a skip entry.

    Two merge outcomes leave no skip entry and are excluded here: an
    ``EXCLUDED`` member (a ``--packages`` selection kept another member of a
    conflicting group) and a ``CONFLICT`` member whose bare name a declaration
    of the other interface kind holds. Indexing either would bind a port to an
    element the export never named; the port instead falls through to
    ``unresolved.keep``. An ``ALIAS`` is indexed, because its group's mapped
    element stands for every member path -- but only while the group's primary
    was really mapped: a primary skipped for its own reason (blank member name,
    unresolvable content, bare name held by the other interface kind) leaves its
    aliases without a skip entry, so the primary's skip counts for them too.

    **What this does not reach:** the index relies on ``map_interfaces``
    recording a member-level refusal (a blank operation or argument
    ``SHORT-NAME``) under the operation's path, never the interface's. Nothing
    enforces that convention; a member-level refusal recorded at its owner's
    path would wrongly drop the interface from this index.
    """
    mapped_names = {iface.name for iface in interfaces}
    skipped_paths = {entry.path for entry in iface_skip_log}
    path_to_name: dict[str, str] = {}
    declarations: list[SRInterfaceInfo | CSInterfaceInfo] = [*model.sr_interfaces, *model.cs_interfaces]
    for raw in declarations:
        if raw.name not in mapped_names:
            continue
        if raw.path in skipped_paths:
            continue
        if merges is not None:
            if merges.outcome(raw.path) in (EXCLUDED, CONFLICT):
                continue
            if merges.primary_path(raw.path) in skipped_paths:
                continue
        path_to_name[raw.path] = raw.name
    return path_to_name


PR_PORT_SKIP_REASON = (
    "PR (combined provide+require) ports have no SWCraft equivalent (Port.direction is provide|require only)"
)
"""The skip reason for a PR-PORT-PROTOTYPE, shared by component and composition boundary ports."""

_PORT_WITHOUT_INTERFACE_REASON = (
    "port has no interface reference -- the standard lets a file leave it out (partial information), "
    "and SWCraft cannot import a port without an interface"
)
"""The skip reason for a port with no ``PROVIDED-``/``REQUIRED-INTERFACE-TREF``.

The standardized schema does not enforce the reference's minimum multiplicity,
so a file may leave it out to carry partial information (``[TPS_GST_00057]``,
GST R4.2.2 :1331, R4.3.0 :1408) and breaks no rule by doing so; SWCraft's
``Port`` cannot be imported without one."""


@dataclass(frozen=True)
class PortOutcome:
    """What became of one raw port: the `Port` `map_ports` kept, or the skip entry it recorded (exactly one is set).

    Indexed by the port's full path, which is what an access's PORT-REF holds.
    The skip entry, not its text, is kept so an access or event chained behind
    the port inherits its category and rule (`SkipLog.record_chained`).
    """

    raw: SwcPortInfo
    port: Port | None
    skip: SkipEntry | None
    #: ``(element, radix line)`` per own init value carried from a hexadecimal spelling.
    init_radix: tuple[tuple[str, str], ...] = ()
    #: ``(element, report key)`` per own init value carried by equivalence with its element's.
    init_equivalents: tuple[tuple[str, str], ...] = ()
    #: ``(element, report key, labels lost)`` per held ``SHORT-LABEL`` line.
    init_labels: tuple[tuple[str, str, int], ...] = ()
    #: The com-specs held for the stub decision, on a target that may become an interface stub.
    held_com_specs: tuple[HeldComSpec, ...] = ()
    #: The port is kept on a path a late-resolving policy decides after the target is read.
    late: bool = False
    #: Each com-spec naming a member its own interface does not declare, under a late-resolving policy.
    member_misses: tuple[ComSpecMemberMiss, ...] = ()

    @property
    def skip_entry(self) -> SkipEntry:
        """The skip entry of a port `map_ports` did not keep: the root an access or event behind it chains to."""
        if self.skip is None:
            raise RuntimeError(f"port {self.raw.path} was neither mapped nor skipped")
        return self.skip


def interface_members(iface: Interface) -> tuple[str, frozenset[str]]:
    """(what a member is called, the member names) of one mapped interface.

    Kept apart from `validator._interface_members` on purpose: the coverage
    oracle attributes a field read to the module that makes it.
    """
    if iface.kind == "sender_receiver":
        return "data element", frozenset(el.name for el in iface.data_elements)
    client_server = required_instance(iface, CSInterface, "map_interfaces maps no interface stub")
    return "operation", frozenset(op.name for op in client_server.operations)


def undeclared_port_reason(
    subject: str, port_ref: str, *, swc_name: str, swc_path: str, declared: Collection[str]
) -> SkipReason:
    """Why an access or an operation event names no raw port of its component.

    The caller's port index holds every raw port, skipped ones included, so
    this is never a chain behind a skipped port. Decided by where the path points:

    - under the component's own path and declared nowhere in the file
      (*declared*, :func:`merge.declared_paths`):
      :attr:`SkipCategory.OUTSIDE_FILE`. ``SwComponentType.port`` is
      ``atpSplitable`` (SWCT R4.2.2 ``:2732``, R4.3.0 ``:3083``), so another
      file may declare the port;
    - under the component's own path and declared, but not a port the reader
      handed over (a port kind the reader does not recognize, which it
      reports itself): :attr:`SkipCategory.NOT_SUPPORTED`;
    - under any other path: :attr:`SkipCategory.EXPORT_DEFECT`,
      :attr:`StandardRule.INSTANCE_REF_CONTEXT` -- the port is the first
      context element of the reference and the base is the component, so the
      port must be a feature of the component (``[constr_2530]``).

    **What this does not reach:** the parent is compared as a path, exactly;
    a component whose own path the file spells differently from its ports'
    prefix is judged by the spelling.
    """
    parent, _, port_name = port_ref.rpartition("/")
    if parent == swc_path:
        if port_ref in declared:
            return not_supported(
                f"port '{port_name}' is declared on SWC '{swc_name}' but was not imported (reference {port_ref})"
            )
        return SkipReason(
            f"port '{port_name}' is not declared on SWC '{swc_name}' (reference {port_ref})",
            SkipCategory.OUTSIDE_FILE,
        )
    return SkipReason(
        f"{subject} names the port {port_ref}, which is not a port of its own SWC '{swc_name}' ({swc_path})",
        SkipCategory.EXPORT_DEFECT,
        StandardRule.INSTANCE_REF_CONTEXT,
    )


def _build_operation_event(member: str, skip_log: SkipLog, *, port: str) -> OperationEvent:
    """A held operation event, built from its listed *member* (*skip_log* unused)."""
    return OperationEvent(type="operation", port=port, operation=member)


def collapse_line(
    swc_name: str, runnable_name: str, list_name: str, accessed: str, name: str | None, first: str | None
) -> str:
    """The note of an access entry declared twice and collapsed into its first declaration.

    *accessed* is ``{port}.{member}`` for a port entry, the variable's name for a
    local variable access.
    """
    return (
        f"{swc_name}/{runnable_name}: {list_name} access {accessed} declared twice -- "
        f"'{name or '<unnamed>'}' collapsed into '{first or '<unnamed>'}'"
    )


def unlisted_member_clause(member_ref: str) -> str:
    """Why a record through a port on an external interface stub is not kept: its member is not listed.

    The stub lists the operations or data elements the source names exactly one
    segment under its path, of the kind its ``DEST`` admits; a reference that is
    deeper, of the other kind, through a mode-switch stub, one of two names alike
    but for case, or blank or no legal identifier names nothing the stub holds.
    """
    return (
        f"the stub does not list the member {member_ref} names (a stub lists the operations or data elements "
        "the source names directly under its path, of the kind its DEST admits), so there is no member to "
        "resolve it against"
    )


def chain_member_line(
    unresolved: UnresolvedReferences,
    skip_log: SkipLog,
    path: str,
    tag: str,
    reason: str,
    target: str,
    once_stubbed: Callable[[str], str],
) -> None:
    """The line of an event, a read, a write or a call through a port whose interface the export does not declare.

    Recorded through the policy, which decides the category, and re-worded by
    :meth:`UnresolvedReferences.name_written_stubs` once *target* is a stub the
    import writes.
    """
    unresolved.chain_skip(
        skip_log,
        path,
        tag,
        reason,
        target=target,
        layer=LAYER_INTERFACES,
        once_stubbed=once_stubbed,
        effect="abort",
    )


def map_operation_event(
    swc_name: str,
    event: SwcEventInfo,
    *,
    port_by_path: dict[str, PortOutcome],
    interface_by_name: dict[str, Interface],
    skip_log: SkipLog,
    swc_path: str,
    declared: Collection[str],
    unresolved: UnresolvedReferences | None = None,
    held: list[HeldStubRecord],
    misses: list[UnansweredRecord],
    runnable_name: str,
    position: int,
) -> OperationEvent | None:
    """Bind one ``OPERATION-INVOKED-EVENT`` by exact path, or skip it with a reason.

    The ``CONTEXT-P-PORT-REF`` must be the exact path of one of **this**
    component's ports that ``map_ports`` kept (else a chain-skip carrying the
    port's own reason). Then, in :func:`validator.check_operation_events`' order:

    1. the port's interface is one this export declares. A port kept with the
       export's own path in ``interface`` (an undeclared target) has no
       operation to bind here; if the path may become an external interface
       stub, the operation is staged and the event appended to *held* at
       *runnable_name*'s *position* for :func:`finish_stub_members`, otherwise
       its line is recorded at once;
    2. the interface is client/server;
    3. the port is a provide port: a call starts a runnable of the provider;
    4. ``TARGET-PROVIDED-OPERATION-REF`` is *the port's interface path* + the
       name of an operation it declares -- never a bare name, so a path into
       another interface dangles rather than matching by luck.

    An event failing any of them is skipped, so a green import is a green
    model -- except, under a late-resolving policy, an event whose reference sits under the
    port's own interface path and names an operation that interface does not declare: it
    goes to *misses* for the late pass. **Not decided here:** a pair bound twice and the
    signatures of one runnable's operations are ``check-model``'s; an event beside a timing
    or an init event on one runnable is **not judged** -- the RTE specification calls
    mixing unsupported in explanatory text, with no tagged requirement.
    """
    if not event.context_port_ref:
        skip_log.record(
            event.path,
            event.kind,
            "operation event has no CONTEXT-P-PORT-REF in an OPERATION-IREF",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    if not event.target_operation_ref:
        skip_log.record(
            event.path,
            event.kind,
            "operation event has no TARGET-PROVIDED-OPERATION-REF in an OPERATION-IREF",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    outcome = port_by_path.get(event.context_port_ref)
    if outcome is None:
        undeclared = undeclared_port_reason(
            "operation event", event.context_port_ref, swc_name=swc_name, swc_path=swc_path, declared=declared
        )
        skip_log.record_reason(event.path, event.kind, undeclared, effect="abort")
        return None
    raw_port, port = outcome.raw, outcome.port
    if port is None:
        root = outcome.skip_entry
        skip_log.record_chained(
            event.path,
            event.kind,
            f"chain-skipped: port '{raw_port.name}' was skipped ({root.reason})",
            root=root,
        )
        return None
    interface = required(port.interface, KEPT_INTERFACE)
    iface = interface_by_name.get(interface)
    if iface is None:
        reason = (
            f"operation event through port '{port.name}' whose interface reference {interface} "
            "is not declared by this export"
        )
        stub_port, target_ref = port, event.target_operation_ref  # the narrowed values the closure binds

        def once_stubbed(stub: str, port: Port = stub_port, ref: str = target_ref) -> str:
            return (
                f"operation event through port '{port.name}' whose interface is the external interface stub "
                f"'{stub}' ({port.interface}): {unlisted_member_clause(ref)}"
            )

        if unresolved is None:
            skip_log.record(event.path, event.kind, reason, category=SkipCategory.NOT_SUPPORTED, effect="abort")
        elif unresolved.cause(interface) == CAUSE_ABSENT:
            # Through a require port the event is dropped with its line, so it lists
            # nothing (as an access does); nor under a late-resolving policy.
            if port.direction == "provide" and not unresolved.resolves_late:
                unresolved.stage_member(
                    interface, kind="operations", member_ref=event.target_operation_ref, port_path=raw_port.path
                )
            held.append(
                HeldStubRecord(
                    target=interface,
                    kind="operations",
                    member_ref=event.target_operation_ref,
                    runnable_name=runnable_name,
                    collection="events",
                    position=position,
                    port=port.name,
                    build=partial(_build_operation_event, port=port.name),
                    path=event.path,
                    tag=event.kind,
                    name=event.name,
                    misdirected=(
                        f"operation event through {port.direction} port '{port.name}' "
                        "(an operation event needs a provide port)"
                        if port.direction != "provide"
                        else None
                    ),
                    reason=reason,
                    once_stubbed=once_stubbed,
                )
            )
        else:
            chain_member_line(unresolved, skip_log, event.path, event.kind, reason, interface, once_stubbed)
        return None
    if iface.kind != "client_server":
        skip_log.record(
            event.path,
            event.kind,
            f"operation event through port '{port.name}' whose interface '{iface.name}' is "
            f"{INTERFACE_KIND_PROSE[iface.kind]} (an operation event needs a "
            f"{INTERFACE_KIND_PROSE['client_server']} interface)",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    if port.direction != "provide":
        skip_log.record(
            event.path,
            event.kind,
            f"operation event through {port.direction} port '{port.name}' (an operation event needs a provide port)",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    _member_word, operations = interface_members(iface)
    parent_path, slash, operation = event.target_operation_ref.rpartition("/")
    if not slash:
        skip_log.record(
            event.path,
            event.kind,
            not_a_path_reason(event.target_operation_ref),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    if late_member_miss(parent_path, raw_port, operation, operations, unresolved):
        misses.append(
            UnansweredRecord(
                runnable_name=runnable_name,
                collection="events",
                port=port.name,
                member_name=operation,
                is_operation=True,
                interface_name=iface.name,
                tag=event.kind,
            )
        )
        return None
    if parent_path != raw_port.interface_ref or operation not in operations:
        skip_log.record(
            event.path,
            event.kind,
            not_a_member_reason(event.target_operation_ref, "an operation", port.name, iface.name),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    return OperationEvent(type="operation", port=port.name, operation=operation)


def server_queue_length_losses(
    swc_name: str, port_by_path: dict[str, PortOutcome]
) -> list[tuple[tuple[str, str], str]]:
    """One ``(report key, line)`` per kept provide port's ``SERVER-COM-SPEC`` whose queue length is not 1.

    The writer emits every server com-spec with ``QUEUE-LENGTH`` 1
    (``SERVER_QUEUE_LENGTH``), so another value is not carried; the line is not
    a skip entry, because nothing of the model is dropped. ``0x1`` reads as 1;
    an unreadable value is reported as spelled; an **absent** ``QUEUE-LENGTH`` is
    not, as the export states nothing to lose. Only kept ports are read, in
    declaration order. The key is ``({port}/PROVIDED-COM-SPECS, QUEUE-LENGTH)``.
    """
    lines: list[tuple[tuple[str, str], str]] = []
    for outcome in port_by_path.values():
        if outcome.port is None or outcome.port.direction != "provide":
            continue
        for spec in outcome.raw.server_com_specs:
            if spec.queue_length is None:
                continue
            if parse_int_literal(spec.queue_length, grammar="positive-integer") == SERVER_QUEUE_LENGTH:
                continue
            operation = (spec.operation_ref or "").rsplit("/", 1)[-1] or "<no OPERATION-REF>"
            lines.append(
                (
                    (f"{outcome.raw.path}/PROVIDED-COM-SPECS", "QUEUE-LENGTH"),
                    f"{swc_name}/{outcome.port.name}: SERVER-COM-SPEC of operation '{operation}' declares "
                    f"QUEUE-LENGTH {spec.queue_length}",
                )
            )
    return lines


#: ``(direction, queued) -> (the one com-spec tag, its fields)``. The fields per
#: kind are ``merge.COM_SPEC_KIND_FIELDS`` (Tables 4.60--4.62 and 4.67 of SWCT R4.2.2).
_COM_SPEC_KINDS: dict[tuple[str, bool], tuple[str, frozenset[str]]] = {
    (direction, queued): (tag, COM_SPEC_KIND_FIELDS[tag])
    for (direction, queued), tag in {
        ("provide", False): "NONQUEUED-SENDER-COM-SPEC",
        ("provide", True): "QUEUED-SENDER-COM-SPEC",
        ("require", False): "NONQUEUED-RECEIVER-COM-SPEC",
        ("require", True): "QUEUED-RECEIVER-COM-SPEC",
    }.items()
}


COM_SPEC_BLOCK = {"provide": "PROVIDED-COM-SPECS", "require": "REQUIRED-COM-SPECS"}

_NO_DATA_ELEMENT = "it names no data element of the port's interface"
_GATE_PAIR = "_gate_data_com_spec gives an element and its fields with no reason"
KEPT_INTERFACE = "map_ports sets every kept port's interface"


def _gate_data_com_spec(
    p: SwcPortInfo, direction: str, elements: Mapping[str, Any], spec: DataElementComSpecInfo, seen: set[str]
) -> tuple[Any, frozenset[str], None] | tuple[None, None, str]:
    """Whether *spec* is the com-spec the writer writes back for one data element of the port's interface.

    ``(element, fields, None)`` when it is -- the element and the attributes its
    com-spec kind has a place for -- and ``(None, None, why)`` when it is not.
    Both :func:`_port_com_specs` and :func:`_com_spec_losses` ask it, so a value
    is carried exactly when no whole-com-spec line is written for it:

    1. it sits in the block of the port's own side (no ``block_tag`` counts as there);
    2. its ``DATA-ELEMENT-REF`` names a data element of the port's interface, by
       the port's own interface path;
    3. it is the first such com-spec naming that element -- *seen* (one per
       port) takes the element before the kind is asked, so a later com-spec
       for an element whose first was of the wrong kind is a second one;
    4. its kind is the one the port's side and the element's policy call for
       (``[TPS_SWCT_01219]``: a queued element takes the ``QUEUED-*`` kinds).
    """
    if spec.block_tag and spec.block_tag != COM_SPEC_BLOCK[direction]:
        return None, None, f"it sits in {spec.block_tag} of a {direction} port"
    parent, slash, element_name = (spec.data_element_ref or "").rpartition("/")
    element = elements.get(element_name) if slash and parent == p.interface_ref else None
    if element is None:
        return None, None, _NO_DATA_ELEMENT
    if element_name in seen:
        return None, None, "it is a second com-spec for one data element"
    seen.add(element_name)
    expected, fields = _COM_SPEC_KINDS[(direction, element.sw_impl_policy == "queued")]
    if spec.com_spec_tag != expected:
        return None, None, f"the port's side and the element's policy call for {expected}"
    return element, fields, None


class _GatedInit(NamedTuple):
    """One gated com-spec's own init value: its block, its outcome, and whether the element states an ``init``.

    The element's ``init`` is what the output holds there when the port's own value is not carried.
    """

    block: str
    outcome: PortInitOutcome
    element_has_init: bool


def port_init_not_carried(element: str) -> str:
    """The opening of every line naming a port's own init value not carried, followed by the reason."""
    return f"INIT-VALUE of a com-spec not carried: data element '{element}': "


def port_init_radix(port_by_path: Mapping[str, PortOutcome]) -> list[PortInitRadix]:
    """Every radix line of an own init value a kept port carries from a hexadecimal spelling."""
    return [
        PortInitRadix(outcome.port.name, element, line)
        for outcome in port_by_path.values()
        if outcome.port is not None
        for element, line in outcome.init_radix
    ]


def port_init_equivalents(port_by_path: Mapping[str, PortOutcome]) -> list[PortInitEquivalent]:
    """Every own init value a kept port carries by equivalence, at its report key."""
    return [
        PortInitEquivalent(outcome.port.name, element, key)
        for outcome in port_by_path.values()
        if outcome.port is not None
        for element, key in outcome.init_equivalents
    ]


def port_init_labels(port_by_path: Mapping[str, PortOutcome]) -> list[PortInitLabels]:
    """Every held ``SHORT-LABEL`` line of a kept port's own init values."""
    return [
        PortInitLabels(outcome.port.name, element, key, lost)
        for outcome in port_by_path.values()
        if outcome.port is not None
        for element, key, lost in outcome.init_labels
    ]


def held_stub_com_specs(port_by_path: Mapping[str, PortOutcome]) -> list[HeldStubComSpecs]:
    """Every kept port's com-specs held for the stub decision."""
    return [
        HeldStubComSpecs(outcome.port.name, outcome.raw, outcome.port.direction, outcome.held_com_specs)
        for outcome in port_by_path.values()
        if outcome.port is not None and outcome.held_com_specs
    ]


def late_ports(port_by_path: Mapping[str, PortOutcome]) -> list[LatePort]:
    """Every kept port a late-resolving policy decides after the target is read, in port order."""
    return [
        LatePort(outcome.port.name, outcome.raw, outcome.port.direction)
        for outcome in port_by_path.values()
        if outcome.port is not None and outcome.late
    ]


def com_spec_member_misses(port_by_path: Mapping[str, PortOutcome]) -> list[ComSpecMemberMiss]:
    """Every com-spec a kept port names a member its interface lacks by, in port order."""
    return [miss for outcome in port_by_path.values() if outcome.port is not None for miss in outcome.member_misses]


def report_port_init_labels(
    labels: Sequence[PortInitLabels],
    skip_log: SkipLog,
    *,
    holds_no_init_value: Callable[[str, str], bool] | None = None,
) -> None:
    """Record each held ``SHORT-LABEL`` line (:func:`_port_init_lines`) whose com-spec still holds an ``INIT-VALUE``.

    The label difference the line accounts for exists only while the output
    holds an ``INIT-VALUE`` there; an unspent line would cover another
    com-spec's at the same key. *holds_no_init_value* ``(port, data element)``
    answers whether a pass after the mapping took it away (``import model``'s
    stub passes); ``import component`` mints no type stub and passes none.
    """
    for port, element, key, lost in labels:
        if holds_no_init_value is not None and holds_no_init_value(port, element):
            continue
        _report_port_init_labels(key, element, lost, skip_log)


def _gated_port_inits(
    p: SwcPortInfo,
    direction: str,
    interface: Interface | None,
    port_inits: Mapping[PortInitKey, PortInitOutcome],
) -> dict[str, _GatedInit]:
    """The port's own init values, by data element name, of the com-specs the writer writes back.

    One entry per com-spec that passes :func:`_gate_data_com_spec` and states
    a value of its own; a refused com-spec is reported whole by
    :func:`_com_spec_losses`, which covers its ``INIT-VALUE``. The
    ``registry.PortInitKey`` includes the com-spec's and the port's positions,
    so the outcome found is the gated com-spec's own -- never a later second
    com-spec's, never a same-named sibling port's. Empty unless the interface
    is a mapped sender/receiver one.
    """
    if not isinstance(interface, SRInterface):
        return {}
    elements = {element.name: element for element in interface.data_elements}
    gated: dict[str, _GatedInit] = {}
    seen: set[str] = set()
    for spec in p.data_element_com_specs:
        element, _fields, why = _gate_data_com_spec(p, direction, elements, spec, seen)
        if why is not None:
            continue
        element = required(element, _GATE_PAIR)
        block = spec.block_tag or COM_SPEC_BLOCK[direction]
        outcome = port_inits.get(PortInitKey.of_com_spec(p, spec, block))
        if outcome is not None:
            gated[element.name] = _GatedInit(block, outcome, element.init is not None)
    return gated


def _port_init_lines(
    p: SwcPortInfo,
    gated: Mapping[str, _GatedInit],
    skip_log: SkipLog,
    unresolved: UnresolvedReferences | None,
    *,
    carries_com_specs: bool,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str, int]]]:
    """The lines a kept port's own init values take, and the radix line of each carried one.

    Per gated com-spec, in the element order the port's com-specs give:

    * a value not carried (unreadable, not of the element's type's shape, or on
      a composition's port, which has no ``com_specs``) is one line at
      ``({port}/{block}, INIT-VALUE)``; where the element's type is an
      unresolved path kept as is, the line is chained to it and names the
      external type stub it becomes;
    * the labels the writer does not write back are **one** line at
      ``({port}/{block}, SHORT-LABEL)``, **held** rather than recorded
      (:func:`report_port_init_labels`), and only when both sides of the
      comparison hold labels: the value is written in place, or the writer
      answers with a reference to the element's constant, which exists only when
      the element states an ``init``. Two constant references compare no labels;
    * a value the output states at the port (carried, or the element's own in
      its place) takes the lines of how it is written other than the source
      wrote it (``PortInitOutcome.value_lines``), each at
      ``({port}/{block}, INIT-VALUE)``;
    * a value equal to the element's own is carried by equivalence: no entry.

    Returns:
        ``(element, radix line)`` per carried value read from a hexadecimal
        spelling; ``(element, key)`` per value carried by equivalence, which the
        orchestrator names once a later pass drops the element's ``init``; and
        ``(element, key, labels lost)`` per held label line.
    """
    radix: list[tuple[str, str]] = []
    equivalents: list[tuple[str, str]] = []
    labels: list[tuple[str, str, int]] = []
    for element_name, (block, outcome, element_has_init) in gated.items():
        key = f"{p.path}/{block}"
        carried = carries_com_specs and outcome.value is not None
        if outcome.reason is not None or (outcome.value is not None and not carries_com_specs):
            why = (
                outcome.reason
                if outcome.reason is not None
                else not_supported(
                    "a composition's port carries no com-spec values, and SWCraft writes the element's own"
                )
            )
            if outcome.type_target is not None and outcome.once_stubbed is not None and unresolved is not None:
                reword = outcome.once_stubbed
                unresolved.chain_skip(
                    skip_log,
                    key,
                    "INIT-VALUE",
                    port_init_not_carried(element_name) + why.text,
                    target=outcome.type_target,
                    layer=LAYER_IMPLEMENTATION_TYPES,
                    # mypy types no lambda with defaulted parameters in a context (a checker limitation)
                    once_stubbed=lambda stub, element_name=element_name, reword=reword: (  # type: ignore[misc]
                        port_init_not_carried(element_name) + reword(stub)
                    ),
                    effect=REPORT,
                )
            else:
                skip_log.record_reason(
                    key, "INIT-VALUE", why, prefix=port_init_not_carried(element_name), effect=REPORT
                )
        elif carried or (outcome.value is None and element_has_init):
            for line in outcome.value_lines:
                _report_port_value_line(key, line, skip_log)
        if outcome.lost_labels and (carried or (outcome.in_place and element_has_init)):
            labels.append((element_name, key, outcome.lost_labels))
        if carried and outcome.radix_spellings:
            radix.append((element_name, init_radix_line(key, outcome.radix_spellings)))
        if outcome.value is None and outcome.reason is None:
            equivalents.append((element_name, key))
    return radix, equivalents, labels


def _report_port_value_line(key: str, line: CarriedValueLine, skip_log: SkipLog) -> None:
    """One line about how a port's value the output states is written other than the source wrote it.

    Keyed ``({port}/{block}, INIT-VALUE)``: the numerical form under a collapsed
    twin, a unit that is not the type's, a number read through its type's table.
    Recorded with the line's own category and rule.
    """
    skip_log.record(key, "INIT-VALUE", line.text, category=line.category, rule=line.rule, effect=REPORT)


def _report_port_init_labels(key: str, element_name: str, lost: int, skip_log: SkipLog) -> None:
    """One descriptive line for the ``SHORT-LABEL``s a port's own init value loses.

    Keyed ``({port}/{block}, SHORT-LABEL)``: one line per com-spec, never one per
    label, because the comparison reports one difference per value. The count
    follows ``type_mapping.value_label_losses``.
    """
    skip_log.record(
        key,
        "SHORT-LABEL",
        f"{lost} SHORT-LABEL(s) of the port's own init value for data element '{element_name}' not carried: "
        "SWCraft labels an init value's record members with their field names and nothing else",
        category=SkipCategory.NOT_SUPPORTED,
        effect=REPORT,
    )


def _port_com_specs(
    p: SwcPortInfo,
    direction: str,
    interface: Interface | None,
    port_inits: Mapping[str, _GatedInit],
) -> tuple[list[PortComSpec], frozenset[str]]:
    """A component port's ``com_specs``: what its com-specs state other than silently.

    Returned with the names of the data elements an entry states a
    ``queue_length`` for. A value equal to the writer's silent value is carried
    by equivalence, so a SWCraft-written file yields no entry. An entry is
    carried only where the model can hold it:

    * the port's interface is a mapped sender/receiver interface (a port on an
      external interface stub takes its entries in :func:`finish_stub_members`);
    * the com-spec passes :func:`_gate_data_com_spec`;
    * only the attributes its kind has a place for;
    * the port's own init value from *port_inits*, when it differs from the
      element's own -- also on an entry that states nothing else.

    What is not carried here is reported by :func:`_com_spec_losses`.
    """
    if not isinstance(interface, SRInterface):
        return [], frozenset()
    elements = {element.name: element for element in interface.data_elements}
    entries: list[PortComSpec] = []
    with_queue_length: set[str] = set()
    seen: set[str] = set()
    for spec in p.data_element_com_specs:
        element, fields, why = _gate_data_com_spec(p, direction, elements, spec, seen)
        if why is not None:
            continue
        element_name, fields = required(element, _GATE_PAIR).name, required(fields, _GATE_PAIR)
        carried, _uncarried = com_spec_values(spec)
        stated: Mapping[str, Any] = {name: value for name, value in carried.items() if name in fields}
        if stated.get("queue_length") is not None:
            with_queue_length.add(element_name)
        own = port_inits.get(element_name)
        init = own.outcome.value if own is not None else None
        if stated or init is not None:
            entries.append(_com_spec_entry(element_name, stated, init))
    return entries, frozenset(with_queue_length)


def _com_spec_entry(element_name: str, stated: Mapping[str, Any], init: Any) -> PortComSpec:
    """One ``com_specs`` entry for *element_name*: the attributes *stated* and the port's own *init*.

    A stub port's entry never takes an ``init`` or a ``queue_length``
    (``rule:swc.port.com-spec-on-stub`` refuses both).
    """
    # Each attribute spelled as a keyword, never `**stated`: the coverage
    # oracle binds a construction site by its keywords.
    return PortComSpec(
        data_element=element_name,
        enable_update=stated.get("enable_update"),
        alive_timeout=stated.get("alive_timeout"),
        handle_never_received=stated.get("handle_never_received"),
        handle_timeout_type=stated.get("handle_timeout_type"),
        handle_out_of_range=stated.get("handle_out_of_range"),
        uses_end_to_end_protection=stated.get("uses_end_to_end_protection"),
        queue_length=stated.get("queue_length"),
        init=init,
    )


_WHOLE_COM_SPEC_REASON = (
    "{tag} not carried: {why} -- SWCraft writes one com-spec per data element or operation of the port's interface"
)


class ComSpecMemberMiss(NamedTuple):
    """A com-spec naming, under the port's interface path, a member that interface does not declare.

    On ``import component`` that is a reference the resulting project lacks: a
    dependency verdict, never a com-spec loss line. ``tag`` is the com-spec's
    kind, ``member_name`` the reference's last segment, ``is_operation`` whether
    it names an operation (else a data element) and ``interface_name`` the
    interface that answered the port.
    """

    port_name: str
    tag: str
    member_name: str
    is_operation: bool
    interface_name: str


def _member_miss(
    p: SwcPortInfo, interface: Interface, shape: ComSpecInfo, ref: str | None, *, is_operation: bool
) -> ComSpecMemberMiss | None:
    """The :class:`ComSpecMemberMiss` of a whole com-spec naming no member of the port's interface, or ``None``.

    ``None`` when the reference's parent is not the port's own interface path
    (a member is never bound by its leaf under another interface: the loss
    line stands) or the reference is no path at all.
    """
    parent, slash, member = (ref or "").rpartition("/")
    if not slash or parent != p.interface_ref or not member:
        return None
    return ComSpecMemberMiss(p.name, shape.tag, member, is_operation, interface.name)


def _com_spec_losses(
    p: SwcPortInfo,
    direction: str,
    interface: Interface | None,
    *,
    carries_com_specs: bool,
    misses: list[ComSpecMemberMiss] | None = None,
) -> list[tuple[str, str, str]]:
    """What a kept port's com-specs state that neither the model nor the writer carries.

    ``(block, tag, reason)`` per loss, keyed ``({port}/{block}, tag)`` by the
    caller. A whole com-spec the writer does not write back -- of a kind the
    port's side, interface kind and element policy do not call for, in the other
    block, a second for one member, naming no member of the port's interface, or
    a queued element's on a **composition's** require port -- is one loss tagged
    with its kind. In a com-spec the writer does write, every unread child and
    every value :func:`~swcraft_tools.arxml_import.merge.com_spec_values` cannot
    carry is one loss by its tag: a text no model value spells, a value with no
    place on the kind, and on a composition's port (*carries_com_specs* false)
    every value other than the writer's silent one. On a non-queued receiver a
    filter other than a bare ``ALWAYS`` is a ``FILTER`` loss; on any other kind
    every ``FILTER`` is an unread child.

    A port on an external interface stub is not asked here; it is held for the
    stub decision (:func:`_stub_com_spec_losses`).

    *misses* is ``import component``'s: a whole com-spec naming, under the
    port's interface path, a member that interface does not declare is appended
    to it as a :class:`ComSpecMemberMiss` instead of returned as a loss.
    """
    losses: list[tuple[str, str, str]] = []
    block = COM_SPEC_BLOCK[direction]
    seen: set[str] = set()
    elements = (
        {element.name: element for element in interface.data_elements} if isinstance(interface, SRInterface) else {}
    )
    operations = {op.name for op in interface.operations} if isinstance(interface, CSInterface) else set()
    for shape in p.com_spec_shapes:
        spec = shape.data
        server = shape.server

        def whole(why: str, shape=shape) -> None:
            losses.append((shape.block_tag, shape.tag, _WHOLE_COM_SPEC_REASON.format(tag=shape.tag, why=why)))

        if interface is None:
            # A port kept with an unresolved reference (a conflicting group's
            # interface, or one the package selection left out).
            whole("the port's interface is not imported, so there is no member to write it for")
            continue
        if shape.block_tag != block:
            # Asked here of every kind of com-spec, a server's and a client's
            # included; the gate below asks it again of a data com-spec because
            # `_port_com_specs` reaches the gate without this loop.
            whole(f"it sits in {shape.block_tag} of a {direction} port")
            continue
        if isinstance(interface, SRInterface) and spec is not None:
            element, fields, why = _gate_data_com_spec(p, direction, elements, spec, seen)
            if why is not None:
                if (
                    why == _NO_DATA_ELEMENT
                    and misses is not None
                    and (miss := _member_miss(p, interface, shape, spec.data_element_ref, is_operation=False))
                    is not None
                ):
                    misses.append(miss)
                else:
                    whole(why)
                continue
            element, fields = required(element, _GATE_PAIR), required(fields, _GATE_PAIR)
            queued = element.sw_impl_policy == "queued"
            if queued and direction == "require" and not carries_com_specs:
                whole(
                    "a composition's port carries no queue length, "
                    "and SWCraft writes no com-spec for a queued element on a composition's require port"
                )
                continue
            losses.extend(_data_com_spec_value_losses(shape, spec, block, fields, carries_com_specs=carries_com_specs))
        elif isinstance(interface, CSInterface) and shape.tag in ("SERVER-COM-SPEC", "CLIENT-COM-SPEC"):
            if shape.tag != ("SERVER-COM-SPEC" if direction == "provide" else "CLIENT-COM-SPEC"):
                whole(f"a {direction} port on a client-server interface takes the other kind")
                continue
            # The writer writes one com-spec per operation of the interface, so
            # one naming another operation, or a second for one, is lost.
            named = server if server is not None else shape.client
            if named is not None:
                parent, slash, operation = (named.operation_ref or "").rpartition("/")
                if not slash or parent != p.interface_ref or operation not in operations:
                    if (
                        misses is not None
                        and (miss := _member_miss(p, interface, shape, named.operation_ref, is_operation=True))
                        is not None
                    ):
                        misses.append(miss)
                    else:
                        whole("it names no operation of the port's interface")
                    continue
                if operation in seen:
                    whole("it is a second com-spec for one operation")
                    continue
                seen.add(operation)
            if not carries_com_specs:
                losses.extend(_composition_queue_length_losses(shape, block))
        else:
            whole(f"a {shape.tag} has no place on a port of this interface kind")
            continue
        losses.extend(_unread_com_spec_losses(shape, block))
    return losses


def unreadable_time_value_reason(tag: str, text: str, holder: str) -> str:
    """The line of a ``TIME-VALUE`` text no finite number reads, whose *holder* is not imported.

    Such a text is never read as 0. ``TIME-VALUE--SIMPLE`` restricts ``xsd:double`` with no
    facet of its own (XSD R4.3.0:88606-88608; R4.2.2:76972-76974), and
    ``arxml_reader.parse_float_literal`` reads that lexical space and refuses its
    non-finite three. ``MINIMUM-START-INTERVAL`` and a timing event's ``PERIOD`` are
    numbers the model always holds, so the runnable or the event is not carried. The
    text is quoted by ``repr``, so a no-break space or a full-width digit shows as itself.
    """
    return (
        f"{tag} {text!r} not carried: the text could not be read as a number (a TIME-VALUE is a finite "
        f"xsd:double), and SWCraft never reads it as 0 -- the {holder} is not imported"
    )


def _data_com_spec_value_losses(
    shape: ComSpecInfo,
    spec: DataElementComSpecInfo,
    block: str,
    fields: frozenset[str],
    *,
    carries_com_specs: bool,
) -> list[tuple[str, str, str]]:
    """The loss lines for the values of a sender or receiver com-spec the writer writes back.

    A value no model value spells, a carried value the com-spec's kind (*fields*)
    has no place for -- every non-silent value on a composition's port, which
    carries no ``com_specs`` (*carries_com_specs* false) -- a silent value the
    kind has no place for, and a ``FILTER`` other than a bare ``ALWAYS``.
    """
    losses: list[tuple[str, str, str]] = []
    carried, uncarried = com_spec_values(spec)
    for tag, text in uncarried.items():
        losses.append((block, tag, f"{tag} value '{text}' of a com-spec not carried: no model value spells it"))
    for name, _value in carried.items():
        if carries_com_specs and name in fields:
            continue
        tag = next((t for t, a in COM_SPEC_SILENT_VALUES.items() if a.field == name), "QUEUE-LENGTH")
        losses.append(
            (
                block,
                tag,
                f"{tag} of a com-spec not carried: "
                + (
                    f"a {shape.tag} has no place for it"
                    if carries_com_specs
                    else "a composition's port carries no com-spec values, and SWCraft writes the silent one"
                ),
            )
        )
    for tag in silent_values_without_place(spec):
        losses.append((block, tag, f"{tag} of a com-spec not carried: a {shape.tag} has no place for it"))
    if spec.com_spec_tag == FILTER_COM_SPEC_KIND and not spec.filter_is_silent:
        # Elsewhere a FILTER, silent or not, is one unread child.
        losses.append((block, "FILTER", "FILTER other than DATA-FILTER-TYPE ALWAYS alone not carried"))
    return losses


def _composition_queue_length_losses(shape: ComSpecInfo, block: str) -> list[tuple[str, str, str]]:
    """The loss line for a composition port's ``SERVER-COM-SPEC`` whose queue length is not 1.

    The writer emits ``QUEUE-LENGTH`` 1 and a composition's port carries no
    ``com_specs``, so another length is not carried. Read as :func:`server_queue_length_losses` reads a component's
    (``0x1`` is 1, an absent length states nothing to lose), but recorded as a
    loss line, since a composition keeps no list of its own.
    """
    server = shape.server
    if server is None or server.queue_length is None:
        return []
    if parse_int_literal(server.queue_length, grammar="positive-integer") == SERVER_QUEUE_LENGTH:
        return []
    return [
        (
            block,
            "QUEUE-LENGTH",
            f"QUEUE-LENGTH {server.queue_length} of a SERVER-COM-SPEC not carried: a composition's port carries no "
            f"com-spec values, and SWCraft writes QUEUE-LENGTH {SERVER_QUEUE_LENGTH}",
        )
    ]


def _unread_com_spec_losses(shape: ComSpecInfo, block: str) -> list[tuple[str, str, str]]:
    """One loss line per child and attribute of a written-back com-spec SWCraft does not read."""
    return [
        (block, tag, f"{tag} of a {shape.tag} is not read by SWCraft -- not imported or preserved")
        for tag in [*unread_com_spec_children(shape, read_com_spec_children(shape.tag)), *shape.attributes]
    ]


def _stub_com_spec_losses(
    shape: ComSpecInfo, direction: str, *, carries_com_specs: bool
) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    """What a com-spec naming a listed stub member states that the stub port's derivation does not write.

    The writer derives a ``SERVER-COM-SPEC`` with ``QUEUE-LENGTH`` 1 or a
    non-queued sender or receiver com-spec with the silent values a
    ``com_specs`` entry replaces. Returns the loss lines and the values a
    component port's entry carries. The ``INIT-VALUE`` is not carried: there is
    no type to carry it against, and its line is ``type_mapping``'s.
    """
    block = COM_SPEC_BLOCK[direction]
    losses: list[tuple[str, str, str]] = []
    stated: dict[str, Any] = {}
    spec = shape.data
    if spec is not None:
        fields = COM_SPEC_KIND_FIELDS[shape.tag]
        losses.extend(_data_com_spec_value_losses(shape, spec, block, fields, carries_com_specs=carries_com_specs))
        if carries_com_specs:
            carried, _uncarried = com_spec_values(spec)
            stated = {name: value for name, value in carried.items() if name in fields}
    elif not carries_com_specs:
        losses.extend(_composition_queue_length_losses(shape, block))
    losses.extend(_unread_com_spec_losses(shape, block))
    return losses, stated


def queued_element(interface: Interface, name: str) -> bool:
    """Whether *interface* is a sender/receiver interface whose first data element *name* is queued."""
    if not isinstance(interface, SRInterface):
        return False
    element = next((element for element in interface.data_elements if element.name == name), None)
    return element is not None and element.sw_impl_policy == "queued"


def _queued_elements_without_queue_length(
    p: SwcPortInfo, direction: str, interface: Interface | None, with_queue_length: frozenset[str]
) -> list[SkipReason]:
    """One reason per queued data element a kept component require port states no queue length for.

    The port is carried as stated, with no invented ``queue_length``, so the
    model is one ``check-model`` and ``generate arxml`` refuse
    (``rule:swc.port.queued-receiver-without-queue-length``). The category
    follows what the file states:

    * no ``QUEUED-RECEIVER-COM-SPEC`` of the port names the element: the file
      breaks ``[TPS_SWCT_01219]`` (a queued element's com-spec *shall be defined*);
    * one does, with no ``QUEUE-LENGTH`` the model carries: no quoted rule is
      broken, and the line is SWCraft's (``queueLength`` has multiplicity 1 in
      the class table and no default).

    *with_queue_length* holds the elements the port's carried entries state a
    ``queue_length`` for. What this does not reach: a composition's port (it
    carries no queue length at all) and a port on an external interface stub.
    """
    if direction != "require" or not isinstance(interface, SRInterface):
        return []
    defined = {
        (spec.data_element_ref or "").rpartition("/")[2]
        for spec in p.data_element_com_specs
        if spec.com_spec_tag == "QUEUED-RECEIVER-COM-SPEC"
        and (not spec.block_tag or spec.block_tag == COM_SPEC_BLOCK["require"])
        and (spec.data_element_ref or "").rpartition("/")[0] == p.interface_ref
    }
    reasons: list[SkipReason] = []
    seen: set[str] = set()
    for element in interface.data_elements:
        if element.name in seen:
            continue
        seen.add(element.name)
        if element.sw_impl_policy != "queued" or element.name in with_queue_length:
            continue
        tail = (
            "the port is imported with no queue_length for it, and the model is one check-model refuses "
            "(rule:swc.port.queued-receiver-without-queue-length)"
        )
        if element.name in defined:
            reasons.append(
                not_supported(
                    f"the QUEUED-RECEIVER-COM-SPEC of queued data element '{element.name}' states no QUEUE-LENGTH "
                    f"SWCraft carries, and neither AUTOSAR nor SWCraft defaults one; {tail}"
                )
            )
        else:
            reasons.append(
                SkipReason(
                    f"no QUEUED-RECEIVER-COM-SPEC is defined for queued data element '{element.name}'; {tail}",
                    SkipCategory.EXPORT_DEFECT,
                    StandardRule.QUEUED_COM_SPEC_DEFINED,
                )
            )
    return reasons


#: Why a runnable is written with its ``SHORT-NAME`` as its ``SYMBOL``, as a
#: sibling's line names it (:func:`settle_runnable_symbols`).
_ABSENT = "is absent"
_NOT_A_C_IDENTIFIER = "is not a C identifier"
_SHARED = "is shared with another runnable of the component"
_NOT_CARRIED = "was not carried"


def stated_symbol(runnable: SwcRunnableInfo, skip_log: SkipLog) -> tuple[str | None, str | None]:
    """``(text, None)`` when *runnable*'s ``SYMBOL`` is a C identifier **as written**; ``(None, why)`` otherwise.

    *why* is :data:`_ABSENT` or :data:`_NOT_A_C_IDENTIFIER`, the sentence a
    sibling's line quotes.

    The raw text is judged, never a normalized one, so surrounding whitespace,
    an inner space, a line break or an empty element fails it. Such a text is
    dropped with one behavioural line citing the ``CIdentifier`` primitive's
    class table (:attr:`StandardRule.C_IDENTIFIER_FORM`), and the runnable is
    written with its ``SHORT-NAME``. An absent element states nothing here.
    """
    if runnable.symbol is None:
        return None, _ABSENT
    if check_c_identifier(runnable.symbol) is not None:
        skip_log.record(
            runnable.path,
            "SYMBOL",
            f"SYMBOL {runnable.symbol!r} is not a C identifier as written (a CIdentifier follows the pattern "
            f"{C_IDENTIFIER_PATTERN}) -- not carried; the runnable is written with its SHORT-NAME as its SYMBOL",
            category=SkipCategory.EXPORT_DEFECT,
            rule=StandardRule.C_IDENTIFIER_FORM,
            effect=ABORT,
        )
        return None, _NOT_A_C_IDENTIFIER
    return runnable.symbol, None


def settle_runnable_symbols(
    swc_name: str,
    stated: Mapping[str, str | None],
    why_unstated: Mapping[str, str],
    paths: Mapping[str, str],
    skip_log: SkipLog,
) -> dict[str, str | None]:
    """Each kept runnable's ``symbol``, after no two runnables of the component are written with one.

    *stated* maps each kept runnable's name, in document order, to its ``SYMBOL``
    text when that is a C identifier, else ``None``; *why_unstated* says why
    for a runnable stating none. A runnable carries its stated text as its
    ``symbol`` only when it differs from its name (equal writes the same bytes).

    Compared exactly, C being case-sensitive:

    * **the stated texts.** Every runnable of a group of two or more stating one
      text loses its ``symbol`` and gets one line citing ``[constr_2025]``
      (:attr:`StandardRule.SYMBOL_UNIQUE`), with ``[SWS_Rte_03527]`` named as
      support -- also a member whose text equals its own name;
    * **then the written symbols** (``symbol`` when carried, else the name). A
      collision there arises only because SWCraft writes a sibling with its
      name, so the runnable carrying the symbol loses it with a line naming that
      sibling. Repeated until no group remains; it ends, because each pass
      unsets at least one symbol and the names are unique.

    One behavioural line per runnable, so no import reaches
    ``rule:name.duplicate-runnable-symbol``.
    """
    carried: dict[str, str | None] = {
        name: text if text is not None and text != name else None for name, text in stated.items()
    }
    why_by_name: dict[str, str] = dict(why_unstated)

    groups: dict[str, list[str]] = {}
    for name, text in stated.items():
        if text is not None:
            groups.setdefault(text, []).append(name)
    for text, names in groups.items():
        if len(names) < 2:
            continue
        for name in names:
            others = ", ".join(repr(other) for other in names if other != name)
            skip_log.record(
                paths[name],
                "SYMBOL",
                f"SYMBOL {text!r} is stated by {len(names)} runnables of component '{swc_name}' (also {others}): "
                "one component's runnables share its symbol, so their combinations are not unique, and an RTE "
                "supports no two runnables with one entry point ([SWS_Rte_03527]) -- each is written with its "
                "own SHORT-NAME as its SYMBOL",
                category=SkipCategory.EXPORT_DEFECT,
                rule=StandardRule.SYMBOL_UNIQUE,
                effect=ABORT,
            )
            carried[name] = None
            why_by_name[name] = _SHARED

    while True:
        written: dict[str, list[str]] = {}
        for name, symbol in carried.items():
            written.setdefault(symbol if symbol is not None else name, []).append(name)
        dropped = False
        for symbol, names in written.items():
            if len(names) < 2:
                continue
            # Exactly one runnable of the group is written with its name: names are
            # unique, and equal stated texts were dropped above.
            (sibling,) = [name for name in names if carried[name] is None]
            for name in names:
                if carried[name] is None:
                    continue
                skip_log.record(
                    paths[name],
                    "SYMBOL",
                    f"SYMBOL {symbol!r} not carried: runnable '{sibling}' of component '{swc_name}' is written "
                    f"with its SHORT-NAME as its SYMBOL because its own SYMBOL {why_by_name[sibling]}, and two "
                    "runnables of one component may not share an entry point -- the runnable is written with its "
                    "SHORT-NAME as its SYMBOL",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect=ABORT,
                )
                carried[name] = None
                why_by_name[name] = _NOT_CARRIED
                dropped = True
        if not dropped:
            return carried


def report_refused_combination(path: str, tag: str, reason: SkipReason, skip_log: SkipLog) -> None:
    """One line for a combination the import carries as stated and ``check-model`` refuses. Behavioural.

    Nothing is dropped, so the line is the only place the import says the model
    will not pass the gate: a queued element with no queue length, a
    composition's port on a service interface (``[constr_1174]``) and a by-value
    read of a queued element (``[constr_1277]``).
    """
    skip_log.record_reason(path, tag, reason, effect=ABORT)


def _report_com_spec_loss(port_path: str, block: str, tag: str, reason: str, skip_log: SkipLog) -> None:
    """One com-spec child, attribute, value or whole com-spec not carried.

    Descriptive: a com-spec value SWCraft does not carry is one line under *Not
    carried from the file*, never a refusal.
    """
    skip_log.record(f"{port_path}/{block}", tag, reason, category=SkipCategory.NOT_SUPPORTED, effect=REPORT)


#: The three reasons a stub port's com-spec is not carried, each true of its case alone: a shape or block
#: the derivation never writes; a second com-spec for one member; a member the stub does not list.
_F5_NOT_DERIVED = (
    "SWCraft writes a stub port's com-specs from the members the stub lists -- a server or a client com-spec, "
    "or a non-queued sender or receiver com-spec in the port's own block -- and this com-spec is none of them"
)
_F5_REPEATED = "another com-spec of the port names the same member, and SWCraft writes one com-spec per listed member"
_F5_UNLISTED = "it names no member the stub lists, and SWCraft writes a com-spec for a listed member only"


def _report_stub_com_spec(port_path: str, block: str, tag: str, why: str, skip_log: SkipLog) -> None:
    """A com-spec of a port imported on an external interface stub, not carried. Descriptive.

    The writer derives a stub port's com-specs from the members the stub lists;
    every other com-spec is one line here, with the clause *why* true of it.
    """
    skip_log.record(
        f"{port_path}/{block}",
        tag,
        f"{tag} of a port on an external interface stub not carried: {why}",
        category=SkipCategory.NOT_SUPPORTED,
        effect=REPORT,
    )


def _stub_member_of(shape: ComSpecInfo, direction: str) -> tuple[str, str] | None:
    """``(member kind, member reference)`` of a com-spec a stub port's derivation writes back, or ``None``.

    The derivation writes, per listed member, a ``SERVER-COM-SPEC`` or a
    ``CLIENT-COM-SPEC`` on a client-server stub's port and a non-queued sender
    or receiver com-spec on a sender-receiver one, in the port's own block. Any
    other com-spec -- queued, of the other side, mode-switch, in the other
    block, naming no member -- returns ``None``.
    """
    if shape.block_tag and shape.block_tag != COM_SPEC_BLOCK[direction]:
        return None
    if shape.tag == "SERVER-COM-SPEC" and direction == "provide":
        ref = shape.server.operation_ref if shape.server is not None else None
        return ("operations", ref) if ref else None
    if shape.tag == "CLIENT-COM-SPEC" and direction == "require":
        ref = shape.client.operation_ref if shape.client is not None else None
        return ("operations", ref) if ref else None
    if shape.tag == _COM_SPEC_KINDS[(direction, False)][0]:
        ref = shape.data.data_element_ref if shape.data is not None else None
        return ("data_elements", ref) if ref else None
    return None


def _hold_stub_com_specs(
    p: SwcPortInfo, direction: str, unresolved: UnresolvedReferences, skip_log: SkipLog
) -> tuple[HeldComSpec, ...]:
    """The com-specs of a kept port on a target that may become an interface stub, held for its decision.

    The first com-spec naming each member is held for :func:`finish_stub_members`
    and stages that member: server, client and sender/receiver com-specs all
    list a member (a ``ClientComSpec`` references its ``ClientServerOperation``
    in the role ``ServerComSpec.operation`` has, SWCT R4.2.2 Tables 4.73 / 4.74,
    R4.3.0 Tables 4.75 / 4.76). Every other com-spec gets its line at once.
    """
    held: list[HeldComSpec] = []
    seen: set[tuple[str, str]] = set()
    for shape in p.com_spec_shapes:
        member = _stub_member_of(shape, direction)
        if member is None or member in seen:
            why = _F5_NOT_DERIVED if member is None else _F5_REPEATED
            _report_stub_com_spec(p.path, shape.block_tag, shape.tag, why, skip_log)
            continue
        seen.add(member)
        kind, ref = member
        unresolved.stage_member(p.interface_ref or "", kind=kind, member_ref=ref, port_path=p.path)
        held.append(HeldComSpec(shape, kind, ref))
    return tuple(held)


def report_disabled_mode(event_path: str, reason: str, skip_log: SkipLog) -> None:
    """A disabled mode not carried, and not because its context port was skipped. Behavioural.

    One entry per ``DISABLED-MODE-IREF``: on an event kind other than a timing or
    a data-received event, through a context port that is no port of the
    component, or with a mode reference absent or not an absolute path.
    """
    skip_log.record(event_path, "DISABLED-MODE-IREFS", reason, category=SkipCategory.NOT_SUPPORTED, effect=ABORT)


def _disabled_mode(mode: DisabledModeInfo, port: Port) -> DisabledMode | None:
    """One disabled mode through a kept *port*, or ``None`` when a reference is absent or not an absolute path."""
    if mode.mode_group_ref is None or mode.mode_ref is None:
        return None
    try:
        return DisabledMode(port=port.name, mode_group_ref=mode.mode_group_ref, mode_ref=mode.mode_ref)
    except ValidationError:
        return None


def disabled_modes(event: SwcEventInfo, port_by_path: dict[str, PortOutcome], skip_log: SkipLog) -> list[DisabledMode]:
    """A timing or a data-received event's ``disabled_modes``, in source order.

    Each ``DISABLED-MODE-IREF`` binds its ``CONTEXT-PORT-REF`` by exact path to
    one of the component's own ports and carries the two mode references
    verbatim. A reference whose context port was **skipped** is one chained line
    at the event, and the event is kept without it. Whether a carried port is a
    ``require`` port on a mode-switch interface stub is ``check-model``'s
    ``rule:swc.timing_event.disabled-mode-port``.
    """
    modes: list[DisabledMode] = []
    for mode in event.disabled_modes:
        outcome = port_by_path.get(mode.context_port_ref or "")
        if outcome is None:
            report_disabled_mode(
                event.path,
                "disabled mode not carried: its CONTEXT-PORT-REF names no port of this component",
                skip_log,
            )
            continue
        if outcome.port is None:
            skip_log.record_chained(
                event.path,
                "DISABLED-MODE-IREFS",
                f"disabled mode not carried: its context port {mode.context_port_ref} was skipped "
                f"({outcome.skip_entry.reason})",
                root=outcome.skip_entry,
            )
            continue
        carried = _disabled_mode(mode, outcome.port)
        if carried is not None:
            modes.append(carried)
        else:
            report_disabled_mode(
                event.path,
                "disabled mode not carried: a mode reference is absent or not an absolute path",
                skip_log,
            )
    return modes


def _skip_port(
    skip_log: SkipLog,
    port_by_path: dict[str, PortOutcome],
    p: SwcPortInfo,
    reason: SkipReason,
    *,
    effect: SkipEffect,
) -> None:
    """Skip+report one raw port and index its entry under its path (first outcome wins).

    *effect* has no default: every caller states what the line does to ``import component``.
    """
    entry = skip_log.record_reason(p.path, p.kind, reason, effect=effect)
    port_by_path.setdefault(p.path, PortOutcome(raw=p, port=None, skip=entry))


#: The children of a port API option with both features off and nothing else: exactly one of each
#: (``group PORT-API-OPTION``, XSD 4.2.2 ``:50283-50339``, 4.3.0 ``:57101-57168``).
_PORT_API_OPTION_CHILDREN = frozenset({"PORT-REF", "ENABLE-TAKE-ADDRESS", "INDIRECT-API"})
#: ``AR:BOOLEAN``'s false spellings, matched on the raw text: its simple type is the pattern
#: ``0|1|true|false`` on ``xsd:string`` (``BOOLEAN--SIMPLE``, XSD 4.2.2 ``:69226-69230``, 4.3.0
#: ``:79700-79704``), so a padded ``false`` matches no alternative and is not false.
_FALSE_SPELLINGS = frozenset({"false", "0"})
_NO_PORT_API_OPTION = "SWCraft carries no port API option"
#: The one descriptive option line: an option with both features off requests nothing -- each feature
#: acts only when ``true`` (``PortAPIOption``'s table, SWCT R4.3.0 ``:29670-29674``, ``:29684-29690`` /
#: R4.2.2 ``:26732-26736``, ``:26746-26752``; ``[TPS_SWCT_01355]`` R4.3.0 ``:29737-29741`` / R4.2.2
#: ``:26789-26792``; ``[TPS_SWCT_01356]`` R4.3.0 ``:29749-29752`` / R4.2.2 ``:26800-26803``) -- so
#: refusing a component for it would be disproportionate; every other option's line stops ``import component``.
_PORT_API_OPTION_OFF = (
    "PORT-API-OPTION states both of its features off: it requests nothing, and SWCraft carries no port API option"
)


def _port_api_option_anchor(option: PortApiOptionInfo) -> str | None:
    """The key path of *option*'s line: its ``PORT-REF`` text as written, ``None`` when there is none to key by."""
    if option.port_ref is None or not option.port_ref.strip():
        return None
    return option.port_ref


def _port_api_option_reason(
    option: PortApiOptionInfo, *, root: SkipEntry | None, second: bool
) -> tuple[str | None, SkipEntry | None]:
    """Why *option* is not carried, with the port entry it is chained to.

    The first reason that holds is named; ``None`` for an option with both
    features off on a kept port, whose descriptive line the caller writes
    (:data:`_PORT_API_OPTION_OFF`) -- the effect is decided by that ``None``,
    never by comparing a reason's text. *root* is the named port's own skip
    entry at ``(PORT-REF text, DEST)``; *second* whether an earlier option of
    the behaviour states the same ``PORT-REF`` text and ``DEST``.
    """
    if option.stated_attributes:
        stated = ", ".join(f"{attribute} on {tag}" for tag, attribute in option.stated_attributes)
        return (
            f"PORT-API-OPTION states the attribute {stated}, beyond its PORT-REF's DEST -- {_NO_PORT_API_OPTION}",
            None,
        )
    anchor = _port_api_option_anchor(option)
    if anchor is None:
        return f"PORT-API-OPTION has no PORT-REF, or an empty one -- {_NO_PORT_API_OPTION}", None
    if not option.referenced_kinds:
        return f"PORT-API-OPTION's PORT-REF names no port of this component -- {_NO_PORT_API_OPTION}", None
    if option.port_ref_dest not in option.referenced_kinds:
        return (
            f"PORT-API-OPTION's PORT-REF DEST {option.port_ref_dest!r} is not the kind of the port it names -- "
            f"{_NO_PORT_API_OPTION}",
            None,
        )
    if root is not None:
        return (
            f"PORT-API-OPTION of port {anchor}, which is not imported ({root.reason}) -- {_NO_PORT_API_OPTION}",
            root,
        )
    if option.port_ref_dest not in MAPPED_PORT_KINDS:
        # A PR port or an unrecognized kind has its own entry (the mapper's skip, the
        # reader's); kept so a port without one is never taken for a kept one.
        return f"PORT-API-OPTION names a {option.port_ref_dest}, which is not imported -- {_NO_PORT_API_OPTION}", None
    if second:
        return f"a second PORT-API-OPTION for one port -- {_NO_PORT_API_OPTION}", None
    extra = sorted({tag for tag in option.child_tags if tag not in _PORT_API_OPTION_CHILDREN})
    if extra:
        return f"PORT-API-OPTION carries {', '.join(extra)}, which SWCraft does not read -- {_NO_PORT_API_OPTION}", None
    if option.child_tags.count("PORT-REF") != 1:
        return f"PORT-API-OPTION states PORT-REF more than once -- {_NO_PORT_API_OPTION}", None
    flags = (option.enable_take_address, option.indirect_api)
    if any(len(texts) != 1 or texts[0] not in _FALSE_SPELLINGS for texts in flags):
        return (
            "PORT-API-OPTION's ENABLE-TAKE-ADDRESS or INDIRECT-API is absent, stated twice or not false -- "
            f"{_NO_PORT_API_OPTION}",
            None,
        )
    return None, None


def report_port_api_options(raw: SwcInfo, skip_log: SkipLog, reader_log: SkipLog) -> None:
    """One line per source ``PORT-API-OPTION``, behavioural except the both-features-off one.

    SWCraft carries no port API option and the writer writes none. An option for which all five clauses below hold
    requests nothing and gets a **descriptive** line (:data:`_PORT_API_OPTION_OFF`); every other option's line is
    behavioural:

    1. its ``PORT-REF`` names a **kept** P- or R-port of this component, with that port's kind as ``DEST``: the port
       is among the component's ports at that path, and neither this mapper's log nor the reader's holds an entry at
       ``(port path, port kind)`` (an entry under another tag leaves the port kept);
    2. it is the first option of the behaviour stating that ``PORT-REF`` text and ``DEST``, in document order;
    3. its children are exactly one ``PORT-REF``, one ``ENABLE-TAKE-ADDRESS`` and one ``INDIRECT-API``;
    4. it states no attribute but the ``PORT-REF``'s ``DEST``;
    5. each flag's raw text is ``false`` or ``0`` (:data:`_FALSE_SPELLINGS`).

    A behavioural line is keyed ``(PORT-REF text as written, PORT-API-OPTION)``; an option with no or an empty
    ``PORT-REF`` keeps the behaviour's shared key ``({behaviour}/PORT-API-OPTIONS, PORT-API-OPTION)``. An option
    naming a port the import does not keep is chained to that port's own entry and inherits its category, rule and
    effect. A missing flag is never guessed. An option of a skipped component or of a behaviour after the first is
    covered by that component's or the reader's line.
    """
    options = raw.port_api_options
    if not options:
        return
    # Each named port's own entry, taken before this component's option lines are
    # recorded; an indexed lookup, never a scan of logs that grow with every component.
    roots: dict[tuple[str | None, str | None], SkipEntry] = {}
    for option in options:
        if option.port_ref is None or option.port_ref_dest is None:
            continue
        key = (option.port_ref, option.port_ref_dest)
        root = skip_log.first_tagged(*key) or reader_log.first_tagged(*key)
        if root is not None:
            roots.setdefault(key, root)
    seen: set[tuple[str | None, str | None]] = set()
    for option in options:
        option_key = (option.port_ref, option.port_ref_dest)
        second = option.port_ref is not None and option_key in seen
        if option.port_ref is not None:
            seen.add(option_key)
        reason, root = _port_api_option_reason(option, root=roots.get(option_key), second=second)
        path = _port_api_option_anchor(option) or option.container_path
        if reason is None:
            skip_log.record(
                path, "PORT-API-OPTION", _PORT_API_OPTION_OFF, category=SkipCategory.NOT_SUPPORTED, effect=REPORT
            )
        elif root is not None:
            skip_log.record_chained(path, "PORT-API-OPTION", reason, root=root)
        else:
            skip_log.record(path, "PORT-API-OPTION", reason, category=SkipCategory.NOT_SUPPORTED, effect=ABORT)


_PortDirection = Literal["provide", "require"]


@dataclass(frozen=True)
class _PortScope:
    """What every raw port of one owner shares while :func:`map_ports` maps it (its parameters of the same names)."""

    owner_name: str
    interface_path_to_name: dict[str, str]
    skip_log: SkipLog
    unresolved: UnresolvedReferences | None
    field_id: str
    variant_rich: bool
    declared: Collection[str]
    chain_logs: Sequence[SkipLog]
    interface_by_name: Mapping[str, Interface] | None
    report_interfaces: Mapping[str, Interface] | None
    port_inits: Mapping[PortInitKey, PortInitOutcome]


class _PortJudgement(NamedTuple):
    """What the judgement of one kept port's com-specs and initial values leaves for its `PortOutcome`."""

    init_radix: list[tuple[str, str]]
    init_equivalents: list[tuple[str, str]]
    init_labels: list[tuple[str, str, int]]
    held_com_specs: tuple[HeldComSpec, ...]
    member_misses: tuple[ComSpecMemberMiss, ...]
    late: bool


def _port_interface(scope: _PortScope, p: SwcPortInfo, port_by_path: dict[str, PortOutcome]) -> tuple[str, bool] | None:
    """The interface name a raw port is kept with and whether it is on a stub, or ``None`` with the port skipped.

    A port with no interface reference, or one whose interface was skipped or
    is declared nowhere without a policy to keep it, is skipped with its reason.
    With an unresolved policy a path declared nowhere is kept as the export's own
    path (the port is *on a stub* when no tier has answered it yet).
    """
    skip_log = scope.skip_log
    unresolved = scope.unresolved
    if not p.interface_ref:
        # SWCraft's limit, not the file's defect (`[TPS_GST_00057]`).
        _skip_port(skip_log, port_by_path, p, not_supported(_PORT_WITHOUT_INTERFACE_REASON), effect="abort")
        return None
    interface_name = scope.interface_path_to_name.get(p.interface_ref)
    on_stub = interface_name is None and unresolved is not None and unresolved.cause(p.interface_ref) == CAUSE_ABSENT
    if interface_name is None and unresolved is not None:
        # An unresolved reference is kept as the export's own path, never bound to
        # a same-named element. `field_id` is the only discriminator between the
        # component and the composition role: the referrer's `tag` is the port's kind.
        interface_name = unresolved.keep(
            p.interface_ref,
            UnresolvedReferrer(
                path=p.path,
                tag=p.kind,
                attribute=("PROVIDED-INTERFACE-TREF" if p.kind == "P-PORT-PROTOTYPE" else "REQUIRED-INTERFACE-TREF"),
                field=scope.field_id,
            ),
        )
    if interface_name is None:
        # With a policy, only a declared interface SWCraft skipped reaches here.
        if p.interface_ref in scope.declared:
            # The port inherits the interface's own entry.
            skip = chained_reason(
                f"unresolved reference {p.interface_ref} (interface was skipped)",
                cause_path=p.interface_ref,
                chain_logs=scope.chain_logs,
            )
        else:
            skip = SkipReason(
                f"unresolved reference {p.interface_ref} (the file declares no interface at that path)",
                SkipCategory.OUTSIDE_FILE,
            )
        _skip_port(skip_log, port_by_path, p, skip, effect="abort")
        return None
    return interface_name, on_stub


def _report_port_com_specs(
    scope: _PortScope, p: SwcPortInfo, direction: _PortDirection, interface: Interface | None
) -> tuple[ComSpecMemberMiss, ...]:
    """Report a kept port's com-spec losses on a mapped interface; return the members its interface lacks.

    Under a late-resolving policy (``import component``) a com-spec naming a
    member its own interface lacks is a dependency verdict, decided by the late
    pass, and comes back as a miss instead of a line.
    """
    unresolved = scope.unresolved
    interface_by_name = scope.interface_by_name
    misses: list[ComSpecMemberMiss] | None = (
        [] if unresolved is not None and unresolved.resolves_late and interface_by_name is not None else None
    )
    for block, tag, reason in _com_spec_losses(
        p,
        direction,
        interface,
        carries_com_specs=interface_by_name is not None,
        misses=misses,
    ):
        _report_com_spec_loss(p.path, block, tag, reason, scope.skip_log)
    return tuple(misses or ())


def _report_port_refusals(
    scope: _PortScope,
    p: SwcPortInfo,
    direction: _PortDirection,
    interface: Interface | None,
    with_queue_length: frozenset[str],
) -> None:
    """Report the combinations a kept port is imported with and ``check-model`` refuses.

    A component port's queued receiver element with no queue length, and a
    composition's port typed by a service interface.
    """
    skip_log = scope.skip_log
    if scope.interface_by_name is not None:
        for missing in _queued_elements_without_queue_length(p, direction, interface, with_queue_length):
            report_refused_combination(
                f"{p.path}/{COM_SPEC_BLOCK['require']}", "QUEUED-RECEIVER-COM-SPEC", missing, skip_log
            )
    elif isinstance(interface, (SRInterface, CSInterface)) and interface.is_service:
        report_refused_combination(
            p.path,
            "PROVIDED-INTERFACE-TREF" if p.kind == "P-PORT-PROTOTYPE" else "REQUIRED-INTERFACE-TREF",
            SkipReason(
                f"composition port '{p.name}' is typed by interface '{interface.name}', which states "
                "IS-SERVICE true; the port is imported as the file states it, and the model is one "
                "check-model refuses (rule:composition.port-service-interface)",
                SkipCategory.EXPORT_DEFECT,
                StandardRule.NO_SERVICE_PORT_ON_A_COMPOSITION,
            ),
            skip_log,
        )


def _judge_port(
    scope: _PortScope,
    p: SwcPortInfo,
    direction: _PortDirection,
    on_stub: bool,
    gated_inits: dict[str, _GatedInit],
    interface: Interface | None,
    with_queue_length: frozenset[str],
) -> _PortJudgement:
    """Judge a kept port's com-specs and initial values, in the order their lines are recorded.

    On a stub its com-specs are held for the stub decision, or left to the late
    pass; on a mapped interface the com-spec losses come first
    (:func:`_report_port_com_specs`), then the initial values' lines
    (:func:`_port_init_lines`), then the refused combinations
    (:func:`_report_port_refusals`).
    """
    init_radix: list[tuple[str, str]] = []
    init_equivalents: list[tuple[str, str]] = []
    init_labels: list[tuple[str, str, int]] = []
    held_com_specs: tuple[HeldComSpec, ...] = ()
    member_misses: tuple[ComSpecMemberMiss, ...] = ()
    late = False
    if on_stub:
        unresolved = required(scope.unresolved, "map_ports sets on_stub only with an unresolved policy")
        if unresolved.resolves_late:
            # Judged after the target is read, against its declared interface or as a stub's.
            late = True
        else:
            held_com_specs = _hold_stub_com_specs(p, direction, unresolved, scope.skip_log)
    else:
        member_misses = _report_port_com_specs(scope, p, direction, interface)
        init_radix, init_equivalents, init_labels = _port_init_lines(
            p, gated_inits, scope.skip_log, scope.unresolved, carries_com_specs=scope.interface_by_name is not None
        )
        _report_port_refusals(scope, p, direction, interface, with_queue_length)
    return _PortJudgement(init_radix, init_equivalents, init_labels, held_com_specs, member_misses, late)


def _map_port(
    scope: _PortScope,
    p: SwcPortInfo,
    ports: list[Port],
    seen_port_names: set[str],
    port_by_path: dict[str, PortOutcome],
) -> None:
    """One raw port of the owner: appended to *ports* when kept, indexed by path with what became of it either way."""
    skip_log = scope.skip_log
    if not p.name or not p.name.strip():
        # Port.name has no non-blank validator; a blank name would otherwise
        # crash ConnectorEndpoint validation in `map_compositions`.
        _skip_port(
            skip_log,
            port_by_path,
            p,
            blank_short_name("port has missing or blank SHORT-NAME"),
            effect="abort",
        )
        return
    if p.name in seen_port_names:
        # Two ports of one name under one owner share its name space:
        # `[constr_2508]`, unless the file is variant-rich.
        duplicate = duplicate_name_reason(
            f"duplicate port name '{p.name}' on SWC '{scope.owner_name}'", variant_rich=scope.variant_rich
        )
        _skip_port(skip_log, port_by_path, p, duplicate, effect="abort")
        return
    seen_port_names.add(p.name)
    if p.kind == "PR-PORT-PROTOTYPE":
        _skip_port(skip_log, port_by_path, p, not_supported(PR_PORT_SKIP_REASON), effect="abort")
        return
    resolved = _port_interface(scope, p, port_by_path)
    if resolved is None:
        return
    interface_name, on_stub = resolved
    direction: _PortDirection = "provide" if p.kind == "P-PORT-PROTOTYPE" else "require"
    interface_by_name = scope.interface_by_name
    known = interface_by_name if interface_by_name is not None else scope.report_interfaces or {}
    gated_inits = {} if on_stub else _gated_port_inits(p, direction, known.get(interface_name), scope.port_inits)
    com_specs, with_queue_length = (
        _port_com_specs(p, direction, interface_by_name.get(interface_name), gated_inits)
        if interface_by_name is not None
        else ([], frozenset())
    )
    port = Port(
        name=p.name,
        direction=direction,
        interface=interface_name,
        description=p.description,
        com_specs=com_specs,
    )
    ports.append(port)
    judged = _judge_port(scope, p, direction, on_stub, gated_inits, known.get(interface_name), with_queue_length)
    port_by_path.setdefault(
        p.path,
        PortOutcome(
            raw=p,
            port=port,
            skip=None,
            init_radix=tuple(judged.init_radix),
            init_equivalents=tuple(judged.init_equivalents),
            init_labels=tuple(judged.init_labels),
            held_com_specs=judged.held_com_specs,
            late=judged.late,
            member_misses=judged.member_misses,
        ),
    )
    # Committed here because the referrer is the port: both callers take every
    # refusal of the owning declaration before calling, so this port is kept.
    if scope.unresolved is not None:
        scope.unresolved.commit(p.path)


def map_ports(
    owner_name: str,
    raw_ports: list[SwcPortInfo],
    interface_path_to_name: dict[str, str],
    skip_log: SkipLog,
    unresolved: UnresolvedReferences | None = None,
    field_id: str = "swc.port.interface",
    *,
    variant_rich: bool,
    declared: Collection[str],
    chain_logs: Sequence[SkipLog],
    interface_by_name: Mapping[str, Interface] | None = None,
    report_interfaces: Mapping[str, Interface] | None = None,
    port_inits: Mapping[PortInitKey, PortInitOutcome],
) -> tuple[list[Port], dict[str, PortOutcome]]:
    """The `Port`s of one owner, in declaration order, and what became of every raw port by full path.

    The owner is a leaf APPLICATION-SW-COMPONENT-TYPE (`map_swcs`) or a
    composition's own boundary (`connectors.map_compositions`); the rules are
    identical for both. A PR-PORT-PROTOTYPE, a blank name, a duplicate name and
    an absent or unresolvable interface ref each skip that one port with a
    reason. Every raw port is indexed with its outcome, first outcome winning,
    because an access resolves a PORT-REF by exact path; a port claims its name
    **before** its kind and interface are decided, so a skipped port claims it
    too (a blank-named PR port is reported for its blank name).

    *chain_logs* are searched, in order, for a skipped interface's own entry,
    so a port chained behind it inherits its category and rule.

    *interface_by_name* is given by ``map_swcs`` alone (a composition's boundary
    port has no ``com_specs``); a composition's com-specs are judged against
    *report_interfaces*.

    *port_inits* is required, never defaulted: a forgotten mapping would drop
    every port's own init value with no line. What is not carried (always, on a
    composition's port) is a line; a skipped port's skip entry covers it.
    """
    scope = _PortScope(
        owner_name=owner_name,
        interface_path_to_name=interface_path_to_name,
        skip_log=skip_log,
        unresolved=unresolved,
        field_id=field_id,
        variant_rich=variant_rich,
        declared=declared,
        chain_logs=chain_logs,
        interface_by_name=interface_by_name,
        report_interfaces=report_interfaces,
        port_inits=port_inits,
    )
    ports: list[Port] = []
    seen_port_names: set[str] = set()
    port_by_path: dict[str, PortOutcome] = {}
    for p in raw_ports:
        _map_port(scope, p, ports, seen_port_names, port_by_path)
    return ports, port_by_path


def data_type_mappings(
    raw: SwcInfo,
    name_by_path: dict[str, str],
    unresolved: UnresolvedReferences | None,
) -> list[str]:
    """``swc.data_type_mappings`` for one component, in the export's own order.

    A reference that does not resolve is written as the export spelled it,
    under the role ``swc.data_type_mappings``, and ``check-model`` reports it
    under ``rule:swc.unknown-mapping-set``. A reference the export declares and
    this import skipped for its own reason (a blank ``SHORT-NAME``, a duplicate
    name) is **dropped**, and the set's own skip entry says why; a member of a
    **conflicting group** is kept as its path.

    The list and its staging are de-duplicated, because ``SWComponent`` refuses
    a set named twice and two refs onto one merged group resolve to one name.
    The referrer's tag is exactly ``APPLICATION-SW-COMPONENT-TYPE``, the one tag
    a ``SwcInfo`` is read from.
    """
    names: list[str] = []
    for ref in raw.data_type_mapping_refs:
        name = name_by_path.get(ref)
        staged = False
        if name is None and unresolved is not None:
            name = unresolved.answer(ref, role="swc.data_type_mappings")
            staged = name is not None
        if name is None or name in names:
            continue
        names.append(name)
        if staged:
            unresolved = required(unresolved, "data_type_mappings stages a name only from an unresolved policy")
            unresolved.keep(
                ref,
                UnresolvedReferrer(
                    path=raw.path,
                    tag="APPLICATION-SW-COMPONENT-TYPE",
                    attribute="DATA-TYPE-MAPPING-REF",
                    field="swc.data_type_mappings",
                ),
            )
    return names


# ---------------------------------------------------------------------------
# Inter-runnable variables and the runnables' local accesses
# ---------------------------------------------------------------------------

#: A variable's ``access``, by the wrapper the export put it in (``[TPS_SWCT_01052]``).
_VARIABLE_ACCESS_BY_ROLE: dict[str, Literal["explicit", "implicit"]] = {
    "EXPLICIT-INTER-RUNNABLE-VARIABLES": "explicit",
    "IMPLICIT-INTER-RUNNABLE-VARIABLES": "implicit",
}
#: The list a local access lands in, by its container (``[TPS_SWCT_01053]``).
_LOCAL_ACCESS_LIST: dict[str, str] = {
    "READ-LOCAL-VARIABLES": "reads_variables",
    "WRITTEN-LOCAL-VARIABLES": "writes_variables",
}


class MappedVariables(NamedTuple):
    """What :func:`map_variables` decided for one component's variables."""

    variables: list[Variable]
    #: The kept variable's name, by its ARXML path -- what a ``LOCAL-VARIABLE-REF`` names.
    name_by_path: dict[str, str]
    #: The entry of every variable skipped, by its path: what an access of it is chained to.
    skipped: dict[str, tuple[str, SkipEntry]]
    #: The radix lines of the kept variables' values, by the variable's name.
    radix_lines: list[tuple[str, str]]


def _variable_init_through_an_unresolved_type(target: str) -> str:
    """The line of a variable's ``INIT-VALUE`` dropped because its unresolved type was kept as a path."""
    return (
        f"INIT-VALUE not carried: the variable's type {target} is declared nowhere in this file, so no value can be "
        "shaped against it"
    )


def _variable_init_on_a_stub(stub: str, target: str) -> str:
    """The same line once *target* is the external type stub *stub* (``rule:interface.init-on-external-type``)."""
    return (
        f"INIT-VALUE not carried: the variable is typed by the external type stub '{stub}' ({target}), whose content "
        "the export does not declare, so no value can be shaped against it"
    )


def map_variables(
    raw: SwcInfo,
    variable_types: Mapping[str, VariableTypeOutcome],
    unresolved: UnresolvedReferences | None,
    skip_log: SkipLog,
) -> MappedVariables:
    """Every inter-runnable variable of *raw*, kept or skipped with a reason.

    The type and the value are ``type_mapping.map_variable_types``' decision
    (*variable_types*, keyed by the variable's path); this records every line of
    a kept variable -- a refused ``init`` (the variable kept without it), the
    value's descriptive lines. A type the file declares nowhere whose ``DEST`` is
    no application kind is kept as its path under the role
    ``swc.variable.implementation_type``; the ``init`` such a variable states
    cannot be shaped and is one line, re-worded if the path becomes a stub.
    Every other skip is one entry at the variable, which its accesses chain to.

    What this does not reach: two variables of one name are the index's line
    (the reader keeps the first); two whose names differ only in case are both
    kept, for ``check-model``'s ``name.name-space-collision``.
    """
    variables: list[Variable] = []
    name_by_path: dict[str, str] = {}
    skipped: dict[str, tuple[str, SkipEntry]] = {}
    radix_lines: list[tuple[str, str]] = []
    for raw_variable in raw.variables:
        if not raw_variable.name or not raw_variable.name.strip():
            entry = skip_log.record_reason(
                raw_variable.path, "VARIABLE-DATA-PROTOTYPE", blank_short_name(), effect="abort"
            )
            skipped[raw_variable.path] = (raw_variable.name, entry)
            continue
        outcome = variable_types.get(raw_variable.path)
        if outcome is None:
            # Unreachable through both importers; a hand-built call that decides
            # none gets a skip rather than a guess or a KeyError.
            outcome = VariableTypeOutcome(skip=not_supported("its type was not decided by this import"))
        type_name = outcome.type_name
        init = outcome.init
        if type_name is None:
            kept = None
            if outcome.keep_target is not None and unresolved is not None:
                kept = unresolved.keep(
                    outcome.keep_target,
                    UnresolvedReferrer(
                        path=raw.path,
                        tag="APPLICATION-SW-COMPONENT-TYPE",
                        attribute="TYPE-TREF",
                        field="swc.variable.implementation_type",
                        member=raw_variable.name,
                    ),
                    occurrence=raw_variable.path,
                )
                # Any stated INIT-VALUE, an unreadable one included: none can be
                # shaped against a path, and none goes without a line.
                if kept is not None and (
                    raw_variable.init_constant_ref is not None
                    or raw_variable.init_value is not None
                    or raw_variable.init_unreadable is not None
                ):
                    unresolved.chain_skip(
                        skip_log,
                        raw_variable.path,
                        "INIT-VALUE",
                        _variable_init_through_an_unresolved_type(kept),
                        target=kept,
                        layer=LAYER_IMPLEMENTATION_TYPES,
                        once_stubbed=partial(_variable_init_on_a_stub, target=kept),
                        effect="abort",
                    )
            if kept is None:
                reason = outcome.skip or not_supported("its type was not decided by this import")
                entry = skip_log.record_reason(raw_variable.path, "VARIABLE-DATA-PROTOTYPE", reason, effect="abort")
                skipped[raw_variable.path] = (raw_variable.name, entry)
                continue
            type_name = kept
        elif outcome.init_reason is not None:
            skip_log.record_reason(raw_variable.path, "INIT-VALUE", outcome.init_reason, effect="abort")
        else:
            for path, tag, line in outcome.lines:
                skip_log.record(path, tag, line.text, category=line.category, rule=line.rule, effect=REPORT)
            if outcome.radix_spellings:
                radix_lines.append((raw_variable.name, init_radix_line(raw_variable.path, outcome.radix_spellings)))
        application = outcome.type_name is not None and outcome.application
        variables.append(
            Variable(
                name=raw_variable.name,
                access=_VARIABLE_ACCESS_BY_ROLE[raw_variable.role],
                application_type=type_name if application else None,
                implementation_type=None if application else type_name,
                init=init,
                description=raw_variable.description,
                sw_calibration_access=outcome.sw_calibration_access,
            )
        )
        name_by_path[raw_variable.path] = raw_variable.name
    return MappedVariables(variables, name_by_path, skipped, radix_lines)


def _local_access_target(access: SwcLocalAccessInfo, behaviour_paths: Collection[str]) -> str | SkipReason:
    """The variable path a local access is bound to, or why it cannot be carried.

    ``AUTOSAR-VARIABLE-REF`` is a sequence, so every form it states is judged:
    a form other than ``LOCAL-VARIABLE-REF`` -- an instance reference, an access
    into a sub-element -- breaks ``[TPS_SWCT_01521]`` whether or not a local
    reference stands beside it; a local reference naming no variable of the
    access's own behaviour breaks ``[constr_2026]``.
    """
    others = [form for form in access.reference_forms if form != "LOCAL-VARIABLE-REF"]
    if others:
        return SkipReason(
            f"a local variable access states {', '.join(others)}, and only LOCAL-VARIABLE-REF references an "
            "inter-runnable variable",
            SkipCategory.EXPORT_DEFECT,
            StandardRule.LOCAL_VARIABLE_REFERENCE,
        )
    if not access.variable_ref:
        return not_supported("the local variable access states no LOCAL-VARIABLE-REF")
    if access.variable_ref not in behaviour_paths:
        return SkipReason(
            f"its LOCAL-VARIABLE-REF {access.variable_ref} names no inter-runnable variable of its own behaviour",
            SkipCategory.EXPORT_DEFECT,
            StandardRule.LOCAL_VARIABLE_OWNER,
        )
    return access.variable_ref


def map_local_accesses(
    swc_name: str,
    runnable_name: str,
    accesses: Sequence[SwcLocalAccessInfo],
    mapped: MappedVariables,
    behaviour_paths: Collection[str],
    skip_log: SkipLog,
) -> tuple[list[VariableReadRef], list[VariableWriteRef], list[str], list[GeneratedSource]]:
    """One runnable's local accesses as its ``reads_variables`` / ``writes_variables``.

    Bound by exact path to a variable of the same behaviour (*behaviour_paths*
    holds every variable path read there, kept or not). An access of a skipped
    variable is chained to the variable's entry. A second access to one variable
    in one container collapses into the first, one lossy-transformation line and
    never a skip, since an entry's identity is its variable. Each kept entry has
    a :class:`GeneratedSource`.
    """
    lists: dict[str, list[str]] = {"reads_variables": [], "writes_variables": []}
    first_name: dict[tuple[str, str], str] = {}
    collapses: list[str] = []
    sources: list[GeneratedSource] = []
    for access in accesses:
        target = _local_access_target(access, behaviour_paths)
        if isinstance(target, SkipReason):
            skip_log.record_reason(access.path, access.tag, target, effect="abort")
            continue
        if target in mapped.skipped:
            variable_name, root = mapped.skipped[target]
            skip_log.record_chained(
                access.path,
                access.tag,
                f"chain-skipped: inter-runnable variable '{variable_name}' was skipped ({root.reason})",
                root=root,
            )
            continue
        variable = mapped.name_by_path[target]
        list_name = _LOCAL_ACCESS_LIST[access.container]
        key = (list_name, variable)
        if key in first_name:
            collapses.append(collapse_line(swc_name, runnable_name, list_name, variable, access.name, first_name[key]))
            continue
        first_name[key] = access.name
        sources.append(
            GeneratedSource(runnable_name, list_name, len(lists[list_name]), access.path, access.tag, access.name)
        )
        lists[list_name].append(variable)
    return (
        [VariableReadRef(variable=variable) for variable in lists["reads_variables"]],
        [VariableWriteRef(variable=variable) for variable in lists["writes_variables"]],
        collapses,
        sources,
    )


# ---------------------------------------------------------------------------
# Exclusive areas and the runnables' references to them
# ---------------------------------------------------------------------------

#: The list a reference lands in, by its wrapper (``ExecutableEntity``'s two references).
_AREA_REF_LIST: dict[str, Literal["can_enter", "runs_inside"]] = {
    "CAN-ENTER-EXCLUSIVE-AREA-REFS": "can_enter",
    "RUNS-INSIDE-EXCLUSIVE-AREA-REFS": "runs_inside",
}


class MappedAreas(NamedTuple):
    """What :func:`map_exclusive_areas` decided for one component's areas."""

    areas: list[ExclusiveArea]
    #: The kept area's name, by its ARXML path -- what a reference names.
    name_by_path: dict[str, str]
    #: The entry of every area skipped, by its path: what a reference to it is chained to.
    skipped: dict[str, tuple[str, SkipEntry]]
    #: Every area path the reader read in the behaviour, kept or skipped.
    area_paths: frozenset[str]


def map_exclusive_areas(raw: SwcInfo, skip_log: SkipLog) -> MappedAreas:
    """Every exclusive area of *raw*, kept with its description or skipped with a reason.

    An area is its name and its ``DESC`` (``[TPS_SWCT_01031]``; no attribute of
    its own outside variability), so the one skip is a missing or blank
    ``SHORT-NAME``, which each reference to it is chained to.

    What this does not reach: two areas of one name are the index's line (the
    reader keeps the first); an area named like a runnable or an event of its
    behaviour claims the path before them; an area named like an earlier named
    child of another kind (a ``CONSTANT-MEMORYS`` item, say) is the index's
    dropped declaration and is not read, so a reference to it is skipped
    unchained; names differing only in case are both kept, for ``check-model``'s
    ``name.name-space-collision``.
    """
    areas: list[ExclusiveArea] = []
    name_by_path: dict[str, str] = {}
    skipped: dict[str, tuple[str, SkipEntry]] = {}
    for raw_area in raw.exclusive_areas:
        if not raw_area.name.strip():
            entry = skip_log.record_reason(raw_area.path, "EXCLUSIVE-AREA", blank_short_name(), effect="abort")
            skipped[raw_area.path] = (raw_area.name, entry)
            continue
        areas.append(ExclusiveArea(name=raw_area.name, description=raw_area.description))
        name_by_path[raw_area.path] = raw_area.name
    return MappedAreas(areas, name_by_path, skipped, frozenset(area.path for area in raw.exclusive_areas))


def _area_ref_skip(ref: SwcAreaRefInfo, area_paths: Collection[str]) -> SkipReason | None:
    """Why an exclusive area reference cannot be carried, or ``None``.

    It must name an ``EXCLUSIVE-AREA`` (the one ``DEST`` both schemas admit) of the runnable's own
    behaviour -- ``InternalBehavior``'s table says the area *is local to the component*; no numbered
    rule of either release states that, so each refusal is SWCraft's, never cited as the file's
    defect. ``ref.path`` is the runnable's own path, so its parent is the behaviour's path.
    """
    if not ref.ref:
        return not_supported(f"{ref.tag} names no exclusive area")
    if ref.dest != "EXCLUSIVE-AREA":
        stated = "no DEST" if ref.dest is None else f"DEST {ref.dest!r}"
        return not_supported(f"{ref.tag} {ref.ref} states {stated}, and only EXCLUSIVE-AREA names an exclusive area")
    behaviour_path = ref.path.rpartition("/")[0]
    if ref.ref.rpartition("/")[0] != behaviour_path:
        return not_supported(
            f"{ref.tag} {ref.ref} names an exclusive area outside its own behaviour -- an exclusive area is local "
            "to its component"
        )
    if ref.ref not in area_paths:
        return not_supported(f"{ref.tag} {ref.ref} names no exclusive area its behaviour declares")
    return None


def _area_collapse_line(swc_name: str, runnable_name: str, list_name: str, area: str, tag: str) -> str:
    """The note of a reference repeated in one wrapper, collapsed into the first."""
    return (
        f"{swc_name}/{runnable_name}: {list_name} entry {area} declared twice -- the repeated {tag} collapsed "
        "into the first"
    )


def map_area_refs(
    swc_name: str,
    runnable_name: str,
    refs: Sequence[SwcAreaRefInfo],
    mapped: MappedAreas,
    skip_log: SkipLog,
) -> tuple[list[CanEnterArea], list[RunsInsideArea], list[str]]:
    """One runnable's exclusive area references as its ``can_enter`` / ``runs_inside``.

    Bound by exact path to an area of the same behaviour. A reference to a
    skipped area is chained to the area's entry; a second reference to one area
    in one wrapper collapses into the first (one lossy-transformation line, never
    a skip). One area named in both roles is imported as it is: no rule of
    either release forbids it.
    """
    lists: dict[str, list[str]] = {"can_enter": [], "runs_inside": []}
    collapses: list[str] = []
    for ref in refs:
        reason = _area_ref_skip(ref, mapped.area_paths)
        if reason is not None:
            skip_log.record_reason(ref.path, ref.tag, reason, effect="abort")
            continue
        if ref.ref in mapped.skipped:
            area_name, root = mapped.skipped[ref.ref]
            skip_log.record_chained(
                ref.path,
                ref.tag,
                f"chain-skipped: exclusive area '{area_name}' was skipped ({root.reason})",
                root=root,
            )
            continue
        area = mapped.name_by_path[ref.ref]
        list_name = _AREA_REF_LIST[ref.role]
        if area in lists[list_name]:
            collapses.append(_area_collapse_line(swc_name, runnable_name, list_name, area, ref.tag))
            continue
        lists[list_name].append(area)
    return (
        [CanEnterArea(area=area) for area in lists["can_enter"]],
        [RunsInsideArea(area=area) for area in lists["runs_inside"]],
        collapses,
    )


# ---------------------------------------------------------------------------
# The runnables' execution-time budgets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EligibleBudget:
    """An execution-time requirement no skip rule refused.

    ``runnable_path`` is the ``EXECUTABLE-REF`` it times, ``runnable_name`` that
    runnable's ``SHORT-NAME``, and ``wcet_type`` the value the runnable's model field takes.
    """

    runnable_path: str
    runnable_name: str
    budget: Budget
    wcet_type: Literal["gross", "net"]
    timing: SwcTimingInfo
    constraint: TimingConstraintInfo


#: One constraint's planned outcome: a skip's reason, an eligible budget, or ``None`` for a guarantee.
_Decision = SkipReason | EligibleBudget | None


@dataclass(frozen=True)
class _PlannedTiming:
    """One ``SWC-TIMING``'s plan: the component path it targets (or why none) and each constraint's outcome."""

    timing: SwcTimingInfo
    target: str | SkipReason
    decisions: tuple[tuple[TimingConstraintInfo, _Decision], ...]


@dataclass(frozen=True)
class BudgetPlan:
    """What :func:`map_swcs` gives the runnables it builds, decided before it builds any.

    ``carried`` maps a runnable's path to the one eligible budget that times it;
    ``doubled`` holds the constraints of every runnable timed twice, none of
    which is carried.
    """

    timings: tuple[_PlannedTiming, ...]
    carried: Mapping[str, EligibleBudget]
    doubled: Mapping[str, tuple[EligibleBudget, ...]]


#: The model's ``wcet_type`` of each ``EXECUTION-TIME-TYPE`` literal (``ExecutionTimeTypeEnum``,
#: XSD R4.2.2:72146, R4.3.0:83311), the writer's spelling upper-cased.
_WCET_TYPES: dict[str, Literal["gross", "net"]] = {"GROSS": "gross", "NET": "net"}
#: A factor longer than a budget's integer, as read or normalised.
_OVER_THE_CAP = f"the maximum is larger than a budget holds ({MAX_FACTOR_DIGITS} digits of its unit)"
_GUARANTEE_REASON = (
    "a timing guarantee is the supplier's statement, not the architect's requirement: "
    "SWCraft carries requirements only -- not imported"
)


def _timing_target(
    timing: SwcTimingInfo, behaviours: Mapping[str, SwcInfo], declared: Collection[str]
) -> str | SkipReason:
    """The path of the component *timing* gives budgets to, or why the whole ``SWC-TIMING`` is skipped.

    The target is the component whose first behaviour the ``SWC-TIMING``'s
    ``BEHAVIOR-REF`` names exactly and, where a ``COMPONENT-REF`` is stated, which
    that names too. A reference to content the file does not declare is
    :attr:`SkipCategory.OUTSIDE_FILE`; every other refusal is SWCraft's own.
    """
    ref = timing.behavior_ref
    if timing.variation_point:
        return not_supported(
            "the SWC-TIMING carries a VARIATION-POINT: every budget under it is variant-conditional, and keeping "
            "the maximum without the condition would change what the budget means"
        )
    if ref is None or not ref.strip():
        return not_supported("the SWC-TIMING has no BEHAVIOR-REF, so it names no runnable a budget could be given to")
    raw = behaviours.get(ref)
    if raw is None:
        if ref not in declared:
            return SkipReason(
                f"BEHAVIOR-REF {ref!r} names a behaviour the file does not declare", SkipCategory.OUTSIDE_FILE
            )
        return _no_created_component_reason(ref)
    component = timing.component_ref
    if component is not None and component != raw.path:
        reason = (
            f"COMPONENT-REF {component!r} names another component than the one whose behaviour the SWC-TIMING "
            f"times ({raw.path})"
        )
        if component not in declared:
            return SkipReason(reason, SkipCategory.OUTSIDE_FILE)
        return not_supported(reason)
    return raw.path


def _budget_decision(
    timing: SwcTimingInfo, constraint: TimingConstraintInfo, raw: SwcInfo, declared: Collection[str]
) -> _Decision:
    """The budget an ``SWC-TIMING``'s constraint states, or why it is skipped.

    ``None`` for a guarantee, which the caller reports. The checks run in a
    fixed order: the name, the kind, a scoping member, the runnable, the type,
    the maximum and its two integers, the code.
    """
    if constraint.role == "guarantee":
        return None
    if not constraint.name.strip():
        return blank_short_name(f"{constraint.kind} has a missing or blank SHORT-NAME")
    if constraint.kind != EXECUTION_TIME_CONSTRAINT:
        return not_supported(
            f"a {constraint.kind} under TIMING-REQUIREMENTS is a timing requirement SWCraft does not carry -- only an "
            "EXECUTION-TIME-CONSTRAINT becomes a runnable's budget"
        )
    if constraint.scoping:
        return not_supported(
            f"the execution-time constraint carries {constraint.scoping[0]}: keeping the maximum without it would "
            "change what the budget means"
        )
    ref = constraint.executable_ref
    if ref is None or not ref.strip():
        return not_supported("the execution-time constraint has no EXECUTABLE-REF, so it times no runnable")
    declared_here = {runnable.path: runnable.name for runnable in raw.runnables}
    if ref not in declared_here and ref not in declared:
        return SkipReason(
            f"EXECUTABLE-REF {ref!r} names an executable entity the file does not declare", SkipCategory.OUTSIDE_FILE
        )
    if ref not in declared_here or constraint.executable_dest != "RUNNABLE-ENTITY":
        return _no_runnable_reason(timing, constraint)
    literal = constraint.execution_time_type
    if literal not in _WCET_TYPES:
        return not_supported(
            "the execution-time constraint has no EXECUTION-TIME-TYPE"
            if literal is None
            else f"EXECUTION-TIME-TYPE {literal!r} is neither GROSS nor NET"
        )
    maximum = constraint.maximum
    if maximum is None:
        return not_supported("the execution-time constraint has no MAXIMUM, and a budget is a maximum")
    if maximum.code_text is None:
        return not_supported("the MAXIMUM has no CSE-CODE")
    if maximum.cse_code is None:
        return not_supported(f"the MAXIMUM's CSE-CODE {maximum.code_text!r} is not an xsd:unsignedInt")
    if maximum.factor_text is None:
        return not_supported("the MAXIMUM has no CSE-CODE-FACTOR")
    if maximum.factor_overflow == "positive":
        return not_supported(f"the MAXIMUM's CSE-CODE-FACTOR has more digits than SWCraft reads: {_OVER_THE_CAP}")
    if maximum.cse_factor is None and maximum.factor_overflow is None:
        return not_supported(f"the MAXIMUM's CSE-CODE-FACTOR {maximum.factor_text!r} is not an AR:INTEGER")
    if maximum.cse_factor is None or maximum.cse_factor <= 0:
        return not_supported("the MAXIMUM's CSE-CODE-FACTOR is not positive, and a budget is a positive duration")
    try:
        budget = from_cse(maximum.cse_code, maximum.cse_factor)
    except CseOverTheCap as exc:
        return not_supported(f"{_OVER_THE_CAP}: {exc}")
    except CseNotABudget as exc:
        return not_supported(
            f"the maximum is written in CSE code {maximum.cse_code}, which no budget unit holds exactly: {exc}"
        )
    return EligibleBudget(ref, declared_here[ref], budget, _WCET_TYPES[literal], timing, constraint)


def _no_created_component_reason(ref: str | None) -> SkipReason:
    """The ``BEHAVIOR-REF`` names a behaviour the file declares, of no component this import creates."""
    return not_supported(f"BEHAVIOR-REF {ref!r} names no behaviour of a component this import creates")


def _no_runnable_reason(timing: SwcTimingInfo, constraint: TimingConstraintInfo) -> SkipReason:
    """The ``EXECUTABLE-REF`` names no runnable this import carries in the behaviour timed."""
    return not_supported(
        f"EXECUTABLE-REF {constraint.executable_ref!r} (DEST {constraint.executable_dest}) names no runnable this "
        f"import carries in the behaviour the SWC-TIMING times ({timing.behavior_ref})"
    )


def plan_budgets(model: ImportModel, declared: Collection[str]) -> BudgetPlan:
    """Decide every ``SWC-TIMING`` against the file alone, before :func:`map_swcs` builds a runnable.

    Two eligible requirements on one runnable -- across one or several
    ``SWC-TIMING``\\ s -- are both withheld; a guarantee never counts as the
    second. Nothing is recorded here.
    """
    behaviours = {f"{raw.path}/{raw.behavior_name}": raw for raw in model.swcs if raw.behavior_name}
    raw_by_path = {raw.path: raw for raw in model.swcs}
    timings: list[_PlannedTiming] = []
    by_runnable: dict[str, list[EligibleBudget]] = {}
    for timing in model.swc_timings:
        target = _timing_target(timing, behaviours, declared)
        decisions: list[tuple[TimingConstraintInfo, _Decision]] = []
        if isinstance(target, str):
            for constraint in timing.constraints:
                decision = _budget_decision(timing, constraint, raw_by_path[target], declared)
                decisions.append((constraint, decision))
                if isinstance(decision, EligibleBudget):
                    by_runnable.setdefault(decision.runnable_path, []).append(decision)
        timings.append(_PlannedTiming(timing, target, tuple(decisions)))
    return BudgetPlan(
        timings=tuple(timings),
        carried={path: group[0] for path, group in by_runnable.items() if len(group) == 1},
        doubled={path: tuple(group) for path, group in by_runnable.items() if len(group) > 1},
    )


def budget_records(carried: Sequence[EligibleBudget]) -> tuple[list[BudgetSource], list[Represented]]:
    """A component's carried budgets as :attr:`ImportedSwc.budget_sources` and its normalisation lines.

    A line is written for a budget carried from a CSE code other than its unit's, and for one whose
    ``CSE-CODE-FACTOR`` was spelled in hexadecimal, octal or binary, which the model writes decimal.
    """
    sources = [
        BudgetSource(
            candidate.runnable_name,
            candidate.timing.path,
            candidate.timing.name,
            candidate.constraint.path,
            candidate.constraint.name,
        )
        for candidate in carried
    ]
    normalizations = [
        Represented(
            source_path=candidate.constraint.path,
            line=(
                f"{candidate.constraint.path}: MAXIMUM CSE-CODE {maximum.cse_code} with CSE-CODE-FACTOR "
                f"{maximum.factor_radix_source or maximum.cse_factor} represented as wcet {candidate.budget.text}"
            ),
        )
        for candidate in carried
        if (maximum := candidate.constraint.maximum) is not None
        and (maximum.cse_code not in CSE_CODE.values() or maximum.factor_radix_source is not None)
    ]
    return sources, normalizations


def record_budgets(
    plan: BudgetPlan, created: Collection[str], carried_runnables: Collection[str], skip_log: SkipLog
) -> None:
    """Record the plan's skips and the lines beside each carried budget, once :func:`map_swcs` has run.

    *created* holds the paths of the components :func:`map_swcs` created and
    *carried_runnables* the paths of the runnables it built. An ``SWC-TIMING``
    whose target is no created component is skipped once, its constraints
    chained to it; a guarantee is reported and the import goes on; every other
    skip is behavioural, among them a budget whose runnable was not built and
    the two of a runnable timed twice (each naming the other). A carried
    budget's ``MINIMUM`` and traceable children are descriptive lines.

    **What this does not reach:** a component the import drops after mapping
    (``--packages``, a reserved device name) takes its runnables' budgets with
    it, and its lines about the file stay; a ``<runnable>_Budget`` longer than
    128 characters is carried as written and left to ``check-model``.
    """
    for planned in plan.timings:
        timing = planned.timing
        target = planned.target
        if isinstance(target, str) and target not in created:
            target = _no_created_component_reason(timing.behavior_ref)
        if isinstance(target, SkipReason):
            root = skip_log.record_reason(timing.path, "SWC-TIMING", target, effect=ABORT)
            for constraint in timing.constraints:
                skip_log.record_chained(
                    constraint.path,
                    constraint.kind,
                    f"chain-skipped: SWC-TIMING '{timing.name}' was skipped ({target.text})",
                    root=root,
                )
            continue
        for constraint, decision in planned.decisions:
            if decision is None:
                skip_log.record(
                    constraint.path,
                    constraint.kind,
                    _GUARANTEE_REASON,
                    category=SkipCategory.NOT_SUPPORTED,
                    effect=REPORT,
                )
                continue
            if isinstance(decision, EligibleBudget) and decision.runnable_path in plan.doubled:
                others = ", ".join(
                    other.constraint.path for other in plan.doubled[decision.runnable_path] if other is not decision
                )
                skip_log.record(
                    constraint.path,
                    constraint.kind,
                    f"runnable '{decision.runnable_name}' is given a second execution-time "
                    f"requirement ({others}): a runnable has one budget, so neither is carried",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect=ABORT,
                )
                continue
            if isinstance(decision, EligibleBudget) and decision.runnable_path not in carried_runnables:
                decision = _no_runnable_reason(timing, constraint)
            if isinstance(decision, SkipReason):
                skip_log.record_reason(constraint.path, constraint.kind, decision, effect=ABORT)
                continue
            losses = list(constraint.uncarried)
            if constraint.minimum is not None:
                losses.insert(0, ("MINIMUM", "MINIMUM not carried: the model holds a budget's maximum alone"))
            for tag, reason in losses:
                skip_log.record(constraint.path, tag, reason, category=SkipCategory.NOT_SUPPORTED, effect=REPORT)


# ---------------------------------------------------------------------------
# The records that name a stub's member
# ---------------------------------------------------------------------------


def finish_stub_members(imported: ImportedSwc, unresolved: UnresolvedReferences, skip_log: SkipLog) -> ImportedSwc:
    """Keep or report every record :func:`map_swcs` held for the stub decision.

    Called once per component right after :meth:`UnresolvedReferences.mint_stubs`
    decided the stubs and their member lists. A held record whose member the
    stub lists is **kept**:

    * an event, a read, a write or a call is inserted into its runnable where
      the source put it, with its :class:`GeneratedSource`; a read, write or call
      repeating one ``(port, member)`` pair already kept is one collapse line;
      one through a port of the wrong direction gets the line a declared
      interface's construct gets;
    * a com-spec is carried as the derivation writes it: a component port's
      ``com_specs`` gains an entry for every representable attribute whose value
      differs from the derivation's, and what the derivation cannot write is a
      loss line.

    Every other held record keeps its line: an event, a read, a write or a call
    is chained to the target, its text naming the stub once the stub is
    written; a com-spec gets its not-carried line. That covers a target the stub
    decision declined, a reference deeper than one segment or under another
    path, a kind the stub's ``DEST`` does not admit, a mode-switch stub, two
    names alike but for case, and a blank or illegal segment.

    Returns *imported* with its held lists empty; the same object when nothing
    was held. The component is re-validated (``model_copy`` runs no validation).
    """
    if not imported.held_stub_records and not imported.held_stub_com_specs:
        return imported
    swc = imported.swc
    admitted: list[tuple[HeldStubRecord, str]] = []
    for held in imported.held_stub_records:
        member = unresolved.listed_member(held.target, held.kind, held.member_ref)
        if member is None:
            chain_member_line(unresolved, skip_log, held.path, held.tag, held.reason, held.target, held.once_stubbed)
            continue
        if held.misdirected is not None:
            skip_log.record(held.path, held.tag, held.misdirected, category=SkipCategory.NOT_SUPPORTED, effect=ABORT)
            continue
        admitted.append((held, member))
    lists, sources, collapses = _insert_held(imported, admitted, skip_log)

    entries_by_port: dict[str, list[PortComSpec]] = {}
    for port in imported.held_stub_com_specs:
        carried = _finish_stub_com_specs(port, unresolved, skip_log, carries_com_specs=True)
        if carried:
            entries_by_port[port.port_name] = carried

    update: dict[str, object] = {}
    if lists:
        update["runnables"] = _runnables_with(swc, lists)
    ports = [
        port.model_copy(update={"com_specs": [*port.com_specs, *entries_by_port[port.name]]})
        if port.name in entries_by_port
        else port
        for port in swc.ports
    ]
    if entries_by_port:
        update["ports"] = ports
    # An admitted implicit read against the com-specs its port now carries.
    _report_held_implicit_enable_update(admitted, ports, skip_log)
    if update:
        swc = SWComponent.model_validate(swc.model_copy(update=update).model_dump())
    return replace(
        imported,
        swc=swc,
        generated_sources=sources,
        access_collapses=collapses,
        held_stub_records=[],
        held_stub_com_specs=[],
    )


def _insert_held(
    imported: ImportedSwc, admitted: Sequence[tuple[HeldStubRecord, str]], skip_log: SkipLog
) -> tuple[dict[tuple[str, str], list[Any]], list[GeneratedSource], list[str]]:
    """Insert every *admitted* held record, built from its member name, where the source put it.

    Each entry goes in after the entries kept before it
    (``HeldStubRecord.position``), with its :class:`GeneratedSource` and every
    later source of that list shifted; a read, write or call repeating one
    ``(port, member)`` pair already admitted is one collapse line. Each entry is
    built with *skip_log*, the admitting pass's own. Returns the changed lists
    by ``(runnable, collection)``, the sources and the collapse lines.
    """
    swc = imported.swc
    runnables = {runnable.name: runnable for runnable in swc.runnables}
    lists: dict[tuple[str, str], list[Any]] = {}
    sources = list(imported.generated_sources)
    collapses = list(imported.access_collapses)
    inserted: dict[tuple[str, str], int] = {}
    kept_names: dict[tuple[str, str, str, str], str] = {}
    for held, member in admitted:
        slot = (held.runnable_name, held.collection)
        if held.collection != "events":
            key = (held.runnable_name, held.collection, held.port, member)
            first = kept_names.get(key)
            if first is not None:
                collapses.append(
                    collapse_line(
                        swc.name, held.runnable_name, held.collection, f"{held.port}.{member}", held.name, first
                    )
                )
                continue
            kept_names[key] = held.name
        entries = lists.setdefault(slot, list(getattr(runnables[held.runnable_name], held.collection)))
        index = held.position + inserted.get(slot, 0)
        entries.insert(index, held.build(member, skip_log))
        inserted[slot] = inserted.get(slot, 0) + 1
        sources = [
            replace(source, index=source.index + 1)
            if (source.runnable_name, source.collection.split(".", 1)[0]) == slot and source.index >= index
            else source
            for source in sources
        ]
        sources.append(GeneratedSource(held.runnable_name, held.collection, index, held.path, held.tag, held.name))
    return lists, sources, collapses


def _runnables_with(swc: SWComponent, lists: Mapping[tuple[str, str], list[Any]]) -> list[Runnable]:
    """*swc*'s runnables with each ``(runnable, collection)`` list of *lists* replaced."""
    return [
        runnable.model_copy(
            update={collection: entries for (name, collection), entries in lists.items() if name == runnable.name}
        )
        for runnable in swc.runnables
    ]


# ---------------------------------------------------------------------------
# The late pass's mechanics, over a component a late-resolving policy mapped
# (`UnresolvedReferences.for_component_import`)
# ---------------------------------------------------------------------------


class LateBinding(NamedTuple):
    """What :func:`bind_late_port` made of one port the target project answered.

    The bound ``port``; each com-spec naming a member the target's interface
    does not declare (``member_misses``, each a dependency verdict); and the
    port's own init values carried from a hexadecimal spelling (``init_radix``)
    or by equivalence with the element's (``init_equivalents``).
    """

    port: Port
    member_misses: tuple[ComSpecMemberMiss, ...]
    init_radix: tuple[PortInitRadix, ...]
    init_equivalents: tuple[PortInitEquivalent, ...]


def bind_late_port(
    port: Port,
    late: LatePort,
    interface: Interface,
    skip_log: SkipLog,
    *,
    port_inits: Mapping[PortInitKey, PortInitOutcome],
) -> LateBinding:
    """*port* bound to the target project's declared *interface*, its com-specs and inits judged against it.

    The port becomes what ``map_ports`` makes of a port on a mapped interface;
    what is not carried is a loss line. Every member
    is still owned by the port's own interface **path**, never by a leaf: a
    com-spec naming the member under another path is lost, not bound; one
    naming, under that path, a member the target's interface does not declare
    is a :class:`ComSpecMemberMiss`, never a line.

    *port_inits* is the late pass's decision on every com-spec init the file
    holds under the answered path, decided against the target element's type
    and keyed by ``PortInitKey``. The ``SHORT-LABEL`` line is recorded at once,
    since no later pass on this path takes the ``INIT-VALUE`` away.
    """
    p, direction = late.raw, late.direction
    gated = _gated_port_inits(p, direction, interface, port_inits)
    com_specs, with_queue_length = _port_com_specs(p, direction, interface, gated)
    misses: list[ComSpecMemberMiss] = []
    for block, tag, reason in _com_spec_losses(p, direction, interface, carries_com_specs=True, misses=misses):
        _report_com_spec_loss(p.path, block, tag, reason, skip_log)
    radix, equivalents, labels = _port_init_lines(p, gated, skip_log, None, carries_com_specs=True)
    for element, key, lost in labels:
        _report_port_init_labels(key, element, lost, skip_log)
    for missing in _queued_elements_without_queue_length(p, direction, interface, with_queue_length):
        report_refused_combination(
            f"{p.path}/{COM_SPEC_BLOCK['require']}", "QUEUED-RECEIVER-COM-SPEC", missing, skip_log
        )
    return LateBinding(
        port=port.model_copy(update={"interface": interface.name, "com_specs": com_specs}),
        member_misses=tuple(misses),
        init_radix=tuple(PortInitRadix(port.name, element, line) for element, line in radix),
        init_equivalents=tuple(PortInitEquivalent(port.name, element, key) for element, key in equivalents),
    )


class UnansweredRecord(NamedTuple):
    """A held event, read, write or call the late pass found no member for.

    Everything the verdict names: the runnable, the collection the record would
    have joined, the port, the member's name (the reference's last segment),
    whether it is an operation (else a data element), and ``interface_name`` --
    the interface that answered the port, or the last segment of the path
    nothing answered (any record through a stub port is one of these). ``tag``
    is the source element's tag, so a data-received event is phrased apart from
    an operation event.
    """

    runnable_name: str
    collection: str
    port: str
    member_name: str
    is_operation: bool
    interface_name: str
    tag: str


def _unanswered(held: HeldStubRecord, interface: Interface | None) -> UnansweredRecord:
    """The :class:`UnansweredRecord` of *held*, answered by *interface* or by nothing."""
    return UnansweredRecord(
        runnable_name=held.runnable_name,
        collection=held.collection,
        port=held.port,
        member_name=held.member_ref.rpartition("/")[2],
        is_operation=held.kind == "operations",
        interface_name=interface.name if interface is not None else held.target.rpartition("/")[2],
        tag=held.tag,
    )


def record_unanswered_port(late: LatePort, skip_log: SkipLog) -> None:
    """The line of a port whose path neither tier answered and no stub could take.

    The stub decision declines a path whose occurrences disagree on ``DEST``,
    whose ``DEST`` no interface stub admits, or which is malformed or inside a
    generated root package (``UnresolvedReferences.mint_stubs``).
    """
    skip_log.record_reason(
        late.raw.path,
        late.raw.kind,
        SkipReason(
            f"unresolved reference {late.raw.interface_ref} (the file declares no interface at that path)",
            SkipCategory.OUTSIDE_FILE,
        ),
        effect=ABORT,
    )


def hold_late_stub_com_specs(late: LatePort, unresolved: UnresolvedReferences, skip_log: SkipLog) -> HeldStubComSpecs:
    """The com-specs of a late port the stub decision will decide, held and their members staged.

    Once the target has not answered the path, they are treated as on ``import
    model``'s path: each com-spec the derivation would write back stages its
    member -- the **only** source of a minted stub's member list here, since no
    behavioural record stages one under a late policy. Called before
    :meth:`UnresolvedReferences.mint_stubs`.
    """
    held = _hold_stub_com_specs(late.raw, late.direction, unresolved, skip_log)
    return HeldStubComSpecs(late.port_name, late.raw, late.direction, held)


def bind_minted_stub_port(
    port: Port, held: HeldStubComSpecs, stub_name: str, unresolved: UnresolvedReferences, skip_log: SkipLog
) -> Port:
    """*port* re-pointed at the interface stub minted for its path, its held com-specs settled.

    The com-spec half of :func:`finish_stub_members`. The behavioural half is
    **not** run: every event, read, write and call through a stub port is a
    missing reference on this path, whatever the stub lists.
    """
    entries = _finish_stub_com_specs(held, unresolved, skip_log, carries_com_specs=True)
    return port.model_copy(update={"interface": stub_name, "com_specs": [*port.com_specs, *entries]})


def bind_reused_stub_port(port: Port, late: LatePort, stub: ExternalInterface, skip_log: SkipLog) -> Port:
    """*port* re-pointed at the target project's stub of the same ``external_ref``, its com-specs judged.

    The stub is **reused**: nothing is minted and it is not rewritten. Each
    com-spec is judged against the members the stub **already lists**; any
    other is a line, never a verdict and never a member added to the stub.
    What this does not reach: the stub's lists were an earlier import's
    inference, and they admit no **behaviour** through the port (every such
    record is a missing reference).
    """
    target = late.interface_path
    listed = {"operations": frozenset(stub.operations), "data_elements": frozenset(stub.data_elements)}
    entries: list[PortComSpec] = []
    seen: set[tuple[str, str]] = set()
    for shape in late.raw.com_spec_shapes:
        member = _stub_member_of(shape, late.direction)
        if member is None or member in seen:
            why = _F5_NOT_DERIVED if member is None else _F5_REPEATED
            _report_stub_com_spec(late.raw.path, shape.block_tag, shape.tag, why, skip_log)
            continue
        seen.add(member)
        kind, ref = member
        name = direct_member(target, ref)
        if name is None or name not in listed[kind]:
            _report_stub_com_spec(late.raw.path, shape.block_tag, shape.tag, _F5_UNLISTED, skip_log)
            continue
        losses, stated = _stub_com_spec_losses(shape, late.direction, carries_com_specs=True)
        for block, tag, reason in losses:
            _report_com_spec_loss(late.raw.path, block, tag, reason, skip_log)
        if stated:
            entries.append(_com_spec_entry(name, stated, None))
    return port.model_copy(update={"interface": stub.name, "com_specs": [*port.com_specs, *entries]})


def admit_held_records(
    imported: ImportedSwc, interfaces_by_port: Mapping[str, Interface], skip_log: SkipLog
) -> tuple[ImportedSwc, list[UnansweredRecord]]:
    """Admit every held record whose port the target answered and whose member its interface declares.

    *interfaces_by_port* maps a port name to the interface the late pass bound.
    In the file tier's order:

    1. the interface's kind is the one the record needs -- else **unanswered**
       (a missing reference): here the interface is the project's statement,
       not the file's;
    2. the port's direction is the one the record needs -- else the record's own
       line: the direction is the file's statement about its own port;
    3. the member reference is a path whose parent is the port's interface
       **path** -- else the file tier's line: a member is never bound by its
       leaf under another interface;
    4. the interface declares the member -- else **unanswered**.

    An admitted record is inserted where the source put it; a by-value read of
    a queued element takes the ``[constr_1277]`` line. A record whose port
    nothing answered is unanswered. Returns *imported* with its held records
    spent, and the unanswered ones.
    """
    admitted: list[tuple[HeldStubRecord, str]] = []
    unanswered: list[UnansweredRecord] = []
    for held in imported.held_stub_records:
        interface = interfaces_by_port.get(held.port)
        if interface is None:
            unanswered.append(_unanswered(held, None))
            continue
        needed_kind = "client_server" if held.kind == "operations" else "sender_receiver"
        if interface.kind != needed_kind:
            unanswered.append(_unanswered(held, interface))
            continue
        if held.misdirected is not None:
            skip_log.record(held.path, held.tag, held.misdirected, category=SkipCategory.NOT_SUPPORTED, effect=ABORT)
            continue
        member_word, members = interface_members(interface)
        parent, slash, member = held.member_ref.rpartition("/")
        if not slash:
            skip_log.record(
                held.path,
                held.tag,
                not_a_path_reason(held.member_ref),
                category=SkipCategory.NOT_SUPPORTED,
                effect=ABORT,
            )
            continue
        if parent != held.target:
            skip_log.record(
                held.path,
                held.tag,
                not_a_member_reason(held.member_ref, f"a {member_word}", held.port, interface.name),
                category=SkipCategory.NOT_SUPPORTED,
                effect=ABORT,
            )
            continue
        if member not in members:
            unanswered.append(_unanswered(held, interface))
            continue
        admitted.append((held, member))
        if held.by_value and queued_element(interface, member):
            report_refused_combination(
                held.path, held.tag, queued_read_by_value_reason(held.tag, member, held.port), skip_log
            )
        if held.implicit_read and queued_element(interface, member):
            report_refused_combination(held.path, held.tag, queued_implicit_read_reason(member, held.port), skip_log)
    _report_held_implicit_enable_update(admitted, imported.swc.ports, skip_log)
    lists, sources, collapses = _insert_held(imported, admitted, skip_log)
    swc = imported.swc
    if lists:
        # No validation here: the late pass validates the component once, after its last change.
        swc = swc.model_copy(update={"runnables": _runnables_with(swc, lists)})
    return (
        replace(imported, swc=swc, generated_sources=sources, access_collapses=collapses, held_stub_records=[]),
        unanswered,
    )


def finish_stub_port_com_specs(
    held: Sequence[HeldStubComSpecs], unresolved: UnresolvedReferences, skip_log: SkipLog
) -> None:
    """:func:`finish_stub_members`' com-spec half for a composition's boundary ports.

    A composition's port carries no ``com_specs`` entry, so a com-spec naming a
    listed member is carried by equivalence only: every value other than the
    derivation's silent one is a loss line.
    """
    for port in held:
        _finish_stub_com_specs(port, unresolved, skip_log, carries_com_specs=False)


def _finish_stub_com_specs(
    port: HeldStubComSpecs, unresolved: UnresolvedReferences, skip_log: SkipLog, *, carries_com_specs: bool
) -> list[PortComSpec]:
    """Carry or report each held com-spec of one port; the ``com_specs`` entries a component port gains."""
    entries: list[PortComSpec] = []
    target = port.interface_path
    for held in port.held:
        member = unresolved.listed_member(target, held.kind, held.member_ref)
        if member is None:
            _report_stub_com_spec(port.raw.path, held.shape.block_tag, held.shape.tag, _F5_UNLISTED, skip_log)
            continue
        losses, stated = _stub_com_spec_losses(held.shape, port.direction, carries_com_specs=carries_com_specs)
        for block, tag, reason in losses:
            _report_com_spec_loss(port.raw.path, block, tag, reason, skip_log)
        if stated:
            entries.append(_com_spec_entry(member, stated, None))
    return entries


# ---------------------------------------------------------------------------
# The names the source states
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NameCandidate:
    """One source name that differs from the derived one: where the report keys it, and the two names."""

    key: str
    source: str
    derived: str


def _written_as(noun: str, derived: str) -> str:
    """The holder phrase of a derived name another element keeps."""
    return f"the {noun} SWCraft writes as '{derived}'"


def _settle_scope(candidates: Sequence[_NameCandidate], reserved: dict[str, str], noun: str) -> dict[int, str]:
    """Which *candidates* one AUTOSAR name scope declines, by index, each with the holder it would collide with.

    *reserved* maps the :func:`~swcraft_tools.identifier.short_name_key` of every
    name that stays in the scope to the phrase naming its holder; it grows by
    the derived name of every declined candidate. ``[constr_2508]``: a
    ``SHORT-NAME`` is unique ignoring case in its scope. Every group of
    candidates the source names alike is declined whole; then a candidate equal
    to a reserved name is declined and its derived name reserved, repeated until
    none is declined (declining restores a derived name that may collide).
    Declining only grows the reserved set, so the result is the least fixpoint,
    independent of order. A swap of two derived names is no collision.
    """
    declined: dict[int, str] = {}
    groups: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        groups.setdefault(short_name_key(candidate.source), []).append(index)
    for members in groups.values():
        if len(members) < 2:
            continue
        for index in members:
            other = next(candidates[i].source for i in members if i != index)
            declined[index] = f"another {noun} the source gives the same name ('{other}', ignoring case)"
    for index in declined:
        reserved.setdefault(short_name_key(candidates[index].derived), _written_as(noun, candidates[index].derived))
    while True:
        changed = False
        for index, candidate in enumerate(candidates):
            if index in declined:
                continue
            holder = reserved.get(short_name_key(candidate.source))
            if holder is None:
                continue
            declined[index] = holder
            reserved.setdefault(short_name_key(candidate.derived), _written_as(noun, candidate.derived))
            changed = True
        if not changed:
            return declined


def access_slots(runnable: Runnable) -> list[tuple[str, int]]:
    """``(collection, index)`` of every access entry of *runnable*, in :func:`emitted_access_names` order.

    ``reads``, ``writes``, ``calls``, ``reads_variables``, ``writes_variables``,
    each list in its own order; the settling pass and the report pair a source
    with an entry by these slots. The variable accesses share the runnable's
    name space with the port accesses, so they are one scope's slots.
    """
    return [
        *(("reads", index) for index in range(len(runnable.reads))),
        *(("writes", index) for index in range(len(runnable.writes))),
        *(("calls", index) for index in range(len(runnable.calls))),
        *(("reads_variables", index) for index in range(len(runnable.reads_variables))),
        *(("writes_variables", index) for index in range(len(runnable.writes_variables))),
    ]


def result_slots(runnable: Runnable) -> list[tuple[str, int]]:
    """``(collection, index)`` of every result point of *runnable*.

    In ``validator.result_point_entries``' order -- one per ``poll: true`` call,
    as ``calls.result``, then one per ``call_returned`` event, as
    ``events.result`` -- so it pairs slot for slot with
    ``validator.emitted_result_point_names``. A result point shares its
    runnable's access point name space.
    """
    return [
        *(("calls.result", index) for index, call in enumerate(runnable.calls) if call.poll),
        *(
            ("events.result", index)
            for index, event in enumerate(runnable.events)
            if isinstance(event, CallReturnedEvent)
        ),
    ]


def emitted_access_names(runnable: Runnable) -> list[str]:
    """The ``SHORT-NAME`` every access entry of *runnable* is written with, slot for slot of :func:`access_slots`."""
    return [*emitted_access_point_names(runnable), *emitted_variable_access_names(runnable)]


def _settle(
    slots: Sequence[tuple[str, str, int]],
    derived_names: Sequence[str],
    sources: Mapping[tuple[str, str, int], GeneratedSource],
    reserved: dict[str, str],
    noun: str,
    why_not: dict[str, str],
) -> dict[tuple[str, str, int], str]:
    """The names one scope states, by slot: each slot's source name where it is a candidate and is not declined.

    A slot is ``(runnable, collection, index)``; *derived_names* its derived name,
    slot for slot. A slot whose source name is no candidate (:func:`_stated_name`)
    keeps its derived name, which joins *reserved*; the candidates are settled by
    :func:`_settle_scope`, and each declined one's clause joins *why_not*.
    """
    candidates: list[_NameCandidate] = []
    slot_of: list[tuple[str, str, int]] = []
    for slot, derived in zip(slots, derived_names, strict=True):
        source = sources.get(slot)
        if source is not None and _stated_name(source.name, derived, source.path, why_not):
            candidates.append(_NameCandidate(source.path, source.name, derived))
            slot_of.append(slot)
        else:
            reserved.setdefault(short_name_key(derived), _written_as(noun, derived))
    declined = _settle_scope(candidates, reserved, noun)
    names: dict[tuple[str, str, int], str] = {}
    for position, candidate in enumerate(candidates):
        if position in declined:
            why_not[candidate.key] = f"because the source's name would collide with {declined[position]}"
        else:
            names[slot_of[position]] = candidate.source
    return names


def _stated_name(source: str, derived: str, key: str, why_not: dict[str, str]) -> bool:
    """Whether *source* is a candidate: an identifier unequal to *derived* (*why_not* gets an inadmissible one)."""
    if source == derived:
        return False
    reason = check_identifier(source)
    if reason is not None:
        why_not[key] = f"because the source's name is not a legal AUTOSAR Identifier (a name {reason})"
        return False
    return True


def settle_stated_names(model: ImportModel, swcs: Sequence[ImportedSwc]) -> tuple[list[ImportedSwc], dict[str, str]]:
    """State the source's name of every behaviour, event and access point where it differs from the derived one.

    Run once over the **final** model -- after the external stubs are minted
    and the reserved names reconciled, which can rename a component and so its
    derived behaviour name -- right before the report's generated-name lines
    (:func:`~swcraft_tools.arxml_import.report.generated_name_losses`). The
    mapped components state no name yet, so the validator's emitted-name
    functions answer the derived names.

    A source name equal to the derived one is not stated. Otherwise it is stated unless:

    * it is not a legal AUTOSAR identifier of at most 128 characters
      (admissibility first, the ``symbol`` precedent); or
    * it would collide, ignoring case (``[constr_2508]``), in its scope -- the
      behaviour's events and runnables, a runnable's access points, the
      component's ports for the behaviour -- with a runnable's or a port's name,
      the derived name of an element that states none, or another candidate
      (:func:`_settle_scope`).

    Returns the components, each with the names it now states, and, keyed by the
    path the report names it at, the clause saying why each source name was not
    stated, so an import never hands ``check-model`` a name finding it made
    itself. An external stub component and a component the reader does not hold
    are returned as they are. What this does not reach: an element the import
    does not carry has no name to state.
    """

    def derived_result_point_names(runnable: Runnable) -> list[str]:
        """``Result_{port}_{operation}`` for each result point, slot for slot of :func:`result_slots`.

        The derived name whatever the entry states: this pass decides whether the
        mapper's stated result name stays.
        """
        return [
            derived_result_point_name(port, operation)
            for _claim, port, operation, _stated in result_point_entries(runnable)
        ]

    raw_by_path = {raw.path: raw for raw in model.swcs}
    why_not: dict[str, str] = {}
    settled: list[ImportedSwc] = []
    for imported in swcs:
        raw = raw_by_path.get(imported.path)
        if imported.swc.external or raw is None:
            settled.append(imported)
            continue
        swc = imported.swc

        # The behaviour, in the component's scope with its ports: one candidate, so a lookup.
        behavior_name: str | None = None
        if raw.behavior_name is not None:
            key = f"{raw.path}/{raw.behavior_name}"
            if _stated_name(raw.behavior_name, emitted_behavior_name(swc), key, why_not):
                ports = {short_name_key(port.name): f"port '{port.name}'" for port in swc.ports}
                holder = ports.get(short_name_key(raw.behavior_name))
                if holder is not None:
                    why_not[key] = f"because the source's name would collide with {holder}"
                else:
                    behavior_name = raw.behavior_name

        sources = {(s.runnable_name, s.collection, s.index): s for s in imported.generated_sources}

        # The events, in the behaviour's scope with the runnables.
        event_names = _settle(
            [(runnable.name, "events", index) for runnable in swc.runnables for index in range(len(runnable.events))],
            derived_event_names(swc),
            sources,
            {short_name_key(runnable.name): f"runnable '{runnable.name}'" for runnable in swc.runnables},
            "event",
            why_not,
        )

        # The access points, each runnable its own scope, shared with its result points.
        access_names: dict[tuple[str, str, int], str] = {}
        for runnable in swc.runnables:
            access_names.update(
                _settle(
                    [
                        (runnable.name, collection, index)
                        for collection, index in [*access_slots(runnable), *result_slots(runnable)]
                    ],
                    [*emitted_access_names(runnable), *derived_result_point_names(runnable)],
                    sources,
                    {},
                    "access point",
                    why_not,
                )
            )

        # Every result slot, whose name the mapper stated at construction.
        result_slot_keys = {
            (runnable.name, collection, index)
            for runnable in swc.runnables
            for collection, index in result_slots(runnable)
        }
        if behavior_name is None and not event_names and not access_names and not result_slot_keys:
            settled.append(imported)
            continue

        def stated(runnable: Runnable, collection: str, index: int) -> dict[str, str | None]:
            """The names one entry states: its own ``name``, and its result point's."""
            update: dict[str, str | None] = {}
            names = event_names if collection == "events" else access_names
            if (runnable.name, collection, index) in names:
                update["name"] = names[(runnable.name, collection, index)]
            if (runnable.name, f"{collection}.result", index) in result_slot_keys:
                # The name the mapper stated stays where the scope admits it; otherwise it is withdrawn.
                update["result"] = access_names.get((runnable.name, f"{collection}.result", index))
            return update

        runnables = [
            runnable.model_copy(
                update={
                    "events": [
                        event.model_copy(update=update) if (update := stated(runnable, "events", index)) else event
                        for index, event in enumerate(runnable.events)
                    ],
                    **{
                        collection: [
                            entry.model_copy(update=update)
                            if (update := stated(runnable, collection, index))
                            else entry
                            for index, entry in enumerate(entries)
                        ]
                        for collection, entries in (
                            ("reads", runnable.reads),
                            ("writes", runnable.writes),
                            ("calls", runnable.calls),
                            ("reads_variables", runnable.reads_variables),
                            ("writes_variables", runnable.writes_variables),
                        )
                    },
                }
            )
            for runnable in swc.runnables
        ]
        swc_update: dict[str, object] = {"runnables": runnables}
        if behavior_name is not None:
            swc_update["behavior_name"] = behavior_name
        # `model_copy` runs no validator.
        settled.append(
            replace(imported, swc=SWComponent.model_validate(swc.model_copy(update=swc_update).model_dump()))
        )
    return settled, why_not


# ---------------------------------------------------------------------------
# The reasons both tiers state, built in one place each, so a reword at one
# site never splits the file tier from the late pass.
# ---------------------------------------------------------------------------


def not_a_path_reason(member_ref: str) -> str:
    """The line of a member reference that is no AUTOSAR path."""
    return f"unresolved reference {member_ref} (not an AUTOSAR path)"


def not_a_member_reason(member_ref: str, member_phrase: str, port_name: str, interface_name: str) -> str:
    """The line of a member reference not under its port's interface, or naming a member it lacks.

    *member_phrase* is what the member is called, with its article (*an operation*).
    """
    return (
        f"unresolved reference {member_ref} (not {member_phrase} of port '{port_name}'s interface '{interface_name}')"
    )


def queued_read_by_value_reason(tag: str, member: str, port_name: str) -> SkipReason:
    """``[constr_1277]``'s line: a by-value read of a queued data element, imported as stated."""
    return SkipReason(
        f"{tag} reads queued data element '{member}' through port '{port_name}' by value; "
        "the read is imported as the file states it, and the model is one check-model refuses "
        "(rule:swc.access.queued-read-by-value)",
        SkipCategory.EXPORT_DEFECT,
        StandardRule.NO_QUEUED_RECEIVE_BY_VALUE,
    )


def late_member_miss(
    parent_path: str,
    raw_port: SwcPortInfo,
    member: str,
    members: Collection[str],
    unresolved: UnresolvedReferences | None,
) -> bool:
    """Whether a member reference is the file tier's missing reference, a late-pass verdict.

    Under a late-resolving policy only, and only for a reference under the
    port's own interface path whose member the file's interface lacks; any
    other shape keeps the mapper's refusal.
    """
    return (
        parent_path == raw_port.interface_ref
        and member not in members
        and unresolved is not None
        and unresolved.resolves_late
    )


# ---------------------------------------------------------------------------
# The data-received event.
# ---------------------------------------------------------------------------

#: The event kinds whose disabled modes the model carries; every other gets a line.
DISABLED_MODE_EVENT_KINDS = frozenset({"TIMING-EVENT", "DATA-RECEIVED-EVENT"})
#: The one ``DEST`` of a context port a data-received event binds: the abstract
#: kind the enumeration also admits is no port prototype a component declares.
_ABSTRACT_REQUIRED_PORT_DEST = "ABSTRACT-REQUIRED-PORT-PROTOTYPE"


def map_data_event(
    swc_name: str,
    event: SwcEventInfo,
    *,
    port_by_path: dict[str, PortOutcome],
    interface_by_name: dict[str, Interface],
    skip_log: SkipLog,
    swc_path: str,
    declared: Collection[str],
    unresolved: UnresolvedReferences | None = None,
    held: list[HeldStubRecord],
    misses: list[UnansweredRecord],
    runnable_name: str,
    position: int,
) -> DataEvent | None:
    """Bind one ``DATA-RECEIVED-EVENT`` by exact path, or skip it with a reason.

    :func:`map_operation_event`'s twin. Both halves of the ``DATA-IREF`` must be
    there; the context port's ``DEST`` must not be the abstract
    ``ABSTRACT-REQUIRED-PORT-PROTOTYPE``; the ``CONTEXT-R-PORT-REF`` must be the
    exact path of one of **this** component's ports that ``map_ports`` kept
    (else a chain-skip carrying the port's own reason). Then, in
    :func:`validator.data_event_records`' order:

    1. the port is a require port -- decided before the interface, so an event
       through a provide port lists no member on a stub;
    2. the port's interface is one this export declares; if the kept path may
       become an external interface stub, the element is staged and the event
       appended to *held* at *runnable_name*'s *position*;
    3. the interface is sender/receiver;
    4. ``TARGET-DATA-ELEMENT-REF`` is *the port's interface path* + the name of a
       data element that interface declares -- never a bare name. Under a
       late-resolving policy a reference naming an element the port's own
       interface lacks is appended to *misses* rather than refused.

    The disabled modes are read only once the event is kept, so a skipped
    event's modes take no second line. **Not decided here**, as ``check-model``
    leaves it silent: an event beside another kind, one repeated on a runnable,
    one on an element its runnable does not read, and the queued-split case of
    ``[SWS_Rte_07403]`` -- each imported as the file states it.
    """
    if not event.context_r_port_ref:
        skip_log.record(
            event.path,
            event.kind,
            "data-received event has no CONTEXT-R-PORT-REF in a DATA-IREF",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    if not event.target_data_element_ref:
        skip_log.record(
            event.path,
            event.kind,
            "data-received event has no TARGET-DATA-ELEMENT-REF in a DATA-IREF",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    if event.context_r_port_dest == _ABSTRACT_REQUIRED_PORT_DEST:
        skip_log.record(
            event.path,
            event.kind,
            f"data-received event names its context port with DEST {_ABSTRACT_REQUIRED_PORT_DEST}, which no "
            "concrete port prototype is (a data-received event needs an R-PORT-PROTOTYPE)",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    outcome = port_by_path.get(event.context_r_port_ref)
    if outcome is None:
        undeclared = undeclared_port_reason(
            "data-received event", event.context_r_port_ref, swc_name=swc_name, swc_path=swc_path, declared=declared
        )
        skip_log.record_reason(event.path, event.kind, undeclared, effect="abort")
        return None
    raw_port, port = outcome.raw, outcome.port
    if port is None:
        root = outcome.skip_entry
        skip_log.record_chained(
            event.path,
            event.kind,
            f"chain-skipped: port '{raw_port.name}' was skipped ({root.reason})",
            root=root,
        )
        return None
    if port.direction != "require":
        skip_log.record(
            event.path,
            event.kind,
            f"data-received event through {port.direction} port '{port.name}' "
            "(a data-received event needs a require port)",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    build = partial(_build_data_event, port=port.name, event=event, port_by_path=port_by_path)
    interface = required(port.interface, KEPT_INTERFACE)
    iface = interface_by_name.get(interface)
    if iface is None:
        reason = (
            f"data-received event through port '{port.name}' whose interface reference {interface} "
            "is not declared by this export"
        )
        stub_port, target_ref = port, event.target_data_element_ref  # the narrowed values the closure binds

        def once_stubbed(stub: str, port: Port = stub_port, ref: str = target_ref) -> str:
            return (
                f"data-received event through port '{port.name}' whose interface is the external interface stub "
                f"'{stub}' ({port.interface}): {unlisted_member_clause(ref)}"
            )

        if unresolved is None:
            skip_log.record(event.path, event.kind, reason, category=SkipCategory.NOT_SUPPORTED, effect="abort")
        elif unresolved.cause(interface) == CAUSE_ABSENT:
            # Under a late-resolving policy nothing behavioural lists a member, as for the access entries.
            if not unresolved.resolves_late:
                unresolved.stage_member(
                    interface,
                    kind="data_elements",
                    member_ref=event.target_data_element_ref,
                    port_path=raw_port.path,
                )
            held.append(
                HeldStubRecord(
                    target=interface,
                    kind="data_elements",
                    member_ref=event.target_data_element_ref,
                    runnable_name=runnable_name,
                    collection="events",
                    position=position,
                    port=port.name,
                    build=build,
                    path=event.path,
                    tag=event.kind,
                    name=event.name,
                    misdirected=None,
                    reason=reason,
                    once_stubbed=once_stubbed,
                )
            )
        else:
            chain_member_line(unresolved, skip_log, event.path, event.kind, reason, interface, once_stubbed)
        return None
    if iface.kind != "sender_receiver":
        skip_log.record(
            event.path,
            event.kind,
            f"data-received event through port '{port.name}' whose interface '{iface.name}' is "
            f"{INTERFACE_KIND_PROSE[iface.kind]} (a data-received event needs a "
            f"{INTERFACE_KIND_PROSE['sender_receiver']} interface)",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    _member_word, elements = interface_members(iface)
    parent_path, slash, element = event.target_data_element_ref.rpartition("/")
    if not slash:
        skip_log.record(
            event.path,
            event.kind,
            not_a_path_reason(event.target_data_element_ref),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    if late_member_miss(parent_path, raw_port, element, elements, unresolved):
        misses.append(
            UnansweredRecord(
                runnable_name=runnable_name,
                collection="events",
                port=port.name,
                member_name=element,
                is_operation=False,
                interface_name=iface.name,
                tag=event.kind,
            )
        )
        return None
    if parent_path != raw_port.interface_ref or element not in elements:
        skip_log.record(
            event.path,
            event.kind,
            not_a_member_reason(event.target_data_element_ref, "a data element", port.name, iface.name),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    return build(element, skip_log)


def _build_data_event(
    member: str, skip_log: SkipLog, *, port: str, event: SwcEventInfo, port_by_path: dict[str, PortOutcome]
) -> DataEvent:
    """A data-received event, built from its listed *member* -- at once, or once a stub lists it.

    Its disabled modes are read only here, so an event skipped before it is built
    takes no line for them; their lines go to *skip_log*, the building pass's log.
    """
    return DataEvent(
        type="data",
        port=port,
        element=member,
        disabled_modes=disabled_modes(event, port_by_path, skip_log),
    )


# ---------------------------------------------------------------------------
# Implicit access, asynchronous calls, their result points and returns events.
# ---------------------------------------------------------------------------

RETURNS_EVENT = "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT"


def queued_implicit_read_reason(member: str, port_name: str) -> SkipReason:
    """``[constr_2020]``'s line: an implicit read of a queued data element, imported as stated."""
    return SkipReason(
        f"DATA-READ-ACCESS reads queued data element '{member}' through port '{port_name}' implicitly; "
        "the read is imported as the file states it, and the model is one check-model refuses "
        "(rule:swc.access.queued-implicit-read)",
        SkipCategory.EXPORT_DEFECT,
        StandardRule.NO_QUEUED_IMPLICIT_READ,
    )


def implicit_read_enable_update_reason(member: str, port_name: str) -> SkipReason:
    """``[constr_1103]``'s line: an implicit read of an element whose port's com-spec states ``enableUpdate``."""
    return SkipReason(
        f"DATA-READ-ACCESS reads data element '{member}' through port '{port_name}' implicitly, and the port's "
        "com-spec states ENABLE-UPDATE true for it; the read is imported as the file states it, and the model "
        "is one check-model refuses (rule:swc.access.implicit-read-enable-update)",
        SkipCategory.EXPORT_DEFECT,
        StandardRule.NO_ENABLE_UPDATE_FOR_AN_IMPLICIT_READ,
    )


def _report_held_implicit_enable_update(
    admitted: Sequence[tuple[HeldStubRecord, str]], ports: Sequence[Port], skip_log: SkipLog
) -> None:
    """``[constr_1103]``'s line for each admitted implicit read whose port's com-spec states ``enable_update``.

    The held twin of the kept read's check: the read is admitted as the file
    states it and ``check-model`` refuses it
    (``rule:swc.access.implicit-read-enable-update``). A second admitted read of
    one ``(port, element)`` in one runnable collapses into the first, so it
    takes no line here.
    """
    by_name = {port.name: port for port in ports}
    seen: set[tuple[str, str, str, str]] = set()
    for held, member in admitted:
        key = (held.runnable_name, held.collection, held.port, member)
        if key in seen:
            continue
        seen.add(key)
        port = by_name.get(held.port)
        if not held.implicit_read or port is None:
            continue
        if any(entry.data_element == member and entry.enable_update is True for entry in port.com_specs):
            report_refused_combination(
                held.path, held.tag, implicit_read_enable_update_reason(member, held.port), skip_log
            )
