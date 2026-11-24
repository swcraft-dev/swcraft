"""Resource utilization report.

This module **recomputes nothing**. It renders
:func:`~swcraft_tools.resource_analysis.analyze_resource_budgets`'s structured
result into the human-readable breakdown: per resource, its consumption,
threshold, headroom, and the per-consumer breakdown behind that total.
:class:`ResourceReport` holds the result and renders the text;
:func:`report_markdown` is the ``--report`` file, :func:`format_report_json` the
versioned document, and :func:`overview`, :func:`resource_answer` and
:func:`fits_answer` the MCP tool's structured answers. Every
:attr:`ResourceUsage.findings` is rendered inside its owning element's block, the
``info`` findings of an ``enforcement: advisory`` element included (this report is
their only channel: ``swcraft validate`` prints issues only).

**Topology.** A task whose ``core`` names no declared element is in no block, no
total and no per-resource finding. A caller that runs ``check_hardware_topology``
hands its records to :func:`resource_report` (the seam is :class:`TopologyRecord`;
this module imports no validator), and they are rendered in a ``TOPOLOGY``
section; without them there is none.

**Not reached:** ``FINDINGS`` counts the budget findings only; a topology
record's ``severity`` is read by nothing here; the placement findings
(``safety.partition-on-unqualified-core``, ``safety.memory-below-component``) are
``check-model``'s and named nowhere here. ``resource-report`` always exits 0 on a
successful run: it is no second gate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Protocol

from swcraft_tools.data_arrival import BOUNDED, UNKNOWN, Arrival, arrivals
from swcraft_tools.implicit_access import (
    IN_NO_TASK,
    OCCURRENCE,
    PAST_THE_CAP_LINE,
    TASK,
    UNKNOWN_CORE,
    ImplicitLine,
    implicit_accesses,
)
from swcraft_tools.markdown_cells import cell_text, code_span, table
from swcraft_tools.resource_analysis import (
    FINDING_MISSING_BUDGET,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    ConsumerUsage,
    Finding,
    Resource,
    ResourceAnalysis,
    ResourceUsage,
    analyze_resource_budgets,
    budget_status,
    exact,
    fits,
    fits_size,
    format_amount,
    format_bytes,
    format_decimal,
    format_percent,
    format_whole_bytes,
    is_core,
    partition_shares,
    physical_limit,
    reachable_memories,
)

# Every figure and every verdict word comes from `resource_analysis`'s public
# rendering helpers; this module decides line *syntax* only.


def _finding_line(finding: Finding, indent: str) -> str:
    return f"{indent}- [{finding.severity}] {finding.code}: {finding.message}"


class TopologyRecord(Protocol):
    """The two attributes this module reads off a topology finding.

    A structural type, so this module imports no validator; it does **not**
    declare ``subjects`` or ``target``, which this renderer never reads. The
    members are ``@property`` stubs so a frozen dataclass satisfies them.
    """

    @property
    def rule(self) -> str: ...

    @property
    def message(self) -> str: ...


def _topology_line(record: TopologyRecord, indent: str) -> str:
    """One topology finding: its rule id, then the rule's own words; no severity, every topology rule is an error."""
    return f"{indent}- {record.rule}: {record.message}"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class ResourceReport:
    """Rendered view of one :func:`analyze_resource_budgets` result.

    ``topology`` holds the records a caller computed and handed over (empty by
    default). ``arrivals`` is :func:`swcraft_tools.data_arrival.arrivals`'
    answer and ``implicit`` :func:`swcraft_tools.implicit_access.implicit_accesses`';
    each is empty, and rendered nowhere, for a model without such an event or access.
    """

    model_root: str
    analysis: ResourceAnalysis
    topology: tuple[TopologyRecord, ...] = ()
    arrivals: tuple[Arrival, ...] = ()
    implicit: tuple[ImplicitLine, ...] = ()

    @property
    def error_count(self) -> int:
        """``error`` findings across every declared resource."""
        return sum(1 for f in self.analysis.all_findings() if f.severity == SEVERITY_ERROR)

    @property
    def info_count(self) -> int:
        """``info`` findings — the ``advisory`` resources'."""
        return sum(1 for f in self.analysis.all_findings() if f.severity == SEVERITY_INFO)

    def render(self, *, subject: str | None = None, fit: FitQuestion | None = None) -> str:
        """The text form (:func:`report_markdown` is the file's); *subject* heads it; *fit* adds ``FIT`` lines."""
        # No empty-analysis branch: a model with no core or memory renders `DECLARED 0` and
        # `FINDINGS 0 error(s), 0 informational`, which are true.
        lines = [f"Resource utilization report: {self.model_root if subject is None else subject}", ""]

        lines.append(f"DECLARED {len(self.analysis.resources)} resource(s)")
        lines.append(f"FINDINGS {self.error_count} error(s), {self.info_count} informational")

        # Before the per-resource blocks: a topology finding is about the model, not one block.
        if self.topology:
            lines.append("")
            lines.append(f"TOPOLOGY {len(self.topology)} finding(s)")
            lines.extend(_topology_line(record, "  ") for record in self.topology)

        for usage in self.analysis.resources:
            lines.append("")
            lines.extend(_render_usage(usage, fit))

        lines.extend(_arrival_section(self.arrivals))
        lines.extend(implicit_text(self.implicit))
        return "\n".join(lines)


def _render_usage(usage: ResourceUsage, fit: FitQuestion | None = None) -> list[str]:
    """One resource's block: header, status, aggregate, physical headroom, the fit, breakdown, findings."""
    resource = usage.resource
    if is_core(resource):
        header = f"RESOURCE {resource.name} (core, {resource.enforcement})"
        aggregate = (
            f"  UTILIZATION {format_percent(usage.consumption)} of a "
            f"{format_percent(usage.limit)} threshold (headroom {format_percent(usage.headroom)})"
        )
        consumer_label = "task(s)"
    else:
        header = f"RESOURCE {resource.name} (memory, size {resource.size} bytes, {resource.enforcement})"
        aggregate = (
            f"  CONSUMPTION {format_bytes(usage.consumption)} bytes of a "
            f"{format_bytes(usage.limit)} byte budget (headroom {format_bytes(usage.headroom)} bytes)"
        )
        consumer_label = "SWC(s)"

    untracked = sum(1 for c in usage.consumers if not c.declared)

    lines = [
        header,
        f"  STATUS {budget_status(usage)}",
        aggregate,
        _physical_line(usage),
        *_fit_lines(usage, fit),
        f"  CONSUMERS {len(usage.consumers)} {consumer_label}, {untracked} without a declared budget",
    ]
    lines.extend(_render_consumer(resource, c) for c in usage.consumers)
    if usage.findings:
        lines.append("  FINDINGS")
        lines.extend(_finding_line(f, "    ") for f in usage.findings)
    return lines


def _render_consumer(resource: Resource, consumer: ConsumerUsage) -> str:
    detail = f" ({consumer.detail})" if consumer.detail else ""
    return f"    - {consumer.name}: {format_amount(resource, consumer)}{detail}"


def resource_report(model_root: str, topology: Sequence[TopologyRecord] = ()) -> ResourceReport:
    """Build the resource utilization report for the model at *model_root*.

    Args:
        model_root: Root directory of the model.
        topology: the hardware topology findings a caller already computed, to
            be rendered in the report's ``TOPOLOGY`` section. Defaults to none:
            this function runs no check of its own.

    Raises:
        ValueError: if any ``hardware/*.yaml``, ``tasks/*.yaml`` or SWC file is
            malformed, and -- once a data-received event exists -- an interface,
            connector or composition file the arrivals read, and -- once an
            implicit access exists -- a composition file that section reads.
    """
    return ResourceReport(
        model_root=model_root,
        analysis=analyze_resource_budgets(model_root),
        topology=tuple(topology),
        arrivals=arrivals(model_root),
        implicit=implicit_accesses(model_root),
    )


# ---------------------------------------------------------------------------
# The structured answers of the MCP `resource_report` tool
#
# The same report, one question at a time: every figure is the string `render()` prints
# for it, never a float, and every verdict word is `budget_status`'s.
# ---------------------------------------------------------------------------

#: The two kinds a question names a resource by, as the tool's arguments spell them.
KIND_CORE = "core"
KIND_MEMORY = "memory"

#: How many consumers that declared no budget an answer names by name; the rest are counted,
#: since naming each would grow an answer with components x memories.
UNDECLARED_NAMED = 10


#: How many declared names a refusal of an unknown name lists; the rest are counted, since the
#: server's response budget never measures an ``ERROR:`` sentence.
DECLARED_NAMED = 10


class UnknownResource(ValueError):
    """A name no declared resource of the asked kind carries; the message names the first declared ones."""


class AmbiguousResource(ValueError):
    """A name more than one hardware element declares (``hardware.duplicate-name``), so no one answer is its."""


def _kind_of(resource: Resource) -> str:
    return KIND_CORE if is_core(resource) else KIND_MEMORY


def _figure(resource: Resource, value: Fraction) -> str:
    """*value* in the budget's own unit, spelled as the text report spells it: :func:`_figure_of_kind`'s."""
    return _figure_of_kind(_kind_of(resource), value)


def _row(usage: ResourceUsage) -> dict[str, Any]:
    """One resource's headline: what the text's header, ``STATUS`` and aggregate lines say, as fields."""
    resource = usage.resource
    row: dict[str, Any] = {"name": resource.name, "kind": _kind_of(resource), "enforcement": resource.enforcement}
    if is_core(resource):
        row["utilization"] = format_percent(usage.consumption)
    else:
        row["size"] = resource.size
        row["used"] = format_bytes(usage.consumption)
    row["limit"] = _figure(resource, usage.limit)
    row["headroom"] = _figure(resource, usage.headroom)
    row["physical_headroom"] = _physical_headroom(usage)
    row["status"] = budget_status(usage)
    return row


def _finding(finding: Finding) -> dict[str, str]:
    return {"severity": finding.severity, "code": finding.code, "message": finding.message}


def _findings(
    usage: ResourceUsage, *, named: bool = False, ask_for: Callable[[str, str], str] | None = None
) -> list[dict[str, Any]]:
    """*usage*'s findings, its ``missing_budget`` ones folded into one entry that counts them.

    *named* heads each entry with the ``resource`` it is about, as the overview lists them.

    The folded entry keeps the severity and states the count, at the place the first of
    them stood; every other finding is kept as it is. *ask_for*, given the resource's kind
    and name, returns the sentence a caller appends naming how to ask for the consumers, in
    the caller's own syntax, which this layer does not know.
    """
    missing = [finding for finding in usage.findings if finding.code == FINDING_MISSING_BUDGET]
    head: dict[str, Any] = {"resource": usage.name} if named else {}
    folded: list[dict[str, Any]] = []
    for finding in usage.findings:
        if finding.code != FINDING_MISSING_BUDGET:
            folded.append(head | _finding(finding))
        elif finding is missing[0]:
            kind = _kind_of(usage.resource)
            folded.append(
                head
                | {
                    "severity": finding.severity,
                    "code": FINDING_MISSING_BUDGET,
                    "count": len(missing),
                    "message": f"{len(missing)} consumer(s) of {kind} '{usage.name}' declare no budget for it."
                    + (ask_for(kind, usage.name) if ask_for is not None else ""),
                }
            )
    return folded


def overview(report: ResourceReport, *, ask_for: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """Every core and memory as one headline row, the budget findings and the topology records -- never a consumer.

    The rows are the text report's blocks without their ``CONSUMERS`` lines, in
    reader order; ``findings`` are every block's findings (``info`` included),
    each with the resource it is about, the ``missing_budget`` ones of a
    resource folded into one counting entry (:func:`_findings`, given *ask_for*);
    ``topology`` is the ``TOPOLOGY`` section. ``arrivals``, only when the model
    has a data-received event, is ``{"first", "more", "note"}`` plus ``rest``
    (:data:`ARRIVALS_REST`) when any line follows: at most :data:`ARRIVALS_NAMED`
    lines within :data:`ARRIVALS_FIRST_BYTES` wire bytes. ``implicit``, only when
    the model has an implicit access, is :func:`implicit_overview`. Narrower than
    the text in the folded findings and the capped lines.
    """
    usages = report.analysis.resources
    answer: dict[str, Any] = {
        "response": "resources",
        "cores": [_row(usage) for usage in usages if is_core(usage.resource)],
        "memories": [_row(usage) for usage in usages if not is_core(usage.resource)],
        "findings": [finding for usage in usages for finding in _findings(usage, named=True, ask_for=ask_for)],
        "topology": [{"rule": record.rule, "message": record.message} for record in report.topology],
    }
    if report.arrivals:
        first = _first_arrival_lines(report.arrivals)
        more = len(report.arrivals) - len(first)
        answer["arrivals"] = {
            "first": first,
            "more": more,
            "note": ARRIVALS_NOTE,
        } | ({"rest": ARRIVALS_REST} if more else {})
    if report.implicit:
        # The section's two tables' rows, then the unwalked occurrences, capped as `arrivals` is
        # (`implicit_overview`, at the module's end).
        answer["implicit"] = implicit_overview(report.implicit)
    return answer


def _usage(report: ResourceReport, kind: str, name: str) -> ResourceUsage:
    """The one resource of *kind* named *name*, or :class:`UnknownResource` / :class:`AmbiguousResource`.

    A name two resources carry is refused, never answered from the first: the
    two can hold opposite verdicts. The hardware layer is one identity namespace
    (``hardware.duplicate-name``), so a core and a memory sharing the name are
    refused as two memories are.
    """
    usages = report.analysis.resources
    named = [usage for usage in usages if usage.name == name]
    if len(named) > 1:
        raise AmbiguousResource(
            f"{len(named)} hardware elements are named {name!r} (hardware.duplicate-name), so no one answer is"
            " its; give each its own name, and swcraft validate reports the duplicate."
        )
    if named and _kind_of(named[0].resource) == kind:
        return named[0]
    if named:
        actual = _kind_of(named[0].resource)
        raise UnknownResource(f"{name!r} is a {actual}, not a {kind}; ask for it as {actual}={name!r}.")
    declared = [usage.name for usage in usages if _kind_of(usage.resource) == kind]
    if not declared:
        raise UnknownResource(f"no {kind} named {name!r}: the model declares no {kind}.")
    plural = "cores" if kind == KIND_CORE else "memories"
    listed = ", ".join(declared[:DECLARED_NAMED])
    if len(declared) > DECLARED_NAMED:
        listed += f" and {len(declared) - DECLARED_NAMED} more"
    raise UnknownResource(f"no {kind} named {name!r}; the declared {plural} are {listed}.")


def resource_answer(report: ResourceReport, kind: str, name: str) -> dict[str, Any]:
    """One resource: its headline row, its consumers largest first, and its findings.

    The consumers are the text block's ``CONSUMERS`` lines **ordered by amount,
    largest first** (ties in the report's order). A consumer that declared no
    budget is counted in ``undeclared``, the first :data:`UNDECLARED_NAMED` named
    in ``undeclared_first``; one that declared ``0`` is counted in
    ``declared_zero``. For a core the rows are its ``tasks`` and the answer adds
    ``memories``, the headline row of every memory whose ``accessible`` names the
    core; for a memory the rows are its ``consumers``, each with its exact
    ``bytes``. ``findings`` fold the ``missing_budget`` ones into one counting entry.

    Raises:
        UnknownResource: no resource of *kind* is named *name*.
        AmbiguousResource: more than one hardware element is named *name*.
    """
    usage = _usage(report, kind, name)
    used, undeclared, zero = _ordered_consumers(usage)
    answer: dict[str, Any] = {"response": "resource", "resource": _row(usage)}
    if is_core(usage.resource):
        answer["tasks"] = [{"name": consumer.name, "utilization": format_percent(consumer.amount)} for consumer in used]
        rest: dict[str, Any] = {
            "memories": [_row(memory) for memory in reachable_memories(report.analysis, usage.name)]
        }
    else:
        # A memory budget is a declared integer, so the amount is exact as an `int`.
        answer["consumers"] = [{"name": consumer.name, "bytes": int(consumer.amount)} for consumer in used]
        rest = {}
    answer["undeclared"] = len(undeclared)
    answer["undeclared_first"] = [consumer.name for consumer in undeclared[:UNDECLARED_NAMED]]
    answer["declared_zero"] = len(zero)
    answer.update(rest)
    answer["findings"] = _findings(usage)
    return answer


def fits_answer(report: ResourceReport, kind: str, name: str, amount: Fraction) -> dict[str, Any]:
    """Whether *amount* more fits the resource of *kind* named *name*: :func:`fit_verdicts`, with its figures.

    ``fits`` is ``true``, ``false`` or ``null`` (a "yes" a missing load could
    falsify; a "no" is always given). Beside the verdicts stand what was
    ``asked``, what is ``used``, the ``status`` and a memory's ``size``, each
    spelled as the text report spells it.

    Raises:
        UnknownResource: no resource of *kind* is named *name*.
        AmbiguousResource: more than one hardware element is named *name*.
    """
    usage = _usage(report, kind, name)
    resource = usage.resource
    verdicts = fit_verdicts(usage, amount)
    answer: dict[str, Any] = {"response": "fits", "resource": resource.name, "kind": kind}
    if not is_core(resource):
        answer["size"] = resource.size
    answer["asked"] = _figure(resource, amount)
    answer["used"] = _figure(resource, usage.consumption)
    answer["status"] = budget_status(usage)
    answer.update(verdicts)
    return answer


# ---------------------------------------------------------------------------
# The two fit verdicts and the physical headroom
#
# The arithmetic is `resource_analysis`'s; this section only spells it.
# ---------------------------------------------------------------------------

#: The longest amount text either fit reader takes (:func:`parse_utilization`'s text and ``--fit-bytes``' digits in
#: ``cli.project_args``, which imports it), and the largest decimal exponent read before ``Fraction`` expands it; a
#: fit amount needs neither near them.
FIT_TEXT_MAX = 64
UTILIZATION_EXPONENT_MAX = 100

#: The one sentence :func:`fit_verdicts` states, by case, checked in this order. ``{count}`` is the
#: number of consumers that declared nothing; ``{excess}`` a figure spelled by :func:`_excess`.
ANSWER_UNKNOWN = "unknown: {count} consumer(s) declare no budget for it, so its use leaves their load out"
ANSWER_OVER_SIZE = "does not fit: over the size by {excess}"
ANSWER_OVER_THRESHOLD_SIZE_UNKNOWN = (
    "breaks the threshold by {excess}; within the size is unknown: {count} consumer(s) declare no budget for it"
)
ANSWER_WITHIN_SIZE_ONLY = "fits physically, breaks the threshold by {excess}"
ANSWER_FITS = "fits within the threshold and the size"


def parse_utilization(text: str) -> Fraction:
    """*text* as an exact fraction of a core (``"0.05"`` is 1/20), or :class:`ValueError`.

    Read as its decimal (or ``a/b``) spelling, never its binary float. Bounded
    before ``Fraction`` reads it, since a decimal exponent is expanded as
    ``10 ** exp``; an underscore is refused, since the exponent bound would not
    read digit grouping. The sign is **not** checked here: the callers word
    their own refusal of a negative amount.
    """
    text = text.strip()
    exponent = re.search(r"[eE]([+-]?\d+)$", text)
    if (
        len(text) > FIT_TEXT_MAX
        or "_" in text
        or (exponent is not None and abs(int(exponent.group(1))) > UTILIZATION_EXPONENT_MAX)
    ):
        raise ValueError(f"not a bounded fraction of a core: {text[:FIT_TEXT_MAX]!r}")
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"not a fraction of a core: {text!r}") from exc


def _physical_headroom(usage: ResourceUsage) -> str:
    """What is left of the resource's capacity: a memory's ``size - used``, or a core's ``100% - utilization``."""
    return _figure(usage.resource, physical_limit(usage) - usage.consumption)


def byte_figure(figure: str) -> str:
    """A memory's *figure* with its unit, ``1 byte`` else ``<figure> bytes``: the build report's spelling too."""
    return f"{figure} {'byte' if figure == '1' else 'bytes'}"


def _excess(resource: Resource, value: Fraction) -> str:
    """*value* spelled by :func:`_figure`, with :func:`byte_figure`'s unit after a memory's (a core's carries ``%``)."""
    figure = _figure(resource, value)
    return figure if is_core(resource) else byte_figure(figure)


def fit_verdicts(usage: ResourceUsage, amount: Fraction) -> dict[str, Any]:
    """Whether *amount* more fits *usage*, against the threshold and against the size, and the sentence for the pair.

    ``within_threshold`` checks against the budget's limit (the core's threshold,
    a memory's ``size x threshold``), ``within_size`` against the capacity (a
    memory's ``size``, a core's 100 %). Each ``headroom`` is its limit minus the
    use before *amount*. ``fits`` repeats ``within_threshold``'s verdict.

    ``answer`` is one of the five ``ANSWER_*`` sentences, by case in their
    order: both verdicts unknown; the size a "no"; the threshold a "no" and the
    size unknown; the threshold a "no" and the size a "yes"; both a "yes". No
    other pair occurs: ``fits`` is ``true`` only when no ``strict`` consumer is
    undeclared, and a threshold at most 1 puts the budget within the size.
    """
    resource = usage.resource
    within_threshold = fits(usage, amount)
    within_size = fits_size(usage, amount)
    capacity = physical_limit(usage)
    total = usage.consumption + amount
    undeclared = sum(1 for consumer in usage.consumers if not consumer.declared)
    if within_threshold is None and within_size is None:
        sentence = ANSWER_UNKNOWN.format(count=undeclared)
    elif within_size is False:
        sentence = ANSWER_OVER_SIZE.format(excess=_excess(resource, total - capacity))
    elif within_size is None:
        sentence = ANSWER_OVER_THRESHOLD_SIZE_UNKNOWN.format(
            excess=_excess(resource, total - usage.limit), count=undeclared
        )
    elif within_threshold is False:
        sentence = ANSWER_WITHIN_SIZE_ONLY.format(excess=_excess(resource, total - usage.limit))
    else:
        sentence = ANSWER_FITS
    return {
        "fits": within_threshold,
        "within_threshold": {
            "fits": within_threshold,
            "limit": _figure(resource, usage.limit),
            "headroom": _figure(resource, usage.headroom),
        },
        "within_size": {
            "fits": within_size,
            "limit": _figure(resource, capacity),
            "headroom": _physical_headroom(usage),
        },
        "answer": sentence,
    }


# ---------------------------------------------------------------------------
# The CLI's fit question and its JSON document
#
# The same strings as the MCP answers, except a memory row's `limit` and `headroom`,
# in whole bytes (`_budget_figure`).
# ---------------------------------------------------------------------------

#: The version of :func:`format_report_json`'s document; a reader refuses another one.
#: Adding an optional field does not raise it; removing or renaming a field,
#: changing a type, or changing a field's meaning does.
RESOURCE_REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FitQuestion:
    """*amount* more asked of every resource of *resource_kind* (:data:`KIND_CORE` or :data:`KIND_MEMORY`), exact.

    No name: the CLI asks it of every resource of the kind, so the command's
    exit contract (always 0 on a successful run) needs no refusal of its own.
    """

    resource_kind: str
    amount: Fraction


def _figure_of_kind(kind: str, value: Fraction) -> str:
    """*value* spelled for a resource of *kind*: :func:`_figure`'s spelling, for the question, which has no resource."""
    return format_percent(value) if kind == KIND_CORE else format_bytes(value)


def _physical_line(usage: ResourceUsage) -> str:
    """The text block's ``PHYSICAL HEADROOM`` line: what is left of a memory's size, or of the core."""
    headroom = _physical_headroom(usage)
    if is_core(usage.resource):
        return f"  {_PHYSICAL_LABEL} {headroom} of the core"
    return f"  {_PHYSICAL_LABEL} {headroom} bytes of a {usage.resource.size} byte size"


def _fit_lines(usage: ResourceUsage, fit: FitQuestion | None) -> list[str]:
    """The text block's ``FIT`` line, when *fit* asks *usage*'s kind: the amount with its unit, and the sentence."""
    if fit is None or fit.resource_kind != _kind_of(usage.resource):
        return []
    return [f"  {_FIT_LABEL} {_excess(usage.resource, fit.amount)}: {fit_verdicts(usage, fit.amount)['answer']}"]


def _ordered_consumers(
    usage: ResourceUsage,
) -> tuple[list[ConsumerUsage], list[ConsumerUsage], list[ConsumerUsage]]:
    """*usage*'s consumers as three lists: declared and non-zero largest first, the undeclared, the declared zeros.

    Ties and the two other lists keep the report's order.
    """
    declared = [consumer for consumer in usage.consumers if consumer.declared]
    used = sorted((consumer for consumer in declared if consumer.amount), key=lambda consumer: -consumer.amount)
    undeclared = [consumer for consumer in usage.consumers if not consumer.declared]
    zero = [consumer for consumer in declared if not consumer.amount]
    return used, undeclared, zero


def _budget_figure(usage: ResourceUsage, value: Fraction) -> str:
    """A budget's limit or headroom as the report tabs and the Resources view show it: a memory's in whole bytes.

    The one spelling of the file, the document and the build report; :func:`_row`
    and ``fit_verdicts`` keep the exact figure.
    """
    return _figure(usage.resource, value) if is_core(usage.resource) else format_whole_bytes(value)


def _budget_row(usage: ResourceUsage) -> dict[str, Any]:
    """:func:`_row` with its limit and headroom as a report shows them (:func:`_budget_figure`)."""
    entry = _row(usage)
    entry["limit"] = _budget_figure(usage, usage.limit)
    entry["headroom"] = _budget_figure(usage, usage.headroom)
    return entry


def budget_rows(report: ResourceReport) -> list[dict[str, Any]]:
    """Every core, then every memory, each a :func:`_budget_row`: the build report's table."""
    return [
        _budget_row(usage)
        for usage in sorted(report.analysis.resources, key=lambda each: _kind_of(each.resource) != KIND_CORE)
    ]


def _resource_json(usage: ResourceUsage, fit: FitQuestion | None) -> dict[str, Any]:
    """One resource of the document: its :func:`_budget_row`, consumers, findings, the fit."""
    resource = usage.resource
    layer = "tasks" if is_core(usage.resource) else "swc"
    used, undeclared, zero = _ordered_consumers(usage)
    entry = _budget_row(usage)
    entry["consumers"] = [
        {
            "name": consumer.name,
            "layer": layer,
            "amount": format_amount(resource, consumer),
            "declared": consumer.declared,
            "detail": consumer.detail,
        }
        for consumer in (*used, *undeclared, *zero)
    ]
    entry["findings"] = [
        _finding(finding) | {"subjects": [subject.to_json() for subject in finding.subjects]}
        for finding in usage.findings
    ]
    if fit is not None and fit.resource_kind == _kind_of(resource):
        entry["fit"] = fit_verdicts(usage, fit.amount)
    return entry


def format_report_json(report: ResourceReport, fit: FitQuestion | None = None) -> str:
    """The whole report as one versioned JSON document, ``swcraft resource-report --format json``'s.

    ``findings`` counts what the text's ``FINDINGS`` line counts; ``topology``
    is the ``TOPOLOGY`` section; ``fit`` is the question (``null`` without
    one), and its verdicts stand on the asked kind's rows only. Each core and
    memory is its :func:`overview` row, a memory's ``limit`` and ``headroom`` in
    whole bytes (:func:`_budget_figure`), plus its consumers (in
    :func:`_ordered_consumers`' order) and its findings, every one unfolded,
    with its subjects in their ``Subject.to_json`` form. What it does not
    carry: a storage path (a consumer is named by layer and name), and anything
    the text does not print. Deterministic, ``indent=2``, ``allow_nan=False``
    and newline-terminated. ``arrivals`` (:func:`arrival_json` per line) and
    ``arrivalsNote`` follow only when the model has a data-received event;
    ``implicit`` (:func:`implicit_rows`) and ``implicitNote`` only when it has
    an implicit access.
    """
    usages = report.analysis.resources
    asked = None
    if fit is not None:
        asked = {"kind": fit.resource_kind, "asked": _figure_of_kind(fit.resource_kind, fit.amount)}
    document = {
        "schemaVersion": RESOURCE_REPORT_SCHEMA_VERSION,
        "findings": {"errors": report.error_count, "informational": report.info_count},
        "topology": [{"rule": record.rule, "message": record.message} for record in report.topology],
        "fit": asked,
        "cores": [_resource_json(usage, fit) for usage in usages if is_core(usage.resource)],
        "memories": [_resource_json(usage, fit) for usage in usages if not is_core(usage.resource)],
    }
    if report.arrivals:
        document["arrivals"] = [arrival_json(arrival) for arrival in report.arrivals]
        document["arrivalsNote"] = ARRIVALS_NOTE
    if report.implicit:
        document["implicit"] = implicit_rows(report.implicit)
        document["implicitNote"] = IMPLICIT_NOTE
    return json.dumps(document, indent=2, allow_nan=False) + "\n"


# ---------------------------------------------------------------------------
# The report file: ``resource-report --report``
#
# The file says what the text says, in the text's helpers' words, except that a core's
# tasks show their partition and a memory's budget and headroom read in whole bytes.
# No timestamp and no storage path: the title is the project's name, never `model_root`.
# ---------------------------------------------------------------------------

#: The text's two line labels, which the file's :func:`_markdown_usage` takes off again.
_PHYSICAL_LABEL = "PHYSICAL HEADROOM"
_FIT_LABEL = "FIT"

#: The directory below ``dist_root`` the report is written in, and its one file.
REPORT_DIRECTORY = "resource-report"
MARKDOWN_FILE = "resource-report.md"

_MARKDOWN_TITLE = "Resource utilization report"
_CONSUMER_COLUMN = {KIND_CORE: "Task", KIND_MEMORY: "Component"}
_LIMIT_WORD = {KIND_CORE: "threshold", KIND_MEMORY: "budget"}
_PARTITION_COLUMN = "Partition"
_AMOUNT_COLUMN = "Amount"
_DETAIL_COLUMN = "Detail"
_SHARE_COLUMN = "Share of the core"


def _budget_text(usage: ResourceUsage, value: Fraction) -> str:
    """:func:`_budget_figure` with :func:`byte_figure`'s unit after a memory's (a core's carries ``%``)."""
    figure = _budget_figure(usage, value)
    return figure if is_core(usage.resource) else byte_figure(figure)


def _limit_text(usage: ResourceUsage) -> str:
    """The Consumption line's limit, with the unit before its noun as the text has it: ``209715 byte budget``."""
    figure = _budget_figure(usage, usage.limit)
    word = _LIMIT_WORD[_kind_of(usage.resource)]
    return f"{figure} {word}" if is_core(usage.resource) else f"{figure} byte {word}"


def _consumer_tables(usage: ResourceUsage, consumers: Sequence[ConsumerUsage]) -> list[str]:
    """The consumer table, and under a core's the subtotal table: a partition's name and summed share, nothing else.

    A partition has no budget of its own, so a subtotal carries no verdict or
    headroom. A memory has neither the column nor the second table.
    """
    resource = usage.resource
    column = _CONSUMER_COLUMN[_kind_of(resource)]
    shares = partition_shares(usage)
    if not shares:
        rows = [(code_span(c.name), cell_text(format_amount(resource, c)), cell_text(c.detail)) for c in consumers]
        return [*table((column, _AMOUNT_COLUMN, _DETAIL_COLUMN), rows), ""]
    partition_rows = [
        (code_span(c.name), code_span(c.partition), cell_text(format_amount(resource, c)), cell_text(c.detail))
        for c in consumers
    ]
    subtotals = [(code_span(partition), format_percent(share)) for partition, share in shares]
    return [
        *table((column, _PARTITION_COLUMN, _AMOUNT_COLUMN, _DETAIL_COLUMN), partition_rows),
        "",
        *table((_PARTITION_COLUMN, _SHARE_COLUMN), subtotals),
        "",
    ]


def _markdown_usage(usage: ResourceUsage, fit: FitQuestion | None) -> list[str]:
    """One resource's section: its figures as a list, its consumers as a table, its findings as a list."""
    resource = usage.resource
    kind = _kind_of(resource)
    used, undeclared, zero = _ordered_consumers(usage)
    consumers = (*used, *undeclared, *zero)
    lines = [
        f"## {cell_text(resource.name)} ({kind}, {cell_text(resource.enforcement)})",
        "",
        f"- **Status:** {budget_status(usage)}",
        f"- **Consumption:** {_excess(resource, usage.consumption)} of a {_limit_text(usage)} "
        f"(headroom {_budget_text(usage, usage.headroom)})",
        f"- **Physical headroom:** {_physical_line(usage).strip().removeprefix(f'{_PHYSICAL_LABEL} ')}",
        *(f"- **Fit:** {cell_text(line.strip().removeprefix(f'{_FIT_LABEL} '))}" for line in _fit_lines(usage, fit)),
        "",
    ]
    if consumers:
        lines += _consumer_tables(usage, consumers)
    else:
        lines += ["No consumer.", ""]
    if usage.findings:
        lines += [f"- [{f.severity}] {f.code}: {cell_text(f.message)}" for f in usage.findings]
        lines.append("")
    return lines


def report_markdown(report: ResourceReport, project_name: str, fit: FitQuestion | None = None) -> str:
    """The report as the Markdown file ``resource-report --report`` writes, newline-terminated.

    The content of :meth:`ResourceReport.render` in its order, with
    :func:`_ordered_consumers`' order in each table. Every model text is on one
    line, holds no raw HTML, and a ``|`` ends no cell. *project_name* titles it
    and *fit* adds the fit line.

    Two things go past the text. A core's table has a Partition column, and a
    second table under it sums each partition's share, with no verdict
    (:func:`_consumer_tables`); a core with no task shows *No consumer.* A
    memory's budget and headroom read in whole bytes, rounded down; a finding
    and the fit line keep the exact figures.

    What it does not reach: a partition no task names is in neither table, a
    subtotal counts an undeclared task as zero as the core's total does, and
    Markdown's inline markers in a model text are left as they are, so an ``*``
    in a project name renders as a marker would.
    """
    lines = [
        f"# {_MARKDOWN_TITLE}: {cell_text(project_name)}",
        "",
        f"{len(report.analysis.resources)} resource(s) declared; "
        f"findings: {report.error_count} error(s), {report.info_count} informational.",
        "",
    ]
    if report.topology:
        lines += ["## Topology", ""]
        lines += [f"- {record.rule}: {cell_text(record.message)}" for record in report.topology]
        lines.append("")
    for usage in report.analysis.resources:
        lines += _markdown_usage(usage, fit)
    if report.arrivals:
        lines += ["## Arrivals", ""]
        lines += [f"- {cell_text(arrival_line(arrival))}" for arrival in report.arrivals]
        lines += ["", ARRIVALS_NOTE, ""]
    if report.implicit:
        lines += implicit_markdown(report.implicit)
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# The ARRIVALS section
#
# Derived and reported only: the section changes no count of the `FINDINGS` line
# and no exit code.
# ---------------------------------------------------------------------------

#: The sentence printed once under the section, on every surface.
ARRIVALS_NOTE = (
    "These are bounds taken from the senders' task periods, derived from the model; they are not measured rates. "
    "Where the RTE activates the receiving task on arrival, a bound limits that task's activations from the "
    "sender's side only."
)

#: How many ``ARRIVALS`` lines the MCP overview carries; the rest are counted in its ``more``.
#: The lines grow with the model and would otherwise push the overview past the server's
#: response budget.
ARRIVALS_NAMED = 20

#: The wire bytes the overview's ``first`` lines may take together, a fifth of the server's
#: response budget: a line names an instance path, which grows with the composition nesting.
ARRIVALS_FIRST_BYTES = 16_000

#: Where the overview points for the lines past :data:`ARRIVALS_NAMED` or :data:`ARRIVALS_FIRST_BYTES`:
#: the CLI prints every one.
ARRIVALS_REST = "swcraft resource-report prints every line; its --format json carries them as arrivals."


def _period_ms(arrival: Arrival) -> str:
    """The bound's period in milliseconds, exactly (``format_decimal``'s rule over the model's decimal)."""
    assert arrival.period is not None, "only a bound carries a period"
    return format_decimal(exact(arrival.period) * 1000)


def _more(count: int) -> str:
    return f", and {count} more" if count else ""


def arrival_line(arrival: Arrival) -> str:
    """One line of the section, without its indent: who receives what, then the verdict."""
    head = f"{arrival.occurrence}.{arrival.runnable} ({arrival.port}.{arrival.element}): "
    if arrival.verdict == BOUNDED:
        return (
            f"{head}data arrives at most every {_period_ms(arrival)} ms "
            f"(from {arrival.writer} in {arrival.task}{_more(arrival.more)})"
        )
    if arrival.verdict == UNKNOWN:
        return f"{head}arrival rate unknown: the source is outside the model ({arrival.case}{_more(arrival.more)})"
    return f"{head}not derived: {arrival.case}" + (f" ({arrival.more} more)" if arrival.more else "")


def arrival_json(arrival: Arrival) -> dict[str, Any]:
    """One line as the JSON document carries it: ``periodMs``, ``writer`` and ``task`` for a bound, ``case`` else."""
    entry: dict[str, Any] = {
        "occurrence": arrival.occurrence,
        "runnable": arrival.runnable,
        "port": arrival.port,
        "element": arrival.element,
        "verdict": arrival.verdict,
    }
    if arrival.verdict == BOUNDED:
        entry |= {"periodMs": _period_ms(arrival), "writer": arrival.writer, "task": arrival.task}
    else:
        entry["case"] = arrival.case
    entry["more"] = arrival.more
    return entry


def _arrival_section(arrivals: Sequence[Arrival]) -> list[str]:
    """The text's ``ARRIVALS`` section, or nothing for a model with no data-received event.

    The header counts lines, not events: one per event per walked occurrence, and one more per event past the cap.
    """
    if not arrivals:
        return []
    return [
        "",
        f"ARRIVALS {len(arrivals)} line(s)",
        *(f"  - {arrival_line(arrival)}" for arrival in arrivals),
        f"  {ARRIVALS_NOTE}",
    ]


def _first_arrival_lines(arrivals: Sequence[Arrival]) -> list[str]:
    """The overview's ``first``: at most :data:`ARRIVALS_NAMED` lines, together at most :data:`ARRIVALS_FIRST_BYTES`.

    A prefix of the section: the first line that would pass the byte cap ends
    it. A line is measured as a JSON string inside the escaped compact document,
    as the server's wire carries it.
    """
    return _first_lines(
        [arrival_line(arrival) for arrival in arrivals[:ARRIVALS_NAMED]], ARRIVALS_NAMED, ARRIVALS_FIRST_BYTES
    )


def _first_lines(lines: Sequence[Any], named: int, first_bytes: int) -> list[Any]:
    """A prefix of *lines*: at most *named* of them, together at most *first_bytes* wire bytes.

    :func:`_first_arrival_lines`' rule, shared by the ``IMPLICIT ACCESS`` overview, whose items are rows.
    """
    first: list[Any] = []
    spent = 0
    for line in lines[:named]:
        size = len(json.dumps(json.dumps(line, ensure_ascii=False), ensure_ascii=False).encode("utf-8"))
        if spent + size > first_bytes:
            break
        first.append(line)
        spent += size
    return first


# ---------------------------------------------------------------------------
# The IMPLICIT ACCESS section
#
# Two tables on every surface: the tasks, then the runnables in no task. It marks
# and does not cost: no byte or time figure, no finding, no exit code.
# ---------------------------------------------------------------------------

#: What the section counts and what it does not claim, after the word ``implicit``: the text's header carries it.
IMPLICIT_CLAUSE = (
    "reads and writes the task's runnables declare; when the RTE copies them, and what that costs, is not in the model"
)

#: :data:`IMPLICIT_CLAUSE` as the sentence every other surface carries once: the Markdown, the JSON and the overview.
IMPLICIT_NOTE = f"Implicit {IMPLICIT_CLAUSE}."

#: How many rows the MCP overview carries, over the two tables and the unwalked occurrences together; the rest are
#: counted in its ``more``.
IMPLICIT_NAMED = 20

#: The wire bytes the overview's rows may take together; with the arrivals, two fifths of the response budget.
IMPLICIT_FIRST_BYTES = 16_000

#: Where the overview points for the rows past :data:`IMPLICIT_NAMED` or :data:`IMPLICIT_FIRST_BYTES`.
IMPLICIT_REST = "swcraft resource-report prints every row; its --format json carries them as implicit."

#: The first table's columns: one row per task that maps an implicit access.
IMPLICIT_TASK_COLUMNS = ("Task", "Core", "Implicit reads", "Implicit writes", "Runnables")

#: The second table's heading and columns: one row per runnable occurrence with an implicit access no task maps.
IMPLICIT_NO_TASK_HEADING = "Runnables in no task"
IMPLICIT_NO_TASK_COLUMNS = ("Runnable", "Implicit reads", "Implicit writes")

#: What stands for the first table when no task maps an implicit access (the second table then has every row).
IMPLICIT_NO_TASK_MAPS = "No task maps an implicit access."


def _implicit_parts(
    implicit: Sequence[ImplicitLine],
) -> tuple[list[ImplicitLine], list[ImplicitLine], list[ImplicitLine]]:
    """The section's lines by shape, each in the leaf's order: the tasks, the occurrences in no task, the unwalked."""
    tasks = [line for line in implicit if line.shape == TASK]
    no_task = [line for line in implicit if line.shape == OCCURRENCE]
    not_walked = [line for line in implicit if line.shape == PAST_THE_CAP_LINE]
    return tasks, no_task, not_walked


def _runnable_cell(line: ImplicitLine, name: Callable[[str], str]) -> str:
    """The second table's runnable, ``occurrence.runnable`` spelled by *name*, with why it has no occurrence if so.

    A type placed in no composition is named by its type, and the cell says so: ``Reader.Run (placed in no
    composition)``. An occurrence a walk reached and no task maps needs no word: the table's heading is it.
    """
    cell = name(f"{line.occurrence}.{line.runnable}")
    return cell if line.case == IN_NO_TASK else f"{cell} ({line.case})"


def _not_walked(line: ImplicitLine) -> str:
    """The sentence under the second table for a type placed past the cap: its occurrences left unwalked, counted."""
    return f"{line.case} ({line.more} more)"


def implicit_rows(implicit: Sequence[ImplicitLine]) -> dict[str, list[dict[str, Any]]]:
    """The section as the JSON document carries it: the two tables' rows, then the unwalked occurrences.

    ``tasks`` is the first table (``core`` ``null`` where it does not resolve), ``noTask`` the second, its
    runnable split into ``occurrence`` and ``runnable`` with the ``case`` the text's cell carries, and
    ``notWalked`` the sentences under it, one per type placed past the cap (``type``, ``case``, ``more``). The
    three keys are always there, a list empty when its part is.
    """
    tasks, no_task, not_walked = _implicit_parts(implicit)
    return {
        "tasks": [
            {"task": t.task, "core": t.core, "reads": t.reads, "writes": t.writes, "runnables": t.runnables}
            for t in tasks
        ],
        "noTask": [
            {"occurrence": o.occurrence, "runnable": o.runnable, "reads": o.reads, "writes": o.writes, "case": o.case}
            for o in no_task
        ],
        "notWalked": [{"type": c.occurrence, "case": c.case, "more": c.more} for c in not_walked],
    }


def implicit_overview(implicit: Sequence[ImplicitLine]) -> dict[str, Any]:
    """The MCP overview's ``implicit``: :func:`implicit_rows` cut to a prefix, and ``more``, ``note`` and ``rest``.

    The prefix runs over the rows in the section's order, at most :data:`IMPLICIT_NAMED` of them within
    :data:`IMPLICIT_FIRST_BYTES` wire bytes; ``rest`` (:data:`IMPLICIT_REST`) is there when any row is past it.
    """
    rows = implicit_rows(implicit)
    tagged = [(key, row) for key, part in rows.items() for row in part]
    first = _first_lines([row for _, row in tagged], IMPLICIT_NAMED, IMPLICIT_FIRST_BYTES)
    kept = tagged[: len(first)]
    more = len(tagged) - len(kept)
    answer: dict[str, Any] = {key: [row for tag, row in kept if tag == key] for key in rows}
    return answer | {"more": more, "note": IMPLICIT_NOTE} | ({"rest": IMPLICIT_REST} if more else {})


def _text_table(header: Sequence[str], rows: Sequence[Sequence[str]], indent: str) -> list[str]:
    """Columns aligned by padding, two spaces apart, no trailing blank: ``metrics_render``'s text table's form."""
    cells = [list(header), *(list(row) for row in rows)]
    widths = [max(len(row[index]) for row in cells) for index in range(len(header))]
    return [
        indent + "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in cells
    ]


def implicit_text(implicit: Sequence[ImplicitLine]) -> list[str]:
    """The text's ``IMPLICIT ACCESS`` section, or nothing for a model with no implicit access.

    The header carries :data:`IMPLICIT_CLAUSE`; the task table follows (or :data:`IMPLICIT_NO_TASK_MAPS`), then,
    when any runnable occurrence is in no task or any type is placed past the cap, ``RUNNABLES IN NO TASK`` with
    its table and one sentence per such type under it.
    """
    if not implicit:
        return []
    tasks, no_task, not_walked = _implicit_parts(implicit)
    lines = ["", f"IMPLICIT ACCESS (implicit {IMPLICIT_CLAUSE})"]
    if tasks:
        rows = [(t.task or "", t.core or UNKNOWN_CORE, str(t.reads), str(t.writes), str(t.runnables)) for t in tasks]
        lines += _text_table(IMPLICIT_TASK_COLUMNS, rows, "  ")
    else:
        lines.append(f"  {IMPLICIT_NO_TASK_MAPS}")
    if no_task or not_walked:
        lines.append(f"  {IMPLICIT_NO_TASK_HEADING.upper()}")
        if no_task:
            no_task_rows = [(_runnable_cell(o, str), str(o.reads), str(o.writes)) for o in no_task]
            lines += _text_table(IMPLICIT_NO_TASK_COLUMNS, no_task_rows, "    ")
        lines += [f"    {c.occurrence}: {_not_walked(c)}" for c in not_walked]
    return lines


def implicit_markdown(implicit: Sequence[ImplicitLine]) -> list[str]:
    """The report file's ``## Implicit access`` section: :func:`implicit_text`'s content as Markdown tables.

    Names are code spans and every model text a cell (``markdown_cells``), as the consumer tables' are; the
    unwalked occurrences are a list under the second table. Ends with an empty line.
    """
    tasks, no_task, not_walked = _implicit_parts(implicit)
    lines = ["## Implicit access", "", IMPLICIT_NOTE, ""]
    if tasks:
        rows = [
            (
                code_span(t.task or ""),
                code_span(t.core) if t.core is not None else cell_text(UNKNOWN_CORE),
                str(t.reads),
                str(t.writes),
                str(t.runnables),
            )
            for t in tasks
        ]
        lines += [*table(IMPLICIT_TASK_COLUMNS, rows), ""]
    else:
        lines += [IMPLICIT_NO_TASK_MAPS, ""]
    if no_task or not_walked:
        lines += [f"### {IMPLICIT_NO_TASK_HEADING}", ""]
        if no_task:
            no_task_rows = [(_runnable_cell(o, code_span), str(o.reads), str(o.writes)) for o in no_task]
            lines += [*table(IMPLICIT_NO_TASK_COLUMNS, no_task_rows), ""]
        if not_walked:
            lines += [f"- {code_span(c.occurrence or '')}: {cell_text(_not_walked(c))}" for c in not_walked]
            lines.append("")
    return lines
