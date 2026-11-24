"""Record the order in which a module's functions are called, for the check-order tests.

``test_termination_required.py`` and ``test_name_space_collisions.py`` each prove
that ``generate_arxml`` runs its guards in their fixed order, by wrapping the
guards and reading the calls back.

What it does not reach: a call that does not go through the module attribute -- a
reference captured before the patch, or a call from inside the wrapped function to
another wrapped name bound locally -- is not recorded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from types import ModuleType
from typing import Any

import pytest


def record_calls(monkeypatch: pytest.MonkeyPatch, module: ModuleType, names: Iterable[str]) -> list[str]:
    """Wrap each of *names* on *module* so that every call appends its name; return that list.

    The list is live: it fills as the code under test runs. Each wrapper calls the
    original unchanged; *monkeypatch* restores the originals after the test.
    """
    calls: list[str] = []

    def spy(name: str) -> Callable[..., Any]:
        original = getattr(module, name)

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return original(*args, **kwargs)

        return wrapped

    for name in names:
        monkeypatch.setattr(module, name, spy(name))
    return calls
