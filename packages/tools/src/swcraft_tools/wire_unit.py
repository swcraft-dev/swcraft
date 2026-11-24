"""The wire unit: the bytes a host receives for one MCP tool result, in one spelling.

* :func:`compact_json` -- the one writer of a mapping body's text;
* :func:`escaped_bytes` -- the one spelling of the unit's formula;
* :func:`measure` -- ``escaped_bytes(compact_json(payload))``.

A leaf: it imports nothing but the standard library, so ``editing/`` and modules that may not import
``editing/`` share one formula without a cycle. What it does not reach: a budget;
``RESPONSE_BUDGET_WIRE_BYTES`` stays in ``editing/read.py``.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["compact_json", "escaped_bytes", "measure"]


def compact_json(payload: Any) -> str:
    """*payload* as compact JSON: no separator spaces, non-ASCII characters as they are.

    The one writer of a mapping body's text, so what is measured and what is sent cannot drift apart. A value
    of a type stdlib ``json`` cannot encode raises ``TypeError``.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def escaped_bytes(text: str) -> int:
    """The wire bytes of *text* carried as one text block: escaped as a JSON string, in UTF-8."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def measure(payload: Any) -> int:
    """The wire bytes a host receives for *payload* as one mapping tool result.

    The MCP server sends a mapping body once, as one text block holding :func:`compact_json` of it; JSON-RPC
    carries that text as an escaped JSON string, so the escaped copy is counted: every ``"`` is two bytes on the
    wire. ``ensure_ascii=False`` because the wire carries UTF-8.

    What this does not reach: JSON-RPC framing (a fixed ~70 bytes) is not counted, the one place this is an
    under-count. The server serializes after FastMCP's ``to_jsonable_python`` reduction while this function
    sees the engine's raw response, so a field of a type stdlib ``json`` cannot encode (``Decimal``, ``Enum``,
    ``datetime``, ``Path``) raises ``TypeError`` here rather than being measured; no ``default=`` fallback, so
    that a loud failure never becomes a silently wrong figure.
    """
    return escaped_bytes(compact_json(payload))
