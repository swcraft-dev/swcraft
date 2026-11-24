"""MCP client wrapper: spawns `swcraft mcp-serve` and talks to it over stdio."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from swcraft_agent.approval import ANSWER_PROMPT

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from swcraft_agent.approval import Approver

logger = logging.getLogger(__name__)

#: How long construction waits for ``initialize`` and the ``discover`` health check. A ceiling, never a delay:
#: every failure the background loop sees releases the wait at once through ``_bg_error``, so this only bounds a
#: merely slow server (a loaded CI host needed more than 10 s).
_STARTUP_TIMEOUT: float = 30.0
_SHUTDOWN_TIMEOUT: float = 5.0

#: How long one look at a pending call blocks before the next. The unbounded wait (a panel review waits for its
#: human) is cut into looks because on Windows CPython before 3.14 an untimed lock acquire ignores Ctrl-C.
_WAIT_SLICE_SECONDS: float = 0.5

#: What the elicitation prompt appends to the server's message, which is displayed unchanged. It is
#: :data:`swcraft_agent.approval.ANSWER_PROMPT`, stated beside the code that reads the answer.
ELICITATION_ANSWER_PROMPT = ANSWER_PROMPT


class McpClientError(RuntimeError):
    """Raised when the MCP client encounters a lifecycle or protocol error."""


class SwcraftMcpClient:
    """Synchronous wrapper around an async MCP stdio session.

    Spawns the MCP server via ``sys.executable -m swcraft_mcp.server`` with ``SWCRAFT_MANIFEST`` set; the
    interpreter rather than the ``swcraft`` shim, because PATH-based shim resolution on Windows is fragile.
    Keeps the session alive in a background daemon thread, and exposes synchronous
    ``list_openai_tools()`` / ``call_tool()`` methods suitable for use as ``tool_executor`` in
    ``LLMClient.complete()``.

    A server started with ``SWCRAFT_REVIEW=elicit`` asks through MCP elicitation before it commits a change set;
    an *approver* supplied here becomes the session's ``elicitation_callback``. Without one the client declares
    no elicitation capability, and the server abandons the change set with a note naming the missing channel.

    * Construction blocks until ``session.initialize()`` **and** a ``discover`` health-check succeed, or
      ``_STARTUP_TIMEOUT`` elapses (raises ``TimeoutError`` / ``McpClientError``).
    * Any exception captured in the background loop is surfaced on the next ``list_openai_tools()`` /
      ``call_tool()`` call.
    * ``close()`` signals graceful shutdown (``_SHUTDOWN_TIMEOUT``), then force-cancels the background task
      if the thread has not exited, which terminates the subprocess.

    Usage::

        with SwcraftMcpClient("/path/to/swcraft.yaml") as client:
            tools = client.list_openai_tools()
            result = client.call_tool("discover", {})
    """

    def __init__(
        self,
        project_path: str,
        env: Mapping[str, str] | None = None,
        approver: Approver | None = None,
    ) -> None:
        self._project_path = str(project_path)
        #: The human behind this client, or ``None``. Public and mutable: a scripted replay replaces it before
        #: every turn, so it is read at answer time, never at spawn.
        self.approver: Approver | None = approver
        # The whole environment the subprocess runs with (see `core.review_server_env`); `None` inherits
        # os.environ.
        self._env: dict[str, str] | None = dict(env) if env is not None else None
        #: The environment the subprocess is spawned with, resolved here so :attr:`server_env` answers before the
        #: spawn too.
        self._server_env: dict[str, str] = {
            **(os.environ if self._env is None else self._env),
            # A supplied env may not point the server at another manifest.
            "SWCRAFT_MANIFEST": self._project_path,
        }
        #: Whether this client declares the elicitation capability. Fixed: capabilities are declared once, in
        #: ``initialize()``. Declaring none lets the server abandon the change set and say why, rather than
        #: reading as the user's refusal.
        self._can_elicit: bool = approver is not None
        #: A ``KeyboardInterrupt`` raised at a review prompt, re-raised on the caller's thread when the call
        #: returns, so the owning ``with`` block closes the client.
        self._interrupt: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._ready: threading.Event = threading.Event()
        self._bg_error: BaseException | None = None
        self._session: Any = None
        self._main_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event | None = None

        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="swcraft-mcp")
        self._thread.start()

        if not self._ready.wait(_STARTUP_TIMEOUT):
            self._force_cancel()
            raise TimeoutError(
                f"MCP server at '{self._project_path}' did not become ready "
                f"within {_STARTUP_TIMEOUT}s. "
                "Verify the project path exists and swcraft_mcp is installed."
            )
        if self._bg_error:
            exc = self._bg_error
            self._force_cancel()
            raise McpClientError(f"MCP startup failed: {exc}") from exc

    @property
    def server_env(self) -> Mapping[str, str]:
        """The environment the spawned server runs with (read-only); what a host's escape-command gate reads."""
        return MappingProxyType(self._server_env)

    @property
    def is_alive(self) -> bool:
        """Whether the background session thread, and with it the subprocess, is still running.

        The thread exits only once the ``stdio_client`` context, which terminates the server, has been left; the
        MCP SDK does not expose the child process.
        """
        return self._thread.is_alive()

    def list_openai_tools(self) -> list[dict]:
        """Return the server's MCP tools as OpenAI function-calling schemas."""
        self._check_error()
        result = self._submit(self._session.list_tools())
        return [_to_openai_tool(t) for t in result.tools]

    def call_tool(self, name: str, args: dict) -> ToolResult:
        """Call a tool on the MCP server and return its result.

        Raises:
            KeyboardInterrupt: the user interrupted a review prompt this call raised; re-raised on the caller's
                thread so the owning ``with`` block closes the client.
        """
        self._check_error()
        result = self._submit(self._session.call_tool(name, args))
        self._raise_pending_interrupt()
        return _normalize_result(result)

    def close(self) -> None:
        """Shut down the MCP session, ensuring no orphaned subprocess remains."""
        if self._loop.is_closed():
            return

        if self._shutdown_event is not None and not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._signal_shutdown(), self._loop).result(timeout=1.0)
            except Exception:  # noqa: BLE001 -- thread: a best-effort shutdown signal; the join below still runs
                pass

        if self._thread.is_alive():
            self._thread.join(timeout=_SHUTDOWN_TIMEOUT)

        if self._thread.is_alive():
            logger.warning("Graceful MCP shutdown timed out after %ss; force-cancelling.", _SHUTDOWN_TIMEOUT)
            self._force_cancel()

    def __enter__(self) -> SwcraftMcpClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _check_error(self) -> None:
        if self._bg_error:
            raise McpClientError(f"MCP session error: {self._bg_error}") from self._bg_error

    def _raise_pending_interrupt(self) -> None:
        """Re-raise, once, an interrupt the review prompt caught."""
        interrupt, self._interrupt = self._interrupt, None
        if interrupt is not None:
            raise interrupt

    async def _elicit(self, context: Any, params: Any) -> Any:
        """Answer one server elicitation from this host's :class:`Approver`.

        The LLM cannot reach this: the question travels from the server to the human this host owns.
        Every path that is not an answered "yes" is a no: no approver, a question that is not a yes/no prompt,
        an exhausted script, EOF, an empty line and a failing approver all end as ``decline`` or
        ``accept(False)``.
        """
        from mcp import types

        approver = self.approver
        if approver is None or not _is_boolean_answer_schema(getattr(params, "requestedSchema", None)):
            # A question this host cannot put: declining keeps the change set from landing unreviewed.
            logger.warning("Declining an elicitation this host cannot answer: %r", getattr(params, "message", ""))
            return types.ElicitResult(action="decline")

        prompt = render_elicitation(str(getattr(params, "message", "")))
        try:
            # A worker thread: a human may take long, and the event loop must keep draining the pipe.
            answered = await asyncio.to_thread(approver.ask, prompt)
        except KeyboardInterrupt as interrupt:
            # `cancel` is the MCP dismissal; the interrupt goes to the caller's thread, which closes the client.
            self._interrupt = interrupt
            return types.ElicitResult(action="cancel")
        except Exception:
            logger.exception("The approval channel failed while answering an elicitation; declining.")
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content={"value": bool(answered)})

    def _submit(self, coro: Any) -> Any:
        """Submit a coroutine to the background loop and block, interruptibly, for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return _wait_interruptibly(future)

    def _force_cancel(self) -> None:
        """Force-cancel the main task (causes anyio to kill the subprocess)."""
        # `_run_loop` may already have closed the loop, and scheduling on a closed loop raises.
        if self._main_task and not self._main_task.done() and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._main_task.cancel)
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._main_task = self._loop.create_task(self._async_main())
            self._loop.run_until_complete(self._main_task)
        except (asyncio.CancelledError, RuntimeError):
            pass
        except Exception as exc:  # noqa: BLE001 -- thread: the exception is handed to the waiting thread
            if not self._ready.is_set():
                self._bg_error = exc
                self._ready.set()
            else:
                self._bg_error = exc
        finally:
            # Closed on the thread that ran it: an unclosed loop leaks its descriptors, once per scoped turn.
            # `close()` checks `is_closed()` before signalling shutdown, so the graceful path is unaffected.
            try:
                self._loop.close()
            except Exception:  # pragma: no cover - a loop that will not close
                logger.debug("The MCP client's event loop did not close cleanly.", exc_info=True)

    async def _async_main(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._shutdown_event = asyncio.Event()
        env = dict(self._server_env)

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "swcraft_mcp.server"],
            env=env,
        )

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    elicitation_callback=self._elicit if self._can_elicit else None,
                ) as session:
                    self._session = session
                    await session.initialize()

                    # Health check: initialize() succeeds even for a bad SWCRAFT_MANIFEST (the server loads
                    # lazily); `discover` resolves the project. A protocol errorResult is a live server
                    # answering, not an unhealthy one.
                    try:
                        health = await session.call_tool("discover", {})
                        if getattr(health, "isError", False):
                            error_text = _normalize_result(health)
                            self._bg_error = McpClientError(f"discover health check failed: {error_text}")
                            self._ready.set()
                            return
                    except Exception as exc:  # noqa: BLE001 -- thread: a failed health check is handed to the waiting thread
                        self._bg_error = exc
                        self._ready.set()
                        return

                    self._ready.set()
                    await self._shutdown_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- thread: the exception is handed to the waiting thread
            if not self._ready.is_set():
                self._bg_error = exc
                self._ready.set()
            else:
                self._bg_error = exc

    async def _signal_shutdown(self) -> None:
        if self._shutdown_event is not None:
            self._shutdown_event.set()


def _to_openai_tool(mcp_tool: Any) -> dict:
    """Convert an MCP Tool descriptor to an OpenAI function-calling schema."""
    return {
        "type": "function",
        "function": {
            "name": mcp_tool.name,
            "description": mcp_tool.description or "",
            "parameters": mcp_tool.inputSchema,
        },
    }


def render_elicitation(message: str) -> str:
    """The prompt a user answers: the server's message, unchanged so every host asks the same, then the question."""
    return f"{message}\n\n{ELICITATION_ANSWER_PROMPT}"


def _is_boolean_answer_schema(schema: Any) -> bool:
    """Whether *schema* is the single-boolean form the review boundary asks with.

    That is an object schema with exactly one ``boolean`` property ``value``; anything else is a question this
    host must not guess an answer to.
    """
    if not isinstance(schema, Mapping):
        return False
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or set(properties) != {"value"}:
        return False
    value = properties["value"]
    return isinstance(value, Mapping) and value.get("type") == "boolean"


class ToolResult(str):
    """One MCP tool result: the body's text, the body parsed, and the text blocks.

    A ``str`` subclass, so the retained tools' ``OK:`` / ``ERROR:`` / ``REFUSED:`` readers keep working, while a
    protocol tool's ``changeSetResponse`` body and a later host note stay separable.

    Attributes:
        structured: the body as a mapping (``structuredContent``, else the first text block when it is a JSON
            object), or ``None`` for a plain-text body.
        texts: every text content block, in wire order, unfiltered.
    """

    structured: Any
    texts: tuple[str, ...]

    def __new__(cls, text: str, *, structured: Any = None, texts: Sequence[str] = ()) -> ToolResult:
        result = super().__new__(cls, text)
        result.structured = structured
        result.texts = tuple(texts)
        return result


def _normalize_result(result: Any) -> ToolResult:
    """Normalize an MCP CallToolResult to a :class:`ToolResult`.

    The string is, in this order:

    * with ``structuredContent``: that value as ``json.dumps``;
    * when the first text block parses as a JSON object: that block's text, with ``structured`` the mapping;
    * otherwise the joined text blocks, ``structured`` ``None``;
    * with no text block, ``str(result)``.

    ``texts`` keeps every block. Not reached: a plain-text report that is itself a JSON object would read as a
    body; no SWCraft tool renders one.
    """
    structured = getattr(result, "structuredContent", None)
    blocks = getattr(result, "content", None) or ()
    texts = tuple(block.text for block in blocks if hasattr(block, "text"))
    if structured is not None:
        return ToolResult(json.dumps(structured), structured=structured, texts=texts)
    if texts:
        body = _json_object(texts[0])
        if body is not None:
            return ToolResult(texts[0], structured=body, texts=texts)
        return ToolResult("\n".join(texts), structured=None, texts=texts)
    return ToolResult(str(result), structured=None, texts=texts)


#: What :func:`_parsed` answers for a text that is not JSON (``None`` is ``null``).
_NOT_JSON = object()


def _parsed(text: str) -> Any:
    """*text* parsed as JSON, or :data:`_NOT_JSON` when it does not parse."""
    try:
        return json.loads(text)
    except ValueError:
        return _NOT_JSON


def is_json(text: str) -> bool:
    """Whether a text block holds a serialized value rather than a sentence."""
    return _parsed(text) is not _NOT_JSON


def _json_object(text: str) -> dict[str, Any] | None:
    """*text* parsed, when it is a JSON object; ``None`` for anything else."""
    parsed = _parsed(text)
    return parsed if isinstance(parsed, dict) else None


def _wait_interruptibly(future: concurrent.futures.Future[Any], slice_seconds: float = _WAIT_SLICE_SECONDS) -> Any:
    """*future*'s result, waited for in looks of *slice_seconds* so Ctrl-C is delivered between them.

    An untimed ``future.result()`` ignores Ctrl-C on Windows CPython before 3.14. ``concurrent.futures.wait``
    rather than ``future.result(timeout=…)``, so a ``TimeoutError`` the coroutine raised is not mistaken for a
    look that ran out. An interrupt leaves the call running; closing the client withdraws a waiting review.

    Not reached: Ctrl-C is seen up to *slice_seconds* late, and the Windows behaviour is CPython's known one
    (bpo-29971), not measured here.
    """
    while not future.done():
        concurrent.futures.wait((future,), timeout=slice_seconds)
    return future.result()
