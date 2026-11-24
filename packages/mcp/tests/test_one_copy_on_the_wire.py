"""Every tool result is sent once: one text block, no structured half, no output schema.

FastMCP sends a result carrying a structured half twice in one JSON-RPC message; the server's
``_OneCopy`` middleware drops the second copy, since one copy is supported and two only inflate the
context. This module holds the wire to that through the in-memory ``fastmcp.Client`` a host talks to.

The sweep is derived from the listing: the live ``tools/list`` must name exactly the tools the labels
of :data:`CALLS` reach, so a new tool fails here until it has a case. The listing publishes no
``outputSchema`` (a published schema with no structured half makes a call ``is_error``). Each call is
answered (``not is_error`` and the tool's own predicate, since a raising tool also arrives as one
block), carries no structured half and exactly one text block: a mapping body as its own
``compact_json``, a ``str`` body as the raw text. ``discover`` and ``read`` are also compared with
``handle_request`` called directly.

What it does not reach: argument shapes the smallest answering call does not exercise; a tool's other
refusal branches; a client other than FastMCP's. ``build`` needs Node.js.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_mcp.server as server
from fastmcp import Client
from fastmcp.tools import ToolResult
from guide_support import fixture_corpus, install_corpus
from mcp.types import TextContent
from protocol_client import list_tools
from scratch_git import quiet_maintenance
from swcraft_tools.editing.delta import summarize_response
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.read import compact_json, measure
from swcraft_tools.project import require_project

from tests.test_audit_completeness import git
from tests.test_review_mode import PLAIN, arguments, set_mode, use_project


def _import_arguments(project: Path) -> dict[str, Any]:
    """An exported component and the current baseline: what ``import_component`` plans from.

    The export is made in-process, so the gate's own ``import_component`` call is the only one on the wire.
    """
    source = project.parent / "import-source.arxml"
    exported = server.export_component(swc_name="ExampleSWC", output_path=str(source))
    assert exported["success"], exported
    return {"source": str(source), "baseline": fixtures.current_baseline_token(project)}


#: The smallest arguments that reach an answer, keyed by label: the tool's name, or ``tool/form`` for a
#: second probe of the same tool.
CALLS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "discover": lambda project: {},
    "read": lambda project: {"address": {"layer": "swc"}},
    "find": lambda project: {"names": ["ExampleSWC"]},
    "change_set": lambda project: arguments(PLAIN, project),
    "model_diff": lambda project: {},
    "impact_of": lambda project: {"name": "ExampleSWC"},
    "resource_report": lambda project: {},
    "architecture_metrics": lambda project: {"since": "HEAD"},
    "guide": lambda project: {"search": "reading"},
    "build": lambda project: {},
    "validate": lambda project: {},
    "export_component": lambda project: {
        "swc_name": "ExampleSWC",
        "output_path": str(project.parent / "ExampleSWC.arxml"),
    },
    "export_component/every-component": lambda project: {"output_dir": str(project.parent / "exports")},
    "import_component": _import_arguments,
}

#: When each call counts as answered; exactly one of the two tables applies to each tool.
MAPPING_ANSWERED: dict[str, Callable[[dict[str, Any]], bool]] = {
    "discover": lambda body: "error" not in body,
    "read": lambda body: body.get("response") == "read",
    "find": lambda body: body.get("response") == "find",
    "resource_report": lambda body: body.get("response") == "resources",
    "architecture_metrics": lambda body: body.get("response") == "metrics" and body.get("movement") is not None,
    "guide": lambda body: body.get("response") == "guide" and bool(body.get("hits")),
    "model_diff": lambda body: body.get("response") == "diff",
    "change_set": lambda body: "error" not in body,
    "build": lambda body: body.get("success") is True,
    "validate": lambda body: body.get("success") is True,
    "export_component": lambda body: body.get("success") is True,
    "export_component/every-component": lambda body: body.get("success") is True,
    "import_component": lambda body: body.get("success") is True,
}
TEXT_ANSWERED: dict[str, Callable[[str], bool]] = {
    "impact_of": lambda text: text.startswith("Impact of 'ExampleSWC'"),
}


def tool_of(label: str) -> str:
    """The tool a label calls: the part before the first ``/``."""
    return label.partition("/")[0]


@pytest.fixture
def answering_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``examples/basic`` through the vector harness, committed once for ``model_diff``.

    The review mode stays ``stage-only``, so a staged body is deterministic; the fixture guide is
    installed beside it for ``guide``.
    """
    install_corpus(monkeypatch, tmp_path / "installed-guide", fixture_corpus())
    project = use_project(PLAIN, tmp_path, monkeypatch)
    git("init", cwd=project)
    quiet_maintenance(project)
    git("add", "-A", cwd=project)
    git("commit", "-m", "the baseline model", cwd=project)
    return project


async def _araw(name: str, arguments: dict[str, Any], *, list_first: bool = False) -> Any:
    async with Client(server.mcp) as client:
        if list_first:
            await client.list_tools()
        return await client.call_tool(name, arguments, raise_on_error=False)


def raw_call(name: str, arguments: dict[str, Any] | None = None, *, list_first: bool = False) -> Any:
    """``tools/call`` returning FastMCP's own, unsplit result object."""
    return asyncio.run(_araw(name, arguments or {}, list_first=list_first))


def texts_of(result: Any) -> list[str]:
    return [block.text for block in result.content if getattr(block, "type", None) == "text"]


def _assert_one_copy(name: str, result: Any) -> str:
    """The shape every answered call must have; returns its one block."""
    assert not result.is_error, (name, texts_of(result))
    assert result.structured_content is None, f"{name}: a structured half is a second copy on the wire"
    assert len(result.content) == 1, f"{name}: {len(result.content)} content blocks, expected one"
    (text,) = texts_of(result)
    return text


def test_the_listing_is_the_call_table_and_publishes_no_output_schema() -> None:
    tools = list_tools()
    assert {tool.name for tool in tools} == {tool_of(label) for label in CALLS}, (
        "the live tools/list and this gate's CALLS table disagree: a tool without a case is a tool whose "
        "wire shape nothing checks"
    )
    assert set(MAPPING_ANSWERED) | set(TEXT_ANSWERED) == set(CALLS)
    assert not set(MAPPING_ANSWERED) & set(TEXT_ANSWERED)
    published = [tool.name for tool in tools if tool.outputSchema is not None]
    assert not published, f"an outputSchema with no structured half is an 'Output validation error': {published}"


def test_the_registered_tools_keep_their_schema() -> None:
    """The listing publishes a copy: the registered objects are never assigned.

    Assigning them would make a ``str`` tool's shape depend on whether a listing had already happened.
    """
    assert all(tool.outputSchema is None for tool in list_tools())
    registered = asyncio.run(server.mcp.list_tools(run_middleware=False))
    assert any(tool.output_schema is not None for tool in registered)


@pytest.mark.parametrize("name", sorted(CALLS))
def test_every_tool_answers_with_one_block_and_no_structured_half(name: str, answering_project: Path) -> None:
    result = raw_call(tool_of(name), CALLS[name](answering_project))
    text = _assert_one_copy(name, result)

    if name in MAPPING_ANSWERED:
        body = json.loads(text)
        assert isinstance(body, dict), f"{name}: the body block is not a JSON object"
        assert text == compact_json(body), f"{name}: the body block is not the compact dump of itself"
        assert list(body) != ["result"], f"{name}: the body arrived in FastMCP's {{'result': ...}} envelope"
        assert MAPPING_ANSWERED[name](body), f"{name} did not answer: {body}"
    else:
        assert not text.startswith('{"result"'), f"{name}: the text arrived in an envelope"
        assert TEXT_ANSWERED[name](text), f"{name} did not answer: {text[:200]}"


@pytest.mark.parametrize(
    ("name", "request_"),
    [
        ("discover", {"operation": "discover"}),
        ("read", {"operation": "read", "address": {"layer": "swc"}}),
    ],
)
def test_the_read_only_bodies_are_the_engines_own_response(
    name: str, request_: dict[str, Any], answering_project: Path
) -> None:
    arguments_ = {key: value for key, value in request_.items() if key != "operation"}
    text = _assert_one_copy(name, raw_call(name, arguments_))
    # Under the server's channel keyword, which its dispatch passes on every call.
    direct = handle_request(
        request_,
        project=require_project(str(answering_project / "swcraft.yaml")),
        discover_default=server.HOST_DISCOVER_DEFAULT,
    )
    assert json.loads(text) == direct
    assert text == compact_json(direct)


def test_a_removal_goes_out_as_the_edge_summarises_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one projection ``_handle`` applies to a body, and what it saves on the wire.

    The body sent is the engine's staged answer with the removed ``RAM0``'s ``before`` summarised,
    and nothing else differs.
    """
    vector = "remove-memory-resource-with-referrers-cleared"
    project = use_project(vector, tmp_path, monkeypatch)
    named = arguments(vector, project)
    body = json.loads(_assert_one_copy("change_set", raw_call("change_set", named)))
    direct = handle_request(
        {"operation": "change_set", **named}, project=require_project(str(project / "swcraft.yaml"))
    )

    assert body == summarize_response(direct)
    (removed,) = [entry for entry in body["delta"] if entry["address"] == {"layer": "hardware", "element": "RAM0"}]
    assert removed["before_form"] == "summary"
    assert measure(body) < measure(direct)


def test_the_string_branch_of_resource_report_is_raw_text(answering_project: Path) -> None:
    # A `dict | str` tool on its `str` branch: a memory the model does not declare.
    text = _assert_one_copy("resource_report", raw_call("resource_report", {"memory": "GhostRAM"}))
    assert text.startswith("ERROR:"), text


def test_a_protocol_refusal_is_one_block_holding_the_error_result(answering_project: Path) -> None:
    result = raw_call("read", {"address": {"layer": "swc", "element": "NoSuchSWC"}})
    body = json.loads(_assert_one_copy("read", result))
    assert body["response"] == "error"
    assert body["error"]


def test_a_host_note_is_its_own_block_after_the_body(answering_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``elicit`` with a client that cannot be asked: the body, then the note."""
    set_mode(monkeypatch, server.REVIEW_ELICIT)
    result = raw_call("change_set", arguments(PLAIN, answering_project))

    assert not result.is_error
    assert result.structured_content is None
    texts = texts_of(result)
    assert len(texts) == 2, texts
    body = json.loads(texts[0])
    assert texts[0] == compact_json(body)
    assert body["status"] == "abandoned"
    assert "could not be asked" in texts[1]


def test_a_masked_fault_is_one_error_block(answering_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("an unexpected fault")

    monkeypatch.setattr(server, "handle_request", explode)
    result = raw_call("discover")

    assert result.is_error
    assert result.structured_content is None
    assert len(result.content) == 1
    assert "an unexpected fault" not in texts_of(result)[0]


@pytest.mark.parametrize("list_first", [False, True], ids=["empty-tool-cache", "after-a-listing"])
def test_a_text_tools_shape_does_not_depend_on_call_order(
    list_first: bool, answering_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call answers in the one shape whether or not a ``tools/list`` came first.

    Every in-memory client shares one low-level server and its tool cache, so the cache is emptied:
    the first case then fetches the definitions through the middleware, where a cached
    ``outputSchema`` would make the call ``is_error``.
    """
    monkeypatch.setattr(server.mcp._mcp_server, "_tool_cache", {})
    # `impact_of`'s default `summary` is text.
    text = _assert_one_copy("impact_of", raw_call("impact_of", {"name": "ExampleSWC"}, list_first=list_first))
    assert text.startswith("Impact of 'ExampleSWC'")
    assert not text.startswith('{"result"')

    registered = asyncio.run(server.mcp.get_tool("impact_of"))
    assert registered.output_schema is not None
    assert registered.convert_result("hello").meta == {"fastmcp": {"wrap_result": True}}


def _through_one_copy(result: ToolResult) -> ToolResult:
    """*result* passed through ``_OneCopy.on_call_tool`` by a stub ``call_next``."""

    async def call_next(_context: Any) -> ToolResult:
        return result

    return asyncio.run(server._OneCopy().on_call_tool(None, call_next))


def test_the_middleware_drops_only_the_wrap_flag_and_mutates_nothing() -> None:
    meta = {"fastmcp": {"wrap_result": True, "other": 1}, "kept": "yes"}
    original = ToolResult(
        content=[TextContent(type="text", text="x"), TextContent(type="text", text="a note")],
        structured_content={"result": "x"},
        meta=meta,
    )

    rebuilt = _through_one_copy(original)

    assert [block.text for block in rebuilt.content] == ["x", "a note"]
    assert rebuilt.structured_content is None
    assert rebuilt.meta == {"fastmcp": {"other": 1}, "kept": "yes"}
    assert original.meta == {"fastmcp": {"wrap_result": True, "other": 1}, "kept": "yes"}
    assert original.structured_content == {"result": "x"}


def test_an_emptied_fastmcp_meta_is_dropped() -> None:
    rebuilt = _through_one_copy(
        ToolResult(content="x", structured_content={"result": "x"}, meta={"fastmcp": {"wrap_result": True}})
    )

    assert rebuilt.meta is None
    assert [block.text for block in rebuilt.content] == ["x"]


def test_an_error_result_stays_an_error() -> None:
    rebuilt = _through_one_copy(ToolResult(structured_content={"ok": False}, is_error=True))

    assert rebuilt.is_error
    assert [block.text for block in rebuilt.content] == [compact_json({"ok": False})]


def test_a_first_block_that_is_not_the_bodys_serialization_is_kept() -> None:
    note = TextContent(type="text", text="a note written first")
    rebuilt = _through_one_copy(ToolResult(content=[note], structured_content={"ok": True}))

    assert [block.text for block in rebuilt.content] == [compact_json({"ok": True}), "a note written first"]


def test_a_body_echoing_a_nan_still_drops_fastmcps_block() -> None:
    # A value comparison fails on NaN and would keep FastMCP's block beside the body: two copies.
    body = {"echo": float("nan"), "ok": False}
    rebuilt = _through_one_copy(ToolResult(content='{"ok":false,"echo":NaN}', structured_content=body))

    assert [block.text for block in rebuilt.content] == [compact_json(body)]
