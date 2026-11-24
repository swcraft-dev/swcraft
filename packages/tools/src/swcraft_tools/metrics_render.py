"""The architecture metrics' documents, rendered from a :class:`~swcraft_tools.metrics.MetricsReport`.

:func:`render_text` is what ``swcraft metrics`` prints, :func:`render_markdown` the file ``--report``
writes and :func:`render_json` the ``--format json`` document. The text and the Markdown are two
spellings of one list of sections, built once by :func:`_sections`: *Overview* first, the two answers,
then *Definitions and method*, then *State* with size, coupling, the composition tables, cohesion,
coverage and integrity in that order, then *Movement* when a base was asked for, then *What to look at*.
The definitions are fixed text, :data:`DEFINITIONS`; each section names the terms it uses
(:data:`SECTION_TERMS`) instead of defining them again. Each section carries, in this order, that
pointer, its figures, its conclusion for this model and its limit; the sentences are
:func:`~swcraft_tools.metrics_conclusions.conclude`'s, so no format states a meaning of its own. A
section whose figure is ``n/a`` still stands, with its reason, so the page has one shape on a full model
and on a fresh import.

**Exact to the last printed digit.** A figure is a ``Fraction`` until it is printed, rounded half-up to
two places; a root expression ``a + b·√v`` is rounded whole by exact comparison, never the root first.
The JSON carries the same rounded figures as strings, so its bytes do not depend on a float's tail. No
timestamp and no location: the same model gives the same bytes.

**Safely Markdown.** Every string of a section reaches the Markdown through
:func:`~swcraft_tools.markdown_cells.cell_text`, model names and fixed words alike, so a ``|`` ends no
cell and the document holds no raw HTML; the text keeps every string on its line.

**What it does not reach.** It renders figures, names and the conclusions it is given; it judges
nothing. Markdown's inline markers in a model name are left as they are, so an ``*`` in a composition's
name renders as a marker would in its caption.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from swcraft_tools.markdown_cells import cell_text, table
from swcraft_tools.metrics import (
    COHESION_THRESHOLD,
    LOOK_ERRORS,
    LOOK_INTRODUCED,
    LOOK_LOW_COHESION,
    LOOK_NO_LINK,
    LOOK_ROSE,
    LOWER_THRESHOLD_BELOW_ZERO,
    NO_ACCESS,
    NO_RUNNABLE_OF_ITS_OWN,
    ONE_ACCESSING_RUNNABLE,
    ONE_PORT_OR_VARIABLE,
    ArchitectureState,
    AsilCoverage,
    Cohesion,
    ComponentRow,
    CompositionTable,
    Coupling,
    Coverage,
    Integrity,
    LookAtEntry,
    LookAtLine,
    MemberRow,
    MetricsReport,
    Movement,
    MovementFigure,
    NotAvailable,
    RequirementCoverage,
    RunnableCoverage,
    Size,
    TaskCoverage,
    top_components,
)
from swcraft_tools.metrics_conclusions import (
    LOOK_AT_LABELS,
    Conclusions,
    conclude,
    count_of,
    format_exact,
    format_hundredths,
    format_shown_change,
    name_list,
    outlier_bound,
    round_hundredths,
    shown_difference,
)
from swcraft_tools.model_registry import current_registry

if TYPE_CHECKING:
    from swcraft_tools.validator import ConsistencyIssue

__all__ = [
    "ANSWER_LIST_CAP",
    "DEFINITIONS",
    "DEFINITIONS_TITLE",
    "DOCUMENT_VERSION",
    "MARKDOWN_FILE",
    "OVERVIEW_TITLE",
    "REPORT_DIRECTORY",
    "SECTION_TERMS",
    "bounded_document",
    "json_document",
    "render_json",
    "render_markdown",
    "render_text",
]

#: The directory below ``dist_root`` the report is written in, and its one file.
REPORT_DIRECTORY = "metrics"
MARKDOWN_FILE = "metrics.md"

#: ``--format json``'s major version; a consumer rejects one it does not know.
DOCUMENT_VERSION = 1

_TITLE = "Architecture metrics"
_GATE = "No overall score; `swcraft validate` is the gate."
_NOT_AVAILABLE = "n/a"
_NO_COMPOSITION = "the model declares no composition"
_DASH = "—"
_ARROW = "→"
#: How many names a figure's cell names before it counts the rest.
_CAP = 10
#: How many items each list of the tool's bounded answer carries before it counts the rest.
ANSWER_LIST_CAP = 10

#: The two questions the page opens with, each followed by its answer.
_STATE_QUESTION = "What state is the architecture in?"
_HEADING_QUESTION = "Where is it heading?"

#: The heading of the page's first section, the two answers, and of the definitions after it.
OVERVIEW_TITLE = "Overview"
DEFINITIONS_TITLE = "Definitions and method"
#: The anchor a Markdown viewer gives :data:`DEFINITIONS_TITLE`, which each section's pointer links to.
_DEFINITIONS_ANCHOR = "#definitions-and-method"

#: The page's first section: each term the figures use, what it is and how it is counted, in the
#: order a reader meets them. Fixed text, the same for every model, and no verdict.
DEFINITIONS: tuple[tuple[str, str], ...] = (
    (
        "Model elements",
        "a component type is one software component file; a placement is a member of a composition that "
        "puts a type in it, and a type can be placed many times. A port is a component's or a composition's "
        "point of connection, providing or requiring an interface, the contract the two ends share. A "
        "connector joins two ports inside a composition; a delegation joins a composition's boundary port "
        "to a port of one of its members. A runnable is a piece of a component's behaviour that runs. A "
        "requirement link is a requirement id an element names as one it satisfies: a component, one of its "
        "ports or runnables, an interface, a composition, a task or a hardware element.",
    ),
    (
        "Size",
        "the elements the model's files declare, one element per file, so a name declared twice counts "
        "twice here and once in the figures below, except a composition table, which reads every connector "
        "file: two connectors sharing a name and joining different pairs are two links there. A name two "
        "component files share is read below off one of them, its last declared file, or its last external "
        "stub when no declared file holds the name; the other files, their ports, runnables, safety levels "
        "and requirement links, are read by no figure below but the errors and warnings, which check-model "
        "reports over every file. Components are the declared component types, "
        "a stub sharing a declared component's name counted with them, external "
        "stubs counted apart; Compositions counts the composition types, and nested N deep is the longest "
        "chain of compositions placed in one another, a composition holding no other one being 1 deep and "
        "the compositions that hold one another through a cycle, which check-model reports, counting as "
        "many levels as they are. "
        "Ports, Connectors, Delegations and Runnables count those elements, the stubs' included; every "
        "other row counts the files of that kind, such as interfaces, data types, units, tasks and hardware.",
    ),
    (
        "Node",
        "a component type the model declares. A composition is a container, not a node: its connectors "
        "count as links between the components behind it, so wrapping components in a composition moves "
        "no coupling figure. A type placed twice is one node, and a connector between two placements of "
        "one type is dropped.",
    ),
    (
        "External stub",
        "a component declared only by reference, with the ports an export showed and whatever else its file "
        "declares, such as a runnable, which the size counts. Stubs are counted in the "
        "size and are rows of a composition table, and a link to a stub counts in its declared neighbour's "
        "Ca or Ce. A stub is left out of every mean, deviation, hub, isolated and cohesion figure and of every "
        "coverage over the declared components; a stub's link still allocates a declared requirement id no "
        "stub requirement holds. "
        "A link between two stubs gives no dependency and counts in no Ca or Ce of the coupling; its "
        "connector still counts in the size, and it is a link of the composition table both stubs are "
        "members of.",
    ),
    (
        "Dependency",
        "its direction runs from the component with the require port to the component with the provide "
        "port, read off the two ports' directions for every kind of interface: a receiver depends on its "
        "sender, a client "
        "on its server. A connector to a composition's boundary port is followed down every delegation to "
        "the components behind it, so one connector can give several dependencies; a connector whose two "
        "ends have one direction gives none. The Dependencies figure counts the distinct directed pairs "
        "with a declared component at one end at least.",
    ),
    (
        "Ca, Ce, Ct",
        "per declared component, Ca counts the distinct other components that depend on it, Ce the distinct "
        "components it depends on, stubs included, and Ct = Ca + Ce. Components are counted, not connectors: "
        "any number of connectors that make one component depend on another is one dependency. Two "
        "components that depend on each other count each other in both Ca and Ce.",
    ),
    (
        "Mean and deviation",
        "the mean Ct, μ = ΣCt / n, and its standard deviation, σ = √(Σ(Ct − μ)² / n), are taken over the "
        "declared components only, n of them: the deviation divides by n, not n − 1. Stubs are not in n.",
    ),
    (
        "Hub",
        "a declared component whose Ct lies more than two deviations above the mean, Ct > μ + 2σ, decided "
        "on exact figures. The threshold is this model's own; a hub is a place to look, not a defect.",
    ),
    (
        "Isolated",
        "a declared component whose Ct lies more than two deviations below the mean, Ct < μ − 2σ. When "
        "μ − 2σ is below zero no Ct can be under it, so the row is n/a, and the components with no link at "
        "all are named under What to look at instead.",
    ),
    (
        "Fewer than six components",
        "with n < 6, hubs and isolated are n/a: with so few values none can lie more than two deviations "
        "from their mean, so a count of 0 would say nothing about the model.",
    ),
    (
        "Mean Ct without outliers",
        "the trimmed mean, the mean Ct over the declared components that are neither hub nor isolated, "
        "shown with the highest Ct among them. A list that is n/a leaves no component out, so with n < 6 "
        "it is over all of them.",
    ),
    (
        "Composition table",
        "the links among one composition's direct members: its components, its stubs and each nested "
        "composition as one block; a type placed twice is one row, under its first member's name. A link "
        "is a distinct directed pair of members joined by a connector the composition owns, so several "
        "connectors between two members in one direction are one link, and Ct = Ca + Ce among the members. "
        "A connector between two placements of one type gives none; one whose ends have one direction, or "
        "whose composition, member or port does not resolve, gives none and is counted under the limit. No "
        "delegation is followed: a link that crosses a composition's border counts as that box's link at "
        "the level where it is drawn. No threshold applies; compare members inside one box.",
    ),
    (
        "Cohesion",
        "per declared component, C = s / (m(m − 1)/2), from 0 to 1: m is the number of its runnables that use "
        "a port or a variable, s the number of pairs of them that use a port or a variable in common. A "
        "runnable uses the ports its reads, writes and calls name, the port of an operation, of a data "
        "reception or of an asynchronous call's return that starts it, and the inter-runnable variables its "
        "reads_variables and writes_variables name; a port and a variable of one name are two things, never "
        "one in common. This is the measure as "
        "computed here, not a textbook one: a runnable that uses neither a port nor a variable is left out of "
        "m, never counted as sharing nothing. With m = 1 the figure is 1 when that runnable is the component's "
        "only runnable, and n/a otherwise; it is n/a too when the component has no runnable, when none uses a "
        "port or a variable, or when two or more use one and all use one and the same port or variable. Mean "
        "cohesion is over the components with a figure; 0.5 is a starting threshold, not calibrated.",
    ),
    (
        "Coverage",
        "each row has its own whole. ASIL, a safety level from QM (no safety level required) up to D: over "
        "the declared components, declared when the component states a level, QM included; inherited when "
        "it states none and the highest level of the requirements allocated to it is above QM, QM(D) "
        "counting as QM; neither otherwise. Components with a runnable: over the declared components. "
        "Runnables on a task: over the declared components' runnables that are not server runnables; a "
        "server runnable, one an operation starts and no timing event does, runs in its callers' tasks and "
        "is excluded. A task naming one placement's runnable covers that runnable of the type. Requirements "
        "allocated: over the requirement ids no stub requirement holds, a stub requirement being a placeholder "
        "an import created; an id a stub requirement holds is excluded with every file declaring it, and an "
        "id is allocated when some element satisfies it. Components with a requirement link: "
        "over the declared components, a link to a requirement the model declares, a stub requirement "
        "included, on the component, a port or a runnable; a link naming an id no file declares counts for "
        "none. A coverage is n/a only when the declared components and the requirements hold none of its "
        "kind of data: no level on any declared component or requirement, no runnable on a declared "
        "component, no task, no requirement; an external stub's level and runnables are not looked at.",
    ),
    (
        "n/a",
        "the figure is not given: the model lacks the data it needs, or the method does not apply, as with "
        "fewer than six components, a threshold below zero or a component whose runnables give no pair, save "
        "one whose only runnable uses a port or a variable, which gives 1. The "
        "reason follows, never 0.",
    ),
    (
        "Findings",
        "check-model's errors and warnings, counted by family: the word before the first dot of a rule's "
        "name. The metrics add no rule of their own.",
    ),
    (
        "Unconnected port",
        "a declared component's port that no connector and no delegation names.",
    ),
    (
        "Movement",
        "the figures of its table at the base, the first revision named, at any further revision and in the "
        "working tree, with each component's Ct and the findings. It does not follow the other size rows, "
        "such as interfaces, data types, tasks, delegations and the nesting depth, nor the composition "
        "tables, which components are isolated, the unconnected ports or the names behind a coverage count, "
        "so a change only there reads as no figure changed. The change is the working tree's figure minus "
        "the base's; a ratio's change is the difference of the two figures as shown, and a coverage row "
        "shows a count, not a share. A rename reads as a "
        "removal and an addition. A finding is the same at both revisions when its rule and message are, "
        "counted per occurrence, so a changed message reads as one resolved and one introduced. Where a "
        "change of the dependencies shows is named among the components present at both revisions; an "
        "added or removed component is named in the sentence on components.",
    ),
    (
        "What to look at",
        "the facts above that name a place, in a fixed order; the order is not a ranking.",
    ),
)

#: The terms of :data:`DEFINITIONS` each section uses, which its pointer names in place of a definition.
SECTION_TERMS: dict[str, tuple[str, ...]] = {
    "size": ("Model elements", "Size", "Node", "External stub"),
    "coupling": (
        "Dependency",
        "Ca, Ce, Ct",
        "Mean and deviation",
        "Hub",
        "Isolated",
        "Fewer than six components",
        "Mean Ct without outliers",
        "External stub",
    ),
    "tables": ("Composition table",),
    "cohesion": ("Cohesion",),
    "coverage": ("Coverage", "n/a"),
    "integrity": ("Findings", "Unconnected port"),
    "movement": ("Movement",),
    "look_at": ("What to look at",),
}


def _root(a: Fraction, b: int, v: Fraction) -> str:
    """``a + b·√v`` as printed: a deviation or a threshold."""
    return format_hundredths(round_hundredths(a, b, v))


def _percent(part: int, whole: int) -> str:
    return f"{format_exact(Fraction(100 * part, whole))} %"


def _figure(value: int | Fraction | NotAvailable) -> str:
    """A cell of a figure: an integer as it is, a ratio to two places, ``n/a`` where there is none."""
    if isinstance(value, NotAvailable):
        return _NOT_AVAILABLE
    if isinstance(value, Fraction):
        return format_exact(value)
    return str(value)


def _na(reason: str) -> str:
    return f"{_NOT_AVAILABLE} {_DASH} {reason}"


def _names(names: Sequence[str]) -> str:
    """A figure's cell: the first :data:`_CAP` names, then how many more, as every list counts its rest."""
    return name_list(names, _CAP)


# ---------------------------------------------------------------------------
# The sections: one list, two spellings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Paragraph:
    text: str


@dataclass(frozen=True)
class _Pairs:
    """A figure per line: its label and its value."""

    rows: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Table:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _Caption:
    """A composition's name heading its table, or its name and its ``n/a`` on one line."""

    name: str
    text: str | None = None


@dataclass(frozen=True)
class _Bullets:
    """Labelled lines: ``**label:** text``."""

    items: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Answer:
    """One of the two questions and its answer."""

    question: str
    answer: str


@dataclass(frozen=True)
class _Refer:
    """A section's pointer: the terms it uses, defined once in the page's first section."""

    terms: tuple[str, ...]


@dataclass(frozen=True)
class _Limit:
    """What a section's figures do not reach, beside them."""

    text: str


_Block = _Paragraph | _Pairs | _Table | _Caption | _Bullets | _Answer | _Refer | _Limit


@dataclass(frozen=True)
class _Section:
    """One part of the page.

    *key* is one of :data:`~swcraft_tools.metrics_conclusions.SECTION_KEYS`, ``definitions``, ``answers``, or
    ``state`` for the heading above six.
    """

    key: str
    title: str
    level: int
    blocks: tuple[_Block, ...]


def _size(size: Size) -> list[_Block]:
    registry = current_registry()
    shown = {"swc", "compositions", "connectors"}
    nesting = f", nested {size.nesting_depth} deep" if size.compositions else ""
    rows = [
        ("Components", f"{size.components} declared, {count_of(size.external_stubs, 'external stub')}"),
        ("Compositions", f"{size.compositions}{nesting}"),
        ("Ports", _ports(size)),
        ("Connectors", str(size.connectors)),
        ("Delegations", str(size.delegations)),
        ("Runnables", str(size.runnables)),
    ]
    rows += [(registry.layer(name).display_label, str(count)) for name, count in size.layers if name not in shown]
    return [_Pairs(tuple(rows))]


def _ports(size: Size) -> str:
    """The port count and where the ports are: on components, on stubs, on composition boundaries."""
    parts = [f"{size.component_ports} on components"]
    if size.stub_ports:
        parts.append(f"{size.stub_ports} on {'an external stub' if size.stub_ports == 1 else 'external stubs'}")
    if size.boundary_ports:
        parts.append(f"{size.boundary_ports} on composition {'boundaries' if size.boundary_ports != 1 else 'boundary'}")
    return f"{size.ports}: {', '.join(parts)}" if len(parts) > 1 else f"{size.ports}, all on components"


def _outliers(
    label: str, names: tuple[str, ...] | NotAvailable, threshold: str, n: int, side: str, unlinked: bool
) -> tuple[str, str]:
    if isinstance(names, NotAvailable):
        labelled = f"{label} (more than two deviations {side} the mean)"
        if names.reason == LOWER_THRESHOLD_BELOW_ZERO:
            rest = "components with no link at all are named below" if unlinked else "no component is without a link"
            return labelled, _na(f"that threshold is below zero; {rest}")
        return labelled, _na(names.reason)
    return f"{label} (Ct {threshold})", f"{len(names)} of {n} ({_percent(len(names), n)})"


def _coupling(coupling: Coupling | NotAvailable, components: Sequence[ComponentRow]) -> list[_Block]:
    if isinstance(coupling, NotAvailable):
        return [_Paragraph(_na(coupling.reason))]
    trimmed = f"{format_exact(coupling.trimmed_mean)}; highest {coupling.trimmed_max}"
    if coupling.trimmed_over_all:
        trimmed += ", over all components: hubs and isolated are n/a"
    unlinked = any(row.ct == 0 for row in components)
    hubs = outlier_bound(coupling.mean, 2, coupling.variance)
    isolated = outlier_bound(coupling.mean, -2, coupling.variance)
    rows = [
        ("Dependencies", str(coupling.dependencies)),
        (
            "Mean Ct, all components",
            f"{format_exact(coupling.mean)} (deviation {_root(Fraction(0), 1, coupling.variance)})",
        ),
        _outliers("Hubs", coupling.hubs, hubs, coupling.n, "above", unlinked),
        _outliers("Isolated", coupling.isolated, isolated, coupling.n, "below", unlinked),
        ("Mean Ct without outliers", trimmed),
    ]
    if coupling.stubs_depended_on:
        stubs = ", ".join(f"{name} ({count_of(count, 'dependent')})" for name, count in coupling.stubs_depended_on)
        rows.append(("External stubs most depended on", stubs))
    top = tuple((row.name, row.type, str(row.ca), str(row.ce), str(row.ct)) for row in top_components(components))
    return [_Pairs(tuple(rows)), _Table(("Component", "Type", "Ca", "Ce", "Ct"), top)]


def _member(row: MemberRow) -> str:
    """A member as its table names it: its own name, and its type's where the two differ."""
    return row.name if row.type == row.name else f"{row.name} ({row.type})"


def _tables(tables: Sequence[CompositionTable], sentences: Sequence[tuple[str, str]]) -> list[_Block]:
    """Each table under its caption, the caption carrying the table's own sentence."""
    if not tables:
        return [_Paragraph(_na(_NO_COMPOSITION))]
    blocks: list[_Block] = []
    for composition, (_, sentence) in zip(tables, sentences, strict=True):
        if isinstance(composition.rows, NotAvailable):
            blocks.append(_Caption(composition.composition, _na(composition.rows.reason)))
            continue
        rows = tuple((_member(row), row.kind, str(row.ca), str(row.ce), str(row.ct)) for row in composition.rows)
        blocks += [_Caption(composition.composition, sentence), _Table(("Member", "Kind", "Ca", "Ce", "Ct"), rows)]
    return blocks


#: Each reason for no cohesion figure as a count of components reads it: ``8 whose runnables ...``.
_COHESION_REASON_WORDS = {
    NO_RUNNABLE_OF_ITS_OWN: "with no runnable",
    NO_ACCESS: "whose runnables declare no access",
    ONE_ACCESSING_RUNNABLE: "where only one runnable declares an access",
    ONE_PORT_OR_VARIABLE: "whose runnables that use a port or a variable all use one and the same port or variable",
}


def _cohesion(cohesion: Cohesion | NotAvailable) -> list[_Block]:
    if isinstance(cohesion, NotAvailable):
        return [_Paragraph(_na(cohesion.reason))]
    below = [f"{name} ({format_exact(figure)})" for name, figure in cohesion.below]
    rows = [
        ("Mean cohesion", f"{format_exact(cohesion.mean)} over {count_of(cohesion.components, 'component')}"),
        (
            "Below 0.5, a starting threshold not calibrated",
            f"{len(below)}: {_names(below)}" if below else "0",
        ),
    ]
    if cohesion.not_available:
        count = sum(number for _, number in cohesion.not_available)
        reasons = ", ".join(f"{number} {_COHESION_REASON_WORDS[reason]}" for reason, number in cohesion.not_available)
        rows.append(("No figure", f"{count_of(count, 'component')}: {reasons}"))
    return [_Pairs(tuple(rows))]


def _asil(asil: AsilCoverage | NotAvailable) -> str:
    if isinstance(asil, NotAvailable):
        return _na(asil.reason)
    return f"{asil.declared} declared, {asil.inherited} inherited, {len(asil.neither)} neither"


def _runnables(runnable: RunnableCoverage | NotAvailable) -> str:
    if isinstance(runnable, NotAvailable):
        return _na(runnable.reason)
    return f"{runnable.with_runnable} of {runnable.components}"


def _tasks(task: TaskCoverage | NotAvailable) -> str:
    if isinstance(task, NotAvailable):
        return _na(task.reason)
    server = ""
    if task.server:
        their = "its" if task.server == 1 else "their"
        runs = "runs" if task.server == 1 else "run"
        server = f"; {count_of(task.server, 'server runnable')} excluded ({runs} in {their} callers' tasks)"
    return f"{task.on_task} of {task.runnables}{server}"


def _requirements(requirement: RequirementCoverage | NotAvailable) -> tuple[str, str]:
    if isinstance(requirement, NotAvailable):
        return _na(requirement.reason), _na(requirement.reason)
    stubs = ""
    if requirement.stubs:
        # The count is of ids: two stub files of one id, or a stub and a non-stub file of one id, are one.
        held = "a stub requirement, a placeholder" if requirement.stubs == 1 else "stub requirements, placeholders"
        stubs = f"; {count_of(requirement.stubs, 'id')} held by {held}, excluded"
    return (
        f"{requirement.allocated} of {requirement.requirements}{stubs}",
        f"{requirement.linked} of {requirement.components}",
    )


def _coverage(coverage: Coverage) -> list[_Block]:
    allocated, linked = _requirements(coverage.requirement)
    rows = (
        ("ASIL", _asil(coverage.asil)),
        ("Components with a runnable", _runnables(coverage.runnable)),
        ("Runnables on a task", _tasks(coverage.task)),
        ("Requirements allocated", allocated),
        ("Components with a requirement link", linked),
    )
    return [_Pairs(rows)]


def _families(count: int, families: Sequence[tuple[str, int]]) -> str:
    if not count:
        return "0"
    return f"{count}: " + ", ".join(f"{family} {number}" for family, number in families)


def _integrity(integrity: Integrity) -> list[_Block]:
    rows = [
        ("Errors", _families(integrity.errors, integrity.errors_by_family)),
        ("Warnings", _families(integrity.warnings, integrity.warnings_by_family)),
        ("Unconnected component ports", f"{len(integrity.unconnected)} of {integrity.ports}"),
    ]
    if integrity.unconnected:
        rows.append(("Unconnected", _names(integrity.unconnected)))
    return [_Pairs(tuple(rows))]


def _shown_change(figure: MovementFigure) -> str:
    """The change from the base to the working tree as the two figures are shown."""
    before, after = figure.values[0], figure.values[-1]
    assert not isinstance(before, NotAvailable) and not isinstance(after, NotAvailable)
    return format_shown_change(before, after)


def _change_cell(figure: MovementFigure) -> str:
    if figure.change is None:
        return f"{_NOT_AVAILABLE} at {' and '.join(figure.not_available_at)}"
    return _shown_change(figure)


def _threshold(side: ArchitectureState) -> int | None:
    """The hub threshold in hundredths, as shown, or ``None`` where coupling is ``n/a``."""
    coupling = side.coupling
    return None if isinstance(coupling, NotAvailable) else round_hundredths(coupling.mean, 2, coupling.variance)


def _hub_names(names: tuple[str, ...] | NotAvailable, base: ArchitectureState, current: ArchitectureState) -> str:
    """Hubs gained or lost; one whose ``Ct`` did not move says so, and how the threshold moved."""
    if isinstance(names, NotAvailable):
        return _na(names.reason)
    before = {row.name: row.ct for row in base.components}
    after = {row.name: row.ct for row in current.components}
    old, new = _threshold(base), _threshold(current)

    def hub(name: str) -> str:
        if name not in before or before[name] != after.get(name) or old is None or new is None or old == new:
            return name
        moved = "rose" if new > old else "fell"
        shown = f"from {format_hundredths(old)} to {format_hundredths(new)}"
        return f"{name} (its Ct is unchanged at {before[name]}; the threshold {moved} {shown})"

    return _names([hub(name) for name in names])


def _movement(report: MetricsReport) -> list[_Block]:
    """The figures per column; none when no base was read or nothing differs, the conclusion's one sentence."""
    movement = report.movement
    if movement is None or movement.unchanged:
        return []
    labels = tuple(side.label for side in report.series)
    rows = tuple(
        (figure.figure, *(_figure(value) for value in figure.values), _change_cell(figure))
        for figure in movement.figures
    )
    pairs = []
    if movement.added:
        pairs.append(("Components added", _names(movement.added)))
    if movement.removed:
        pairs.append(("Components removed", _names(movement.removed)))
    base, current = report.series[0], report.series[-1]
    if movement.hubs_gained:
        pairs.append(("Hubs gained", _hub_names(movement.hubs_gained, base, current)))
    if movement.hubs_lost:
        pairs.append(("Hubs lost", _hub_names(movement.hubs_lost, base, current)))
    pairs.append(
        (
            "Findings",
            f"{len(movement.introduced)} introduced, {len(movement.resolved)} resolved; "
            f"{movement.still_open} of the base's {movement.base_findings} still open",
        )
    )
    return [_Table(("", *labels, "change"), rows), _Pairs(tuple(pairs))]


def _entry(key: str, entry: LookAtEntry) -> str:
    if key == LOOK_ERRORS:
        return f"{entry.name} {entry.figure}"
    if key == LOOK_NO_LINK:
        if entry.unconnected:
            return f"{entry.name} ({entry.word}, {count_of(entry.unconnected, 'unconnected port')})"
        return f"{entry.name} ({entry.word})"
    if key == LOOK_LOW_COHESION and isinstance(entry.figure, Fraction):
        return f"{entry.name} ({format_exact(entry.figure)})"
    if key == LOOK_ROSE:
        return f"{entry.name} Ct {entry.before} {_ARROW} {entry.figure}"
    if key == LOOK_INTRODUCED:
        # The message's own closing period is left to the line, which ends in one.
        return f"{entry.name}: {(entry.message or '').removesuffix('.')}"
    if entry.figure is not None:
        return f"{entry.name} (Ct {entry.figure})"
    return entry.name


def _look_at(lines: Sequence[LookAtLine]) -> list[_Block]:
    items = []
    for line in lines:
        separator = "; " if line.key in (LOOK_NO_LINK, LOOK_INTRODUCED) else ", "
        entries = [_entry(line.key, entry) for entry in line.entries]
        text = name_list(entries, len(entries), separator=separator, more=line.more)
        items.append((LOOK_AT_LABELS[line.key], f"{text}."))
    return [_Bullets(tuple(items))] if items else []


def _sections(report: MetricsReport, conclusions: Conclusions) -> list[_Section]:
    """The page's sections in their order; *Movement* only when a base was asked for.

    *Overview*, the two answers, comes first, then *Definitions and method*. Each section after them
    is, in this order: the pointer to the terms it uses, its figures, its conclusion for this model, and
    its limit where it has one. *What to look at* puts its conclusion above its lists, which the
    sentence introduces.
    """
    current = report.series[-1]
    figures: dict[str, tuple[str, int, list[_Block]]] = {
        "size": ("Size", 3, _size(current.size)),
        "coupling": ("Coupling", 3, _coupling(current.coupling, current.components)),
        "tables": (
            "Coupling inside each composition",
            3,
            _tables(current.composition_tables, conclusions.tables),
        ),
        "cohesion": ("Cohesion", 3, _cohesion(current.cohesion)),
        "coverage": ("Coverage", 3, _coverage(current.coverage)),
        "integrity": ("Integrity", 3, _integrity(current.integrity)),
        "movement": ("Movement", 2, _movement(report)),
        "look_at": ("What to look at", 2, _look_at(report.look_at)),
    }
    sections = [
        _Section("answers", OVERVIEW_TITLE, 2, tuple(_Answer(*pair) for pair in _answers(conclusions))),
        _Section("definitions", DEFINITIONS_TITLE, 2, (_Bullets(DEFINITIONS),)),
        _Section("state", "State", 2, ()),
    ]
    for key, conclusion in conclusions.sections:
        title, level, blocks = figures[key]
        explained: list[_Block] = [_Refer(SECTION_TERMS[key])]
        if key == "look_at":
            # Its sentence introduces the lists ("the lists below"), so it stands above them.
            explained += [_Paragraph(conclusion.text), *blocks]
        else:
            explained += [*blocks, _Paragraph(conclusion.text)]
        if conclusion.limit is not None:
            explained.append(_Limit(conclusion.limit))
        sections.append(_Section(key, title, level, tuple(explained)))
    return sections


def _answers(conclusions: Conclusions) -> tuple[tuple[str, str], tuple[str, str]]:
    return (_STATE_QUESTION, conclusions.state_answer), (_HEADING_QUESTION, conclusions.heading_answer)


def _subtitle(report: MetricsReport) -> str:
    """Which revisions the page reads: the working tree, and the base and the series' other points."""
    current = report.series[-1].label
    subject = current[:1].upper() + current[1:]
    if report.movement is None:
        return f"{subject}; nothing to compare it with." if report.no_base is not None else f"{subject}."
    others = [side.label for side in report.series[1:-1]]
    also = f", by way of {', '.join(others)}" if others else ""
    return f"{subject}, compared with {report.series[0].label}{also}."


# ---------------------------------------------------------------------------
# The text and the Markdown
# ---------------------------------------------------------------------------


def _line(text: str) -> str:
    """A string of the text on one line."""
    return " ".join(text.splitlines())


def _text_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    cells = [[_line(cell) for cell in row] for row in (header, *rows)]
    widths = [max(len(row[index]) for row in cells) for index in range(len(header))]
    return [
        "  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in cells
    ]


def _text_block(block: _Block) -> list[str]:
    if isinstance(block, _Paragraph):
        return [_line(block.text)]
    if isinstance(block, _Pairs):
        width = max(len(label) for label, _ in block.rows)
        return [f"  {label.ljust(width)}  {_line(value)}" for label, value in block.rows]
    if isinstance(block, _Table):
        return _text_table(block.header, block.rows)
    if isinstance(block, _Caption):
        return [_line(f"{block.name}." if block.text is None else f"{block.name}. {block.text}")]
    if isinstance(block, _Answer):
        return [_line(f"{block.question} {block.answer}")]
    if isinstance(block, _Refer):
        return [f"Terms: {'; '.join(block.terms)} (see {DEFINITIONS_TITLE} above)."]
    if isinstance(block, _Limit):
        return [f"Limit: {_line(block.text)}"]
    return [f"- {label}: {_line(text)}" for label, text in block.items]


def render_text(report: MetricsReport) -> str:
    """What ``swcraft metrics`` prints.

    The title, the overview, the definitions, then every section, newline-terminated.
    """
    conclusions = conclude(report)
    lines = [_line(f"{_TITLE} {_DASH} {report.project_name}"), _subtitle(report) + " " + _GATE.replace("`", "")]
    for section in _sections(report, conclusions):
        lines += ["", section.title.upper() if section.level == 2 else section.title]
        for block in section.blocks:
            lines += ["", *_text_block(block)]
    return "\n".join(lines) + "\n"


def _markdown_block(block: _Block) -> list[str]:
    if isinstance(block, _Paragraph):
        return [cell_text(block.text)]
    if isinstance(block, _Pairs):
        return table(("", ""), [(cell_text(label), cell_text(value)) for label, value in block.rows])
    if isinstance(block, _Table):
        return table(
            tuple(cell_text(cell) for cell in block.header),
            [tuple(cell_text(cell) for cell in row) for row in block.rows],
        )
    if isinstance(block, _Caption):
        caption = f"*{cell_text(block.name)}.*"
        return [caption if block.text is None else f"{caption} {cell_text(block.text)}"]
    if isinstance(block, _Answer):
        return [f"**{block.question}** {cell_text(block.answer)}"]
    if isinstance(block, _Refer):
        terms = "; ".join(cell_text(term) for term in block.terms)
        return [f"*Terms:* {terms}; see [{DEFINITIONS_TITLE}]({_DEFINITIONS_ANCHOR})."]
    if isinstance(block, _Limit):
        return [f"*Limit:* {cell_text(block.text)}"]
    return [f"- **{cell_text(label)}:** {cell_text(text)}" for label, text in block.items]


def render_markdown(report: MetricsReport) -> str:
    """The file ``--report`` writes: the same sections as :func:`render_text`, newline-terminated."""
    conclusions = conclude(report)
    lines = [f"# {_TITLE} {_DASH} {cell_text(report.project_name)}", "", f"{cell_text(_subtitle(report))} {_GATE}"]
    for section in _sections(report, conclusions):
        lines += ["", f"{'#' * section.level} {section.title}"]
        for block in section.blocks:
            lines += ["", *_markdown_block(block)]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The JSON
# ---------------------------------------------------------------------------


def _json_value(value: int | Fraction | NotAvailable | None) -> Any:
    """An integer as it is, a ratio as its printed string, ``n/a`` as an object carrying the reason."""
    if isinstance(value, NotAvailable):
        return {"not_available": value.reason}
    if isinstance(value, Fraction):
        return format_exact(value)
    return value


def _json_names(names: tuple[str, ...] | NotAvailable) -> Any:
    return _json_value(names) if isinstance(names, NotAvailable) else list(names)


def _size_json(size: Size) -> dict[str, Any]:
    return {
        "components": size.components,
        "external_stubs": size.external_stubs,
        "compositions": size.compositions,
        "nesting_depth": size.nesting_depth,
        "ports": size.ports,
        "component_ports": size.component_ports,
        "stub_ports": size.stub_ports,
        "boundary_ports": size.boundary_ports,
        "connectors": size.connectors,
        "delegations": size.delegations,
        "runnables": size.runnables,
        "layers": dict(size.layers),
    }


def _reasons_json(by_reason: Sequence[tuple[str, int]]) -> list[dict[str, Any]]:
    return [{"reason": reason, "count": count} for reason, count in by_reason]


def _coupling_json(coupling: Coupling | NotAvailable) -> Any:
    if isinstance(coupling, NotAvailable):
        return _json_value(coupling)
    return {
        "n": coupling.n,
        "dependencies": coupling.dependencies,
        "mean": format_exact(coupling.mean),
        "deviation": _root(Fraction(0), 1, coupling.variance),
        "hub_threshold": _root(coupling.mean, 2, coupling.variance),
        "hubs": _json_names(coupling.hubs),
        "isolated_threshold": _root(coupling.mean, -2, coupling.variance),
        "isolated": _json_names(coupling.isolated),
        "trimmed_mean": format_exact(coupling.trimmed_mean),
        "trimmed_max": coupling.trimmed_max,
        "trimmed_over_all": coupling.trimmed_over_all,
        "external_stubs": coupling.external_stubs,
        "stubs_depended_on": [{"name": name, "dependents": count} for name, count in coupling.stubs_depended_on],
        "links_not_measured": coupling.links_not_measured,
        "links_not_measured_by_reason": _reasons_json(coupling.not_measured_by_reason),
    }


def _component_json(row: ComponentRow) -> dict[str, Any]:
    return {
        "name": row.name,
        "type": row.type,
        "ca": row.ca,
        "ce": row.ce,
        "ct": row.ct,
        "external_neighbours": row.external_neighbours,
        "unconnected_ports": row.unconnected_ports,
        "cohesion": _json_value(row.cohesion),
        "measured_runnables": row.measured_runnables,
        "no_link": row.no_link,
    }


def _table_json(composition: CompositionTable, sentence: str) -> dict[str, Any]:
    rows: Any
    if isinstance(composition.rows, NotAvailable):
        rows = _json_value(composition.rows)
    else:
        rows = [
            {"name": row.name, "type": row.type, "kind": row.kind, "ca": row.ca, "ce": row.ce, "ct": row.ct}
            for row in composition.rows
        ]
    return {"composition": composition.composition, "links": composition.links, "rows": rows, "conclusion": sentence}


def _cohesion_json(cohesion: Cohesion | NotAvailable) -> Any:
    if isinstance(cohesion, NotAvailable):
        return _json_value(cohesion)
    return {
        "components": cohesion.components,
        "mean": format_exact(cohesion.mean),
        "threshold": format_exact(COHESION_THRESHOLD),
        "below": [{"name": name, "cohesion": format_exact(figure)} for name, figure in cohesion.below],
        "not_available": [{"reason": reason, "count": count} for reason, count in cohesion.not_available],
        "runnables": cohesion.runnables,
        "runnables_used": cohesion.runnables_used,
    }


def _coverage_json(coverage: Coverage) -> dict[str, Any]:
    asil, runnable, task, requirement = coverage.asil, coverage.runnable, coverage.task, coverage.requirement
    return {
        "asil": _json_value(asil)
        if isinstance(asil, NotAvailable)
        else {
            "components": asil.components,
            "declared": asil.declared,
            "inherited": asil.inherited,
            "neither": list(asil.neither),
        },
        "runnable": _json_value(runnable)
        if isinstance(runnable, NotAvailable)
        else {
            "components": runnable.components,
            "with_runnable": runnable.with_runnable,
            "without": list(runnable.without),
        },
        "task": _json_value(task)
        if isinstance(task, NotAvailable)
        else {
            "runnables": task.runnables,
            "on_task": task.on_task,
            "server": task.server,
            "not_on_task": list(task.not_on_task),
        },
        "requirement": _json_value(requirement)
        if isinstance(requirement, NotAvailable)
        else {
            "requirements": requirement.requirements,
            "allocated": requirement.allocated,
            "stubs": requirement.stubs,
            "unallocated": list(requirement.unallocated),
            "components": requirement.components,
            "linked": requirement.linked,
            "unlinked": list(requirement.unlinked),
        },
    }


def _integrity_json(integrity: Integrity) -> dict[str, Any]:
    return {
        "errors": integrity.errors,
        "warnings": integrity.warnings,
        "errors_by_family": [{"family": family, "count": count} for family, count in integrity.errors_by_family],
        "warnings_by_family": [{"family": family, "count": count} for family, count in integrity.warnings_by_family],
        "by_rule": [{"rule": rule, "count": count} for rule, count in integrity.by_rule],
        "ports": integrity.ports,
        "unconnected": list(integrity.unconnected),
    }


def _state_json(state: ArchitectureState, conclusions: Conclusions, *, rows: bool = True) -> dict[str, Any]:
    """The state block; without *rows*, no per-component row and no composition table."""
    tables = zip(state.composition_tables, conclusions.tables, strict=True)
    listed: dict[str, Any] = {}
    if rows:
        listed = {
            "components": [_component_json(row) for row in state.components],
            "composition_tables": [_table_json(composition, sentence) for composition, (_, sentence) in tables],
        }
    return {
        "label": state.label,
        "size": _size_json(state.size),
        "coupling": _coupling_json(state.coupling),
        **listed,
        "links_not_placed": state.links_not_placed,
        "links_not_placed_by_reason": _reasons_json(state.links_not_placed_by_reason),
        "cohesion": _cohesion_json(state.cohesion),
        "coverage": _coverage_json(state.coverage),
        "integrity": _integrity_json(state.integrity),
    }


def _finding_json(finding: ConsistencyIssue) -> dict[str, str]:
    return {"rule": finding.rule, "severity": finding.severity, "message": finding.message}


def _json_change(figure: MovementFigure) -> Any:
    """A change as the JSON carries it: an integer's as it is, a ratio's as the shown figures' difference."""
    if isinstance(figure.change, Fraction):
        before, after = figure.values[0], figure.values[-1]
        assert not isinstance(before, NotAvailable) and not isinstance(after, NotAvailable)
        return format_hundredths(shown_difference(before, after))
    return figure.change


def _movement_json(movement: Movement | None) -> Any:
    if movement is None:
        return None
    return {
        "base": movement.base,
        "current": movement.current,
        "unchanged": movement.unchanged,
        "figures": [
            {
                "figure": figure.figure,
                "values": [_json_value(value) for value in figure.values],
                "change": None if figure.change is None else _json_change(figure),
                "not_available_at": list(figure.not_available_at),
            }
            for figure in movement.figures
        ],
        "added": list(movement.added),
        "removed": list(movement.removed),
        "ct_changes": [
            {"name": change.name, "before": change.before, "after": change.after, "change": change.change}
            for change in movement.ct_changes
        ],
        "hubs_gained": _json_names(movement.hubs_gained),
        "hubs_lost": _json_names(movement.hubs_lost),
        "introduced": [_finding_json(finding) for finding in movement.introduced],
        "resolved": [_finding_json(finding) for finding in movement.resolved],
        "base_findings": movement.base_findings,
        "still_open": movement.still_open,
    }


def _look_at_json(line: LookAtLine) -> dict[str, Any]:
    return {
        "line": line.key,
        "entries": [
            {
                "name": entry.name,
                "figure": _json_value(entry.figure),
                "before": entry.before,
                "word": entry.word,
                "unconnected": entry.unconnected,
                "message": entry.message,
            }
            for entry in line.entries
        ],
        "more": line.more,
    }


def json_document(
    report: MetricsReport, conclusions: Conclusions | None = None, *, rows: bool = True
) -> dict[str, Any]:
    """``--format json``'s document as a mapping: one versioned document, every component's row.

    A ratio is its printed string, two places; an ``n/a`` is ``{"not_available": reason}`` wherever a
    figure would stand. ``state`` is the working tree's; ``movement`` is ``null`` when no base was read,
    and ``no_base`` then says why when one was asked for. ``answers`` holds the two answers and
    ``conclusions`` each section's sentences, as one string, and its limit, ``null`` where it has none, in
    the sections' order; each composition table carries its own sentence. The definitions are not in it.
    *conclusions* is :func:`conclude`'s of *report*, computed here when not given. Without *rows*, the
    state carries no per-component row and no composition table.
    """
    if conclusions is None:
        conclusions = conclude(report)
    return {
        "document_version": DOCUMENT_VERSION,
        "kind": "architecture-metrics",
        "project": report.project_name,
        "columns": [side.label for side in report.series],
        "answers": {"state": conclusions.state_answer, "heading": conclusions.heading_answer},
        "state": _state_json(report.series[-1], conclusions, rows=rows),
        "movement": _movement_json(report.movement),
        "no_base": report.no_base,
        "look_at": [_look_at_json(line) for line in report.look_at],
        "conclusions": {
            key: {"conclusion": conclusion.text, "limit": conclusion.limit} for key, conclusion in conclusions.sections
        },
    }


def render_json(report: MetricsReport) -> str:
    """``--format json``: :func:`json_document`, ``indent=2``, newline-terminated."""
    return json.dumps(json_document(report), indent=2, allow_nan=False) + "\n"


# ---------------------------------------------------------------------------
# The bounded answer
# ---------------------------------------------------------------------------

#: Every list the bounded answer caps, as ``(block, key)`` paths into the JSON document's ``state``
#: and ``movement``. A list whose length is a constant of the tool (families, rules, the cohesion
#: reasons, the movement's figures, the five stubs depended on) is not capped and not listed.
_CAPPED_STATE = (
    ("coupling", "hubs"),
    ("coupling", "isolated"),
    ("cohesion", "below"),
    ("integrity", "unconnected"),
)
_CAPPED_COVERAGE = (
    ("asil", "neither"),
    ("runnable", "without"),
    ("task", "not_on_task"),
    ("requirement", "unallocated"),
    ("requirement", "unlinked"),
)
_CAPPED_MOVEMENT = ("added", "removed", "ct_changes", "hubs_gained", "hubs_lost", "introduced", "resolved")


def _cap(block: Any, key: str) -> None:
    """``block[key]``, a list, capped in place.

    It becomes ``{"first": its first :data:`ANSWER_LIST_CAP`, "more": how many follow}``; an ``n/a`` stays.
    """
    if isinstance(block, dict) and isinstance(block.get(key), list):
        items = block[key]
        block[key] = {"first": items[:ANSWER_LIST_CAP], "more": max(len(items) - ANSWER_LIST_CAP, 0)}


def bounded_document(report: MetricsReport, conclusions: Conclusions) -> dict[str, Any]:
    """:func:`json_document` with its size bounded by constants and not by the model: the tool's answer.

    The strings :func:`json_document` carries, with no per-component row, no composition table (the count
    of compositions stays in ``size``) and every list whose length grows with the model capped at
    :data:`ANSWER_LIST_CAP`: ``{"first": [...], "more": n}``. No version, kind or project name. What this
    does not bound: the length of a name, which the model chooses, and the rules ``check-model`` can name, a
    constant of the release; the caller refuses an answer over its budget.
    """
    document = json_document(report, conclusions, rows=False)
    state = document["state"]
    for block, key in _CAPPED_STATE:
        _cap(state[block], key)
    for block, key in _CAPPED_COVERAGE:
        _cap(state["coverage"][block], key)
    for key in _CAPPED_MOVEMENT:
        _cap(document["movement"], key)
    return {
        "response": "metrics",
        "columns": document["columns"],
        "answers": document["answers"],
        "state": state,
        "movement": document["movement"],
        "no_base": document["no_base"],
        "look_at": document["look_at"],
        "conclusions": document["conclusions"],
    }
