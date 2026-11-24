"""How a function's running time grows with its input: the form a test of "this is one linear pass" takes.

A test never asserts a number of seconds for a complexity claim: a loaded runner makes any such bound flake.
It times the same call at two input scales on the same machine, moments apart, and asserts the ratio
(:func:`growth` below :data:`LINEAR_GROWTH`), so load slows both timings and cancels out. The unit timed is the
function the claim is about, never a caller whose own fixed or super-linear costs (a tokeniser, a process start)
would sit in the ratio.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

#: The input grows four times between the two timings of :func:`growth`: linear work grows about four times
#: (a little more for a logarithm), quadratic work sixteen times. Eight tells them apart with room for noise.
LINEAR_GROWTH = 8.0


def timed(call: Callable[[], Any]) -> tuple[Any, float]:
    """*call*'s result and the seconds it took."""
    started = time.perf_counter()
    result = call()
    return result, time.perf_counter() - started


def growth(call: Callable[[int], Any], repeats: int = 5) -> float:
    """How many times longer ``call(4)`` takes than ``call(1)``, each the fastest of *repeats* runs.

    *call* builds or picks its input from the scale it is given; build the inputs outside the timed call when
    building them is not the work under test. The runs alternate between the scales, so a change of load during
    the measurement reaches both, and the fastest run of each scale drops a pause that hit only one run.
    """
    small: list[float] = []
    large: list[float] = []
    for _ in range(repeats):
        small.append(timed(lambda: call(1))[1])
        large.append(timed(lambda: call(4))[1])
    return min(large) / min(small)
