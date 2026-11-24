"""The two rename pairings side by side: staging's and the safety guard's.

Staging (:func:`swcraft_tools.editing.staging._rename_pairs`) and the guard
(:func:`swcraft_tools.editing.safety_guard._detect_renames`) must decide the same
whole-element renames: one carries an old file's text into the new one, the other
compares a renamed element under one owner. These helpers read each one's decision
as ``{(layer, was): now}``, so a test holds the two equal.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
import swcraft_tools.editing.safety_guard as safety_guard_mod
import swcraft_tools.editing.staging as staging_mod
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.apply import TouchedElement
from swcraft_tools.editing.protocol import DeltaEntry


def guarded_element_renames(result: Any) -> dict[tuple[str, str], str]:
    """The element-level half of a ``_Renames``' ``forward``; the member pairs are not staging's shape."""
    return {(key[1], key[2]): now for key, now in result.forward.items() if key[0] == "element"}


def staged_forward(result: Any) -> dict[tuple[str, str], str]:
    """An ``ElementRenames``' pairs as ``{(layer, was): now}``."""
    return {(layer, was): now for layer, was, now in result.pairs}


def staged_renames_of(delta: Iterable[DeltaEntry]) -> dict[tuple[str, str], str]:
    """Staging's pairing over *delta*'s whole-element removals and creations, as touched elements.

    For a case built by editing files rather than through the engine: the delta's
    published values stand in for the staged ones, which is what they are.
    """
    touched = [
        TouchedElement(entry.address.layer, entry.address.element, entry.before, None)
        if entry.operation == "remove"
        else TouchedElement(entry.address.layer, entry.address.element, None, entry.after)
        for entry in delta
        if entry.operation in {"remove", "create"} and not entry.address.path and entry.address.element is not None
    ]
    return staged_forward(staging_mod._rename_pairs(touched, reg.or_current(None)))


def spy_on_the_two_pairings(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[tuple[str, str], str]], list[dict[tuple[str, str], str]]]:
    """Record what staging and the guard decide during a change set, each as ``{(layer, was): now}``."""
    staged: list[dict[tuple[str, str], str]] = []
    guarded: list[dict[tuple[str, str], str]] = []
    pairs, detect = staging_mod._rename_pairs, safety_guard_mod._detect_renames

    def recorded_pairs(touched: Any, registry: Any) -> Any:
        result = pairs(touched, registry)
        staged.append(staged_forward(result))
        return result

    def recorded_detect(delta: Any) -> Any:
        result = detect(delta)
        guarded.append(guarded_element_renames(result))
        return result

    monkeypatch.setattr(staging_mod, "_rename_pairs", recorded_pairs)
    monkeypatch.setattr(safety_guard_mod, "_detect_renames", recorded_detect)
    return staged, guarded
