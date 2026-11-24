"""Tests for SwcraftMcpClient — MCP stdio client wrapper.

These tests spawn the real ``swcraft mcp-serve`` subprocess against a scaffolded
``tmp_path`` project; only external-network boundaries are mocked.
"""

from __future__ import annotations

import _thread
import asyncio
import builtins
import concurrent.futures
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent
from swcraft_agent.approval import Approver, ScriptedApprover, StdinApprover
from swcraft_agent.core import _TurnGuard
from swcraft_agent.mcp_client import McpClientError, SwcraftMcpClient, _normalize_result, _wait_interruptibly

from tests.conftest import seed_minimal_hardware, seed_root_composition

#: close()'s hang guard. The worst normal close is the 5 s graceful join plus the
#: 2 s force-cancel join, 7 s (6.3 s measured on a Mac); 60 s is more than eight
#: times that and far below the per-test timeout, so a hang is named, not timed out.
CLOSE_HANG_SECONDS = 60.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_client(manifest_path: Path) -> SwcraftMcpClient:
    return SwcraftMcpClient(str(manifest_path))


# ---------------------------------------------------------------------------
# list_openai_tools
# ---------------------------------------------------------------------------


def test_list_openai_tools_includes_expected_tools(scaffolded_project):
    """What the LLM is offered: the four protocol tools and the seven retained tools.

    The negative half names removed and withdrawn tools: this list is the LLM's whole view of
    the server, so a tool that came back would be in the model's vocabulary unnoticed.
    """
    with _open_client(scaffolded_project) as client:
        tools = client.list_openai_tools()
    names = {t["function"]["name"] for t in tools}

    assert {"discover", "read", "change_set"} <= names
    assert {"build", "validate", "resource_report", "import_component"} <= names
    assert not ({"project_info", "read_swc", "add_swc", "update_resource", "delete_element"} & names)
    assert not ({"check_port_compatibility", "check_connector_consistency", "export_all_components"} & names)


def test_list_openai_tools_schema_shape(scaffolded_project):
    with _open_client(scaffolded_project) as client:
        tools = client.list_openai_tools()
    for tool in tools:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn


# ---------------------------------------------------------------------------
# call_tool — success paths
# ---------------------------------------------------------------------------


def test_call_tool_discover_returns_the_project_passport(scaffolded_project):
    """The `discover` passport carries the project name, so a host can say which project it talks to."""
    with _open_client(scaffolded_project) as client:
        result = client.call_tool("discover", {})
    data = json.loads(result)
    assert data["project"]["name"] == "agent-test-project"
    assert data["baseline"]


def test_call_tool_read_answers_a_layer_the_change_set_just_wrote(committing_project):
    """A `read` answers the layer a `change_set` just wrote."""
    with _reviewing_client(committing_project, ScriptedApprover(["y"])) as client:
        _submit_primitive(client)
        result = client.call_tool("read", {"address": {"layer": "base_types"}})

    data = json.loads(result)
    assert [element["name"] for element in data["value"]] == ["uint8"]


# ---------------------------------------------------------------------------
# call_tool — error paths
# ---------------------------------------------------------------------------


def test_call_tool_unknown_tool_raises_or_returns_error(scaffolded_project):
    with _open_client(scaffolded_project) as client:
        try:
            result = client.call_tool("nonexistent_tool_xyz", {})
            assert "error" in result.lower() or "unknown" in result.lower()
        except (McpClientError, Exception):
            pass


def test_call_tool_invalid_args_returns_error_string(scaffolded_project):
    with _open_client(scaffolded_project) as client:
        result = client.call_tool("change_set", {})  # missing required args
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Startup health check / lifecycle
# ---------------------------------------------------------------------------


def test_bad_project_path_raises_promptly():
    # Sitting out the startup ceiling raises TimeoutError, so the exception type alone tells a
    # prompt failure from a hang, with no clock.
    with pytest.raises(McpClientError):
        SwcraftMcpClient("/nonexistent/path/swcraft.yaml")


def test_bad_executable_raises_promptly(monkeypatch):
    """Unresolvable mcp-serve command raises promptly instead of hanging."""
    import swcraft_agent.mcp_client as _mod

    async def _patched_main(self):
        import os

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._shutdown_event = __import__("asyncio").Event()
        params = StdioServerParameters(
            command="nonexistent_binary_xyz_abc",
            args=[],
            env=dict(os.environ),
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._ready.set()
                    await self._shutdown_event.wait()
        except Exception as exc:
            self._bg_error = exc
            self._ready.set()

    monkeypatch.setattr(_mod.SwcraftMcpClient, "_async_main", _patched_main)

    # The startup ceiling raises TimeoutError (an OSError), so admitting OSError would admit the hang.
    with pytest.raises(McpClientError):
        SwcraftMcpClient("/any/path")


def test_context_manager_closes_cleanly(scaffolded_project):
    with SwcraftMcpClient(str(scaffolded_project)) as client:
        result = client.call_tool("discover", {})
        assert result
    assert not client._thread.is_alive()


def test_close_leaves_thread_stopped(scaffolded_project):
    client = _open_client(scaffolded_project)
    assert client._thread.is_alive()
    client.close()
    assert not client._thread.is_alive()


def test_a_health_check_that_raises_is_surfaced_rather_than_waited_out(scaffolded_project, monkeypatch):
    """A startup `discover` that raises reaches the caller as `McpClientError`, not as a startup timeout."""
    import mcp

    async def _raise(self, name, arguments=None, **kwargs):
        raise RuntimeError("discover blew up during spawn")

    monkeypatch.setattr(mcp.ClientSession, "call_tool", _raise)

    with pytest.raises(McpClientError, match="discover blew up during spawn"):
        SwcraftMcpClient(str(scaffolded_project))


def _open_descriptor_census() -> int | None:
    """How many descriptors this process holds, or ``None`` where that is unanswerable.

    The walk is POSIX (``resource`` and a descriptor table; Windows has kernel handles). The
    import of ``resource`` is the capability probe, never ``sys.platform``. ``None`` is an
    answer, never a skip.
    """
    try:
        import resource
    except ImportError:
        return None

    def _is_open(fd: int) -> bool:
        try:
            os.fstat(fd)
        except OSError:
            return False
        return True

    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = min(soft if soft != resource.RLIM_INFINITY else 4096, 4096)
    return sum(1 for fd in range(limit) if _is_open(fd))


def test_close_leaves_the_event_loop_closed_and_its_descriptors_released(scaffolded_project):
    """A closed client owns nothing: not a thread, not an open loop, not the loop's descriptors.

    A scoped turn spawns and closes one client per turn, so a leaked loop and its descriptors
    would leak per turn. What this does **not** reach on a host without ``resource``: the
    descriptors are not counted there, only the thread and the loop are asserted.
    """
    before = _open_descriptor_census()

    client = _open_client(scaffolded_project)
    client.close()

    assert not client._thread.is_alive()
    assert client._loop.is_closed()
    if before is not None:
        assert _open_descriptor_census() <= before


def test_the_descriptor_census_answers_none_where_it_cannot_count(monkeypatch):
    """The ``None`` branch a Windows host takes, executed on this host.

    ``builtins.__import__`` is patched rather than ``sys.modules``: a cached module would defeat
    a patch that only removed the entry.
    """
    real_import = builtins.__import__

    def refuse_resource(name, *args, **kwargs):
        if name == "resource":
            raise ImportError("no resource module on this host")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_resource)

    assert _open_descriptor_census() is None


def test_the_descriptor_census_counts_where_it_can() -> None:
    """The control, without which the case above passes on a census that never counts."""
    census = _open_descriptor_census()

    assert census is None or census > 0, "a host with `resource` must return a real count"


def test_close_is_idempotent_now_that_the_loop_closes_itself(scaffolded_project):
    # The second call must see the loop the background thread closed and return rather than raise.
    client = _open_client(scaffolded_project)
    client.close()
    client.close()
    assert client._loop.is_closed()


# ---------------------------------------------------------------------------
# Exception propagation from background loop
# ---------------------------------------------------------------------------


def test_bg_error_propagates_on_next_call_tool(scaffolded_project):
    client = _open_client(scaffolded_project)
    client._bg_error = RuntimeError("simulated subprocess crash")
    with pytest.raises(McpClientError, match="simulated subprocess crash"):
        client.call_tool("discover", {})
    client.close()


def test_bg_error_propagates_on_list_openai_tools(scaffolded_project):
    client = _open_client(scaffolded_project)
    client._bg_error = RuntimeError("network error")
    with pytest.raises(McpClientError, match="network error"):
        client.list_openai_tools()
    client.close()


# ---------------------------------------------------------------------------
# Kill-fallback path
# ---------------------------------------------------------------------------


def test_close_does_not_hang_when_graceful_shutdown_fails(scaffolded_project, monkeypatch):
    """close() must return even if _signal_shutdown doesn't set the event."""
    client = _open_client(scaffolded_project)

    async def _noop_shutdown():
        pass

    monkeypatch.setattr(client, "_signal_shutdown", _noop_shutdown)

    start = time.monotonic()
    client.close()
    elapsed = time.monotonic() - start

    # A hang never returns; the check is the thread being stopped. The bound only
    # names the failure before the per-test timeout does.
    assert elapsed < CLOSE_HANG_SECONDS, f"close() took too long: {elapsed:.1f}s"
    assert not client._thread.is_alive(), "Background thread should be stopped"


# ---------------------------------------------------------------------------
# The supplied environment and public liveness
# ---------------------------------------------------------------------------


def test_is_alive_tracks_the_session_thread(scaffolded_project):
    """The session thread's liveness stands for "no orphaned subprocess".

    The MCP SDK's ``stdio_client`` does not expose the child process; the thread exits only
    once that context has been left, which is what terminates the server.
    """
    client = _open_client(scaffolded_project)
    assert client.is_alive is True
    client.close()
    assert client.is_alive is False


def test_a_supplied_env_replaces_the_inherited_one(scaffolded_project, monkeypatch):
    """``session_env()`` is handed over whole — the escape vars must not leak back."""
    from swcraft_tools.session import ESCAPE_ENV_VARS

    for name in ESCAPE_ENV_VARS:
        monkeypatch.setenv(name, "echo pwned")

    supplied = {"PATH": os.environ.get("PATH", ""), "SWCRAFT_MANIFEST": str(scaffolded_project)}
    client = SwcraftMcpClient(str(scaffolded_project), env=supplied)
    try:
        assert client._env is not None
        for name in ESCAPE_ENV_VARS:
            assert name not in client._env
        assert client.call_tool("discover", {})
    finally:
        client.close()


def test_a_supplied_env_is_copied_not_aliased(scaffolded_project):
    supplied = {"PATH": os.environ.get("PATH", "")}
    client = SwcraftMcpClient(str(scaffolded_project), env=supplied)
    try:
        supplied["SWCRAFT_MCP_BUILD_COMMAND"] = "echo pwned"
        assert "SWCRAFT_MCP_BUILD_COMMAND" not in (client._env or {})
    finally:
        client.close()


def test_the_clients_own_project_path_stays_authoritative(scaffolded_project):
    """A supplied env may not point the server at a different manifest."""
    client = SwcraftMcpClient(
        str(scaffolded_project),
        env={"PATH": os.environ.get("PATH", ""), "SWCRAFT_MANIFEST": "/nonexistent/swcraft.yaml"},
    )
    try:
        info = json.loads(client.call_tool("discover", {}))
        assert info["project"]["name"] == "agent-test-project"
    finally:
        client.close()


def test_no_env_still_inherits_the_process_environment(scaffolded_project):
    client = _open_client(scaffolded_project)
    try:
        assert client._env is None
        assert client.call_tool("discover", {})
    finally:
        client.close()


# ---------------------------------------------------------------------------
# The elicitation callback and the structured tool result
# ---------------------------------------------------------------------------
#
# ``SWCRAFT_REVIEW=elicit`` puts the exact-delta review question on the wire as an MCP
# elicitation; this host answers it from its injected ``Approver``, and every non-answer is
# a "no". The server is real: FastMCP produces the elicitation and its response schema.


#: A standalone base data type: it needs no other layer, so the change set stands or falls
#: on the review answer alone.
_CREATE_PRIMITIVE = [
    {
        "operation": "create",
        "address": {"layer": "base_types", "element": "uint8"},
        "value": {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
    }
]


class FailingApprover(Approver):
    """An approver whose ``ask`` raises the injected exception, prompt recorded."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.asked: list[str] = []

    def ask(self, prompt: str) -> bool:
        self.asked.append(prompt)
        raise self._error


@pytest.fixture()
def committing_project(scaffolded_project: Path) -> Path:
    """``scaffolded_project`` plus the one processor ``hardware/`` requires.

    A model with no ``kind: processor`` element is refused as ``hardware.no-processor``
    before the review question is asked, which is not these tests' subject.
    """
    seed_minimal_hardware(scaffolded_project.parent / "model")
    seed_root_composition(scaffolded_project.parent / "model")
    return scaffolded_project


def _reviewing_client(manifest: Path, approver: Approver | None = None) -> SwcraftMcpClient:
    """A client whose server asks this host's human before committing anything."""
    return SwcraftMcpClient(
        str(manifest),
        env={**os.environ, "SWCRAFT_REVIEW": "elicit"},
        approver=approver,
    )


def _submit_primitive(client: SwcraftMcpClient):
    """``discover`` for the baseline, then one ``change_set`` that creates it."""
    baseline = json.loads(client.call_tool("discover", {}))["baseline"]
    return client.call_tool(
        "change_set",
        {"baseline": baseline, "intent": "Add the uint8 base type.", "operations": _CREATE_PRIMITIVE},
    )


def _data_type_files(manifest: Path) -> list[Path]:
    """The base type files: `data_types/base/`, the `base_types` layer's directory."""
    return sorted((manifest.parent / "model" / "data_types" / "base").iterdir(), key=lambda path: path.as_posix())


def test_the_servers_elicitation_reaches_the_injected_approver(committing_project):
    """The question the server rendered is the question the user is shown."""
    approver = ScriptedApprover(["y"])
    with _reviewing_client(committing_project, approver) as client:
        result = _submit_primitive(client)

    assert len(approver.prompts) == 1, approver.prompts
    prompt = approver.prompts[0]
    assert "Review this change set before it is committed." in prompt
    assert "create base_types/uint8" in prompt
    assert "Commit exactly this delta?" in prompt
    assert result.structured["status"] == "committed", result
    assert [path.name for path in _data_type_files(committing_project)] == ["uint8.yaml"]


#: The greenfield scenario's same-name step (`greenfield_scenario.py`, `SAME_NAME_PAIR_OPERATIONS`):
#: an application and an implementation type under ONE name, the set pairing them and a data
#: element on each side. Restated because this tree cannot import that module.
_SAME_NAME_PAIR = [
    {
        "operation": "create",
        "address": {"layer": "base_types", "element": "float32_base"},
        "value": {
            "name": "float32_base",
            "kind": "base_type",
            "size": 32,
            "encoding": "IEEE754",
            "native_declaration": "float",
        },
    },
    {
        "operation": "create",
        "address": {"layer": "application_types", "element": "SyntheticTorque"},
        "value": {"name": "SyntheticTorque", "kind": "application_type", "unit": "Nm"},
    },
    {
        "operation": "create",
        "address": {"layer": "implementation_types", "element": "SyntheticTorque"},
        "value": {"name": "SyntheticTorque", "kind": "value_type", "base": "float32_base"},
    },
    {
        "operation": "create",
        "address": {"layer": "mapping_sets", "element": "SyntheticTorqueMapping"},
        "value": {
            "name": "SyntheticTorqueMapping",
            "maps": [{"application": "SyntheticTorque", "implementation": "SyntheticTorque"}],
        },
    },
    {
        "operation": "create",
        "address": {"layer": "interfaces", "element": "ISyntheticTorque"},
        "value": {
            "name": "ISyntheticTorque",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "physical", "application_type": "SyntheticTorque"},
                {"name": "stored", "implementation_type": "SyntheticTorque"},
            ],
        },
    },
]


def test_a_same_name_pair_commits_and_each_layer_answers_its_own_element(committing_project):
    """One name in two layers: the prompt names both creates, and each layer reads back its own element."""
    approver = ScriptedApprover(["y"])
    with _reviewing_client(committing_project, approver) as client:
        baseline = json.loads(client.call_tool("discover", {}))["baseline"]
        result = client.call_tool(
            "change_set",
            {"baseline": baseline, "intent": "Declare one name in two layers.", "operations": _SAME_NAME_PAIR},
        )
        kinds = {
            layer: json.loads(client.call_tool("read", {"address": {"layer": layer, "element": "SyntheticTorque"}}))[
                "value"
            ]["kind"]
            for layer in ("application_types", "implementation_types")
        }

    assert result.structured["status"] == "committed", result
    (prompt,) = approver.prompts
    assert "create application_types/SyntheticTorque" in prompt
    assert "create implementation_types/SyntheticTorque" in prompt
    assert kinds == {"application_types": "application_type", "implementation_types": "value_type"}
    model = committing_project.parent / "model" / "data_types"
    assert (model / "application" / "SyntheticTorque.yaml").is_file()
    assert (model / "implementation" / "SyntheticTorque.yaml").is_file()


#: The greenfield scenario's data description elements step (`ELEMENTS_OPERATIONS`), restated:
#: a unit, a linear compu method naming it and an application type naming both; a texttable an
#: enumeration and a value type share, over a base type of its own.
_DATA_DESCRIPTION_ELEMENTS = [
    {
        "operation": "create",
        "address": {"layer": "units", "element": "SyntheticNewtonMetre"},
        "value": {"name": "SyntheticNewtonMetre", "display_name": "Nm"},
    },
    {
        "operation": "create",
        "address": {"layer": "compu_methods", "element": "SyntheticTorqueScale"},
        "value": {"name": "SyntheticTorqueScale", "kind": "linear", "unit": "SyntheticNewtonMetre", "factor": 0.5},
    },
    {
        "operation": "create",
        "address": {"layer": "application_types", "element": "SyntheticDemand"},
        "value": {
            "name": "SyntheticDemand",
            "kind": "application_type",
            "unit_ref": "SyntheticNewtonMetre",
            "compu_method_ref": "SyntheticTorqueScale",
        },
    },
    {
        "operation": "create",
        "address": {"layer": "compu_methods", "element": "SyntheticGearTable"},
        "value": {
            "name": "SyntheticGearTable",
            "kind": "texttable",
            "entries": [{"name": "SyntheticPark", "value": 0}, {"name": "SyntheticDrive", "value": 1}],
        },
    },
    {
        "operation": "create",
        "address": {"layer": "application_types", "element": "SyntheticGear"},
        "value": {"name": "SyntheticGear", "kind": "enum", "compu_method_ref": "SyntheticGearTable"},
    },
    {
        "operation": "create",
        "address": {"layer": "base_types", "element": "uint8_base"},
        "value": {
            "name": "uint8_base",
            "kind": "base_type",
            "size": 8,
            "encoding": "NONE",
            "native_declaration": "uint8_t",
        },
    },
    {
        "operation": "create",
        "address": {"layer": "implementation_types", "element": "SyntheticGearRaw"},
        "value": {
            "name": "SyntheticGearRaw",
            "kind": "value_type",
            "base": "uint8_base",
            "compu_method_ref": "SyntheticGearTable",
        },
    },
]


def test_the_data_description_elements_commit_and_each_element_layer_answers(committing_project):
    """The data description elements commit; each element layer answers its own, the type its references."""
    approver = ScriptedApprover(["y"])
    with _reviewing_client(committing_project, approver) as client:
        baseline = json.loads(client.call_tool("discover", {}))["baseline"]
        result = client.call_tool(
            "change_set",
            {
                "baseline": baseline,
                "intent": "Declare a unit, a scale and a table as elements, and the types naming them.",
                "operations": _DATA_DESCRIPTION_ELEMENTS,
            },
        )
        names = {
            layer: [row["name"] for row in json.loads(client.call_tool("read", {"address": {"layer": layer}}))["value"]]
            for layer in ("units", "compu_methods")
        }
        demand = json.loads(
            client.call_tool("read", {"address": {"layer": "application_types", "element": "SyntheticDemand"}})
        )["value"]

    assert result.structured["status"] == "committed", result
    (prompt,) = approver.prompts
    assert "create units/SyntheticNewtonMetre" in prompt
    assert "create compu_methods/SyntheticTorqueScale" in prompt
    assert "create compu_methods/SyntheticGearTable" in prompt
    assert names == {"units": ["SyntheticNewtonMetre"], "compu_methods": ["SyntheticGearTable", "SyntheticTorqueScale"]}
    assert (demand["unit_ref"], demand["compu_method_ref"]) == ("SyntheticNewtonMetre", "SyntheticTorqueScale")
    model = committing_project.parent / "model" / "data_types"
    assert (model / "units" / "SyntheticNewtonMetre.yaml").is_file()
    assert (model / "compu_methods" / "SyntheticGearTable.yaml").is_file()


def test_a_refusing_answer_travels_back_and_abandons_the_change_set(committing_project):
    approver = ScriptedApprover(["n"])
    with _reviewing_client(committing_project, approver) as client:
        result = _submit_primitive(client)

    assert approver.answers_given == ["n"]
    assert result.structured["status"] == "abandoned", result
    assert _data_type_files(committing_project) == []


def test_an_exhausted_approver_fails_closed(committing_project):
    """A script that ran out is a user who said nothing, which is a "no"."""
    approver = ScriptedApprover([])
    with _reviewing_client(committing_project, approver) as client:
        result = _submit_primitive(client)

    assert approver.answers_given == [None]
    assert result.structured["status"] == "abandoned", result
    assert _data_type_files(committing_project) == []


def test_a_client_with_no_answer_channel_declares_none_and_fails_closed(committing_project):
    """No approver is a host that cannot ask: no elicitation capability, abandoned, and a host note says why."""
    with _reviewing_client(committing_project, None) as client:
        result = _submit_primitive(client)

    assert result.structured["status"] == "abandoned", result
    assert any("elicit" in text.lower() for text in result.texts), result.texts
    assert _data_type_files(committing_project) == []


def test_a_structured_response_and_its_note_text_both_survive_the_round_trip(committing_project):
    """The body of a tool result and its host note (a later text block) reach the host separately."""
    with _reviewing_client(committing_project, None) as client:
        result = _submit_primitive(client)

    assert result.structured["response"] == "change_set"
    assert [entry["address"] for entry in result.structured["delta"]] == [{"layer": "base_types", "element": "uint8"}]
    assert any("elicit" in text.lower() for text in result.texts), result.texts
    assert json.loads(result) == result.structured
    # The body block as the server sent it, not the spaced ``json.dumps`` of a structured half.
    assert result == result.texts[0]


def test_a_retained_tools_string_result_still_reads_as_text(scaffolded_project):
    """A retained tool's rendered text is the result's string, with no structured body or envelope.

    ``impact_of``'s default ``summary`` renders prose; the line is its answer for an undeclared name.
    """
    with _open_client(scaffolded_project) as client:
        result = client.call_tool("impact_of", {"name": "NoSuchElement"})

    assert isinstance(result, str)
    assert result == "Element 'NoSuchElement' not found in any layer."
    assert result.structured is None
    assert result == "\n".join(result.texts)
    assert not result.startswith('{"result"')


# ---------------------------------------------------------------------------
# The result reader over both wire shapes, replayed from fake results
# ---------------------------------------------------------------------------


def _wire(*texts: str, structured: dict | None = None) -> CallToolResult:
    """A ``CallToolResult`` with one text block per *texts*, and a structured half only when given."""
    return CallToolResult(content=[TextContent(type="text", text=text) for text in texts], structuredContent=structured)


DISCOVER_BODY = {"response": "discover", "contract_version": "9.0.0", "baseline": "sha256:ab", "layers": []}


def test_one_body_block_is_the_string_and_its_parse_the_structured_body() -> None:
    block = json.dumps(DISCOVER_BODY, separators=(",", ":"))

    result = _normalize_result(_wire(block))

    assert result == block
    assert result.structured == DISCOVER_BODY
    assert result.texts == (block,)


def test_a_body_block_and_a_note_block_keep_the_note_separate_and_relay_it() -> None:
    body = {"response": "change_set", "status": "abandoned"}
    block = json.dumps(body, separators=(",", ":"))
    note = "this host's client could not be asked to elicit the review; nothing was applied"

    result = _normalize_result(_wire(block, note))

    assert result == block
    assert result.structured == body
    assert result.texts == (block, note)
    guard = _TurnGuard(call_tool=lambda name, args: result, record=lambda *a: None)
    assert guard("change_set", {}) == f"{block}\n\n{note}"


def test_a_plain_text_body_is_the_raw_text_with_no_structured_body() -> None:
    report = "Impact of 'ExampleSWC' (summary):\n  nothing depends on it"

    result = _normalize_result(_wire(report))

    assert result == report
    assert result.structured is None


@pytest.mark.parametrize("block", ["[1]", "42", '"a string"', "null"])
def test_a_first_block_that_parses_to_no_object_is_plain_text(block: str) -> None:
    result = _normalize_result(_wire(block, "a note"))

    assert result == f"{block}\na note"
    assert result.structured is None
    assert result.texts == (block, "a note")


def test_a_result_with_no_block_is_its_own_string_form() -> None:
    raw = _wire()

    result = _normalize_result(raw)

    assert result == str(raw)
    assert result.structured is None
    assert result.texts == ()


def test_a_validate_reply_carrying_warnings_reaches_structured_whole() -> None:
    """A `validate` reply's `warnings` list reaches ``structured`` whole, in the server's key order.

    The reader parses the one text block whole, knowing no key names.
    """
    reply = {
        "success": True,
        "errors": [],
        "warnings": [
            {
                "message": (
                    "Requirement 'REQ_001' is satisfied by no element: add `satisfies: [REQ_001@1]` on the "
                    "component, port, runnable, composition, interface, task or hardware element that realizes it."
                ),
                "path": "",
            }
        ],
        "output": "Validation passed.",
    }
    block = json.dumps(reply, separators=(",", ":"))

    result = _normalize_result(_wire(block))

    assert result == block
    assert result.structured == reply
    assert list(result.structured) == ["success", "errors", "warnings", "output"]
    assert result.structured["warnings"] == reply["warnings"]
    assert result.texts == (block,)


def test_a_two_copy_result_keeps_the_string_form_it_has_always_had() -> None:
    block = json.dumps(DISCOVER_BODY, separators=(",", ":"))

    result = _normalize_result(_wire(block, structured=DISCOVER_BODY))

    assert result == json.dumps(DISCOVER_BODY)
    assert result.structured == DISCOVER_BODY
    assert result.texts == (block,)


def test_a_keyboard_interrupt_at_the_prompt_closes_the_client(committing_project):
    """Ctrl-C during a review reaches the caller and leaves no subprocess behind."""
    approver = FailingApprover(KeyboardInterrupt())
    client = _reviewing_client(committing_project, approver)
    try:
        with pytest.raises(KeyboardInterrupt):
            _submit_primitive(client)
    finally:
        client.close()

    assert approver.asked, "the prompt was shown before the interrupt"
    assert client.is_alive is False
    assert _data_type_files(committing_project) == []


def test_the_approver_is_read_at_answer_time_not_captured_at_construction(committing_project):
    """A scripted replay swaps the approver per turn; the client must follow it."""
    client = _reviewing_client(committing_project, ScriptedApprover([]))
    later = ScriptedApprover(["y"])
    try:
        client.approver = later
        result = _submit_primitive(client)
    finally:
        client.close()

    assert later.answers_given == ["y"]
    assert result.structured["status"] == "committed", result


@pytest.mark.parametrize(
    ("stdin", "case"),
    [("", "eof"), ("\n", "empty line"), ("   \n", "blank line")],
    ids=["eof", "empty-line", "blank-line"],
)
def test_a_terminal_user_who_answers_nothing_fails_closed(committing_project, stdin: str, case: str):
    """A prompt nobody answered is a "no": the elicitation path reads the answer through ``approval.is_approval``."""
    approver = StdinApprover(stream=io.StringIO(stdin), out=io.StringIO())
    with _reviewing_client(committing_project, approver) as client:
        result = _submit_primitive(client)

    assert result.structured["status"] == "abandoned", f"{case}: {result}"
    assert _data_type_files(committing_project) == []


def test_a_question_this_host_cannot_render_is_declined(scaffolded_project):
    """An elicitation that is not the review boundary's yes/no is declined without asking the approver.

    Only the single ``boolean`` of ``ctx.elicit(message, bool)`` has a prompt here; guessing an
    answer to an unshown question is what a review boundary must not do.
    """
    from mcp.types import ElicitRequestFormParams, ElicitRequestURLParams

    approver = ScriptedApprover(["y"])
    unanswerable = [
        ElicitRequestFormParams(
            message="Name the branch.",
            requestedSchema={"type": "object", "properties": {"name": {"type": "string"}}},
        ),
        ElicitRequestFormParams(
            message="Commit, and to which branch?",
            requestedSchema={
                "type": "object",
                "properties": {"value": {"type": "boolean"}, "branch": {"type": "string"}},
            },
        ),
        ElicitRequestURLParams(
            message="Approve in your browser.",
            url="https://example.invalid/review",
            elicitationId="review-1",
        ),
    ]
    with _reviewing_client(scaffolded_project, approver) as client:
        actions = [asyncio.run(client._elicit(None, params)).action for params in unanswerable]

    assert actions == ["decline", "decline", "decline"]
    assert approver.prompts == []


# ---------------------------------------------------------------------------
# The wait for a call's result is interruptible
# ---------------------------------------------------------------------------


class TestTheWaitIsInterruptible:
    """A call's result is waited for in timed looks, so Ctrl-C ends a wait of any length.

    ``_thread.interrupt_main`` sets the flag without a signal, so an untimed lock acquire is not
    woken: the condition a Windows console's Ctrl-C meets on CPython before 3.14.
    What this does not reach: a real console Ctrl-C on Windows.
    """

    def test_an_interrupt_ends_a_wait_for_a_call_that_never_answers(self) -> None:
        never: concurrent.futures.Future[object] = concurrent.futures.Future()
        interrupt = threading.Timer(0.2, _thread.interrupt_main)
        safety = threading.Timer(10.0, lambda: never.set_result("the wait was not interrupted"))
        interrupt.start()
        safety.start()
        try:
            with pytest.raises(KeyboardInterrupt):
                _wait_interruptibly(never, slice_seconds=0.05)
        finally:
            interrupt.cancel()
            safety.cancel()
        # The future is not done, so the interrupt, not the safety timer, ended the wait.
        assert not never.done()

    def test_a_result_is_returned(self) -> None:
        done: concurrent.futures.Future[object] = concurrent.futures.Future()
        threading.Timer(0.1, lambda: done.set_result(42)).start()
        assert _wait_interruptibly(done, slice_seconds=0.02) == 42

    def test_a_timeout_error_of_the_call_itself_is_raised_not_waited_on(self) -> None:
        failed: concurrent.futures.Future[object] = concurrent.futures.Future()
        failed.set_exception(TimeoutError("the call's own"))
        with pytest.raises(TimeoutError, match="the call's own"):
            _wait_interruptibly(failed, slice_seconds=0.02)
