"""The MCP client (`tests/oracle/mcp_client.py`) against a stand-in server, without SWCraft.

Covers an answer after a notification and a server request, the structured and the text-only
result, `isError`, a JSON-RPC error, a server that closes its output, one that never answers (the
read deadline, quoting the server's standard error) and one that ignores SIGTERM (killed by `close`).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.oracle import mcp_client
from tests.oracle.mcp_client import McpClient
from tests.oracle.mcp_stand_in import STARTED

STAND_IN = Path(__file__).parent / "oracle" / "mcp_stand_in.py"


def start(*arguments: str, timeout: float = 10.0) -> McpClient:
    return McpClient.start([sys.executable, str(STAND_IN), *arguments], timeout=timeout)


@pytest.fixture
def client() -> Iterator[McpClient]:
    started = start()
    try:
        started.initialize()
        yield started
    finally:
        started.close()


def test_initialize_answers_after_a_notification_and_a_server_request(client: McpClient) -> None:
    assert client.request("tools/list", {}) == {}


def test_a_structured_result(client: McpClient) -> None:
    assert client.call_tool("structured", {}) == {"answer": 1}


def test_a_text_only_result_is_read_from_its_first_text_content(client: McpClient) -> None:
    assert client.call_tool("text", {}) == {"answer": 2}


def test_an_is_error_result_fails(client: McpClient) -> None:
    with pytest.raises(AssertionError, match="is-error"):
        client.call_tool("is-error", {})


def test_a_json_rpc_error_fails(client: McpClient) -> None:
    with pytest.raises(AssertionError, match="no such tool"):
        client.call_tool("error", {})


def test_a_server_closing_its_output_fails_with_its_standard_error(client: McpClient) -> None:
    with pytest.raises(AssertionError, match="closed its output") as raised:
        client.call_tool("eof", {})
    assert STARTED in str(raised.value)


def test_a_silent_server_runs_out_of_time_with_its_standard_error() -> None:
    silent = start(timeout=0.5)
    try:
        silent.initialize()
        with pytest.raises(AssertionError, match="no answer to tools/call within 0.5 s") as raised:
            silent.call_tool("silent", {})
        assert STARTED in str(raised.value)
    finally:
        silent.close()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM cannot be ignored on Windows")
def test_close_kills_a_server_that_ignores_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_client, "EXIT_TIMEOUT", 1)
    stubborn = start("stubborn")
    stubborn.initialize()
    stubborn.close()
    process = stubborn._process
    assert process.returncode is not None and process.returncode < 0
