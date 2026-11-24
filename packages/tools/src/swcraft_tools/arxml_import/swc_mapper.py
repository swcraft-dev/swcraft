"""Mapping the software components of an export onto the model.

``map_swcs()`` maps every APPLICATION-SW-COMPONENT-TYPE the reader extracted onto a SWCraft
``SWComponent`` of the ``swc/`` layer, each paired with its ``group`` (``ImportedSwc``): its ports,
inter-runnable variables, exclusive areas, runnables with their accesses, and events. The
per-element policies it applies live in ``swc_mapping``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from swcraft_tools.arxml_import.merge import CONFLICT, MAP, MergeDecisions, declared_paths
from swcraft_tools.arxml_import.registry import PortInitKey, PortInitOutcome, VariableTypeOutcome
from swcraft_tools.arxml_import.runnable_accesses import (
    CallPoint,
    ReturnsEventBinding,
    bind_polls,
    map_returns_event,
    map_runnable_accesses,
    stated_result_calls,
)
from swcraft_tools.arxml_import.swc_mapping import (
    DISABLED_MODE_EVENT_KINDS,
    HANDLE_TERMINATION_AND_RESTART_VALUES,
    RETURNS_EVENT,
    BudgetPlan,
    EligibleBudget,
    GeneratedSource,
    HeldStubRecord,
    ImportedSwc,
    MappedAreas,
    MappedVariables,
    PortOutcome,
    UnansweredRecord,
    budget_records,
    build_interface_path_index,
    com_spec_member_misses,
    data_type_mappings,
    disabled_modes,
    held_stub_com_specs,
    late_ports,
    map_area_refs,
    map_data_event,
    map_exclusive_areas,
    map_local_accesses,
    map_operation_event,
    map_ports,
    map_variables,
    parent_package_name,
    plan_budgets,
    port_init_equivalents,
    port_init_labels,
    port_init_radix,
    record_budgets,
    report_disabled_mode,
    report_port_api_options,
    server_queue_length_losses,
    settle_runnable_symbols,
    stated_symbol,
    unreadable_time_value_reason,
)
from swcraft_tools.arxml_import.unresolved import UnresolvedReferences
from swcraft_tools.arxml_reader import (
    ImportModel,
    SkipCategory,
    SkipEntry,
    SkipLog,
    SkipReason,
    SwcAccessInfo,
    SwcAreaRefInfo,
    SwcEventInfo,
    SwcInfo,
    SwcLocalAccessInfo,
    SwcResultPointInfo,
    SwcRunnableInfo,
    blank_short_name,
    duplicate_name_reason,
    not_supported,
)
from swcraft_tools.schema import InitEvent, Interface, Port, Runnable, SWComponent, TimingEvent
from swcraft_tools.writer.mutations import check_path_segment


@dataclass(frozen=True)
class _SwcContext:
    """What :func:`map_swcs` reads and never changes: its arguments and the indices over the export."""

    model: ImportModel
    merges: MergeDecisions | None
    unresolved: UnresolvedReferences | None
    mapping_set_name_by_path: dict[str, str] | None
    port_inits: Mapping[PortInitKey, PortInitOutcome]
    variable_types: Mapping[str, VariableTypeOutcome]
    #: ARXML path -> emitted interface name, for the interfaces that survived.
    interface_path_to_name: dict[str, str]
    #: Where a port chained behind a skipped interface finds the interface's own entry:
    #: `map_interfaces`' log, then the reader's.
    interface_chain_logs: tuple[SkipLog, SkipLog]
    #: A kept port's interface by the name `Port.interface` carries, first declaration winning.
    interface_by_name: dict[str, Interface]
    declared: frozenset[str]
    #: Every `SWC-TIMING`, decided against the file before any runnable is built.
    budget_plan: BudgetPlan


@dataclass
class _SwcAccumulator:
    """What :func:`map_swcs` fills across the components, for the whole call."""

    swcs: list[ImportedSwc] = field(default_factory=list)
    skip_log: SkipLog = field(default_factory=SkipLog)
    seen_swc_names: dict[str, str] = field(default_factory=dict)
    created_paths: set[str] = field(default_factory=set)
    carried_runnable_paths: set[str] = field(default_factory=set)


@dataclass
class _SwcState:
    """One component's state while :func:`_map_swc` maps it; created anew for each component."""

    raw: SwcInfo
    ports: list[Port]
    port_by_path: dict[str, PortOutcome]
    mapped_variables: MappedVariables
    behaviour_variable_paths: frozenset[str]
    mapped_areas: MappedAreas
    access_collapses: list[str] = field(default_factory=list)
    generated_sources: list[GeneratedSource] = field(default_factory=list)
    #: What names a member of a target that may become an interface stub, held for the stub decision.
    held_records: list[HeldStubRecord] = field(default_factory=list)
    member_misses: list[UnansweredRecord] = field(default_factory=list)
    runnables_by_name: dict[str, Runnable] = field(default_factory=dict)
    runnable_name_by_path: dict[str, str] = field(default_factory=dict)
    seen_runnable_names: set[str] = field(default_factory=set)
    #: Every source call point the accesses carried, held or collapsed, by path.
    call_points: dict[str, CallPoint] = field(default_factory=dict)
    skipped_runnables: dict[str, SkipEntry] = field(default_factory=dict)
    #: The runnables kept, whose result points are bound after the events.
    kept_runnables: list[SwcRunnableInfo] = field(default_factory=list)
    #: Each kept runnable's stated SYMBOL, settled after the runnables.
    stated_symbols: dict[str, str | None] = field(default_factory=dict)
    why_unstated: dict[str, str] = field(default_factory=dict)
    carried_budgets: list[EligibleBudget] = field(default_factory=list)


def map_swcs(
    model: ImportModel,
    interfaces: list[Interface],
    iface_skip_log: SkipLog | None = None,
    merges: MergeDecisions | None = None,
    unresolved: UnresolvedReferences | None = None,
    mapping_set_name_by_path: dict[str, str] | None = None,
    *,
    port_inits: Mapping[PortInitKey, PortInitOutcome],
    variable_types: Mapping[str, VariableTypeOutcome],
) -> tuple[list[ImportedSwc], SkipLog]:
    """Map every APPLICATION-SW-COMPONENT-TYPE of *model* onto a ``SWComponent``.

    The components are mapped in declaration order, each paired with its ``group``
    (``ImportedSwc``); each ``SWC-TIMING`` is then recorded against the components and
    runnables kept.

    Args:
        model: the reader's model of the export.
        interfaces: the interfaces already mapped, which port references resolve through.
        iface_skip_log: ``map_interfaces``' own skip log for this model, which tells which
            raw interface declarations survived. ``None`` is an empty log.
        merges: the merge verdict for this model; ``None`` decides nothing.
        unresolved: the record of references the export does not resolve, or ``None``.
        mapping_set_name_by_path: the emitted name of each mapping set by its path.
        port_inits: ``map_interfaces``' decision on each port com-spec that initializes its
            element with a value of its own; empty when there is nothing to carry.
        variable_types: ``map_variable_types``' decision on each inter-runnable variable's
            type and value. An empty mapping skips every variable with a reason.

    Returns:
        (swcs, skip_log).

    Raises:
        pydantic.ValidationError: a mapped element fails schema validation (not expected
            for well-formed ARXML content).
    """
    acc = _SwcAccumulator()
    ctx = _swc_context(
        model,
        interfaces,
        iface_skip_log,
        merges,
        unresolved,
        mapping_set_name_by_path,
        port_inits=port_inits,
        variable_types=variable_types,
    )
    for raw in model.swcs:
        _map_swc(ctx, acc, raw)

    record_budgets(ctx.budget_plan, acc.created_paths, acc.carried_runnable_paths, acc.skip_log)
    return acc.swcs, acc.skip_log


def _swc_context(
    model: ImportModel,
    interfaces: list[Interface],
    iface_skip_log: SkipLog | None,
    merges: MergeDecisions | None,
    unresolved: UnresolvedReferences | None,
    mapping_set_name_by_path: dict[str, str] | None,
    *,
    port_inits: Mapping[PortInitKey, PortInitOutcome],
    variable_types: Mapping[str, VariableTypeOutcome],
) -> _SwcContext:
    """Build the lookups :func:`map_swcs` reads for every component, once per call."""
    interface_log = iface_skip_log if iface_skip_log is not None else SkipLog()
    interface_path_to_name = build_interface_path_index(model, interfaces, interface_log, merges)
    interface_chain_logs = (interface_log, model.skip_log)
    # Built once per call, never per access: a per-access rebuild is quadratic on a large export.
    interface_by_name: dict[str, Interface] = {}
    for iface in interfaces:
        interface_by_name.setdefault(iface.name, iface)
    # Tells whether a reference the mapper cannot answer points at content the file lacks.
    declared = declared_paths(model)
    budget_plan = plan_budgets(model, declared)
    return _SwcContext(
        model=model,
        merges=merges,
        unresolved=unresolved,
        mapping_set_name_by_path=mapping_set_name_by_path,
        port_inits=port_inits,
        variable_types=variable_types,
        interface_path_to_name=interface_path_to_name,
        interface_chain_logs=interface_chain_logs,
        interface_by_name=interface_by_name,
        declared=declared,
        budget_plan=budget_plan,
    )


def _map_swc(ctx: _SwcContext, acc: _SwcAccumulator, raw: SwcInfo) -> None:
    """Map one component: claim its name and group, then its ports, runnables and events, then keep it."""
    group = _claim_swc(ctx, acc, raw)
    if group is None:
        return
    state = _swc_state(ctx, acc, raw)
    _map_swc_runnables(ctx, acc, state)
    _map_swc_events(ctx, acc, state)
    _keep_imported_swc(ctx, acc, state, group)


def _claim_swc(ctx: _SwcContext, acc: _SwcAccumulator, raw: SwcInfo) -> str | None:
    """Claim *raw*'s name and return its ``group``, or ``None`` when the component is not mapped.

    A blank SHORT-NAME skips the component. Under a merge verdict, a component of a
    conflicting group is not mapped and its name stays claimed, and a component the
    verdict does not map is passed over. A repeated name (a flat namespace) is skipped
    with a reason, first wins; nothing is renamed.

    ``group``, the parent AR-PACKAGE SHORT-NAME, must pass ``check_path_segment`` (no
    ``/``, ``\\`` or ``..``): no schema validator covers it and ``run_import`` writes under
    ``root / "swc" / group``. A rejected group skips the whole component.
    """
    skip_log = acc.skip_log
    seen_swc_names = acc.seen_swc_names
    merges = ctx.merges
    if not raw.name or not raw.name.strip():
        skip_log.record_reason(raw.path, "APPLICATION-SW-COMPONENT-TYPE", blank_short_name(), effect="abort")
        return None
    if merges is not None:
        outcome = merges.outcome(raw.path)
        if outcome == CONFLICT:
            # A conflicting group imports neither declaration and keeps the name claimed.
            seen_swc_names.setdefault(raw.name, raw.path)
            return None
        if outcome != MAP:
            return None
    if raw.name in seen_swc_names:
        skip_log.record(
            raw.path,
            "APPLICATION-SW-COMPONENT-TYPE",
            f"duplicate SWC name '{raw.name}' (already declared at {seen_swc_names[raw.name]})",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    seen_swc_names[raw.name] = raw.path

    # Checked before any port or runnable work, so every consumer excludes the component, not only the write.
    group = parent_package_name(raw.path)
    try:
        check_path_segment(group, "group")
    except ValueError:
        skip_log.record(
            raw.path,
            "APPLICATION-SW-COMPONENT-TYPE",
            f"group name {group!r} (from parent AR-PACKAGE SHORT-NAME) would escape the model root -- SWC skipped",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return None
    return group


def _swc_state(ctx: _SwcContext, acc: _SwcAccumulator, raw: SwcInfo) -> _SwcState:
    """Map *raw*'s ports, inter-runnable variables and exclusive areas, the state its runnables read.

    A P-PORT-PROTOTYPE becomes a provide ``Port`` and an R-PORT-PROTOTYPE a require one. A
    PR-PORT-PROTOTYPE, a port whose interface reference does not resolve and a duplicate
    port name (first declaration wins) each skip that one port; the component is kept.
    """
    model = ctx.model
    skip_log = acc.skip_log
    ports, port_by_path = map_ports(
        raw.name,
        raw.ports,
        ctx.interface_path_to_name,
        skip_log,
        ctx.unresolved,
        variant_rich=model.variant_rich,
        declared=ctx.declared,
        chain_logs=ctx.interface_chain_logs,
        interface_by_name=ctx.interface_by_name,
        port_inits=ctx.port_inits,
    )
    report_port_api_options(raw, skip_log, model.skip_log)
    # The variables and areas go before the runnables whose accesses name them.
    mapped_variables = map_variables(raw, ctx.variable_types, ctx.unresolved, skip_log)
    behaviour_variable_paths = frozenset(variable.path for variable in raw.variables)
    mapped_areas = map_exclusive_areas(raw, skip_log)
    return _SwcState(
        raw=raw,
        ports=ports,
        port_by_path=port_by_path,
        mapped_variables=mapped_variables,
        behaviour_variable_paths=behaviour_variable_paths,
        mapped_areas=mapped_areas,
    )


def _map_swc_runnables(ctx: _SwcContext, acc: _SwcAccumulator, state: _SwcState) -> None:
    """Map every RUNNABLE-ENTITY of the component, then settle the SYMBOL each kept runnable carries.

    A kept runnable carries the SYMBOL it states where that differs from its name, unless
    two runnables would be written with one.
    """
    raw = state.raw
    for runnable in raw.runnables:
        min_start_interval = runnable.min_start_interval or 0.0
        runnable_skip = _claim_runnable(ctx, state, runnable, min_start_interval)
        if runnable_skip is not None:
            _skip_runnable(acc, state, runnable, runnable_skip)
            continue
        _map_runnable(ctx, acc, state, runnable, min_start_interval)

    settled = settle_runnable_symbols(
        raw.name,
        state.stated_symbols,
        state.why_unstated,
        {name: path for path, name in state.runnable_name_by_path.items()},
        acc.skip_log,
    )
    for name, symbol in settled.items():
        if symbol is not None:
            state.runnables_by_name[name] = state.runnables_by_name[name].model_copy(update={"symbol": symbol})


def _claim_runnable(
    ctx: _SwcContext, state: _SwcState, runnable: SwcRunnableInfo, min_start_interval: float
) -> SkipReason | None:
    """Claim *runnable*'s name and return why it is skipped, or ``None`` when it is kept.

    A second runnable of one name in one behaviour is a duplicate (``[constr_2508]``,
    unless the file is variant-rich). An unreadable MINIMUM-START-INTERVAL, or one that is
    not finite or is negative, skips the runnable: the model's ``min_start_interval``
    cannot be absent.
    """
    raw = state.raw
    runnable_skip: SkipReason | None = None
    if runnable.name in state.seen_runnable_names:
        # Two runnables of one name in one behaviour share its name
        # space: `[constr_2508]`, unless the file is variant-rich.
        runnable_skip = duplicate_name_reason(
            f"duplicate runnable name '{runnable.name}' on SWC '{raw.name}'", variant_rich=ctx.model.variant_rich
        )
    else:
        # Claimed before the interval check: a later same-named declaration is a
        # duplicate whether or not this one survives.
        state.seen_runnable_names.add(runnable.name)
        if runnable.unreadable_min_start_interval is not None:
            # An unreadable text is never the absent element's 0, and the model's value cannot be absent.
            runnable_skip = not_supported(
                unreadable_time_value_reason(
                    "MINIMUM-START-INTERVAL", runnable.unreadable_min_start_interval, "runnable"
                )
            )
        elif not math.isfinite(min_start_interval) or min_start_interval < 0:
            # A bare `< 0` comparison never catches NaN.
            runnable_skip = not_supported(f"MIN-START-INTERVAL must be >= 0 and finite, got {min_start_interval}")
    return runnable_skip


def _skip_runnable(
    acc: _SwcAccumulator, state: _SwcState, runnable: SwcRunnableInfo, runnable_skip: SkipReason
) -> None:
    """Record *runnable*'s skip, and chain-skip each of its accesses, area references and result points to it.

    Each chained entry inherits the runnable's category and rule, one entry per item, so
    nothing the runnable held vanishes silently with it.
    """
    skip_log = acc.skip_log
    runnable_entry = skip_log.record_reason(runnable.path, "RUNNABLE-ENTITY", runnable_skip, effect="abort")
    if runnable.path not in state.runnable_name_by_path:
        # A duplicate shares the kept runnable's path, which a reference resolves to.
        state.skipped_runnables.setdefault(runnable.path, runnable_entry)
    chained: list[SwcAccessInfo | SwcLocalAccessInfo | SwcAreaRefInfo | SwcResultPointInfo] = [
        *runnable.accesses,
        *runnable.local_accesses,
        *runnable.area_refs,
        *runnable.result_points,
    ]
    for access in chained:
        skip_log.record_chained(
            access.path,
            access.tag,
            f"chain-skipped: runnable '{runnable.name}' was skipped ({runnable_skip.text})",
            root=runnable_entry,
        )


def _map_runnable(
    ctx: _SwcContext, acc: _SwcAccumulator, state: _SwcState, runnable: SwcRunnableInfo, min_start_interval: float
) -> None:
    """Map the kept *runnable* onto a ``Runnable`` of the component, with its accesses and budget.

    ``min_start_interval`` is float seconds in both, so it is copied unconverted. Local
    accesses and CAN-ENTER- / RUNS-INSIDE-EXCLUSIVE-AREA-REFs bind to the kept variables and
    areas by exact path; each collapsed duplicate is one line on
    ``ImportedSwc.access_collapses``, never a skip entry. CAN-BE-INVOKED-CONCURRENTLY sets
    ``concurrent`` only when it reads ``true``; an unreadable value is reported and written
    as ``false``. The planned ``SWC-TIMING`` budget gives ``wcet`` and ``wcet_type``.
    """
    raw = state.raw
    skip_log = acc.skip_log
    reads, writes, calls, collapses, access_sources = map_runnable_accesses(
        raw.name,
        runnable.name,
        runnable.accesses,
        port_by_path=state.port_by_path,
        interface_by_name=ctx.interface_by_name,
        skip_log=skip_log,
        swc_path=raw.path,
        declared=ctx.declared,
        unresolved=ctx.unresolved,
        held=state.held_records,
        misses=state.member_misses,
        call_points=state.call_points,
    )
    state.access_collapses.extend(collapses)
    state.generated_sources.extend(access_sources)
    reads_variables, writes_variables, local_collapses, local_sources = map_local_accesses(
        raw.name,
        runnable.name,
        runnable.local_accesses,
        state.mapped_variables,
        state.behaviour_variable_paths,
        skip_log,
    )
    state.access_collapses.extend(local_collapses)
    state.generated_sources.extend(local_sources)
    can_enter, runs_inside, area_collapses = map_area_refs(
        raw.name, runnable.name, runnable.area_refs, state.mapped_areas, skip_log
    )
    state.access_collapses.extend(area_collapses)
    if runnable.unreadable_concurrency is not None:
        skip_log.record(
            runnable.path,
            "CAN-BE-INVOKED-CONCURRENTLY",
            f"unreadable value {runnable.unreadable_concurrency!r} (not an xsd:boolean) -- "
            f"runnable '{runnable.name}' is written with concurrent: false",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
    planned_budget = ctx.budget_plan.carried.get(runnable.path)
    state.runnables_by_name[runnable.name] = Runnable(
        name=runnable.name,
        min_start_interval=min_start_interval,
        concurrent=runnable.can_be_invoked_concurrently is True,
        events=[],
        reads=reads,
        writes=writes,
        calls=calls,
        reads_variables=reads_variables,
        writes_variables=writes_variables,
        can_enter=can_enter,
        runs_inside=runs_inside,
        description=runnable.description,
        wcet=planned_budget.budget.text if planned_budget is not None else None,
        wcet_type=planned_budget.wcet_type if planned_budget is not None else None,
    )
    if planned_budget is not None:
        state.carried_budgets.append(planned_budget)
    state.runnable_name_by_path[runnable.path] = runnable.name
    state.kept_runnables.append(runnable)
    state.stated_symbols[runnable.name], symbol_why = stated_symbol(runnable, skip_log)
    if symbol_why is not None:
        state.why_unstated[runnable.name] = symbol_why


def _map_swc_events(ctx: _SwcContext, acc: _SwcAccumulator, state: _SwcState) -> None:
    """Map every event of the component onto its runnables, then bind the result points no event names.

    The returns events are decided before the polls: an event starting a runnable is the
    stronger statement, so a kept call's one result point is an event's before it is a
    poll's. A result point no returns event names is bound as its own call's poll.
    """
    raw = state.raw
    behaviour_path = f"{raw.path}/{raw.behavior_name or ''}"
    binding = ReturnsEventBinding.of(raw)

    for event in raw.events:
        if event.kind == RETURNS_EVENT:
            _map_swc_returns_event(ctx, acc, state, event, behaviour_path, binding)
            continue
        _map_swc_event(ctx, acc, state, event)

    bind_polls(
        raw.name,
        raw,
        kept=state.kept_runnables,
        runnables_by_name=state.runnables_by_name,
        call_points=state.call_points,
        behaviour_path=behaviour_path,
        skip_log=acc.skip_log,
        reader_log=ctx.model.skip_log,
        collapses=state.access_collapses,
        sources=state.generated_sources,
        taken=binding.taken,
    )


def _map_swc_returns_event(
    ctx: _SwcContext,
    acc: _SwcAccumulator,
    state: _SwcState,
    event: SwcEventInfo,
    behaviour_path: str,
    binding: ReturnsEventBinding,
) -> None:
    """Map one returns event onto the runnable it starts, as ``map_returns_event`` decides it.

    A kept event is the runnable's last one; its source and its result point's source are
    recorded for the report.
    """
    raw = state.raw
    returns = map_returns_event(
        raw.name,
        event,
        raw=raw,
        runnable_name_by_path=state.runnable_name_by_path,
        skipped_runnables=state.skipped_runnables,
        call_points=state.call_points,
        binding=binding,
        behaviour_path=behaviour_path,
        skip_log=acc.skip_log,
        reader_log=ctx.model.skip_log,
        collapses=state.access_collapses,
    )
    if returns is not None:
        started, returns_event, point = returns
        events = state.runnables_by_name[started].events
        events.append(returns_event)
        state.generated_sources.append(
            GeneratedSource(started, "events", len(events) - 1, event.path, event.kind, event.name)
        )
        state.generated_sources.append(
            GeneratedSource(started, "events.result", len(events) - 1, point.path, point.tag, point.name)
        )


def _map_swc_event(ctx: _SwcContext, acc: _SwcAccumulator, state: _SwcState, event: SwcEventInfo) -> None:
    """Map one event other than a returns event onto the runnable its START-ON-EVENT-REF names.

    An event whose reference matches none of the component's own runnable paths is skipped
    with a reason. A TIMING-EVENT's ``period`` is copied unconverted (float seconds), or the
    event is skipped when PERIOD is absent, unreadable, not finite or not positive. An
    OPERATION-INVOKED-EVENT and a DATA-RECEIVED-EVENT are bound by exact path to a port of
    the component and a member of its interface, or skipped with a reason. Disabled modes
    are carried on a timing or a data-received event only; on another kind each is
    reported. A kept event is the runnable's last one.
    """
    raw = state.raw
    skip_log = acc.skip_log
    port_by_path = state.port_by_path
    target_name = state.runnable_name_by_path.get(event.runnable_ref) if event.runnable_ref else None
    if target_name is None:
        skip_log.record(
            event.path,
            event.kind,
            f"unresolved reference {event.runnable_ref!r} (runnable not found on SWC '{raw.name}')",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return

    runnable = state.runnables_by_name[target_name]
    events_before = len(runnable.events)
    if event.kind not in DISABLED_MODE_EVENT_KINDS:
        for _mode in event.disabled_modes:
            report_disabled_mode(
                event.path,
                f"disabled mode of a {event.kind} not carried: SWCraft carries disabled modes on a timing "
                "or a data-received event only",
                skip_log,
            )
    # One arm per event kind the reader admits, and no `else` meaning any of them:
    # an operation event taken for an init event would be a silent wrong mapping.
    if event.kind == "OPERATION-INVOKED-EVENT":
        operation_event = map_operation_event(
            raw.name,
            event,
            port_by_path=port_by_path,
            interface_by_name=ctx.interface_by_name,
            skip_log=skip_log,
            swc_path=raw.path,
            declared=ctx.declared,
            unresolved=ctx.unresolved,
            held=state.held_records,
            misses=state.member_misses,
            runnable_name=target_name,
            position=events_before,
        )
        if operation_event is not None:
            runnable.events.append(operation_event)
    elif event.kind == "TIMING-EVENT":
        if event.period is None:
            skip_log.record(
                event.path,
                event.kind,
                "timing event has no PERIOD"
                if event.unreadable_period is None
                else unreadable_time_value_reason("PERIOD", event.unreadable_period, "timing event"),
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            return
        if not math.isfinite(event.period) or event.period <= 0:
            # A bare `<= 0` comparison never catches NaN.
            skip_log.record(
                event.path,
                event.kind,
                f"PERIOD must be > 0 and finite, got {event.period}",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            return
        runnable.events.append(
            TimingEvent(
                type="timing",
                period=event.period,
                disabled_modes=disabled_modes(event, port_by_path, skip_log),
            )
        )
    elif event.kind == "INIT-EVENT":
        runnable.events.append(InitEvent(type="init"))
    elif event.kind == "DATA-RECEIVED-EVENT":
        data_event = map_data_event(
            raw.name,
            event,
            port_by_path=port_by_path,
            interface_by_name=ctx.interface_by_name,
            skip_log=skip_log,
            swc_path=raw.path,
            declared=ctx.declared,
            unresolved=ctx.unresolved,
            held=state.held_records,
            misses=state.member_misses,
            runnable_name=target_name,
            position=events_before,
        )
        if data_event is not None:
            runnable.events.append(data_event)
    else:
        # Unreachable from the reader; a hand-built record takes a skip, never another kind's arm.
        skip_log.record(
            event.path,
            event.kind,
            "unrecognized event kind inside EVENTS",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
    if len(runnable.events) > events_before:
        state.generated_sources.append(
            GeneratedSource(target_name, "events", len(runnable.events) - 1, event.path, event.kind, event.name)
        )


def _keep_imported_swc(ctx: _SwcContext, acc: _SwcAccumulator, state: _SwcState, group: str) -> None:
    """Construct the component's ``SWComponent`` and its ``ImportedSwc`` record, and append it to ``acc.swcs``.

    Each kept provide port's SERVER-COM-SPEC whose QUEUE-LENGTH is not 1 is one
    lossy-transformation line, never a skip entry. Once the component is kept, its own
    unresolved references (its DATA-TYPE-MAPPING-REFs) are committed.
    """
    raw = state.raw
    unresolved = ctx.unresolved
    port_by_path = state.port_by_path
    mapped_variables = state.mapped_variables
    mapped_areas = state.mapped_areas
    swc = SWComponent(
        name=raw.name,
        type="application",
        description=raw.description,
        ports=state.ports,
        runnables=list(state.runnables_by_name.values()),
        data_type_mappings=data_type_mappings(raw, ctx.mapping_set_name_by_path or {}, unresolved),
        # The reader kept only the schema's three literals, so this table is total over what arrives.
        handle_termination_and_restart=HANDLE_TERMINATION_AND_RESTART_VALUES.get(
            raw.handle_termination_and_restart or ""
        ),
        variables=mapped_variables.variables,
        exclusive_areas=mapped_areas.areas,
    )
    queue_length_losses = server_queue_length_losses(raw.name, port_by_path)
    budget_sources, budget_normalizations = budget_records(state.carried_budgets)
    acc.swcs.append(
        ImportedSwc(
            swc=swc,
            group=group,
            path=raw.path,
            access_collapses=state.access_collapses,
            generated_sources=state.generated_sources,
            queue_length_losses=[line for _key, line in queue_length_losses],
            queue_length_keys=[key for key, _line in queue_length_losses],
            port_init_radix=port_init_radix(port_by_path),
            port_init_equivalents=port_init_equivalents(port_by_path),
            port_init_labels=port_init_labels(port_by_path),
            held_stub_records=state.held_records,
            held_stub_com_specs=held_stub_com_specs(port_by_path),
            late_ports=late_ports(port_by_path),
            com_spec_member_misses=com_spec_member_misses(port_by_path),
            behaviour_member_misses=state.member_misses,
            budget_sources=budget_sources,
            budget_normalizations=budget_normalizations,
            variable_init_radix=mapped_variables.radix_lines,
            variable_paths={name: path for path, name in mapped_variables.name_by_path.items()},
            stated_result_calls=stated_result_calls(raw, state.call_points),
        )
    )
    acc.created_paths.add(raw.path)
    acc.carried_runnable_paths.update(state.runnable_name_by_path)
    # Commits the component's own references (its DATA-TYPE-MAPPING-REFs); a port's are committed in `_map_port`.
    if unresolved is not None:
        unresolved.commit(raw.path)
