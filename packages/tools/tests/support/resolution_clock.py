"""A judgement deadline for the Codex guard's tests that counts path resolutions instead of reading a clock."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from swcraft_tools import copilot_guard

#: How many resolutions a :class:`ResolutionClock` lets a judgement make before its deadline passes.
TICKS_TO_THE_DEADLINE = 5


class ResolutionClock:
    """A :class:`copilot_guard._Deadline` stand-in whose time is the paths resolved.

    The paths are counted as ``resolved_path``, as ``copilot_guard`` names it, one tick each: from
    :data:`TICKS_TO_THE_DEADLINE` ticks on :meth:`check` stops the judgement. Its ``end`` is never reached, so no step
    reads a wall clock."""

    def __init__(self) -> None:
        self.end, self.ticks = math.inf, 0

    def check(self) -> None:
        if self.ticks >= TICKS_TO_THE_DEADLINE:
            raise copilot_guard._OverBudget


def resolution_clocks(monkeypatch: pytest.MonkeyPatch) -> list[ResolutionClock]:
    """Install :class:`ResolutionClock` for every judgement made from now on; the clocks made, in order."""
    clocks: list[ResolutionClock] = []
    resolve = copilot_guard.resolved_path

    def ticking(path: Path) -> str:
        if clocks:
            clocks[-1].ticks += 1
        return resolve(path)

    def deadline() -> ResolutionClock:
        clocks.append(ResolutionClock())
        return clocks[-1]

    monkeypatch.setattr(copilot_guard, "_Deadline", deadline)
    monkeypatch.setattr(copilot_guard, "resolved_path", ticking)
    return clocks
