"""Components: the port distribution, hubs, runnables and declared levels, in closed form.

Notation: n = `layers.swc`, k = `len(hubs)`, m = n - k the non-hubs, T = `components.ports.total`;
the median is over all n per-component port counts (`statistics.median`: an even count takes the
mean of the two middle ones).

The checks before generation, each refused at its leaf with a closed class:

- `order`: `components.ports.top` is descending and has `min(8, n)` entries; `components.hubs` is
  descending and `hubs[:8]` is a prefix of `top`;
- `hub-rule`: k >= n/2 with k > 0 (the median must lie among the non-hubs), or no median exists
  that puts every non-hub at or below twice it and every hub above it;
- `port-total`: the ports left for the free non-hubs, S = T - sum(hubs) - sum(top[k:]), is below 0
  or above what they can hold;
- `sum`: the declared levels add up to more than n (refused at the level whose running sum, in
  the order QM, A, B, C, D, first exceeds n), or runnables are stated for no component.

The hubs are placed exactly, then the non-hub part of `top`, then the free non-hubs summing to S,
each at most `top[-1]` when `top` is stated and n >= 8. Twice the median, t, is searched upwards
below the smallest hub, and the first t whose slots can hold S is chosen; the free values are
filled level, the smallest first. An unstated `top` is derived; an unstated `total` defaults to the
smallest admitted total giving every free non-hub two ports on average, and the shape tries others,
so an unstated leaf is never refused by a default alone. Unstated runnables are one per component,
or B when the busy tasks are more (`tasks.runnable_count`); an unstated level counts 0. Runnables
are spread evenly, the largest components first; declared levels go in the order QM, A, B, C, D
from the smallest component up.

The checks are necessary conditions, decided exactly only within the median window scanned; a
built placement that mismatches them is a `GenerationError`, never a passport refusal.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..errors import GenerationError
from ..passport import ASIL_LEVELS, TOP, Refusal
from ..preset import ASIL
from .common import COUNT_CEILING, ceil_div, hub_count, level_fill, stated_count
from .tasks import runnable_count
from .vocabulary import (
    HUB_RULE,
    HUBS,
    LAYER_SWC,
    LEVEL_LEAVES,
    ORDER,
    PORT_TOTAL,
    PORTS_TOP,
    PORTS_TOTAL,
    RUNNABLES,
    SUM,
)

#: The free non-hubs' preferred average when the total is unstated: one provide, one require.
DEFAULT_PORTS_PER_COMPONENT = 2
#: The median candidates `port_placements` scans for other placements of one total.
PLACEMENT_SCAN = 64


@dataclass(frozen=True)
class ComponentShape:
    """One component's shape: its port count, its runnable count and its declared level."""

    ports: int
    runnables: int
    level: ASIL


@dataclass(frozen=True)
class PortSpec:
    """The stated port leaves: n, the hubs, and `top` and the total when stated."""

    n: int
    hubs: tuple[int, ...]
    top: tuple[int, ...] | None
    total: int | None

    @property
    def k(self) -> int:
        return len(self.hubs)

    @property
    def placed(self) -> tuple[int, ...]:
        """The non-hub part of `top`, placed exactly (empty without `top` or with k >= 8)."""
        return self.top[self.k :] if self.top is not None else ()

    @property
    def free(self) -> int:
        return self.n - self.k - len(self.placed)

    @property
    def placed_sum(self) -> int:
        """The ports the hubs and the non-hub part of `top` place exactly."""
        return sum(self.hubs) + sum(self.placed)

    @property
    def largest(self) -> int | None:
        """The largest stated count: `top`'s first, else the first hub's; `None` when neither is stated."""
        if self.top:
            return self.top[0]
        return self.hubs[0] if self.hubs else None

    @property
    def top_cap(self) -> int | None:
        """The bound `top[-1]` puts on every free value (`None` when it puts none)."""
        if self.top is None or self.n < TOP or not self.top:
            return None
        return self.top[-1]

    @property
    def free_bound(self) -> int | None:
        """The most one free value can hold (`None` when nothing bounds it)."""
        bounds = [] if self.top_cap is None else [self.top_cap]
        if self.k:
            bounds.append(min(self.hubs) - 1)
        return min(bounds) if bounds else None


@dataclass(frozen=True)
class _Candidate:
    """A median candidate with one fitting of the placed values: the free slots it leaves."""

    t: int
    a: int
    b: int
    lower: int
    lower_hi: int
    centers: tuple[int, ...]
    upper: int
    upper_lo: int
    upper_hi: int
    #: Upper-half values placed exactly (the u1 candidate's one); empty otherwise.
    fixed: tuple[int, ...] = ()

    @property
    def low(self) -> int:
        return sum(self.centers) + self.upper * self.upper_lo + sum(self.fixed)

    @property
    def high(self) -> int:
        return self.lower * self.lower_hi + sum(self.centers) + self.upper * self.upper_hi + sum(self.fixed)


#: A placement: the median candidate and the free sum it takes.
Placement = tuple[_Candidate, int]


def port_spec(stated: Mapping[str, Any]) -> PortSpec:
    """The port leaves of a stated passport (dotted paths), as the placement reads them."""
    top = stated.get(PORTS_TOP)
    return PortSpec(
        n=int(stated[LAYER_SWC]),
        hubs=tuple(stated[HUBS]),
        top=None if top is None else tuple(top),
        total=stated.get(PORTS_TOTAL),
    )


def port_most_stated(spec: PortSpec) -> bool:
    """Whether the stated port leaves decide `port_range`'s most, not the count ceiling.

    They do with a stated total (the ceiling itself included), every count placed, or a bound on
    the free counts that stays at or below the ceiling.
    """
    if spec.total is not None or spec.n == 0 or spec.free == 0:
        return True
    bound = spec.free_bound
    return bound is not None and spec.placed_sum + spec.free * bound <= COUNT_CEILING


def port_range(spec: PortSpec) -> tuple[int, int]:
    """The least and the most total the port leaves admit, the most at the count ceiling."""
    if spec.total is not None:
        return spec.total, spec.total
    placed = spec.placed_sum
    if spec.n == 0:
        return 0, 0
    if spec.free == 0:
        return placed, placed
    bound = spec.free_bound
    most = COUNT_CEILING if bound is None else min(COUNT_CEILING, placed + spec.free * bound)
    # Placed ports above the ceiling are refused as `ceiling` before the shape is asked; a
    # direct caller then gets the placed ports as the range, never an empty one.
    return placed, max(placed, most)


def _descending(values: Sequence[int]) -> bool:
    return all(x >= y for x, y in zip(values, values[1:], strict=False))


def _order_refusals(spec: PortSpec) -> list[Refusal]:
    refusals: list[Refusal] = []
    if spec.top is not None:
        if not _descending(spec.top):
            refusals.append(Refusal(PORTS_TOP, ORDER, "is not descending"))
        elif len(spec.top) != min(TOP, spec.n):
            refusals.append(
                Refusal(
                    PORTS_TOP,
                    ORDER,
                    f"has {len(spec.top)} entries; {spec.n} components give min(8, n) = {min(TOP, spec.n)}",
                )
            )
    if not _descending(spec.hubs):
        refusals.append(Refusal(HUBS, ORDER, "is not descending"))
    elif spec.top is not None and list(spec.top[: min(TOP, spec.k)]) != list(spec.hubs[:TOP]):
        refusals.append(Refusal(HUBS, ORDER, "its first eight entries are not a prefix of top"))
    return refusals


def _t_window(spec: PortSpec, s: int | None) -> tuple[int, int]:
    """The window of t = 2 x median the search scans."""
    low = max(spec.placed, default=0)
    if spec.free and s is not None:
        low = max(low, ceil_div(s, spec.free))
    bounds: list[int] = []
    if spec.k:
        bounds.append(min(spec.hubs) - 1)
    if spec.total is not None:
        bounds.append(2 * spec.total // ceil_div(spec.n, 2))
    if not bounds:
        bounds.append(2 * spec.top[0] if spec.top else 2 * DEFAULT_PORTS_PER_COMPONENT)
    high = min(bounds)
    if s is not None:
        # Above twice the larger of S and every placed value, every central and upper slot is
        # free and at least half of t, so the free sum exceeds S.
        high = min(high, 2 * max(s, max(spec.placed, default=0)) + 1)
    return low, high


def _splits(n: int, t: int, placed: Sequence[int], every: bool = False) -> Iterator[tuple[int, int]]:
    """The central values (a, b) of a median t / 2 worth trying, smallest b first.

    For even n, how the placed values fit changes only where a or b crosses one of them; between
    two such points a larger b only raises the free slots' least sum and lowers their most, so the
    smallest b of each stretch admits every free sum the stretch admits. Only those b are tried,
    which decides exactly what trying every split would; with `every`, every b is (other splits
    place other counts, which the shape's derivation tries).
    """
    if n % 2:
        if t % 2 == 0:
            yield t // 2, t // 2
        return
    first = ceil_div(t, 2)
    if every:
        for b in range(first, t + 1):
            yield t - b, b
        return
    points = {first}
    for value in placed:
        points.update((value, value + 1, t - value, t - value + 1))
    for b in sorted(point for point in points if first <= point <= t):
        yield t - b, b


def _fittings(
    placed: Sequence[int], lower: int, centers: tuple[int, ...], upper: int, a: int, b: int, t: int
) -> Iterator[tuple[int, tuple[int, ...], int]]:
    """Every way the placed values fill slots; yields the slots each way leaves free."""
    seen: set[tuple[int, tuple[int, ...], int]] = set()

    def walk(
        index: int, lower: int, centers: tuple[int, ...], upper: int
    ) -> Iterator[tuple[int, tuple[int, ...], int]]:
        if index == len(placed):
            state = (lower, centers, upper)
            if state not in seen:
                seen.add(state)
                yield state
            return
        value = placed[index]
        if upper and b <= value <= t:
            yield from walk(index + 1, lower, centers, upper - 1)
        if value in centers:
            position = centers.index(value)
            yield from walk(index + 1, lower, centers[:position] + centers[position + 1 :], upper)
        if lower and value <= a:
            yield from walk(index + 1, lower - 1, centers, upper)

    yield from walk(0, lower, centers, upper)


def _most_at(spec: PortSpec, t: int) -> int:
    """An upper bound of the free sum any split of t admits: every non-hub slot at its most."""
    half = spec.n // 2
    cap = t if spec.top_cap is None else min(t, spec.top_cap)
    lower_cap = min(t // 2, cap)
    if spec.n % 2:
        return half * lower_cap + lower_cap + (half - spec.k) * cap
    return (half - 1) * lower_cap + min(t, 2 * cap) + (half - 1 - spec.k) * cap


def _candidates(spec: PortSpec, s: int | None, every: bool = False, least_t: int = 0) -> Iterator[_Candidate]:
    n, k = spec.n, spec.k
    cap = spec.top_cap
    low, high = _t_window(spec, s)
    low = max(low, least_t)
    if s is not None:
        # The most the free values hold rises with t, so the first t that can hold S is found by
        # bisection; the scan starts there, so its work does not grow with the stated total.
        top = high + 1
        while low < top:
            middle = (low + top) // 2
            if _most_at(spec, middle) >= s:
                top = middle
            else:
                low = middle + 1
    half = n // 2
    for t in range(low, high + 1):
        if s is not None and s > _most_at(spec, t):
            continue
        for a, b in _splits(n, t, spec.placed, every):
            centers: tuple[int, ...]
            if n % 2:
                lower, centers, upper = half, (a,), half - k
            else:
                lower, centers, upper = half - 1, (a, b), half - 1 - k
            lower_hi = a if cap is None else min(a, cap)
            upper_hi = t if cap is None else min(t, cap)
            for free_lower, free_centers, free_upper in _fittings(spec.placed, lower, centers, upper, a, b, t):
                if cap is not None and any(value > cap for value in free_centers):
                    continue
                if free_upper and b > upper_hi:
                    continue
                yield _Candidate(t, a, b, free_lower, lower_hi, free_centers, free_upper, b, upper_hi)


def choose_ports(spec: PortSpec) -> Placement | Refusal:
    """The first candidate and the free sum it takes, or the refusal of the port leaves.

    With the total unstated, the free sum is the placement's own default.
    """
    if spec.k and 2 * spec.k >= spec.n:
        return Refusal(
            HUBS,
            HUB_RULE,
            f"states {spec.k} hubs among {spec.n} components; the median lies among the "
            "non-hubs only when fewer than half are hubs",
        )
    placed_sum = spec.placed_sum
    if spec.total is not None:
        s = spec.total - placed_sum
        bound = spec.free_bound
        most = 0 if spec.free == 0 else (None if bound is None else spec.free * bound)
        if s < 0 or (most is not None and s > most):
            return Refusal(
                PORTS_TOTAL,
                PORT_TOTAL,
                f"states {spec.total}; the hubs and top place {placed_sum}, and the "
                f"{spec.free} remaining components hold between 0 and "
                f"{'any number of' if most is None else most} ports",
            )
        for candidate in _candidates(spec, s):
            if candidate.low <= s <= candidate.high:
                return candidate, s
    else:
        candidates = list(_candidates(spec, None))
        preferred = DEFAULT_PORTS_PER_COMPONENT * spec.free
        reachable = [max(c.low, preferred) for c in candidates if max(c.low, preferred) <= c.high]
        target = min(reachable) if reachable else min((c.low for c in candidates), default=None)
        for candidate in candidates:
            if target is not None and candidate.low <= target <= candidate.high:
                return candidate, target
    return Refusal(
        HUBS,
        HUB_RULE,
        "no median puts every non-hub at or below twice it and every stated hub above it",
    )


def _u1_candidate(spec: PortSpec, s: int, on_one: int) -> Placement | None:
    """The u1 candidate: one free component at exactly u1 = `on_one` ports in the upper half.

    The others are level-filled in their slots, on the first median candidate (smallest t first,
    from t = u1, every split) whose slots hold it and the free sum; `None` when none does within
    the scanned window (u1 above the bound the stated counts put on a free one, or no median with
    u1 at or below twice it).
    """
    bound = spec.free_bound
    if spec.free == 0 or on_one <= 0 or (bound is not None and on_one > bound):
        return None
    for candidate in _candidates(spec, s, every=True, least_t=on_one):
        if not candidate.upper or not candidate.upper_lo <= on_one <= candidate.upper_hi:
            continue
        fixed = replace(candidate, upper=candidate.upper - 1, fixed=(on_one,))
        if fixed.low <= s <= fixed.high:
            return fixed, s
    return None


def port_placements(spec: PortSpec, most: int, on_one: int = 0) -> list[Placement]:
    """Up to `most` placements of a stated total, as `choose_ports` takes them.

    Its own first, then other candidates whose interval holds the free sum, each with distinct
    counts, ordered by the largest count they place (the larger first) and, when there are more,
    picked evenly over that order from the most uneven to the least. Empty when the port leaves
    are refused.

    With u1 = `on_one` above the largest count of `choose_ports`'s placement, which is then
    refused at u1 whatever follows, the u1 candidate (`_u1_candidate`) takes its place
    (position 0), when it exists; the list keeps its length. The alternatives are not reached:
    one whose own largest count is below u1 is refused."""
    chosen = choose_ports(spec)
    if isinstance(chosen, Refusal) or spec.total is None:
        return []
    s = chosen[1]
    seen = {place_ports(spec, chosen)}
    if on_one > max(place_ports(spec, chosen), default=0):
        u1_placement = _u1_candidate(spec, s, on_one)
        if u1_placement is not None:
            chosen = u1_placement
            seen.add(place_ports(spec, chosen))
    others: list[tuple[int, int, tuple[_Candidate, int]]] = []
    for order, candidate in enumerate(_candidates(spec, s, every=True)):
        if order >= PLACEMENT_SCAN:
            break
        if not candidate.low <= s <= candidate.high:
            continue
        counts = place_ports(spec, (candidate, s))
        if counts not in seen:
            seen.add(counts)
            others.append((-max(counts, default=0), order, (candidate, s)))
    others.sort(key=lambda item: (item[0], item[1]))
    if len(others) >= most:
        # Spread over the unevenness: the most uneven, the least, and evenly between.
        span = len(others) - 1
        picks = sorted({(span * step) // max(1, most - 2) for step in range(most - 1)})
        others = [others[index] for index in picks]
    return [chosen, *(item[2] for item in others)][:most]


def place_ports(spec: PortSpec, chosen: Placement | Refusal | None = None) -> tuple[int, ...]:
    """The n port counts, descending, of a feasible port specification.

    `chosen` is its `choose_ports`, when the caller has it.
    """
    if spec.n == 0:
        return ()
    if chosen is None:
        chosen = choose_ports(spec)
    if isinstance(chosen, Refusal):
        raise GenerationError(f"the port placement was refused after feasibility: {chosen.leaf}")
    candidate, s = chosen
    slots = (
        [(0, candidate.lower_hi)] * candidate.lower
        + [(value, value) for value in candidate.centers]
        + [(candidate.upper_lo, candidate.upper_hi)] * candidate.upper
        + [(value, value) for value in candidate.fixed]
    )
    free = level_fill([low for low, _ in slots], [high for _, high in slots], s)
    counts = sorted([*spec.hubs, *spec.placed, *free], reverse=True)
    if (
        list(counts[: hub_count(counts)]) != list(spec.hubs)
        or (spec.top is not None and counts[:TOP] != list(spec.top))
        or (spec.total is not None and sum(counts) != spec.total)
    ):
        raise GenerationError("the port placement does not reproduce the stated port leaves")
    return tuple(counts)


def _levels(stated: Mapping[str, Any]) -> list[int]:
    return [stated_count(stated, leaf) for leaf in LEVEL_LEAVES]


def component_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    """Every port-distribution refusal of a stated passport, in no particular order."""
    spec = port_spec(stated)
    refusals = _order_refusals(spec)
    if not refusals and spec.n == 0:
        if spec.k:
            refusals.append(Refusal(HUBS, HUB_RULE, f"states {spec.k} hubs for no component"))
        if spec.total:
            refusals.append(Refusal(PORTS_TOTAL, PORT_TOTAL, f"states {spec.total} ports for no component"))
    elif not refusals:
        chosen = choose_ports(spec)
        if isinstance(chosen, Refusal):
            refusals.append(chosen)
    running = 0
    for leaf, count in zip(LEVEL_LEAVES, _levels(stated), strict=True):
        running += count
        if count and running > spec.n:
            refusals.append(
                Refusal(
                    leaf,
                    SUM,
                    f"brings the declared levels to {running}, above the {spec.n} components",
                )
            )
            break
    if spec.n == 0 and stated.get(RUNNABLES):
        refusals.append(Refusal(RUNNABLES, SUM, f"states {stated[RUNNABLES]} runnables for no component"))
    return refusals


def build_components(stated: Mapping[str, Any], counts: Sequence[int]) -> tuple[ComponentShape, ...]:
    """The component shapes of a feasible stated passport, largest first (deterministic).

    The port counts are those its shape decided (`shape.py`).
    """
    n = len(counts)
    # Stated, else max(n, B): one per component, more when the busy tasks need them.
    total_runnables = runnable_count(stated) if n else 0
    per, extra = divmod(total_runnables, n) if n else (0, 0)
    levels: list[ASIL] = [ASIL.ABSENT] * n
    position = n - 1
    for level, count in zip(ASIL_LEVELS, _levels(stated), strict=True):
        for _ in range(count):
            levels[position] = ASIL(level)
            position -= 1
    return tuple(
        ComponentShape(ports, per + (1 if index < extra else 0), levels[index]) for index, ports in enumerate(counts)
    )
