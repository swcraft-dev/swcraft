"""The behaviour of the component ``import component`` leaves, and the findings the import introduces.

A component whose references the late pass could not answer is written with its behaviour withheld
(:func:`withhold_behaviour`, with the lines about that content dropped by :func:`without_withheld_lines`);
a held ``--update`` keeps the target's behaviour under the file's descriptive part
(:func:`behaviour_overlay`) and is refused for each reference that overlay breaks
(:func:`kept_behaviour_breaks`, over :func:`_behaviour_findings`). :func:`call_finding_lines` gives the
report lines of the asynchronous call and implicit read findings the import's carries introduce
(:func:`_async_call_finding_lines`, :func:`_implicit_read_finding_lines`), and :func:`area_counts` the
exclusive areas the report counts. :mod:`.import_`'s planner calls them; nothing here raises.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace

from swcraft_tools.arxml_import.swc_mapping import ImportedSwc
from swcraft_tools.arxml_reader import ImportModel, SkipEntry
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.data_description import DataDescription
from swcraft_tools.schema import Interface, MappingSet, SWComponent
from swcraft_tools.validator import (
    ASYNC_WITHOUT_RESULT,
    SEVERITY_ERROR,
    ConsistencyIssue,
    async_call_records,
    async_call_subject,
    data_event_records,
    disabled_mode_records,
    exclusive_area_records,
    implicit_read_records,
    inter_runnable_variable_records,
    operation_event_records,
    queued_read_records,
    runnable_access_records,
)

from .dependencies import raw_swc
from .target_project import target_first


def withhold_behaviour(imported: ImportedSwc) -> ImportedSwc:
    """*imported* with its runnables, variables and exclusive areas withheld.

    Every other field, ``data_type_mappings`` included, stays as the file states it. The mapper's
    records about the runnables (access collapses, generated sources, budget lines) go with them.
    """
    swc = imported.swc.model_copy(update={"runnables": [], "variables": [], "exclusive_areas": []})
    return replace(
        imported,
        swc=swc,
        access_collapses=[],
        generated_sources=[],
        budget_sources=[],
        budget_normalizations=[],
    )


def without_withheld_lines(entries: Sequence[SkipEntry], model: ImportModel, imported: ImportedSwc) -> list[SkipEntry]:
    """*entries* without the lines about content below the internal behaviour, for a withheld behaviour.

    Lines below the ``SWC-INTERNAL-BEHAVIOR`` and at or below an ``SWC-TIMING`` naming it are dropped;
    a line at the behaviour's own path and every line about a port, an interface or a type are kept.
    """
    raw = raw_swc(model, imported)
    if raw is None or raw.behavior_name is None:
        return list(entries)
    behaviour = f"{raw.path}/{raw.behavior_name}"
    timings = {timing.path for timing in model.swc_timings if timing.behavior_ref == behaviour}
    prefixes = (f"{behaviour}/", *(f"{path}/" for path in sorted(timings)))
    return [entry for entry in entries if entry.path not in timings and not entry.path.startswith(prefixes)]


def behaviour_overlay(existing: SWComponent, incoming: SWComponent) -> SWComponent:
    """*incoming*'s descriptive part over *existing*'s behaviour.

    For a target component whose incoming behaviour was withheld: the file's ports, their com-specs, its
    description and its type over the project's behavioural state, preserved whole --
    ``behavior_name``, ``handle_termination_and_restart``, ``data_type_mappings``, the runnables with
    everything they carry, and the variables and exclusive areas they name. What every update carries
    besides (``memory``, ``asil``, ``package``, ``measures``, the com-spec spelling, the descriptions)
    is carried onto this overlay as usual.
    """
    return incoming.model_copy(
        update={
            "behavior_name": existing.behavior_name,
            "handle_termination_and_restart": existing.handle_termination_and_restart,
            "data_type_mappings": list(existing.data_type_mappings),
            "runnables": list(existing.runnables),
            "variables": list(existing.variables),
            "exclusive_areas": list(existing.exclusive_areas),
        }
    )


def _behaviour_findings(
    swc: SWComponent,
    interfaces: Sequence[Interface],
    description: DataDescription,
    mapping_sets: Sequence[MappingSet],
    arxml_version: ArxmlVersion | None = None,
) -> list[str]:
    """The validator's findings about *swc*'s behavioural references, as messages naming no path.

    Asks in memory the predicates ``check-model`` and the writer judge a runnable's references by: the
    access lists, the operation events, the disabled modes, a by-value read of a queued element, the
    variables' errors, the area references (``unknown-area``), the data-received events, the implicit
    reads and the asynchronous calls.

    The asynchronous call rules are asked at *arxml_version*, the target project's; a caller handing a
    model root and no project gets the default, 4.2.2. The one caller is the withheld-behaviour overlay
    (:func:`kept_behaviour_breaks`), which keeps every runnable whole, so a finding is the target's
    own and is not refused; the version is passed so that stays true if the overlay ever carries a file
    runnable. Elsewhere a call with no result point at 4.2.2 is a report line, not a refusal
    (:func:`_async_call_finding_lines`).
    """
    return [
        record.message
        for record in (
            *runnable_access_records([swc], interfaces),
            *operation_event_records([swc], interfaces, description, mapping_sets),
            *disabled_mode_records([swc], interfaces),
            *queued_read_records([swc], interfaces),
            # The read-without-init warning refuses nothing, here as in `check-model`.
            *(
                record
                for record in inter_runnable_variable_records([swc], description, mapping_sets)
                if record.severity == SEVERITY_ERROR
            ),
            *exclusive_area_records([swc]),
            *data_event_records([swc], interfaces),
            *implicit_read_records([swc], interfaces),
            *async_call_records([swc], arxml_version),
        )
    ]


def kept_behaviour_breaks(
    existing: SWComponent,
    overlay: SWComponent,
    *,
    target_interfaces: Mapping[str, Interface],
    interfaces: Sequence[Interface],
    target_description: DataDescription,
    description: DataDescription,
    target_mapping_sets: Mapping[str, MappingSet],
    mapping_sets: Sequence[MappingSet],
    arxml_version: ArxmlVersion | None = None,
) -> list[str]:
    """One conflict line per reference of the kept behaviour the ``--update`` overlay breaks.

    The overlay is judged in the project the import would leave: the target's interfaces and mapping
    sets first (a name the import also brings is reused, or is a conflict of its own), then the import's
    new ones, and the target's data description with the import's types. A finding the component
    already has in the target is the project's own; the update is refused only for a finding it would
    introduce -- a removed port, a changed direction, an interface without a used member, a kept
    by-value read of an element the overlay makes queued.

    What this does **not** reach: the predicates judge the kept **behaviour**. A port the overlay adds
    that speaks an application type the kept ``data_type_mappings`` do not map
    (``rule:data-type.application-type-unmapped``, judged on ports) is written, and ``check-model``
    reports it afterwards.
    """
    resulting_interfaces = target_first(target_interfaces, interfaces)
    resulting_sets = target_first(target_mapping_sets, mapping_sets)
    before = set(
        _behaviour_findings(
            existing,
            list(target_interfaces.values()),
            target_description,
            list(target_mapping_sets.values()),
            arxml_version,
        )
    )
    return [
        f"SWC '{overlay.name}' keeps the project's own behaviour (the file's is withheld), and the update would "
        f"break it: {message}"
        for message in _behaviour_findings(overlay, resulting_interfaces, description, resulting_sets, arxml_version)
        if message not in before
    ]


# ---------------------------------------------------------------------------
# The exclusive areas the report counts
# ---------------------------------------------------------------------------


def area_counts(swc: SWComponent) -> tuple[int, int]:
    """``(areas, references)`` of *swc*, a reference being one ``can_enter`` or ``runs_inside`` entry."""
    return len(swc.exclusive_areas), sum(len(r.can_enter) + len(r.runs_inside) for r in swc.runnables)


# ---------------------------------------------------------------------------
# The validator's findings an import's carries introduce: the asynchronous calls and the implicit reads
# ---------------------------------------------------------------------------


def _async_call_finding_lines(
    swc: SWComponent, existing: SWComponent | None, arxml_version: ArxmlVersion | None
) -> tuple[list[str], list[str]]:
    """The report lines of the asynchronous call findings *swc* introduces that *existing* did not hold.

    *arxml_version* ``None`` means 4.2.2. A finding the target's component already holds is not repeated.

    Returns two lists: one line per call left with no result point (``swc.call.async-without-result``,
    at 4.2.2 alone: ``[constr_2006]`` of R4.2.2, exactly one result point; R4.3.0 allows none), with
    its repair; and every finding of the other six rules as ``rule <id>: <message>``.

    What this does **not** reach: a component left ``unchanged`` or refused, and any other rule.
    """
    before = set() if existing is None else {_finding_key(r) for r in async_call_records([existing], arxml_version)}
    records = async_call_records([swc], arxml_version)
    without_result = [
        _async_without_result_line(record)
        for record in records
        if record.rule == ASYNC_WITHOUT_RESULT and _finding_key(record) not in before
    ]
    others = _new_finding_lines((record for record in records if record.rule != ASYNC_WITHOUT_RESULT), before)
    return without_result, others


def _async_without_result_line(record: ConsistencyIssue) -> str:
    """The report line of one call left with no result point (:func:`~swcraft_tools.validator.async_call_subject`)."""
    runnable, port, operation = async_call_subject(record)
    return (
        f"runnable '{runnable}' calls operation '{operation}' of port '{port}' asynchronously and nothing "
        f"fetches its result: rule {ASYNC_WITHOUT_RESULT}, [constr_2006] of AUTOSAR R4.2.2 (an asynchronous "
        "call point has exactly one result point) -- add `poll: true` to the call or a `call_returned` event "
        "naming it, or set `arxml_version: 4.3.0` in swcraft.yaml"
    )


def _finding_key(record: ConsistencyIssue) -> tuple[str, str]:
    """A finding as ``(rule, message)``: the message names the component, the runnables and the pair."""
    return record.rule, record.message


def _new_finding_lines(records: Iterable[ConsistencyIssue], known: AbstractSet[tuple[str, str]]) -> list[str]:
    """``rule <id>: <message>`` of each of *records* that *known* does not hold, keyed by :func:`_finding_key`."""
    return [f"rule {record.rule}: {record.message}" for record in records if _finding_key(record) not in known]


def _implicit_read_finding_lines(
    swc: SWComponent,
    existing: SWComponent | None,
    stated: SWComponent,
    target_interfaces: Mapping[str, Interface],
    interfaces: Sequence[Interface],
) -> list[str]:
    """The report lines of the implicit read findings the import's carries introduce.

    One ``rule <id>: <message>`` line each. A finding is left out when the target's component already
    holds it, or the file's own component (*stated*) holds it in the same project (the reader's
    ``[constr_2020]`` / ``[constr_1103]`` line names it).

    What this does **not** reach: a component left ``unchanged`` or refused, the withheld-behaviour
    overlay, and any other rule.
    """
    resulting = target_first(target_interfaces, interfaces)
    known = {_finding_key(r) for r in implicit_read_records([stated], resulting)}
    if existing is not None:
        known |= {_finding_key(r) for r in implicit_read_records([existing], list(target_interfaces.values()))}
    return _new_finding_lines(implicit_read_records([swc], resulting), known)


@dataclass(frozen=True)
class CallFindings:
    """The report lines of the call findings the import introduces (:func:`call_finding_lines`)."""

    async_without_result: list[str]
    async_findings: list[str]
    implicit_findings: list[str]


def call_finding_lines(
    *,
    swc_action: str,
    swc: SWComponent,
    existing_swc: SWComponent | None,
    arxml_version: ArxmlVersion | None,
    behaviour_kept: bool,
    stated_swc: SWComponent,
    target_interfaces: Mapping[str, Interface],
    interfaces: Sequence[Interface],
) -> CallFindings:
    """The report lines of the call findings the import introduces into the component it writes.

    Returns the calls left with no result point, the other asynchronous call findings and the implicit
    read findings, each empty for a component left ``unchanged``; the implicit reads are also empty
    under a kept behaviour, whose overlay :func:`kept_behaviour_breaks` refuses instead.
    """
    async_without_result, async_findings = (
        _async_call_finding_lines(swc, existing_swc, arxml_version)
        if swc_action in ("created", "updated")
        else ([], [])
    )
    # Report lines, never a refusal; the withheld-behaviour overlay refuses its own.
    implicit_findings = (
        _implicit_read_finding_lines(swc, existing_swc, stated_swc, target_interfaces, interfaces)
        if swc_action in ("created", "updated") and not behaviour_kept
        else []
    )
    return CallFindings(
        async_without_result=async_without_result, async_findings=async_findings, implicit_findings=implicit_findings
    )
