"""An in-memory FastMCP client, so every protocol test drives the wire shapes.

What a host drives is ``tools/list`` and ``tools/call``: the argument binding, the body block
with the host notes after it and the transport-level tool error are produced by FastMCP and the
server, not by the function body, so a test that called the function would assert none of them.
``Client(mcp)`` connects in-memory to the live ``FastMCP`` instance: no subprocess, no port.

The sync wrappers run ``asyncio.run`` per call (the repository has no async pytest plugin); the
``a``-prefixed coroutines serve a test that needs two calls in flight at once.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, NamedTuple

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from mcp.types import Tool
from swcraft_mcp.server import mcp
from swcraft_tools.editing.audit import LIFECYCLE_OPERATIONS


class ToolCallResult(NamedTuple):
    """A ``tools/call`` result as a body and the notes after it.

    ``structured`` is the body decoded by :func:`body_of`, or ``None`` when it is not a mapping
    (a ``str`` tool's report). ``texts`` holds every text block in order: the body block first,
    then any host note, which carries what ``$defs/changeSetResponse`` is closed against.
    """

    structured: Any
    texts: tuple[str, ...]


def body_of(result: Any) -> dict[str, Any] | None:
    """The body of a raw ``tools/call`` result as a mapping, or ``None``.

    The first text block parsed, when it is a JSON object; ``None`` for a non-mapping body or no
    text block. Read off the result because ``resource_report`` answers a mapping or a string per
    call. A structured half is not read: the server sends none, and reading one would let a
    returned second copy pass silently.
    """
    return _body(_texts(result))


def _texts(result: Any) -> tuple[str, ...]:
    """Every text block of a raw ``tools/call`` result, in order."""
    return tuple(block.text for block in result.content if getattr(block, "type", None) == "text")


def _body(texts: tuple[str, ...]) -> dict[str, Any] | None:
    """The first of *texts* parsed, when it is a JSON object; ``None`` otherwise."""
    if not texts:
        return None
    try:
        parsed = json.loads(texts[0])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _split(result: Any) -> ToolCallResult:
    texts = _texts(result)
    return ToolCallResult(structured=_body(texts), texts=texts)


#: The three non-affirmative answers an elicitation can end in, beside ``True``/``False``.
#: ``"fail"`` is the transport failure: the handler raises and the server's ``ctx.elicit`` raises,
#: as for a client that dropped the connection mid-question.
DECLINE = "decline"
CANCEL = "cancel"
FAIL = "fail"

Answer = bool | str


class ScriptedHuman:
    """The person behind the client, scripted: one answer per question, in order.

    The review boundary may ask two questions (the delta, then the resource weakening), so a
    script can answer "yes to the delta, no to the weakening". Every question is recorded in
    :attr:`questions`; one nobody scripted is recorded in :attr:`unscripted` and declined, which
    keeps the run fail-closed while making the defect visible.
    """

    def __init__(self, *answers: Answer) -> None:
        self._answers = list(answers)
        self.questions: list[str] = []
        #: The `requestedSchema` of each question, in order: the deprecated empty form renders as
        #: an unanswerable empty form in VS Code, so a test must see what was asked for.
        self.schemas: list[Any] = []
        self.unscripted: list[str] = []

    async def __call__(self, message: str, response_type: Any, params: Any, context: Any) -> Any:
        self.questions.append(message)
        self.schemas.append(getattr(params, "requestedSchema", None))
        if not self._answers:
            self.unscripted.append(message)
            return ElicitResult(action="decline")
        answer = self._answers.pop(0)
        if answer == DECLINE:
            return ElicitResult(action="decline")
        if answer == CANCEL:
            return ElicitResult(action="cancel")
        if answer == FAIL:
            raise RuntimeError("the elicitation channel failed")
        return response_type(value=answer)

    @property
    def unanswered(self) -> list[Answer]:
        """Scripted answers no question ever asked for."""
        return list(self._answers)


async def alist_tools() -> list[Tool]:
    async with Client(mcp) as client:
        return await client.list_tools()


async def acall(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    human: ScriptedHuman | None = None,
) -> ToolCallResult:
    """Call *name* over the wire, raising ``ToolError`` on a tool error.

    An omitted required argument or an unknown tool has no protocol response, only this error.

    Args:
        human: the client's elicitation channel, or ``None`` for a client that declares no
            elicitation capability: a host that cannot ask, which ``SWCRAFT_REVIEW=elicit`` must
            fail closed on.
    """
    async with Client(mcp, elicitation_handler=human) as client:
        return _split(await client.call_tool(name, arguments or {}))


def list_tools() -> list[Tool]:
    return asyncio.run(alist_tools())


def tool_names() -> set[str]:
    return {tool.name for tool in list_tools()}


def call(name: str, /, **arguments: Any) -> ToolCallResult:
    """``tools/call`` *name* with *arguments* as the tool's named arguments."""
    return asyncio.run(acall(name, arguments))


def call_reviewed(human: ScriptedHuman | None, name: str, /, **arguments: Any) -> ToolCallResult:
    """``tools/call`` *name* with *human* as the client's elicitation channel."""
    return asyncio.run(acall(name, arguments, human=human))


# What an exchange leaves behind: the audit log.


def audit_records(project: Path) -> list[dict[str, Any]]:
    """Every entry in the project's **real** append-only log, in order.

    ``[]`` when there is no log file, the ordinary state of a project no change set has committed
    in; a test claiming the file was never created reads the path itself.
    """
    log = project / "dist" / "audit.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def lifecycle_records(project: Path) -> list[dict[str, Any]]:
    """:func:`audit_records` filtered to the change-set lifecycle vocabulary."""
    return [entry for entry in audit_records(project) if entry["operation"] in LIFECYCLE_OPERATIONS]
