"""The result types every check returns, and the severity of a rule.

Holds :class:`ConsistencyIssue` (one finding), :class:`ConsistencyResult` (a check's
answer, split into errors and warnings), :class:`CompatibilityResult`, the warning
rule set :data:`WARNING_RULES` and the helpers that build and merge results.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from swcraft_tools.diagnostics import Subject

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CompatibilityResult:
    """Result of a port compatibility check.

    ``rule`` names the :data:`CONSISTENCY_RULES` member an incompatibility is; it is empty on a
    compatible result and on :func:`check_port_compatibility`, whose answer is never a
    consistency finding.
    """

    compatible: bool
    reason: str = ""
    rule: str = ""


#: The two classes a finding can have. A rule's class is declared once, in the
#: model contract (`ruleDecl.severity`); an emission site never states one.
SEVERITY_ERROR = "error"


SEVERITY_WARNING = "warning"


#: The rules whose findings are warnings — listed by ``check-model`` and
#: ``validate``, never a refusal; every other rule is an error. Must equal the
#: contract's ``severity: warning`` declarations. A variable read with no ``init``
#: is warned because the standard leaves such a read undefined before a write and
#: the model states no order. A rule moves here by a contract change first.
WARNING_RULES: frozenset[str] = frozenset(
    {
        "trace.requirement-unallocated",
        "trace.requirement-stub",
        "trace.link-version-not-stated",
        "trace.link-needs-confirmation",
        "swc.variable.read-without-init",
        "trace.link-unpublishable-id",
        "trace.requirement-blank-text",
    }
)


def severity_of(rule: str) -> str:
    """The class of *rule*'s findings: warning for a member of :data:`WARNING_RULES`, else error."""
    return SEVERITY_WARNING if rule in WARNING_RULES else SEVERITY_ERROR


@dataclass(frozen=True)
class ConsistencyIssue:
    """One consistency finding: which rule, in the rule's own words, and where.

    Args:
        rule: a member of :data:`CONSISTENCY_RULES`, the stable key a consumer reads (never the
            message); or, from ``check-model`` and ``validate`` only, ``cli.MANIFEST_UNKNOWN_KEY_RULE``,
            the one rule outside the contract.
        message: the finding's prose, the ``issues`` entry itself.
        subjects: the model places the finding is about. For a reference rule these are the
            **referrers**, never the missing thing, because the referrers are what a repair changes.
        target: for a reference rule, the identity that does not resolve, ``None`` otherwise. A
            **grouping key** (one missing interface named by four ports is one finding with four
            subjects), never an address. For ``cli.MANIFEST_UNKNOWN_KEY_RULE``, the top-level
            manifest key the finding sits under.

    ``severity`` is read off the rule, never stored, so it cannot disagree with the contract.
    """

    rule: str
    message: str
    subjects: tuple[Subject, ...] = ()
    target: str | None = None

    @property
    def severity(self) -> str:
        """The rule's class, :func:`severity_of` applied to ``rule``."""
        return severity_of(self.rule)


@dataclass
class ConsistencyResult:
    """Result of a cross-layer consistency check.

    ``consistent`` and ``issues`` are the verdict over the **error** findings and their prose;
    ``records`` holds the same errors, structured, in the same order. ``warnings`` holds the
    :data:`WARNING_RULES` findings in emission order and decides nothing: a result with warnings
    alone is ``consistent``.
    """

    consistent: bool
    issues: list[str] = field(default_factory=list)
    records: tuple[ConsistencyIssue, ...] = ()
    warnings: tuple[ConsistencyIssue, ...] = ()


def consistency_result(records: Sequence[ConsistencyIssue]) -> ConsistencyResult:
    """The result of *records*, split into errors and warnings by :func:`severity_of`.

    The one place a finding's class is applied; every entry point of the package returns through it.
    """
    errors = [record for record in records if record.severity == SEVERITY_ERROR]
    warnings = [record for record in records if record.severity == SEVERITY_WARNING]
    return ConsistencyResult(
        consistent=len(errors) == 0,
        issues=[record.message for record in errors],
        records=tuple(errors),
        warnings=tuple(warnings),
    )


def with_records(result: ConsistencyResult, records: Sequence[ConsistencyIssue]) -> ConsistencyResult:
    """*result* with *records* placed **first**, rebuilt through :func:`consistency_result`.

    For a finding no check of the package emits (an undeclared manifest key). Nothing is
    deduplicated; with no record to add, *result* is returned as it is.

    What it does not reach: the rebuild reads *result*'s records, never its ``issues``, so a
    result built by hand with ``issues`` and no ``records`` keeps its prose only when nothing is added.
    """
    if not records:
        return result
    return consistency_result([*records, *result.records, *result.warnings])


def deduplicated(records: Iterable[ConsistencyIssue]) -> list[ConsistencyIssue]:
    """*records* with repeated messages dropped, first occurrence kept.

    Keyed on the message: two walks that reach the same finding word it identically.
    """
    seen: set[str] = set()
    unique: list[ConsistencyIssue] = []
    for record in records:
        if record.message in seen:
            continue
        seen.add(record.message)
        unique.append(record)
    return unique
