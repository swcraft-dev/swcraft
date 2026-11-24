"""The MCP tools' integer arguments read an integer as the protocol schema does, over the wire.

``protocol.schema.json`` types ``read``'s ``limit`` and ``find``'s ``candidates`` as integers:
``2.0`` is the integer ``2``; ``2.5``, ``true`` and ``"2"`` are not. FastMCP validates arguments in
pydantic's lax mode before the protocol parser runs, so this suite pins, per tool: the integral
float answers what the integer answers, byte for byte (``1e19`` too, past the 64 bits pydantic
converts itself); the three others are refused as a transport tool error; and the published
``inputSchema`` still says integer or null and nothing wider. ``impact_of`` is not a protocol
tool: its ``depth``/``limit`` keep the lax reading, and the integral float is pinned for it too.

What this does not reach: a host that forwards an argument without validating it against the
published ``inputSchema``; the protocol parser's own reading is tested in ``packages/tools``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from protocol_client import call, list_tools

_READ = {"address": {"layer": "swc"}, "form": "index"}
_FIND = {"for_requirement": "REQ_001"}

#: One integer argument per protocol tool: (tool, the other arguments, the argument, its integer).
PROTOCOL_ARGUMENTS = [("read", _READ, "limit", 1), ("find", _FIND, "candidates", 2)]


@pytest.mark.usefixtures("model_root")
@pytest.mark.parametrize(("tool", "arguments", "key", "number"), PROTOCOL_ARGUMENTS, ids=["read", "find"])
class TestTheProtocolToolsReadTheSchemasInteger:
    def test_an_integral_float_answers_what_the_integer_answers(
        self, tool: str, arguments: dict[str, Any], key: str, number: int
    ) -> None:
        as_integer = call(tool, **arguments, **{key: number})
        assert "error" not in as_integer.structured, as_integer.structured
        assert call(tool, **arguments, **{key: float(number)}).texts == as_integer.texts

    def test_an_integral_float_past_64_bits_answers_what_the_integer_answers(
        self, tool: str, arguments: dict[str, Any], key: str, number: int
    ) -> None:
        # Pydantic converts an integral float only while it fits 64 bits; the protocol's own
        # bounds may refuse both spellings, so the answers are compared, never assumed to succeed.
        huge = 1e19
        assert huge.is_integer() and int(huge) > 2**63
        as_integer = call(tool, **arguments, **{key: int(huge)})
        as_float = call(tool, **arguments, **{key: huge})
        assert as_float.texts == as_integer.texts
        assert as_float.structured == as_integer.structured

    @pytest.mark.parametrize("wrong", ["fraction", "bool", "str"])
    def test_a_value_the_schema_refuses_is_refused(
        self, tool: str, arguments: dict[str, Any], key: str, number: int, wrong: str
    ) -> None:
        value = {"fraction": number + 0.5, "bool": True, "str": str(number)}[wrong]
        with pytest.raises(ToolError, match=key):
            call(tool, **arguments, **{key: value})


@pytest.mark.parametrize(("tool", "key"), [(tool, key) for tool, _, key, _ in PROTOCOL_ARGUMENTS], ids=["read", "find"])
def test_the_published_input_schema_is_still_the_integer(tool: str, key: str) -> None:
    (listed,) = [entry for entry in list_tools() if entry.name == tool]
    published = dict(listed.inputSchema["properties"][key])
    assert published.pop("anyOf") == [{"type": "integer"}, {"type": "null"}]
    assert set(published) <= {"default", "description"}, published


@pytest.mark.usefixtures("model_root")
@pytest.mark.parametrize("key", ["depth", "limit"])
def test_impact_of_reads_an_integral_float_as_the_integer(key: str) -> None:
    assert call("impact_of", name="ISpeed", **{key: 2.0}).texts == call("impact_of", name="ISpeed", **{key: 2}).texts
