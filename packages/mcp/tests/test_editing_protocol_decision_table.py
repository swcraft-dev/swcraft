"""Every disposition of the MCP tool decision table is carried out on the live surface.

No ``replace-generic-read`` or ``replace-change-set`` tool is registered, every
``retain-specialized`` one is unless its row carries a ``withdrawn`` record, and nothing outside
those sets, the protocol tools and the added questions is on the surface. The table is read,
never edited: a withdrawn tool keeps its disposition and gains a ``withdrawn`` record, so the live
surface is *retained minus withdrawn* and what is gone is *removed plus withdrawn*.

The module lives in ``packages/mcp/tests/`` because it imports the server, which
``packages/tools/`` may never know about.
"""

from __future__ import annotations

import asyncio

import pytest
from decision_table import ADDED_QUESTIONS, live_retained, load_projections, removed
from swcraft_mcp.server import mcp

# The protocol tools are not rows of the table: the table is the disposition of the tools that
# existed before them, and three of them are what those dispositions are replaced by.
PROTOCOL_TOOLS = frozenset({"discover", "read", "find", "change_set"})


@pytest.fixture(scope="module")
def projections() -> dict[str, list[str]]:
    """The table's name sets: ``replaced`` holds the rows replaced by the protocol tools."""
    return load_projections()


@pytest.fixture(scope="module")
def registered_tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def _live_retained(projections: dict[str, list[str]]) -> set[str]:
    return set(live_retained(projections))


def _gone(projections: dict[str, list[str]]) -> set[str]:
    return set(removed(projections))


class TestEveryDispositionIsApplied:
    """The table read as a statement about the surface that exists now."""

    def test_no_replaced_tool_is_registered(
        self, projections: dict[str, list[str]], registered_tool_names: set[str]
    ) -> None:
        assert len(projections["replaced"]) == 29
        gone = _gone(projections)
        assert len(gone) == 32
        assert not (gone & registered_tool_names), sorted(gone & registered_tool_names)

    def test_every_retained_tool_is_registered(
        self, projections: dict[str, list[str]], registered_tool_names: set[str]
    ) -> None:
        # The way a removal goes wrong quietly is by taking a retained tool with
        # it, so this half is asserted in its own right and not as a remainder.
        assert len(projections["retained"]) == 10
        retained = _live_retained(projections)
        assert len(retained) == 7
        assert retained <= registered_tool_names, sorted(retained - registered_tool_names)

    def test_the_surface_is_the_retained_tools_and_the_protocol_and_nothing_else(
        self, projections: dict[str, list[str]], registered_tool_names: set[str]
    ) -> None:
        expected = _live_retained(projections) | PROTOCOL_TOOLS | ADDED_QUESTIONS
        assert registered_tool_names == expected, (
            f"unexpected: {sorted(registered_tool_names - expected)}; "
            f"missing: {sorted(expected - registered_tool_names)}"
        )
