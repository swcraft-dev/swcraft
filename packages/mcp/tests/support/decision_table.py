"""The readings of the MCP tool decision table the surface tests share.

Which retained tools are live, which were withdrawn and which are gone are each a name set of
the table, read from ``tool-projections.json`` (:data:`PROJECTIONS_PATH`), one key per set. The open
tree does not check the fixture against the table.
``scripts/tests/test_clean_install.py`` reads the same fixture itself: it does not import from
``packages/mcp/tests``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

PROJECTIONS_PATH = (
    Path(__file__).resolve().parents[3]
    / "tools"
    / "tests"
    / "fixtures"
    / "contracts"
    / "editing-protocol"
    / "tool-projections.json"
)

#: Tools with no row: each answers a question no existing tool's parameter reaches.
ADDED_QUESTIONS: frozenset[str] = frozenset({"architecture_metrics", "guide"})


def load_projections() -> dict[str, list[str]]:
    """The table's name sets, by key (``rows``, ``retained``, ``withdrawn``, ``live_retained``, ...)."""
    return json.loads(PROJECTIONS_PATH.read_text(encoding="utf-8"))["projections"]


def withdrawn(projections: Mapping[str, Sequence[str]]) -> frozenset[str]:
    """The rows a later decision took off the surface by a dated ``withdrawn`` record."""
    return frozenset(projections["withdrawn"])


def live_retained(projections: Mapping[str, Sequence[str]]) -> frozenset[str]:
    """The ``retain-specialized`` rows still registered: the retained less the withdrawn."""
    return frozenset(projections["live_retained"])


def removed(projections: Mapping[str, Sequence[str]]) -> frozenset[str]:
    """Every row not live: a non-retaining disposition, or a retained row withdrawn."""
    return frozenset(projections["removed"])
