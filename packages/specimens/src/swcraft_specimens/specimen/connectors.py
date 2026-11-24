"""Connectors, links and unconnected ports, decided by construction.

Notation: the n components hold the port counts p_j (the first k the hubs), the named interfaces
the fan-outs f_i. U = `components.unconnected.total`, u1 = `components.unconnected.max_on_one`,
R the reached ports, A the connectors (`connectors.assembly`, or `layers.connectors`),
L = `connectors.links`, m = n - k the ordinary components, C_oo the connectors between two of them.

A connector joins a provide port to a require port of the same interface on two different
components; a require port carries at most one connector and a provide port at least one. A provide
port and the require ports it serves are a **star**; an interface holds whole stars, and its other
ports are unconnected.

The checks before generation, each refused at its leaf:

- `sum` at `connectors.assembly` when it differs from `layers.connectors`;
- `unconnected` at `components.unconnected.max_on_one`: u1 <= U, u1 <= the largest component,
  U <= sum(min(p_j, u1)), and u1 > 0 when U > 0;
- `connectors` at the connector leaf: A = 0 exactly when R = 0, otherwise R >= 2 and
  ceil(R / 2) <= A <= R - 1, and A <= sum(f_i - 1); and no component keeps more reached ports
  than there are connectors, p_j - min(p_j, u1) <= A;
- `links` at `connectors.links`: with no hub A = m // 2 + L when L > 0 and A <= m // 2 when L = 0;
  with a hub, C_oo = m // 2 + L <= A, and with L > 0 also C_oo <= (the ordinary components' ports) - 1.

A passport the checks admit and the search cannot build is refused at the connector leaf (or at
`connectors.links` when A is unstated) with the class `links` when there is a hub or A is unstated,
else `connectors`. The search is bounded (`PACK_BUDGET`, `EXACT_BUDGET`, `EXACT_STEPS`,
`VARIANT_STEPS`): one that runs out refuses, never errs, and a configuration it misses is refused,
never approximated. With delegations :func:`solve_open` builds one fixed open placement for the
shape. The built connectors are re-checked; a mismatch is a `GenerationError`, never a refusal.
"""

from __future__ import annotations

import bisect
import functools
import heapq
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

from ..errors import GenerationError
from ..model import (
    AnyInterface,
    Connector,
    ConnectorEndpoint,
    Port,
)
from ..passport import Refusal
from .common import Budget, ceil_div, hub_count, link_bounds, stated_connector_count
from .identifiers import ConnectorNames, connector_names
from .vocabulary import (
    ASSEMBLY,
    CONNECTORS_CLASS,
    LAYER_CONNECTORS,
    LINKS,
    LINKS_CLASS,
    MAX_ON_ONE,
    SUM,
    UNCONNECTED_CLASS,
    UNCONNECTED_TOTAL,
)

#: The stars up to which the packing is decided exactly, by search.
EXACT_STARS = 40
#: The search steps one exact packing may take before the greedy packing decides alone.
EXACT_STEPS = 20000
#: The most middle arcs (component pairs that can carry a connector) a side's matrix is laid by
#: max-flow with; above it the greedy lays it (its output differs, both are valid). Every
#: configuration the tests and the committed fixtures build stays below it (at most 4,901).
MAX_FLOW_ARCS = 40000
#: The greedy packings one decision may try before it gives up and refuses.
PACK_BUDGET = 4000
#: The exact packings one decision may try after the greedy ones, before it refuses.
EXACT_BUDGET = 200
#: The placements of the unconnected ports one pass of a decision may walk, over every count
#: it tries, before it refuses: the walk spends no packing when the closed forms reject every
#: provider split, so without it a count the closed forms refuse late costs seconds each.
VARIANT_STEPS = 20000


class Search(NamedTuple):
    """The packings one decision may try: greedy ones, then exact ones (none when 0)."""

    greedy: int
    exact: int


#: The whole search: every decision of a stated configuration takes it.
FULL_SEARCH = Search(PACK_BUDGET, EXACT_BUDGET)
#: The greedy packings of a quick search.
QUICK_PACKINGS = 100
#: A quick search (`shape.py` tries many configurations with it): greedy packings only, few.
QUICK_SEARCH = Search(QUICK_PACKINGS, 0)


@dataclass(frozen=True)
class ConnectorSpec:
    """The stated connector leaves: U, u1, A (assembly, and the layer count) and L.

    For an unstated A the construction chooses, it also holds what the identifier check asks of
    it (`construction_spec`).
    """

    unconnected: int
    max_on_one: int
    assembly: int | None
    layer: int | None
    links: int
    names: ConnectorNames | None = None

    @property
    def connectors(self) -> int | None:
        """A: `connectors.assembly` when stated, else `layers.connectors`, else unstated."""
        return stated_connector_count(self.assembly, self.layer)

    @property
    def connector_leaf(self) -> str:
        """The leaf a refusal of A names."""
        if self.assembly is not None:
            return ASSEMBLY
        return LAYER_CONNECTORS if self.layer is not None else LINKS


@dataclass(frozen=True)
class ConnectorPlan:
    """Each component's ports (in component order) and the connectors, all drawn in the root.

    The compositions are `compositions.py`'s. Per component the names of its ports no connector
    end reaches (port order), and the star centres, the provide ports a connector reaches, as
    (component, port), in component and port order.
    """

    ports: tuple[tuple[Port, ...], ...]
    connectors: tuple[Connector, ...]
    open_ports: tuple[tuple[str, ...], ...]
    centres: tuple[tuple[int, str], ...]


def connector_spec(stated: Mapping[str, Any]) -> ConnectorSpec:
    """The connector leaves of a stated passport (dotted paths).

    The schema requires U, u1 and L, and A (`connectors.assembly` or `layers.connectors`) may be
    unstated.
    """

    def optional(path: str) -> int | None:
        value = stated.get(path)
        return None if value is None else int(value)

    return ConnectorSpec(
        unconnected=int(stated[UNCONNECTED_TOTAL]),
        max_on_one=int(stated[MAX_ON_ONE]),
        assembly=optional(ASSEMBLY),
        layer=optional(LAYER_CONNECTORS),
        links=int(stated[LINKS]),
    )


def construction_spec(stated: Mapping[str, Any]) -> ConnectorSpec:
    """The connector leaves the construction searches with.

    `connector_spec`, and with A unstated the identifier check's demands on the count it chooses
    (`identifiers.connector_names`). The shape and the builder search with the same.
    """
    spec = connector_spec(stated)
    if spec.connectors is not None:
        return spec
    return replace(spec, names=connector_names(stated))


# --- the checks before generation ---------------------------------------------------------------


def assembly_sum_refusal(spec: ConnectorSpec) -> Refusal | None:
    """`sum` at `connectors.assembly` when it differs from `layers.connectors`."""
    if spec.assembly is None or spec.layer is None or spec.assembly == spec.layer:
        return None
    return Refusal(ASSEMBLY, SUM, f"states {spec.assembly}; layers.connectors states {spec.layer}")


def unconnected_refusal(spec: ConnectorSpec, largest: int | None, room: int, holders: str) -> Refusal | None:
    """`unconnected` at `components.unconnected.max_on_one`, or `None`.

    Refused: u1 above U or above the largest count (`None` when no count is known), U > 0 with
    u1 = 0, or U above the `room` the components (`holders`, as the sentence names them) leave at
    most u1 each.
    """
    total, u1 = spec.unconnected, spec.max_on_one
    if u1 > total:
        sentence = f"states {u1} on one component, above the {total} unconnected in all"
    elif largest is not None and u1 > largest:
        sentence = f"states {u1} on one component; the largest holds {largest} ports"
    elif total and not u1:
        sentence = f"states none on any component beside {total} unconnected in all"
    elif total > room:
        sentence = (
            f"states at most {u1} on one component; {holders} leave room for at most {room} "
            f"unconnected ports, below the {total} stated"
        )
    else:
        return None
    return Refusal(MAX_ON_ONE, UNCONNECTED_CLASS, sentence)


def kept_refusal(spec: ConnectorSpec, largest: int | None) -> Refusal | None:
    """`connectors` at the connector leaf: the largest count keeps more reached ports than A.

    Refused when the largest count keeps more reached ports beyond u1 than there are connectors
    (each needs a connector of its own); `None` with A unstated.
    """
    connectors = spec.connectors
    if connectors is None or largest is None:
        return None
    kept = largest - min(largest, spec.max_on_one)
    if kept <= connectors:
        return None
    return Refusal(
        spec.connector_leaf,
        CONNECTORS_CLASS,
        f"states {connectors}; a component of {largest} ports keeps at least {kept} reached, "
        "each with a connector of its own",
    )


def _unconnected_refusal(spec: ConnectorSpec, counts: Sequence[int]) -> Refusal | None:
    room = sum(min(count, spec.max_on_one) for count in counts)
    return unconnected_refusal(spec, max(counts, default=0), room, "the components")


class _Terms(NamedTuple):
    """What the connector checks read of one configuration beside A.

    Its port counts, fan-outs, U and u1, never A. Computed once per configuration (`_terms`), so
    the checks of each derived A candidate cost no scan of the counts (the median above all).
    """

    n: int
    hubs: int
    largest: int
    reached: int
    capacity: int
    ordinary: int
    ordinary_kept: int
    hub_kept: int


def _terms(spec: ConnectorSpec, counts: Sequence[int], fan_outs: Sequence[int]) -> _Terms:
    hubs = hub_count(counts)
    total, on_one = spec.unconnected, spec.max_on_one
    ordinary = sum(counts[hubs:])
    return _Terms(
        n=len(counts),
        hubs=hubs,
        largest=max(counts, default=0),
        reached=sum(counts) - total,
        capacity=sum(fan_out - 1 for fan_out in fan_outs),
        ordinary=ordinary,
        ordinary_kept=ordinary - min(total, sum(min(count, on_one) for count in counts[hubs:])),
        hub_kept=sum(counts[:hubs]) - min(total, sum(min(count, on_one) for count in counts[:hubs])),
    )


def _connector_refusal(spec: ConnectorSpec, terms: _Terms) -> Refusal | None:
    connectors = spec.connectors
    reached = terms.reached
    if reached < 0:
        return None
    if connectors is None:
        return one_reached_refusal() if reached == 1 else None
    capacity = terms.capacity
    if reached == 0 and connectors:
        sentence = f"states {connectors}; no port is reached"
    elif reached and not connectors:
        sentence = f"states none; {reached} ports are reached"
    elif reached == 1:
        sentence = f"states {connectors}; one port is reached, and a connector joins two"
    elif reached and connectors < ceil_div(reached, 2):
        sentence = (
            f"states {connectors}; the {reached} reached ports need at least "
            f"{ceil_div(reached, 2)}, one per require port"
        )
    elif reached and connectors > reached - 1:
        sentence = (
            f"states {connectors}; the {reached} reached ports allow at most {reached - 1}, "
            "since a provide port carries no require port's connector"
        )
    else:
        return capacity_refusal(spec, capacity)
    return Refusal(spec.connector_leaf, CONNECTORS_CLASS, sentence)


def one_reached_refusal() -> Refusal:
    """`unconnected` at `components.unconnected.total` with A unstated: one reached port."""
    return Refusal(UNCONNECTED_TOTAL, UNCONNECTED_CLASS, "leaves one port reached; a connector joins two")


def capacity_refusal(spec: ConnectorSpec, capacity: int) -> Refusal | None:
    """`connectors` at the connector leaf: a stated A above the named interfaces' `capacity`.

    The capacity is sum(f_i - 1), each named interface needing a provide port (`None` with A
    unstated).
    """
    connectors = spec.connectors
    if connectors is None or connectors <= capacity:
        return None
    return Refusal(
        spec.connector_leaf,
        CONNECTORS_CLASS,
        f"states {connectors}; the named interfaces allow at most {capacity}, each with a provide port of its own",
    )


def _links_refusal(spec: ConnectorSpec, terms: _Terms) -> Refusal | None:
    return links_refusal(spec, terms.n, terms.hubs)


def links_refusal(spec: ConnectorSpec, n: int, hubs: int) -> Refusal | None:
    """The `links` inequality for n components of which `hubs` are hubs.

    It reads no port count, so the stated leaves decide it alone (`shape.py`).
    """
    connectors = spec.connectors
    if connectors is None:
        return None
    least, most = link_bounds(n, hubs, spec.links)
    if least <= connectors and (most is None or connectors <= most):
        return None
    half = (n - hubs) // 2
    links = spec.links
    if not hubs and links:
        sentence = (
            f"states {links}; with no hub every connector joins two ordinary components, so "
            f"{connectors} connectors give {max(0, connectors - half)}"
        )
    elif not hubs:
        sentence = (
            f"states 0; with no hub every connector joins two ordinary components, and "
            f"{connectors} connectors exceed the {half} pair connectors"
        )
    else:
        sentence = (
            f"states {links}: {half + links} connectors between ordinary components, above the "
            f"{connectors} connectors in all"
        )
    return Refusal(LINKS, LINKS_CLASS, sentence)


def _extra_refusal(spec: ConnectorSpec, terms: _Terms) -> Refusal | None:
    """The necessary conditions beyond the inequalities: the kept-ports and ordinary-ports bounds."""
    kept = kept_refusal(spec, terms.largest)
    if kept is not None:
        return kept
    if not terms.hubs:
        return None
    return hub_links_refusal(spec, terms.n, terms.hubs, terms.ordinary, terms.ordinary_kept, terms.hub_kept)


def hub_links_refusal(
    spec: ConnectorSpec,
    n: int,
    hubs: int,
    ordinary: int | None,
    ordinary_kept: int,
    hub_kept: int,
) -> Refusal | None:
    """The `links` conditions with a hub.

    The connectors between ordinary components against their `ordinary` ports (the most they
    hold; `None` when unbounded), and with A stated the reached ports the ordinary components and
    the hubs keep at least (`ordinary_kept`, `hub_kept`) against the connector ends that can take
    them.
    """
    connectors = spec.connectors
    half = (n - hubs) // 2
    if spec.links and ordinary is not None:
        between = half + spec.links
        if between > ordinary - 1:
            return Refusal(
                LINKS,
                LINKS_CLASS,
                f"states {spec.links}: {between} connectors between ordinary components, whose "
                f"{ordinary} ports hold at most {max(0, ordinary - 1)}",
            )
    if connectors is None:
        return None
    most = half + spec.links if spec.links else min(half, connectors)
    least = half + spec.links if spec.links else 0
    if ordinary_kept > most + connectors:
        return Refusal(
            LINKS,
            LINKS_CLASS,
            f"states {spec.links}: the ordinary components keep at least {ordinary_kept} reached "
            f"ports, each an end of one of the {most} connectors between them or of one of the "
            f"{connectors - most} others, at most {most + connectors} ends",
        )
    if hub_kept > 2 * (connectors - least):
        return Refusal(
            LINKS,
            LINKS_CLASS,
            f"states {spec.links}: the hubs keep at least {hub_kept} reached ports, each an end "
            f"of one of the {connectors - least} connectors with a hub end, at most "
            f"{2 * (connectors - least)} ends",
        )
    return None


def connector_refusals(
    spec: ConnectorSpec,
    counts: Sequence[int],
    fan_outs: Sequence[int],
    search: Search = FULL_SEARCH,
) -> list[Refusal]:
    """Every connector refusal for these port counts and fan-outs, in no particular order.

    The construction is searched within `search`.
    """
    refusals: list[Refusal] = []
    terms = _terms(spec, counts, fan_outs)
    for refusal in (
        assembly_sum_refusal(spec),
        _unconnected_refusal(spec, counts),
        _connector_refusal(spec, terms),
        _links_refusal(spec, terms),
    ):
        if refusal is not None:
            refusals.append(refusal)
    if refusals:
        return refusals
    extra = _extra_refusal(spec, terms)
    if extra is not None:
        return [extra]
    if _solve(spec, counts, fan_outs, search) is None:
        if (
            spec.connectors is None
            and spec.names is not None
            and sum(counts) == spec.unconnected
            and not spec.names.admits(0)
        ):
            return spec.names.refusals_without_connectors()
        leaf = spec.connector_leaf if spec.connectors is not None else LINKS
        refusal_class = LINKS_CLASS if terms.hubs or spec.connectors is None else CONNECTORS_CLASS
        names = (
            " with a number of connectors whose names meet " + " and ".join(spec.names.stated())
            if spec.connectors is None and spec.names is not None and spec.names.constrains
            else ""
        )
        return [
            Refusal(
                leaf,
                refusal_class,
                "no placement of the connectors, the unconnected ports and the links fits the "
                f"component ports and the interface fan-outs{names}",
            )
        ]
    return []


# --- the search ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectorPiece:
    """A star: its provider component, its interface (room) and its connectors."""

    node: int
    room: int
    edges: int


class Category(NamedTuple):
    """One side's stars (the ordinary components', or the hubs').

    Its providers, its extras and the most connector ends one of its components may hold.
    """

    providers: int
    extras: int
    bound: int


#: A packing: per component its providers and its extras, and the stars placed into the
#: interfaces.
Packing = tuple[tuple[int, ...], tuple[int, ...], tuple[ConnectorPiece, ...]]

#: The sides of the packings: the ordinary components and the hubs.
ORDINARY_SIDE = 0
HUB_SIDE = 1


@dataclass(frozen=True)
class ConnectorSolution:
    """The per-component choices and the block totals the realisation turns into connectors."""

    unconnected: tuple[int, ...]
    providers: tuple[int, ...]
    extras: tuple[int, ...]
    pieces: tuple[ConnectorPiece, ...]
    ordinary: int
    hub_to_hub: int
    to_hubs: int
    from_hubs: int


def star_solution(
    counts: Sequence[int],
    fan_outs: Sequence[int],
    open_units: Sequence[int],
    providers: Sequence[int],
    extras: Sequence[int],
    pieces: Sequence[tuple[int, int, int]],
    *,
    ordinary: int,
    to_hubs: int,
    from_hubs: int,
    hub_to_hub: int,
) -> ConnectorSolution:
    """A construction decided elsewhere (the entry planner's layout).

    `build_connectors(..., solution=...)` lays it with no search.

    `open_units` holds per component its ports no connector end reaches (its unconnected and its
    delegation-only ones); `providers` and `extras` its stars and its connectors beyond one per
    star; `pieces` the stars as (component, interface index into `fan_outs`, connectors); the
    blocks are C_oo (`ordinary`), OH (`to_hubs`: an ordinary provide end, a hub require end), HO
    (`from_hubs`) and HH (`hub_to_hub`). Each is checked in integers against the others and the
    connector module's closed form; a mismatch, an interface the stars overfill
    above all, is a `GenerationError`. What this does not check: the stated leaves (U, u1, A, L),
    which the post-check of `build_connectors` holds the laid ports to.
    """
    n = len(counts)
    if not (len(open_units) == len(providers) == len(extras) == n):
        raise GenerationError("the star solution does not give every component")
    if any(not 0 <= units <= count for units, count in zip(open_units, counts, strict=True)):
        raise GenerationError("the star solution's open ports exceed a component's ports")
    used = [0] * len(fan_outs)
    stars = [0] * n
    edges = [0] * n
    for node, room, amount in pieces:
        if not (0 <= node < n and 0 <= room < len(fan_outs) and amount >= 1):
            raise GenerationError("the star solution holds a star outside the configuration")
        used[room] += amount + 1
        stars[node] += 1
        edges[node] += amount
    overfilled = [i for i, (taken, size) in enumerate(zip(used, fan_outs, strict=True)) if taken > size]
    if overfilled:
        raise GenerationError(
            f"the star solution's stars overfill interface {overfilled[0]} "
            f"({used[overfilled[0]]} ports of {fan_outs[overfilled[0]]})"
        )
    if (
        list(stars) != list(providers)
        or [p + e for p, e in zip(providers, extras, strict=True)] != edges
        or min(extras, default=0) < 0
    ):
        raise GenerationError("the star solution's stars differ from its providers and extras")
    hubs = hub_count(counts)
    ins = [count - units - star for count, units, star in zip(counts, open_units, providers, strict=True)]
    outs = edges
    connectors = sum(outs)
    blocks = (ordinary, to_hubs, from_hubs, hub_to_hub)
    if (
        min(ins, default=0) < 0
        or min(blocks) < 0
        or sum(ins) != connectors
        or sum(blocks) != connectors
        or sum(ins[hubs:]) != ordinary + from_hubs
        or sum(outs[hubs:]) != ordinary + to_hubs
        or any(ins[a] + outs[a] + hub_to_hub > connectors for a in range(hubs, n))
        or any(ins[h] + outs[h] + ordinary > connectors for h in range(hubs))
    ):
        raise GenerationError("the star solution's blocks differ from its stars")
    return ConnectorSolution(
        tuple(open_units),
        tuple(providers),
        tuple(extras),
        tuple(ConnectorPiece(node, room, amount) for node, room, amount in pieces),
        ordinary,
        hub_to_hub,
        to_hubs,
        from_hubs,
    )


def _connector_candidates(spec: ConnectorSpec, counts: Sequence[int], fan_outs: Sequence[int]) -> list[int]:
    if spec.connectors is not None:
        return [spec.connectors]
    terms = _terms(spec, counts, fan_outs)
    reached = terms.reached
    names = spec.names
    if reached == 0:
        return [0] if names is None or names.admits(0) else []
    candidates: list[int] = []
    for connectors in range(ceil_div(reached, 2), reached):
        if names is not None and not names.admits(connectors):
            continue
        trial = ConnectorSpec(spec.unconnected, spec.max_on_one, connectors, None, spec.links)
        if (
            _connector_refusal(trial, terms) is None
            and _links_refusal(trial, terms) is None
            and _extra_refusal(trial, terms) is None
        ):
            candidates.append(connectors)
    return candidates


def _solve(
    spec: ConnectorSpec,
    counts: Sequence[int],
    fan_outs: Sequence[int],
    search: Search = FULL_SEARCH,
) -> ConnectorSolution | None:
    """The first construction the search finds, or `None`.

    One search per configuration, shared by the feasibility check, the identifier lengths and the
    builder. The search is deterministic, so a construction a smaller search finds is the one a
    larger search finds first.
    """
    return _solve_cached(spec, tuple(counts), tuple(fan_outs), search)


def configuration_solution(
    spec: ConnectorSpec,
    counts: Sequence[int],
    fan_outs: Sequence[int],
    search: Search = FULL_SEARCH,
) -> ConnectorSolution | None:
    """The construction the search lands on for a configuration with no delegation.

    `None` when refused; the shape records it as the layout with D = 0.
    """
    return _solve(spec, counts, fan_outs, search)


def unconnected_placements(spec: ConnectorSpec, counts: Sequence[int]) -> Iterator[tuple[int, ...]]:
    """Every placement of the U unconnected ports the search walks, in its order.

    The hubs' share from the fewest up, and for each the placements `_unconnected_variants` gives,
    each once.

    Made lazily, so a walk that stops early makes no more: at the count ceiling a case can have
    thousands of placements, each a tuple over every component."""
    hubs = hub_count(counts)
    total, u1 = spec.unconnected, spec.max_on_one
    hub_room = sum(min(count, u1) for count in counts[:hubs])
    ordinary_room = sum(min(count, u1) for count in counts[hubs:])
    for on_hubs in range(max(0, total - ordinary_room), min(total, hub_room) + 1):
        yield from _unconnected_variants(counts, hubs, on_hubs, total - on_hubs, u1)


def _open_candidates(
    spec: ConnectorSpec,
    counts: Sequence[int],
    units: Sequence[int],
    fan_outs: Sequence[int],
    most: int | None,
) -> list[int]:
    """The connector counts a fixed open placement admits, from the fewest up.

    `units` holds per component the ports no connector reaches. A when stated, else ceil(R_c / 2)
    to R_c - 1 (or 0 for R_c = 0), each within the capacity, the `links` inequality, no component
    keeping more connector-reached ports than A, at most `most`, and passing the identifier
    check's demands.
    """
    reach = [count - u for count, u in zip(counts, units, strict=True)]
    connected = sum(reach)
    names = spec.names
    if spec.connectors is not None:
        candidates = [spec.connectors]
    elif connected == 0:
        candidates = [0] if names is None or names.admits(0) else []
    else:
        candidates = [a for a in range(ceil_div(connected, 2), connected) if names is None or names.admits(a)]
    capacity = sum(fan_out - 1 for fan_out in fan_outs)
    largest = max(reach, default=0)
    least, link_most = link_bounds(len(counts), hub_count(counts), spec.links)
    admitted: list[int] = []
    for connectors in candidates:
        if (
            (most is not None and connectors > most)
            or connectors > capacity
            or connectors < least
            or (link_most is not None and connectors > link_most)
            or largest > connectors
            or (connected == 0) != (connectors == 0)
            or (connected and not ceil_div(connected, 2) <= connectors <= connected - 1)
        ):
            continue
        admitted.append(connectors)
    return admitted


def solve_open(
    spec: ConnectorSpec,
    counts: Sequence[int],
    units: Sequence[int],
    fan_outs: Sequence[int],
    search: Search = FULL_SEARCH,
    most: int | None = None,
) -> ConnectorSolution | None:
    """The construction with every component's ports no connector reaches fixed, or `None`.

    `units` holds its unconnected and its delegation-only ports; A is at most `most`; `None` when
    the search finds none. Deterministic and cached, so the builder finds what the shape found.
    """
    return _solve_open_cached(spec, tuple(counts), tuple(units), tuple(fan_outs), search, most)


def solution_connectors(solution: ConnectorSolution) -> int:
    """The connectors a construction builds."""
    return sum(piece.edges for piece in solution.pieces)


@functools.lru_cache(maxsize=64)
def _solve_open_cached(
    spec: ConnectorSpec,
    counts: tuple[int, ...],
    units: tuple[int, ...],
    fan_outs: tuple[int, ...],
    search: Search,
    most: int | None,
) -> ConnectorSolution | None:
    hubs = hub_count(counts)

    def phase(exact: bool, budget: Budget) -> ConnectorSolution | None:
        for connectors in _open_candidates(spec, counts, units, fan_outs, most):
            fixed = _Fixed(counts, hubs, fan_outs, sum(units), spec.max_on_one, connectors, spec.links)
            solution = _solve_units(fixed, units, budget, exact=exact)
            if solution is not None or budget.left <= 0:
                return solution
        return None

    return _two_phases(search, phase)


def _two_phases(search: Search, phase: Callable[[bool, Budget], ConnectorSolution | None]) -> ConnectorSolution | None:
    """The greedy phase, then the exact one; the first solution a phase finds.

    Each phase has its own budget from `search`; a phase with none is not run, nor any after it.
    """
    for exact in (False, True):
        budget = Budget(search.exact if exact else search.greedy)
        if budget.left <= 0:
            break
        solution = phase(exact, budget)
        if solution is not None:
            return solution
    return None


def _solve_units(fixed: _Fixed, units: tuple[int, ...], budget: Budget, *, exact: bool) -> ConnectorSolution | None:
    """`_solve_fixed` with the open placement given.

    The block totals are walked as there, and the one placement packed.
    """
    counts, hubs, connectors, links = fixed.counts, fixed.hubs, fixed.connectors, fixed.links
    n = len(counts)
    reach = [count - u for count, u in zip(counts, units, strict=True)]
    if min(reach, default=0) < 0:
        return None
    reached = sum(reach)
    if reached - connectors < 0 or connectors < 0:
        return None
    hub_reached = sum(reach[:hubs])
    ordinary_reached = sum(reach[hubs:])
    hub_kept = max(reach[:hubs], default=0)
    ordinary_kept = max(reach[hubs:], default=0)
    for ordinary in _orders(n, hubs, links, connectors):
        if hub_kept > connectors - ordinary:
            continue
        for block in _blocks(connectors, hubs, ordinary, hub_reached, ordinary_reached, ordinary_kept):
            solution = _try_providers(fixed, block, units, budget, exact=exact)
            if solution is not None or budget.left < 0:
                return solution
    return None


def connector_count(
    spec: ConnectorSpec,
    counts: Sequence[int],
    fan_outs: Sequence[int],
    search: Search = FULL_SEARCH,
) -> int | None:
    """The connectors the construction builds: A when stated, else the search's.

    `None` when the configuration is refused.
    """
    if spec.connectors is not None:
        return spec.connectors
    solution = _solve(spec, counts, fan_outs, search)
    return None if solution is None else solution_connectors(solution)


@functools.lru_cache(maxsize=64)
def _solve_cached(
    spec: ConnectorSpec, counts: tuple[int, ...], fan_outs: tuple[int, ...], search: Search
) -> ConnectorSolution | None:
    hubs = hub_count(counts)

    def phase(exact: bool, budget: Budget) -> ConnectorSolution | None:
        variants = Budget(VARIANT_STEPS)
        for connectors in _connector_candidates(spec, counts, fan_outs):
            fixed = _Fixed(
                counts,
                hubs,
                fan_outs,
                spec.unconnected,
                spec.max_on_one,
                connectors,
                spec.links,
            )
            solution = _solve_fixed(fixed, budget, variants, exact=exact)
            if solution is not None or budget.left <= 0 or variants.left <= 0:
                return solution
        return None

    return _two_phases(search, phase)


def _level_fill(
    counts: Sequence[int], nodes: Sequence[int], caps: Mapping[int, int], total: int
) -> dict[int, int] | None:
    """`total` units onto the nodes, always to the one with the most ports left (ties: lowest)."""
    if sum(caps[a] for a in nodes) < total:
        return None

    def taken(level: int) -> dict[int, int]:
        return {a: min(caps[a], max(0, counts[a] - level)) for a in nodes}

    low, high = 0, max((counts[a] for a in nodes), default=0)
    while low < high:
        middle = (low + high) // 2
        if sum(taken(middle).values()) <= total:
            high = middle
        else:
            low = middle + 1
    units = taken(low)
    left = total - sum(units.values())
    for a in sorted(nodes):
        if not left:
            break
        if units[a] < caps[a] and counts[a] - units[a] == low and low > 0:
            units[a] += 1
            left -= 1
    if left:
        return None
    return units


def _rising_fill(
    counts: Sequence[int], nodes: Sequence[int], caps: Mapping[int, int], total: int
) -> dict[int, int] | None:
    """`total` units onto the nodes, the one with the fewest ports first, each to its cap."""
    units = dict.fromkeys(nodes, 0)
    left = total
    for a in sorted(nodes, key=lambda node: (counts[node], node)):
        take = min(caps[a], left)
        units[a] = take
        left -= take
    return None if left else units


def _unconnected_variants(
    counts: Sequence[int], hubs: int, on_hubs: int, on_ordinary: int, u1: int
) -> Iterator[tuple[int, ...]]:
    """Placements of the unconnected ports: u1 on one component, the rest at most u1 each."""
    n = len(counts)
    if not on_hubs and not on_ordinary:
        yield (0,) * n
        return
    sides = (tuple(range(hubs)), tuple(range(hubs, n)))
    totals = (on_hubs, on_ordinary)
    holders: list[int] = []
    for nodes, total in zip(sides, totals, strict=True):
        if u1 and total >= u1:
            eligible = [a for a in nodes if counts[a] >= u1]
            for holder in eligible[:1] + eligible[-1:]:
                if holder not in holders:
                    holders.append(holder)
    seen: set[tuple[int, ...]] = set()
    for holder in holders:
        for fill in (_level_fill, _rising_fill):
            units = [0] * n
            for nodes, total in zip(sides, totals, strict=True):
                caps = {a: min(counts[a], u1) for a in nodes}
                left = total
                if holder in caps:
                    units[holder] = u1
                    caps[holder] = 0
                    left -= u1
                placed = fill(counts, nodes, caps, left)
                if placed is None:
                    break
                for a, value in placed.items():
                    units[a] += value
            else:
                result = tuple(units)
                if result not in seen:
                    seen.add(result)
                    yield result


@dataclass(frozen=True)
class _Providers:
    """The provider counts a category admits: 0 (when it has no extras) and lo..hi."""

    zero: bool
    low: int | None
    high: int

    def admits(self, providers: int) -> bool:
        if providers == 0:
            return self.zero
        return self.low is not None and self.low <= providers <= self.high


def _provider_range(reached: Sequence[int], extras: int, bound: int) -> _Providers | None:
    """The closed form: which provider totals one category can hold."""
    plus = sorted(c for c in reached if c > 0)
    if extras < 0 or any(c > bound for c in plus):
        return None
    low: int | None = None
    running = 0
    for q, c in enumerate(plus, start=1):
        running += c
        if running <= q * bound - extras:
            low = q
            break
    return _Providers(extras == 0, low, sum(plus))


@dataclass(frozen=True)
class _Fixed:
    """One connector count's search.

    The port counts (descending), the hubs among them, the fan-outs, U, u1, A and L.
    """

    counts: tuple[int, ...]
    hubs: int
    fan_outs: tuple[int, ...]
    unconnected: int
    u1: int
    connectors: int
    links: int


class _Block(NamedTuple):
    """The block totals one placement of the unconnected ports is tried with.

    C_oo (`ordinary`), HH, the ordinary components' reached ports and each category's extras and
    bound.
    """

    order: int
    hub_to_hub: int
    ordinary_reached: int
    ordinary: Category
    hub: Category


def _orders(n: int, hubs: int, links: int, connectors: int) -> list[int]:
    """The connectors between two ordinary components (C_oo) a search tries, in its order.

    The one the `links` inequality fixes when L > 0, else every one from the most down; with no
    hub, only all A of them.
    """
    half = (n - hubs) // 2
    if links:
        orders = [half + links] if half + links <= connectors else []
    else:
        orders = list(range(min(half, connectors), -1, -1))
    return orders if hubs else [x for x in orders if x == connectors]


def _blocks(
    connectors: int,
    hubs: int,
    ordinary: int,
    hub_reached: int,
    ordinary_reached: int,
    ordinary_kept: int,
) -> Iterator[_Block]:
    """The block totals at one C_oo (`ordinary`) for the hubs' and the ordinary components' ports.

    Each hub-to-hub count HH the bounds admit, from the fewest up, with its categories' extras
    (none negative) and bounds; HH = 0 alone with fewer than two hubs, and none leaving an
    ordinary component's kept reached ports (`ordinary_kept`) above its bound.
    """
    lowest = max(0, hub_reached - connectors + ordinary)
    highest = min(connectors - ordinary, ordinary + connectors - ordinary_reached)
    if hubs < 2:
        highest = min(highest, 0)
    highest = min(highest, connectors - ordinary_kept)
    for hub_to_hub in range(lowest, highest + 1):
        ordinary_extras = ordinary + connectors - ordinary_reached - hub_to_hub
        hub_extras = connectors - ordinary - hub_reached + hub_to_hub
        if ordinary_extras < 0 or hub_extras < 0:
            continue
        yield _Block(
            ordinary,
            hub_to_hub,
            ordinary_reached,
            Category(0, ordinary_extras, connectors - hub_to_hub),
            Category(0, hub_extras, connectors - ordinary),
        )


def _solve_fixed(fixed: _Fixed, budget: Budget, variants: Budget, *, exact: bool) -> ConnectorSolution | None:
    counts, hubs, unconnected, u1 = fixed.counts, fixed.hubs, fixed.unconnected, fixed.u1
    connectors, links = fixed.connectors, fixed.links
    n = len(counts)
    reached = sum(counts) - unconnected
    stars = reached - connectors
    if reached < 0 or stars < 0 or connectors < 0:
        return None
    hub_ports = sum(counts[:hubs])
    ordinary_ports = sum(counts[hubs:])
    hub_room = sum(min(count, u1) for count in counts[:hubs])
    ordinary_room = sum(min(count, u1) for count in counts[hubs:])
    # Whatever the unconnected ports' placement, a component keeps at least its ports beyond u1
    # reached, and `_provider_range` refuses a category one of whose components keeps more than
    # its bound: so an order or a hub-to-hub count above those bounds is skipped whole.
    hub_kept = max((count - min(count, u1) for count in counts[:hubs]), default=0)
    ordinary_kept = max((count - min(count, u1) for count in counts[hubs:]), default=0)
    for ordinary in _orders(n, hubs, links, connectors):
        if hub_kept > connectors - ordinary:
            continue
        for on_hubs in range(max(0, unconnected - ordinary_room), min(unconnected, hub_room) + 1):
            on_ordinary = unconnected - on_hubs
            for block in _blocks(
                connectors,
                hubs,
                ordinary,
                hub_ports - on_hubs,
                ordinary_ports - on_ordinary,
                ordinary_kept,
            ):
                for units in _unconnected_variants(counts, hubs, on_hubs, on_ordinary, u1):
                    if not variants.spend():
                        return None
                    solution = _try_providers(fixed, block, units, budget, exact=exact)
                    if solution is not None or budget.left < 0:
                        return solution
    return None


def _try_providers(
    fixed: _Fixed, block: _Block, units: tuple[int, ...], budget: Budget, *, exact: bool
) -> ConnectorSolution | None:
    """The provider splits of one placement of the unconnected ports, each packed.

    `None` when none packs or the budget runs out (`budget.left` below 0).
    """
    hubs, connectors = fixed.hubs, fixed.connectors
    stars = sum(fixed.counts) - fixed.unconnected - connectors
    reach = [count - u for count, u in zip(fixed.counts, units, strict=True)]
    ordinary_range = _provider_range(reach[hubs:], block.ordinary.extras, block.ordinary.bound)
    hub_range = _provider_range(reach[:hubs], block.hub.extras, block.hub.bound)
    if ordinary_range is None or hub_range is None:
        return None
    first = max(0, block.ordinary_reached - connectors + block.hub_to_hub)
    last = min(block.ordinary_reached - block.order, stars)
    for ordinary_providers in range(first, last + 1):
        hub_providers = stars - ordinary_providers
        if not (ordinary_range.admits(ordinary_providers) and hub_range.admits(hub_providers)):
            continue
        if not budget.spend():
            return None
        packed = _pack(
            reach,
            hubs,
            block.ordinary._replace(providers=ordinary_providers),
            block.hub._replace(providers=hub_providers),
            fixed.fan_outs,
            exact=exact,
        )
        if packed is not None:
            providers, extras, pieces = packed
            to_hubs = ordinary_providers - block.ordinary_reached + connectors - block.hub_to_hub
            return ConnectorSolution(
                units,
                providers,
                extras,
                pieces,
                block.order,
                block.hub_to_hub,
                to_hubs,
                connectors - block.order - block.hub_to_hub - to_hubs,
            )
    return None


# --- packing the stars into the interfaces -------------------------------------------------------


def _pack(
    reach: Sequence[int],
    hubs: int,
    ordinary: Category,
    hub: Category,
    fan_outs: Sequence[int],
    *,
    exact: bool,
) -> Packing | None:
    """Per component (providers, extras) and the stars placed into the interfaces, or `None`.

    The greedy packings first; the exact search only in the second pass of the search (`exact`),
    so that a configuration the greedy packings meet never waits on it.
    """
    stars = ordinary.providers + hub.providers
    if exact:
        return _exact_pack(reach, hubs, ordinary, hub, fan_outs) if stars <= EXACT_STARS else None
    for descending in (True, False):
        packed = _room_pack(reach, hubs, ordinary, hub, fan_outs, descending=descending)
        if packed is not None:
            return packed
    for spread in (True, False):
        packed = _greedy_pack(reach, hubs, ordinary, hub, fan_outs, spread=spread)
        if packed is not None:
            return packed
    return None


def _room_plan(fan_outs: Sequence[int], stars: int, ports: int) -> list[int] | None:
    """The ports each interface gives to stars (0 or at least 2), the rest unconnected.

    The interfaces of one port are emptied; then the smallest ones, as few as needed, so that
    the interfaces left in use number at most `stars` and hold at least `stars` stars of two
    ports. The unconnected ports left go one to each odd interface first (which costs no star),
    then two at a time from the largest.
    """
    spare = sum(fan_outs) - ports
    if spare < 0:
        return None
    ones = [i for i, size in enumerate(fan_outs) if size == 1]
    larger = sorted((i for i, size in enumerate(fan_outs) if size >= 2), key=lambda i: (fan_outs[i], i))
    left_after_ones = spare - len(ones)
    if left_after_ones < 0:
        return None
    for emptied in range(len(larger) + 1):
        left = left_after_ones - sum(fan_outs[i] for i in larger[:emptied])
        if left < 0:
            return None
        used = larger[emptied:]
        room = [0] * len(fan_outs)
        for i in used:
            room[i] = fan_outs[i]
        for i in sorted(used, key=lambda i: (-room[i], i)):
            if left and room[i] % 2 and room[i] >= 3:
                room[i] -= 1
                left -= 1
        for i in sorted(used, key=lambda i: (-room[i], i)):
            while left >= 2 and room[i] >= 4:
                room[i] -= 2
                left -= 2
        if left:
            for i in sorted(used, key=lambda i: (-room[i], i)):
                if room[i] >= 4:
                    room[i] -= 1
                    left -= 1
                    break
        if left:
            continue
        if len(used) <= stars <= sum(room[i] // 2 for i in used):
            return room
    return None


def _room_pack(
    reach: Sequence[int],
    hubs: int,
    ordinary: Category,
    hub: Category,
    fan_outs: Sequence[int],
    *,
    descending: bool,
) -> Packing | None:
    """The interfaces first: split them into stars, then the stars over the components."""
    n = len(reach)
    stars = ordinary.providers + hub.providers
    extras = ordinary.extras + hub.extras
    if not stars:
        return ((0,) * n, (0,) * n, ()) if not extras else None
    room = _room_plan(fan_outs, stars, 2 * stars + extras)
    if room is None:
        return None
    used = sorted((i for i in range(len(room)) if room[i]), key=lambda i: (-room[i], i))
    count = dict.fromkeys(used, 1)
    added = _spread(used, {i: room[i] // 2 for i in used}, stars - len(used), count)
    if added is None:
        return None
    room_extras = {i: room[i] - 2 * added[i] for i in used}
    # The hub stars per interface: at most all but one in each first (the interface stays mixed,
    # so its extras split freely), then whole interfaces; ordered by the interface's extras.
    by_extras = sorted(used, key=lambda i: ((-1 if descending else 1) * room_extras[i], i))
    on_hub = dict.fromkeys(used, 0)
    wanted = hub.providers
    for whole in (False, True):
        for i in by_extras:
            take = min(wanted, added[i] - on_hub[i] - (0 if whole else 1))
            if take > 0:
                on_hub[i] += take
                wanted -= take
    if wanted:
        return None
    forced = sum(room_extras[i] for i in used if on_hub[i] == added[i])
    hub_extra = {i: (room_extras[i] if on_hub[i] == added[i] else 0) for i in used}
    rest = hub.extras - forced
    if rest < 0:
        return None
    for i in by_extras:
        if 0 < on_hub[i] < added[i]:
            take = min(rest, room_extras[i])
            hub_extra[i] += take
            rest -= take
    if rest:
        return None
    pieces_by_side: tuple[list[tuple[int, int]], list[tuple[int, int]]] = ([], [])
    for i in used:
        for side, number, extra in (
            (ORDINARY_SIDE, added[i] - on_hub[i], room_extras[i] - hub_extra[i]),
            (HUB_SIDE, on_hub[i], hub_extra[i]),
        ):
            if number:
                per, more = divmod(extra, number)
                pieces_by_side[side].extend((per + (1 if j < more else 0), i) for j in range(number))
    providers = [0] * n
    extra_of = [0] * n
    pieces: list[ConnectorPiece] = []
    for side, category in ((ORDINARY_SIDE, ordinary), (HUB_SIDE, hub)):
        nodes = _category_nodes(reach, hubs, side)
        extra_room = {a: category.bound - reach[a] for a in nodes}
        for extra, i in sorted(pieces_by_side[side], key=lambda item: (-item[0], item[1])):
            fitting = [a for a in nodes if providers[a] < reach[a] and extra_room[a] >= extra]
            if not fitting:
                return None
            a = max(fitting, key=lambda node: (extra_room[node], -providers[node], -node))
            providers[a] += 1
            extra_of[a] += extra
            extra_room[a] -= extra
            pieces.append(ConnectorPiece(a, i, 1 + extra))
    return tuple(providers), tuple(extra_of), tuple(pieces)


def _category_nodes(reach: Sequence[int], hubs: int, side: int) -> list[int]:
    nodes = range(hubs) if side == HUB_SIDE else range(hubs, len(reach))
    return [a for a in nodes if reach[a] > 0]


def _spread(
    nodes: Sequence[int], caps: Mapping[int, int], total: int, start: Mapping[int, int]
) -> dict[int, int] | None:
    """Round-robin `total` more units over the nodes in order, each up to its cap."""
    values = dict(start)
    left = total
    while left:
        moved = False
        for a in nodes:
            if left and values[a] < caps[a]:
                values[a] += 1
                left -= 1
                moved = True
        if not moved:
            return None
    return values


def _greedy_pack(
    reach: Sequence[int],
    hubs: int,
    ordinary: Category,
    hub: Category,
    fan_outs: Sequence[int],
    *,
    spread: bool,
) -> Packing | None:
    n = len(reach)
    providers = [0] * n
    extras = [0] * n
    for side, (count, extra, bound) in ((ORDINARY_SIDE, ordinary), (HUB_SIDE, hub)):
        nodes = sorted(_category_nodes(reach, hubs, side), key=lambda a: (reach[a], a))
        chosen = nodes[: min(count, len(nodes))]
        if not count:
            if extra:
                return None
            continue
        given = _spread(chosen, {a: reach[a] for a in chosen}, count - len(chosen), dict.fromkeys(chosen, 1))
        if given is None:
            return None
        order = sorted(chosen, key=lambda a: (-given[a], a))
        added = _spread(order, {a: bound - reach[a] for a in chosen}, extra, dict.fromkeys(chosen, 0))
        if added is None:
            return None
        for a in chosen:
            providers[a] = given[a]
            extras[a] = added[a]
    sizes: list[tuple[int, int]] = []
    for a in range(n):
        if not providers[a]:
            continue
        if spread:
            per, more = divmod(extras[a], providers[a])
            sizes.extend((1 + per + (1 if j < more else 0), a) for j in range(providers[a]))
        else:
            sizes.append((1 + extras[a], a))
            sizes.extend((1, a) for _ in range(providers[a] - 1))
    sizes.sort(key=lambda item: (-item[0], item[1]))
    rooms = sorted((size, index) for index, size in enumerate(fan_outs))
    pieces: list[ConnectorPiece] = []
    for edges, a in sizes:
        position = bisect.bisect_left(rooms, (edges + 1, -1))
        if position == len(rooms):
            return None
        size, index = rooms.pop(position)
        bisect.insort(rooms, (size - edges - 1, index))
        pieces.append(ConnectorPiece(a, index, edges))
    return tuple(providers), tuple(extras), tuple(pieces)


class _Exhausted(Exception):
    """The exact packing ran out of steps."""


def _exact_pack(
    reach: Sequence[int],
    hubs: int,
    ordinary: Category,
    hub: Category,
    fan_outs: Sequence[int],
) -> Packing | None:
    """Every split of the providers and the extras and every placement, searched in order."""
    n = len(reach)
    order = [a for a in range(hubs, n)] + [a for a in range(hubs)]
    bound = {a: (hub.bound if a < hubs else ordinary.bound) for a in range(n)}
    side_of = {a: (HUB_SIDE if a < hubs else ORDINARY_SIDE) for a in range(n)}
    reach_left = [[0, 0] for _ in range(n + 1)]
    cap_left = [[0, 0] for _ in range(n + 1)]
    for position in range(n - 1, -1, -1):
        a = order[position]
        side = side_of[a]
        reach_left[position] = list(reach_left[position + 1])
        cap_left[position] = list(cap_left[position + 1])
        if reach[a] > 0:
            reach_left[position][side] += reach[a]
            cap_left[position][side] += bound[a] - reach[a]
    failed: set[tuple[Any, ...]] = set()
    steps = Budget(EXACT_STEPS)
    chosen: list[tuple[int, int, list[tuple[int, int]]]] = []

    def placements(
        rooms: list[int], count: int, extra: int, most: int
    ) -> Iterator[tuple[list[int], list[tuple[int, int]]]]:
        if not steps.spend():
            raise _Exhausted
        if count == 0:
            if extra == 0:
                yield rooms, []
            return
        for size in range(min(extra, most), -1, -1):
            if (count - 1) * size < extra - size:
                break
            tried: set[int] = set()
            for index, room in enumerate(rooms):
                if room < size + 2 or room in tried:
                    continue
                tried.add(room)
                left = list(rooms)
                left[index] -= size + 2
                for rest, placed in placements(left, count - 1, extra - size, size):
                    yield rest, [(index, size + 1), *placed]

    def search(position: int, rooms: list[int], left: tuple[int, int, int, int]) -> bool:
        if not steps.spend():
            raise _Exhausted
        if position == n:
            return left == (0, 0, 0, 0)
        key = (position, tuple(sorted(rooms)), left)
        if key in failed:
            return False
        for side in (0, 1):
            if left[2 * side] > reach_left[position][side] or left[2 * side + 1] > cap_left[position][side]:
                failed.add(key)
                return False
        a = order[position]
        side = side_of[a]
        count_left, extra_left = left[2 * side], left[2 * side + 1]
        options = [(0, 0)]
        if reach[a] > 0:
            options += [
                (count, extra)
                for count in range(min(reach[a], count_left), 0, -1)
                for extra in range(min(bound[a] - reach[a], extra_left), -1, -1)
            ]
        for count, extra in options:
            for rest, placed in placements(rooms, count, extra, extra):
                after = list(left)
                after[2 * side] -= count
                after[2 * side + 1] -= extra
                chosen.append((count, extra, placed))
                if search(position + 1, rest, (after[0], after[1], after[2], after[3])):
                    return True
                chosen.pop()
        failed.add(key)
        return False

    try:
        found = search(0, list(fan_outs), (ordinary.providers, ordinary.extras, hub.providers, hub.extras))
    except _Exhausted:
        return None
    if not found:
        return None
    providers = [0] * n
    extras = [0] * n
    pieces: list[ConnectorPiece] = []
    for a, (count, extra, placed) in zip(order, chosen, strict=True):
        providers[a] = count
        extras[a] = extra
        pieces.extend(ConnectorPiece(a, index, edges) for index, edges in placed)
    return tuple(providers), tuple(extras), tuple(pieces)


# --- the realisation -----------------------------------------------------------------------------


#: A sparse zero-diagonal matrix: (row, column) -> its positive amount.
Matrix = dict[tuple[int, int], int]


def _matrix(outs: Sequence[int], ins: Sequence[int]) -> Matrix:
    """A matrix with these row and column sums and a zero diagonal.

    By max-flow while its middle arcs are at most `MAX_FLOW_ARCS`, else by the greedy.
    """
    rows = sum(1 for amount in outs if amount)
    columns = sum(1 for amount in ins if amount)
    both = sum(1 for out, into in zip(outs, ins, strict=True) if out and into)
    if rows * columns - both <= MAX_FLOW_ARCS:
        return _max_flow_matrix(outs, ins)
    return _greedy_matrix(outs, ins)


class _Live:
    """The first index at or after a position whose amount is still positive.

    The amounts only fall, so a skipped index stays skipped (path-compressed).
    """

    def __init__(self, amounts: list[int]) -> None:
        self.amounts = amounts
        self.following = list(range(1, len(amounts) + 1))

    def first(self, start: int) -> int:
        index, path = start, []
        while index < len(self.amounts) and self.amounts[index] == 0:
            path.append(index)
            index = self.following[index]
        for skipped in path:
            self.following[skipped] = index
        return index

    def first_except(self, excluded: int) -> int:
        index = self.first(0)
        if index == excluded:
            index = self.first(excluded + 1)
        if index >= len(self.amounts):
            raise GenerationError("the connectors cannot be laid between distinct components")
        return index


def _greedy_matrix(outs: Sequence[int], ins: Sequence[int]) -> Matrix:
    """A matrix with these row and column sums and a zero diagonal, one connector at a time.

    It exists exactly when the sums agree and every node's out + in is at most the total S (the
    max-flow cut of `_max_flow_matrix`). The greedy keeps that invariant: it takes the node t of
    the largest out + in and joins it to the first other node with a sum left (t's row when it
    has one, else its column). Only another node k with out + in = S could break the invariant;
    then t, the largest, has S too, every other node is empty, and the other end is k itself.
    The work is the connectors times log n; the matrix holds at most one entry per connector."""
    rows, columns = list(outs), list(ins)
    total = sum(rows)
    if total != sum(columns) or any(out + into > total for out, into in zip(rows, columns, strict=True)):
        raise GenerationError("the connectors cannot be laid between distinct components")
    heap = [(-(out + into), node) for node, (out, into) in enumerate(zip(rows, columns)) if out or into]
    heapq.heapify(heap)
    live_rows, live_columns = _Live(rows), _Live(columns)
    matrix: Matrix = {}
    for _ in range(total):
        while -heap[0][0] != rows[heap[0][1]] + columns[heap[0][1]]:
            heapq.heappop(heap)
        node = heap[0][1]
        if rows[node]:
            row, column = node, live_columns.first_except(node)
        else:
            row, column = live_rows.first_except(node), node
        rows[row] -= 1
        columns[column] -= 1
        matrix[(row, column)] = matrix.get((row, column), 0) + 1
        for changed in (row, column):
            if rows[changed] + columns[changed]:
                heapq.heappush(heap, (-(rows[changed] + columns[changed]), changed))
    return matrix


def _max_flow_matrix(outs: Sequence[int], ins: Sequence[int]) -> Matrix:
    """A matrix with these row and column sums and a zero diagonal (Dinic's max-flow)."""
    size = len(outs)
    source, sink = 2 * size, 2 * size + 1
    graph: list[list[int]] = [[] for _ in range(2 * size + 2)]
    to: list[int] = []
    capacity: list[int] = []

    def arc(u: int, v: int, amount: int) -> int:
        graph[u].append(len(to))
        to.append(v)
        capacity.append(amount)
        graph[v].append(len(to))
        to.append(u)
        capacity.append(0)
        return len(to) - 2

    for i in range(size):
        arc(source, i, outs[i])
        arc(size + i, sink, ins[i])
    middle: dict[tuple[int, int], int] = {}
    for i in range(size):
        for j in range(size):
            if i != j and outs[i] and ins[j]:
                middle[(i, j)] = arc(i, size + j, min(outs[i], ins[j]))
    total = 0
    while True:
        level = [-1] * len(graph)
        level[source] = 0
        queue = deque([source])
        while queue:
            u = queue.popleft()
            for edge in graph[u]:
                if capacity[edge] and level[to[edge]] < 0:
                    level[to[edge]] = level[u] + 1
                    queue.append(to[edge])
        if level[sink] < 0:
            break
        pointer = [0] * len(graph)

        def push(u: int, amount: int) -> int:
            if u == sink:
                return amount
            while pointer[u] < len(graph[u]):
                edge = graph[u][pointer[u]]
                v = to[edge]
                if capacity[edge] and level[v] == level[u] + 1:
                    moved = push(v, min(amount, capacity[edge]))
                    if moved:
                        capacity[edge] -= moved
                        capacity[edge ^ 1] += moved
                        return moved
                pointer[u] += 1
            return 0

        while True:
            moved = push(source, sum(outs) + 1)
            if not moved:
                break
            total += moved
    if total != sum(outs) or sum(outs) != sum(ins):
        raise GenerationError("the connectors cannot be laid between distinct components")
    return {cell: capacity[edge ^ 1] for cell, edge in middle.items() if capacity[edge ^ 1]}


def _transport(supply: Sequence[int], demand: Sequence[int]) -> list[tuple[int, int, int]]:
    """Any matrix with these sums (north-west corner): (row, column, amount) triples."""
    triples: list[tuple[int, int, int]] = []
    rows, columns = list(supply), list(demand)
    i = j = 0
    while i < len(rows) and j < len(columns):
        amount = min(rows[i], columns[j])
        if amount:
            triples.append((i, j, amount))
        rows[i] -= amount
        columns[j] -= amount
        if rows[i] == 0:
            i += 1
        else:
            j += 1
    return triples


def _edges(solution: ConnectorSolution, counts: Sequence[int], hubs: int) -> list[list[int]]:
    """Per provider component, the components its connectors end on, ascending."""
    n = len(counts)
    reach = [count - u for count, u in zip(counts, solution.unconnected, strict=True)]
    ins = [reach[a] - solution.providers[a] for a in range(n)]
    outs = [solution.providers[a] + solution.extras[a] for a in range(n)]
    ordinary = list(range(hubs, n))
    hub_nodes = list(range(hubs))
    near = _matrix(
        [outs[a] for a in ordinary] + [solution.from_hubs],
        [ins[a] for a in ordinary] + [solution.to_hubs],
    )
    far = _matrix(
        [outs[h] for h in hub_nodes] + [solution.to_hubs],
        [ins[h] for h in hub_nodes] + [solution.from_hubs],
    )
    m = len(ordinary)
    count: Matrix = {}
    for (i, j), amount in near.items():
        if i < m and j < m:
            count[(ordinary[i], ordinary[j])] = amount
    for (i, j), amount in far.items():
        if i < hubs and j < hubs:
            count[(hub_nodes[i], hub_nodes[j])] = amount
    for i, j, amount in _transport(
        [near.get((i, m), 0) for i in range(m)], [far.get((hubs, j), 0) for j in range(hubs)]
    ):
        count[(ordinary[i], hub_nodes[j])] = count.get((ordinary[i], hub_nodes[j]), 0) + amount
    for i, j, amount in _transport(
        [far.get((i, hubs), 0) for i in range(hubs)], [near.get((m, j), 0) for j in range(m)]
    ):
        count[(hub_nodes[i], ordinary[j])] = count.get((hub_nodes[i], ordinary[j]), 0) + amount
    targets: list[list[int]] = [[] for _ in range(n)]
    for (a, b), amount in sorted(count.items()):
        targets[a].extend([b] * amount)
    return targets


#: The direction ranks of a port record: a component's provide ports sort before its require
#: ports of the same interface.
_PROVIDE = 0
_REQUIRE = 1


class _PortRecord(NamedTuple):
    """One port of a component before it is named.

    Its interface (room), its direction rank, the order it was made in and its key (the star, and
    the slot of a require port).
    """

    room: int
    rank: int
    sequence: int
    key: tuple[int, ...]


def build_connectors(
    spec: ConnectorSpec,
    counts: Sequence[int],
    fan_outs: Sequence[int],
    interfaces: Sequence[AnyInterface],
    component_names: Sequence[str],
    connector_names: Sequence[str],
    search: Search = FULL_SEARCH,
    *,
    connectable: int | None = None,
    unconnected: Sequence[int] | None = None,
    solution: ConnectorSolution | None = None,
) -> ConnectorPlan:
    """The ports and the connectors of a feasible configuration.

    `interfaces` names the named interfaces in fan-out order (`interfaces[i]` holds `fan_outs[i]`
    ports); `component_names` names the components in count order; `connector_names` is the
    identifier plan's; `search` is the one the shape decided with.
    Only the first `connectable` fan-outs (every one when `None`) hold stars: the others are the
    external interface stubs', whose ports are all unconnected.
    With `solution` (`star_solution` or `solve_open`'s) nothing is searched: its stars, blocks and
    open ports are laid as they are, `unconnected` (when given) the unconnected placement within
    its open ports, which the post-check holds to U and u1; a star on an interface stub is a
    `GenerationError`. Without it the search decides the construction.
    """
    stars_in = tuple(fan_outs if connectable is None else fan_outs[:connectable])
    if solution is not None:
        if any(piece.room >= len(stars_in) for piece in solution.pieces):
            raise GenerationError("the given solution places a star on an interface stub")
    else:
        solution = _solve(spec, counts, stars_in, search)
    if solution is None:
        raise GenerationError("the connectors were refused after feasibility")
    n = len(counts)
    hubs = hub_count(counts)
    targets = _edges(solution, counts, hubs)
    records: list[list[_PortRecord]] = [[] for _ in range(n)]
    used = [0] * len(fan_outs)
    links: list[tuple[int, int, int, int]] = []
    cursor = [0] * n
    sequence = 0
    pieces = sorted(solution.pieces, key=lambda p: (p.node, p.room, -p.edges))
    for number, piece in enumerate(pieces):
        records[piece.node].append(_PortRecord(piece.room, _PROVIDE, sequence, (number,)))
        sequence += 1
        used[piece.room] += piece.edges + 1
        ends = targets[piece.node][cursor[piece.node] : cursor[piece.node] + piece.edges]
        cursor[piece.node] += piece.edges
        if len(ends) != piece.edges:
            raise GenerationError("the connectors do not fill the stars")
        for slot, target in enumerate(ends):
            records[target].append(_PortRecord(piece.room, _REQUIRE, sequence, (number, slot)))
            links.append((piece.node, number, target, slot))
            sequence += 1
    if cursor != [len(t) for t in targets]:
        raise GenerationError("the stars do not take every connector")
    spare = [fan_out - taken for fan_out, taken in zip(fan_outs, used, strict=True)]
    owners = [a for a in range(n) for _ in range(solution.unconnected[a])]
    if sum(spare) != len(owners) or min(spare, default=0) < 0:
        raise GenerationError("the interfaces do not hold the unconnected ports")
    rooms = [room for room, amount in enumerate(spare) for _ in range(amount)]
    for index, (owner, room) in enumerate(zip(owners, rooms, strict=True)):
        records[owner].append(_PortRecord(room, _REQUIRE, sequence, (-1, index)))
        sequence += 1
    names: dict[tuple[int, tuple[int, ...]], str] = {}
    ports: list[tuple[Port, ...]] = []
    for a in range(n):
        ordered = sorted(records[a])
        component_ports: list[Port] = []
        for position, record in enumerate(ordered, start=1):
            name = f"P{position}"
            names[(a, record.key)] = name
            direction = "provide" if record.rank == _PROVIDE else "require"
            component_ports.append(Port(name, direction, interfaces[record.room].name))
        ports.append(tuple(component_ports))
    if len(links) != len(connector_names):
        raise GenerationError("the connectors do not match the identifier plan")
    connectors = tuple(
        Connector(
            name,
            interfaces[pieces[number].room].kind,
            ConnectorEndpoint(component_names[provider], names[(provider, (number,))]),
            ConnectorEndpoint(component_names[target], names[(target, (number, slot))]),
        )
        for name, (provider, number, target, slot) in zip(connector_names, links, strict=True)
    )
    reached = _reached(connectors)
    open_ports = tuple(
        tuple(port.name for port in component_ports if (name, port.name) not in reached)
        for name, component_ports in zip(component_names, ports, strict=True)
    )
    centres = tuple(
        (a, port.name)
        for a, (name, component_ports) in enumerate(zip(component_names, ports, strict=True))
        for port in component_ports
        if port.direction == "provide" and (name, port.name) in reached
    )
    plan = ConnectorPlan(tuple(ports), connectors, open_ports, centres)
    _verify(
        plan,
        spec,
        counts,
        fan_outs,
        interfaces,
        component_names,
        solution.unconnected,
        solution.unconnected if unconnected is None else unconnected,
    )
    return plan


def _reached(connectors: Sequence[Connector]) -> set[tuple[str | None, str]]:
    """The (component, port) ends the connectors reach."""
    return {(c.source.swc, c.source.port) for c in connectors} | {(c.target.swc, c.target.port) for c in connectors}


def _verify(
    plan: ConnectorPlan,
    spec: ConnectorSpec,
    counts: Sequence[int],
    fan_outs: Sequence[int],
    interfaces: Sequence[AnyInterface],
    component_names: Sequence[str],
    units: Sequence[int],
    unconnected: Sequence[int],
) -> None:
    """Recompute the connector leaves from the ports and connectors, as SWCraft's pass does.

    The ports no connector reaches are the placement's `units` per component, and `unconnected`
    (per component, at most its `units`; equal to them with no delegation) the unconnected ones
    among them, whose sum is U and whose most is u1 whatever the delegations.
    """
    reached = _reached(plan.connectors)
    open_ports = [
        sum(1 for port in ports if (name, port.name) not in reached)
        for name, ports in zip(component_names, plan.ports, strict=True)
    ]
    fan_out = {i.name: 0 for i in interfaces[: len(fan_outs)]}
    for ports in plan.ports:
        for port in ports:
            fan_out[port.interface] = fan_out.get(port.interface, 0) + 1
    ordinary = set(component_names[hub_count(counts) :])
    between = sum(1 for c in plan.connectors if c.source.swc in ordinary and c.target.swc in ordinary)
    connectors = spec.connectors
    if (
        [len(ports) for ports in plan.ports] != list(counts)
        or [fan_out[i.name] for i in interfaces[: len(fan_outs)]] != list(fan_outs)
        or open_ports != list(units)
        or any(u > o for u, o in zip(unconnected, units, strict=True))
        or sum(unconnected) != spec.unconnected
        or max(unconnected, default=0) != spec.max_on_one
        or (connectors is not None and len(plan.connectors) != connectors)
        or max(0, between - len(ordinary) // 2) != spec.links
    ):
        raise GenerationError("the connectors do not reproduce the stated connector leaves")
