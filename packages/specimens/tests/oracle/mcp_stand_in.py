"""A stand-in MCP server over stdio for `tests/test_mcp_client.py`, run as a subprocess.

It writes one line to standard error, then answers each request, a notification first.
`initialize` gets an empty result; `tools/call` acts by tool name: `structured` answers
`structuredContent`, `text` a text content only, `is-error` a result with `isError`, `error` a
JSON-RPC error, `eof` closes the output and `silent` never answers. With the argument `stubborn` it
ignores SIGTERM (POSIX) and outlives its input, so only a kill ends it.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from typing import Any

STARTED = "stand-in server started"


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def answer(request: dict[str, Any]) -> bool:
    """Answer one request; `False` once the output is closed."""
    send({"method": "notifications/message", "params": {"level": "info", "data": "working"}})
    # A server-to-client request carrying the client's own id, which the client must skip.
    send({"id": request["id"], "method": "ping"})
    if request["method"] != "tools/call":
        send({"id": request["id"], "result": {}})
        return True
    name = request["params"]["name"]
    if name == "structured":
        send({"id": request["id"], "result": {"structuredContent": {"answer": 1}}})
    elif name == "text":
        content = [
            {"type": "image", "data": "", "mimeType": "image/png"},
            {"type": "text", "text": json.dumps({"answer": 2})},
        ]
        send({"id": request["id"], "result": {"content": content}})
    elif name == "is-error":
        send({"id": request["id"], "result": {"isError": True, "content": []}})
    elif name == "error":
        send({"id": request["id"], "error": {"code": -32602, "message": "no such tool"}})
    elif name == "eof":
        sys.stdout.close()
        return False
    return True


def main() -> None:
    stubborn = sys.argv[1:] == ["stubborn"]
    if stubborn and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stderr.write(STARTED + "\n")
    sys.stderr.flush()
    for line in sys.stdin:
        message = json.loads(line)
        if "id" in message and not answer(message):
            break
    while stubborn:
        time.sleep(1)


if __name__ == "__main__":
    main()
