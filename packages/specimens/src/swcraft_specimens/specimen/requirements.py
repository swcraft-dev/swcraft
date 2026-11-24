"""The requirement cases and their texts, restated from SWCraft's benchmark workload generator.

The source is named in `PROVENANCE.md`; nothing is imported from SWCraft. Notation: n components,
P ports, Rn = `components.runnables`, N_r = `layers.requirements`, S = `on_all_site_kinds`,
O = `on_one_port`, N = `unallocated`, V = `stale_links`, L_r = `requirements.links`, G = N_r - S - O - N.

Every requirement is one of four cases, in id order: S **spread** ones (linked once on each of the
seven site kinds), O **one-port** ones, N **unallocated** ones (no link), then G **general** ones
(linked on components and runnables only, from the component g mod n onwards). The extra links
X = L_r - (7S + O + G) go to the general requirements first, then to the spread ones; an unstated
L_r is derived, capped at the capacity and at `COUNT_CEILING`. The first V links in enumeration
order are stale: their requirement declares version 2 and those links state `@1`. The spread and
unallocated cases take `REQUIREMENT_TOPICS`' texts, the others a neutral title and description, and
a few components holding no link of a case take the sentence "Takes part in <title in lower case>.".

Refused before generation (`requirement_refusals`, `requirement_ceiling_refusals`), each at its leaf:

- `sum` at `layers.requirements`: S + O + N > N_r (the other requirement refusals are then not
  looked for);
- `requirements` at `requirements.on_all_site_kinds`: S > 0 while a site kind has no element;
  at `requirements.on_one_port`: O > 0 with no port; at `layers.requirements`: G > 0 with n + Rn = 0;
- with none of those three, `requirements` at `requirements.links` (stated): L_r below 7S + O + G or
  above the capacity; at `requirements.stale_links`: V above a stated L_r, or above the capacity
  with L_r unstated. A capacity bound read through an unstated Rn is `derived`;
- `ceiling` at `requirements.links` (7S + O + G above `COUNT_CEILING`) and at `layers.requirements`
  (the description sentences above it).

The sites, links and texts are fixed choices no passport leaf states, so a real model's
requirements match its specimen's in the counts alone.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import GenerationError
from ..model import (
    COMPOSITION_SITE,
    HARDWARE_SITE,
    INTERFACE_SITE,
    LINK_KINDS,
    PORT_SITE,
    RUNNABLE_SITE,
    SWC_SITE,
    TASK_SITE,
    Requirement,
    RequirementLink,
)
from ..passport import Refusal
from ..preset import ASIL
from .common import CEILING_SENTENCE, COUNT_CEILING, stated_count
from .components import build_components, component_refusals
from .compositions import composition_refusals
from .interfaces import interface_refusals, stub_count
from .shape import Shape, specimen_shape
from .tasks import runnable_count
from .vocabulary import (
    CEILING,
    CLIENT_SERVER,
    DERIVED,
    LAYER_REQUIREMENTS,
    LAYER_SWC,
    LAYER_TASKS,
    ON_ALL_SITE_KINDS,
    ON_ONE_PORT,
    REQUIREMENT_LINKS,
    REQUIREMENTS_CLASS,
    RUNNABLES,
    SENDER_RECEIVER,
    STALE_LINKS,
    SUM,
    UNALLOCATED,
)

#: The requirement cases' titles and descriptions, copied verbatim from the workload's
#: `_REQUIREMENT_TOPICS`: case k (the spread ones, then the unallocated ones) takes topic k mod 7.
REQUIREMENT_TOPICS: tuple[tuple[str, str], ...] = (
    (
        "Supply voltage monitoring",
        "The provider components shall monitor the supply voltage and report a low supply voltage within 10 ms.",
    ),
    (
        "Output frame timing",
        "Every output frame shall be published within its period, measured at the sender port.",
    ),
    (
        "Supply voltage recovery",
        "After a low supply voltage the components shall resume normal operation within 50 ms.",
    ),
    (
        "Output voltage range",
        "The published output voltage value shall stay inside its declared range.",
    ),
    (
        "Hub forwarding latency",
        "The hub components shall forward every received signal to the counterpart port of the receiving component.",
    ),
    (
        "Watchdog supervision",
        "Every task shall be supervised by the watchdog; a missed deadline shall be reported.",
    ),
    ("Supply line diagnosis", "A short circuit of the sensor supply line shall be detected."),
)
#: The description of a one-port or general requirement.
NEUTRAL_DESCRIPTION = "A specimen requirement."
#: The components a case's sentence goes to, at most.
SENTENCES_PER_CASE = 3
#: The version a requirement holding a stale link declares.
STALE_VERSION = 2


def neutral_title(requirement_id: str) -> str:
    """The title of a one-port or general requirement."""
    return f"Specimen requirement {requirement_id}"


def sentence(case: int) -> str:
    """The description sentence of case k: "Takes part in <title in lower case>."."""
    return f"Takes part in {REQUIREMENT_TOPICS[case % len(REQUIREMENT_TOPICS)][0].lower()}."


@dataclass(frozen=True)
class RequirementCounts:
    """N_r, S, O, N, V and the stated L_r (`None` unstated)."""

    total: int
    spread: int
    one_port: int
    unallocated: int
    stale: int
    links: int | None

    @property
    def general(self) -> int:
        """G = N_r - S - O - N (negative when refused `sum`)."""
        return self.total - self.spread - self.one_port - self.unallocated

    @property
    def least(self) -> int:
        """The least links: seven per spread requirement, one per one-port and general one."""
        return 7 * self.spread + self.one_port + self.general


@dataclass(frozen=True)
class SiteCounts:
    """What the sites are counted from: per component its ports and runnables, the interfaces and tasks."""

    port_counts: tuple[int, ...]
    runnable_counts: tuple[int, ...]
    interfaces: int
    tasks: int


@dataclass(frozen=True)
class PlannedLink:
    """One link: its kind, its element's index of that kind and its version.

    Ports and runnables are indexed in component order; the composition and the processor are 0.
    """

    kind: str
    index: int
    version: int


@dataclass(frozen=True)
class PlannedRequirement:
    """One requirement: its links, its version and, for a spread or unallocated case, its case index."""

    links: tuple[PlannedLink, ...]
    version: int
    topic: int | None


@dataclass(frozen=True)
class RequirementPlan:
    """The requirements in id order, and each component's sentences as case indices, in case order."""

    requirements: tuple[PlannedRequirement, ...]
    sentences: tuple[tuple[int, ...], ...]


def requirement_counts(stated: Mapping[str, Any]) -> RequirementCounts:
    """The requirement counts of a stated passport; an absent count reads as 0."""
    links = stated.get(REQUIREMENT_LINKS)
    return RequirementCounts(
        stated_count(stated, LAYER_REQUIREMENTS),
        stated_count(stated, ON_ALL_SITE_KINDS),
        stated_count(stated, ON_ONE_PORT),
        stated_count(stated, UNALLOCATED),
        stated_count(stated, STALE_LINKS),
        None if links is None else int(links),
    )


def link_capacity(counts: RequirementCounts, n: int, runnables: int) -> int:
    """The most links the requirements hold, no site twice."""
    sites = n + runnables
    return 7 * counts.spread + counts.one_port + counts.spread * (sites - 2) + counts.general * sites


def link_total(counts: RequirementCounts, n: int, runnables: int) -> int:
    """L_r: the stated count, else derived and capped.

    7S + O + max(n + G - 1 when G > 0, G, V - 7S - O), capped at the capacity and at
    `COUNT_CEILING`.
    """
    if counts.links is not None:
        return counts.links
    general = counts.general
    base = 7 * counts.spread + counts.one_port
    derived = base + max(n + general - 1 if general > 0 else 0, general, counts.stale - base)
    return min(derived, link_capacity(counts, n, runnables), COUNT_CEILING)


def _even_fill(total: int, capacities: Sequence[int]) -> list[int]:
    """`total` split as evenly as the capacities allow, larger first by position."""
    given = [0] * len(capacities)
    active = [index for index, capacity in enumerate(capacities) if capacity > 0]
    while total > 0 and active:
        per, extra = divmod(total, len(active))
        for rank, index in enumerate(active):
            give = min(capacities[index] - given[index], per + (1 if rank < extra else 0))
            given[index] += give
            total -= give
        active = [index for index in active if given[index] < capacities[index]]
    return given


def _owners(counts: Sequence[int]) -> list[int]:
    """The component index of each element, the elements in component order."""
    return [component for component, count in enumerate(counts) for _ in range(count)]


def _extra_links(counts: RequirementCounts, n: int, runnables: int) -> tuple[list[int], list[int]]:
    """The links beyond each general and each spread requirement's least ones."""
    general = max(0, counts.general)
    extra = link_total(counts, n, runnables) - counts.least
    general_capacity = [n + runnables - 1] * general
    general_extra = [0] * general
    if counts.links is None and general:
        general_extra[0] = min(n - 1, extra)
    rest = extra - sum(general_extra)
    to_general = min(rest, sum(general_capacity) - sum(general_extra))
    filled = _even_fill(
        to_general,
        [cap - given for cap, given in zip(general_capacity, general_extra, strict=True)],
    )
    general_extra = [given + more for given, more in zip(general_extra, filled, strict=True)]
    spread_extra = _even_fill(rest - to_general, [n + runnables - 2] * counts.spread)
    return general_extra, spread_extra


def _sites(
    counts: RequirementCounts,
    sites: SiteCounts,
    general_extra: Sequence[int],
    spread_extra: Sequence[int],
) -> list[list[tuple[str, int]]]:
    """Each requirement's sites in id order, base links first."""
    n = len(sites.port_counts)
    ports = sum(sites.port_counts)
    runnables = sum(sites.runnable_counts)
    site_list = [(SWC_SITE, index) for index in range(n)] + [(RUNNABLE_SITE, index) for index in range(runnables)]
    sites_of: list[list[tuple[str, int]]] = []
    for s in range(counts.spread):
        base = [
            (SWC_SITE, s % n),
            (PORT_SITE, s % ports),
            (RUNNABLE_SITE, s % runnables),
            (INTERFACE_SITE, s % sites.interfaces),
            (COMPOSITION_SITE, 0),
            (TASK_SITE, s % sites.tasks),
            (HARDWARE_SITE, 0),
        ]
        own = {(SWC_SITE, s % n), (RUNNABLE_SITE, s % runnables)}
        extras = list(itertools.islice((site for site in site_list if site not in own), spread_extra[s]))
        sites_of.append(base + extras)
    for j in range(counts.one_port):
        sites_of.append([(PORT_SITE, (ports - 1 - j) % ports)])
    for _ in range(counts.unallocated):
        sites_of.append([])
    for g, more in enumerate(general_extra):
        start = g % n
        sites_of.append([site_list[(start + i) % len(site_list)] for i in range(1 + more)])
    return sites_of


def _stale_order(counts: RequirementCounts, sites_of: Sequence[Sequence[tuple[str, int]]]) -> list[tuple[int, int]]:
    """Every link as (requirement, position) in the stale enumeration's order."""
    spread = counts.spread
    general_first = spread + counts.one_port + counts.unallocated
    order = [(s, position) for s in range(spread) for position in range(len(LINK_KINDS))]
    order += [(spread + j, 0) for j in range(counts.one_port)]
    order += [
        (requirement, position)
        for requirement in range(general_first, len(sites_of))
        for position in range(len(sites_of[requirement]))
    ]
    order += [(s, position) for s in range(spread) for position in range(len(LINK_KINDS), len(sites_of[s]))]
    return order


def _held_components(
    requirement_sites: Sequence[tuple[str, int]],
    port_owner: Sequence[int],
    runnable_owner: Sequence[int],
) -> set[int]:
    """The components a requirement's links hold: a component link's own, a port's or a runnable's owner."""
    held: set[int] = set()
    for kind, element in requirement_sites:
        if kind == SWC_SITE:
            held.add(element)
        elif kind == PORT_SITE:
            held.add(port_owner[element])
        elif kind == RUNNABLE_SITE:
            held.add(runnable_owner[element])
    return held


def plan_requirements(counts: RequirementCounts, sites: SiteCounts) -> RequirementPlan:
    """The requirements and the description sentences, by index, for counts the refusals admit."""
    n = len(sites.port_counts)
    general_extra, spread_extra = _extra_links(counts, n, sum(sites.runnable_counts))
    sites_of = _sites(counts, sites, general_extra, spread_extra)
    stale = set(_stale_order(counts, sites_of)[: counts.stale])
    holding = {requirement for requirement, _ in stale}
    port_owner = _owners(sites.port_counts)
    runnable_owner = _owners(sites.runnable_counts)
    # The spread cases, then the unallocated ones, are cases 0, 1, ...; the others have none.
    spread = counts.spread
    unallocated_first = spread + counts.one_port
    general_first = unallocated_first + counts.unallocated

    requirements: list[PlannedRequirement] = []
    cases: list[set[int]] = []
    for index, requirement_sites in enumerate(sites_of):
        topic: int | None = None
        if index < spread:
            topic = index
        elif unallocated_first <= index < general_first:
            topic = spread + index - unallocated_first
        version = STALE_VERSION if index in holding else 1
        links = tuple(
            PlannedLink(
                kind,
                element,
                1 if (index, position) in stale or version == 1 else STALE_VERSION,
            )
            for position, (kind, element) in enumerate(requirement_sites)
        )
        requirements.append(PlannedRequirement(links, version, topic))
        if topic is not None:
            cases.append(_held_components(requirement_sites, port_owner, runnable_owner))
    return RequirementPlan(tuple(requirements), _place_sentences(n, cases))


def _place_sentences(n: int, cases: Sequence[set[int]]) -> tuple[tuple[int, ...], ...]:
    """Each component's sentences (case indices), placed as the module docstring describes."""
    sentences: list[list[int]] = [[] for _ in range(n)]
    for case, held in enumerate(cases):
        wanted = min(SENTENCES_PER_CASE, n - len(held))
        taken = 0
        step = 0
        while taken < wanted:
            component = (n - 1 - SENTENCES_PER_CASE * case - step) % n
            if component not in held:
                sentences[component].append(case)
                taken += 1
            step += 1
    return tuple(tuple(chosen) for chosen in sentences)


def sentence_count(plan: RequirementPlan) -> int:
    """The description sentences a plan writes."""
    return sum(len(chosen) for chosen in plan.sentences)


def descriptions(plan: RequirementPlan) -> tuple[str, ...]:
    """Each component's description: its sentences in case order, joined by one space."""
    return tuple(" ".join(sentence(case) for case in chosen) for chosen in plan.sentences)


def _interface_count(stated: Mapping[str, Any]) -> int:
    return int(stated[SENDER_RECEIVER]) + int(stated[CLIENT_SERVER]) + max(0, stub_count(stated))


def requirement_refusals(stated: Mapping[str, Any], ports: int | None) -> list[Refusal]:
    """The `sum`, `requirements` and `derived` refusals, at most one per leaf.

    `ports` is P, stated or derived by the shape (`None` when neither decided it).
    """
    counts = requirement_counts(stated)
    cases = counts.spread + counts.one_port + counts.unallocated
    if cases > counts.total:
        return [
            Refusal(
                LAYER_REQUIREMENTS,
                SUM,
                f"states {counts.total}; {ON_ALL_SITE_KINDS}, {ON_ONE_PORT} and {UNALLOCATED} state {cases} in all",
            )
        ]
    n = stated_count(stated, LAYER_SWC)
    runnables = runnable_count(stated)
    refusals: list[Refusal] = []
    if counts.spread > 0:
        kinds = (
            ("component", n),
            ("port", ports),
            ("runnable", runnables),
            ("interface", _interface_count(stated)),
            ("task", stated_count(stated, LAYER_TASKS)),
        )
        missing = next((kind for kind, count in kinds if count == 0), None)
        if missing is not None:
            refusals.append(
                Refusal(
                    ON_ALL_SITE_KINDS,
                    REQUIREMENTS_CLASS,
                    f"states {counts.spread}; a spread requirement is linked on every site kind, "
                    f"and the specimen has no {missing}",
                )
            )
    if counts.one_port > 0 and ports == 0:
        refusals.append(
            Refusal(
                ON_ONE_PORT,
                REQUIREMENTS_CLASS,
                f"states {counts.one_port}; a one-port requirement is linked on one port, and the specimen has no port",
            )
        )
    if counts.general > 0 and n + runnables == 0:
        refusals.append(
            Refusal(
                LAYER_REQUIREMENTS,
                REQUIREMENTS_CLASS,
                f"states {counts.general} requirements that are none of spread, one-port or "
                "unallocated; a general requirement is linked on a component or a runnable, and "
                "the specimen has neither",
            )
        )
    if refusals:
        return refusals
    capacity = link_capacity(counts, n, runnables)
    over = f"the requirements hold at most {capacity} links, no site twice"

    def through_runnables(refusal: Refusal) -> Refusal:
        # A closed-form bound read through the derived Rn; no search was tried, so the shape's
        # "no derivation tried" suffix would mislead. Rn enters the capacity only through S and
        # G; with both 0 the refusal keeps its class.
        if RUNNABLES in stated or counts.spread == counts.general == 0:
            return refusal
        return Refusal(
            refusal.leaf,
            DERIVED,
            f"{refusal.sentence}; {RUNNABLES} is unstated and derived as {runnables}, and a "
            "larger stated count raises the capacity",
        )

    if counts.links is not None:
        if counts.links < counts.least:
            refusals.append(
                Refusal(
                    REQUIREMENT_LINKS,
                    REQUIREMENTS_CLASS,
                    f"states {counts.links}; the {counts.spread} spread, {counts.one_port} "
                    f"one-port and {counts.general} general requirements need at least "
                    f"{counts.least}",
                )
            )
        elif counts.links > capacity:
            refusals.append(
                through_runnables(Refusal(REQUIREMENT_LINKS, REQUIREMENTS_CLASS, f"states {counts.links}; {over}"))
            )
        if counts.stale > counts.links:
            refusals.append(
                Refusal(
                    STALE_LINKS,
                    REQUIREMENTS_CLASS,
                    f"states {counts.stale}; {REQUIREMENT_LINKS} states {counts.links}",
                )
            )
    elif counts.stale > capacity:
        refusals.append(through_runnables(Refusal(STALE_LINKS, REQUIREMENTS_CLASS, f"states {counts.stale}; {over}")))
    return refusals


def stated_site_counts(stated: Mapping[str, Any], port_counts: Sequence[int]) -> SiteCounts:
    """The site counts of a stated passport over the port counts its shape decided."""
    return SiteCounts(
        tuple(port_counts),
        tuple(shape.runnables for shape in build_components(stated, port_counts)),
        _interface_count(stated),
        stated_count(stated, LAYER_TASKS),
    )


def requirement_ceiling_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    """The two `ceiling` refusals: the least links and the description sentences.

    The sentences are counted exactly only when their bound min(3, n)(S + N) passes the ceiling.
    When another refusal leaves the exact count undecided, the unallocated cases' own min(3, n) N
    sentences (no shape changes them) still refuse it, so a ceiling refusal is never hidden behind
    another class.
    """
    counts = requirement_counts(stated)
    if counts.general < 0:
        return []
    ceiling = CEILING_SENTENCE
    if counts.least > COUNT_CEILING:
        return [Refusal(REQUIREMENT_LINKS, CEILING, f"needs at least {counts.least} links; {ceiling}")]
    n = stated_count(stated, LAYER_SWC)
    if min(SENTENCES_PER_CASE, n) * (counts.spread + counts.unallocated) <= COUNT_CEILING:
        return []
    written = requirement_sentence_count(stated)
    if written is None:
        unallocated = min(SENTENCES_PER_CASE, n) * counts.unallocated
        if unallocated > COUNT_CEILING:
            sentence = f"writes at least {unallocated} description sentences; {ceiling}"
            return [Refusal(LAYER_REQUIREMENTS, CEILING, sentence)]
        return []
    if written <= COUNT_CEILING:
        return []
    return [Refusal(LAYER_REQUIREMENTS, CEILING, f"writes {written} description sentences; {ceiling}")]


def requirement_sentence_count(stated: Mapping[str, Any]) -> int | None:
    """The exact description sentences of a stated passport, by `plan_requirements`.

    `None` when the components, compositions, interfaces, shape or requirement refusals leave them
    undecided.
    """
    if component_refusals(stated) or composition_refusals(stated):
        return None
    if interface_refusals(stated):
        return None
    shape = specimen_shape(stated)
    if not isinstance(shape, Shape):
        return None
    port_counts = shape.counts
    if requirement_refusals(stated, sum(port_counts)):
        return None
    return sentence_count(plan_requirements(requirement_counts(stated), stated_site_counts(stated, port_counts)))


def build_requirements(
    plan: RequirementPlan,
    ids: Sequence[str],
    component_names: Sequence[str],
    ports: Sequence[Sequence[str]],
    runnables: Sequence[int],
    interfaces: Sequence[str],
    composition: str,
    tasks: Sequence[str],
    processor: str,
) -> tuple[Requirement, ...]:
    """The planned requirements under their names.

    `ports` holds each component's port names and `runnables` its runnable count, in component order.
    """
    port_sites = [(component, port) for component, names in zip(component_names, ports, strict=True) for port in names]
    runnable_sites = [
        (component, f"R{index}")
        for component, count in zip(component_names, runnables, strict=True)
        for index in range(1, count + 1)
    ]

    def site(link: PlannedLink) -> RequirementLink:
        kind, index, version = link.kind, link.index, link.version
        if kind == SWC_SITE:
            return RequirementLink(kind, component_names[index], None, version)
        if kind == PORT_SITE:
            return RequirementLink(kind, *port_sites[index], version)
        if kind == RUNNABLE_SITE:
            return RequirementLink(kind, *runnable_sites[index], version)
        if kind == INTERFACE_SITE:
            return RequirementLink(kind, interfaces[index], None, version)
        if kind == COMPOSITION_SITE:
            return RequirementLink(kind, composition, None, version)
        if kind == TASK_SITE:
            return RequirementLink(kind, tasks[index], None, version)
        if kind == HARDWARE_SITE:
            return RequirementLink(kind, processor, None, version)
        raise GenerationError(f"a planned requirement link names no site kind: {kind!r}")

    requirements: list[Requirement] = []
    for requirement_id, planned in zip(ids, plan.requirements, strict=True):
        if planned.topic is None:
            title, description = neutral_title(requirement_id), NEUTRAL_DESCRIPTION
        else:
            title, description = REQUIREMENT_TOPICS[planned.topic % len(REQUIREMENT_TOPICS)]
        requirements.append(
            Requirement(
                requirement_id,
                title,
                description,
                ASIL.ABSENT,
                tuple(site(link) for link in planned.links),
                planned.version,
            )
        )
    return tuple(requirements)
