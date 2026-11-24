"""Every tool's docstring reaches the host whole.

FastMCP publishes the first text section of a Google-style docstring as the
tool's description and each ``Args:`` entry as its argument's description; a
section after the parameters section is published nowhere. Two questions over
the live registration:

- **the shape**: in griffe's Google parse (the one FastMCP makes), no section
  but a parameters section follows the first parameters section, so an
  admonition or a ``Returns:`` after ``Args:`` is caught too;
- **the published description**: what ``list_tools()`` answers over the
  in-memory client equals the docstring's text before ``Args:``, so a FastMCP
  rule change is caught as well.

Both are shown to fire against a planted docstring with a tail.

**What this module does not reach**: a paragraph inside an ``Args:`` entry (it is
published with its argument), and whether the published text is right or meant
for the host.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from fastmcp import Client, FastMCP
from griffe import Docstring, DocstringSectionKind
from swcraft_mcp.server import mcp


def _registered(server: FastMCP) -> dict[str, Callable[..., Any]]:
    """Every tool *server* registers, by name, with the function its docstring is read from."""
    return {tool.name: tool.fn for tool in asyncio.run(server.list_tools())}


def _published(server: FastMCP) -> dict[str, str]:
    """Every tool's description as a host receives it, over the in-memory client."""

    async def descriptions() -> dict[str, str]:
        async with Client(server) as client:
            return {tool.name: tool.description or "" for tool in await client.list_tools()}

    return asyncio.run(descriptions())


def _text_before_args(fn: Callable[..., Any]) -> str:
    """The docstring up to its ``Args:`` line, or the whole docstring when it has none."""
    return (inspect.getdoc(fn) or "").split("\nArgs:\n", 1)[0].strip()


def _sections_after_parameters(name: str, fn: Callable[..., Any]) -> list[str]:
    """One line per section of *fn*'s docstring that follows its parameters section."""
    sections = Docstring(inspect.getdoc(fn) or "", lineno=1, parser="google").parse()
    kinds = [section.kind for section in sections]
    if DocstringSectionKind.parameters not in kinds:
        return []
    first = kinds.index(DocstringSectionKind.parameters)
    return [
        f"{name}: a {kind.value} section follows the parameters section and is published nowhere"
        for kind in kinds[first + 1 :]
        if kind is not DocstringSectionKind.parameters
    ]


def _unpublished(server: FastMCP) -> list[str]:
    """One line per tool whose published description is not its text before ``Args:``."""
    published = _published(server)
    return [
        f"{name}: the published description is not the docstring's text before Args:"
        for name, fn in _registered(server).items()
        if published[name] != _text_before_args(fn)
    ]


def _planted() -> FastMCP:
    """A server registering one tool whose docstring carries a paragraph after ``Args:``."""
    planted = FastMCP("planted")

    @planted.tool()
    def tailed(value: str) -> str:
        """Echo a value.

        Args:
            value: what is echoed.

        The host never reads this paragraph.
        """
        return value

    return planted


class TestEveryToolReachesTheHostWhole:
    def test_the_registration_is_read_live(self) -> None:
        assert {"discover", "read", "change_set", "impact_of"} <= set(_registered(mcp))

    def test_no_section_follows_the_parameters_section(self) -> None:
        offenders = [line for name, fn in _registered(mcp).items() for line in _sections_after_parameters(name, fn)]
        assert offenders == []

    def test_the_published_description_is_the_text_before_args(self) -> None:
        assert _unpublished(mcp) == []


class TestTheGateFires:
    def test_a_tail_after_args_is_refused_with_the_tools_name(self) -> None:
        planted = _planted()
        offenders = [line for name, fn in _registered(planted).items() for line in _sections_after_parameters(name, fn)]
        assert offenders == ["tailed: a text section follows the parameters section and is published nowhere"]

    def test_the_tail_is_really_lost_on_the_wire(self) -> None:
        """The finding itself, measured: the paragraph after ``Args:`` reaches no host."""
        published = _published(_planted())["tailed"]
        assert published == "Echo a value."
        assert "never reads" not in published
        assert _unpublished(_planted()) == []
