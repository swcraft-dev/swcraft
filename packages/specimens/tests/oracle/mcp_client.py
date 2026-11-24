"""A minimal MCP client over stdio, for the integration acceptance.

It speaks newline-delimited JSON-RPC 2.0 to a server started as a subprocess (``swcraft mcp-serve
--project <manifest>``): ``initialize``, ``notifications/initialized``, then ``tools/call``. A
tool's result is read from ``structuredContent``, else from the first text content.

No read blocks for ever: a reader thread queues the server's output lines, and ``request`` waits at
most ``timeout`` seconds (``READ_TIMEOUT`` by default), then fails quoting the tail of the server's
standard error, kept in a temporary file. ``close`` closes stdin, terminates the server and waits
with a timeout (then kills it), so a caller using it in a ``finally`` leaves no process running.

What this does not reach: one request at a time, no server-to-client request (an elicitation or a
sampling call is skipped like a notification, so the request it blocks times out), and no batch.
"""

from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

#: The protocol version the client announces; the server answers with its own.
PROTOCOL_VERSION = "2025-03-26"
EXIT_TIMEOUT = 30
READ_TIMEOUT = 120.0
#: The characters of the server's standard error a failure quotes, at most.
STDERR_TAIL = 4_000


class McpClient:
    """One MCP server process and the requests sent to it, numbered from 1."""

    def __init__(self, process: subprocess.Popen[str], stderr: IO[str], timeout: float = READ_TIMEOUT) -> None:
        self._process = process
        self._stderr = stderr
        self._timeout = timeout
        self._next_id = 0
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    @classmethod
    def start(cls, command: Sequence[str], cwd: Path | None = None, timeout: float = READ_TIMEOUT) -> McpClient:
        stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="\n")
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                encoding="utf-8",
            )
        except BaseException:
            stderr.close()
            raise
        return cls(process, stderr, timeout)

    def _read(self) -> None:
        """Queue every output line, then `None` once the server closes its output."""
        stdout = self._process.stdout
        assert stdout is not None
        try:
            for line in stdout:
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)

    def stderr_tail(self) -> str:
        """The last `STDERR_TAIL` characters the server wrote to its standard error."""
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()[-STDERR_TAIL:]

    def _stdin(self) -> IO[str]:
        assert self._process.stdin is not None
        return self._process.stdin

    def _send(self, message: dict[str, Any]) -> None:
        stdin = self._stdin()
        stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send one request and return its `result`; notifications in between are skipped."""
        self._next_id += 1
        self._send({"id": self._next_id, "method": method, "params": params})
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(
                    f"the MCP server gave no answer to {method} within {self._timeout:g} s; "
                    f"its standard error ends:\n{self.stderr_tail()}"
                ) from None
            if line is None:
                raise AssertionError(
                    f"the MCP server closed its output before answering {method}; its standard "
                    f"error ends:\n{self.stderr_tail()}"
                )
            message = json.loads(line)
            if message.get("id") != self._next_id or "method" in message:
                continue
            assert "error" not in message, f"{method}: {message['error']}"
            return message["result"]

    def initialize(self) -> Any:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "swcraft-specimens-tests", "version": "0"},
            },
        )
        self._send({"method": "notifications/initialized"})
        return result

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        assert not result.get("isError"), f"{name}: {result}"
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        texts = [item["text"] for item in result.get("content", []) if item.get("type") == "text"]
        assert texts, f"{name}: no structured or text content in {result}"
        return json.loads(texts[0])

    def close(self) -> None:
        """Close stdin, terminate the server and wait for it; kill it if it does not exit."""
        try:
            self._stdin().close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self._process.terminate()
            self._process.wait(timeout=EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=EXIT_TIMEOUT)
        finally:
            self._reader.join(timeout=EXIT_TIMEOUT)
            if self._process.stdout is not None:
                self._process.stdout.close()
            self._stderr.close()
