"""A runnable's accesses, call points, polls and returns events.

``map_runnable_accesses`` maps the data accesses and server call points of one
runnable onto the ``reads``, ``writes`` and ``calls`` lists of its SWCraft
``Runnable``; ``bind_polls`` and ``map_returns_event`` bind the result points of
the asynchronous calls and the returns events of one behaviour. ``swc_mapping``
imports nothing from here.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence, Sized
from dataclasses import dataclass, field
from functools import partial
from typing import Literal, NamedTuple

from swcraft_tools.arxml_import.swc_mapping import (
    KEPT_INTERFACE,
    RETURNS_EVENT,
    GeneratedSource,
    HeldStubRecord,
    PortOutcome,
    UnansweredRecord,
    chain_member_line,
    collapse_line,
    implicit_read_enable_update_reason,
    interface_members,
    late_member_miss,
    not_a_member_reason,
    not_a_path_reason,
    queued_element,
    queued_implicit_read_reason,
    queued_read_by_value_reason,
    report_refused_combination,
    undeclared_port_reason,
    unlisted_member_clause,
)
from swcraft_tools.arxml_import.unresolved import (
    CAUSE_ABSENT,
    UnresolvedReferences,
)
from swcraft_tools.arxml_reader import (
    ABORT,
    SkipCategory,
    SkipEntry,
    SkipLog,
    SwcAccessInfo,
    SwcEventInfo,
    SwcInfo,
    SwcPortInfo,
    SwcResultPointInfo,
    SwcRunnableInfo,
    parse_float_literal,
)
from swcraft_tools.guarantee import required
from swcraft_tools.schema import (
    ACCESS_LIST_NEEDS,
    INTERFACE_KIND_PROSE,
    CallReturnedEvent,
    DataReadRef,
    DataWriteRef,
    Interface,
    OperationCallRef,
    Port,
    Runnable,
)
from swcraft_tools.validator import (
    derived_result_point_name,
    runnables_phrase,
)

#: The reader's access containers, each onto the list on `Runnable` it fills.
#: An access failing `schema.ACCESS_LIST_NEEDS`, which `check-model` decides by
#: too, is skipped with a line, so a green import is a green model.
_ACCESS_CONTAINER_LIST: dict[str, str] = {
    "DATA-READ-ACCESSS": "reads",
    "DATA-RECEIVE-POINT-BY-ARGUMENTS": "reads",
    "DATA-RECEIVE-POINT-BY-VALUES": "reads",
    "DATA-SEND-POINTS": "writes",
    "DATA-WRITE-ACCESSS": "writes",
    "SERVER-CALL-POINTS": "calls",
}
#: The one container whose reads carry the `by: value` form.
_BY_VALUE_CONTAINER = "DATA-RECEIVE-POINT-BY-VALUES"
#: The `by` form each container gives; a container not named gives none (a read
#: by argument, an explicit send).
_WRITE_FORM: dict[str, Literal["implicit"]] = {"DATA-WRITE-ACCESSS": "implicit"}
_ACCESS_FORM: dict[str, Literal["value", "implicit"]] = {
    _BY_VALUE_CONTAINER: "value",
    "DATA-READ-ACCESSS": "implicit",
    **_WRITE_FORM,
}
#: The call point tag that makes a call `asynchronous`.
_ASYNCHRONOUS_CALL_POINT = "ASYNCHRONOUS-SERVER-CALL-POINT"
#: The ``DEST`` each new reference states when the source is the writer's shape.
_RESULT_POINT_DEST = "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT"
#: A call the source states with no ``TIMEOUT`` carries none; a synchronous one
#: is written with ``0``.
_SYNCHRONOUS_TIMEOUT = 0.0
#: The guarantee a returns event's chained line reads: the result point it names already has its line.
_RESULT_POINT_LINE = "_result_point_call records a line at every result point it carries no call for"


def _build_read(member: str, skip_log: SkipLog, *, port: str, by: Literal["value", "implicit"] | None) -> DataReadRef:
    """A held read's model entry, built from its listed *member* (:attr:`HeldStubRecord.build`; *skip_log* unused)."""
    return DataReadRef(port=port, element=member, by=by)


def _build_write(member: str, skip_log: SkipLog, *, port: str, by: Literal["implicit"] | None = None) -> DataWriteRef:
    """A held write's model entry, built from its listed *member* (*skip_log* unused)."""
    return DataWriteRef(port=port, element=member, by=by)


def _build_call(
    member: str, skip_log: SkipLog, *, port: str, asynchronous: bool = False, timeout: float | None = None
) -> OperationCallRef:
    """A held call's model entry, built from its listed *member* (*skip_log* unused).

    A held call is never polled: the result points are bound before the stub
    decision admits the call.
    """
    return OperationCallRef(port=port, operation=member, asynchronous=asynchronous, timeout=timeout)


@dataclass(frozen=True)
class _AccessScope:
    """What every access of one runnable shares while :func:`map_runnable_accesses` maps it.

    The binding is frozen; the containers are the caller's accumulators.
    """

    swc_name: str
    runnable_name: str
    swc_path: str
    declared: Collection[str]
    port_by_path: dict[str, PortOutcome]
    interface_by_name: dict[str, Interface]
    skip_log: SkipLog
    unresolved: UnresolvedReferences | None
    held: list[HeldStubRecord]
    misses: list[UnansweredRecord]
    call_points: dict[str, CallPoint] | None


@dataclass
class _MappedAccesses:
    """What the accesses of one runnable have filled so far: the three lists, the collapse lines and the sources.

    ``first_name_by_key`` keeps the first entry of each ``(list, port, member)``
    pair, which a repeat is collapsed into.
    """

    reads: list[DataReadRef] = field(default_factory=list)
    writes: list[DataWriteRef] = field(default_factory=list)
    calls: list[OperationCallRef] = field(default_factory=list)
    collapses: list[str] = field(default_factory=list)
    sources: list[GeneratedSource] = field(default_factory=list)
    first_name_by_key: dict[tuple[str, str, str], str] = field(default_factory=dict)
    # Each kept call's point by its pair, for a discarded duplicate to bind through.
    kept_calls: dict[tuple[str, str], CallPoint] = field(default_factory=dict)
    kept_by_list: dict[str, Sized] = field(init=False)

    def __post_init__(self) -> None:
        self.kept_by_list = {"reads": self.reads, "writes": self.writes, "calls": self.calls}


def _kept_access_port(
    scope: _AccessScope, access: SwcAccessInfo, list_name: str
) -> tuple[SwcPortInfo, Port, str] | None:
    """The raw port, kept `Port` and target reference of *access*, or ``None`` with the line that says why.

    The port must be one the SWC declares, by exact path, and one `map_swcs`
    kept; a skipped port is a chain-skip carrying its own reason.
    """
    skip_log = scope.skip_log
    if not access.port_ref:
        skip_log.record(
            access.path,
            access.tag,
            f"{list_name} access has no port reference",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    if not access.target_ref:
        skip_log.record(
            access.path,
            access.tag,
            f"{list_name} access has no target reference",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    outcome = scope.port_by_path.get(access.port_ref)
    if outcome is None:
        undeclared = undeclared_port_reason(
            f"{list_name} access",
            access.port_ref,
            swc_name=scope.swc_name,
            swc_path=scope.swc_path,
            declared=scope.declared,
        )
        skip_log.record_reason(access.path, access.tag, undeclared, effect="abort")
        return None
    raw_port, port = outcome.raw, outcome.port
    if port is None:
        root = outcome.skip_entry
        skip_log.record_chained(
            access.path,
            access.tag,
            f"chain-skipped: port '{raw_port.name}' was skipped ({root.reason})",
            root=root,
        )
        return None
    return raw_port, port, access.target_ref


def _hold_access_for_stub(
    scope: _AccessScope,
    mapped: _MappedAccesses,
    access: SwcAccessInfo,
    list_name: str,
    raw_port: SwcPortInfo,
    port: Port,
    interface: str,
    target_ref: str,
) -> None:
    """An access through a kept port whose interface the export declares nowhere: held for the stub decision, or a line.

    When the target may still become an external interface stub that lists the
    member, the entry is appended to ``scope.held``, never to the lists.
    Otherwise the line is recorded.
    """
    needed_kind, needed_direction = ACCESS_LIST_NEEDS[list_name]
    kept = mapped.kept_by_list[list_name]
    skip_log = scope.skip_log
    unresolved = scope.unresolved
    call_points = scope.call_points
    runnable_name = scope.runnable_name
    reason = (
        f"{list_name} access through port '{port.name}' whose interface reference "
        f"{interface} is not declared by this export"
    )

    def once_stubbed(stub: str, list_name: str = list_name, port: Port = port, ref: str = target_ref) -> str:
        return (
            f"{list_name} access through port '{port.name}' whose interface is the external "
            f"interface stub '{stub}' ({port.interface}): {unlisted_member_clause(ref)}"
        )

    if unresolved is None:
        skip_log.record(access.path, access.tag, reason, category=SkipCategory.NOT_SUPPORTED, effect="abort")
    elif unresolved.cause(interface) == CAUSE_ABSENT:
        kind = "operations" if needed_kind == "client_server" else "data_elements"
        # A dropped construct (wrong direction) lists no member. Under a
        # late-resolving policy the behaviour lists none either: it could
        # manufacture the evidence that admits it, so the com-specs decide.
        if port.direction == needed_direction and not unresolved.resolves_late:
            unresolved.stage_member(interface, kind=kind, member_ref=target_ref, port_path=raw_port.path)
        build: Callable[[str, SkipLog], DataReadRef | DataWriteRef | OperationCallRef]
        if list_name == "reads":
            build = partial(_build_read, port=port.name, by=_ACCESS_FORM.get(access.kind))
        elif list_name == "writes":
            build = partial(_build_write, port=port.name, by=_WRITE_FORM.get(access.kind))
        else:
            asynchronous = access.tag == _ASYNCHRONOUS_CALL_POINT
            build = partial(
                _build_call,
                port=port.name,
                asynchronous=asynchronous,
                timeout=_call_timeout(access, skip_log),
            )
            if call_points is not None:
                call_points[access.path] = CallPoint(
                    runnable_name,
                    port.name,
                    target_ref.rpartition("/")[2],
                    asynchronous,
                    access.name,
                    held=True,
                )
        scope.held.append(
            HeldStubRecord(
                target=interface,
                kind=kind,
                member_ref=target_ref,
                runnable_name=runnable_name,
                collection=list_name,
                position=len(kept),
                port=port.name,
                build=build,
                path=access.path,
                tag=access.tag,
                name=access.name,
                misdirected=(
                    f"{list_name} access through {port.direction} port '{port.name}' "
                    f"(a {list_name} entry needs a {needed_direction} port)"
                    if port.direction != needed_direction
                    else None
                ),
                reason=reason,
                once_stubbed=once_stubbed,
                by_value=access.kind == _BY_VALUE_CONTAINER,
                implicit_read=access.kind == "DATA-READ-ACCESSS",
            )
        )
    else:
        # Declared in a conflicting group or outside the selection: never a
        # stub; the policy decides the line's category.
        chain_member_line(unresolved, skip_log, access.path, access.tag, reason, interface, once_stubbed)


def _map_access(scope: _AccessScope, mapped: _MappedAccesses, access: SwcAccessInfo) -> None:
    """One access of a runnable: kept in its list, collapsed into an earlier entry, held, missed or skipped with a line.

    The checks run in the order :func:`map_runnable_accesses` states.
    """
    list_name = _ACCESS_CONTAINER_LIST[access.kind]
    needed_kind, needed_direction = ACCESS_LIST_NEEDS[list_name]
    kept = mapped.kept_by_list[list_name]
    skip_log = scope.skip_log
    kept_port = _kept_access_port(scope, access, list_name)
    if kept_port is None:
        return
    raw_port, port, target_ref = kept_port
    interface = required(port.interface, KEPT_INTERFACE)
    iface = scope.interface_by_name.get(interface)
    if iface is None:
        _hold_access_for_stub(scope, mapped, access, list_name, raw_port, port, interface, target_ref)
        return
    if iface.kind != needed_kind:
        skip_log.record(
            access.path,
            access.tag,
            f"{list_name} access through port '{port.name}' whose interface '{iface.name}' is "
            f"{INTERFACE_KIND_PROSE[iface.kind]} (a {list_name} entry needs a "
            f"{INTERFACE_KIND_PROSE[needed_kind]} interface)",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    if port.direction != needed_direction:
        skip_log.record(
            access.path,
            access.tag,
            f"{list_name} access through {port.direction} port '{port.name}' "
            f"(a {list_name} entry needs a {needed_direction} port)",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    member_word, members = interface_members(iface)
    parent_path, slash, member = target_ref.rpartition("/")
    if not slash:
        skip_log.record(
            access.path,
            access.tag,
            not_a_path_reason(target_ref),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    if late_member_miss(parent_path, raw_port, member, members, scope.unresolved):
        if list_name == "calls" and scope.call_points is not None:
            scope.call_points[access.path] = CallPoint(
                scope.runnable_name,
                port.name,
                member,
                access.tag == _ASYNCHRONOUS_CALL_POINT,
                access.name,
                held=True,
            )
        scope.misses.append(
            UnansweredRecord(
                runnable_name=scope.runnable_name,
                collection=list_name,
                port=port.name,
                member_name=member,
                is_operation=needed_kind == "client_server",
                interface_name=iface.name,
                tag=access.tag,
            )
        )
        return
    if parent_path != raw_port.interface_ref or member not in members:
        skip_log.record(
            access.path,
            access.tag,
            not_a_member_reason(target_ref, f"a {member_word}", port.name, iface.name),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    key = (list_name, port.name, member)
    first_name = mapped.first_name_by_key.get(key)
    if first_name is not None:
        mapped.collapses.append(
            collapse_line(
                scope.swc_name, scope.runnable_name, list_name, f"{port.name}.{member}", access.name, first_name
            )
        )
        if list_name == "calls" and scope.call_points is not None:
            # A result point naming the discarded duplicate binds to the kept
            # entry of the same runnable and pair.
            scope.call_points[access.path] = mapped.kept_calls[(port.name, member)]._replace(duplicate=access.name)
        return
    mapped.first_name_by_key[key] = access.name
    mapped.sources.append(
        GeneratedSource(
            scope.runnable_name,
            list_name,
            len(kept),
            access.path,
            access.tag,
            access.name,
        )
    )
    _keep_access(scope, mapped, access, list_name, iface, port, member)


def _keep_access(
    scope: _AccessScope,
    mapped: _MappedAccesses,
    access: SwcAccessInfo,
    list_name: str,
    iface: Interface,
    port: Port,
    member: str,
) -> None:
    """Append *access* to the list its container fills, with the lines for the combinations ``check-model`` refuses.

    A read by value of a queued element, an implicit read of one, and an implicit
    read of an element its port's com-spec states ``enable_update`` for each get
    their line. A call also records its point in *scope*'s ``call_points`` when
    it has one.
    """
    skip_log = scope.skip_log
    call_points = scope.call_points
    runnable_name = scope.runnable_name
    if list_name == "reads":
        by = _ACCESS_FORM.get(access.kind)
        mapped.reads.append(DataReadRef(port=port.name, element=member, by=by))
        if by == "value" and queued_element(iface, member):
            report_refused_combination(
                access.path, access.tag, queued_read_by_value_reason(access.tag, member, port.name), skip_log
            )
        if by == "implicit":
            if queued_element(iface, member):
                report_refused_combination(
                    access.path, access.tag, queued_implicit_read_reason(member, port.name), skip_log
                )
            if any(entry.data_element == member and entry.enable_update is True for entry in port.com_specs):
                report_refused_combination(
                    access.path, access.tag, implicit_read_enable_update_reason(member, port.name), skip_log
                )
    elif list_name == "writes":
        mapped.writes.append(DataWriteRef(port=port.name, element=member, by=_WRITE_FORM.get(access.kind)))
    else:
        asynchronous = access.tag == _ASYNCHRONOUS_CALL_POINT
        timeout = _call_timeout(access, skip_log)
        mapped.calls.append(
            OperationCallRef(port=port.name, operation=member, asynchronous=asynchronous, timeout=timeout)
        )
        mapped.kept_calls[(port.name, member)] = CallPoint(
            runnable_name,
            port.name,
            member,
            asynchronous,
            access.name,
            call_index=len(mapped.calls) - 1,
            timeout_value=timeout,
        )
        if call_points is not None:
            call_points[access.path] = mapped.kept_calls[(port.name, member)]


def map_runnable_accesses(
    swc_name: str,
    runnable_name: str,
    accesses: list[SwcAccessInfo],
    *,
    port_by_path: dict[str, PortOutcome],
    interface_by_name: dict[str, Interface],
    skip_log: SkipLog,
    swc_path: str,
    declared: Collection[str],
    unresolved: UnresolvedReferences | None = None,
    held: list[HeldStubRecord],
    misses: list[UnansweredRecord],
    call_points: dict[str, CallPoint] | None = None,
) -> tuple[list[DataReadRef], list[DataWriteRef], list[OperationCallRef], list[str], list[GeneratedSource]]:
    """Map one runnable's access points to its three lists.

    Every access resolves in order, else it is skipped with a line: it names a port and a target; the port is
    one the SWC declares, by exact path, and one `map_swcs` kept (else a chain-skip carrying the port's own
    reason); its interface's kind and then its direction are what the list needs (`ACCESS_LIST_NEEDS`, so an
    access wrong on both counts is skipped for the kind); and its TARGET-REF is the port's interface path plus a
    member of that interface, never a bare member name.

    A repeat of one `(port, member)` pair within one list keeps the first entry and reports the second as a
    lossy-transformation line in the returned collapses, never as a skip entry; `by` is no part of the identity.
    The implicit containers fill ``reads`` and ``writes`` with ``by: implicit``, and an
    ``ASYNCHRONOUS-SERVER-CALL-POINT`` fills ``calls`` with ``asynchronous: true`` and its stated ``TIMEOUT``. A
    kept read by value of a queued element (``[constr_1277]``), an implicit read of a queued element
    (``[constr_2020]``) and an implicit read of an element whose com-spec states ``enable_update``
    (``[constr_1103]``) are imported as stated, each with its line.

    An access through a kept port whose interface the export declares nowhere is appended to *held*, never to the
    lists. Under a late-resolving policy (``import component``) an access whose target sits under its port's own
    interface path and names a member that interface does not declare is appended to *misses* rather than refused.
    *call_points*, when given, receives every source call point of the runnable the mapper carried, held or
    collapsed.

    Returns:
        The three lists, the collapse lines, and one `GeneratedSource` per kept entry (the source access's path,
        tag and SHORT-NAME beside its list and index).
    """
    scope = _AccessScope(
        swc_name=swc_name,
        runnable_name=runnable_name,
        swc_path=swc_path,
        declared=declared,
        port_by_path=port_by_path,
        interface_by_name=interface_by_name,
        skip_log=skip_log,
        unresolved=unresolved,
        held=held,
        misses=misses,
        call_points=call_points,
    )
    mapped = _MappedAccesses()
    for access in accesses:
        _map_access(scope, mapped, access)
    return mapped.reads, mapped.writes, mapped.calls, mapped.collapses, mapped.sources


class CallPoint(NamedTuple):
    """What became of one source call point of a component, keyed by its path.

    ``duplicate`` is the discarded duplicate's own ``SHORT-NAME`` when this path
    was collapsed into the kept entry; ``held`` marks a call the stub decision or
    the late pass decides after the result points are bound, for which
    ``operation_name`` is the last step of the source reference.
    ``call_index`` is the kept entry's index in its runnable's ``calls`` (``None``
    for a held one).
    """

    runnable_name: str
    port_name: str
    operation_name: str
    is_asynchronous: bool
    name: str
    duplicate: str | None = None
    held: bool = False
    call_index: int = -1
    timeout_value: float | None = None


def _call_timeout(access: SwcAccessInfo, skip_log: SkipLog) -> float | None:
    """The ``timeout`` a call point carries, with a line for what it does not.

    An asynchronous point's stated ``TIMEOUT`` is carried exactly, ``0``
    included, and an absent one as absence. A text that reads as no non-negative
    number of seconds is one line, and the call is carried without it. A
    synchronous point carries none: the writer writes ``0``, so any other stated
    value is one line.
    """
    text = access.timeout_text
    if text is None:
        return None
    value = parse_float_literal(text)
    if access.tag != _ASYNCHRONOUS_CALL_POINT:
        if value != _SYNCHRONOUS_TIMEOUT:
            skip_log.record(
                access.path,
                "TIMEOUT",
                f"TIMEOUT {text} of a {access.tag} not carried: SWCraft writes a synchronous call point's TIMEOUT as 0",
                category=SkipCategory.NOT_SUPPORTED,
                effect=ABORT,
            )
        return None
    if value is None or value < 0:
        skip_log.record(
            access.path,
            "TIMEOUT",
            f"TIMEOUT {text} of an {access.tag} not carried: it reads as no non-negative number of seconds, "
            "and the call is imported with no timeout",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
        return None
    return value


def _result_point_root(path: str, skip_log: SkipLog, reader_log: SkipLog) -> SkipEntry | None:
    """The first entry recorded at *path*: the mapper's, then the reader's (a declaration the index dropped)."""
    return skip_log.first_at(path) or reader_log.first_at(path)


def _result_point_call(
    point: SwcResultPointInfo,
    *,
    call_points: Mapping[str, CallPoint],
    behaviour_path: str,
    skip_log: SkipLog,
    reader_log: SkipLog,
) -> CallPoint | None:
    """The carried asynchronous call *point* references by exact path, or ``None`` with the line.

    The reference must be stated, with the ``DEST`` the writer states or none,
    and name a call point of the same behaviour that the mapper carried: one it
    skipped chains the result point to that call point's entry; a held one is a
    line, because the result points are bound before it is decided; a
    synchronous one is a line, since only an asynchronous call has a result.
    Whose call it is is the caller's question.
    """
    ref = point.call_point_ref
    if ref is None or not ref.strip():
        skip_log.record(
            point.path,
            point.tag,
            "result point has no ASYNCHRONOUS-SERVER-CALL-POINT-REF",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
        return None
    if point.call_point_dest not in (None, _ASYNCHRONOUS_CALL_POINT):
        skip_log.record(
            point.path,
            point.tag,
            f"result point references {ref} with DEST {point.call_point_dest}: a result point names an "
            f"{_ASYNCHRONOUS_CALL_POINT}",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
        return None
    call = call_points.get(ref)
    if call is None:
        root = _result_point_root(ref, skip_log, reader_log)
        if root is not None:
            skip_log.record_chained(
                point.path,
                point.tag,
                f"chain-skipped: its call point {ref} was not carried ({root.reason})",
                root=root,
            )
        else:
            skip_log.record(
                point.path,
                point.tag,
                f"result point references {ref}, which is no call point of a runnable of its own behaviour "
                f"{behaviour_path}",
                category=SkipCategory.NOT_SUPPORTED,
                effect=ABORT,
            )
        return None
    if call.held:
        skip_log.record(
            point.path,
            point.tag,
            f"result point of the call point {ref} not carried: the call is through a port whose interface "
            "this export does not declare, decided after the result points are bound",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
        return None
    if not call.is_asynchronous:
        skip_log.record(
            point.path,
            point.tag,
            f"result point references {ref}, a SYNCHRONOUS-SERVER-CALL-POINT: only an asynchronous call has a "
            "result point",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
        return None
    return call


def _stated_result(point: SwcResultPointInfo, call: CallPoint) -> str | None:
    """The source's result point name where it differs from ``Result_{port}_{operation}``, else ``None``.

    :func:`swc_mapping.settle_stated_names` later withdraws one that is no legal
    identifier or would collide in the runnable's access point scope.
    """
    derived = derived_result_point_name(call.port_name, call.operation_name)
    return point.name if point.name != derived else None


def _bound_through_duplicate(swc_name: str, owner: str, point: SwcResultPointInfo, call: CallPoint) -> str:
    """The lossy-transformation line of a result point bound through a collapsed duplicate call point."""
    return (
        f"{swc_name}/{owner}: result point '{point.name or '<unnamed>'}' references call point "
        f"'{call.duplicate or '<unnamed>'}', collapsed into '{call.name or '<unnamed>'}' -- bound to it"
    )


def bind_polls(
    swc_name: str,
    raw: SwcInfo,
    *,
    kept: Sequence[SwcRunnableInfo],
    runnables_by_name: Mapping[str, Runnable],
    call_points: Mapping[str, CallPoint],
    behaviour_path: str,
    skip_log: SkipLog,
    reader_log: SkipLog,
    collapses: list[str],
    sources: list[GeneratedSource],
    taken: dict[tuple[str, int], _ResultTaker],
) -> None:
    """Carry every result point no returns event names as a poll of its own runnable's call.

    The call gets ``poll: true``, and the result point's source name is stated
    where it differs from ``Result_{port}_{operation}``. Each kept :class:`Runnable`
    is changed in place. The returns events are decided first
    (:func:`map_returns_event`); a result point one of them names is theirs.

    Recorded as a line: a result point of another runnable's call (not
    expressible); a second one of one call, which holds one poll; and one whose
    call point SWCraft collapsed into a kept call whose result is already *taken*
    (``rule:swc.call.several-results``). A second result point of the same call
    point is the file's own (``[constr_2006]``); a poll beside an event's is
    carried as stated and named by ``import model``'s finding pass.
    """
    named_by_an_event = {
        event.event_source_ref for event in raw.events if event.kind == RETURNS_EVENT and event.event_source_ref
    }
    polled: set[tuple[str, int]] = set()
    for r in kept:
        runnable = runnables_by_name[r.name]
        for point in r.result_points:
            if point.path in named_by_an_event:
                continue
            call = _result_point_call(
                point, call_points=call_points, behaviour_path=behaviour_path, skip_log=skip_log, reader_log=reader_log
            )
            if call is None:
                continue
            if call.runnable_name != r.name:
                skip_log.record(
                    point.path,
                    point.tag,
                    f"result point of runnable '{r.name}' references a call point of runnable "
                    f"'{call.runnable_name}' and no returns event names it: a poll of another runnable's call is "
                    "not expressible -- SWCraft models a poll on the calling runnable only",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect=ABORT,
                )
                continue
            index = call.call_index
            prior = taken.get((r.name, index))
            if prior is not None and prior.call_point_ref != point.call_point_ref:
                skip_log.record(
                    point.path,
                    point.tag,
                    _several_results_reason(f"result point '{point.name or '<unnamed>'}'", call, prior),
                    category=SkipCategory.NOT_SUPPORTED,
                    effect=ABORT,
                )
                continue
            if (r.name, index) in polled:
                skip_log.record(
                    point.path,
                    point.tag,
                    f"a second result point of the call {call.port_name}.{call.operation_name} on runnable "
                    f"'{r.name}' not carried: a call holds one poll",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect=ABORT,
                )
                continue
            polled.add((r.name, index))
            taken.setdefault(
                (r.name, index),
                _ResultTaker(point.call_point_ref, _call_point_name(call), f"the poll of result point '{point.name}'"),
            )
            # The settling pass later withdraws a stated name that is not admissible.
            runnable.calls[index] = OperationCallRef(
                port=call.port_name,
                operation=call.operation_name,
                asynchronous=True,
                poll=True,
                result=_stated_result(point, call),
                timeout=call.timeout_value,
            )
            if call.duplicate is not None:
                collapses.append(_bound_through_duplicate(swc_name, r.name, point, call))
            sources.append(GeneratedSource(r.name, "calls.result", index, point.path, point.tag, point.name))


#: The started runnable's name, the ``call_returned`` event and its result point.
_CarriedReturnsEvent = tuple[str, CallReturnedEvent, SwcResultPointInfo]


@dataclass(frozen=True)
class _ReturnsEventScope:
    """What every step of one returns event shares while :func:`map_returns_event` decides it.

    The binding is frozen; the containers are the caller's accumulators.
    """

    swc_name: str
    behaviour_path: str
    skipped_runnables: Mapping[str, SkipEntry]
    call_points: Mapping[str, CallPoint]
    binding: ReturnsEventBinding
    skip_log: SkipLog
    reader_log: SkipLog
    collapses: list[str]


def _refuse_returns_event(
    scope: _ReturnsEventScope,
    event: SwcEventInfo,
    target_name: str | None,
    named: tuple[SwcRunnableInfo, SwcResultPointInfo] | None,
) -> SkipEntry | None:
    """The line of a returns event that fails before its result point is looked at, or ``None`` when none does.

    In order: no ``START-ON-EVENT-REF``, a runnable the mapper skipped, a runnable
    the SWC lacks, disabled modes, no ``EVENT-SOURCE-REF``, a source of another
    ``DEST``, and a source that is no result point of the behaviour (*named* is
    ``None``), chained when the result point's own entry says why.
    """
    skip_log = scope.skip_log
    skipped_runnables = scope.skipped_runnables
    if not event.runnable_ref:
        return skip_log.record(
            event.path,
            event.kind,
            "returns event has no START-ON-EVENT-REF: the event a WaitPoint waits for ([TPS_SWCT_01347]) -- "
            "SWCraft models no wait point, so a blocking fetch of the result is not carried",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
    if event.runnable_ref in skipped_runnables:
        runnable_root = skipped_runnables[event.runnable_ref]
        return skip_log.record_chained(
            event.path,
            event.kind,
            f"chain-skipped: the runnable it starts, {event.runnable_ref}, was skipped ({runnable_root.reason})",
            root=runnable_root,
        )
    if target_name is None:
        return skip_log.record(
            event.path,
            event.kind,
            f"unresolved reference {event.runnable_ref!r} (runnable not found on SWC '{scope.swc_name}')",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
    if event.disabled_modes:
        return skip_log.record(
            event.path,
            event.kind,
            f"returns event states {len(event.disabled_modes)} disabled mode(s): a call_returned event holds "
            "none, and the event without them would start its runnable in modes the file disables",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
    if not event.event_source_ref or not event.event_source_ref.strip():
        return skip_log.record(
            event.path,
            event.kind,
            "returns event has no EVENT-SOURCE-REF",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
    if event.event_source_dest not in (None, _RESULT_POINT_DEST):
        return skip_log.record(
            event.path,
            event.kind,
            f"returns event references {event.event_source_ref} with DEST {event.event_source_dest}: its source "
            f"is an {_RESULT_POINT_DEST}",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
    if named is None:
        root = _result_point_root(event.event_source_ref, skip_log, scope.reader_log)
        if root is not None:
            return skip_log.record_chained(
                event.path,
                event.kind,
                f"chain-skipped: its result point {event.event_source_ref} was not carried ({root.reason})",
                root=root,
            )
        return skip_log.record(
            event.path,
            event.kind,
            f"returns event references {event.event_source_ref}, which is no result point of a runnable of its "
            f"own behaviour {scope.behaviour_path}",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
    return None


def _decide_named_returns_event(
    scope: _ReturnsEventScope,
    event: SwcEventInfo,
    target_name: str,
    named: tuple[SwcRunnableInfo, SwcResultPointInfo],
) -> tuple[SkipEntry | None, _CarriedReturnsEvent | None]:
    """The returns event whose source is a result point of the behaviour: its line, or the event it is carried as.

    The result point's runnable must be kept and be the one the event starts, and
    the point must be neither one an earlier event carried nor one whose own line
    is already recorded. A point with no carried call point records its chained
    line and returns ``(None, None)``.
    """
    skip_log = scope.skip_log
    binding = scope.binding
    owner, point = named
    if owner.path in scope.skipped_runnables:
        root = scope.skipped_runnables[owner.path]
        entry = skip_log.record_chained(
            event.path,
            event.kind,
            f"chain-skipped: its result point's runnable '{owner.name}' was skipped ({root.reason})",
            root=root,
        )
        return entry, None
    if owner.path != event.runnable_ref:
        entry = skip_log.record(
            event.path,
            event.kind,
            f"returns event starts runnable '{target_name}' and its result point belongs to runnable "
            f"'{owner.name}': SWCraft models the returns event that starts the runnable owning its result point",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
        return entry, None
    if point.path in binding.uncarried:
        root = required(skip_log.first_at(point.path), _RESULT_POINT_LINE)
        skip_log.record_chained(
            event.path,
            event.kind,
            f"chain-skipped: its result point '{point.name}' was not carried ({root.reason})",
            root=root,
        )
        return None, None
    if point.path in binding.bound:
        entry = skip_log.record(
            event.path,
            event.kind,
            f"a second returns event of the result point '{point.name}' not carried: a call_returned event "
            "states its own result point",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
        return entry, None
    return _bind_returns_event(scope, event, target_name, named)


def _bind_returns_event(
    scope: _ReturnsEventScope,
    event: SwcEventInfo,
    target_name: str,
    named: tuple[SwcRunnableInfo, SwcResultPointInfo],
) -> tuple[SkipEntry | None, _CarriedReturnsEvent | None]:
    """Bind a returns event to the call its result point references: the event, or the line that says why not.

    Two or more runnables calling its pair asynchronously make the event
    ambiguous, and a call whose result another call point of the collapsed pair
    already took cannot take a second one. A call reached through a discarded
    duplicate adds its collapse line.
    """
    skip_log = scope.skip_log
    binding = scope.binding
    owner, point = named
    call = _result_point_call(
        point,
        call_points=scope.call_points,
        behaviour_path=scope.behaviour_path,
        skip_log=skip_log,
        reader_log=scope.reader_log,
    )
    if call is None:
        root = required(skip_log.first_at(point.path), _RESULT_POINT_LINE)
        skip_log.record_chained(
            event.path,
            event.kind,
            f"chain-skipped: its result point '{point.name}' was not carried ({root.reason})",
            root=root,
        )
        binding.uncarried.add(point.path)
        return None, None
    callers = _asynchronous_callers(scope.call_points, call)
    prior = binding.taken.get((call.runnable_name, call.call_index))
    if len(callers) > 1:
        entry = skip_log.record(
            event.path,
            event.kind,
            _ambiguous_returns_event_reason(callers, call),
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
        return entry, None
    if prior is not None and prior.call_point_ref != point.call_point_ref:
        entry = skip_log.record(
            event.path,
            event.kind,
            _several_results_reason(f"its result point '{point.name}'", call, prior),
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )
        return entry, None
    binding.taken.setdefault(
        (call.runnable_name, call.call_index),
        _ResultTaker(
            point.call_point_ref,
            _call_point_name(call),
            f"the returns event '{event.name}' (result point '{point.name}')",
        ),
    )
    binding.bound.add(point.path)
    binding.failed.pop(point.path, None)
    if call.duplicate is not None:
        scope.collapses.append(_bound_through_duplicate(scope.swc_name, owner.name, point, call))
    carried = (
        target_name,
        CallReturnedEvent(
            type="call_returned",
            port=call.port_name,
            operation=call.operation_name,
            result=_stated_result(point, call),
        ),
        point,
    )
    return None, carried


def _chain_failed_result_point(
    scope: _ReturnsEventScope,
    event: SwcEventInfo,
    named: tuple[SwcRunnableInfo, SwcResultPointInfo],
    entry: SkipEntry,
) -> None:
    """Chain the result point of a skipped returns event to the first event that failed it, at the last event naming it.

    A point no event carries has no poll standing for it, so it takes a chained
    line of its own; an event skipped for a reason of its own does not take the
    point from a later one that is carried.
    """
    owner, point = named
    skip_log = scope.skip_log
    binding = scope.binding
    if owner.path not in scope.skipped_runnables and point.path not in binding.bound | binding.uncarried:
        binding.failed.setdefault(point.path, (entry, event.name))
        if not binding.remaining[point.path]:
            first, first_name = binding.failed.pop(point.path)
            skip_log.record_chained(
                point.path,
                point.tag,
                f"chain-skipped: its returns event '{first_name}' was not carried ({first.reason})",
                root=first,
            )


def map_returns_event(
    swc_name: str,
    event: SwcEventInfo,
    *,
    raw: SwcInfo,
    runnable_name_by_path: Mapping[str, str],
    skipped_runnables: Mapping[str, SkipEntry],
    call_points: Mapping[str, CallPoint],
    binding: ReturnsEventBinding,
    behaviour_path: str,
    skip_log: SkipLog,
    reader_log: SkipLog,
    collapses: list[str],
) -> tuple[str, CallReturnedEvent, SwcResultPointInfo] | None:
    """One ``ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT`` as a ``call_returned`` event, or ``None`` with its line.

    The event is carried on the runnable its ``START-ON-EVENT-REF`` names when
    its ``EVENT-SOURCE-REF`` resolves, by exact path, to a result point of that
    same runnable, and the result point's reference to a carried asynchronous
    call point of the same behaviour; its pair is that call point's.

    Skipped with a reason: no ``START-ON-EVENT-REF`` (a wait point's,
    ``[TPS_SWCT_01347]``); a runnable the mapper skipped (chained); disabled
    modes (the kind holds none); no or a dangling source; a result point of
    another runnable; a second event of a result point an earlier event carried;
    a pair two or more runnables call asynchronously
    (``swc.call-returned-event.ambiguous-call``: the event names the pair, not
    the caller); and a collapsed call whose result an earlier event already took
    (``swc.call.several-results``). A result point no event carries takes a
    chained line at the last event naming it, chained to the first that failed.

    Returns:
        The started runnable's name, the event and its result point.
    """
    named = binding.points.get(event.event_source_ref or "")
    if named is not None:
        binding.remaining[named[1].path] -= 1
    scope = _ReturnsEventScope(
        swc_name=swc_name,
        behaviour_path=behaviour_path,
        skipped_runnables=skipped_runnables,
        call_points=call_points,
        binding=binding,
        skip_log=skip_log,
        reader_log=reader_log,
        collapses=collapses,
    )
    carried: _CarriedReturnsEvent | None = None
    target_name = runnable_name_by_path.get(event.runnable_ref) if event.runnable_ref else None
    entry = _refuse_returns_event(scope, event, target_name, named)
    if entry is None and named is not None and target_name is not None:
        entry, carried = _decide_named_returns_event(scope, event, target_name, named)
    if entry is not None and named is not None:
        _chain_failed_result_point(scope, event, named, entry)
    return carried


@dataclass
class ReturnsEventBinding:
    """The returns events' state over one behaviour, for :func:`map_returns_event`.

    ``points`` is every result point by path (the first declaration wins);
    ``remaining`` the undecided returns events per result point. ``taken`` holds,
    per kept call ``(runnable, index)``, the result point that took its result
    first. ``failed`` holds, for a point neither bound nor uncarried, the first
    skipped event, until the last event naming the point decides the chain.
    """

    points: dict[str, tuple[SwcRunnableInfo, SwcResultPointInfo]]
    remaining: dict[str, int]
    bound: set[str] = field(default_factory=set)
    uncarried: set[str] = field(default_factory=set)
    failed: dict[str, tuple[SkipEntry, str]] = field(default_factory=dict)
    taken: dict[tuple[str, int], _ResultTaker] = field(default_factory=dict)

    @classmethod
    def of(cls, raw: SwcInfo) -> ReturnsEventBinding:
        """The binding of *raw*'s behaviour, before its first returns event is decided."""
        points: dict[str, tuple[SwcRunnableInfo, SwcResultPointInfo]] = {}
        for r in raw.runnables:
            for point in r.result_points:
                points.setdefault(point.path, (r, point))
        remaining: dict[str, int] = {}
        for event in raw.events:
            if event.kind == RETURNS_EVENT and event.event_source_ref in points:
                remaining[event.event_source_ref] = remaining.get(event.event_source_ref, 0) + 1
        return cls(points, remaining)


def _asynchronous_callers(call_points: Mapping[str, CallPoint], call: CallPoint) -> list[str]:
    """The runnables whose kept asynchronous call is *call*'s pair, in source order.

    A collapsed duplicate maps to its kept entry, so a runnable counts once; a
    held call is not counted, since its pair is decided after the returns events.
    """
    pair = (call.port_name, call.operation_name)
    return list(
        dict.fromkeys(
            point.runnable_name
            for point in call_points.values()
            if point.is_asynchronous and not point.held and (point.port_name, point.operation_name) == pair
        )
    )


def _ambiguous_returns_event_reason(callers: Sequence[str], call: CallPoint) -> str:
    """The line of a returns event whose call's pair *callers* all call asynchronously.

    ``[constr_2006]`` counts the result points of each call point, so the file
    is valid at both editions; the model's ``call_returned`` event names
    ``(port, operation)``, not the call point, so with two callers
    ``check-model`` refuses it (``swc.call-returned-event.ambiguous-call``).
    """
    return (
        f"{runnables_phrase(callers)} call {call.port_name}.{call.operation_name} asynchronously; "
        "a call_returned event names the pair, not the caller, "
        "so the event is not carried (rule:swc.call-returned-event.ambiguous-call)"
    )


class _ResultTaker(NamedTuple):
    """The result point that took a kept call's result first.

    ``taker`` is the words naming it in a line: a returns event with its result
    point, or a poll's result point.
    """

    call_point_ref: str | None
    call_point_name: str
    taker: str


def _call_point_name(call: CallPoint) -> str:
    """The ``SHORT-NAME`` of the call point *call* was reached through: a discarded duplicate's or the kept one's."""
    return call.duplicate or call.name or "<unnamed>"


def _several_results_reason(subject: str, call: CallPoint, prior: _ResultTaker) -> str:
    """The line of a result point whose call point SWCraft collapsed into a call whose result is taken.

    The source is valid: ``[constr_2006]`` counts the result points of each call
    point. SWCraft keeps one call per ``(port, operation)`` in a runnable, and the
    model's call holds one result point, so the second is not carried
    (``swc.call.several-results``). *subject* names the result point.
    """
    return (
        f"{subject} references call point '{_call_point_name(call)}' of {call.port_name}.{call.operation_name} on "
        f"runnable '{call.runnable_name}', which SWCraft collapsed with call point '{prior.call_point_name}' into "
        f"one call, whose result {prior.taker} already takes; the call holds one result point, so this one is not "
        "carried (rule:swc.call.several-results)"
    )


def stated_result_calls(raw: SwcInfo, call_points: Mapping[str, CallPoint]) -> frozenset[tuple[str, str, str]]:
    """Every carried call of *raw* a result point of the file references, ``(runnable, port, operation)``.

    A result point of any runnable, kept or skipped, counts; a collapsed
    duplicate maps to its kept entry.
    """
    stated: set[tuple[str, str, str]] = set()
    for r in raw.runnables:
        for point in r.result_points:
            call = call_points.get(point.call_point_ref or "")
            if call is not None:
                stated.add((call.runnable_name, call.port_name, call.operation_name))
    return frozenset(stated)
