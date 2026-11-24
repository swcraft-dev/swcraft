"""The helpers the specimen builder's modules share.

Integer division rounded up, the level fill, the hubs by the pass's definition, the count ceiling
and its sentence, a stated count and a spending budget.

The passport pass keeps its own restatement of the hubs, so it stays independent of the builder it
checks.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import GenerationError

#: The most of anything (elements of a layer, ports, runnables, connectors, a list entry) this
#: version builds; above it a count is refused as `ceiling` before any work grows with it
#: (`feasibility.py`), and the shape derives no total above it (`shape.py`).
COUNT_CEILING = 10_000
#: The tail every `ceiling` refusal's sentence ends with.
CEILING_SENTENCE = f"this version builds at most {COUNT_CEILING:,} of anything"


def stated_count(stated: Mapping[str, Any], leaf: str) -> int:
    """A count leaf's stated value, 0 when the passport leaves it unstated."""
    return int(stated.get(leaf, 0))


class Budget:
    """What a search may still try: `limit` units, each `spend` taking one."""

    def __init__(self, limit: int) -> None:
        self.left = limit

    def spend(self) -> bool:
        """Take one unit; whether the budget held it."""
        self.left -= 1
        return self.left >= 0


def stated_connector_count(assembly: int | None, layer: int | None) -> int | None:
    """A, the one definition `connectors.ConnectorSpec` and `interfaces` read.

    `connectors.assembly` when stated, else `layers.connectors`, else unstated (`None`).
    """
    return assembly if assembly is not None else layer


def ceil_div(value: int, divisor: int) -> int:
    """`value / divisor` rounded up (`divisor` > 0)."""
    return -(-value // divisor)


def link_bounds(n: int, hubs: int, links: int) -> tuple[int, int | None]:
    """The least and the most connectors (`None`: no most) the `links` inequality admits.

    For n components of which `hubs` are hubs and L = `links`: with no hub every connector joins
    two ordinary components, so A = m // 2 + L when L > 0 and A <= m // 2 when L = 0; with a hub,
    m // 2 + L <= A when L > 0 (m = n - hubs). It reads no port count, so the stated leaves
    decide it alone.
    """
    half = (n - hubs) // 2
    if links:
        return half + links, None if hubs else half + links
    return 0, None if hubs else half


def hub_count(counts: Sequence[int]) -> int:
    """The components above twice the median count, as SWCraft's pass takes the hubs."""
    if not counts:
        return 0
    threshold = 2 * statistics.median(counts)
    return sum(1 for count in counts if count > threshold)


def level_fill(lows: Sequence[int], highs: Sequence[int], total: int) -> list[int]:
    """Values within their bounds summing to `total`, raised level by level.

    The earliest slot goes first on the last level: the level L is the highest at which every
    value clamped to [low, min(L, high)] sums to at most `total`, and the rest go one each to the
    earliest values that can still rise. Closed form; the work is linear in the slots, not in the
    values.
    """
    if not lows:
        if total:
            raise GenerationError("the values cannot hold their total")
        return []
    if not sum(lows) <= total <= sum(max(low, high) for low, high in zip(lows, highs, strict=True)):
        raise GenerationError("the values cannot hold their total")

    def at(level: int) -> list[int]:
        return [max(low, min(level, high)) for low, high in zip(lows, highs, strict=True)]

    low, high = min(lows), max(max(lows), max(highs))
    while low < high:
        middle = (low + high + 1) // 2
        if sum(at(middle)) <= total:
            low = middle
        else:
            high = middle - 1
    values = at(low)
    remaining = total - sum(values)
    for index, (value, bound) in enumerate(zip(values, highs, strict=True)):
        if not remaining:
            break
        if value == low and bound > low:
            values[index] += 1
            remaining -= 1
    if remaining:
        raise GenerationError("the values cannot hold their total")
    return values
