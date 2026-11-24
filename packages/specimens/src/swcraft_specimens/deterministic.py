"""Small, fixed, explicitly passed deterministic random primitives."""

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")
MASK64 = (1 << 64) - 1
GAMMA = 0x9E3779B97F4A7C15


class SplitMix64:
    """SplitMix64 with a 64-bit masked state and rejection-sampled bounds."""

    def __init__(self, seed: int) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError("seed must be an integer")
        self.state = seed & MASK64

    def next_u64(self) -> int:
        self.state = (self.state + GAMMA) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return (z ^ (z >> 31)) & MASK64

    def bounded(self, bound: int) -> int:
        if isinstance(bound, bool) or not isinstance(bound, int) or bound <= 0 or bound > (1 << 64):
            raise ValueError("bound must be an integer in 1..2**64")
        threshold = ((1 << 64) - bound) % bound
        while True:
            value = self.next_u64()
            if value >= threshold:
                return value % bound


def weighted_choice(rng: SplitMix64, items: Sequence[T], weights: Sequence[int]) -> T:
    if not items or len(items) != len(weights):
        raise ValueError("items and weights must be non-empty and have equal length")
    if any(isinstance(weight, bool) or not isinstance(weight, int) or weight < 0 for weight in weights):
        raise ValueError("weights must be non-negative integers")
    total = sum(weights)
    if total <= 0:
        raise ValueError("weight total must be positive")
    draw = rng.bounded(total)
    for item, weight in zip(items, weights):
        if draw < weight:
            return item
        draw -= weight
    raise AssertionError("weighted choice fell through")


def shuffle(rng: SplitMix64, items: Sequence[T]) -> list[T]:
    result = list(items)
    for index in range(len(result) - 1, 0, -1):
        other = rng.bounded(index + 1)
        result[index], result[other] = result[other], result[index]
    return result


def sample_without_replacement(rng: SplitMix64, items: Sequence[T], count: int) -> list[T]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > len(items):
        raise ValueError("count must be an integer between zero and the input length")
    return shuffle(rng, items)[:count]


def uneven_partition(total: int, parts: int, rng: SplitMix64) -> list[int]:
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise ValueError("total must be a positive integer")
    if isinstance(parts, bool) or not isinstance(parts, int) or parts <= 0 or parts > total:
        raise ValueError("parts must be a positive integer no greater than total")
    result = [1] * parts
    remaining = total - parts
    for _ in range(remaining):
        result[rng.bounded(parts)] += 1
    return result
