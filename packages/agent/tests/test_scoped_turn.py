"""The agent turn end to end.

A real MCP subprocess against the **real** manifest, the real editing engine and the real
review elicitation; only the LLM and the human are doubles (``FakeLLMClient`` /
``DriverLLM`` and ``ScriptedApprover``).

* **No unapproved change reaches the real model.** The engine owns the change set: it
  stages in its own shadow, validates, asks the human through elicitation and applies
  atomically. Every case fingerprints the model before the turn.
* **The scope travels in the environment of a server spawned for this turn**
  (``SWCRAFT_REVIEW``, ``SWCRAFT_APPROVED_SCOPE``, ``SWCRAFT_SCOPE_ID``); an unscoped run
  keeps the long-lived client and sets only the first. No variable is reachable from
  anything the LLM emits.

The retained tools' containment is under "The two escapes the engine's scope cannot see".
"""

from __future__ import annotations

import ast
import json
import os
from collections import deque
from collections.abc import Callable
from pathlib import Path

import pytest
import swcraft_agent.core as _core
import yaml
from swcraft_agent.approval import ScriptedApprover
from swcraft_agent.core import SwcraftAgent
from swcraft_agent.llm_client import LLMClient
from swcraft_agent.mcp_client import McpClientError
from swcraft_tools import writer
from swcraft_tools.auditor import OUTSIDE_THE_PROJECT
from swcraft_tools.editing.audit import LIFECYCLE_OPERATIONS
from swcraft_tools.session import ESCAPE_ENV_VARS, model_fingerprint, pinned_escape_env

from tests.conftest import MODEL_SUBDIRS, FakeLLMClient, FakeMcp, declare_no_support, seed_root_composition

# ---------------------------------------------------------------------------
# Project fixture
# ---------------------------------------------------------------------------


def _seed_model(model_root: Path) -> None:
    """A small model reaching the layers every case below touches.

    `hardware/` and the root composition are mandatory; a memory names at least one core.
    """
    root = str(model_root)
    writer.create_data_type(root, "uint32_base", "base_type", size=32, encoding="NONE", native_declaration="uint32_t")
    writer.create_data_type(root, "uint32", "value_type", base="uint32_base")
    writer.create_interface(root, "ISpeed", [{"name": "speed", "implementation_type": "uint32"}])
    writer.create_swc(root, "EngineController", "powertrain")
    declare_no_support(root, "powertrain", "EngineController")
    writer.add_port(root, "EngineController", "pSpeed", "provide", "ISpeed")
    writer.create_hardware_element(root, "CPU", "processor")
    writer.create_hardware_element(root, "CORE0", "core", threshold=0.7)
    writer.create_hardware_element(
        root, "RAM0", "memory", memory_type="RAM", size=1024, accessible=["CORE0"], threshold=0.5
    )
    seed_root_composition(root, ["EngineController"])


def _write_manifest(root: Path, name: str) -> Path:
    manifest = root / "swcraft.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "name": name,
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return manifest


def _make_project(root: Path) -> Path:
    model_root = root / "model"
    for subdir in MODEL_SUBDIRS:
        (model_root / subdir).mkdir(parents=True, exist_ok=True)
    (root / "dist").mkdir(parents=True, exist_ok=True)
    _seed_model(model_root)
    return _write_manifest(root, "scoped-test-project")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A real, seeded SWCraft project; returns its manifest path."""
    return _make_project(tmp_path / "proj")


@pytest.fixture()
def protocol_project(tmp_path: Path) -> Path:
    """A seeded project that is **free of every finding**, so a change set can stage.

    The engine validates the whole staged result, so the baseline gets the memory budget (an
    error without it) and a trace link (a warning without it) the ``project`` fixture lacks.
    """
    root = tmp_path / "protoproj"
    model_root = root / "model"
    for subdir in MODEL_SUBDIRS:
        (model_root / subdir).mkdir(parents=True, exist_ok=True)
    (root / "dist").mkdir(parents=True, exist_ok=True)
    _seed_model(model_root)
    m = str(model_root)
    writer.set_swc_memory_budget(m, "EngineController", "RAM0", 256)
    writer.create_requirement(m, "REQ-001", "Speed", "The system shall measure the vehicle speed")
    writer.add_trace_link(m, "REQ-001", "EngineController")
    return _write_manifest(root, "protocol-test-project")


def _model_root(manifest: Path) -> str:
    return str(manifest.parent / "model")


def _fingerprint(manifest: Path) -> str:
    return model_fingerprint(_model_root(manifest))


def _audit_entries(manifest: Path) -> list[dict]:
    log = manifest.parent / "dist" / "audit.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _operations(manifest: Path) -> list[str]:
    return [entry["operation"] for entry in _audit_entries(manifest)]


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class DriverLLM(LLMClient):
    """An LLM double whose turn is a function of the tool executor, so a call can use an earlier result."""

    def __init__(
        self,
        driver: Callable[[Callable[[str, dict], str]], list[str]],
        reply: str = "done.",
        structured: list | None = None,
    ) -> None:
        self._driver = driver
        self._reply = reply
        self._structured: deque = deque(structured or [])
        self.results: list[str] = []
        self.structured_calls: list[tuple[list[dict], dict]] = []

    def complete(self, messages: list[dict], tools: list[dict], tool_executor: Callable[[str, dict], str]) -> str:
        self.results.extend(self._driver(tool_executor) or [])
        return self._reply

    def complete_structured(self, messages: list[dict], schema: dict) -> dict:
        self.structured_calls.append((list(messages), schema))
        if not self._structured:
            raise RuntimeError("DriverLLM structured program exhausted")
        item = self._structured.popleft()
        if isinstance(item, Exception):
            raise item
        return item


class InterruptingApprover(ScriptedApprover):
    """Approves everything until the named prompt, where the user presses Ctrl-C."""

    def __init__(self, answers: list[str], raise_on: str) -> None:
        super().__init__(answers)
        self._raise_on = raise_on

    def ask(self, prompt: str) -> bool:
        if self._raise_on in prompt:
            raise KeyboardInterrupt("the user walked away from the prompt")
        return super().ask(prompt)


def _install_client_spy(monkeypatch, *, fail_call_after: int | None = None) -> list:
    """Record every client ``core`` spawns for itself; a subclass, so a real subprocess is spawned."""
    clients: list = []
    base_client = _core.SwcraftMcpClient

    class SpyClient(base_client):  # type: ignore[valid-type, misc]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._call_count = 0
            self.spawn_env = dict(kwargs.get("env") or {})
            clients.append(self)

        def call_tool(self, name: str, args: dict):
            self._call_count += 1
            if fail_call_after is not None and self._call_count > fail_call_after:
                raise McpClientError("simulated MCP transport failure")
            return super().call_tool(name, args)

    monkeypatch.setattr(_core, "SwcraftMcpClient", SpyClient)
    return clients


class _RecordingClient:
    """An inert client that only remembers how it was spawned; it starts no subprocess."""

    instances: list = []

    def __init__(self, project_path, env=None, approver=None) -> None:
        self.project_path = str(project_path)
        self.server_env = dict(env) if env is not None else dict(os.environ)
        self.approver = approver
        self.closed = False
        self.calls: list[tuple[str, dict]] = []
        _RecordingClient.instances.append(self)

    def list_openai_tools(self) -> list:
        return []

    def call_tool(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return json.dumps({"result": "OK"})

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


@pytest.fixture()
def recording_client(monkeypatch):
    """Replace the client ``core`` spawns with :class:`_RecordingClient`."""
    _RecordingClient.instances = []
    monkeypatch.setattr(_core, "SwcraftMcpClient", _RecordingClient)
    return _RecordingClient


def _agent(
    manifest: Path, llm, approver, monkeypatch, *, scoped: bool = True, mcp=None, review: str = _core.REVIEW_ELICIT
) -> SwcraftAgent:
    """An agent whose reviews the scripted *approver* answers (``elicit``, a ``--script`` replay's mode)."""
    prompt_file = manifest.parent / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))
    return SwcraftAgent(
        project_path=str(manifest),
        llm=llm,
        mcp=mcp,
        scoped=scoped,
        approver=approver,
        review=review,
    )


# ---------------------------------------------------------------------------
# The protocol vocabulary these tests drive
# ---------------------------------------------------------------------------


def _entry(operation: str, layer: str, element: str) -> dict:
    """One ``$defs/scopeEntry``, in the shape ``ScopeProposal`` parses."""
    return {"operation": operation, "address": {"layer": layer, "element": element}}


def _proposal(summary: str, entries: list[dict]) -> dict:
    return {"summary": summary, "entries": entries}


_BRAKE_SWC_OPERATION = {
    "operation": "create",
    "address": {"layer": "swc", "element": "BrakeController"},
    "value": {
        "name": "BrakeController",
        "group": "chassis",
        "type": "application",
        "memory": {"RAM0": 128},
        "handle_termination_and_restart": "no_support",
        "satisfies": ["REQ-002@1"],
    },
}

_BRAKE_REQUIREMENT_OPERATION = {
    "operation": "create",
    "address": {"layer": "requirements", "element": "REQ-002"},
    "value": {
        "id": "REQ-002",
        "title": "Braking",
        "description": "The system shall control the brakes",
        "req_type": "req",
        "version": 1,
        "source": "architecture",
    },
}

#: The scope that covers exactly the two operations above: a new SWC and the requirement its
#: ``satisfies`` names, in one transaction so the requirement stays allocated.
_BRAKE_PROPOSAL = _proposal(
    "Add the SWC BrakeController with its requirement link",
    [_entry("create", "swc", "BrakeController"), _entry("create", "requirements", "REQ-002")],
)


def _change_set_driver(operations: list[dict], intent: str = "Add the brake controller"):
    """Drive ``discover`` for the baseline, then submit one ``change_set``."""

    def driver(call: Callable[[str, dict], str]) -> list[str]:
        discovered = call("discover", {})
        baseline = json.loads(discovered)["baseline"]
        return [
            discovered,
            call("change_set", {"baseline": baseline, "intent": intent, "operations": operations}),
        ]

    return driver


def _response(result: str) -> dict:
    return json.loads(result)


# ---------------------------------------------------------------------------
# The scoped turn: the engine owns the transaction
# ---------------------------------------------------------------------------


def test_a_scoped_turn_spawns_its_own_server_per_turn_against_the_real_manifest(
    protocol_project, recording_client, monkeypatch
):
    """The scope travels in the environment of a process spawned *for this turn*.

    A scope approved mid-conversation cannot reach an earlier server, so no long-lived client
    is started. Both variables are set together, and the correlation id matches the host's
    own scope records.
    """
    llm = DriverLLM(lambda call: [], structured=[_BRAKE_PROPOSAL, _BRAKE_PROPOSAL])
    approver = ScriptedApprover(["y", "y"])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    assert agent._mcp is None, "a scoped run has no long-lived client to dispatch to"

    agent.chat("Add a SWC called BrakeController.")
    first_scope_id = agent._scope_id
    agent.chat("Add it again.")

    assert len(recording_client.instances) == 2, "one server per turn, not one per session"
    for client in recording_client.instances:
        assert client.project_path == str(protocol_project), "the REAL manifest, never a shadow"
        assert client.closed is True, "each turn's client is closed before the next turn"
        assert client.server_env["SWCRAFT_REVIEW"] == "elicit"
        assert json.loads(client.server_env["SWCRAFT_APPROVED_SCOPE"]) == _BRAKE_PROPOSAL["entries"]
        assert client.server_env["SWCRAFT_SCOPE_ID"]

    scope_ids = [client.server_env["SWCRAFT_SCOPE_ID"] for client in recording_client.instances]
    assert scope_ids[0] == first_scope_id
    assert scope_ids[0] != scope_ids[1], "the correlation id is per turn"
    proposed = [entry for entry in _audit_entries(protocol_project) if entry["operation"] == "scope_proposed"]
    assert [entry["scope_id"] for entry in proposed] == scope_ids


def test_the_three_environment_names_are_the_ones_the_server_reads():
    """A renamed variable on either side must not silently unscope a run; the two ends hold the names apart."""
    from swcraft_mcp import server as _server

    assert _core.REVIEW_ENV == _server._REVIEW_ENV
    assert _core.APPROVED_SCOPE_ENV == _server._APPROVED_SCOPE_ENV
    assert _core.SCOPE_ID_ENV == _server._SCOPE_ID_ENV
    assert _core.REVIEW_ELICIT == _server.REVIEW_ELICIT
    assert _core.REVIEW_PANEL == _server.REVIEW_PANEL
    assert set(_core.REVIEW_MODES) <= set(_server.REVIEW_MODES)


def test_a_read_only_proposal_approves_nothing_and_is_not_an_unscoped_run(
    protocol_project, recording_client, monkeypatch
):
    """``[]`` and *absent* are different answers, and the host must not collapse them.

    ``[]`` makes gate 1 refuse every operation; absence is an unscoped run. A proposal that
    grants nothing is ``[]`` and is not put to the user.
    """
    llm = DriverLLM(lambda call: [], structured=[_proposal("List the SWCs", [])])
    approver = ScriptedApprover([])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    agent.chat("Which SWCs exist?")

    assert approver.prompts == [], "a proposal that grants nothing has nothing to approve"
    client = recording_client.instances[0]
    assert client.server_env["SWCRAFT_APPROVED_SCOPE"] == "[]"
    assert client.server_env["SWCRAFT_SCOPE_ID"], "an empty scope is still a scoped run and still correlates"


def test_a_declined_scope_proposal_becomes_the_empty_scope_and_not_a_read_only_one(protocol_project, monkeypatch):
    """A declined proposal becomes the empty scope, which gate 1 refuses per operation before staging.

    The turn stays alive and the server is still spawned with a correlation id.
    """
    clients = _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        reply="You declined that.",
        structured=[_BRAKE_PROPOSAL],
    )
    approver = ScriptedApprover(["n"])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    agent.chat("Add a SWC called BrakeController.")

    assert clients[0].spawn_env["SWCRAFT_APPROVED_SCOPE"] == "[]", "a declined proposal approves nothing"
    assert clients[0].spawn_env["SWCRAFT_SCOPE_ID"] == agent._scope_id
    response = _response(llm.results[1])
    assert response["response"] == "error"
    assert response["error"] == "unauthorized-delta", response
    assert approver.answers_given == ["n"], "only the scope was ever asked about"
    assert _fingerprint(protocol_project) == before
    assert "change_set_rejected" in _operations(protocol_project)

    activated = [entry for entry in _audit_entries(protocol_project) if entry["operation"] == "scope_activated"]
    assert [entry["result"] for entry in activated] == ["declined"]
    assert activated[0]["args"]["entries"] == []


def test_an_approved_change_set_commits_and_the_response_says_so(protocol_project, monkeypatch):
    """propose -> approve the scope -> stage -> approve the delta -> committed."""
    _install_client_spy(monkeypatch)
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        reply="Added BrakeController.",
        structured=[_BRAKE_PROPOSAL],
    )
    approver = ScriptedApprover(["y", "y"])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    reply = agent.chat("Add a SWC called BrakeController.")

    response = _response(llm.results[1])
    assert response["status"] == "committed", response
    assert reply == "Added BrakeController."
    created = protocol_project.parent / "model" / "swc" / "chassis" / "BrakeController.yaml"
    assert created.is_file(), "the approved change set must reach the real model"
    assert approver.answers_given == ["y", "y"], "exactly two questions: the scope, then the delta"
    assert "Add the brake controller" in approver.prompts[1], "the delta prompt states the intent"
    assert "BrakeController" in approver.prompts[1], "and the delta itself"

    # The engine's lifecycle records land in the REAL log under this turn's correlation id.
    entries = _audit_entries(protocol_project)
    operations = [entry["operation"] for entry in entries]
    assert operations[:2] == ["scope_proposed", "scope_activated"]
    for lifecycle in ("change_set_staged", "change_set_approved", "change_set_commit_intent", "change_set_committed"):
        assert lifecycle in operations, f"missing {lifecycle}: {operations}"
    lifecycle_and_host = [
        entry
        for entry in entries
        if entry["operation"].startswith("change_set_") or entry["operation"].startswith("scope_")
    ]
    assert {entry.get("scope_id") for entry in lifecycle_and_host} == {agent._scope_id}


def test_a_declined_delta_leaves_the_model_byte_identical(protocol_project, monkeypatch):
    """The user answered the delta question with ``n``: nothing is applied."""
    _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        reply="I did not apply it.",
        structured=[_BRAKE_PROPOSAL],
    )
    approver = ScriptedApprover(["y", "n"])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    agent.chat("Add a SWC called BrakeController.")

    assert _response(llm.results[1])["status"] == "abandoned"
    assert _fingerprint(protocol_project) == before
    assert not (protocol_project.parent / "model" / "swc" / "chassis" / "BrakeController.yaml").exists()
    assert "change_set_abandoned" in _operations(protocol_project)
    assert "change_set_committed" not in _operations(protocol_project)


def test_an_out_of_scope_change_set_is_refused_by_the_engine_and_never_reaches_a_prompt(protocol_project, monkeypatch):
    """Gate 1 refuses an operation outside the scope with a code, before staging and before any question."""
    _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    narrow = _proposal("Add the SWC BrakeController", [_entry("create", "swc", "BrakeController")])
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        reply="That went beyond what you approved.",
        structured=[narrow],
    )
    approver = ScriptedApprover(["y", "y"])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    agent.chat("Add a SWC called BrakeController.")

    response = _response(llm.results[1])
    assert response["response"] == "error"
    assert response["error"] == "unauthorized-delta", response
    assert "REQ-002" in response["message"]
    assert approver.answers_given == ["y"], "only the scope was ever asked about"
    assert _fingerprint(protocol_project) == before
    assert "change_set_rejected" in _operations(protocol_project)


def test_a_failed_scope_proposal_spawns_no_server_at_all(protocol_project, recording_client, monkeypatch):
    """A transport failure in ``propose_scope`` spawns no server, issues no call and leaves the history balanced."""
    from swcraft_agent.scope_proposal import ScopeProposalError

    before = _fingerprint(protocol_project)
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION]),
        structured=[ConnectionError("the endpoint is unreachable")],
    )
    approver = ScriptedApprover(["y", "y"])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    with pytest.raises(ScopeProposalError):
        agent.chat("Add a SWC called BrakeController.")

    assert recording_client.instances == [], "no server may be spawned on a proposal that failed"
    assert approver.prompts == [], "nothing may be put to the user"
    assert _fingerprint(protocol_project) == before
    assert [m["role"] for m in agent._conversation.messages] == ["system"]


def test_the_turns_client_is_closed_even_when_the_turn_raises(protocol_project, monkeypatch):
    """No orphaned subprocess, whatever the turn did on its way out."""
    clients = _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)

    def driver(call):
        call("discover", {})
        raise RuntimeError("the tool loop gave up")

    llm = DriverLLM(driver, structured=[_BRAKE_PROPOSAL])
    agent = _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch)

    with pytest.raises(RuntimeError):
        agent.chat("Add a SWC called BrakeController.")

    assert clients and all(client.is_alive is False for client in clients), "no orphaned MCP subprocess"
    assert _fingerprint(protocol_project) == before


def test_an_interrupt_at_the_review_prompt_leaves_nothing_behind(protocol_project, monkeypatch):
    """A ``KeyboardInterrupt`` at the delta prompt is the realistic shape of this."""
    clients = _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        structured=[_BRAKE_PROPOSAL],
    )
    approver = InterruptingApprover(["y"], raise_on="Commit exactly this delta")
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        agent.chat("Add a SWC called BrakeController.")

    assert clients and all(client.is_alive is False for client in clients), "no orphaned MCP subprocess"
    assert _fingerprint(protocol_project) == before


def test_a_turn_server_that_will_not_start_ends_the_turn_and_not_the_process(protocol_project, monkeypatch):
    """A per-turn server that times out is a failed turn, not a dead process.

    The REPL loop and ``run_script`` catch only ``RuntimeError``, so a bare ``TimeoutError``
    would end the process.
    """
    base_client = _core.SwcraftMcpClient

    class NeverStarts(base_client):  # type: ignore[valid-type, misc]
        def __init__(self, *args, **kwargs) -> None:
            raise TimeoutError("did not become ready within 10.0s")

    monkeypatch.setattr(_core, "SwcraftMcpClient", NeverStarts)
    before = _fingerprint(protocol_project)
    llm = DriverLLM(lambda call: [], structured=[_BRAKE_PROPOSAL])
    agent = _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch)

    with pytest.raises(McpClientError) as exc:
        agent.chat("Add a SWC called BrakeController.")

    assert "did not become ready" in str(exc.value)
    assert _fingerprint(protocol_project) == before


# ---------------------------------------------------------------------------
# The unscoped turn: no scope, but the same review boundary
# ---------------------------------------------------------------------------


def _unscoped_agent(manifest: Path, llm, approver, monkeypatch) -> tuple[SwcraftAgent, object]:
    """An unscoped agent over a real long-lived client, spawned as the CLI does."""
    from swcraft_agent.mcp_client import SwcraftMcpClient

    client = SwcraftMcpClient(str(manifest), env=_core.review_server_env(review=_core.REVIEW_ELICIT), approver=approver)
    return _agent(manifest, llm, approver, monkeypatch, scoped=False, mcp=client), client


def test_an_unscoped_change_set_still_passes_the_review_boundary(protocol_project, monkeypatch):
    """The review boundary is the server's mode, not the scope: an unscoped delta is reviewed too."""
    before = _fingerprint(protocol_project)
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        structured=[],
    )
    approver = ScriptedApprover(["n"])
    agent, client = _unscoped_agent(protocol_project, llm, approver, monkeypatch)
    try:
        agent.chat("Add a SWC called BrakeController.")
    finally:
        client.close()

    assert llm.structured_calls == [], "no scope is proposed without --scoped"
    assert len(approver.prompts) == 1, "the delta question, and nothing about a scope"
    assert "BrakeController" in approver.prompts[0]
    assert _response(llm.results[1])["status"] == "abandoned"
    assert _fingerprint(protocol_project) == before


def test_an_unscoped_approved_change_set_commits_through_the_same_long_lived_client(protocol_project, monkeypatch):
    """The client really is long-lived: two turns, one server, both reviewed."""
    llm_one = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        structured=[],
    )
    approver = ScriptedApprover(["y"])
    agent, client = _unscoped_agent(protocol_project, llm_one, approver, monkeypatch)
    try:
        agent.chat("Add a SWC called BrakeController.")
        assert _response(llm_one.results[1])["status"] == "committed"
        assert client.is_alive, "the unscoped client survives its turn"

        second = DriverLLM(_change_set_driver([]), structured=[])
        agent._llm = second
        agent.chat("And read it back.")
        assert client.is_alive
    finally:
        client.close()

    created = protocol_project.parent / "model" / "swc" / "chassis" / "BrakeController.yaml"
    assert created.is_file()
    # Present and empty, not missing, so "unscoped" survives the child's own `load_env_file()`.
    assert client.server_env[_core.APPROVED_SCOPE_ENV] == ""
    assert client.server_env[_core.SCOPE_ID_ENV] == ""
    assert client.server_env[_core.REVIEW_ENV] == "elicit"


def test_an_unscoped_turn_proposes_no_scope_and_spawns_nothing_of_its_own(
    protocol_project, recording_client, monkeypatch
):
    llm = FakeLLMClient([[("build", {})], "Built."], structured=[])
    approver = ScriptedApprover([])
    agent = _agent(protocol_project, llm, approver, monkeypatch, scoped=False, mcp=FakeMcp())

    reply = agent.chat("Build the project.")

    assert reply == "Built."
    assert llm.structured_calls == [], "no scope is proposed without --scoped"
    assert recording_client.instances == [], "an unscoped turn spawns no per-turn server"
    assert approver.prompts == []


# ---------------------------------------------------------------------------
# `pinned_escape_env` on every turn, scoped or not
# ---------------------------------------------------------------------------


def test_the_agents_own_unscoped_client_is_spawned_pinned_and_in_panel_mode(
    protocol_project, recording_client, monkeypatch
):
    """The long-lived client an unscoped agent spawns for itself is pinned and in ``panel`` mode.

    The escape-command gate answers from the environment the child was spawned with, so
    the project's ``.env`` must not be able to add an override. ``panel`` is the default review mode.
    """
    for name in ESCAPE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    prompt_file = protocol_project.parent / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    SwcraftAgent(
        project_path=str(protocol_project),
        llm=FakeLLMClient(["ok"]),
        scoped=False,
        approver=ScriptedApprover([]),
    )

    env = recording_client.instances[0].server_env
    for name in ESCAPE_ENV_VARS:
        assert name in env, f"{name} must be PRESENT, or the child's own .env can add it"
        assert env[name] == ""
    assert env[_core.REVIEW_ENV] == "panel"


def test_a_scoped_turns_own_client_carries_the_agents_review_mode(protocol_project, recording_client, monkeypatch):
    """A scoped turn's per-turn server carries the agent's review mode: ``panel`` by default, or ``elicit``."""
    llm = DriverLLM(lambda call: [], structured=[_BRAKE_PROPOSAL, _BRAKE_PROPOSAL])
    _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch, review=_core.REVIEW_PANEL).chat("Add it.")
    _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch).chat("Add it.")

    modes = [client.server_env[_core.REVIEW_ENV] for client in recording_client.instances]
    assert modes == ["panel", "elicit"]


@pytest.mark.parametrize("review", ["stage-only", "", "Panel"])
def test_a_mode_outside_the_two_reviewing_ones_is_refused_before_any_spawn(
    protocol_project, recording_client, monkeypatch, review
):
    """The host refuses a review mode other than its own before a subprocess exists.

    ``stage-only`` would leave every change set staged and never applied, and any
    other value is one the server refuses to start on.
    """
    with pytest.raises(ValueError, match="SWCRAFT_REVIEW"):
        _agent(protocol_project, FakeLLMClient(["ok"]), ScriptedApprover([]), monkeypatch, scoped=False, review=review)
    with pytest.raises(ValueError, match="SWCRAFT_REVIEW"):
        _core.review_server_env({}, review=review)
    assert recording_client.instances == []


def test_a_scoped_turns_own_client_is_spawned_pinned_too(protocol_project, recording_client, monkeypatch):
    for name in ESCAPE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    llm = DriverLLM(lambda call: [], structured=[_BRAKE_PROPOSAL])
    agent = _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch)

    agent.chat("Add a SWC called BrakeController.")

    env = recording_client.instances[0].server_env
    for name in ESCAPE_ENV_VARS:
        assert name in env and env[name] == ""


@pytest.mark.parametrize("scoped", [True, False])
def test_a_real_override_stays_visible_to_the_gate(protocol_project, recording_client, monkeypatch, scoped):
    """Pinning is not neutralizing: a configured override is passed on, so the gate reads the server's value."""
    monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", "moon run :build")
    llm = DriverLLM(lambda call: [], structured=[_BRAKE_PROPOSAL] if scoped else [])
    agent = _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch, scoped=scoped)
    if scoped:
        agent.chat("Add a SWC called BrakeController.")

    assert recording_client.instances[0].server_env["SWCRAFT_MCP_BUILD_COMMAND"] == "moon run :build"


def test_a_stale_scope_in_this_process_environment_never_reaches_an_unscoped_server(
    protocol_project, recording_client, monkeypatch
):
    """Both scope variables are *overwritten*, so a leftover scope never bounds an unscoped server."""
    monkeypatch.setenv(_core.APPROVED_SCOPE_ENV, '[{"operation": "create", "address": {"layer": "swc"}}]')
    monkeypatch.setenv(_core.SCOPE_ID_ENV, "left-over")
    prompt_file = protocol_project.parent / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    SwcraftAgent(
        project_path=str(protocol_project),
        llm=FakeLLMClient(["ok"]),
        scoped=False,
        approver=ScriptedApprover([]),
    )

    env = recording_client.instances[0].server_env
    assert env[_core.APPROVED_SCOPE_ENV] == ""
    assert env[_core.SCOPE_ID_ENV] == ""
    from swcraft_mcp import server as _server

    monkeypatch.setenv(_server._APPROVED_SCOPE_ENV, env[_core.APPROVED_SCOPE_ENV])
    monkeypatch.setenv(_server._SCOPE_ID_ENV, env[_core.SCOPE_ID_ENV])
    assert _server._resolve_scope_channel() == (None, None)


def test_an_unscoped_spawn_environment_survives_a_project_env_file(tmp_path, monkeypatch):
    """The scope keys are pinned present-and-empty, so a project ``.env`` cannot fill them.

    The child's ``load_dotenv(override=False)`` sets only **absent** keys; a popped pair let a
    ``.env`` scope an unscoped server, or with one key stop it starting. Applied end to end here.
    """
    from swcraft_mcp import server as _server

    env = _core.review_server_env({})
    (tmp_path / ".env").write_text(
        f"{_core.APPROVED_SCOPE_ENV}=[]\n{_core.SCOPE_ID_ENV}=from-a-project-env-file\n",
        encoding="utf-8",
    )
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)

    from swcraft_tools.env import load_env_file

    assert load_env_file() == tmp_path / ".env", "the probe must really load the file"

    assert _server._resolve_scope_channel() == (None, None)


def test_a_scoped_environment_without_a_correlation_id_is_refused_here():
    """A scope without a correlation id is refused where it is composed, not as a server startup failure."""
    with pytest.raises(ValueError, match=_core.SCOPE_ID_ENV):
        _core.scoped_server_env([], "")


def test_the_approver_a_replay_swaps_in_reaches_the_long_lived_client(protocol_project, recording_client, monkeypatch):
    """The client reads the approver ``run_script`` swaps in per turn at answer time."""
    prompt_file = protocol_project.parent / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    first = ScriptedApprover(["y"])
    agent = SwcraftAgent(
        project_path=str(protocol_project),
        llm=FakeLLMClient(["ok"]),
        scoped=False,
        approver=first,
    )
    client = recording_client.instances[0]
    assert client.approver is first

    second = ScriptedApprover(["n"])
    agent.approver = second
    assert client.approver is second


# ---------------------------------------------------------------------------
# The two escapes the engine's scope cannot see
# ---------------------------------------------------------------------------
#
# An export with a caller-supplied path and a `build`/`validate` that shells out through
# `SWCRAFT_MCP_*_COMMAND` write outside the model, where no scope address reaches.


@pytest.mark.parametrize(
    ("tool", "path_arg"),
    [("export_component", "output_path"), ("export_component", "output_dir")],
    ids=["one-component-output_path", "every-component-output_dir"],
)
def test_a_caller_directed_export_is_refused_on_a_scoped_turn(protocol_project, monkeypatch, tool, path_arg):
    """While a scope is in force an export writes only to the project's default.

    An explicit path can overwrite any file, a model file included, and a scope speaks only of model addresses.
    """
    _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    escape = (
        str(protocol_project.parent / "model" / "swc")
        if path_arg == "output_dir"
        else str(protocol_project.parent / "model" / "swc" / "powertrain" / "EngineController.yaml")
    )
    args = {path_arg: escape}
    # `swc_name` beside `output_dir` is a mixed call the server refuses itself.
    if path_arg == "output_path":
        args["swc_name"] = "EngineController"

    llm = DriverLLM(lambda call: [call(tool, args)], structured=[_BRAKE_PROPOSAL])
    agent = _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch)

    agent.chat("Export EngineController over the model.")

    assert llm.results[0].startswith("REFUSED:"), llm.results[0]
    assert path_arg in llm.results[0]
    assert _fingerprint(protocol_project) == before


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("export_component", {"swc_name": "EngineController"}),
        ("export_component", {}),
    ],
    ids=["one-component", "every-component"],
)
def test_the_argument_free_export_is_allowed_on_a_scoped_turn(protocol_project, monkeypatch, tool, args):
    """Only the caller-supplied path is refused; the project's own default is fine."""
    _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    llm = DriverLLM(lambda call: [call(tool, args)], structured=[_BRAKE_PROPOSAL])
    agent = _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch)

    agent.chat("Export the components.")

    assert not llm.results[0].startswith("REFUSED:"), llm.results[0]
    # A server-side failure is a dict too, so the reply must say it succeeded.
    reply = json.loads(llm.results[0])
    assert reply["success"] is True, reply
    assert "OUTPUT dist/components/EngineController.arxml" in reply["output"]
    if "swc_name" not in args:
        assert "Summary: dist/components/export-summary.json" in reply["output"]
    assert _fingerprint(protocol_project) == before, "an export never changes the model"


@pytest.mark.parametrize("tool", ["build", "validate"])
def test_a_configured_override_is_refused_on_a_scoped_turn(protocol_project, monkeypatch, tool):
    """The override runs a shell command against the real project, so a scoped turn refuses it."""
    import shlex
    import sys

    marker = protocol_project.parent / "PWNED"
    command = shlex.join([sys.executable, "-c", f"open({str(marker)!r}, 'w').write('pwned')"])
    monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", command)
    monkeypatch.setenv("SWCRAFT_MCP_VALIDATE_COMMAND", command)

    _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    llm = DriverLLM(lambda call: [call(tool, {})], structured=[_BRAKE_PROPOSAL])
    agent = _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch)

    agent.chat("Build the project.")

    assert not marker.exists(), f"the {tool} override ran against the real project on a scoped turn"
    assert llm.results[0].startswith("REFUSED:"), llm.results[0]
    assert f"SWCRAFT_MCP_{tool.upper()}_COMMAND" in llm.results[0]
    assert _fingerprint(protocol_project) == before


@pytest.mark.parametrize("tool", ["build", "validate"])
@pytest.mark.parametrize("scoped", [True, False])
def test_a_dot_env_cannot_re_inject_the_override_into_a_client_this_host_spawned(
    protocol_project, monkeypatch, tool, scoped, tmp_path
):
    """A project ``.env`` cannot re-inject an override, on scoped and unscoped turns alike.

    ``load_dotenv(override=False)`` sets only absent keys; ``pinned_escape_env()`` passes them
    present-and-empty. Driven with a clean ``os.environ`` through the agent's own client spawn.
    """
    import shlex
    import sys

    for name in ESCAPE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    marker = tmp_path / f"PWNED_FROM_DOTENV_{tool}_{scoped}"
    command = shlex.join([sys.executable, "-c", f"open({str(marker)!r}, 'w').write('pwned')"])
    (protocol_project.parent / ".env").write_text(
        f"SWCRAFT_MCP_BUILD_COMMAND={command}\nSWCRAFT_MCP_VALIDATE_COMMAND={command}\n",
        encoding="utf-8",
    )
    # The child inherits the cwd and load_env_file() walks up from it, as when run from a project.
    monkeypatch.chdir(protocol_project.parent)

    clients = _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    llm = DriverLLM(lambda call: [call(tool, {})], structured=[_BRAKE_PROPOSAL] if scoped else [])
    agent = _agent(protocol_project, llm, ScriptedApprover(["y"]), monkeypatch, scoped=scoped)
    try:
        agent.chat("Build the project.")
    finally:
        for client in clients:
            client.close()

    assert not marker.exists(), f"a .env re-injected the {tool} override into the server this host spawned"
    assert _fingerprint(protocol_project) == before


@pytest.mark.parametrize(
    ("tool", "path_arg"),
    [("export_component", "output_path"), ("export_component", "output_dir")],
    ids=["one-component-output_path", "every-component-output_dir"],
)
def test_an_unscoped_run_still_accepts_any_export_path(project, monkeypatch, tool, path_arg, tmp_path):
    """With no scope in force both export path arguments accept any path (a non-goal of the gate)."""
    from swcraft_agent.mcp_client import SwcraftMcpClient

    destination = tmp_path / "exports"
    destination.mkdir()
    args: dict = {path_arg: str(destination if path_arg == "output_dir" else destination / "Engine.arxml")}
    # `swc_name` beside `output_dir` is a mixed call the server refuses.
    if path_arg == "output_path":
        args["swc_name"] = "EngineController"

    client = SwcraftMcpClient(str(project))
    try:
        llm = FakeLLMClient([[(tool, args)], "Exported."])
        agent = _agent(project, llm, ScriptedApprover([]), monkeypatch, scoped=False, mcp=client)
        agent.chat("Export EngineController to my own directory.")
    finally:
        client.close()

    assert not llm.results[0].startswith("REFUSED:"), llm.results[0]
    assert any(destination.iterdir()), "the export must really have written there"


@pytest.mark.parametrize("tool", ["build", "validate"])
def test_an_unscoped_run_still_runs_the_configured_override_command(project, monkeypatch, tool):
    """With no scope in force the override is the project's own configuration and still runs."""
    import shlex
    import sys

    marker = project.parent / "RAN"
    command = shlex.join([sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"])
    monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", command)
    monkeypatch.setenv("SWCRAFT_MCP_VALIDATE_COMMAND", command)

    from swcraft_agent.mcp_client import SwcraftMcpClient

    client = SwcraftMcpClient(str(project))
    try:
        llm = FakeLLMClient([[(tool, {})], "Built."])
        agent = _agent(project, llm, ScriptedApprover([]), monkeypatch, scoped=False, mcp=client)
        agent.chat("Build the project.")
    finally:
        client.close()

    assert not llm.results[0].startswith("REFUSED:"), llm.results[0]
    assert marker.exists(), "the unscoped run must still honour the project's own build command"


# ---------------------------------------------------------------------------
# An injected client cannot be pinned after spawn: a scoped agent refuses an unpinned one
# ---------------------------------------------------------------------------


class _EnvMcp(FakeMcp):
    """A test double that *does* model a server environment, like the real client."""

    def __init__(self, server_env: dict[str, str]) -> None:
        self._env = dict(server_env)

    @property
    def server_env(self):
        from types import MappingProxyType

        return MappingProxyType(self._env)


@pytest.mark.parametrize(
    "missing",
    [
        ("SWCRAFT_MCP_BUILD_COMMAND",),
        ("SWCRAFT_MCP_VALIDATE_COMMAND",),
        ESCAPE_ENV_VARS,
    ],
    ids=["build_absent", "validate_absent", "both_absent"],
)
def test_a_scoped_agent_refuses_an_injected_client_that_is_not_pinned(project, monkeypatch, missing):
    """``scoped=True`` over a client whose escape variables are not PRESENT is refused at construction.

    The child fills ABSENT keys from the project's ``.env``, so the gate would report "no
    override" for a server that has one. The message names the remedy.
    """
    env = dict(pinned_escape_env())
    for name in missing:
        env.pop(name, None)

    prompt_file = project.parent / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    with pytest.raises(_core.UnpinnedEscapeEnvError) as excinfo:
        SwcraftAgent(
            project_path=str(project),
            llm=FakeLLMClient(["never reached"]),
            mcp=_EnvMcp(env),
            scoped=True,
            approver=ScriptedApprover([]),
        )

    message = str(excinfo.value)
    for name in missing:
        assert name in message, message
    assert "pinned_escape_env" in message, message


def test_an_injected_client_pinned_with_pinned_escape_env_is_accepted(project, monkeypatch):
    """A client spawned with ``env=pinned_escape_env()`` is accepted, and the guard trusts that environment."""
    env = pinned_escape_env()
    client = _EnvMcp(env)

    prompt_file = project.parent / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    agent = SwcraftAgent(
        project_path=str(project),
        llm=FakeLLMClient(["ok"]),
        mcp=client,
        scoped=True,
        approver=ScriptedApprover([]),
    )

    assert agent._long_lived_server_env() == client.server_env


def test_a_scoped_turn_never_dispatches_to_an_injected_client(project, monkeypatch, recording_client):
    """An injected client is accepted, pinned, and then not used by a scoped turn.

    Dispatching to it would run the turn against a server the approved scope never reached.
    """

    class _Recording(_EnvMcp):
        def __init__(self, env):
            super().__init__(env)
            self.calls: list[str] = []

        def call_tool(self, name, args):
            self.calls.append(name)
            return ""

    injected = _Recording(pinned_escape_env())
    agent = _agent(project, FakeLLMClient(["done"]), ScriptedApprover([]), monkeypatch, mcp=injected)

    assert agent._mcp is injected
    agent._scope_id = "turn-1"
    agent._scoped_turn("Rename the SWC.", [])

    assert injected.calls == [], "the injected client must never see a scoped turn's tool calls"
    assert len(recording_client.instances) == 1, "exactly one per-turn client was spawned"
    assert recording_client.instances[0].closed is True


def test_a_double_that_models_no_server_environment_stays_injectable(project, monkeypatch):
    """A double that exposes no ``server_env`` declares no subprocess environment and stays injectable."""
    prompt_file = project.parent / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    agent = SwcraftAgent(
        project_path=str(project),
        llm=FakeLLMClient(["ok"]),
        mcp=FakeMcp(),
        scoped=True,
        approver=ScriptedApprover([]),
    )

    assert agent._long_lived_server_env() is None


def test_an_unscoped_agent_accepts_an_unpinned_injected_client(project, monkeypatch):
    """With no scope in force an unscoped host may inject any client."""
    prompt_file = project.parent / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    agent = SwcraftAgent(
        project_path=str(project),
        llm=FakeLLMClient(["ok"]),
        mcp=_EnvMcp({"PATH": os.environ.get("PATH", "")}),
        scoped=False,
        approver=ScriptedApprover([]),
    )

    assert agent._long_lived_server_env() == {"PATH": os.environ.get("PATH", "")}


# ---------------------------------------------------------------------------
# The whole chain in one run
# ---------------------------------------------------------------------------
#
# The identities that hold the chain together are written by independent parties, so no
# single-seam test can compare them:
#
#   the LLM proposes            -> `scope_proposal.ScopeProposal`
#   the human approves          -> `approval.Approver.confirm_scope`
#   the host spawns this turn's server with SWCRAFT_APPROVED_SCOPE / _SCOPE_ID
#   the engine stages, reviews through the elicitation callback, and commits


class _CapturingApprover(ScriptedApprover):
    """A scripted approver that keeps the ``ScopeProposal`` object it was shown, not a payload copy."""

    def __init__(self, answers: list[str]) -> None:
        super().__init__(answers)
        self.proposals: list = []

    def confirm_scope(self, proposal) -> bool:  # type: ignore[no-untyped-def]
        self.proposals.append(proposal)
        return super().confirm_scope(proposal)


def test_the_migrated_chain_carries_one_scope_and_one_correlation_id_end_to_end(protocol_project, monkeypatch):
    """propose -> approve -> spawn -> stage -> review -> commit, as one run, with three identities.

    1. **The entries are the export**: :meth:`ScopeProposal.to_json` is exactly the
       ``SWCRAFT_APPROVED_SCOPE`` document, so the server enforces the scope the user answered.
    2. **The id is the id**: ``SWCRAFT_SCOPE_ID`` is the turn's correlation id, on the host's
       two scope records and every engine lifecycle record; only the environment connects them.
    3. **The prompt is the grant**: the scope question names every entry that bounded the server.
    """
    clients = _install_client_spy(monkeypatch)
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        reply="Added BrakeController.",
        structured=[_BRAKE_PROPOSAL],
    )
    approver = _CapturingApprover(["y", "y"])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    agent.chat("Add a SWC called BrakeController.")

    # -- the change set really landed ----------------------------------
    assert _response(llm.results[1])["status"] == "committed", llm.results[1]
    assert (protocol_project.parent / "model" / "swc" / "chassis" / "BrakeController.yaml").is_file()

    # -- 1. the entries are the export ---------------------------------
    (proposal,) = approver.proposals
    (client,) = clients
    exported = json.loads(client.spawn_env[_core.APPROVED_SCOPE_ENV])
    assert exported == proposal.to_json(), "the server was bounded by something other than what was approved"
    assert exported == _BRAKE_PROPOSAL["entries"], exported
    assert client.spawn_env[_core.REVIEW_ENV] == _core.REVIEW_ELICIT

    # -- 2. the id is the id, across all three writers -----------------
    scope_id = agent._scope_id
    assert scope_id, "a scoped turn always has a correlation id"
    assert client.spawn_env[_core.SCOPE_ID_ENV] == scope_id

    entries = _audit_entries(protocol_project)
    host_records = [entry for entry in entries if entry["operation"].startswith("scope_")]
    assert [entry["operation"] for entry in host_records] == ["scope_proposed", "scope_activated"]
    assert {entry.get("scope_id") for entry in host_records} == {scope_id}, host_records
    assert host_records[0]["args"]["entries"] == exported
    assert host_records[1]["result"] == "approved"

    lifecycle = [entry for entry in entries if entry["operation"] in LIFECYCLE_OPERATIONS]
    assert lifecycle, "the engine's lifecycle records must reach the REAL project's log"
    assert {entry.get("scope_id") for entry in lifecycle} == {scope_id}, [
        (entry["operation"], entry.get("scope_id")) for entry in lifecycle
    ]
    assert {entry["operation"] for entry in lifecycle} == {
        "change_set_staged",
        "change_set_approved",
        "change_set_commit_intent",
        "change_set_committed",
    }
    assert len({entry["args"]["change_set_id"] for entry in lifecycle}) == 1

    # -- 3. the prompt is the grant ------------------------------------
    scope_prompt = approver.prompts[0]
    for entry in proposal.entries:
        assert entry.operation in scope_prompt, scope_prompt
        assert entry.address.element in scope_prompt, scope_prompt
    assert proposal.summary in scope_prompt
    # The second question is the server's rendering of the staged delta.
    assert "BrakeController" in approver.prompts[1]
    assert approver.answers_given == ["y", "y"], "exactly two questions: the scope, then the delta"


def test_every_record_this_host_writes_is_one_the_commit_block_excludes():
    """The host's audit vocabulary is exactly `HOST_OPERATIONS`, name for name.

    Host records can land inside a change set's commit block, which `commit_block` excludes
    by name; an unlisted name would break the block's completeness. Asserted over the
    `_record(...)` call sites, not only the constants.
    """
    from swcraft_tools.editing.audit import HOST_OPERATIONS

    declared = {_core._AUDIT_SCOPE_PROPOSED, _core._AUDIT_SCOPE_ACTIVATED, _core._AUDIT_CALL_REFUSED}
    assert declared == set(HOST_OPERATIONS)

    module = ast.parse(Path(_core.__file__).read_text(encoding="utf-8"))
    constants = {
        target.id: node.value.value
        for node in ast.walk(module)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    recorded = set()
    for node in ast.walk(module):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_record"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant):
            recorded.add(first.value)
        else:
            assert isinstance(first, ast.Name), ast.dump(first)
            recorded.add(constants[first.id])
    assert recorded == set(HOST_OPERATIONS), recorded


def test_a_read_only_proposal_cannot_mutate_through_the_real_engine(protocol_project, monkeypatch):
    """Under ``[]`` an emitted ``change_set`` is refused by the engine per operation, before staging and asking."""
    clients = _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        reply="I can only read on this turn.",
        structured=[_proposal("List the SWCs", [])],
    )
    approver = _CapturingApprover([])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    agent.chat("Which SWCs exist? Then add BrakeController anyway.")

    assert clients[0].spawn_env[_core.APPROVED_SCOPE_ENV] == "[]"
    assert clients[0].spawn_env[_core.SCOPE_ID_ENV] == agent._scope_id
    response = _response(llm.results[1])
    assert response["response"] == "error"
    assert response["error"] == "unauthorized-delta", response
    assert approver.prompts == [], "nothing was approved, so nothing was asked — including the delta"
    assert _fingerprint(protocol_project) == before
    assert "change_set_rejected" in _operations(protocol_project)
    assert "change_set_committed" not in _operations(protocol_project)


def test_a_malformed_proposal_spawns_no_server_and_touches_nothing(protocol_project, recording_client, monkeypatch):
    """A proposal naming an undeclared layer fails at the host: no server, no prompt, no turn."""
    from swcraft_agent.scope_proposal import ScopeProposalError

    before = _fingerprint(protocol_project)
    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION]),
        structured=[_proposal("Add a widget", [_entry("create", "widgets", "BrakeController")])],
    )
    approver = _CapturingApprover(["y", "y"])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    with pytest.raises(ScopeProposalError, match="widgets"):
        agent.chat("Add a SWC called BrakeController.")

    assert recording_client.instances == [], "no server may be spawned on a proposal that did not parse"
    assert approver.prompts == [], "nothing may be put to the user"
    assert _fingerprint(protocol_project) == before
    assert _operations(protocol_project) == [], "not even a scope_proposed record: there is no scope"
    assert [message["role"] for message in agent._conversation.messages] == ["system"]


def test_an_operation_the_scope_does_not_cover_never_reaches_the_review_prompt(protocol_project, monkeypatch):
    """Gate 1 matches the operation exactly: ``remove`` under an ``update`` scope is refused before staging."""
    _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)
    update_only = _proposal(
        "Correct the EngineController",
        [_entry("update", "swc", "EngineController")],
    )
    remove_the_swc = {
        "operation": "remove",
        "address": {"layer": "swc", "element": "EngineController"},
    }
    llm = DriverLLM(
        _change_set_driver([remove_the_swc], intent="Delete the engine controller"),
        reply="That is beyond what you approved.",
        structured=[update_only],
    )
    approver = _CapturingApprover(["y", "y"])
    agent = _agent(protocol_project, llm, approver, monkeypatch)

    agent.chat("Rename the engine controller.")

    response = _response(llm.results[1])
    assert response["response"] == "error"
    assert response["error"] == "unauthorized-delta", response
    assert approver.answers_given == ["y"], "only the scope was ever asked about"
    assert _fingerprint(protocol_project) == before
    assert (protocol_project.parent / "model" / "swc" / "powertrain" / "EngineController.yaml").is_file()
    assert "change_set_rejected" in _operations(protocol_project)


def test_a_review_channel_that_fails_writes_no_model_bytes(protocol_project, monkeypatch):
    """An approval channel that raises abandons the change set and records a decline, never an approval."""
    _install_client_spy(monkeypatch)
    before = _fingerprint(protocol_project)

    class _BrokenChannel(ScriptedApprover):
        def ask(self, prompt: str) -> bool:
            if "Commit exactly this delta" in prompt:
                raise OSError("the terminal went away")
            return super().ask(prompt)

    llm = DriverLLM(
        _change_set_driver([_BRAKE_SWC_OPERATION, _BRAKE_REQUIREMENT_OPERATION]),
        reply="I could not ask you.",
        structured=[_BRAKE_PROPOSAL],
    )
    agent = _agent(protocol_project, llm, _BrokenChannel(["y"]), monkeypatch)

    agent.chat("Add a SWC called BrakeController.")

    assert _response(llm.results[1])["status"] == "abandoned", llm.results[1]
    assert _fingerprint(protocol_project) == before
    assert not (protocol_project.parent / "model" / "swc" / "chassis" / "BrakeController.yaml").exists()
    entries = _audit_entries(protocol_project)
    operations = [entry["operation"] for entry in entries]
    assert "change_set_abandoned" in operations
    assert "change_set_committed" not in operations
    # `change_set_approved` records what the boundary said, so it must say "declined".
    (answered,) = [entry for entry in entries if entry["operation"] == "change_set_approved"]
    assert answered["result"] == "declined", answered


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param("{project}/dist/out", "dist/out", id="inside-the-project"),
        pytest.param("{home}/Desktop/out", OUTSIDE_THE_PROJECT, id="the-users-home"),
        pytest.param("relative/out", "relative/out", id="a-relative-argument"),
        pytest.param("SWCRAFT_BUILD_COMMAND", "SWCRAFT_BUILD_COMMAND", id="a-variable-name"),
        pytest.param("../../../Users/someone/out", OUTSIDE_THE_PROJECT, id="a-climbing-relative-path"),
        pytest.param("~/out", OUTSIDE_THE_PROJECT, id="a-home-relative-path"),
        pytest.param("C:\\Users\\someone\\out", OUTSIDE_THE_PROJECT, id="a-windows-spelling"),
        pytest.param("\\Users\\someone\\out", OUTSIDE_THE_PROJECT, id="a-windows-root-relative-path"),
        pytest.param("C:..\\Users\\someone\\out", OUTSIDE_THE_PROJECT, id="a-windows-drive-relative-path"),
        pytest.param("out\\..\\..\\Users\\someone", OUTSIDE_THE_PROJECT, id="a-backslash-climb"),
    ],
)
def test_a_refused_calls_target_is_recorded_relative_to_the_project(project, monkeypatch, target, expected):
    """``call_refused`` records the target relative to the project, never the user's home.

    An absolute target becomes project-relative, a climbing or home-relative one ``(outside the
    project)``, and any other relative one stays the LLM's literal.
    """
    agent = _agent(project, object(), _CapturingApprover([]), monkeypatch, scoped=False, mcp=object())
    value = target.format(project=project.parent, home=Path.home())

    agent._record(_core._AUDIT_CALL_REFUSED, {"tool": "export_component", "code": "c", "target": value}, "REFUSED")

    (entry,) = _audit_entries(project)
    assert entry["args"]["target"] == expected
    line = (project.parent / "dist" / "audit.jsonl").read_text(encoding="utf-8")
    assert str(Path.home()) not in line
    assert str(project.parent) not in line


def test_a_scope_summary_that_looks_like_a_path_is_recorded_as_written(project, monkeypatch):
    """Only ``target`` is a location: free text is evidence and is never rewritten."""
    agent = _agent(project, object(), _CapturingApprover([]), monkeypatch, scoped=False, mcp=object())

    agent._record(_core._AUDIT_SCOPE_PROPOSED, {"summary": "/swc rename Foo", "entries": []}, "mutation")

    (entry,) = _audit_entries(project)
    assert entry["args"]["summary"] == "/swc rename Foo"


def test_an_unscoped_turn_without_the_long_lived_client_is_refused(protocol_project, monkeypatch):
    """A scoped agent starts no long-lived client, so the unscoped path refuses rather than dispatching."""
    agent = _agent(protocol_project, DriverLLM(lambda call: []), ScriptedApprover([]), monkeypatch)
    with pytest.raises(RuntimeError, match="long-lived client"):
        agent._plain_turn("Add a SWC called BrakeController.")
