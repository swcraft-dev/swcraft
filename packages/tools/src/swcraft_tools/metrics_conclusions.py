"""The architecture metrics' conclusions: what the figures of one report say about this model, in sentences.

:func:`conclude` reads a finished :class:`~swcraft_tools.metrics.MetricsReport` and returns, for each
section, its sentences and its limit, and the two answers the report's *Overview* opens with.

**The two answers** are in plain words, with no symbol (``Ca``, ``Ce``, ``Ct``) and no technical term (hub,
deviation, cohesion, stub, ``n/a``): *What state is the architecture in?* in at most
:data:`STATE_ANSWER_SENTENCES` sentences, *Where is it heading?* in at most :data:`HEADING_ANSWER_SENTENCES`.
The first holds the size and integrity headlines, the hubs and the low cohesion; the second the change of
the dependencies, the components added and removed and the findings, or why there is nothing to compare
(a file's parse error quoted as it is). Each restates a figure its section states, so the two cannot disagree.

**A deterministic function of the figures.** No model, clock, locale, environment or float reaches a
sentence: a figure is a ``Fraction`` until it is printed, rounded half-up by exact comparison
(:func:`round_hundredths`), and every order is by figure, ties by name.

**A fact with its meaning, never a verdict.** A sentence restates figures and names at most three
components, then how many more (``A, B, C, and 2 more``). No sentence carries a score or a good or bad word:
cohesion below 0.5 is labelled *not calibrated* wherever it appears. Where a figure is ``n/a``, the sentence
says what the model lacks and what that leaves unanswered. A share reads *One component in 8*, *3 of 42
components*, *All 6 components*, *Both components* or *No component*; a percentage change that rounds to 0
reads *by under 1 %*, and a change from zero has none.

A conclusion knows no project's intent, so a component with no link by design reads as one with no link.
English is the only language, and a number is written in digits.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from swcraft_tools.change_report import WORKING_TREE
from swcraft_tools.metrics import (
    END_NOT_A_MEMBER,
    LOOK_AT_KEYS,
    NO_COHESION_FIGURE,
    NO_TASK,
    ONE_DIRECTION,
    REPEATED_NAME,
    UNREACHED_END,
    UNRESOLVED_OWNER,
    UNRESOLVED_PORT,
    ArchitectureState,
    AsilCoverage,
    CompositionTable,
    Coverage,
    CtChange,
    Integrity,
    LookAtLine,
    MetricsReport,
    Movement,
    MovementFigure,
    NotAvailable,
    RequirementCoverage,
    RunnableCoverage,
    TaskCoverage,
)

__all__ = [
    "HEADING_ANSWER_SENTENCES",
    "LOOK_AT_LABELS",
    "MOVEMENT",
    "NAMES_IN_A_SENTENCE",
    "NO_BASE_NAMED",
    "SECTION_KEYS",
    "STATE_ANSWER_SENTENCES",
    "Conclusions",
    "SectionConclusion",
    "conclude",
    "count_of",
    "format_change",
    "format_exact",
    "format_hundredths",
    "format_shown_change",
    "name_list",
    "outlier_bound",
    "round_hundredths",
    "shown_difference",
]

#: The sections in the order they are printed and concluded.
SECTION_KEYS = ("size", "coupling", "tables", "cohesion", "coverage", "integrity", "movement", "look_at")
#: The key of the section whose sentences are the second answer.
MOVEMENT = "movement"
#: The most sentences each of the two answers holds: a few, for a reader who reads nothing else.
STATE_ANSWER_SENTENCES = 4
HEADING_ANSWER_SENTENCES = 3
#: How many names a sentence names before it counts the rest.
NAMES_IN_A_SENTENCE = 3

#: The second answer when no base revision was named.
NO_BASE_NAMED = "No base revision was named, so the direction is unanswered; --since names one."

#: Each line of *what to look at* by its key, in :data:`~swcraft_tools.metrics.LOOK_AT_KEYS`' order.
LOOK_AT_LABELS = dict(
    zip(
        LOOK_AT_KEYS,
        (
            "Errors",
            "Hubs",
            "No link at all",
            "Statistically isolated",
            "Cohesion below 0.5, not calibrated",
            "Runnables on no task",
            "New hubs",
            "Rose most",
            "Findings introduced",
        ),
        strict=True,
    )
)

_MINUS = "−"
_DEPENDENCIES = "Dependencies"
_MEAN_CT = "Mean Ct"


# ---------------------------------------------------------------------------
# Exact numbers
# ---------------------------------------------------------------------------


def _at_most(t: Fraction, b: Fraction, v: Fraction) -> bool:
    """Whether ``t <= b·√v``, decided on squares, without a root."""
    if b == 0 or v == 0:
        return t <= 0
    if b > 0:
        return t <= 0 or t * t <= b * b * v
    return t <= 0 and t * t >= b * b * v


def _approximate_root(v: Fraction) -> Fraction:
    """``√v`` to within a millionth, only to start the exact search near its answer."""
    return Fraction(math.isqrt(v.numerator * v.denominator * 10**12), v.denominator * 10**6)


def round_hundredths(a: Fraction | int, b: Fraction | int = 0, v: Fraction | int = 0) -> int:
    """``a + b·√v`` in hundredths, rounded half-up on the whole expression.

    The answer is the largest integer ``k`` with ``k/100 − ½/100 ≤ a + b·√v``.

    *v* is not negative. Every comparison is exact, so the answer is the same on any platform.
    """
    a, b, v = Fraction(a), Fraction(b), Fraction(v)

    def fits(k: int) -> bool:
        return _at_most(Fraction(2 * k - 1, 200) - a, b, v)

    k = math.floor(100 * (a + b * _approximate_root(v)) + Fraction(1, 2))
    while not fits(k):
        k -= 1
    while fits(k + 1):
        k += 1
    return k


def outlier_bound(a: Fraction, b: int, v: Fraction) -> str:
    """The bound a ``Ct`` passes to be an outlier, ``a + b·√v``, rounded half-up to two places.

    ``above`` it for ``b`` above zero (a hub), ``below`` it otherwise (isolated).

    A ``Ct`` is an integer, so a rounding that lands on an integer the exact bound lies short of would
    put a component on the wrong side of the printed figure: a hub at ``Ct 5`` beside *above 5.00*.
    There the bound is the integer itself, *5 or more* (*5 or less*), which holds exactly.
    """
    k = round_hundredths(a, b, v)
    whole = Fraction(k, 100)
    if k % 100 == 0:
        if b > 0 and not _at_most(whole - a, Fraction(b), v):
            return f"{k // 100} or more"
        if b < 0 and not _at_most(a - whole, Fraction(-b), v):
            return f"{k // 100} or less"
    return f"{'above' if b > 0 else 'below'} {format_hundredths(k)}"


def format_hundredths(k: int) -> str:
    """Hundredths as a figure with two places: ``148`` is ``1.48``, ``-34`` is ``-0.34``."""
    sign = "-" if k < 0 else ""
    return f"{sign}{abs(k) // 100}.{abs(k) % 100:02d}"


def format_exact(value: Fraction) -> str:
    """A rational figure as printed: two places, half-up."""
    return format_hundredths(round_hundredths(value))


def _signed_hundredths(k: int) -> str:
    text = format_hundredths(abs(k))
    return text if k == 0 else f"+{text}" if k > 0 else f"{_MINUS}{text}"


def format_change(change: int | Fraction) -> str:
    """A signed change: ``+2``, ``−2``, ``0``; a ratio's to two places, ``+0.34``."""
    if isinstance(change, Fraction):
        return _signed_hundredths(round_hundredths(change))
    return str(change) if change == 0 else f"+{change}" if change > 0 else f"{_MINUS}{-change}"


def shown_difference(before: int | Fraction, after: int | Fraction) -> int:
    """The change from *before* to *after* in hundredths, as the two figures are shown.

    The difference of the two rounded figures, so ``0.83 → 0.86`` is ``3`` and never the exact change's own
    rounding.
    """
    return round_hundredths(after) - round_hundredths(before)


def format_shown_change(before: int | Fraction, after: int | Fraction) -> str:
    """The change from *before* to *after* as the two are shown.

    A ratio's is :func:`shown_difference`, signed, so ``0.83 → 0.86`` reads ``+0.03``; an integer's is the
    plain difference.
    """
    if isinstance(before, Fraction) or isinstance(after, Fraction):
        return _signed_hundredths(shown_difference(before, after))
    return format_change(after - before)


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------


def count_of(number: int, singular: str, plural: str | None = None) -> str:
    """``1 component``, ``2 components``, ``0 components``: a count and its noun, in agreement."""
    return f"{number} {singular if number == 1 else plural or singular + 's'}"


def _verb(singular: bool, one: str, many: str) -> str:
    return one if singular else many


def _ratio(part: int, whole: int, singular: str, plural: str | None = None) -> tuple[str, bool]:
    """A share as a sentence opens with it, and whether its verb is singular.

    ``One component in 8`` when it reduces to one in ``k``, ``3 of 42 components`` otherwise, ``All 6
    components`` for every one, ``Both components`` for two of two, ``The only component`` for the one
    of one and ``No component`` for none.
    """
    plural = plural or singular + "s"
    if part == 0:
        return f"No {singular}", True
    if part == whole:
        if whole == 1:
            return f"The only {singular}", True
        return (f"Both {plural}", False) if whole == 2 else (f"All {whole} {plural}", False)
    if whole % part == 0:
        return f"One {singular} in {whole // part}", True
    return f"{part} of {whole} {plural}", False


def _lower(text: str) -> str:
    """A sentence's opening, set inside a sentence."""
    return text[:1].lower() + text[1:]


def name_list(names: Sequence[str], cap: int = NAMES_IN_A_SENTENCE, *, separator: str = ", ", more: int = 0) -> str:
    """The first *cap* of *names*, then how many more, *more* already left out of *names* included.

    Every list of the report counts its rest one way, the separator repeated before it: ``A, B, C, and
    2 more``, and ``A (no port); B (incomplete); and 2 more`` where entries are set apart by semicolons.
    """
    hidden = max(len(names) - cap, 0) + more
    shown = separator.join(names[:cap])
    return f"{shown}{separator}and {hidden} more" if hidden else shown


def _names(names: Sequence[str]) -> str:
    """The first three names, then how many more: ``A, B, C, and 2 more``."""
    return name_list(names)


def _joined(names: Sequence[str]) -> str:
    """Names a sentence's subject joins, the way *the most* names a tie.

    ``A``, ``A and B``, ``A, B and C``, ``A, B, C, and 2 more``.
    """
    if len(names) <= 1 or len(names) > NAMES_IN_A_SENTENCE:
        return _names(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _side(label: str) -> str:
    """A revision's label inside a sentence; the working tree takes its article."""
    return f"the {label}" if label == WORKING_TREE else label


def _flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# The conclusions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionConclusion:
    """What one section's figures say: *sentences*, the first being the headline, and its *limit*."""

    sentences: tuple[str, ...]
    limit: str | None = None

    @property
    def headline(self) -> str:
        return self.sentences[0]

    @property
    def text(self) -> str:
        """The sentences as one paragraph."""
        return " ".join(self.sentences)


@dataclass(frozen=True)
class Conclusions:
    """The two answers, each section's conclusion in :data:`SECTION_KEYS`' order, and each table's sentence.

    *state_sentences* and *heading_sentences* are the two answers' sentences, at most
    :data:`STATE_ANSWER_SENTENCES` and :data:`HEADING_ANSWER_SENTENCES`. *sections* holds ``movement``
    only when a base was asked for. *tables* holds ``(composition, sentence)`` in the tables' order; it
    is not on the MCP answer, which carries no table.
    """

    state_sentences: tuple[str, ...]
    heading_sentences: tuple[str, ...]
    sections: tuple[tuple[str, SectionConclusion], ...]
    tables: tuple[tuple[str, str], ...]

    @property
    def state_answer(self) -> str:
        return " ".join(self.state_sentences)

    @property
    def heading_answer(self) -> str:
        return " ".join(self.heading_sentences)

    def section(self, key: str) -> SectionConclusion | None:
        return dict(self.sections).get(key)


def _size(state: ArchitectureState) -> SectionConclusion:
    size = state.size
    if not size.components:
        sentences = ["The model declares no component."]
        if size.external_stubs:
            stubs = count_of(size.external_stubs, "external stub")
            # No claim of what a stub holds: a stub's file can declare a runnable, which Size counts.
            sentences.append(f"It declares {stubs} by reference.")
        return SectionConclusion(tuple(sentences))
    components = count_of(size.components, "component")
    if size.compositions:
        compositions = count_of(size.compositions, "composition")
        # "and", never "in": the two counts say nothing of which components are placed in a composition.
        headline = f"The model declares {components} and {compositions}, nested {size.nesting_depth} deep."
    else:
        headline = f"The model declares {components} and no composition."
    sentences = [headline]
    if size.external_stubs == 1:
        sentences.append("1 more is an external stub, declared by reference.")
    elif size.external_stubs:
        sentences.append(f"{size.external_stubs} more are external stubs, declared by reference.")
    return SectionConclusion(tuple(sentences))


_ONE_TYPE_ONCE = "Each type counts once, however often it is placed."


def _coupling(state: ArchitectureState) -> SectionConclusion:
    coupling = state.coupling
    if isinstance(coupling, NotAvailable):
        return SectionConclusion(("No component is declared, so dependencies are unanswered.",), _ONE_TYPE_ONCE)
    rows = state.components
    highest = max(row.ct for row in rows)
    sentences: list[str] = []
    at_highest = [row.name for row in rows if row.ct == highest]
    if isinstance(coupling.hubs, NotAvailable):
        sentences.append(
            f"With {count_of(coupling.n, 'component')} no Ct can lie more than two deviations from the mean, so "
            f"which components are hubs is unanswered; the highest Ct is {highest}, in {_joined(at_highest)}."
        )
    elif coupling.hubs:
        share, one = _ratio(len(coupling.hubs), coupling.n, "component")
        verb = _verb(one, "is a hub", "are hubs")
        sentences.append(
            f"{share} {verb}, with a Ct more than two deviations above this model's mean: {_names(coupling.hubs)}."
        )
    else:
        sentences.append(
            f"No component is a hub: the highest Ct is {highest}, in {_joined(at_highest)}, "
            f"against a mean of {format_exact(coupling.mean)}."
        )
    if isinstance(coupling.isolated, tuple) and coupling.isolated:
        # Isolated is decided on its own terms, so it is said beside the hubs, never folded into them.
        share, one = _ratio(len(coupling.isolated), coupling.n, "component")
        verb = _verb(one, "is statistically isolated", "are statistically isolated")
        sentences.append(
            f"{share} {verb}, with a Ct more than two deviations below this model's mean: {_names(coupling.isolated)}."
        )
    unlinked = [row.name for row in rows if row.ct == 0]
    if unlinked:
        share, one = _ratio(len(unlinked), coupling.n, "component")
        fact = _verb(one, "depends on nobody and nobody depends on it", "depend on nobody and nobody depends on them")
        sentences.append(f"{share} {fact}: {_names(unlinked)}.")
    limit = _ONE_TYPE_ONCE
    if coupling.not_measured_by_reason:
        limit = f"{_not_measured(coupling.not_measured_by_reason)} {limit}"
    return SectionConclusion(tuple(sentences), limit)


#: Each reason a connector, or a pair of ends, gives no measured dependency, as its count reads it.
_NOT_MEASURED_WORDS = {
    UNREACHED_END: ("connector", "connectors", "with an end that reaches no component"),
    ONE_DIRECTION: ("pair of ends", "pairs of ends", "of one direction"),
    REPEATED_NAME: ("connector", "connectors", "repeating a name declared before"),
}

#: Each reason a connector is placed in no composition table, as its count reads it.
_NOT_PLACED_WORDS = {
    UNRESOLVED_OWNER: "whose composition does not resolve",
    END_NOT_A_MEMBER: "with an end that is no member of its composition",
    UNRESOLVED_PORT: "with a port that does not resolve",
    ONE_DIRECTION: "joining two ends of one direction",
}


def _not_measured(by_reason: Sequence[tuple[str, int]]) -> str:
    """The connectors, and pairs of ends, that give no measured dependency, each reason with its count."""
    parts = []
    for reason, number in by_reason:
        singular, plural, words = _NOT_MEASURED_WORDS[reason]
        parts.append(f"{count_of(number, singular, plural)} {words}")
    return f"Giving no measured dependency: {', '.join(parts)}."


def _not_placed(by_reason: Sequence[tuple[str, int]]) -> str:
    """The connectors placed in no composition table, each reason with its count."""
    parts = [f"{count_of(number, 'connector')} {_NOT_PLACED_WORDS[reason]}" for reason, number in by_reason]
    return f"Placed in no table: {', '.join(parts)}."


def _table_sentence(table: CompositionTable) -> str:
    """One composition table's own sentence. It counts the table's rows, and a type placed twice is one row."""
    if isinstance(table.rows, NotAvailable):
        reason = table.rows.reason
        return f"{reason[:1].upper()}{reason[1:]}."
    rows = len(table.rows)
    if not table.links:
        return "1 row, no link." if rows == 1 else f"{rows} rows, no link among them."
    highest = max(row.ct for row in table.rows)
    most = sorted(row.name for row in table.rows if row.ct == highest)
    sentence = f"{count_of(rows, 'row')}, {count_of(table.links, 'link')}; "
    sentence += f"the most linked {_verb(len(most) == 1, 'is', 'are')} {_joined(most)} (Ct {highest})"
    unlinked = sum(1 for row in table.rows if row.ct == 0)
    if unlinked:
        sentence += f"; {unlinked} {_verb(unlinked == 1, 'has', 'have')} no link here"
    return sentence + "."


def _tables(state: ArchitectureState) -> SectionConclusion:
    tables = state.composition_tables
    limit = _not_placed(state.links_not_placed_by_reason) if state.links_not_placed_by_reason else None
    if not tables:
        return SectionConclusion(
            ("The model declares no composition, so coupling inside a composition is unanswered.",), limit
        )
    linked = [table for table in tables if table.links and not isinstance(table.rows, NotAvailable)]
    if not linked:
        return SectionConclusion(("No composition has a link among its members.",), limit)
    highest = max(table.links for table in linked)
    most = sorted(table.composition for table in linked if table.links == highest)
    one = len(most) == 1
    has = _verb(one, "has the most links among its", "have the most links among their")
    each = "" if one else " each"
    return SectionConclusion(
        (f"{count_of(len(tables), 'composition')}; {_joined(most)} {has} direct members: {highest}{each}.",),
        limit,
    )


_UNCALIBRATED = "0.5, a starting threshold not calibrated"


def _cohesion(state: ArchitectureState) -> SectionConclusion:
    cohesion = state.cohesion
    if isinstance(cohesion, NotAvailable):
        if cohesion.reason == NO_COHESION_FIGURE:
            sentence = (
                "Some runnables declare an access, but no component's runnables give a figure, so whether "
                "components hold together is unanswered; a figure needs a component whose only runnable "
                "declares an access, or two runnables declaring accesses over more than one port or variable."
            )
        else:
            sentence = (
                "No component's runnables declare an access, so whether components hold together is "
                "unanswered; reads, writes, calls, or variable reads and writes on runnables would answer it."
            )
        return SectionConclusion((sentence,))
    mean = f"Mean cohesion is {format_exact(cohesion.mean)} over {count_of(cohesion.components, 'component')}"
    if cohesion.below:
        below = len(cohesion.below)
        names = _names([name for name, _ in cohesion.below])
        sentence = f"{mean}; {below} {_verb(below == 1, 'falls', 'fall')} below {_UNCALIBRATED}: {names}."
    else:
        sentence = f"{mean}; none falls below {_UNCALIBRATED}."
    used = count_of(cohesion.runnables_used, "runnable")
    measured = count_of(cohesion.components, "component")
    limit = (
        f"Rests on the {used} that {_verb(cohesion.runnables_used == 1, 'uses', 'use')} a port or a variable in the "
        f"{measured} with a figure, "
        f"of {count_of(cohesion.runnables, 'runnable')} in the declared components"
    )
    without = sum(count for _, count in cohesion.not_available)
    if without:
        limit += f"; {count_of(without, 'component')} {_verb(without == 1, 'has', 'have')} no figure"
    return SectionConclusion((sentence,), limit + ".")


def _asil(asil: AsilCoverage | NotAvailable) -> str:
    if isinstance(asil, NotAvailable):
        return (
            "No declared component or requirement states a safety level, so which components are "
            "safety-relevant is unanswered."
        )
    share, one = _ratio(asil.declared, asil.components, "component")
    sentence = f"{share} {_verb(one, 'declares', 'declare')} a safety level"
    if asil.inherited:
        inherit = _verb(asil.inherited == 1, "inherits one above QM from its", "inherit one above QM from their")
        sentence += f" and {asil.inherited} {inherit} requirements"
    if asil.neither:
        none = len(asil.neither)
        neither = _verb(none == 1, "states none and inherits", "state none and inherit")
        sentence += f"; {none} {neither} none above QM: {_names(asil.neither)}"
    return sentence + "."


def _runnable(runnable: RunnableCoverage | NotAvailable) -> str:
    if isinstance(runnable, NotAvailable):
        return "No declared component has a runnable, so what the components run is unanswered."
    share, one = _ratio(runnable.with_runnable, runnable.components, "component")
    sentence = f"{share} {_verb(one, 'declares', 'declare')} a runnable"
    if runnable.without:
        sentence += f"; without one: {_names(runnable.without)}"
    return sentence + "."


def _task(task: TaskCoverage | NotAvailable) -> str:
    if isinstance(task, NotAvailable):
        if task.reason == NO_TASK:
            return "The model has no task, so whether runnables are scheduled is unanswered."
        return "No declared component has a runnable, so task coverage is unanswered."
    if not task.runnables:
        return (
            "Every runnable of the declared components is a server runnable, run in its callers' tasks, so "
            "none needs a task of its own."
        )
    # The share counts what the figure counts: beside a server runnable, the runnables that are not one.
    kind = "non-server runnable" if task.server else "runnable"
    share, one = _ratio(task.on_task, task.runnables, kind)
    sentence = f"In the declared components, {_lower(share)} {_verb(one, 'is', 'are')} on a task"
    if task.not_on_task:
        sentence += f"; on none: {_names(task.not_on_task)}"
    return sentence + "."


def _requirement(requirement: RequirementCoverage | NotAvailable) -> str:
    if isinstance(requirement, NotAvailable):
        return "The model declares no requirement, so what the components are traced to is unanswered."
    if requirement.requirements:
        share, one = _ratio(requirement.allocated, requirement.requirements, "requirement")
        allocated = f"{share} {_verb(one, 'is', 'are')} allocated"
    else:
        # The stub classification is the id's: a placeholder excludes every file declaring its id.
        allocated = "Every requirement id is held by a stub requirement, a placeholder, so none counts as allocated"
    share, one = _ratio(requirement.linked, requirement.components, "component")
    sentence = f"{allocated}, and {_lower(share)} {_verb(one, 'has', 'have')} a requirement link"
    if requirement.unallocated:
        sentence += f"; not allocated: {_names(requirement.unallocated)}"
    if requirement.unlinked:
        sentence += f"; no link to a declared requirement: {_names(requirement.unlinked)}"
    return sentence + "."


def _coverage(coverage: Coverage) -> SectionConclusion:
    """One sentence per coverage, in their order; the headline is the first that is a number."""
    answers = (coverage.asil, coverage.runnable, coverage.task, coverage.requirement)
    sentences = [_asil(coverage.asil), _runnable(coverage.runnable), _task(coverage.task)]
    sentences.append(_requirement(coverage.requirement))
    first = next((index for index, answer in enumerate(answers) if not isinstance(answer, NotAvailable)), 0)
    return SectionConclusion((sentences[first], *sentences[:first], *sentences[first + 1 :]))


def _most_in(integrity: Integrity) -> str:
    """Where the findings come from, by rule family.

    ``all from`` the one family, ``most from`` a family holding more than half of them, otherwise ``the
    largest share from`` every family with the most, by name, with its count of the whole: ``the largest
    share from hardware rules (3 of 10)``.
    """
    counts: Counter[str] = Counter()
    for family, count in (*integrity.errors_by_family, *integrity.warnings_by_family):
        counts[family] += count
    highest, whole = max(counts.values()), sum(counts.values())
    families = sorted(name for name, count in counts.items() if count == highest)
    if len(counts) == 1:
        return f"all from {families[0]} rules"
    if 2 * highest > whole:
        return f"most from {families[0]} rules"
    if len(families) == 1:
        return f"the largest share from {families[0]} rules ({highest} of {whole})"
    return f"the largest shares from {_joined(families)} rules ({highest} of {whole} each)"


_INTEGRITY_LIMIT = (
    "Ports of external stubs and of composition boundaries are left out, and a port delegated to an "
    "unwired boundary port counts as connected."
)


def _integrity(integrity: Integrity) -> SectionConclusion:
    errors, warnings = integrity.errors, integrity.warnings
    warned = count_of(warnings, "warning") if warnings else "no warning"
    if errors:
        sentence = (
            f"check-model reports {count_of(errors, 'error')} and {warned}, {_most_in(integrity)}: "
            "the model does not pass as it stands."
        )
    elif warnings:
        sentence = f"check-model reports no error and {warned}, {_most_in(integrity)}."
    else:
        sentence = "check-model reports no error and no warning."
    sentences = [sentence]
    if integrity.unconnected:
        share, one = _ratio(len(integrity.unconnected), integrity.ports, "component port")
        sentences.append(f"{share} {_verb(one, 'is', 'are')} unconnected: {_names(integrity.unconnected)}.")
    return SectionConclusion(tuple(sentences), _INTEGRITY_LIMIT)


#: Said after *most in* when a component added or removed moved too: it is named by the components
#: sentence, not here.
_AT_BOTH_SIDES = " among the components present at both revisions"


def _largest(moves: Sequence[CtChange]) -> tuple[int, tuple[CtChange, ...], str]:
    """The largest move among *moves* of components declared on both sides, and who made it.

    The largest move, every component with it by name, and :data:`_AT_BOTH_SIDES` when an added or removed
    component moved too. ``(0, (), "")`` when no component on both sides moved.
    """
    kept = [change for change in moves if change.before is not None and change.after is not None]
    if not kept:
        return 0, (), ""
    largest = max(abs(change.change) for change in kept)
    top = tuple(sorted((change for change in kept if abs(change.change) == largest), key=lambda change: change.name))
    return largest, top, _AT_BOTH_SIDES if len(kept) < len(moves) else ""


def _where(movement: Movement, rise: bool) -> str:
    """Where a rise, or a fall, of the dependencies shows in ``Ct``.

    Named only among the components declared on both sides, as *what to look at*'s rises are: ``mostly in``
    one that holds more than half of every move that way, added and removed components included, ``most in``
    the largest otherwise, every one tied with it named; empty when no component on both sides moved that way.
    """
    moves = [change for change in movement.ct_changes if change.change and (change.change > 0) == rise]
    largest, top, among = _largest(moves)
    if not top:
        return ""
    whole = sum(abs(change.change) for change in moves)
    signed = format_change(largest if rise else -largest)
    if len(top) == 1 and 2 * largest > whole:
        return f", mostly in {top[0].name} (Ct {signed}){among}"
    names = [change.name for change in top]
    return f", most in {_joined(names)} (Ct {signed}{'' if len(top) == 1 else ' each'}){among}"


def _figure_of(movement: Movement, name: str) -> MovementFigure:
    return next(figure for figure in movement.figures if figure.figure == name)


def _mean_against(movement: Movement, rise: bool) -> str:
    """The mean ``Ct`` per component, when it moved, as shown, the other way from the dependencies."""
    mean = _figure_of(movement, _MEAN_CT)
    before, after = mean.values[0], mean.values[-1]
    if mean.change is None or not isinstance(before, Fraction) or not isinstance(after, Fraction):
        return ""
    shown_before, shown_after = round_hundredths(before), round_hundredths(after)
    if shown_before == shown_after or (shown_after > shown_before) == rise:
        return ""
    verb = "rose" if shown_after > shown_before else "fell"
    shown = f"from {format_hundredths(shown_before)} to {format_hundredths(shown_after)}"
    return f"; the mean Ct per component {verb} {shown}"


def _coupling_movement(movement: Movement, short: bool = False) -> str | None:
    """The dependencies' change.

    *short* is the overview's form, which says the change and not where it shows, so it names no ``Ct``.
    """
    dependencies = _figure_of(movement, _DEPENDENCIES)
    before, after = dependencies.values[0], dependencies.values[-1]
    base = movement.base
    if dependencies.change is None:
        if len(dependencies.not_available_at) == 2:
            return (
                f"Neither {base} nor {_side(movement.current)} declares a component, so dependencies cannot be "
                "compared."
            )
        return None
    assert isinstance(before, int) and isinstance(after, int)
    if before == after:
        if all(value == before for value in dependencies.values):
            held = f"Dependencies between components held at {before} since {base}"
        else:
            # Equal at both ends and not between: a revision in the series holds another total.
            held = f"Dependencies between components are back at {before}, as at {base}"
        if not movement.ct_changes:
            return held + "."
        moved = len(movement.ct_changes)
        if short:
            # Only a component present at both revisions can be said to have changed its dependencies: an
            # added or removed one, a stub promoted to a component included, is the added sentence's.
            kept = sum(1 for change in movement.ct_changes if change.before is not None and change.after is not None)
            if not kept:
                return held + "."
            present = "present at both revisions changed"
            return f"{held}, though the dependencies, in and out, of {count_of(kept, 'component')} {present}."
        whose = _verb(moved == 1, "component's", "components'")
        _, top, among = _largest(movement.ct_changes)
        most = ""
        if top:
            signs = {format_change(change.change) for change in top}
            if len(signs) == 1:
                each = "" if len(top) == 1 else " each"
                most = f", most in {_joined([change.name for change in top])} (Ct {signs.pop()}{each})"
            else:
                most = ", most in " + _joined([f"{change.name} (Ct {format_change(change.change)})" for change in top])
            most += among
        return f"{held}, though {moved} {whose} Ct changed{most}."
    rise = after > before
    where, mean = ("", "") if short else (_where(movement, rise), _mean_against(movement, rise))
    if before == 0:
        return f"Dependencies between components grew from 0 to {after} since {base}{where}{mean}."
    percent = math.floor(Fraction(100 * abs(after - before), before) + Fraction(1, 2))
    by = f"{percent} %" if percent else "under 1 %"
    verb = "grew" if rise else "fell"
    # "from a to b", not "(a → b)": a revision's label carries its short id in parentheses already.
    return f"Dependencies between components {verb} by {by} since {base}, from {before} to {after}{where}{mean}."


def _components_moved(added: Sequence[str], removed: Sequence[str]) -> str:
    if added and removed:
        return f"{count_of(len(added), 'component')} added: {_names(added)}; {len(removed)} removed: {_names(removed)}."
    if added:
        return f"{count_of(len(added), 'component')} added: {_names(added)}."
    return f"{count_of(len(removed), 'component')} removed: {_names(removed)}."


def _findings_moved(movement: Movement) -> str | None:
    introduced, resolved = len(movement.introduced), len(movement.resolved)
    total, still_open = movement.base_findings, movement.still_open
    if not total:
        return f"{count_of(introduced, 'finding')} introduced; the base had none." if introduced else None
    if introduced or resolved:
        moved = f"{count_of(introduced, 'finding')} introduced and {resolved} resolved since {movement.base}"
    else:
        moved = f"No finding introduced or resolved since {movement.base}"
    if still_open == total:
        held = (
            "the base's one finding is still open"
            if total == 1
            else f"all {total} of the base's findings are still open"
        )
    elif still_open == 0:
        held = "the base's one finding is resolved" if total == 1 else f"none of the base's {total} is still open"
    else:
        held = f"{still_open} of the base's {total} {_verb(still_open == 1, 'is', 'are')} still open"
    return f"{moved}; {held}."


def _movement_sentences(movement: Movement, short: bool = False) -> tuple[str, ...]:
    """The movement's sentences; *short* is the overview's form.

    The dependencies' change, the components added and removed, the findings and, unless *short*, each
    figure that cannot be compared.

    Never empty once something differs: with a declared component at both ends the dependencies' sentence
    stands, with one at neither end it says so, and with one at one end only a component was added or
    removed.
    """
    if movement.unchanged:
        return (f"No figure this report follows changed since {movement.base}.",)
    sentences = [
        sentence
        for sentence in (
            _coupling_movement(movement, short),
            _components_moved(movement.added, movement.removed) if movement.added or movement.removed else None,
            _findings_moved(movement),
        )
        if sentence is not None
    ]
    if not short:
        for figure in movement.figures:
            if figure.change is None and len(figure.not_available_at) == 1:
                side = _side(figure.not_available_at[0])
                sentences.append(f"{figure.figure} cannot be compared: it is n/a at {side}.")
    assert sentences, "a movement that differs always has a sentence"
    return tuple(sentences)


def _movement(report: MetricsReport) -> SectionConclusion | None:
    movement = report.movement
    if movement is None:
        if report.no_base is None:
            return None
        return SectionConclusion((f"Nothing to compare: {_flat(report.no_base).rstrip('.')}.",))
    return SectionConclusion(_movement_sentences(movement))


_LOOK_AT_LIMIT = "Each line is a place to look, not a defect."


def _look_at(lines: Sequence[LookAtLine]) -> SectionConclusion:
    if not lines:
        return SectionConclusion(("No list below has content.",), _LOOK_AT_LIMIT)
    first = _lower(LOOK_AT_LABELS[lines[0].key])
    if len(lines) == 1:
        return SectionConclusion((f"1 list below has content: {first}.",), _LOOK_AT_LIMIT)
    return SectionConclusion((f"{len(lines)} lists below have content; the first is {first}.",), _LOOK_AT_LIMIT)


def _hubs_in_short(state: ArchitectureState) -> str | None:
    """The coupling headline in plain words: which components stand out by their links."""
    coupling = state.coupling
    if isinstance(coupling, NotAvailable):
        return None
    if isinstance(coupling.hubs, NotAvailable):
        # The method cannot answer here; it does not show that none stands out.
        return f"With {count_of(coupling.n, 'component')}, which of them stand out by their links is not measured."
    if not coupling.hubs:
        return "No component has far more dependencies, in and out, than the rest."
    share, one = _ratio(len(coupling.hubs), coupling.n, "component")
    verb = _verb(one, "has", "have")
    return f"{share} {verb} far more dependencies, in and out, than the rest: {_names(coupling.hubs)}."


def _cohesion_in_short(state: ArchitectureState) -> str | None:
    """The cohesion headline in plain words: in how many components few runnables use a port or a variable together.

    The components that give a figure include one whose only runnable gives 1 with no pair, so the
    sentence with nothing below half says *no fewer than half*, which holds of no pair too.
    """
    cohesion = state.cohesion
    if isinstance(cohesion, NotAvailable):
        return None
    whole = cohesion.components
    measured = "the one component that gives a figure" if whole == 1 else f"the {whole} components that give a figure"
    if cohesion.below:
        below = len(cohesion.below)
        where = measured if whole == 1 else f"{below} of {measured}"
        if below == whole and whole > 1:
            where = f"all {measured}"
        found = "fewer than half the pairs of runnables that use a port or a variable have one in common"
    else:
        where = measured if whole == 1 else f"each of {measured}"
        found = "no fewer than half the pairs of runnables that use a port or a variable have one in common"
    return f"In {where}, {found}; half is a starting threshold, not calibrated."


def _state_answer(state: ArchitectureState, by_key: dict[str, SectionConclusion]) -> tuple[str, ...]:
    """What state the architecture is in, in at most :data:`STATE_ANSWER_SENTENCES` sentences."""
    sentences = [by_key["size"].headline, by_key["integrity"].headline]
    for sentence in (_hubs_in_short(state), _cohesion_in_short(state)):
        if sentence is not None:
            sentences.append(sentence)
    return tuple(sentences)


def _heading_answer(report: MetricsReport, movement: SectionConclusion | None) -> tuple[str, ...]:
    """Where the architecture is heading, in at most :data:`HEADING_ANSWER_SENTENCES` sentences.

    The dependencies' change, the components added and removed, and the findings.
    """
    if movement is None:
        return (NO_BASE_NAMED,)
    if report.movement is None:
        return movement.sentences
    return _movement_sentences(report.movement, short=True)


def conclude(report: MetricsReport) -> Conclusions:
    """The conclusions of *report*: pure, the same report giving the same strings."""
    current = report.series[-1]
    sections: list[tuple[str, SectionConclusion]] = [
        ("size", _size(current)),
        ("coupling", _coupling(current)),
        ("tables", _tables(current)),
        ("cohesion", _cohesion(current)),
        ("coverage", _coverage(current.coverage)),
        ("integrity", _integrity(current.integrity)),
    ]
    movement = _movement(report)
    if movement is not None:
        sections.append((MOVEMENT, movement))
    sections.append(("look_at", _look_at(report.look_at)))
    by_key = dict(sections)
    state_answer = _state_answer(current, by_key)
    heading_answer = _heading_answer(report, movement)
    tables = tuple((table.composition, _table_sentence(table)) for table in current.composition_tables)
    return Conclusions(state_answer, heading_answer, tuple(sections), tables)
