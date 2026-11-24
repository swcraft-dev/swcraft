"""Architecture metrics: what state the architecture is in, computed from the model and added to nothing.

The computation only; rendering lives apart. Every figure is computed from a loaded
:class:`~swcraft_tools.model_state.ModelState`, and a figure the model holds no data for is :class:`NotAvailable`
with its reason, never zero. It is no gate: nothing here refuses a model, an invalid one included.

- **A node** is an atomic component type, linked through the ``connection_ends`` computed value; a link between two
  prototypes of one type is dropped. Stubs are neighbours, not members; a name two component files share is read off
  its last declared file (else its last stub), and only :class:`Size` counts every file.
- **A dependency** points from the require port's owner to the provide port's owner, read off the ports'
  ``direction``; ``Ct = Ca + Ce``. The statistics are ``Fraction``s and the hub and isolated verdicts are decided on
  squares, so no float decides them.
- **The per-composition tables** count each connector once, at the level it is drawn, and walk no delegation; no
  figure reads them.
- **Integrity** counts ``check-model``'s findings and the ``unconnected`` computed value; it adds no rule.
- **Cohesion** reads the ports and inter-runnable variables each runnable declares it reads, writes, calls or is
  started by; a runnable that declares nothing is left out and counted. ``PSI < 0.5`` is a starting threshold, not
  calibrated.
- **A coverage** is ``n/a`` only where the declared components and requirements hold no such data.
- **Movement** reads every revision as ``swcraft diff`` does, the first named the base and the working tree last;
  nothing is stored, and a figure ``n/a`` at either end has no change.

What this does not reach: the instance level; a link leaving a table's box; a ``PASS-THROUGH-SW-CONNECTOR``. A
repeated connector name is read at its first declaration and counted in ``links_not_measured``; a port delegated to
an unwired boundary port counts as connected; a task entry naming an ``instance`` covers the runnable for its whole
type; a link to a requirement the model does not declare counts for no component. *Movement* follows only its
table's figures, each component's ``Ct`` and the findings; it reads the manifest from the working tree; a rename is
a removal and an addition, and a finding whose message moved compares as new.
"""

from __future__ import annotations

import contextlib
import math
import traceback
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from typing import TYPE_CHECKING, TypeVar

from swcraft_tools.allocation import runnable_kind
from swcraft_tools.change_report import RevisionParseError, Side, all_findings, introduced_findings, open_side
from swcraft_tools.model_registry import ModelRegistry, current_registry
from swcraft_tools.model_state import ModelState, RetiredStorageError, RevisionReadError, single_read
from swcraft_tools.scheduling_identity import identities_from
from swcraft_tools.schema import (
    CallReturnedEvent,
    Composition,
    CompositionMember,
    ConnectorEndpoint,
    DataEvent,
    DataReadRef,
    DataWriteRef,
    OperationCallRef,
    OperationEvent,
    Requirement,
    Runnable,
    SWComponent,
    VariableReadRef,
    VariableWriteRef,
)
from swcraft_tools.snapshot import SnapshotReadError
from swcraft_tools.validator import SEVERITY_ERROR, ConsistencyIssue, derive_rows

if TYPE_CHECKING:
    from swcraft_tools.arxml_version import ArxmlVersion
    from swcraft_tools.project import SwcraftProject

__all__ = [
    "COHESION_REASONS",
    "COHESION_THRESHOLD",
    "CONNECTED_UNMEASURED",
    "DELEGATED_TO_NO_ONE",
    "EMPTY_COMPOSITION",
    "END_NOT_A_MEMBER",
    "HEAD_DOES_NOT_PARSE",
    "HEAD_GIT_REFUSES",
    "HEAD_NOT_IN_GIT",
    "HEAD_NO_COMMIT",
    "HEAD_NO_GIT",
    "HEAD_NOT_UNPACKED",
    "HEAD_NO_MODEL",
    "HEAD_RETIRED_LAYOUT",
    "INCOMPLETE",
    "KIND_COMPONENT",
    "KIND_COMPOSITION",
    "KIND_STUB",
    "LOOK_AT_CAP",
    "LOOK_AT_KEYS",
    "LOOK_ERRORS",
    "LOOK_HUBS",
    "LOOK_INTRODUCED",
    "LOOK_ISOLATED",
    "LOOK_LOW_COHESION",
    "LOOK_NEW_HUBS",
    "LOOK_NO_LINK",
    "LOOK_ROSE",
    "LOOK_UNSCHEDULED",
    "LOWER_THRESHOLD_BELOW_ZERO",
    "NOT_MEASURED_REASONS",
    "NOT_PLACED_REASONS",
    "NO_ACCESS",
    "NO_COHESION_FIGURE",
    "NO_COMPONENT",
    "NO_COMPONENT_ACCESS",
    "NO_LINK_WORDS",
    "NO_PORT",
    "NO_REQUIREMENT",
    "NO_RUNNABLE",
    "NO_RUNNABLE_OF_ITS_OWN",
    "NO_SAFETY_LEVEL",
    "NO_TASK",
    "ONE_ACCESSING_RUNNABLE",
    "ONE_DIRECTION",
    "ONE_PORT_OR_VARIABLE",
    "REPEATED_NAME",
    "TOO_FEW_FOR_OUTLIERS",
    "UNREACHED_END",
    "UNRESOLVED_OWNER",
    "UNRESOLVED_PORT",
    "ArchitectureState",
    "AsilCoverage",
    "Cohesion",
    "ComponentRow",
    "CompositionTable",
    "Coupling",
    "Coverage",
    "CtChange",
    "Integrity",
    "Links",
    "LookAtEntry",
    "LookAtLine",
    "MemberRow",
    "MetricsReport",
    "Movement",
    "MovementFigure",
    "NotAvailable",
    "RequirementCoverage",
    "RunnableCoverage",
    "Size",
    "TaskCoverage",
    "compare",
    "component_cohesion",
    "composition_tables",
    "compute_metrics",
    "look_at",
    "measure",
    "measure_cohesion",
    "measure_coupling",
    "measure_coverage",
    "measure_integrity",
    "measure_links",
    "measure_size",
    "top_components",
]

_B = TypeVar("_B")

#: Below this many components no value can lie more than two deviations out, by Samuelson's
#: inequality, ``|x - mean| / deviation <= sqrt(n - 1)``, so the test could never fire.
_FEWEST_FOR_OUTLIERS = 6
#: The rows of the top list.
_TOP_COMPONENTS = 10
#: The stubs named beside their count.
_TOP_STUBS = 5

NO_COMPONENT = "the model declares no component"
TOO_FEW_FOR_OUTLIERS = "fewer than six components: no value can lie more than two deviations out"
LOWER_THRESHOLD_BELOW_ZERO = "the lower threshold is below zero"
EMPTY_COMPOSITION = "the composition has no member"
NO_SAFETY_LEVEL = "no declared component or requirement states a safety level"
NO_RUNNABLE = "no component declares a runnable"
NO_TASK = "the model has no task"
NO_REQUIREMENT = "the model declares no requirement"
NO_RUNNABLE_OF_ITS_OWN = "no runnable"
NO_ACCESS = "no runnable declares an access"
ONE_ACCESSING_RUNNABLE = "only one runnable declares an access"
ONE_PORT_OR_VARIABLE = "one port or variable: every runnable that uses one uses only it"
NO_COMPONENT_ACCESS = "no component's runnables declare an access"
NO_COHESION_FIGURE = "no component's runnables give a figure"
#: A component's reasons for no cohesion figure, in the order they are listed.
COHESION_REASONS = (NO_RUNNABLE_OF_ITS_OWN, NO_ACCESS, ONE_ACCESSING_RUNNABLE, ONE_PORT_OR_VARIABLE)
#: The cohesion below which a component is listed: a starting threshold, not calibrated.
COHESION_THRESHOLD = Fraction(1, 2)

#: The four facts of a component with ``Ct = 0``, in the order they are decided; they are exhaustive.
#: None says *by design*: that needs a reason the model does not hold.
NO_PORT = "no port"
INCOMPLETE = "incomplete"
CONNECTED_UNMEASURED = "connected, no measured dependency"
DELEGATED_TO_NO_ONE = "delegated, reaches no one"
NO_LINK_WORDS = (NO_PORT, INCOMPLETE, CONNECTED_UNMEASURED, DELEGATED_TO_NO_ONE)

#: Why a connector, or one pair of its ends, draws no measured dependency, in the order they are listed.
#: The first and the last count connectors, the second counts pairs of ends.
UNREACHED_END = "unreached end"
ONE_DIRECTION = "one direction"
REPEATED_NAME = "repeated name"
NOT_MEASURED_REASONS = (UNREACHED_END, ONE_DIRECTION, REPEATED_NAME)
#: Why a connector is placed in no composition table, in the order they are listed.
UNRESOLVED_OWNER = "unresolved composition"
END_NOT_A_MEMBER = "end not a member"
UNRESOLVED_PORT = "unresolved port"
NOT_PLACED_REASONS = (UNRESOLVED_OWNER, END_NOT_A_MEMBER, UNRESOLVED_PORT, ONE_DIRECTION)

#: The kinds of a composition table's row.
KIND_COMPONENT = "component"
KIND_COMPOSITION = "composition"
KIND_STUB = "stub"


@dataclass(frozen=True)
class NotAvailable:
    """A figure the model cannot answer, with why, in place of a number."""

    reason: str


@dataclass(frozen=True)
class Size:
    """How much model there is, counted over files: a name two files share counts twice.

    *layers* is every declared layer's element count in ``display_order``, so a layer added to the
    contract appears with no edit here. *ports* counts the declared components' ports, the external
    stubs' and the compositions' boundary ports alike; *component_ports*, *stub_ports* and
    *boundary_ports* are its three parts. *nesting_depth* is the longest chain of compositions, 1 for a composition
    holding only components and 0 for no composition; a composition graph that comes back to itself
    still answers, the compositions of one cycle counting as many levels as they are.
    """

    layers: tuple[tuple[str, int], ...]
    components: int
    external_stubs: int
    compositions: int
    nesting_depth: int
    runnables: int
    connectors: int
    delegations: int
    component_ports: int
    stub_ports: int
    boundary_ports: int

    @property
    def ports(self) -> int:
        """Every port: the declared components', the external stubs' and the compositions' boundary ports."""
        return self.component_ports + self.stub_ports + self.boundary_ports


@dataclass(frozen=True)
class Links:
    """The dependencies between atomic component types, stubs included.

    *dependencies* holds ``(dependent, dependency)`` pairs: a pair of ends is one dependency however
    many connectors state it. *not_measured* counts a connector with no pair of ends, a pair with two
    ends of one direction, and a repeat of a declared connector name; *not_measured_by_reason* splits
    it by :data:`NOT_MEASURED_REASONS`, a reason with no count left out. *touched* names every
    component a connector reaches, whether or not the pair draws a link: an end a connector names
    directly, and both ends of every pair ``connection_ends`` resolves.
    """

    dependencies: frozenset[tuple[str, str]]
    touched: frozenset[str] = frozenset()
    not_measured_by_reason: tuple[tuple[str, int], ...] = ()

    @property
    def not_measured(self) -> int:
        """Every connector, or pair of ends, that gives no dependency: the sum of the reasons."""
        return sum(count for _, count in self.not_measured_by_reason)


@dataclass(frozen=True)
class ComponentRow:
    """One declared component's figures.

    *external_neighbours* are the distinct stubs among its links. *measured_runnables* are its
    runnables that declare an access, whether or not they give *cohesion* a figure. *no_link* is
    ``None`` while ``Ct`` is above 0, and at 0 one of :data:`NO_LINK_WORDS`, a fact and never a verdict.
    """

    name: str
    type: str
    ca: int
    ce: int
    external_neighbours: int
    unconnected_ports: int
    cohesion: Fraction | NotAvailable
    measured_runnables: int
    no_link: str | None = None

    @property
    def ct(self) -> int:
        """``Ca + Ce``, so a neighbour linked in both directions counts twice."""
        return self.ca + self.ce


@dataclass(frozen=True)
class Coupling:
    """The statistics over the declared components.

    *dependencies* counts the directed dependency pairs with a declared component at either end, so
    one connector that adds one pair adds one; it is the sum of ``Ce`` when no stub depends on a
    declared component, and half the sum of ``Ct`` when no stub is linked at all. *hubs* run from the
    highest ``Ct``, ties by name; *isolated* by name. The trimmed figures read the components that are
    neither; :attr:`trimmed_over_all` says hubs and isolated are both unavailable, so they read every
    component. *stubs_depended_on* gives the stubs most depended on, with how many declared components
    depend on each, from the most, ties by name. *not_measured_by_reason* gives the connectors, and
    pairs of ends, that give no dependency, by reason; :attr:`links_not_measured` is their sum.
    """

    n: int
    dependencies: int
    mean: Fraction
    variance: Fraction
    hubs: tuple[str, ...] | NotAvailable
    isolated: tuple[str, ...] | NotAvailable
    trimmed_mean: Fraction
    trimmed_max: int
    external_stubs: int
    stubs_depended_on: tuple[tuple[str, int], ...]
    not_measured_by_reason: tuple[tuple[str, int], ...] = ()

    @property
    def trimmed_over_all(self) -> bool:
        """Hubs and isolated are unavailable, so the trimmed figures read every component."""
        return isinstance(self.hubs, NotAvailable)

    @property
    def links_not_measured(self) -> int:
        return sum(count for _, count in self.not_measured_by_reason)


@dataclass(frozen=True)
class MemberRow:
    """One block of a composition table. *kind* is ``component``, ``composition`` or ``stub``.

    *name* is the member's; *type* is its type's name, so the row can be matched with the stub or
    component it places.
    """

    name: str
    kind: str
    ca: int
    ce: int
    type: str

    @property
    def ct(self) -> int:
        """``Ca + Ce`` among the table's members."""
        return self.ca + self.ce


@dataclass(frozen=True)
class CompositionTable:
    """The coupling among one composition's direct members; *links* are its directed pairs of members."""

    composition: str
    rows: tuple[MemberRow, ...] | NotAvailable
    links: int


@dataclass(frozen=True)
class Cohesion:
    """The cohesion over the declared components that have a figure.

    *mean* is over the *components* with a figure. *below* holds ``(name, figure)`` under
    :data:`COHESION_THRESHOLD`, the lowest first, ties by name. *not_available* holds ``(reason,
    count)`` for each reason that has a component, in :data:`COHESION_REASONS`' order. *runnables_used*
    are the measured runnables of the components with a figure, out of every declared component's
    *runnables*.
    """

    components: int
    mean: Fraction
    below: tuple[tuple[str, Fraction], ...]
    not_available: tuple[tuple[str, int], ...]
    runnables: int
    runnables_used: int


@dataclass(frozen=True)
class Integrity:
    """What ``check-model`` finds, and the ports nothing connects.

    A *by_family* tuple holds ``(family, count)``, the family being the text before the first ``.`` of
    a rule, from the highest count, ties by name; *by_rule* holds ``(rule, count)`` by rule. *ports* is
    the declared components' ports, a stub's left out because a stub shows only what is connected;
    *unconnected* names ``component.port``, sorted.
    """

    errors: int
    warnings: int
    errors_by_family: tuple[tuple[str, int], ...]
    warnings_by_family: tuple[tuple[str, int], ...]
    by_rule: tuple[tuple[str, int], ...]
    ports: int
    unconnected: tuple[str, ...]


@dataclass(frozen=True)
class AsilCoverage:
    """The declared components' safety levels; *inherited* have none of their own and rate above QM."""

    components: int
    declared: int
    inherited: int
    neither: tuple[str, ...]


@dataclass(frozen=True)
class RunnableCoverage:
    """The declared components with at least one runnable, and those without."""

    components: int
    with_runnable: int
    without: tuple[str, ...]


@dataclass(frozen=True)
class TaskCoverage:
    """The runnables on a task, over every runnable a task should hold.

    *runnables* leaves out the *server* ones, counted apart because no task is their legal state: a
    direct call runs in its callers' tasks. *not_on_task* names ``component.runnable``, sorted.
    """

    runnables: int
    on_task: int
    server: int
    not_on_task: tuple[str, ...]


@dataclass(frozen=True)
class RequirementCoverage:
    """The requirements allocated and the components linked to one.

    *requirements* and *allocated* leave out the *stubs*, counted apart, for which allocation has no
    answer. *linked* counts the declared components with a link of their own or on a member to a
    declared requirement, a stub requirement included; a link naming an id no file declares counts for none.
    """

    requirements: int
    allocated: int
    stubs: int
    unallocated: tuple[str, ...]
    components: int
    linked: int
    unlinked: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    """The four coverages, over the declared components, each ``n/a`` by its own reason."""

    asil: AsilCoverage | NotAvailable
    runnable: RunnableCoverage | NotAvailable
    task: TaskCoverage | NotAvailable
    requirement: RequirementCoverage | NotAvailable


@dataclass(frozen=True)
class ArchitectureState:
    """Every figure of one side, the working tree or a revision; *findings* are ``check-model``'s, errors first."""

    label: str
    size: Size
    coupling: Coupling | NotAvailable
    integrity: Integrity
    coverage: Coverage
    cohesion: Cohesion | NotAvailable
    components: tuple[ComponentRow, ...]
    composition_tables: tuple[CompositionTable, ...]
    findings: tuple[ConsistencyIssue, ...] = ()
    #: The connectors placed in no table, by :data:`NOT_PLACED_REASONS`, a reason with no count left out.
    links_not_placed_by_reason: tuple[tuple[str, int], ...] = ()

    @property
    def links_not_placed(self) -> int:
        return sum(count for _, count in self.links_not_placed_by_reason)


def _declared_names(state: ModelState) -> frozenset[str]:
    """The names a declared component file holds: a stub of one of these names is no stub."""
    return frozenset(swc.name for swc in state.layers.get("swc", []) if not swc.external)


def _measured_state(state: ModelState) -> ModelState:
    """*state* as every figure below :class:`Size` reads it: one component file per name.

    The file is the name's last declared one, or its last stub when no declared file holds the name,
    so every computed value asked of the result, ``effective_level``'s first file and
    ``connection_ends``' last alike, reads that one file.
    """
    files: Sequence[SWComponent] = state.layers.get("swc", [])
    chosen: dict[str, SWComponent] = {}
    for swc in files:
        held = chosen.get(swc.name)
        if held is None or held.external or not swc.external:
            chosen[swc.name] = swc
    if len(chosen) == len(files):
        return state
    return ModelState({**state.layers, "swc": list(chosen.values())}, state.placement, state.absent)


def _components(state: ModelState) -> dict[str, SWComponent]:
    """The components by name, last declaration winning, as ``connection_ends`` resolves them."""
    return {swc.name: swc for swc in state.layers.get("swc", [])}


def _population(components: Mapping[str, SWComponent]) -> dict[str, SWComponent]:
    """The declared components: a stub is a neighbour and not a member."""
    return {name: swc for name, swc in components.items() if not swc.external}


def _compositions(state: ModelState) -> dict[str, Composition]:
    """The compositions by name, first declaration winning, as ``connection_ends`` resolves them."""
    compositions: dict[str, Composition] = {}
    for composition in state.layers.get("compositions", []):
        compositions.setdefault(composition.name, composition)
    return compositions


def _cycle_groups(children: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """The strongly connected groups of *children*'s graph, a group's every reachable group before it.

    Tarjan's algorithm without recursion, so a long chain reaches no recursion limit; a name outside a
    cycle is a group of one.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    groups: list[list[str]] = []
    for root in sorted(children):
        if root in index:
            continue
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(children[root]))]
        while work:
            node, pending = work[-1]
            descended = False
            for child in pending:
                if child not in index:
                    index[child] = low[child] = len(index)
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(children[child])))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                group: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    group.append(member)
                    if member == node:
                        break
                groups.append(group)
    return groups


def _nesting_depth(compositions: Mapping[str, Composition]) -> int:
    """The longest chain of compositions placed in one another, a composition holding no other one 1.

    Compositions that hold one another through a cycle (a check-model error) form one group, which counts as
    many levels as it holds compositions; the figure is then the deepest chain of groups so weighed. That bounds
    the cost by the model's size, and equals the longest chain whenever a cycle's compositions form one ring or
    a pair.
    """
    children = {
        name: sorted(
            {
                member.composition
                for member in composition.members
                if member.composition is not None and member.composition in compositions
            }
        )
        for name, composition in compositions.items()
    }
    depth: dict[str, int] = {}
    for group in _cycle_groups(children):
        below = [depth[child] for name in group for child in children[name] if child in depth]
        level = len(group) + max(below, default=0)
        depth.update(dict.fromkeys(group, level))
    return max(depth.values(), default=0)


def measure_size(state: ModelState, registry: ModelRegistry) -> Size:
    """The counts of *state*, over files; a stub file whose name a declared file holds counts as a component."""
    layers = state.layers
    components: Sequence[SWComponent] = layers.get("swc", [])
    compositions: Sequence[Composition] = layers.get("compositions", [])
    declared = _declared_names(state)
    stub_files = [swc for swc in components if swc.name not in declared]
    stubs = len(stub_files)
    stub_ports = sum(len(swc.ports) for swc in stub_files)
    component_ports = sum(len(swc.ports) for swc in components) - stub_ports
    boundary_ports = sum(len(composition.ports) for composition in compositions)
    return Size(
        layers=tuple((name, len(layers.get(name, ()))) for name in registry.display_order),
        components=len(components) - stubs,
        external_stubs=stubs,
        compositions=len(compositions),
        nesting_depth=_nesting_depth(_compositions(state)),
        runnables=sum(len(swc.runnables) for swc in components),
        connectors=len(layers.get("connectors", ())),
        delegations=sum(len(composition.delegations) for composition in compositions),
        component_ports=component_ports,
        stub_ports=stub_ports,
        boundary_ports=boundary_ports,
    )


def _port_directions(swc: SWComponent) -> dict[str, str]:
    """*swc*'s port directions by name, the first of a repeated name, as ``component_ends`` finds it."""
    directions: dict[str, str] = {}
    for port in swc.ports:
        directions.setdefault(port.name, port.direction)
    return directions


def measure_links(state: ModelState) -> Links:
    """Every dependency the connectors state between atomic components.

    The order of the checks is the one the composition tables keep: a pair that resolves, then the
    self-link, then the direction. A self-link and a pair of two stubs draw no link and are counted
    nowhere here: the first is no external dependency, the second touches no coupling figure.
    """
    state = _measured_state(state)
    declared = [connector.name for connector in state.layers.get("connectors", [])]
    names = list(dict.fromkeys(declared))
    rows = derive_rows([{"element": name} for name in names], ["connection_ends"], state)
    answered = {row["element"] for row in rows}
    repeated = len(declared) - len(names)
    unreached = len(names) - len(answered)
    one_direction = 0

    components = _components(state)
    directions = {name: _port_directions(swc) for name, swc in components.items()}
    dependencies: set[tuple[str, str]] = set()
    touched = {
        end.swc
        for connector in state.layers.get("connectors", [])
        for end in (connector.from_, connector.to)
        if end.swc is not None
    }
    for row in rows:
        ends = row["derived"]["connection_ends"]
        from_end, to_end = ends["provider"], ends["requester"]
        from_swc, to_swc = from_end["swc"], to_end["swc"]
        touched |= {from_swc, to_swc}
        if from_swc == to_swc or (components[from_swc].external and components[to_swc].external):
            continue
        from_direction = directions[from_swc][from_end["port"]]
        to_direction = directions[to_swc][to_end["port"]]
        if from_direction == to_direction:
            one_direction += 1
        elif from_direction == "provide":
            dependencies.add((to_swc, from_swc))
        else:
            dependencies.add((from_swc, to_swc))
    counts = dict(zip(NOT_MEASURED_REASONS, (unreached, one_direction, repeated), strict=True))
    return Links(
        frozenset(dependencies),
        frozenset(touched),
        tuple((reason, count) for reason, count in counts.items() if count),
    )


def top_components(rows: Iterable[ComponentRow]) -> tuple[ComponentRow, ...]:
    """The rows of the top list: the highest ``Ct`` first, ties by name."""
    return tuple(sorted(rows, key=lambda row: (-row.ct, row.name))[:_TOP_COMPONENTS])


def _statistics(
    rows: Sequence[ComponentRow],
    stubs: int,
    stubs_depended_on: tuple[tuple[str, int], ...],
    links: Links,
    dependencies: int,
) -> Coupling:
    """The statistics over a non-empty population, every verdict decided without a root."""
    n = len(rows)
    mean = Fraction(sum(row.ct for row in rows), n)
    variance = sum(((row.ct - mean) ** 2 for row in rows), Fraction(0)) / n
    # `x > mean + 2 deviations` is `x > mean and (x - mean)^2 > 4 variance`; the lower side likewise.
    four_variances = 4 * variance
    high = [row for row in rows if row.ct > mean and (row.ct - mean) ** 2 > four_variances]
    low = [row for row in rows if row.ct < mean and (mean - row.ct) ** 2 > four_variances]

    hubs: tuple[str, ...] | NotAvailable
    isolated: tuple[str, ...] | NotAvailable
    if n < _FEWEST_FOR_OUTLIERS:
        hubs = isolated = NotAvailable(TOO_FEW_FOR_OUTLIERS)
        outliers: set[str] = set()
    else:
        hubs = tuple(row.name for row in sorted(high, key=lambda row: (-row.ct, row.name)))
        outliers = set(hubs)
        if mean**2 < four_variances:
            isolated = NotAvailable(LOWER_THRESHOLD_BELOW_ZERO)
        else:
            isolated = tuple(sorted(row.name for row in low))
            outliers |= set(isolated)

    # At most a quarter of a population lies two deviations out, so the rest is never empty.
    rest = [row.ct for row in rows if row.name not in outliers]
    return Coupling(
        n=n,
        dependencies=dependencies,
        mean=mean,
        variance=variance,
        hubs=hubs,
        isolated=isolated,
        trimmed_mean=Fraction(sum(rest), len(rest)),
        trimmed_max=max(rest),
        external_stubs=stubs,
        stubs_depended_on=stubs_depended_on,
        not_measured_by_reason=links.not_measured_by_reason,
    )


def _no_link(swc: SWComponent, unconnected: int, touched: frozenset[str]) -> str:
    """The fact of a component with ``Ct = 0``, the first of :data:`NO_LINK_WORDS` that holds.

    With every port connected, a component a connector reaches had each of its links discarded, as a
    self-link or a pair of one direction; one no connector reaches is connected only by delegation.
    """
    if not swc.ports:
        return NO_PORT
    if unconnected:
        return INCOMPLETE
    return CONNECTED_UNMEASURED if swc.name in touched else DELEGATED_TO_NO_ONE


def measure_coupling(
    state: ModelState, unconnected: Mapping[str, Sequence[str]] | None = None
) -> tuple[tuple[ComponentRow, ...], Coupling | NotAvailable]:
    """Each declared component's row, by name, and the statistics over them.

    *unconnected* is :func:`_unconnected_ports`' answer for *state*, computed here when not given.
    """
    state = _measured_state(state)
    components = _components(state)
    population = _population(components)
    links = measure_links(state)
    if unconnected is None:
        unconnected = _unconnected_ports(state, population)
    dependents: defaultdict[str, set[str]] = defaultdict(set)
    needs: defaultdict[str, set[str]] = defaultdict(set)
    stub_dependents: defaultdict[str, set[str]] = defaultdict(set)
    for dependent, dependency in links.dependencies:
        needs[dependent].add(dependency)
        dependents[dependency].add(dependent)
        if dependent in population and dependency not in population:
            stub_dependents[dependency].add(dependent)

    cohesion = {name: component_cohesion(swc) for name, swc in population.items()}
    rows = tuple(
        ComponentRow(
            name=name,
            type=population[name].type,
            ca=len(dependents[name]),
            ce=len(needs[name]),
            external_neighbours=len((dependents[name] | needs[name]) - population.keys()),
            unconnected_ports=len(unconnected[name]),
            cohesion=cohesion[name][0],
            measured_runnables=cohesion[name][1],
            no_link=(
                None
                if dependents[name] or needs[name]
                else _no_link(population[name], len(unconnected[name]), links.touched)
            ),
        )
        for name in sorted(population)
    )
    if not rows:
        return rows, NotAvailable(NO_COMPONENT)
    most_depended_on = sorted(stub_dependents.items(), key=lambda item: (-len(item[1]), item[0]))
    return rows, _statistics(
        rows,
        len(components) - len(population),
        tuple((name, len(needing)) for name, needing in most_depended_on[:_TOP_STUBS]),
        links,
        sum(1 for pair in links.dependencies if pair[0] in population or pair[1] in population),
    )


def _touched(runnable: Runnable) -> frozenset[tuple[str, str]]:
    """``touched(R)``: what *runnable* declares it uses, each item tagged ``port`` or ``variable``.

    The ports its accesses name and the port of an event that starts it (an operation event's, a
    data-received event's and a call-returned event's), plus the inter-runnable variables it reads or writes.
    The tag keeps a port and a variable of one name apart, so they never count as shared. The event's port
    counts because the runnable it starts works on that port whether or not it names the port in an access.
    The variable is taken as the access names it: whether the component declares it is ``check-model``'s.
    """
    accesses: tuple[DataReadRef | DataWriteRef | OperationCallRef, ...] = (
        *runnable.reads,
        *runnable.writes,
        *runnable.calls,
    )
    starting = (OperationEvent, DataEvent, CallReturnedEvent)
    started = (event.port for event in runnable.events if isinstance(event, starting))
    ports = (("port", port) for port in (*(access.port for access in accesses), *started))
    variable_accesses: tuple[VariableReadRef | VariableWriteRef, ...] = (
        *runnable.reads_variables,
        *runnable.writes_variables,
    )
    variables = (("variable", access.variable) for access in variable_accesses)
    return frozenset((*ports, *variables))


def component_cohesion(swc: SWComponent) -> tuple[Fraction | NotAvailable, int]:
    """*swc*'s cohesion and its count of measured runnables.

    The figure is the share of pairs of measured runnables that share a port or a variable. A runnable
    that declares no access, to a port or to a variable, says nothing about what it touches, so it is
    left out of the pairs; it is never read as sharing nothing. One measured runnable gives ``1`` only
    when it is the component's only runnable.
    """
    if not swc.runnables:
        return NotAvailable(NO_RUNNABLE_OF_ITS_OWN), 0
    measured = [touched for touched in map(_touched, swc.runnables) if touched]
    if not measured:
        return NotAvailable(NO_ACCESS), 0
    if len(measured) == 1:
        if len(swc.runnables) == 1:
            return Fraction(1), 1
        return NotAvailable(ONE_ACCESSING_RUNNABLE), 1
    if len(frozenset().union(*measured)) == 1:
        return NotAvailable(ONE_PORT_OR_VARIABLE), len(measured)
    # The pairs are counted as they are made, never held: a component of n runnables has n(n-1)/2.
    shared = sum(1 for left, right in combinations(measured, 2) if left & right)
    return Fraction(shared, math.comb(len(measured), 2)), len(measured)


def measure_cohesion(rows: Sequence[ComponentRow], runnables: int) -> Cohesion | NotAvailable:
    """The cohesion block over the declared components' *rows*, of *runnables* runnables in all.

    It is ``n/a`` when no runnable declares an access, and when some do but no component has a figure,
    each with its own reason.
    """
    if not any(row.measured_runnables for row in rows):
        return NotAvailable(NO_COMPONENT_ACCESS)
    figures = {row.name: row.cohesion for row in rows if isinstance(row.cohesion, Fraction)}
    if not figures:
        return NotAvailable(NO_COHESION_FIGURE)
    reasons = Counter(row.cohesion.reason for row in rows if isinstance(row.cohesion, NotAvailable))
    below = sorted(
        ((name, figure) for name, figure in figures.items() if figure < COHESION_THRESHOLD),
        key=lambda item: (item[1], item[0]),
    )
    return Cohesion(
        components=len(figures),
        mean=sum(figures.values(), Fraction(0)) / len(figures),
        below=tuple(below),
        not_available=tuple((reason, reasons[reason]) for reason in COHESION_REASONS if reasons[reason]),
        runnables=runnables,
        runnables_used=sum(row.measured_runnables for row in rows if row.name in figures),
    )


def _unconnected_ports(state: ModelState, population: Mapping[str, SWComponent] | None = None) -> dict[str, list[str]]:
    """Each declared component's unconnected ports, by the ``unconnected`` computed value.

    *population* is *state*'s declared components, computed here when not given.
    """
    state = _measured_state(state)
    if population is None:
        population = _population(_components(state))
    rows = [
        {"element": name, "member": port}
        for name, swc in sorted(population.items())
        for port in dict.fromkeys(port.name for port in swc.ports)
    ]
    unconnected: dict[str, list[str]] = {name: [] for name in population}
    for row in derive_rows(rows, ["unconnected"], state):
        unconnected[row["element"]].append(row["member"])
    return unconnected


def _by_family(findings: Iterable[ConsistencyIssue]) -> tuple[tuple[str, int], ...]:
    """``(family, count)`` from the highest count, ties by name."""
    counts = Counter(finding.rule.split(".", 1)[0] for finding in findings)
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def measure_integrity(
    state: ModelState,
    findings: Sequence[ConsistencyIssue],
    unconnected: Mapping[str, Sequence[str]] | None = None,
    population: Mapping[str, SWComponent] | None = None,
) -> Integrity:
    """*findings* counted, and the unconnected ports of the declared components.

    *unconnected* is :func:`_unconnected_ports`' answer for *state* and *population* its declared
    components, each computed here when not given.
    """
    state = _measured_state(state)
    errors = [finding for finding in findings if finding.severity == SEVERITY_ERROR]
    warnings = [finding for finding in findings if finding.severity != SEVERITY_ERROR]
    if population is None:
        population = _population(_components(state))
    if unconnected is None:
        unconnected = _unconnected_ports(state, population)
    return Integrity(
        errors=len(errors),
        warnings=len(warnings),
        errors_by_family=_by_family(errors),
        warnings_by_family=_by_family(warnings),
        by_rule=tuple(sorted(Counter(finding.rule for finding in findings).items())),
        ports=sum(len({port.name for port in swc.ports}) for swc in population.values()),
        unconnected=tuple(sorted(f"{name}.{port}" for name, ports in unconnected.items() for port in ports)),
    )


def _asil_coverage(state: ModelState, population: Mapping[str, SWComponent]) -> AsilCoverage | NotAvailable:
    """Declared, inherited above QM, or neither; the rating is ``effective_level``'s, so ``QM(D)`` is QM.

    *state* is :func:`_measured_state`'s, so a name is read off its one file, the one ``effective_level``
    rates too: no other file of a name two files share, and no stub, states a level here.
    """
    requirements: Sequence[Requirement] = state.layers.get("requirements", [])
    declared = {name for name, swc in population.items() if swc.asil is not None}
    if not declared and all(requirement.asil is None for requirement in requirements):
        return NotAvailable(NO_SAFETY_LEVEL)
    silent = [{"element": name} for name in sorted(population) if name not in declared]
    rated = derive_rows(silent, ["effective_level"], state)
    neither = [row["element"] for row in rated if row["derived"]["effective_level"]["rating"] == "QM"]
    return AsilCoverage(len(population), len(declared), len(silent) - len(neither), tuple(neither))


def _runnable_coverage(population: Mapping[str, SWComponent]) -> RunnableCoverage | NotAvailable:
    without = tuple(sorted(name for name, swc in population.items() if not swc.runnables))
    if len(without) == len(population):
        return NotAvailable(NO_RUNNABLE)
    return RunnableCoverage(len(population), len(population) - len(without), without)


def _task_coverage(state: ModelState, population: Mapping[str, SWComponent]) -> TaskCoverage | NotAvailable:
    """The runnables on a task. An entry that resolves to no type covers nothing."""
    tasks = state.layers.get("tasks", [])
    if not tasks:
        return NotAvailable(NO_TASK)
    if not any(swc.runnables for swc in population.values()):
        return NotAvailable(NO_RUNNABLE)
    identities = identities_from(list(state.layers.get("compositions", [])))
    covered = {
        (identities.type_of(entry.swc, entry.instance), entry.runnable) for task in tasks for entry in task.runnables
    }
    server = on_task = 0
    missing: list[str] = []
    for name, swc in sorted(population.items()):
        for runnable in swc.runnables:
            if runnable_kind(runnable) == "server":
                server += 1
            elif (name, runnable.name) in covered:
                on_task += 1
            else:
                missing.append(f"{name}.{runnable.name}")
    return TaskCoverage(on_task + len(missing), on_task, server, tuple(sorted(missing)))


def _requirement_coverage(
    state: ModelState, population: Mapping[str, SWComponent]
) -> RequirementCoverage | NotAvailable:
    """The ``allocation`` and ``realised_by`` computed values, keyed by requirement id."""
    requirements: Sequence[Requirement] = state.layers.get("requirements", [])
    if not requirements:
        return NotAvailable(NO_REQUIREMENT)
    rows = [{"element": identifier} for identifier in sorted({requirement.id for requirement in requirements})]
    answers = {
        row["element"]: row["derived"]["allocation"]["allocated"] for row in derive_rows(rows, ["allocation"], state)
    }
    allocated = [identifier for identifier, answer in answers.items() if answer is True]
    unallocated = tuple(identifier for identifier, answer in answers.items() if answer is False)
    linked = {
        site["element"]
        for site in (row["derived"]["realised_by"] for row in derive_rows(rows, ["realised_by"], state))
        if site["layer"] == "swc"
    } & population.keys()
    return RequirementCoverage(
        requirements=len(allocated) + len(unallocated),
        allocated=len(allocated),
        stubs=len(answers) - len(allocated) - len(unallocated),
        unallocated=unallocated,
        components=len(population),
        linked=len(linked),
        unlinked=tuple(sorted(population.keys() - linked)),
    )


def measure_coverage(state: ModelState, population: Mapping[str, SWComponent] | None = None) -> Coverage:
    """The four coverages of *state*; *population* is its declared components, computed here when not given."""
    state = _measured_state(state)
    if population is None:
        population = _population(_components(state))
    return Coverage(
        asil=_asil_coverage(state, population),
        runnable=_runnable_coverage(population),
        task=_task_coverage(state, population),
        requirement=_requirement_coverage(state, population),
    )


def _single_root(compositions: Mapping[str, Composition]) -> str | None:
    """The root composition's name, or ``None`` when the model has none or several."""
    roots = [composition.name for composition in compositions.values() if composition.root]
    return roots[0] if len(roots) == 1 else None


def _member_key(member: CompositionMember) -> tuple[str, str]:
    """A member's block: its type, because an endpoint names a type and not a prototype."""
    if member.composition is not None:
        return KIND_COMPOSITION, member.composition
    return KIND_COMPONENT, member.type or ""


def _endpoint_key(endpoint: ConnectorEndpoint) -> tuple[str, str]:
    """The block an endpoint names, in :func:`_member_key`'s terms."""
    if endpoint.composition is not None:
        return KIND_COMPOSITION, endpoint.composition
    return KIND_COMPONENT, endpoint.swc or ""


def _endpoint_direction(
    endpoint: ConnectorEndpoint, components: Mapping[str, SWComponent], compositions: Mapping[str, Composition]
) -> str | None:
    """The direction of the port *endpoint* names, a nested box's boundary port included, or ``None``."""
    if endpoint.composition is not None:
        composition = compositions.get(endpoint.composition)
        ports = [] if composition is None else composition.ports
        return next((port.direction for port in ports if port.name == endpoint.port), None)
    swc = components.get(endpoint.swc or "")
    return None if swc is None else _port_directions(swc).get(endpoint.port)


def _member_kind(member: CompositionMember, components: Mapping[str, SWComponent]) -> str:
    """A row's kind. A type no file declares is ``component``, what the member says it is."""
    if member.composition is not None:
        return KIND_COMPOSITION
    swc = components.get(member.type or "")
    return KIND_STUB if swc is not None and swc.external else KIND_COMPONENT


def composition_tables(state: ModelState) -> tuple[tuple[CompositionTable, ...], tuple[tuple[str, int], ...]]:
    """One table per composition type, root first, and the unplaced connectors by reason.

    The tables after the root are by name; the connectors placed in no table are counted by
    :data:`NOT_PLACED_REASONS`, a reason with no count left out.
    A connector is one link at the level it is drawn; no delegation is walked. A connector is skipped
    and counted once, under the first reason that holds: its owner does not resolve, an end is no
    member of the owner, a port does not resolve, or its two ends have one direction. A self-link, one member at both
    ends, is dropped uncounted. *Ct* = 0 here is a fact about the table and nothing more: a member
    wired only through the boundary shows it.
    """
    state = _measured_state(state)
    components = _components(state)
    compositions = _compositions(state)
    root = _single_root(compositions)
    # A repeated type is one block, under its first member's name.
    blocks: dict[str, dict[tuple[str, str], CompositionMember]] = {name: {} for name in compositions}
    for name, composition in compositions.items():
        for member in composition.members:
            blocks[name].setdefault(_member_key(member), member)

    links: defaultdict[str, set[tuple[tuple[str, str], tuple[str, str]]]] = defaultdict(set)
    not_placed: Counter[str] = Counter()
    for connector in state.layers.get("connectors", []):
        owner = connector.composition if connector.composition is not None else root
        members = blocks.get(owner) if owner is not None else None
        from_key, to_key = _endpoint_key(connector.from_), _endpoint_key(connector.to)
        if owner is None or members is None:
            not_placed[UNRESOLVED_OWNER] += 1
            continue
        if from_key not in members or to_key not in members:
            not_placed[END_NOT_A_MEMBER] += 1
            continue
        from_direction = _endpoint_direction(connector.from_, components, compositions)
        to_direction = _endpoint_direction(connector.to, components, compositions)
        if from_direction is None or to_direction is None:
            not_placed[UNRESOLVED_PORT] += 1
        elif from_key == to_key:
            continue
        elif from_direction == to_direction:
            not_placed[ONE_DIRECTION] += 1
        elif from_direction == "provide":
            links[owner].add((to_key, from_key))
        else:
            links[owner].add((from_key, to_key))

    tables = []
    for name in sorted(compositions, key=lambda name: (name != root, name)):
        table_blocks = blocks[name]
        if not table_blocks:
            tables.append(CompositionTable(name, NotAvailable(EMPTY_COMPOSITION), 0))
            continue
        pairs = links[name]
        rows = [
            MemberRow(
                name=member.name,
                kind=_member_kind(member, components),
                ca=sum(1 for _, dependency in pairs if dependency == key),
                ce=sum(1 for dependent, _ in pairs if dependent == key),
                type=key[1],
            )
            for key, member in table_blocks.items()
        ]
        tables.append(CompositionTable(name, tuple(sorted(rows, key=lambda row: (-row.ct, row.name))), len(pairs)))
    return tuple(tables), tuple((reason, not_placed[reason]) for reason in NOT_PLACED_REASONS if not_placed[reason])


def measure(
    state: ModelState, root: str, label: str, registry: ModelRegistry, arxml_version: ArxmlVersion | None = None
) -> ArchitectureState:
    """Every figure of one side; *state* is the model loaded from *root*, which ``check-model`` reads.

    *arxml_version* is the project's manifest version, the one ``check-model`` judges ``[constr_2006]`` at.
    """
    measured = _measured_state(state)
    population = _population(_components(measured))
    unconnected = _unconnected_ports(measured, population)
    components, coupling = measure_coupling(measured, unconnected)
    tables, not_placed = composition_tables(measured)
    findings, _ = all_findings(root, arxml_version)
    return ArchitectureState(
        label=label,
        size=measure_size(state, registry),
        coupling=coupling,
        integrity=measure_integrity(measured, findings, unconnected, population),
        coverage=measure_coverage(measured, population),
        cohesion=measure_cohesion(components, sum(len(swc.runnables) for swc in population.values())),
        components=components,
        composition_tables=tables,
        findings=findings,
        links_not_placed_by_reason=not_placed,
    )


# ---------------------------------------------------------------------------
# Movement against a base revision
# ---------------------------------------------------------------------------

#: Where a list of *what to look at* stops; the rest is counted.
LOOK_AT_CAP = 10
#: The rises in ``Ct`` *what to look at* names.
_TOP_RISES = 5


@dataclass(frozen=True)
class MovementFigure:
    """One figure at every column of the series, the base first and the working tree last.

    *change* runs from the base to the working tree, and is ``None`` when the figure is ``n/a`` at
    either of the two; *not_available_at* names those sides by their labels.
    """

    figure: str
    values: tuple[int | Fraction | NotAvailable, ...]
    change: int | Fraction | None
    not_available_at: tuple[str, ...]


@dataclass(frozen=True)
class CtChange:
    """One declared component whose ``Ct`` moved; ``None`` on a side that does not declare it."""

    name: str
    before: int | None
    after: int | None

    @property
    def change(self) -> int:
        """The move, a side that does not declare the component counting 0."""
        return (self.after or 0) - (self.before or 0)


@dataclass(frozen=True)
class Movement:
    """The working tree against the base, the first revision named.

    *ct_changes* holds every component whose ``Ct`` moved, added and removed ones included, from the
    largest rise to the largest fall, ties by name. *introduced* and *resolved* are the multiset
    comparison of ``check-model``'s findings in its two directions, each errors first; *still_open*
    counts the base's findings the working tree still has.
    """

    base: str
    current: str
    figures: tuple[MovementFigure, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    ct_changes: tuple[CtChange, ...]
    hubs_gained: tuple[str, ...] | NotAvailable
    hubs_lost: tuple[str, ...] | NotAvailable
    introduced: tuple[ConsistencyIssue, ...]
    resolved: tuple[ConsistencyIssue, ...]
    base_findings: int
    still_open: int

    @property
    def unchanged(self) -> bool:
        """Whether nothing *Movement* follows differs between the base and the working tree.

        No figure of :data:`_MOVEMENT_FIGURES` differs at any column, and no component, no ``Ct`` and
        no finding in either direction differs between the two ends.

        What this does not reach: everything *Movement* does not follow -- the other size rows (the
        per-layer counts, the delegations, the nesting depth), the composition tables, which components
        are isolated, the unconnected ports and the names behind a coverage count -- and, at a column
        between the two ends, anything but the figures.
        """
        return all(value == figure.values[0] for figure in self.figures for value in figure.values) and not (
            self.added or self.removed or self.ct_changes or self.introduced or self.resolved
        )


def _or_na(
    block: _B | NotAvailable, read: Callable[[_B], int | Fraction | NotAvailable]
) -> int | Fraction | NotAvailable:
    """*read* of *block*, or *block* itself where it is ``n/a``."""
    return block if isinstance(block, NotAvailable) else read(block)


#: The figures *Movement* follows, by name, in the state block's order.
_MOVEMENT_FIGURES: tuple[tuple[str, Callable[[ArchitectureState], int | Fraction | NotAvailable]], ...] = (
    ("Components", lambda side: side.size.components),
    ("External stubs", lambda side: side.size.external_stubs),
    ("Compositions", lambda side: side.size.compositions),
    ("Ports", lambda side: side.size.ports),
    ("Connectors", lambda side: side.size.connectors),
    ("Runnables", lambda side: side.size.runnables),
    ("Dependencies", lambda side: _or_na(side.coupling, lambda c: c.dependencies)),
    ("Mean Ct", lambda side: _or_na(side.coupling, lambda c: c.mean)),
    ("Hubs", lambda side: _or_na(side.coupling, lambda c: _or_na(c.hubs, len))),
    ("Mean cohesion", lambda side: _or_na(side.cohesion, lambda c: c.mean)),
    ("Cohesion below 0.5 (not calibrated)", lambda side: _or_na(side.cohesion, lambda c: len(c.below))),
    ("Components declaring a safety level", lambda side: _or_na(side.coverage.asil, lambda c: c.declared)),
    ("Components with a runnable", lambda side: _or_na(side.coverage.runnable, lambda c: c.with_runnable)),
    ("Runnables on a task", lambda side: _or_na(side.coverage.task, lambda c: c.on_task)),
    ("Requirements allocated", lambda side: _or_na(side.coverage.requirement, lambda c: c.allocated)),
    ("Components with a requirement link", lambda side: _or_na(side.coverage.requirement, lambda c: c.linked)),
    ("Errors", lambda side: side.integrity.errors),
    ("Warnings", lambda side: side.integrity.warnings),
)


def _figure(
    name: str, read: Callable[[ArchitectureState], int | Fraction | NotAvailable], series: Sequence[ArchitectureState]
) -> MovementFigure:
    values = tuple(read(side) for side in series)
    ends = ((series[0].label, values[0]), (series[-1].label, values[-1]))
    missing = tuple(label for label, value in ends if isinstance(value, NotAvailable))
    first, last = values[0], values[-1]
    change = None if isinstance(first, NotAvailable) or isinstance(last, NotAvailable) else last - first
    return MovementFigure(name, values, change, missing)


def _hubs(side: ArchitectureState) -> tuple[str, ...] | NotAvailable:
    coupling = side.coupling
    if isinstance(coupling, NotAvailable) or isinstance(coupling.hubs, NotAvailable):
        return NotAvailable(f"hubs are n/a at {side.label}")
    return coupling.hubs


def compare(series: Sequence[ArchitectureState]) -> Movement:
    """The movement over *series*: every figure per column, and the working tree, last, against the base, first."""
    base, current = series[0], series[-1]
    before = {row.name: row.ct for row in base.components}
    after = {row.name: row.ct for row in current.components}
    changes = [CtChange(name, before.get(name), after.get(name)) for name in sorted(before.keys() | after.keys())]
    old_hubs, new_hubs = _hubs(base), _hubs(current)
    gained: tuple[str, ...] | NotAvailable
    lost: tuple[str, ...] | NotAvailable
    if isinstance(old_hubs, NotAvailable) or isinstance(new_hubs, NotAvailable):
        gained = lost = old_hubs if isinstance(old_hubs, NotAvailable) else new_hubs
    else:
        gained = tuple(name for name in new_hubs if name not in old_hubs)
        lost = tuple(name for name in old_hubs if name not in new_hubs)
    resolved = introduced_findings(current.findings, base.findings)
    return Movement(
        base=base.label,
        current=current.label,
        figures=tuple(_figure(name, read, series) for name, read in _MOVEMENT_FIGURES),
        added=tuple(sorted(after.keys() - before.keys())),
        removed=tuple(sorted(before.keys() - after.keys())),
        ct_changes=tuple(
            sorted((change for change in changes if change.change), key=lambda change: (-change.change, change.name))
        ),
        hubs_gained=gained,
        hubs_lost=lost,
        introduced=introduced_findings(base.findings, current.findings),
        resolved=resolved,
        base_findings=len(base.findings),
        still_open=len(base.findings) - len(resolved),
    )


# ---------------------------------------------------------------------------
# What to look at
# ---------------------------------------------------------------------------

LOOK_ERRORS = "errors"
LOOK_HUBS = "hubs"
LOOK_NO_LINK = "no link"
LOOK_ISOLATED = "isolated"
LOOK_LOW_COHESION = "low cohesion"
LOOK_UNSCHEDULED = "unscheduled"
LOOK_NEW_HUBS = "new hubs"
LOOK_ROSE = "rose most"
LOOK_INTRODUCED = "introduced"
#: The lines in their fixed order, which is no ranking.
LOOK_AT_KEYS = (
    LOOK_ERRORS,
    LOOK_HUBS,
    LOOK_NO_LINK,
    LOOK_ISOLATED,
    LOOK_LOW_COHESION,
    LOOK_UNSCHEDULED,
    LOOK_NEW_HUBS,
    LOOK_ROSE,
    LOOK_INTRODUCED,
)


@dataclass(frozen=True)
class LookAtEntry:
    """One name on a line, with what the line states about it.

    *figure* is a count of errors for a family, a ``Ct``, or a cohesion; *before* is the base's
    ``Ct`` of a rise; *word* and *unconnected* are a component's ``Ct = 0`` fact; *message* is an
    introduced finding's, whose *name* is its rule.
    """

    name: str
    figure: int | Fraction | None = None
    before: int | None = None
    word: str | None = None
    unconnected: int = 0
    message: str | None = None


@dataclass(frozen=True)
class LookAtLine:
    """One line of *what to look at*: its key, its first :data:`LOOK_AT_CAP` entries, and how many more."""

    key: str
    entries: tuple[LookAtEntry, ...]
    more: int


def _line(key: str, entries: Sequence[LookAtEntry], cap: int = LOOK_AT_CAP) -> LookAtLine:
    return LookAtLine(key, tuple(entries[:cap]), max(len(entries) - cap, 0))


def look_at(current: ArchitectureState, movement: Movement | None) -> tuple[LookAtLine, ...]:
    """The facts that name a place, in :data:`LOOK_AT_KEYS`' order, each line present only with content."""
    ct = {row.name: row.ct for row in current.components}
    lines: list[LookAtLine] = []
    families = [LookAtEntry(family, count) for family, count in current.integrity.errors_by_family]
    lines.append(_line(LOOK_ERRORS, families))
    coupling = current.coupling
    if isinstance(coupling, Coupling) and not isinstance(coupling.hubs, NotAvailable):
        lines.append(_line(LOOK_HUBS, [LookAtEntry(name, ct[name]) for name in coupling.hubs]))
    no_link = [
        LookAtEntry(row.name, word=row.no_link, unconnected=row.unconnected_ports)
        for row in current.components
        if row.no_link is not None
    ]
    lines.append(_line(LOOK_NO_LINK, no_link))
    if isinstance(coupling, Coupling) and not isinstance(coupling.isolated, NotAvailable):
        lines.append(_line(LOOK_ISOLATED, [LookAtEntry(name, ct[name]) for name in coupling.isolated]))
    if isinstance(current.cohesion, Cohesion):
        lines.append(_line(LOOK_LOW_COHESION, [LookAtEntry(name, figure) for name, figure in current.cohesion.below]))
    task = current.coverage.task
    if isinstance(task, TaskCoverage):
        lines.append(_line(LOOK_UNSCHEDULED, [LookAtEntry(name) for name in task.not_on_task]))
    if movement is not None:
        if not isinstance(movement.hubs_gained, NotAvailable):
            lines.append(_line(LOOK_NEW_HUBS, [LookAtEntry(name, ct[name]) for name in movement.hubs_gained]))
        rises = [
            LookAtEntry(change.name, change.after, before=change.before)
            for change in movement.ct_changes
            if change.before is not None and change.after is not None and change.change > 0
        ]
        lines.append(_line(LOOK_ROSE, rises, _TOP_RISES))
        introduced = [LookAtEntry(finding.rule, message=finding.message) for finding in movement.introduced]
        lines.append(_line(LOOK_INTRODUCED, introduced))
    return tuple(line for line in lines if line.entries)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricsReport:
    """Every side measured, the movement and *what to look at*.

    *series* runs from the base, the first revision named, to the working tree, last. *movement* is
    ``None`` when no base was read; *no_base* is then why, when a base was asked for by ``since_head``
    and HEAD could not be read, and ``None`` when no base was asked for.
    """

    project_name: str
    series: tuple[ArchitectureState, ...]
    movement: Movement | None
    no_base: str | None
    look_at: tuple[LookAtLine, ...]


#: The causes of an unreadable HEAD, each in a fixed sentence's words: git that could not be started at
#: all, no repository, a repository git refuses to read, no commit, no model, a retired layout, a model
#: file that does not parse, and HEAD's files that could not be put on disk after git gave them; anything
#: else keeps the refusal's shared ``str()``.
HEAD_NO_GIT = "git could not be run"
HEAD_NOT_IN_GIT = "the project is not in a git repository"
HEAD_GIT_REFUSES = "git refuses to read the repository the project is in"
HEAD_NO_COMMIT = "the current branch has no commit yet"
HEAD_NO_MODEL = "HEAD holds no model yet"
HEAD_RETIRED_LAYOUT = "HEAD keeps the model in a retired layout"
HEAD_DOES_NOT_PARSE = "a model file at HEAD does not parse"
HEAD_NOT_UNPACKED = "HEAD's files could not be unpacked"


def _git_did_not_start(exc: BaseException) -> bool:
    """Whether *exc* was raised while ``subprocess`` started git, and not by a later stage of the read.

    A ``FileNotFoundError`` or ``PermissionError`` reaches the HEAD read from git not being there, or
    not executable, and equally from the temporary tree the revision is unpacked into after both git
    calls succeeded; only the frames it was raised through tell them apart.
    """
    return any(frame.f_globals.get("__name__") == "subprocess" for frame, _ in traceback.walk_tb(exc.__traceback__))


#: git's C-locale words for *no repository here*, by release: the standalone sentence and the one for a
#: ``.git`` file naming a missing directory (git up to 2.54), and git 2.56's wording of the latter.
_NO_REPOSITORY_WORDS = ("not a git repository", "gitfile does not point to a valid repository")


def _head_cause(exc: ValueError) -> str:
    """Why HEAD cannot be read, as the no-base sentence says it, naming no location.

    Recognised by the refusal's class and, for git's, by the opening of its sentence and git's own C-locale
    words (:data:`_NO_REPOSITORY_WORDS`): those are no repository, and any other failure to find the repository
    is one git refuses to read (*dubious ownership*, a ``.git`` file it cannot open or parse). A refusal
    recognised by none keeps its shared ``str()``, which names no location either. A read refused before its
    tree was read (``revision`` ``None``) is git not being installed when it was raised while git was started,
    and HEAD's files not being unpacked otherwise.

    What this does not reach: a later git that words the no-repository case otherwise reads as one git refuses
    to read (the report still says why there is no base, by the other sentence).
    """
    if isinstance(exc, SnapshotReadError) and exc.revision is None:
        return HEAD_NO_GIT if _git_did_not_start(exc.cause) else HEAD_NOT_UNPACKED
    if isinstance(exc, RevisionReadError):
        if exc.opening == "not a git repository":
            text = exc.tool_text.lower()
            return HEAD_NOT_IN_GIT if any(words in text for words in _NO_REPOSITORY_WORDS) else HEAD_GIT_REFUSES
        if exc.opening.startswith("git archive failed") and "not a valid object name" in exc.tool_text.lower():
            return HEAD_NO_COMMIT
    if isinstance(exc, SnapshotReadError) and exc.revision is not None and isinstance(exc.cause, FileNotFoundError):
        return HEAD_NO_MODEL
    if isinstance(exc, RetiredStorageError):
        return HEAD_RETIRED_LAYOUT
    if isinstance(exc, RevisionParseError):
        # The cause's shared form names the file by its storage name, never the extraction directory.
        return f"{HEAD_DOES_NOT_PARSE}: {exc.cause}"
    return str(exc)


def compute_metrics(project: SwcraftProject, since: Sequence[str], *, since_head: bool = False) -> MetricsReport:
    """The metrics of the working tree, and its movement against *since*'s revisions, the first the base.

    Every revision is read as ``swcraft diff`` reads one, each side's figures and findings from one read of
    each file. The temporary trees are removed before this returns, whether it returns or raises.

    *since_head* takes HEAD as the base when it can be read; when it cannot, the run goes on with no movement
    and *no_base* holds the cause in :func:`_head_cause`'s words, which name no location; any other ``OSError``
    while HEAD is read is :data:`HEAD_NO_GIT` when git could not be executed, :data:`HEAD_NOT_UNPACKED` when
    the temporary tree could not be written. That is the one read whose refusal is caught: a revision named in
    *since*, ``HEAD`` included, refuses the run, as does a working tree that cannot be read.

    Raises (each a ``ValueError``): the refusals of :func:`~swcraft_tools.change_report.open_side`, and
    *since* given together with *since_head*.
    """
    if since_head and since:
        raise ValueError("a base is named by a revision or by HEAD, never by both")
    registry = current_registry()
    model_root = project.resolved_model_root
    no_base: str | None = None
    measured: list[ArchitectureState] = []
    with contextlib.ExitStack() as stack:
        sides: list[Side] = []
        if since_head:
            try:
                sides.append(open_side(stack, model_root, "HEAD"))
            except ValueError as exc:
                no_base = _head_cause(exc)
            except OSError as exc:
                # Git that is there and cannot be executed, or a temporary tree that may not be written
                # or fills the disk; a model root the process may not enter refuses the working tree's
                # read right after, so it never reaches a report. The errno text is never said.
                no_base = HEAD_NO_GIT if _git_did_not_start(exc) else HEAD_NOT_UNPACKED
        else:
            sides.extend(open_side(stack, model_root, revision) for revision in since)
        # A revision's block opens after its tree is materialized, since no model write happens inside a
        # block; nothing else writes that tree, so its figures and its findings read the same files.
        for side in sides:
            with single_read():
                measured.append(measure(side.state, side.root, side.label, registry, project.resolved_arxml_version))
        # The working tree's block opens BEFORE it is read, which writes nothing, so that the figures and
        # the findings ``check-model`` reads again come from one read of each file: a save while the run
        # is under way cannot pair the old figures with the new findings.
        with single_read():
            current = open_side(stack, model_root, None)
            measured.append(
                measure(current.state, current.root, current.label, registry, project.resolved_arxml_version)
            )
    series = tuple(measured)
    movement = compare(series) if len(series) > 1 else None
    return MetricsReport(project.name, series, movement, no_base, look_at(series[-1], movement))
