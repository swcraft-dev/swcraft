"""Structured budget analysis over the ``hardware/`` layer.

This module owns **all** budget arithmetic. It is not part of the validator so
that the resource report can consume the structured result without importing
the validator; the validator runs :func:`analyze_resources` over its view, the
report reads :func:`analyze_resource_budgets`, and neither recomputes a figure.

Checks performed, in the order their findings appear:

1. Missing consumer budgets (``Task.wcet`` / ``SWComponent.memory[memory]``).
2. CPU utilization against the core's ``threshold``.
3. Memory consumption against ``size * threshold``.
4. Rate-monotonic priority conformance among tasks sharing a core.

The structural checks (duplicate names, an undeclared memory, a task's
partition or core naming nothing, an unreachable memory) are the validator's
``hardware.*`` rules, not this module's.

**One finding level.** Every finding is attributable to one core or one memory
and lives in that element's :attr:`ResourceUsage.findings`. Its severity
**follows the element's ``enforcement``**: ``strict`` produces ``error``
findings, ``advisory`` produces ``info`` findings. There is no model-wide level.

The arithmetic reads the declared :class:`~swcraft_tools.schema.Core` and
:class:`~swcraft_tools.schema.Memory` elements themselves. The processor is
skipped: it declares no ``threshold``, no ``size`` and no ``enforcement``. A
model whose ``hardware/`` declares neither a core nor a memory yields an empty
analysis, and :func:`analyze_resource_budgets` then reads no task or SWC layer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import TYPE_CHECKING

from swcraft_tools.diagnostics import Subject, field_step, key_step
from swcraft_tools.execution_budget import UNIT_LIST, Budget, parse_budget
from swcraft_tools.reader import read_hardware, read_swc_model, read_tasks
from swcraft_tools.schema import Core, Hardware, Memory, SWComponent, Task, core_of, hardware_by_name

if TYPE_CHECKING:
    # `TypeIs` is in `typing_extensions` from 4.10, above the declared floor; the
    # annotation that names it is never evaluated, so no run imports it.
    from typing_extensions import TypeIs

# ---------------------------------------------------------------------------
# Severities and finding codes
# ---------------------------------------------------------------------------

SEVERITY_ERROR = "error"
SEVERITY_INFO = "info"

#: The finding codes this module emits — severity follows the owning element's
#: ``enforcement``.
FINDING_MISSING_BUDGET = "missing_budget"
FINDING_THRESHOLD_BREACH = "threshold_breach"
FINDING_RATE_MONOTONIC_VIOLATION = "rate_monotonic_violation"

#: Rendered status of a resource (see :func:`budget_status`).
STATUS_OK = "within budget"
STATUS_BREACH = "over budget"
#: A ``strict`` resource with at least one consumer that declared nothing: the
#: figure omits that consumer's load entirely, so the headline must not read as
#: a pass.
STATUS_UNKNOWN = "unknown (not every consumer declared a budget)"

#: Rendered contribution of a consumer that declared no budget.
NOT_DECLARED = "not declared"

#: The example value each ``missing_budget`` sentence offers, so the message says
#: what to write. Each is a value the loader accepts: a ``memory`` entry is a whole
#: number of bytes at least ``0``, a ``wcet`` a positive whole number with one of
#: :data:`~swcraft_tools.execution_budget.UNITS`.
MEMORY_BUDGET_EXAMPLE = 1024
WCET_BUDGET_EXAMPLE = "500us"

#: The bound a ``memory`` budget holds (``>= 0``), as ``SWComponent.memory`` states it.
_MEMORY_BUDGET_BOUND = "a whole number of at least 0"

#: Prefix marking a figure that could not be rendered exactly at two decimals.
_APPROX = "~"


# ---------------------------------------------------------------------------
# What a budget is about
# ---------------------------------------------------------------------------

#: The two hardware kinds that carry a budget.
#: :class:`~swcraft_tools.schema.Processor` is deliberately not a member; it
#: declares no ``threshold``, no ``size`` and no ``enforcement``.
Resource = Core | Memory


def is_core(resource: Resource) -> TypeIs[Core]:
    """Whether *resource* is a core rather than a memory.

    Public so that a renderer discriminates without importing the schema classes.
    """
    return isinstance(resource, Core)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One resource-budget finding.

    Carries **no** owning-resource field: every finding sits in its owning
    :attr:`ResourceUsage.findings`, and every ``message`` names its subject.

    ``subjects`` names the model places the finding is *about*, in
    :mod:`swcraft_tools.diagnostics`' layer/element/path form; it never replaces
    the message, which is what ``check-model`` and ``swcraft resource-report`` print.

    ``target`` is the identity that does not resolve, for a finding about a
    reference; no finding this module builds sets it, but the validator maps the
    field for every rule.
    """

    severity: str
    code: str
    message: str
    subjects: tuple[Subject, ...] = ()
    target: str | None = None


@dataclass(frozen=True)
class ConsumerUsage:
    """One consumer's contribution to a resource.

    ``amount`` is in the budget's own unit: a utilization fraction for a core (so ``2/5`` means
    40 % of the core), bytes for a memory. A consumer that declared no budget contributes zero and
    carries ``declared=False`` — under ``strict`` enforcement that is a finding, under ``advisory``
    the missing budget simply counts as zero. ``partition`` is a task's (:func:`_in_partitions`)
    and empty on a memory's consumer.
    """

    name: str
    amount: Fraction
    declared: bool = True
    detail: str = ""
    partition: str = ""


@dataclass
class ResourceUsage:
    """Aggregated usage of a single declared resource.

    ``consumption``, ``limit`` and ``headroom`` are exact
    :class:`~fractions.Fraction` values, not ``float``s: the threshold verdict is
    decided by exact rational arithmetic, and the printed figures must be the
    same numbers that verdict was reached on.
    """

    resource: Resource
    consumption: Fraction = Fraction(0)
    limit: Fraction = Fraction(0)
    headroom: Fraction = Fraction(0)
    consumers: list[ConsumerUsage] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.resource.name


@dataclass
class ResourceAnalysis:
    """Result of :func:`analyze_resource_budgets`.

    ``resources`` holds one :class:`ResourceUsage` per declared core and memory,
    in reader order.
    """

    resources: list[ResourceUsage] = field(default_factory=list)

    def all_findings(self) -> list[Finding]:
        """Every finding, per resource in reader order."""
        result: list[Finding] = []
        for usage in self.resources:
            result.extend(usage.findings)
        return result

    def error_findings(self) -> list[Finding]:
        """Only the ``error``-severity findings."""
        return [f for f in self.all_findings() if f.severity == SEVERITY_ERROR]


# ---------------------------------------------------------------------------
# Arithmetic helpers
# ---------------------------------------------------------------------------


def _severity_for(resource: Resource) -> str:
    """``strict`` budgets produce errors, ``advisory`` ones informational lines."""
    return SEVERITY_ERROR if resource.enforcement == "strict" else SEVERITY_INFO


def _exact(value: float) -> Fraction:
    """Exact rational value of a model float.

    ``Fraction(str(value))`` deliberately, not ``Fraction(value)``: Python's
    shortest round-trip repr recovers the decimal the architect authored, while
    the binary-float constructor would capture its approximation.
    """
    return Fraction(str(value))


def _cpu_utilization(budget: Budget, period: float) -> Fraction:
    """Exact utilization contribution of one periodic task.

    ``period`` is not quantized: rounding a positive float would need an
    unspecified rule and could collapse a short period to zero.
    """
    return budget.seconds / _exact(period)


# ---------------------------------------------------------------------------
# Rendering helpers — public so every figure is rendered here and nowhere else,
# and one number is never spelled two ways in one report.
# ---------------------------------------------------------------------------


def _as_fraction(value: float | Fraction) -> Fraction:
    """Exact rational value of a float or an already-exact ``Fraction``."""
    return value if isinstance(value, Fraction) else _exact(value)


def _truncated_2dp(exact: Fraction) -> tuple[str, bool]:
    """Fixed two-decimal text for *exact*, truncated downward, and a lossy flag.

    Truncated, never rounded: a rendered figure must never read *higher* than
    the exact value it stands for, or a marginal breach prints the
    self-contradictory "consumption 838861 bytes exceeds its budget of 838861
    bytes" (the exact budget being 838860.8). Downward (floor), not toward
    zero, so a headroom of -0.008 bytes reads ``~-0.01``, not ``~-0``.
    """
    scaled = math.floor(exact * 100)  # exact Fraction floor, no float step
    lossy = Fraction(scaled, 100) != exact
    sign = "-" if scaled < 0 else ""
    whole, hundredths = divmod(abs(scaled), 100)
    return f"{sign}{whole}.{hundredths:02d}", lossy


def format_percent(value: float | Fraction) -> str:
    """Render a utilization fraction as a percentage (``0.55`` -> ``55.00%``).

    A value that two decimals cannot express exactly is prefixed with ``~``, so
    a breach at the boundary reads "utilization ~70.00% exceeds its threshold of
    70.00%" rather than claiming 70.00% exceeds itself.
    """
    text, lossy = _truncated_2dp(_as_fraction(value) * 100)
    return f"{_APPROX}{text}%" if lossy else f"{text}%"


def format_bytes(value: float | Fraction) -> str:
    """Render a byte count, dropping a trailing ``.0`` (``209715.2`` stays exact).

    Not ``%g``, which renders a multi-megabyte pool in exponential notation *and*
    rounds it up. Same ``~`` marker as :func:`format_percent`.
    """
    text, lossy = _truncated_2dp(_as_fraction(value))
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{_APPROX}{text}" if lossy else text


def budget_status(usage: ResourceUsage) -> str:
    """The resource's headline verdict.

    :data:`STATUS_BREACH` when the resource is over its threshold — the
    threshold verdict the analysis already decided; a rate-monotonic finding is
    a real problem but does not put the resource over its budget.

    :data:`STATUS_UNKNOWN` when a ``strict`` budget has **any** consumer that
    declared no budget: that load is missing from the figure, so "within budget"
    would vouch for a number that does not describe the resource. ``advisory``
    counts a missing budget as zero by design. A resource with no consumers at
    all stays :data:`STATUS_OK`.
    """
    if any(f.code == FINDING_THRESHOLD_BREACH for f in usage.findings):
        return STATUS_BREACH
    if usage.resource.enforcement == "strict" and any(not c.declared for c in usage.consumers):
        return STATUS_UNKNOWN
    return STATUS_OK


def format_amount(resource: Resource, consumer: ConsumerUsage) -> str:
    """One consumer's contribution in the budget's own unit, or :data:`NOT_DECLARED`."""
    if not consumer.declared:
        return NOT_DECLARED
    if is_core(resource):
        return format_percent(consumer.amount)
    return f"{format_bytes(consumer.amount)} bytes"


# ---------------------------------------------------------------------------
# Rate-monotonic priority conformance
# ---------------------------------------------------------------------------


def _check_rate_monotonic(resource: Core, core_tasks: list[Task], severity: str) -> list[Finding]:
    """Per-resource check 7: rate-monotonic priority conformance.

    Among tasks sharing a core, a strictly shorter ``period`` must carry a
    strictly higher ``priority`` (the SWCraft convention: higher number = more
    urgent). Equal periods impose no ordering. Tasks on different cores are
    never compared. Findings follow the core's ``enforcement``.

    **One finding per inversion, not per violating pair**: only *adjacent* pairs
    with differing periods are compared after sorting by (period ascending,
    priority descending), which is complete by transitivity and keeps an inverted
    core from burying every other issue.
    """
    ordered = sorted(core_tasks, key=lambda task: (_exact(task.period), -task.priority, task.name))
    findings: list[Finding] = []
    for shorter, longer in zip(ordered, ordered[1:]):
        if _exact(shorter.period) == _exact(longer.period) or shorter.priority > longer.priority:
            continue
        findings.append(
            Finding(
                severity=severity,
                code=FINDING_RATE_MONOTONIC_VIOLATION,
                message=(
                    f"Rate-monotonic violation on core {resource.name}: task "
                    f"'{shorter.name}' (period {shorter.period}s) has priority "
                    f"{shorter.priority}, which is not higher than the priority "
                    f"{longer.priority} of the longer-period task '{longer.name}' "
                    f"(period {longer.period}s)."
                ),
                subjects=(
                    Subject("tasks", shorter.name, (field_step("priority"),)),
                    Subject("tasks", longer.name, (field_step("priority"),)),
                ),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Per-resource usage
# ---------------------------------------------------------------------------


def _analyze_cpu_core(resource: Core, tasks: list[Task], by_name: Mapping[str, Hardware]) -> ResourceUsage:
    """Build the usage breakdown and verdicts for one declared core.

    A task belongs to this core when its partition's ``core`` names the element,
    resolved through ``schema.core_of`` over *by_name* (first wins).

    What this does **not** reach: a task whose partition names no declared
    partition, or whose partition names no declared core, is in no block, no
    total and no finding; ``rule:hardware.undeclared-partition`` and
    ``rule:hardware.partition-undeclared-core`` (and their two ``not-a``
    siblings) report it.
    """
    severity = _severity_for(resource)
    consumers: list[ConsumerUsage] = []
    findings: list[Finding] = []
    total = Fraction(0)
    core_tasks = [task for task in tasks if (core := core_of(task, by_name)) is not None and core.name == resource.name]

    for task in core_tasks:
        if task.wcet is None:
            consumers.append(
                ConsumerUsage(
                    name=task.name,
                    amount=Fraction(0),
                    declared=False,
                    detail=f"period {task.period}s, no wcet declared",
                )
            )
            findings.append(
                Finding(
                    severity=severity,
                    code=FINDING_MISSING_BUDGET,
                    message=(
                        f"Task '{task.name}' on core '{resource.name}' declares no 'wcet' budget: set its `wcet` "
                        f"to a positive whole number with one of {UNIT_LIST}, e.g. `wcet: {WCET_BUDGET_EXAMPLE}`."
                    ),
                    subjects=(Subject("tasks", task.name, (field_step("wcet"),)),),
                )
            )
            continue
        utilization = _cpu_utilization(parse_budget(task.wcet), task.period)
        total += utilization
        consumers.append(
            ConsumerUsage(
                name=task.name,
                amount=utilization,
                detail=f"wcet {task.wcet} / period {task.period}s",
            )
        )

    limit = _exact(resource.threshold)
    # Exact rational comparison: a plain float product misjudges the boundary
    # (2350us / 5ms is 0.47000000000000003 in binary floats, a false breach of
    # a 0.47 threshold).
    if total > limit:
        findings.append(
            Finding(
                severity=severity,
                code=FINDING_THRESHOLD_BREACH,
                message=(
                    f"CPU core '{resource.name}' utilization {format_percent(total)} "
                    f"exceeds its threshold of {format_percent(limit)}."
                ),
                # Raising the threshold and lowering a `wcet` are the two repairs.
                subjects=(
                    Subject("hardware", resource.name, (field_step("threshold"),)),
                    *(Subject("tasks", task.name, (field_step("wcet"),)) for task in core_tasks),
                ),
            )
        )

    findings.extend(_check_rate_monotonic(resource, core_tasks, severity))

    return ResourceUsage(
        resource=resource,
        consumption=total,
        limit=limit,
        headroom=limit - total,
        consumers=_in_partitions(consumers, core_tasks),
        findings=findings,
    )


def _analyze_memory(resource: Memory, swcs: list[SWComponent]) -> ResourceUsage:
    """Build the usage breakdown and verdict for one declared memory.

    **An external stub is not a consumer**: its implementation is one this
    project neither declares nor builds. A stub contributes no
    :class:`ConsumerUsage`, no byte to the total and no
    ``FINDING_MISSING_BUDGET``, and the threshold breach never names one.

    What the skip does **not** reach:

    - It is a check about a **declared** budget, never about consumption: a
      stub's foreign implementation does occupy memory on the real ECU, and it
      is charged to nothing here.
    - A ``memory`` budget declared **on** a stub is dropped here unreported;
      ``rule:swc.external-with-ports`` reports it and the writer refuses it.
    - The CPU arm needs no skip: :func:`_analyze_cpu_core` never sees the
      ``swc`` layer.
    """
    severity = _severity_for(resource)
    consumers: list[ConsumerUsage] = []
    findings: list[Finding] = []
    charged: list[SWComponent] = []
    total = 0

    for swc in swcs:
        if swc.external:
            continue
        declared = swc.memory.get(resource.name)
        if declared is None:
            consumers.append(ConsumerUsage(name=swc.name, amount=Fraction(0), declared=False))
            findings.append(
                Finding(
                    severity=severity,
                    code=FINDING_MISSING_BUDGET,
                    message=(
                        f"SWC '{swc.name}' declares no budget for memory '{resource.name}': add `{resource.name}` "
                        f"to its `memory` map with the bytes it uses, {_MEMORY_BUDGET_BOUND}, "
                        f"e.g. `{resource.name}: {MEMORY_BUDGET_EXAMPLE}`."
                    ),
                    subjects=(Subject("swc", swc.name, (field_step("memory"), key_step(resource.name))),),
                )
            )
            continue
        total += declared
        charged.append(swc)
        consumers.append(ConsumerUsage(name=swc.name, amount=Fraction(declared)))

    limit = Fraction(resource.size) * _exact(resource.threshold)
    # Exact rational comparison: `100 * 0.29` is 28.999999999999996 in binary
    # floats, which would report a 29-byte consumption as over a 29-byte limit.
    if Fraction(total) > limit:
        findings.append(
            Finding(
                severity=severity,
                code=FINDING_THRESHOLD_BREACH,
                message=(
                    f"Memory '{resource.name}' consumption {total} bytes "
                    f"exceeds its budget of {format_bytes(limit)} bytes "
                    f"(size {resource.size} bytes x threshold {resource.threshold})."
                ),
                # `charged`, not `swcs`: a skipped stub never appears as a repair site.
                subjects=(
                    Subject("hardware", resource.name, (field_step("size"),)),
                    *(Subject("swc", swc.name, (field_step("memory"), key_step(resource.name))) for swc in charged),
                ),
            )
        )

    return ResourceUsage(
        resource=resource,
        consumption=Fraction(total),
        limit=limit,
        headroom=limit - total,
        consumers=consumers,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def analyze_resource_budgets(model_root: str) -> ResourceAnalysis:
    """Analyze the budgets the model's ``hardware/`` layer declares, read from disk.

    Reads ``tasks/`` and ``swc/`` only when :func:`has_resources` finds a
    declared core or memory.

    Args:
        model_root: Root directory of the model.

    Returns:
        The :class:`ResourceAnalysis` :func:`analyze_resources` computes.

    Raises:
        ValueError: if any ``hardware/*.yaml`` or ``tasks/*.yaml`` file is
            malformed (propagated from the reader).
    """
    hardware = read_hardware(model_root)
    if not has_resources(hardware):
        return ResourceAnalysis()
    tasks = read_tasks(model_root)
    swcs = read_swc_model(model_root)
    return analyze_resources(hardware, tasks, swcs)


def has_resources(hardware: list[Hardware]) -> bool:
    """Whether *hardware* declares a core or a memory, the elements that carry a budget.

    Args:
        hardware: The ``hardware`` layer's elements.
    """
    return any(isinstance(element, Resource) for element in hardware)


def analyze_resources(hardware: list[Hardware], tasks: list[Task], swcs: list[SWComponent]) -> ResourceAnalysis:
    """Analyze the budgets *hardware* declares against the loaded *tasks* and *swcs*.

    Reads nothing from disk.

    Returns:
        A :class:`ResourceAnalysis` holding one :class:`ResourceUsage` per
        declared core and memory, in *hardware*'s order; an empty analysis when
        *hardware* declares neither. The processor produces no usage.
    """
    resources = [element for element in hardware if isinstance(element, Resource)]
    if not resources:
        return ResourceAnalysis()
    by_name = hardware_by_name(hardware)

    usages = [
        _analyze_cpu_core(resource, tasks, by_name) if is_core(resource) else _analyze_memory(resource, swcs)
        for resource in resources
    ]

    return ResourceAnalysis(resources=usages)


# ---------------------------------------------------------------------------
# One runnable's share (the allocation experiment)
# ---------------------------------------------------------------------------


def runnable_utilization(budget: Budget, period: float) -> Fraction:
    """Exact utilization share of one runnable budgeted *budget* in a task of *period* seconds.

    Computed exactly as a task's share, so this module stays the one place
    budget arithmetic happens.
    """
    return _cpu_utilization(budget, period)


def exact(value: float) -> Fraction:
    """The public face of :func:`_exact` for :mod:`swcraft_tools.allocation`: a model float's exact rational."""
    return _exact(value)


def format_decimal(value: float | Fraction) -> str:
    """Render a figure with no unit of this module's own (``1/3`` -> ``~0.33``, ``25/2`` -> ``12.5``).

    :func:`format_bytes`' rule for a figure that is not a byte count, such as
    the allocation experiment's volumes in bytes x Hz.
    """
    text, lossy = _truncated_2dp(_as_fraction(value))
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{_APPROX}{text}" if lossy else text


# ---------------------------------------------------------------------------
# A question about one resource (the MCP `resource_report` tool)
# ---------------------------------------------------------------------------


def fits(usage: ResourceUsage, amount: Fraction) -> bool | None:
    """Whether *amount* more fits *usage*'s budget; ``None`` for a "yes" a missing load could falsify.

    *amount* fits when ``consumption + amount <= limit`` (``limit`` is the
    core's ``threshold``, or the memory's ``size x threshold``), in exact
    arithmetic. ``None`` exactly when the sum is within the limit **and**
    :func:`budget_status` answers :data:`STATUS_UNKNOWN`. A "no" is always
    given: the missing load only adds.

    *amount* is in the budget's own unit: a utilization fraction for a core
    (``1/20`` is 5 % of it), bytes for a memory.
    """
    within = usage.consumption + amount <= usage.limit
    if within and budget_status(usage) == STATUS_UNKNOWN:
        return None
    return within


def reachable_memories(analysis: ResourceAnalysis, core: str) -> list[ResourceUsage]:
    """The memories whose ``accessible`` names the core *core*, in reader order.

    A read of the declaration, not a check: an entry naming no declared core is
    ``rule:hardware.accessible-undeclared-core``'s to report, and is matched here
    only when *core* is that same name.
    """
    return [
        usage
        for usage in analysis.resources
        if isinstance(usage.resource, Memory) and core in usage.resource.accessible
    ]


# ---------------------------------------------------------------------------
# The physical arithmetic: the second fit verdict
# ---------------------------------------------------------------------------


def physical_limit(usage: ResourceUsage) -> Fraction:
    """The most *usage*'s resource can hold at all: a memory's ``size`` in bytes, or ``1`` (100 %) for a core.

    The threshold plays no part. What it does **not** reach: the topology
    (``rule:hardware.unreachable-memory``'s) and the processor, which declares
    no ``size``.
    """
    if isinstance(usage.resource, Core):
        return Fraction(1)
    return Fraction(usage.resource.size)


def fits_size(usage: ResourceUsage, amount: Fraction) -> bool | None:
    """Whether *amount* more fits *usage*'s resource physically; ``None`` for a "yes" a missing load could falsify.

    ``consumption + amount <= physical_limit(usage)``, in exact arithmetic.
    ``None`` exactly when the resource is ``strict``, a consumer declared
    nothing **and** the sum is within the size; a "no" is always given. Keyed on
    the declarations, not on :func:`budget_status`, which answers *over budget*
    before *unknown*. ``advisory`` always answers.

    *amount* is in the budget's own unit, as for :func:`fits`. What it does
    **not** reach is :func:`physical_limit`'s: the topology and the processor.
    """
    within = usage.consumption + amount <= physical_limit(usage)
    if within and usage.resource.enforcement == "strict" and any(not c.declared for c in usage.consumers):
        return None
    return within


# ---------------------------------------------------------------------------
# The partitions of a core's tasks, and whole bytes (the report file)
# ---------------------------------------------------------------------------


def _in_partitions(consumers: list[ConsumerUsage], tasks: list[Task]) -> list[ConsumerUsage]:
    """*consumers* with each one's partition: :func:`_analyze_cpu_core` builds exactly one per task, in order.

    A task reaches a core only through its partition, so each name here is a
    declared partition.
    """
    return [replace(consumer, partition=task.partition) for consumer, task in zip(consumers, tasks, strict=True)]


def partition_shares(usage: ResourceUsage) -> list[tuple[str, Fraction]]:
    """Each partition of *usage*'s consumers with its summed share, largest first, ties by name.

    A sum, never a verdict; the shares add up to :attr:`ResourceUsage.consumption`.

    What it does **not** reach: a partition no task names, and a load a task did
    not declare, which is zero here as in the core's total. Empty for a memory
    and for a core with no task.
    """
    shares: dict[str, Fraction] = {}
    for consumer in usage.consumers:
        if consumer.partition:
            shares[consumer.partition] = shares.get(consumer.partition, Fraction(0)) + consumer.amount
    return sorted(shares.items(), key=lambda share: (-share[1], share[0]))


def format_whole_bytes(value: Fraction) -> str:
    """A byte figure as whole bytes, rounded down (``209715.2`` -> ``209715``, ``-723.2`` -> ``-724``): display only.

    Down so a figure never reads higher than its exact value and a breach
    never reads as ``0`` left. A use is a whole number of bytes, so this
    spelling cannot disagree with a verdict.
    """
    return str(math.floor(value))
