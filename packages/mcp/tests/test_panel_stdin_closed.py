"""The client closes the server's stdin during a panel wait: the wait ends and the server exits.

The in-process tests replace the private probe the server reads
(``server._session_closed``); this test is the oracle for the real one, so an
mcp or FastMCP upgrade that changes it fails here. The server is a real
subprocess driven by raw newline-delimited JSON-RPC: mcp's ``stdio_client``
would terminate the child itself on leaving its context. No signal is sent; the
process must end by itself. It runs on every platform: Windows is where stdio
pipes differ.

What it does not reach: a client that keeps the pipe open and stops reading,
and a transport other than stdio. Nor can it tell *which* end the wait took: the
reply carrying :data:`~swcraft_mcp.panel_review.SESSION_CLOSED_NOTE` is
never written once stdin is gone; the exit bound is what separates it from a
shutdown cancellation.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

import editing_vector_fixtures as fixtures
import pytest
from protocol_client import lifecycle_records
from swcraft_mcp import panel_review
from swcraft_tools.swcraft_home import swcraft_home

from tests.test_review_mode import PLAIN, arguments

#: How long the server may take to start, stage the change set and hand its question to the panel.
HAND_OFF_SECONDS = 60

#: How long the server may take to end by itself once its stdin is closed.
EXIT_SECONDS = 15

#: How long one JSON-RPC response may take.
RESPONSE_SECONDS = 60


def _send(stdin: IO[bytes], message: dict[str, Any]) -> None:
    stdin.write(json.dumps(message).encode("utf-8") + b"\n")
    stdin.flush()


def _lines(stdout: IO[bytes], into: queue.Queue[bytes | None]) -> None:
    for line in stdout:
        into.put(line)
    into.put(None)


def _response(lines: queue.Queue[bytes | None], request_id: int) -> dict[str, Any]:
    """The response to *request_id*; notifications and other ids in between are skipped."""
    deadline = time.monotonic() + RESPONSE_SECONDS
    while True:
        line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
        assert line is not None, f"the server closed its stdout before answering request {request_id}"
        message = json.loads(line)
        if message.get("id") == request_id and "method" not in message:
            return message


def _stderr_tail(path: Path, characters: int = 2000) -> str:
    """The end of the server's stderr, for a failure message."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as failed:
        return f"<stderr unreadable: {type(failed).__name__}>"
    return text[-characters:]


def _wait_for(condition: Any, seconds: float, what: str, stderr: Path) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, (
            f"timed out after {seconds} s waiting for {what}; server stderr ends:\n{_stderr_tail(stderr)}"
        )
        time.sleep(0.05)


def test_closing_stdin_during_a_panel_wait_abandons_and_the_server_exits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = fixtures.build_fixture(tmp_path / "project", PLAIN)
    home = tmp_path / "home"
    home.mkdir()
    # Popped as the autouse fixture pops it, so the child never starts scoped.
    inherited = {
        name: value for name, value in os.environ.items() if name not in ("SWCRAFT_APPROVED_SCOPE", "SWCRAFT_SCOPE_ID")
    }
    env = {
        **inherited,
        "SWCRAFT_REVIEW": "panel",
        "SWCRAFT_MANIFEST": os.fspath(project / "swcraft.yaml"),
        "HOME": os.fspath(home),
        "LOCALAPPDATA": os.fspath(home),
        "USERPROFILE": os.fspath(home),
    }
    reviews = Path(swcraft_home(env, os.name, home)) / "reviews"
    named = arguments(PLAIN, project)

    def pending() -> list[Path]:
        if not reviews.is_dir():
            return []
        return sorted(reviews.glob(f"*{panel_review.PENDING_SUFFIX}"), key=lambda path: path.as_posix())

    stderr_path = tmp_path / "server-stderr.txt"
    with stderr_path.open("w", encoding="utf-8", newline="\n") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "swcraft_mcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=env,
            cwd=os.fspath(project),
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            lines: queue.Queue[bytes | None] = queue.Queue()
            reader = threading.Thread(target=_lines, args=(process.stdout, lines), daemon=True)
            reader.start()

            _send(
                process.stdin,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "stdin-closed-test", "version": "1"},
                    },
                },
            )
            assert "result" in _response(lines, 1)
            _send(process.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send(
                process.stdin,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "change_set", "arguments": named},
                },
            )

            _wait_for(lambda: bool(pending()), HAND_OFF_SECONDS, "the panel question", stderr_path)
            (question,) = pending()
            change_set_id = json.loads(question.read_text(encoding="utf-8"))["change_set_id"]

            process.stdin.close()
            started = time.monotonic()
            try:
                returncode = process.wait(timeout=EXIT_SECONDS)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    f"the server did not exit within {EXIT_SECONDS} s of its stdin closing: the panel wait "
                    "no longer sees the session close (server._session_closed after an mcp/FastMCP upgrade?); "
                    f"server stderr ends:\n{_stderr_tail(stderr_path)}"
                )
            # Recorded, not asserted: the exit code is FastMCP's to choose; that it ends at all is the claim.
            with capsys.disabled():
                print(
                    f"\nserver exited by itself: return code {returncode}, "
                    f"{time.monotonic() - started:.2f} s after stdin closed"
                )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=EXIT_SECONDS)

    assert pending() == [], "the closed session left its question pending"
    records = [record for record in lifecycle_records(project) if record["args"]["change_set_id"] == change_set_id]
    assert records, lifecycle_records(project)
    assert records[-1]["operation"] == "change_set_abandoned", records
