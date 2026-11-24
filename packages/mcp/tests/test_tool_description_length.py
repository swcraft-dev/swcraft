"""Every tool's description fits the shortest client limit measured (Claude Code's 4 096 characters).

Claude Code cuts an MCP tool's description at 4 096 characters and drops the
rest without telling the model, so detail beyond it belongs in the guide section
the description names. The length is read off the live registration over the
in-memory client, counted in characters.

**What this does not reach.** The other clients' limits (Copilot and Codex cut
nothing measured, which bounds their limits from below only); a later Claude
Code release that lowers the limit; the argument descriptions in
``inputSchema``; and whether the shortened text still tells an agent enough.
"""

from __future__ import annotations

import asyncio

from fastmcp import Client, FastMCP
from swcraft_mcp.server import mcp

#: Claude Code's cut, in characters, as its server log reports it
#: (*description truncated from 7982 to 4096 chars*, Claude Code 2.1.319).
CLAUDE_CODE_DESCRIPTION_LIMIT = 4096


def _descriptions(server: FastMCP) -> dict[str, str]:
    """Every tool's description as a host receives it in ``tools/list``."""

    async def listed() -> dict[str, str]:
        async with Client(server) as client:
            return {tool.name: tool.description or "" for tool in await client.list_tools()}

    return asyncio.run(listed())


def _over_the_limit(server: FastMCP) -> list[str]:
    """One sentence per tool whose description a host would cut, naming the remedy."""
    return [
        f"{name}: its description is {len(text)} characters, over Claude Code's "
        f"{CLAUDE_CODE_DESCRIPTION_LIMIT}, which cuts the rest unseen; move the detail into the "
        "guide section the description names and keep what a call needs"
        for name, text in _descriptions(server).items()
        if len(text) > CLAUDE_CODE_DESCRIPTION_LIMIT
    ]


class TestEveryDescriptionFits:
    def test_the_registration_is_read_live(self) -> None:
        assert {"discover", "read", "change_set", "import_component"} <= set(_descriptions(mcp))

    def test_no_description_is_over_claude_codes_limit(self) -> None:
        assert _over_the_limit(mcp) == []


class TestTheGateFires:
    def test_a_description_over_the_limit_is_named_with_the_remedy(self) -> None:
        planted = FastMCP("planted")

        def long(value: str) -> str:
            return value

        long.__doc__ = "Echo a value — use when a value is echoed.\n\n" + "x" * CLAUDE_CODE_DESCRIPTION_LIMIT
        planted.tool()(long)
        (problem,) = _over_the_limit(planted)
        assert problem.startswith("long: its description is ")
        assert "guide section" in problem

    def test_a_description_at_the_limit_passes(self) -> None:
        planted = FastMCP("planted")

        def exact(value: str) -> str:
            return value

        exact.__doc__ = "x" * CLAUDE_CODE_DESCRIPTION_LIMIT
        planted.tool()(exact)
        assert _over_the_limit(planted) == []
