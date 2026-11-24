"""The component exchange's capability registry: one entry per regime family, and the mechanism that walks them.

A **capability marker** (:mod:`swcraft_tools.capability_markers`) is an ``SD`` in the
``SWCRAFT`` group of a component export's ``SWC-INTERNAL-BEHAVIOR``: it says the exporter
*can* speak about a kind of content, so a file holding none of it means *none* and not
*could not say*. Each kind ``import component`` decides that way is one :class:`Capability`
in :data:`CAPABILITIES`.

Shared by every family: :func:`read_signal`, :func:`decide`, the divergence projections
(:func:`project_runnable`, :func:`project_swc`), the carries in :data:`CARRY_GROUPS`' order
(:func:`carry`), the conflict line's clauses (:func:`notes`) and the report's
``<HEADING> regime: <regime> -- <sentence>`` line. ``DATA-TYPES`` is a marker but not a capability.

This module raises nothing of its own: :func:`read_signal` returns the refusal line for the caller to raise,
and :func:`carry` lets the refusal of ``ctx.refuse_event_collisions`` pass.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .. import capability_markers as markers
from ..schema import (
    CallReturnedEvent,
    DataEvent,
    DataReadRef,
    DataWriteRef,
    MappingSet,
    OperationCallRef,
    OperationEvent,
    Runnable,
    SWComponent,
    VariableReadRef,
    VariableWriteRef,
)
from .report_lines import NONE as NONE
from .report_lines import ReportLine, ReportList, counted, unrecognized_marker_sentence

if TYPE_CHECKING:
    from ..arxml_reader import ImportModel
    from .report import ComponentImportReport

# ---------------------------------------------------------------------------
# The regimes
# ---------------------------------------------------------------------------

#: The export carries the family's SWCraft marker with the writer's own value, so it
#: is the truth for the family's content, absence included.
MARKED = "marked"

#: No marker, and the file states the family's content (a third-party export): what
#: it states is the truth, compared and replaced as a marked file's is. Three-state
#: families only (an entry with ``states``).
STATED = "stated"

#: No marker (and, for a three-state family, the file states none): the target's own
#: content is carried across an update.
CARRIED = "carried"

#: No marker, but the caller asked for the replacement explicitly
#: (``--replace-access-lists``): the access lists alone.
REPLACE_FLAG = "replace-flag"

# NONE (nothing decided anything) is defined in ``report_lines.py`` and imported above.


@dataclass(frozen=True)
class Signal:
    """What the file alone says about one family, read before anything is mapped."""

    #: :data:`MARKED`, :data:`REPLACE_FLAG` or :data:`CARRIED`. Not ``value``, for the reason
    #: ``capability_markers.Marker.text`` gives.
    regime: str
    #: An unrecognized marker value read as unmarked (``on_unrecognized="unmarked"``), else
    #: ``None``.
    unrecognized: str | None


@dataclass(frozen=True)
class Decision:
    """What the signal decides about one family, asked of the incoming component."""

    signal: Signal
    #: Component scope: whether the family is part of the divergence comparison.
    #: Runnable scope: whether it is compared for every runnable (``marked``).
    compared: bool
    #: Runnable scope: the incoming runnables compared although ``compared`` is
    #: ``False`` -- each one *speaks* (the entry's ``speaks``); empty for a
    #: component-scope family.
    speaking: frozenset[str]
    #: The regime an ``updated`` component's report names.
    regime: str


@dataclass(frozen=True)
class CarryContext:
    """What a carry needs of ``import_.py``, built there, so this module never imports it."""

    #: The mapping sets of the project the import would leave, the target's first, by name:
    #: the variables carry's input. A cached callable, so the index is built only when needed.
    mapping_sets: Callable[[], Mapping[str, MappingSet]]
    #: Raises when a carried event generates a name a runnable of the export takes; run after
    #: the events group.
    refuse_event_collisions: Callable[[SWComponent], None]


#: One carry group's hook: ``(existing, incoming, carried keys, ctx)`` to the incoming
#: component with the carried declarations in place and the report fields it fills.
CarryHook = Callable[[SWComponent, SWComponent, frozenset[str], CarryContext], tuple[SWComponent, dict[str, Any]]]


@dataclass(frozen=True)
class CarryGroup:
    """One carry: the families one traversal carries together, and its hook."""

    #: The families the group carries, each by its ``key``. Not ``members``: that is a contract
    #: field name, the reason ``Marker.text`` gives.
    families: tuple[Capability, ...]
    #: Run once when at least one family's regime is :data:`CARRIED`.
    hook: CarryHook


#: What a report hook returns: ``report.py``'s blocks, in the order they render.
Blocks = list[ReportLine | ReportList]


@dataclass(frozen=True)
class Capability:
    """One regime family of the component exchange: its marker, and what is genuinely its own."""

    #: The family's key in a ``decisions`` mapping.
    key: str
    #: The wire marker that selects ``marked``.
    marker: markers.Marker
    #: The report's heading for the family's regime line.
    heading: str
    #: The ``ComponentImportReport`` field holding the family's regime.
    regime_field: str
    #: Every ``ComponentImportReport`` field :attr:`blocks_before` and
    #: :attr:`blocks_after` read (:attr:`regime_field` excluded).
    report_fields: tuple[str, ...]
    #: What each regime says in a rendered import report, naming the signal that decided it.
    sentences: Mapping[str, str]
    #: Decided per component, or per runnable (the operation events).
    scope: Literal["component", "runnable"]
    #: What an unrecognized marker value does: read as unmarked and reported (the access lists,
    #: whose flag can overrule the carry), or refused (every other family: with no flag, an
    #: unrecognized value would decide silently which target declarations survive an update).
    on_unrecognized: Literal["unmarked", "refuse"]
    #: The refusal line's clause naming what the file cannot say, for ``"refuse"``.
    cannot_say: str | None
    #: Whether ``--replace-access-lists`` (given with ``--update``) selects
    #: :data:`REPLACE_FLAG` when the marker is not the writer's (the access lists).
    replace_flag: bool
    #: A three-state family's question: does the component state the content (which older
    #: exports could not)? ``None``: two states.
    states: Callable[[SWComponent], bool] | None
    #: A runnable-scope family's question: does the runnable state the content?
    speaks: Callable[[Runnable], bool] | None
    #: Whether the family is compared when ``decisions`` is ``None`` (the tests; no product
    #: caller). Every family but the access lists, which are nulled on both sides.
    default_compared: bool
    #: The family's share of a runnable's projection, as a ``model_copy`` update: its fields
    #: compared (``True``) or left out on **both** sides (``False``). The third argument is
    #: the update the earlier families built; a family filtering a field another one set
    #: (``events``) filters that value, so the filters compose in registry order.
    project_runnable: Callable[[Runnable, bool, Mapping[str, Any]], dict[str, Any]]
    #: The family's component-level fields of the projection, as a ``model_copy``
    #: update; ``None`` for a family with none.
    project_swc: Callable[[SWComponent, bool], dict[str, Any]] | None
    #: The conflict line's clause for the family, ``""`` for none.
    note: Callable[[Decision, SWComponent, SWComponent], str]
    #: The family's report lines rendered before its regime line.
    blocks_before: Callable[[ComponentImportReport], Blocks]
    #: The family's report lines rendered after its regime line: what its carry kept.
    blocks_after: Callable[[ComponentImportReport], Blocks]


# ---------------------------------------------------------------------------
# The per-family predicates
# ---------------------------------------------------------------------------


def _speaks_about_operation_events(runnable: Runnable) -> bool:
    """Whether *runnable* states an operation event or ``concurrent: true``.

    Older component exports emitted neither, so a runnable carrying either is not their
    silence.
    """
    return runnable.concurrent or any(isinstance(event, OperationEvent) for event in runnable.events)


def _states_variables(swc: SWComponent) -> bool:
    """Whether *swc* states a variable or a variable access, which no export before contract ``12.1.0`` could."""
    return bool(swc.variables) or any(r.reads_variables or r.writes_variables for r in swc.runnables)


def _states_areas(swc: SWComponent) -> bool:
    """Whether *swc* states an exclusive area or an area reference, which no export before contract ``12.2.0`` could."""
    return bool(swc.exclusive_areas) or any(r.can_enter or r.runs_inside for r in swc.runnables)


def _states_data_events(swc: SWComponent) -> bool:
    """Whether *swc* states a data-received event, which no export before contract ``12.3.0`` could."""
    return any(isinstance(event, DataEvent) for runnable in swc.runnables for event in runnable.events)


def _states_implicit_access(swc: SWComponent) -> bool:
    """Whether *swc* states an implicit read or write, which no export before contract ``12.7.0`` could."""
    return any(read.by == "implicit" for runnable in swc.runnables for read in runnable.reads) or any(
        write.by == "implicit" for runnable in swc.runnables for write in runnable.writes
    )


def _states_asynchronous_calls(swc: SWComponent) -> bool:
    """Whether *swc* states an asynchronous call or a ``call_returned`` event, which no export before ``12.7.0`` could.

    ``poll``, ``result`` and ``timeout`` need no clause of their own: the schema
    refuses each on a call that is not asynchronous.
    """
    return any(
        any(call.asynchronous for call in runnable.calls)
        or any(isinstance(event, CallReturnedEvent) for event in runnable.events)
        for runnable in swc.runnables
    )


# ---------------------------------------------------------------------------
# The projections (the divergence comparison's)
# ---------------------------------------------------------------------------


def _sorted_access(entries: list[Any], member_field: str) -> list[Any]:
    """One access list in a comparable order: ``(port, member, by)``.

    Neither the model nor the ARXML carries a declaration order for these lists, so sorting
    keeps "the same entries, listed differently" from reading as a divergence. ``by`` only
    makes the order total; two entries of one list cannot share a ``(port, member)`` pair.
    """
    return sorted(
        entries, key=lambda entry: (entry.port, getattr(entry, member_field), getattr(entry, "by", None) or "")
    )


def _by_variable(entry: VariableReadRef | VariableWriteRef) -> str:
    """A variable access's sort key: its variable, the list's identity, so the order is total."""
    return entry.variable


def _project_access_lists(runnable: Runnable, compared: bool, update: Mapping[str, Any]) -> dict[str, Any]:
    """The access lists' share of a runnable's projection.

    Compared under ``marked`` and ``replace-flag`` (sorted, so order is no difference).
    Otherwise, and by default, they are nulled on **both** sides, so an unmarked export that
    is otherwise identical stays a no-op instead of a conflict whose only exit deletes
    declarations it could not express.
    """
    if compared:
        return {
            "reads": _sorted_access(runnable.reads, "element"),
            "writes": _sorted_access(runnable.writes, "element"),
            "calls": _sorted_access(runnable.calls, "operation"),
        }
    return {"reads": [], "writes": [], "calls": []}


def _project_operation_events(runnable: Runnable, compared: bool, update: Mapping[str, Any]) -> dict[str, Any]:
    """The operation events' share of a runnable's projection.

    When *compared* the events stay; otherwise the operation events are dropped and ``concurrent``
    defaulted. Applied to both sides of the comparison.
    """
    own: dict[str, Any] = {
        "events": [e for e in update.get("events", runnable.events) if compared or not isinstance(e, OperationEvent)]
    }
    if not compared:
        own["concurrent"] = False
    return own


def _project_variable_accesses(runnable: Runnable, compared: bool, update: Mapping[str, Any]) -> dict[str, Any]:
    """The variables' share of a runnable's projection.

    Compared by default: the two variable access lists, sorted by variable. Otherwise both
    are emptied on **both** sides.
    """
    return {
        "reads_variables": sorted(runnable.reads_variables, key=_by_variable) if compared else [],
        "writes_variables": sorted(runnable.writes_variables, key=_by_variable) if compared else [],
    }


def _project_variables(swc: SWComponent, compared: bool) -> dict[str, Any]:
    """The variables' share of the component's projection.

    ``variables`` sorted by name (the writer emits the explicit wrapper before the implicit
    one, so a declaration order is not recoverable), compared or emptied on **both** sides.
    """
    return {"variables": sorted(swc.variables, key=lambda variable: variable.name) if compared else []}


def _project_area_references(runnable: Runnable, compared: bool, update: Mapping[str, Any]) -> dict[str, Any]:
    """The exclusive areas' share of a runnable's projection.

    Compared by default: ``can_enter`` and ``runs_inside`` in declared order, which the
    writer preserves. Otherwise both are emptied on **both** sides.
    """
    if compared:
        return {}
    return {"can_enter": [], "runs_inside": []}


def _project_areas(swc: SWComponent, compared: bool) -> dict[str, Any]:
    """The exclusive areas' share of the component's projection.

    ``exclusive_areas`` in declared order (which the writer preserves), or emptied on
    **both** sides.
    """
    return {"exclusive_areas": list(swc.exclusive_areas) if compared else []}


def _project_data_events(runnable: Runnable, compared: bool, update: Mapping[str, Any]) -> dict[str, Any]:
    """The data-received events' share of a runnable's projection.

    Compared by default, in their places in ``events`` (the writer preserves the order).
    Otherwise they are dropped from ``events`` on **both** sides, the other kinds keeping
    their order. Filters the ``events`` the operation-event family set (*update*).
    """
    return {"events": [e for e in update.get("events", runnable.events) if compared or not isinstance(e, DataEvent)]}


def _project_implicit_access(runnable: Runnable, compared: bool, update: Mapping[str, Any]) -> dict[str, Any]:
    """The implicit access forms' share of a runnable's projection.

    Compared by default: the form stays in the entries the access lists set (*update*).
    Otherwise an implicit read or write is projected in the explicit form (``by`` at its
    default) on **both** sides; a ``by: value`` read stays, since older exports could write
    it.
    """
    if compared:
        return {}
    return {
        "reads": [_explicit(entry) for entry in update.get("reads", runnable.reads)],
        "writes": [_explicit(entry) for entry in update.get("writes", runnable.writes)],
    }


def _explicit(entry: DataReadRef | DataWriteRef) -> DataReadRef | DataWriteRef:
    """*entry* in the explicit form older exports wrote, ``by: value`` kept."""
    return entry.model_copy(update={"by": None}) if entry.by == "implicit" else entry


#: The defaults a call's synchrony takes when it is left out of the comparison.
_SYNCHRONOUS = {"asynchronous": False, "poll": False, "result": None, "timeout": None}


def _project_asynchronous_calls(runnable: Runnable, compared: bool, update: Mapping[str, Any]) -> dict[str, Any]:
    """The asynchronous calls' share of a runnable's projection.

    Compared by default: each call's synchrony with ``poll``, ``result`` and ``timeout``, and
    the ``call_returned`` events in their places. Otherwise every call is projected
    synchronous and the ``call_returned`` events dropped, on **both** sides. Filters the
    ``calls`` and ``events`` the earlier families set (*update*).
    """
    if compared:
        return {}
    return {
        "calls": [call.model_copy(update=_SYNCHRONOUS) for call in update.get("calls", runnable.calls)],
        "events": [e for e in update.get("events", runnable.events) if not isinstance(e, CallReturnedEvent)],
    }


# ---------------------------------------------------------------------------
# The carries
# ---------------------------------------------------------------------------


def _carry_target_access_lists(
    existing: SWComponent, imported: SWComponent
) -> tuple[SWComponent, dict[str, dict[str, int]]]:
    """The ``memory`` rule one level down, for an ``--update`` replacement.

    Runs under the ``carried`` regime alone (no marker, no ``replace_access_lists``). An
    imported runnable whose ``reads``, ``writes`` and ``calls`` are all empty is *silent*,
    and silence from an exporter that cannot speak means "unknown", as a missing ``memory``
    does; replacing the target's runnable would erase declarations the source may not be
    able to express. So the target runnable's three lists are carried, matched by runnable
    NAME, whenever the export's runnable of that name carries none of them. An export
    runnable with any list non-empty is the truth for that runnable and replaces the
    target's through the preview. A target runnable the export no longer declares is
    removed with its lists, the preview naming it.

    Returns the imported SWC with the carried lists in place (before the preview is
    rendered, so it advertises no removal that is not happening) and, per runnable, the
    entries carried by list name. Carried entries are re-checked by ``check-model``, so an
    update that removes a port a carried entry names leaves a visible finding, not a silent
    drop.
    """
    target_runnables = {runnable.name: runnable for runnable in existing.runnables}
    runnables: list[Runnable] = []
    preserved: dict[str, dict[str, int]] = {}
    for runnable in imported.runnables:
        target = target_runnables.get(runnable.name)
        if target is None or runnable.reads or runnable.writes or runnable.calls:
            runnables.append(runnable)
            continue
        carried = {
            name: len(entries)
            for name, entries in (("reads", target.reads), ("writes", target.writes), ("calls", target.calls))
            if entries
        }
        if not carried:
            runnables.append(runnable)
            continue
        preserved[runnable.name] = carried
        runnables.append(
            runnable.model_copy(
                update={"reads": list(target.reads), "writes": list(target.writes), "calls": list(target.calls)}
            )
        )
    if not preserved:
        return imported, {}
    return imported.model_copy(update={"runnables": runnables}), preserved


def _carry_target_events(
    existing: SWComponent,
    imported: SWComponent,
    *,
    operation_events: bool,
    data_events: bool,
    call_returned_events: bool,
) -> tuple[SWComponent, dict[str, dict[str, int | bool]], dict[str, int], dict[str, int]]:
    """:func:`_carry_target_access_lists`' rule for operation, data-received and ``call_returned`` events.

    *operation_events*: a target runnable's operation events and ``concurrent`` are carried onto the
    export's runnable of that name when it is silent about both (:func:`_speaks_about_operation_events`).
    *data_events* and *call_returned_events*: every target runnable's events of that kind are carried.
    A carried ``call_returned`` event whose call the file no longer makes is kept and ``check-model``
    reports it (``swc.call-returned-event.unknown-call``). A target runnable the export no longer
    declares is removed. Carried events keep their places in the target's list; the export's events
    fill the other slots in order.

    Returns the imported SWC with the carried declarations in place and the per-runnable counts for
    ``preserved_operation_events``, ``preserved_data_events`` and ``preserved_call_returned_events``.
    Does **not** reach: an update that removes a carried event's port or member leaves a resolution
    finding ``check-model`` reports.
    """
    target_runnables = {runnable.name: runnable for runnable in existing.runnables}
    runnables: list[Runnable] = []
    preserved_operations: dict[str, dict[str, int | bool]] = {}
    preserved_data: dict[str, int] = {}
    preserved_returned: dict[str, int] = {}
    for runnable in imported.runnables:
        target = target_runnables.get(runnable.name)
        if target is None:
            runnables.append(runnable)
            continue
        carry_operations = operation_events and not _speaks_about_operation_events(runnable)
        kept: dict[str, int | bool] = {}
        if carry_operations:
            operation_count = sum(isinstance(event, OperationEvent) for event in target.events)
            if operation_count:
                kept["operation_events"] = operation_count
            if target.concurrent:
                kept["concurrent"] = True
        data_count = sum(isinstance(event, DataEvent) for event in target.events) if data_events else 0
        returned_count = (
            sum(isinstance(event, CallReturnedEvent) for event in target.events) if call_returned_events else 0
        )
        if not kept and not data_count and not returned_count:
            runnables.append(runnable)
            continue
        if kept:
            preserved_operations[runnable.name] = kept
        if data_count:
            preserved_data[runnable.name] = data_count
        if returned_count:
            preserved_returned[runnable.name] = returned_count
        incoming = iter(runnable.events)
        events: list[Any] = []
        for event in target.events:
            if (
                (carry_operations and isinstance(event, OperationEvent))
                or (data_events and isinstance(event, DataEvent))
                or (call_returned_events and isinstance(event, CallReturnedEvent))
            ):
                events.append(event)
            elif (following := next(incoming, None)) is not None:
                events.append(following)
        events.extend(incoming)
        update: dict[str, Any] = {"events": events}
        if carry_operations:
            update["concurrent"] = target.concurrent
        runnables.append(runnable.model_copy(update=update))
    if not preserved_operations and not preserved_data and not preserved_returned:
        return imported, {}, {}, {}
    return (
        imported.model_copy(update={"runnables": runnables}),
        preserved_operations,
        preserved_data,
        preserved_returned,
    )


def _carry_target_implicit_forms(
    existing: SWComponent, imported: SWComponent
) -> tuple[SWComponent, dict[str, dict[str, int]]]:
    """:func:`_carry_target_access_lists`' rule for the implicit form of reads and writes.

    Runs under the ``carried`` regime alone. Matched by runnable NAME and then by
    ``(port, element)``: the target's ``by: implicit`` is carried onto the export's entry of
    that pair when the file states no form. A ``by: value`` in the file is the file's
    statement and is not overwritten. A pair or runnable the file no longer lists takes its
    form with it, the preview naming the change.

    Returns the imported SWC with the carried forms in place (before the preview) and, per
    runnable, the entries carried by list name.
    """
    target_runnables = {runnable.name: runnable for runnable in existing.runnables}
    runnables: list[Runnable] = []
    preserved: dict[str, dict[str, int]] = {}
    for runnable in imported.runnables:
        target = target_runnables.get(runnable.name)
        if target is None:
            runnables.append(runnable)
            continue
        update: dict[str, Any] = {}
        carried: dict[str, int] = {}
        for name, own, file in (("reads", target.reads, runnable.reads), ("writes", target.writes, runnable.writes)):
            implicit = {(entry.port, entry.element) for entry in own if entry.by == "implicit"}
            entries = [
                entry.model_copy(update={"by": "implicit"})
                if entry.by is None and (entry.port, entry.element) in implicit
                else entry
                for entry in file
            ]
            count = sum(new is not old for new, old in zip(entries, file, strict=True))
            if count:
                update[name] = entries
                carried[name] = count
        if carried:
            preserved[runnable.name] = carried
            runnables.append(runnable.model_copy(update=update))
        else:
            runnables.append(runnable)
    if not preserved:
        return imported, {}
    return imported.model_copy(update={"runnables": runnables}), preserved


def _with_synchrony(call: OperationCallRef, own: OperationCallRef) -> OperationCallRef:
    """*call* with the synchrony of the target's call *own* of its pair: the four fields of an asynchronous call."""
    return call.model_copy(
        update={"asynchronous": own.asynchronous, "poll": own.poll, "result": own.result, "timeout": own.timeout}
    )


def _carry_target_synchrony(existing: SWComponent, imported: SWComponent) -> tuple[SWComponent, dict[str, int]]:
    """:func:`_carry_target_access_lists`' rule for a call's synchrony.

    Runs under the ``carried`` regime alone, so every call the file lists is synchronous.
    Matched by runnable NAME and then by ``(port, operation)``: the target's ``asynchronous``
    is carried onto the export's call, with ``poll``, ``result`` and ``timeout``. A pair or
    runnable the file no longer lists takes its synchrony with it. The ``call_returned``
    events are carried by the events group's one traversal, never by a second walk here.

    Returns the imported SWC with the carried synchrony in place (before the preview) and,
    per runnable, the number of calls made asynchronous.
    """
    target_runnables = {runnable.name: runnable for runnable in existing.runnables}
    runnables: list[Runnable] = []
    preserved: dict[str, int] = {}
    for runnable in imported.runnables:
        target = target_runnables.get(runnable.name)
        asynchronous = (
            {(call.port, call.operation): call for call in target.calls if call.asynchronous} if target else {}
        )
        calls = [
            _with_synchrony(call, asynchronous[(call.port, call.operation)])
            if (call.port, call.operation) in asynchronous and not call.asynchronous
            else call
            for call in runnable.calls
        ]
        count = sum(new is not old for new, old in zip(calls, runnable.calls, strict=True))
        if count:
            preserved[runnable.name] = count
            runnables.append(runnable.model_copy(update={"calls": calls}))
        else:
            runnables.append(runnable)
    if not preserved:
        return imported, {}
    return imported.model_copy(update={"runnables": runnables}), preserved


def _carry_target_variables(
    existing: SWComponent, imported: SWComponent, sets: Mapping[str, MappingSet]
) -> tuple[SWComponent, list[str], dict[str, dict[str, int]], list[str]]:
    """:func:`_carry_target_access_lists`' rule for the variables and the variable accesses.

    Runs under the ``carried`` regime alone. The target's ``variables`` are carried whole, in
    its order, and each target runnable's variable accesses onto the export's runnable of
    that NAME. A target runnable the export no longer declares is removed with its accesses;
    every carried access names a carried variable, so none is left dangling. The mapping sets
    the carried variables need come with them (:func:`carried_variable_mappings`); without
    them an application-typed variable would be unmapped, which ``check-model`` refuses and
    the writer will not build.

    Returns the imported SWC with the carried declarations in place (before the preview), the
    carried variable names, per runnable the carried entries by list name, and the carried
    ``data_type_mappings`` entries. What it does **not** reach: a carried variable's type is
    the target's own; a change to its definition is a shared-definition conflict of its own.
    """
    target_runnables = {runnable.name: runnable for runnable in existing.runnables}
    runnables: list[Runnable] = []
    accesses: dict[str, dict[str, int]] = {}
    for runnable in imported.runnables:
        target = target_runnables.get(runnable.name)
        if target is None or not (target.reads_variables or target.writes_variables):
            runnables.append(runnable)
            continue
        accesses[runnable.name] = {
            name: len(entries)
            for name, entries in (
                ("reads_variables", target.reads_variables),
                ("writes_variables", target.writes_variables),
            )
            if entries
        }
        runnables.append(
            runnable.model_copy(
                update={
                    "reads_variables": list(target.reads_variables),
                    "writes_variables": list(target.writes_variables),
                }
            )
        )
    variables = [variable.name for variable in existing.variables]
    if not variables and not accesses:
        return imported, [], {}, []
    mappings = carried_variable_mappings(existing, imported, sets)
    carried = imported.model_copy(
        update={
            "variables": list(existing.variables),
            "runnables": runnables,
            "data_type_mappings": [*imported.data_type_mappings, *mappings],
        }
    )
    return carried, variables, accesses, mappings


def carried_variable_mappings(
    existing: SWComponent, imported: SWComponent, sets: Mapping[str, MappingSet]
) -> list[str]:
    """The target's ``data_type_mappings`` entries the carried variables need and the file does not name.

    An entry of *existing* the file lacks is carried, in the target's order, when its set
    pairs an ``application_type`` a carried variable states and no set the file names pairs
    (the reading ``data-type.application-type-unmapped`` uses). *sets* is the project the
    import would leave, the target's sets first. An entry the target names for its ports
    alone is not carried: the file is the truth for the ports. A carried entry pairing a type
    the file's sets map differently is a double-mapping conflict, judged after every carry.
    """
    spoken = {variable.application_type for variable in existing.variables if variable.application_type is not None}

    def paired(names: Iterable[str]) -> set[str]:
        return {row.application for name in names if name in sets for row in sets[name].maps}

    needed = spoken - paired(imported.data_type_mappings)
    carried: list[str] = []
    for name in existing.data_type_mappings:
        if name in imported.data_type_mappings or name in carried or not paired([name]) & needed:
            continue
        carried.append(name)
        needed -= paired([name])
    return carried


def _carry_target_areas(
    existing: SWComponent, imported: SWComponent
) -> tuple[SWComponent, list[str], dict[str, dict[str, int]]]:
    """:func:`_carry_target_variables`' rule for the exclusive areas and their references.

    Runs under the ``carried`` regime alone. The target's ``exclusive_areas`` are carried
    whole, in its order, and each target runnable's ``can_enter`` and ``runs_inside`` onto the
    export's runnable of that NAME. Every carried reference names a carried area, so the
    result passes ``rule:swc.exclusive-area.unknown-area`` whenever the target did.

    Returns the imported SWC with the carried declarations in place (before the preview), the
    carried area names, and per runnable the carried entries by list name.
    """
    target_runnables = {runnable.name: runnable for runnable in existing.runnables}
    runnables: list[Runnable] = []
    references: dict[str, dict[str, int]] = {}
    for runnable in imported.runnables:
        target = target_runnables.get(runnable.name)
        if target is None or not (target.can_enter or target.runs_inside):
            runnables.append(runnable)
            continue
        references[runnable.name] = {
            name: len(entries)
            for name, entries in (("can_enter", target.can_enter), ("runs_inside", target.runs_inside))
            if entries
        }
        runnables.append(
            runnable.model_copy(update={"can_enter": list(target.can_enter), "runs_inside": list(target.runs_inside)})
        )
    areas = [area.name for area in existing.exclusive_areas]
    if not areas and not references:
        return imported, [], {}
    carried = imported.model_copy(update={"exclusive_areas": list(existing.exclusive_areas), "runnables": runnables})
    return carried, areas, references


def _carry_access_lists(
    existing: SWComponent, incoming: SWComponent, carried: frozenset[str], ctx: CarryContext
) -> tuple[SWComponent, dict[str, Any]]:
    """The access lists' group."""
    swc, preserved = _carry_target_access_lists(existing, incoming)
    return swc, {"preserved_access_lists": preserved}


def _carry_events(
    existing: SWComponent, incoming: SWComponent, carried: frozenset[str], ctx: CarryContext
) -> tuple[SWComponent, dict[str, Any]]:
    """The events group: operation, data-received and ``call_returned`` events in one traversal.

    A carried event generates an event name (``OIT_{port}_{operation}``,
    ``DRT_{port}_{element}``, ``ASRT_{port}_{operation}``) beside the export's runnables,
    which the earlier name checks never saw; a file adding a runnable of that name would be
    written and then refused by ``check-model`` and the writer, so the collision refusal
    (``ctx.refuse_event_collisions``) runs right after the traversal.
    """
    swc, operations, data, returned = _carry_target_events(
        existing,
        incoming,
        operation_events=OPERATION_EVENTS.key in carried,
        data_events=DATA_EVENTS.key in carried,
        call_returned_events=ASYNCHRONOUS_CALLS.key in carried,
    )
    ctx.refuse_event_collisions(swc)
    return swc, {
        "preserved_operation_events": operations,
        "preserved_data_events": data,
        "preserved_call_returned_events": returned,
    }


def _carry_variables(
    existing: SWComponent, incoming: SWComponent, carried: frozenset[str], ctx: CarryContext
) -> tuple[SWComponent, dict[str, Any]]:
    """The variables' group: the component's whole set, with the mapping sets it needs."""
    swc, variables, accesses, mappings = _carry_target_variables(existing, incoming, ctx.mapping_sets())
    return swc, {
        "preserved_variables": variables,
        "preserved_variable_accesses": accesses,
        "preserved_variable_mappings": mappings,
    }


def _carry_areas(
    existing: SWComponent, incoming: SWComponent, carried: frozenset[str], ctx: CarryContext
) -> tuple[SWComponent, dict[str, Any]]:
    """The exclusive areas' group: the areas and every ``can_enter`` / ``runs_inside``."""
    swc, areas, references = _carry_target_areas(existing, incoming)
    return swc, {"preserved_exclusive_areas": areas, "preserved_exclusive_area_references": references}


def _carry_implicit_forms(
    existing: SWComponent, incoming: SWComponent, carried: frozenset[str], ctx: CarryContext
) -> tuple[SWComponent, dict[str, Any]]:
    """The implicit access group: ``by: implicit`` per pair the file still lists."""
    swc, preserved = _carry_target_implicit_forms(existing, incoming)
    return swc, {"preserved_implicit_accesses": preserved}


def _carry_synchrony(
    existing: SWComponent, incoming: SWComponent, carried: frozenset[str], ctx: CarryContext
) -> tuple[SWComponent, dict[str, Any]]:
    """The synchrony group: ``asynchronous`` with ``poll``, ``result`` and ``timeout``."""
    swc, preserved = _carry_target_synchrony(existing, incoming)
    return swc, {"preserved_asynchronous_calls": preserved}


# ---------------------------------------------------------------------------
# The conflict line's clauses
# ---------------------------------------------------------------------------


_UNREPRESENTABLE_NOTE = (
    "the comparison already ignores fields the exported ARXML does not carry: "
    "a description the incoming export leaves empty, a value type's unit, "
    "data element init values the "
    "incoming export does not declare, a termination and restart handling it does not state, "
    "port order, "
    "memory budgets, declared safety integrity levels, declared measures, a port's "
    "observed interface, and "
    "the runnable access lists reads/writes/calls -- this is a real, "
    "representable difference"
)

#: The SWC conflict line's note under the two regimes that **compare** the access lists. A
#: separate sentence, because the note above claims the lists were ignored, which a marked
#: export makes false. Shared-definition lines keep the note above: an interface has no
#: runnable.
_COMPARED_ACCESS_NOTE = (
    "the comparison already ignores fields the exported ARXML does not carry: "
    "a description the incoming export leaves empty, a value type's unit, "
    "data element init values the "
    "incoming export does not declare, a termination and restart handling it does not state, "
    "port order, "
    "memory budgets, declared safety integrity levels, declared measures and a port's "
    "observed interface -- "
    "but the runnable access lists reads/writes/calls are "
    "compared, because {reason} -- this is a real, representable difference"
)

#: Why the lists were compared, per regime, for :data:`_COMPARED_ACCESS_NOTE`.
_COMPARED_ACCESS_REASONS = {
    MARKED: "the export carries the SWCraft access-point marker",
    REPLACE_FLAG: "--replace-access-lists was given",
}


def _swc_conflict_note(regime: str) -> str:
    """The SWC conflict line's parenthetical, for the regime in force."""
    if regime in _COMPARED_ACCESS_REASONS:
        return _COMPARED_ACCESS_NOTE.format(reason=_COMPARED_ACCESS_REASONS[regime])
    return _UNREPRESENTABLE_NOTE


def _operation_event_note(regime: str, existing: SWComponent, incoming: SWComponent) -> str:
    """The SWC conflict line's clause about operation events, when the comparison left them out.

    Empty under ``marked``, where they are compared, and whenever neither side
    declares an operation event or ``concurrent: true``, so a conflict line about a
    component that has none reads as it always did.
    """
    if regime != CARRIED:
        return ""
    if not any(_speaks_about_operation_events(r) for r in (*existing.runnables, *incoming.runnables)):
        return ""
    return (
        "; operation events and a runnable's concurrency are not compared either, because the export "
        "carries no SWCraft operation-events marker -- except on a runnable that declares an operation "
        "event or concurrent: true, which is compared and replaced"
    )


def _variable_note(compare_variables: bool, existing: SWComponent) -> str:
    """The SWC conflict line's clause about the variables, when the comparison left them out.

    Empty whenever they are compared, and when the project's component declares
    none, so a conflict line about a component without variables reads as it did.
    """
    if compare_variables or not _states_variables(existing):
        return ""
    return (
        "; inter-runnable variables and the runnables' variable accesses are not compared either, because "
        "the export carries no SWCraft inter-runnable variables marker and states none"
    )


def _area_note(compare_areas: bool, existing: SWComponent) -> str:
    """The SWC conflict line's clause about the exclusive areas, when the comparison left them out.

    Empty whenever they are compared, and when the project's component declares none,
    so a conflict line about a component without areas reads as it did.
    """
    if compare_areas or not _states_areas(existing):
        return ""
    return (
        "; exclusive areas and the runnables' can_enter and runs_inside are not compared either, because "
        "the export carries no SWCraft exclusive areas marker and states none"
    )


def _data_event_note(compare_data_events: bool, existing: SWComponent) -> str:
    """The SWC conflict line's clause about the data-received events, when the comparison left them out.

    Empty whenever they are compared, and when the project's component declares none,
    so a conflict line about a component without one reads as it did.
    """
    if compare_data_events or not _states_data_events(existing):
        return ""
    return (
        "; data-received events are not compared either, because the export carries no SWCraft "
        "data-received events marker and states none"
    )


def _implicit_access_note(compared: bool, existing: SWComponent) -> str:
    """The SWC conflict line's clause about the implicit form, when the comparison left it out.

    Empty whenever it is compared, and when the project's component states no implicit
    access, so a conflict line about a component without one reads as it did.
    """
    if compared or not _states_implicit_access(existing):
        return ""
    return (
        "; the implicit form of reads and writes is not compared either, because the export carries no "
        "SWCraft implicit access marker and states none"
    )


def _asynchronous_call_note(compared: bool, existing: SWComponent) -> str:
    """The SWC conflict line's clause about the asynchronous calls, when the comparison left them out.

    Empty whenever they are compared, and when the project's component states no
    asynchronous call and no ``call_returned`` event.
    """
    if compared or not _states_asynchronous_calls(existing):
        return ""
    return (
        "; asynchronous calls with their poll, result and timeout, and call_returned events, are not compared "
        "either, because the export carries no SWCraft asynchronous calls marker and states none"
    )


# ---------------------------------------------------------------------------
# The report's family lines
# ---------------------------------------------------------------------------


def _no_blocks(report: ComponentImportReport) -> Blocks:
    """A family with no line of its own on that side of its regime line."""
    return []


def _access_blocks_before(report: ComponentImportReport) -> Blocks:
    """The unrecognized marker line, before the access regime line."""
    blocks: Blocks = []
    if report.access_marker_value is not None:
        blocks.append(ReportLine(unrecognized_marker_sentence(report.access_marker_value)))
    return blocks


def _access_blocks_after(report: ComponentImportReport) -> Blocks:
    """What the ``carried`` access regime kept, per runnable and list."""
    blocks: Blocks = []
    if report.preserved_access_lists:
        blocks.append(
            ReportList(
                "ACCESS LISTS preserved from the target project (not carried by the source ARXML):",
                counted(report.preserved_access_lists),
            )
        )
    return blocks


def _operation_event_blocks_after(report: ComponentImportReport) -> Blocks:
    """What the ``carried`` operation-event regime kept, per runnable."""
    blocks: Blocks = []
    if report.preserved_operation_events:
        events = [
            f"{runnable}: "
            + ", ".join(
                f"{name}={str(value).lower() if isinstance(value, bool) else value}" for name, value in kept.items()
            )
            for runnable, kept in sorted(report.preserved_operation_events.items())
        ]
        blocks.append(
            ReportList("OPERATION EVENTS preserved from the target project (not carried by the source ARXML):", events)
        )
    return blocks


def _variable_blocks_after(report: ComponentImportReport) -> Blocks:
    """What the ``carried`` variable regime kept: the variables, the accesses and their mapping-set entries."""
    blocks: Blocks = []
    if report.preserved_variables:
        blocks.append(
            ReportLine(
                "VARIABLES preserved from the target project (not carried by the source ARXML): "
                + ", ".join(report.preserved_variables)
            )
        )
    if report.preserved_variable_accesses:
        blocks.append(
            ReportList(
                "VARIABLE ACCESSES preserved from the target project (not carried by the source ARXML):",
                counted(report.preserved_variable_accesses),
            )
        )
    if report.preserved_variable_mappings:
        blocks.append(
            ReportLine(
                "VARIABLES' MAPPING SETS preserved from the target project (not carried by the source ARXML): "
                + ", ".join(report.preserved_variable_mappings)
            )
        )
    return blocks


def _area_blocks_after(report: ComponentImportReport) -> Blocks:
    """What the ``carried`` area regime kept, then the written component's area count line, last."""
    blocks: Blocks = []
    if report.preserved_exclusive_areas:
        blocks.append(
            ReportLine(
                "EXCLUSIVE AREAS preserved from the target project (not carried by the source ARXML): "
                + ", ".join(report.preserved_exclusive_areas)
            )
        )
    if report.preserved_exclusive_area_references:
        blocks.append(
            ReportList(
                "EXCLUSIVE AREA REFERENCES preserved from the target project (not carried by the source ARXML):",
                counted(report.preserved_exclusive_area_references),
            )
        )
    if any(report.written_exclusive_areas):
        areas, references = report.written_exclusive_areas
        blocks.append(ReportLine(f"WRITTEN {areas} exclusive areas and {references} exclusive area references"))
    return blocks


def _data_event_blocks_after(report: ComponentImportReport) -> Blocks:
    """What the ``carried`` data-received event regime kept, per runnable."""
    blocks: Blocks = []
    if report.preserved_data_events:
        blocks.append(
            ReportList(
                "DATA-RECEIVED EVENTS preserved from the target project (not carried by the source ARXML):",
                [
                    f"{runnable}: data_events={count}"
                    for runnable, count in sorted(report.preserved_data_events.items())
                ],
            )
        )
    return blocks


def _implicit_access_blocks_after(report: ComponentImportReport) -> Blocks:
    """What the ``carried`` implicit access regime kept, per runnable and list."""
    blocks: Blocks = []
    if report.preserved_implicit_accesses:
        blocks.append(
            ReportList(
                "IMPLICIT ACCESS preserved from the target project (not carried by the source ARXML):",
                counted(report.preserved_implicit_accesses),
            )
        )
    return blocks


def _asynchronous_call_blocks_after(report: ComponentImportReport) -> Blocks:
    """What the ``carried`` asynchronous call regime kept: the synchrony, then the ``call_returned`` events."""
    blocks: Blocks = []
    if report.preserved_asynchronous_calls:
        blocks.append(
            ReportList(
                "ASYNCHRONOUS CALLS preserved from the target project (not carried by the source ARXML):",
                [
                    f"{runnable}: asynchronous_calls={count}"
                    for runnable, count in sorted(report.preserved_asynchronous_calls.items())
                ],
            )
        )
    if report.preserved_call_returned_events:
        blocks.append(
            ReportList(
                "CALL-RETURNED EVENTS preserved from the target project (not carried by the source ARXML):",
                [
                    f"{runnable}: call_returned_events={count}"
                    for runnable, count in sorted(report.preserved_call_returned_events.items())
                ],
            )
        )
    return blocks


# ---------------------------------------------------------------------------
# The entries
# ---------------------------------------------------------------------------

#: The access lists. The marker is a CAPABILITY token ("this exporter can speak about access
#: points"), never a claim that the component has any: a marked export with empty lists says
#: *none*.
#:
#: The VALUE decides, never the key's presence: a value SWCraft did not write says nothing
#: about what the exporter can express, and trusting it would let three empty lists delete the
#: target's declarations. Anything else is UNMARKED -- not a refusal, so one mistyped ``SD``
#: does not make an export un-importable; the report names what was found.
#:
#: The caller's flag is gated on ``update``: the projection it selects also feeds the
#: ``unchanged``/``conflict`` verdict, so an ungated flag would turn an ``unchanged`` into a
#: conflict. The MARKER is deliberately not gated: under ``marked`` the lists are compared, so
#: differing lists are a conflict ``--update`` resolves -- an exporter's statement about its
#: own content is evidence; a caller's flag about someone else's file is an instruction.
ACCESS_LISTS = Capability(
    key="access_lists",
    marker=markers.ACCESS_POINTS,
    heading="ACCESS LISTS",
    regime_field="access_list_regime",
    report_fields=("access_marker_value", "preserved_access_lists"),
    # A reader must tell "the export told us" from "you told us", and be told how to replace
    # lists that survived.
    sentences={
        MARKED: (
            "the export carries the SWCraft access-point marker, so its lists replaced the target's "
            "through the diff preview above"
        ),
        REPLACE_FLAG: (
            "the export carries no SWCraft access-point marker and --replace-access-lists was given, "
            "so its lists replaced the target's through the diff preview above"
        ),
        CARRIED: (
            "the export carries no SWCraft access-point marker, so it cannot be distinguished from one "
            "that could not express access points and the target's own lists were kept; "
            "pass --replace-access-lists to replace them"
        ),
    },
    scope="component",
    on_unrecognized="unmarked",
    cannot_say=None,
    replace_flag=True,
    states=None,
    speaks=None,
    default_compared=False,
    project_runnable=_project_access_lists,
    project_swc=None,
    note=lambda decision, existing, incoming: _swc_conflict_note(decision.signal.regime),
    blocks_before=_access_blocks_before,
    blocks_after=_access_blocks_after,
)

#: The operation events and ``concurrent``. No marker is ``carried``: older exports emitted no
#: ``OPERATION-INVOKED-EVENT`` and the constant ``false``, and other tools write no marker.
#: Decided per RUNNABLE: under ``carried`` a runnable that declares an operation event or
#: ``concurrent: true`` speaks for itself and is compared.
OPERATION_EVENTS = Capability(
    key="operation_events",
    marker=markers.OPERATION_EVENTS,
    heading="OPERATION EVENTS",
    regime_field="operation_event_regime",
    report_fields=("preserved_operation_events",),
    sentences={
        MARKED: (
            "the export carries the SWCraft operation-events marker, so its runnables' operation events "
            "and concurrency replaced the target's through the diff preview above"
        ),
        CARRIED: (
            "the export carries no SWCraft operation-events marker, so it cannot be distinguished from one "
            "written before operation events and concurrency could be exported, and the target's own were kept"
        ),
    },
    scope="runnable",
    on_unrecognized="refuse",
    cannot_say="whether a runnable without an operation event declares none",
    replace_flag=False,
    states=None,
    speaks=_speaks_about_operation_events,
    default_compared=True,
    project_runnable=_project_operation_events,
    project_swc=None,
    note=lambda decision, existing, incoming: _operation_event_note(decision.signal.regime, existing, incoming),
    blocks_before=_no_blocks,
    blocks_after=_operation_event_blocks_after,
)

#: The inter-runnable variables and the variable accesses. No marker is ``carried`` unless
#: the file states one (:func:`_states_variables`). Asked of the **component**: an access
#: names a variable of its own component, so a file that states variables states the set, and
#: carrying the target's accesses onto it could name variables the file does not declare.
VARIABLES = Capability(
    key="variables",
    marker=markers.INTER_RUNNABLE_VARIABLES,
    heading="VARIABLES",
    regime_field="variable_regime",
    report_fields=("preserved_variables", "preserved_variable_accesses", "preserved_variable_mappings"),
    sentences={
        MARKED: (
            "the export carries the SWCraft inter-runnable variables marker, so its variables and the "
            "runnables' variable accesses replaced the target's through the diff preview above"
        ),
        STATED: (
            "the export carries no SWCraft inter-runnable variables marker and states variables or variable "
            "accesses, so what it states replaced the target's through the diff preview above"
        ),
        CARRIED: (
            "the export carries no SWCraft inter-runnable variables marker and states no variable, so it cannot "
            "be distinguished from one written before variables could be exported, and the target's own were kept"
        ),
    },
    scope="component",
    on_unrecognized="refuse",
    cannot_say="whether a component without a variable declares none",
    replace_flag=False,
    states=_states_variables,
    speaks=None,
    default_compared=True,
    project_runnable=_project_variable_accesses,
    project_swc=_project_variables,
    note=lambda decision, existing, incoming: _variable_note(decision.compared, existing),
    blocks_before=_no_blocks,
    blocks_after=_variable_blocks_after,
)

#: The exclusive areas and each runnable's ``can_enter`` / ``runs_inside``, on the variables'
#: terms: a file that states areas states the set.
EXCLUSIVE_AREAS = Capability(
    key="exclusive_areas",
    marker=markers.EXCLUSIVE_AREAS,
    heading="EXCLUSIVE AREAS",
    regime_field="exclusive_area_regime",
    report_fields=("preserved_exclusive_areas", "preserved_exclusive_area_references", "written_exclusive_areas"),
    sentences={
        MARKED: (
            "the export carries the SWCraft exclusive areas marker, so its exclusive areas and the "
            "runnables' can_enter and runs_inside replaced the target's through the diff preview above"
        ),
        STATED: (
            "the export carries no SWCraft exclusive areas marker and states exclusive areas or references "
            "to them, so what it states replaced the target's through the diff preview above"
        ),
        CARRIED: (
            "the export carries no SWCraft exclusive areas marker and states no exclusive area, so it cannot "
            "be distinguished from one written before exclusive areas could be exported, and the target's own "
            "were kept"
        ),
    },
    scope="component",
    on_unrecognized="refuse",
    cannot_say="whether a component without an exclusive area declares none",
    replace_flag=False,
    states=_states_areas,
    speaks=None,
    default_compared=True,
    project_runnable=_project_area_references,
    project_swc=_project_areas,
    note=lambda decision, existing, incoming: _area_note(decision.compared, existing),
    blocks_before=_no_blocks,
    blocks_after=_area_blocks_after,
)

#: The data-received events: a file that states one on any runnable is the truth for every
#: runnable's. Asked of the **component**, unlike the operation events.
DATA_EVENTS = Capability(
    key="data_events",
    marker=markers.DATA_EVENTS,
    heading="DATA-RECEIVED EVENTS",
    regime_field="data_event_regime",
    report_fields=("preserved_data_events",),
    sentences={
        MARKED: (
            "the export carries the SWCraft data-received events marker, so its runnables' data-received "
            "events replaced the target's through the diff preview above"
        ),
        STATED: (
            "the export carries no SWCraft data-received events marker and states a data-received event, so "
            "what it states replaced every runnable's through the diff preview above"
        ),
        CARRIED: (
            "the export carries no SWCraft data-received events marker and states no data-received event, so "
            "it cannot be distinguished from one written before data-received events could be exported, and "
            "the target's own were kept"
        ),
    },
    scope="component",
    on_unrecognized="refuse",
    cannot_say="whether a runnable without a data-received event declares none",
    replace_flag=False,
    states=_states_data_events,
    speaks=None,
    default_compared=True,
    project_runnable=_project_data_events,
    project_swc=None,
    note=lambda decision, existing, incoming: _data_event_note(decision.compared, existing),
    blocks_before=_no_blocks,
    blocks_after=_data_event_blocks_after,
)

#: The implicit form of reads and writes: a file that states one anywhere is the truth for
#: every runnable's forms, asked of the **component**. The form belongs to an access the
#: access lists carry, so it is projected and carried inside the entries they set.
IMPLICIT_ACCESS = Capability(
    key="implicit_access",
    marker=markers.IMPLICIT_ACCESS,
    heading="IMPLICIT ACCESS",
    regime_field="implicit_access_regime",
    report_fields=("preserved_implicit_accesses",),
    sentences={
        MARKED: (
            "the export carries the SWCraft implicit access marker, so its runnables' implicit reads and writes "
            "replaced the target's through the diff preview above"
        ),
        STATED: (
            "the export carries no SWCraft implicit access marker and states an implicit read or write, so what "
            "it states replaced every runnable's through the diff preview above"
        ),
        CARRIED: (
            "the export carries no SWCraft implicit access marker and states no implicit read or write, so it "
            "cannot be distinguished from one written before the implicit form could be exported, and the "
            "target's own forms were kept"
        ),
    },
    scope="component",
    on_unrecognized="refuse",
    cannot_say="whether a read or a write in an explicit form is explicit",
    replace_flag=False,
    states=_states_implicit_access,
    speaks=None,
    default_compared=True,
    project_runnable=_project_implicit_access,
    project_swc=None,
    note=lambda decision, existing, incoming: _implicit_access_note(decision.compared, existing),
    blocks_before=_no_blocks,
    blocks_after=_implicit_access_blocks_after,
)

#: The asynchronous calls with ``poll``, ``result`` and ``timeout``, and the
#: ``call_returned`` events: a file that states either anywhere is the truth for every
#: runnable's, asked of the **component**. In two carry groups: the synchrony has its own
#: hook, and the events join the events group's one traversal.
ASYNCHRONOUS_CALLS = Capability(
    key="asynchronous_calls",
    marker=markers.ASYNCHRONOUS_CALLS,
    heading="ASYNCHRONOUS CALLS",
    regime_field="asynchronous_call_regime",
    report_fields=("preserved_asynchronous_calls", "preserved_call_returned_events"),
    sentences={
        MARKED: (
            "the export carries the SWCraft asynchronous calls marker, so its runnables' asynchronous calls and "
            "call_returned events replaced the target's through the diff preview above"
        ),
        STATED: (
            "the export carries no SWCraft asynchronous calls marker and states an asynchronous call or a "
            "call_returned event, so what it states replaced every runnable's through the diff preview above"
        ),
        CARRIED: (
            "the export carries no SWCraft asynchronous calls marker and states no asynchronous call and no "
            "call_returned event, so it cannot be distinguished from one written before asynchronous calls "
            "could be exported, and the target's own were kept"
        ),
    },
    scope="component",
    on_unrecognized="refuse",
    cannot_say="whether a synchronous call is synchronous and a runnable without a call_returned event declares none",
    replace_flag=False,
    states=_states_asynchronous_calls,
    speaks=None,
    default_compared=True,
    project_runnable=_project_asynchronous_calls,
    project_swc=None,
    note=lambda decision, existing, incoming: _asynchronous_call_note(decision.compared, existing),
    blocks_before=_no_blocks,
    blocks_after=_asynchronous_call_blocks_after,
)

#: Every family, in the order the signals are read (so the refusals are raised), the regime
#: lines rendered and the conflict clauses joined. A new family is appended.
CAPABILITIES: tuple[Capability, ...] = (
    ACCESS_LISTS,
    OPERATION_EVENTS,
    VARIABLES,
    EXCLUSIVE_AREAS,
    DATA_EVENTS,
    IMPLICIT_ACCESS,
    ASYNCHRONOUS_CALLS,
)

#: The carries, in the order they run: explicit, never derived, because the order is what an
#: update writes. The three event kinds are ONE group, since two sequential carries would
#: reorder a runnable's events. The implicit form and the synchrony run after the access
#: lists' carry whose entries they refine.
CARRY_GROUPS: tuple[CarryGroup, ...] = (
    CarryGroup((ACCESS_LISTS,), _carry_access_lists),
    CarryGroup((OPERATION_EVENTS, DATA_EVENTS, ASYNCHRONOUS_CALLS), _carry_events),
    CarryGroup((VARIABLES,), _carry_variables),
    CarryGroup((EXCLUSIVE_AREAS,), _carry_areas),
    CarryGroup((IMPLICIT_ACCESS,), _carry_implicit_forms),
    CarryGroup((ASYNCHRONOUS_CALLS,), _carry_synchrony),
)


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------


def read_signal(capability: Capability, model: ImportModel, *, replace_flag_given: bool) -> Signal | str:
    """Which regime the file selects for *capability*'s family, or the refusal line.

    Read off ``model.swcs[0]`` (the one component) before the target is read: it is a
    property of the incoming file alone, and governs anything only if the SWC is ``updated``.

    The writer's own value is :data:`MARKED`. Any other value is, for
    ``on_unrecognized="refuse"``, the refusal line built from ``cannot_say``, which the
    caller raises; for ``"unmarked"`` it is kept in :attr:`Signal.unrecognized` and read as no
    marker. With no usable marker the signal is :data:`REPLACE_FLAG` when the entry takes the
    flag and *replace_flag_given* (``replace_access_lists and update``), else :data:`CARRIED`.
    """
    value = model.swcs[0].swcraft_markers.get(capability.marker.sd_gid)
    if value == capability.marker.text:
        return Signal(MARKED, None)
    if value is not None and capability.on_unrecognized == "refuse":
        return (
            f"the component's internal behaviour carries the SWCraft {capability.marker.sd_gid} marker with the "
            f"value {value!r}, not {capability.marker.text!r}, so the file cannot say {capability.cannot_say}; "
            "export the component again with this release"
        )
    if capability.replace_flag and replace_flag_given:
        return Signal(REPLACE_FLAG, value)
    return Signal(CARRIED, value)


def decide(capability: Capability, signal: Signal, incoming: SWComponent) -> Decision:
    """What *signal* decides about *capability*'s family, asked of the *incoming* component.

    **Component scope.** Compared under any signal but :data:`CARRIED`, and under ``carried``
    exactly when the incoming component states the content (``states``): a file that speaks
    is the truth for what it states. The regime is the signal, or under ``carried``
    :data:`STATED` when compared and :data:`CARRIED` otherwise.

    **Runnable scope.** Compared for every runnable under ``marked``; under ``carried``, for
    the runnables that speak (``speaks``), named in :attr:`Decision.speaking`.
    """
    if capability.scope == "runnable":
        speaks = capability.speaks
        return Decision(
            signal=signal,
            compared=signal.regime == MARKED,
            speaking=frozenset(r.name for r in incoming.runnables if speaks is not None and speaks(r)),
            regime=signal.regime,
        )
    compared = signal.regime != CARRIED or (capability.states is not None and capability.states(incoming))
    if signal.regime != CARRIED:
        regime = signal.regime
    else:
        regime = STATED if compared else CARRIED
    return Decision(signal=signal, compared=compared, speaking=frozenset(), regime=regime)


def _compared(capability: Capability, decisions: Mapping[str, Decision] | None, runnable: str | None) -> bool:
    """Whether *capability*'s family is compared -- for *runnable*, when it names one.

    ``None`` *decisions* gives the entry's ``default_compared``.
    """
    if decisions is None:
        return capability.default_compared
    decision = decisions[capability.key]
    return decision.compared or (runnable is not None and runnable in decision.speaking)


def project_runnable(
    runnable: Runnable, decisions: Mapping[str, Decision] | None, base: Mapping[str, Any] | None = None
) -> Runnable:
    """*runnable* with every family's share of the projection applied, in :data:`CAPABILITIES` order.

    One ``model_copy``: *base* (the caller's own fields) and then each family's update merged
    in order. The families' fields are disjoint but for ``events``, which each event family
    filters from the value the one before it set.
    """
    update: dict[str, Any] = dict(base or {})
    for capability in CAPABILITIES:
        update.update(capability.project_runnable(runnable, _compared(capability, decisions, runnable.name), update))
    return runnable.model_copy(update=update)


def project_swc(swc: SWComponent, decisions: Mapping[str, Decision] | None) -> dict[str, Any]:
    """Every family's component-level fields of the projection, as one ``model_copy`` update."""
    update: dict[str, Any] = {}
    for capability in CAPABILITIES:
        if capability.project_swc is not None:
            update.update(capability.project_swc(swc, _compared(capability, decisions, None)))
    return update


def carry(
    decisions: Mapping[str, Decision], existing: SWComponent, incoming: SWComponent, ctx: CarryContext
) -> tuple[SWComponent, dict[str, Any]]:
    """Every carried family's declarations of *existing* onto *incoming*, in :data:`CARRY_GROUPS`' order.

    For an ``updated`` component whose behaviour is not kept. A group runs once when at least
    one member's regime is :data:`CARRIED`; what it kept is merged into the returned report
    fields. Runs before the preview, so it advertises no removal that is not happening.
    """
    fields: dict[str, Any] = {}
    swc = incoming
    for group in CARRY_GROUPS:
        carried = frozenset(c.key for c in group.families if decisions[c.key].regime == CARRIED)
        if carried:
            swc, kept = group.hook(existing, swc, carried, ctx)
            fields.update(kept)
    return swc, fields


def notes(decisions: Mapping[str, Decision], existing: SWComponent, incoming: SWComponent) -> str:
    """The SWC conflict line's family clauses, in :data:`CAPABILITIES` order (the access note first)."""
    return "".join(capability.note(decisions[capability.key], existing, incoming) for capability in CAPABILITIES)
