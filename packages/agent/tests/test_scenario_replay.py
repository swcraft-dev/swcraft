"""Deterministic scenario replay tests.

Each test loads a real ``fixtures/scenarios/0N-*.yaml`` script, drives a ``SwcraftAgent``
with a ``FakeLLMClient`` scripted to issue the walkthrough's tool calls, and asserts the
resulting model state (no real Azure OpenAI calls). Traces use `discover` / `read` /
`change_set` plus `build`; a seeded baseline is written with ``swcraft_tools.writer``.

**Every mutating turn is reviewed**: the server runs in ``SWCRAFT_REVIEW=elicit``, so a
change set is applied only after the turn's ``ScriptedApprover`` approved the exact staged
delta, and a weakening asks a second question. A client with no approver declares no
elicitation capability and the server abandons every change set. The answer counts are
pinned by ``test_scenario_files.py``.

- Scenarios 1–4 use the ``tmp_path`` scaffold.
- Scenario 5 is ``slow`` and uses a git worktree checkout; its build runs the direct
  ``swcraft_tools.cli validate`` command (``build_override_command``), so nothing is
  installed inside the worktree.
- Scenario 6 is the scoped run: a server per turn, a scope question and a safety question.
"""

from __future__ import annotations

import json
import ntpath
import posixpath
import re
import sys
from pathlib import Path

import pytest
import yaml

from tests.conftest import (
    MODEL_SUBDIRS,
    REQUIRED_MODEL_SUBDIRS,
    FakeLLMClient,
    declare_no_support,
    seed_minimal_hardware,
    seed_root_composition,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCENARIOS_DIR = Path(__file__).resolve().parent / "fixtures" / "scenarios"


def build_override_command(manifest: Path, interpreter: str = sys.executable) -> str:
    """Return scenario 5's ``SWCRAFT_MCP_BUILD_COMMAND`` override.

    The *direct* ``swcraft_tools.cli validate`` command, never a repo build wrapper: the
    worktree has no environment of its own. ``interpreter`` defaults to the suite's own,
    which is not on ``PATH`` under ``dev.py test-slow``. ``as_posix()`` keeps ``shlex.split()``
    from treating Windows backslashes as escapes.

    Raises:
        ValueError: if ``interpreter`` is not an absolute path.
    """
    executable = Path(interpreter)
    if not executable.is_absolute():
        raise ValueError(
            f"the build override needs an absolute interpreter path, got {interpreter!r}: "
            f"the worktree checkout has no environment of its own, and a bare name resolves "
            f"against PATH — which does not carry the checkout's .venv"
        )
    return f"{executable.as_posix()} -m swcraft_tools.cli validate --project {Path(manifest).as_posix()}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _ProtocolFake(FakeLLMClient):
    """``FakeLLMClient`` whose change sets learn their baseline from ``discover``.

    A ``change_set`` whose ``baseline`` is the sentinel below gets the last ``discover``'s token.
    """

    BASELINE = "<from discover>"

    def complete(self, messages, tools, tool_executor):
        baseline = {"value": ""}

        def execute(name, args):
            if name == "change_set" and args.get("baseline") == self.BASELINE:
                args = {**args, "baseline": baseline["value"]}
            result = tool_executor(name, args)
            if name == "discover":
                baseline["value"] = json.loads(result)["baseline"]
            return result

        return super().complete(messages, tools, execute)


def change_set(intent: str, operations: list[dict]) -> tuple[str, dict]:
    """One ``change_set`` program round, against whatever ``discover`` last answered."""
    return ("change_set", {"baseline": _ProtocolFake.BASELINE, "intent": intent, "operations": operations})


def _review_client(manifest: Path):
    """A client that can answer the server's review elicitations.

    The approver is a placeholder (``run_script`` installs each turn's own); passing one
    makes the client declare the elicitation capability.
    """
    from swcraft_agent.approval import ScriptedApprover
    from swcraft_agent.core import REVIEW_ELICIT, review_server_env
    from swcraft_agent.mcp_client import SwcraftMcpClient

    return SwcraftMcpClient(str(manifest), env=review_server_env(review=REVIEW_ELICIT), approver=ScriptedApprover())


def _install_recording_approver(monkeypatch, model_root: str | None = None) -> list:
    """Record every per-turn approver ``run_script`` installs, in turn order.

    With a *model_root*, each also captures the model's fingerprint as its turn began, so one
    run can assert that a specific turn changed nothing.
    """
    import swcraft_agent.cli as _cli
    from swcraft_tools.session import model_fingerprint

    created: list = []
    base = _cli.ScriptedApprover

    class _RecordingApprover(base):  # type: ignore[valid-type, misc]
        def __init__(self, answers=None) -> None:
            super().__init__(answers)
            self.fingerprint_at_start = None if model_root is None else model_fingerprint(model_root)
            created.append(self)

    monkeypatch.setattr(_cli, "ScriptedApprover", _RecordingApprover)
    return created


def _replay(scenario_path: str, mcp_client, program: list, tmp_path: Path, monkeypatch):
    """Load scenario, build agent, run via run_script, return transcript."""
    from swcraft_agent.cli import run_script
    from swcraft_agent.core import SwcraftAgent

    prompt_file = tmp_path / "prompt.md"
    if not prompt_file.exists():
        prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    fake = _ProtocolFake(program)
    agent = SwcraftAgent(
        project_path=str(mcp_client._project_path),
        llm=fake,
        mcp=mcp_client,
    )
    turns = yaml.safe_load(Path(scenario_path).read_text(encoding="utf-8"))
    transcript = run_script(agent, scenario_path)
    return transcript, turns, fake


def _read(mcp, address: dict, form: str | None = None) -> object:
    """The ``read`` response's ``value``.

    A *layer* address answers a summary by default, which carries a collection as its count;
    pass ``form="full"`` to check what a collection contains.
    """
    arguments = {"address": address} | ({} if form is None else {"form": form})
    return json.loads(mcp.call_tool("read", arguments))["value"]


# ---------------------------------------------------------------------------
# Scenario 1: Add SWC with port and runnable
# ---------------------------------------------------------------------------


def _seed_scenario1(model_root: str) -> None:
    """The baseline scenario 1 starts from: the interface, and an untraced requirement.

    ``REQ_ENG_001`` is seeded with nothing satisfying it (a ``validate`` warning, not a
    refusal); the SWC's own ``create`` payload carries the ``satisfies`` link the user names.
    The processor is seeded because a model without one fails staged validation.
    """
    from swcraft_tools import writer

    seed_minimal_hardware(model_root)
    seed_root_composition(model_root)
    writer.create_data_type(
        model_root, "float32_base", "base_type", size=32, encoding="IEEE754", native_declaration="float"
    )
    writer.create_data_type(model_root, "float32", "value_type", base="float32_base")
    writer.create_interface(model_root, "ISpeed", [{"name": "speed", "implementation_type": "float32"}])
    writer.create_requirement(
        model_root,
        "REQ_ENG_001",
        "Engine speed control shall be implemented",
        "The engine speed control function shall be provided by EngineController.",
    )


#: Scenario 1's two turns, as its tool trace narrates.
def _scenario1_program() -> list:
    return [
        # Turn 1: the SWC with its provide port and its `satisfies` link, one operation.
        [
            ("discover", {}),
            change_set(
                "Add the application SWC EngineController with its provide port, traced to REQ_ENG_001",
                [
                    {
                        "operation": "create",
                        "address": {"layer": "swc", "element": "EngineController"},
                        "value": {
                            "name": "EngineController",
                            "group": "powertrain",
                            "type": "application",
                            "ports": [{"name": "pEngineSpeed", "direction": "provide", "interface": "ISpeed"}],
                            "handle_termination_and_restart": "no_support",
                            "satisfies": ["REQ_ENG_001@1"],
                        },
                    },
                ],
            ),
        ],
        "Added EngineController with provide port pEngineSpeed on ISpeed, traced to REQ_ENG_001.",
        # Turn 2: the runnable, addressed as a member of the SWC that now exists.
        [
            ("discover", {}),
            change_set(
                "Add the ComputeSpeed runnable with a 10 ms timing event",
                [
                    {
                        "operation": "create",
                        "address": {
                            "layer": "swc",
                            "element": "EngineController",
                            "path": [{"field": "runnables"}, {"member": "ComputeSpeed"}],
                        },
                        "value": {"name": "ComputeSpeed", "events": [{"type": "timing", "period": 0.01}]},
                    }
                ],
            ),
        ],
        "Added runnable ComputeSpeed with a 10ms timing event.",
    ]


def test_scenario1_add_swc_port_runnable(scaffolded_project, tmp_path, monkeypatch):
    """Scenario 1: two change sets, one review question each, both committed."""
    model_root = str(scaffolded_project.parent / "model")
    _seed_scenario1(model_root)
    approvers = _install_recording_approver(monkeypatch)

    scenario_path = str(_SCENARIOS_DIR / "01-add-swc.yaml")

    with _review_client(scaffolded_project) as mcp:
        transcript, turns, fake = _replay(scenario_path, mcp, _scenario1_program(), tmp_path, monkeypatch)

        assert len(transcript) == len(turns) == 2
        assert [name for name, _ in fake.calls] == ["discover", "change_set", "discover", "change_set"]

        results = [json.loads(result) for result in fake.results]
        assert results[1]["status"] == "committed", results[1]
        assert results[3]["status"] == "committed", results[3]

        assert [approver.answers_given for approver in approvers] == [["y"], ["y"]]
        assert "pEngineSpeed" in approvers[0].prompts[0]
        assert "ComputeSpeed" in approvers[1].prompts[0]
        assert "The exact delta (1 entry)" in approvers[0].prompts[0]

        engine = _read(mcp, {"layer": "swc", "element": "EngineController"})
        assert [port["name"] for port in engine["ports"]] == ["pEngineSpeed"]
        assert [runnable["name"] for runnable in engine["runnables"]] == ["ComputeSpeed"]

        assert engine["satisfies"] == ["REQ_ENG_001@1"]

    assert isinstance(transcript[-1][1], str)
    assert transcript[-1][1]


# ---------------------------------------------------------------------------
# Scenario 1, replayed twice: the determinism claim, asserted
# ---------------------------------------------------------------------------

#: The writer's per-session correlation id, a bare uuid4 hex that differs between identical runs.
_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")


def _normalize(value: object) -> object:
    """Replace everything a second identical run is *allowed* to differ in.

    Three things and no more: the change-set id (``CHANGE_SET_ID_PATTERN``), the writer
    session id, and the absolute path of a shadow tree (a random ``mkdtemp``). Baseline tokens
    are content hashes and are **not** normalized. Absoluteness is asked of both ``ntpath``
    and ``posixpath``, so a path spelled either way is recognized on either host.
    """
    from swcraft_tools.editing.audit import CHANGE_SET_ID_PATTERN

    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items() if key != "timestamp"}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        if CHANGE_SET_ID_PATTERN.match(value):
            return "<change-set-id>"
        if _SESSION_ID.match(value):
            return "<session-id>"
        posix = value.replace("\\", "/")
        if (ntpath.isabs(value) or posixpath.isabs(value)) and "/model/" in posix:
            # The tail is spelled POSIX so the normalized record is the same on every platform.
            return "<shadow>/" + posix.split("/model/", 1)[1]
    return value


class TestTheShadowPathNormalization:
    """Both path spellings are normalized, asserted here because one host only ever produces one."""

    def test_a_posix_shadow_path_is_replaced(self) -> None:
        assert _normalize("/tmp/shadow-a1b2/model/swc/powertrain/EngineController.yaml") == (
            "<shadow>/swc/powertrain/EngineController.yaml"
        )

    def test_a_windows_shadow_path_is_replaced_and_spelled_posix(self) -> None:
        """The spelling a POSIX host never produces."""
        windows = "C:\\Users\\dev\\AppData\\Local\\Temp\\shadow-a1b2\\model\\swc\\powertrain\\EngineController.yaml"

        assert _normalize(windows) == "<shadow>/swc/powertrain/EngineController.yaml"

    def test_the_two_spellings_normalize_to_the_same_string(self) -> None:
        """Not merely both replaced — replaced to the *same* record."""
        posix_form = "/tmp/shadow-a1b2/model/interfaces/ISpeed.yaml"
        windows_form = "C:\\Temp\\shadow-c3d4\\model\\interfaces\\ISpeed.yaml"

        assert _normalize(posix_form) == _normalize(windows_form)

    def test_a_relative_target_is_left_alone(self) -> None:
        """The control: a rule that replaced everything would pass all three above."""
        assert _normalize("swc/powertrain/EngineController.yaml") == "swc/powertrain/EngineController.yaml"


def _audit_records(project: Path) -> list:
    """The project's audit log, normalized, in insertion order."""
    log = project.parent / "dist" / "audit.jsonl"
    return [_normalize(json.loads(line)) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _model_tree(project: Path) -> dict[str, str]:
    """Every model file, keyed by its model-root-relative path."""
    model_root = project.parent / "model"
    return {
        str(path.relative_to(model_root).as_posix()): path.read_text(encoding="utf-8")
        for path in sorted(model_root.rglob("*.yaml"), key=lambda path: path.as_posix())
    }


def _replay_scenario1(project: Path, tmp_path: Path, monkeypatch) -> None:
    """One full scenario 1 replay against *project*."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _seed_scenario1(str(project.parent / "model"))
    with _review_client(project) as mcp:
        _replay(str(_SCENARIOS_DIR / "01-add-swc.yaml"), mcp, _scenario1_program(), tmp_path, monkeypatch)


def test_scenario_replay_is_deterministic(tmp_path, monkeypatch):
    """The same script twice, on two fresh projects: the same model tree and audit records.

    The records are compared modulo only what :func:`_normalize` replaces.
    """
    from tests.conftest import _write_yaml

    def scaffold(name: str) -> Path:
        root = tmp_path / name
        for subdir in REQUIRED_MODEL_SUBDIRS:
            (root / "model" / subdir).mkdir(parents=True, exist_ok=True)
        (root / "dist").mkdir(parents=True, exist_ok=True)
        manifest = root / "swcraft.yaml"
        _write_yaml(
            manifest,
            {
                "name": "agent-test-project",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
            },
        )
        return manifest

    first, second = scaffold("run-a"), scaffold("run-b")
    _replay_scenario1(first, tmp_path / "prompt-a", monkeypatch)
    _replay_scenario1(second, tmp_path / "prompt-b", monkeypatch)

    assert _model_tree(first) == _model_tree(second), "the same script twice must produce the same model tree"

    records_a, records_b = _audit_records(first), _audit_records(second)
    assert records_a, "a committing replay must leave audit records"
    assert records_a == records_b, "the same script twice must produce the same audit records"

    # The normalized ids were really there and really differed.
    raw_a = (first.parent / "dist" / "audit.jsonl").read_text(encoding="utf-8")
    raw_b = (second.parent / "dist" / "audit.jsonl").read_text(encoding="utf-8")
    assert "<change-set-id>" in json.dumps(records_a)
    assert raw_a != raw_b, "the ids and timestamps this test normalizes must genuinely differ between runs"


# ---------------------------------------------------------------------------
# Scenario 2: Connect compatible ports
# ---------------------------------------------------------------------------


def test_scenario2_connect_ports(scaffolded_project, tmp_path, monkeypatch):
    """Scenario 2: commit the connector as a change set; staging judges its two ends."""
    from swcraft_tools import writer

    model_root = str(scaffolded_project.parent / "model")
    seed_minimal_hardware(model_root)
    # The root must place both endpoint types: `composition.connector-endpoint-not-a-member`.
    seed_root_composition(model_root, ["EngineController", "SpeedDisplay"])
    writer.create_data_type(
        model_root, "float32_base", "base_type", size=32, encoding="IEEE754", native_declaration="float"
    )
    writer.create_data_type(model_root, "float32", "value_type", base="float32_base")
    writer.create_interface(model_root, "ISpeed", [{"name": "speed", "implementation_type": "float32"}])
    writer.create_swc(model_root, "EngineController", "engine")
    declare_no_support(model_root, "engine", "EngineController")
    writer.add_port(model_root, "EngineController", "pEngineSpeed", "provide", "ISpeed")
    writer.create_swc(model_root, "SpeedDisplay", "display")
    declare_no_support(model_root, "display", "SpeedDisplay")
    writer.add_port(model_root, "SpeedDisplay", "rEngineSpeed", "require", "ISpeed")
    writer.create_requirement(model_root, "REQ_ENG_001", "Speed signal routing", "Speed shall reach the display.")
    writer.add_trace_link(model_root, "REQ_ENG_001", "EngineController")
    writer.add_trace_link(model_root, "REQ_ENG_001", "SpeedDisplay")

    approvers = _install_recording_approver(monkeypatch)
    scenario_path = str(_SCENARIOS_DIR / "02-connect-ports.yaml")
    program = [
        [
            ("discover", {}),
            change_set(
                "Connect EngineController.pEngineSpeed to SpeedDisplay.rEngineSpeed",
                [
                    {
                        "operation": "create",
                        "address": {"layer": "connectors", "element": "EngineCtrl_to_SpeedDisplay"},
                        "value": {
                            "name": "EngineCtrl_to_SpeedDisplay",
                            "from": {"swc": "EngineController", "port": "pEngineSpeed"},
                            "to": {"swc": "SpeedDisplay", "port": "rEngineSpeed"},
                        },
                    }
                ],
            ),
        ],
        "Connected EngineController.pEngineSpeed to SpeedDisplay.rEngineSpeed.",
    ]

    with _review_client(scaffolded_project) as mcp:
        transcript, turns, fake = _replay(scenario_path, mcp, program, tmp_path, monkeypatch)
        assert len(transcript) == len(turns) == 1
        assert [name for name, _ in fake.calls] == ["discover", "change_set"]

        # The server sends one copy, so the body is the response itself, never a `result` envelope.
        committed = json.loads(fake.results[1])
        assert "result" not in committed, committed
        assert committed["status"] == "committed", committed
        assert "error" not in committed, committed

        assert [approver.answers_given for approver in approvers] == [["y"]]
        assert "EngineCtrl_to_SpeedDisplay" in approvers[0].prompts[0]

        # A summary would answer names alone; `full` publishes the `from`/`to` endpoints.
        connectors = _read(mcp, {"layer": "connectors"}, form="full")
        assert [connector["name"] for connector in connectors] == ["EngineCtrl_to_SpeedDisplay"]
        assert connectors[0]["from"]["swc"] == "EngineController"
        assert connectors[0]["to"]["swc"] == "SpeedDisplay"


# ---------------------------------------------------------------------------
# Scenario 3: Incompatible ports — nothing is ever staged
# ---------------------------------------------------------------------------


def test_scenario3_incompatible_ports_error_surfaces(scaffolded_project, tmp_path, monkeypatch):
    """Scenario 3: the connecting change set is refused at staging, so nothing is written.

    Staging refuses it with `connector.interface-mismatch`, naming both interfaces; a change
    set refused before review asks no review question.
    """
    from swcraft_tools import writer

    model_root = str(scaffolded_project.parent / "model")
    seed_minimal_hardware(model_root)
    # Both endpoint types are placed, so the one finding is the interface mismatch.
    seed_root_composition(model_root, ["EngineController", "BrakeController"])
    writer.create_data_type(
        model_root, "float32_base", "base_type", size=32, encoding="IEEE754", native_declaration="float"
    )
    writer.create_data_type(model_root, "float32", "value_type", base="float32_base")
    writer.create_interface(model_root, "ISpeed", [{"name": "speed", "implementation_type": "float32"}])
    writer.create_interface(model_root, "IPressure", [{"name": "pressure", "implementation_type": "float32"}])
    writer.create_swc(model_root, "EngineController", "engine")
    declare_no_support(model_root, "engine", "EngineController")
    writer.add_port(model_root, "EngineController", "pEngineSpeed", "provide", "ISpeed")
    writer.create_swc(model_root, "BrakeController", "brake")
    declare_no_support(model_root, "brake", "BrakeController")
    writer.add_port(model_root, "BrakeController", "rBrakePressure", "require", "IPressure")
    writer.create_requirement(model_root, "REQ_ENG_001", "Speed and pressure", "Both controllers shall exist.")
    writer.add_trace_link(model_root, "REQ_ENG_001", "EngineController")
    writer.add_trace_link(model_root, "REQ_ENG_001", "BrakeController")

    approvers = _install_recording_approver(monkeypatch)
    scenario_path = str(_SCENARIOS_DIR / "03-incompatible-ports.yaml")
    program = [
        [
            ("discover", {}),
            change_set(
                "Connect EngineController.pEngineSpeed to BrakeController.rBrakePressure",
                [
                    {
                        "operation": "create",
                        "address": {"layer": "connectors", "element": "EngineCtrl_to_BrakeController"},
                        "value": {
                            "name": "EngineCtrl_to_BrakeController",
                            "from": {"swc": "EngineController", "port": "pEngineSpeed"},
                            "to": {"swc": "BrakeController", "port": "rBrakePressure"},
                        },
                    }
                ],
            ),
        ],
        "The ports are incompatible: ISpeed vs IPressure. Cannot connect.",
    ]

    with _review_client(scaffolded_project) as mcp:
        transcript, turns, fake = _replay(scenario_path, mcp, program, tmp_path, monkeypatch)
        assert len(transcript) == len(turns) == 1
        assert [name for name, _ in fake.calls] == ["discover", "change_set"]

        refusal = json.loads(fake.results[1])
        assert "result" not in refusal, refusal
        assert refusal["response"] == "error", refusal
        assert refusal["error"] == "consistency-violation", refusal
        assert "interface mismatch" in refusal["message"], refusal
        assert "'IPressure'" in refusal["message"] and "'ISpeed'" in refusal["message"], refusal
        assert [address["path"][0]["field"] for address in refusal["addresses"]] == ["from", "to"], refusal

        assert [approver.prompts for approver in approvers] == [[]]
        assert _read(mcp, {"layer": "connectors"}) == []


# ---------------------------------------------------------------------------
# Scenario 4: Requirement trace link
# ---------------------------------------------------------------------------


def test_scenario4_requirement_trace(scaffolded_project, tmp_path, monkeypatch):
    """Scenario 4: a trace link is authored as a change-set operation.

    The link is a member ``create`` at ``swc/<name>.satisfies[member]`` (a ``whole_value``
    member: added and removed, never edited in place). Turn 2 reads the component and changes nothing.
    """
    from swcraft_tools import writer

    model_root = str(scaffolded_project.parent / "model")
    seed_minimal_hardware(model_root)
    seed_root_composition(model_root)
    writer.create_swc(model_root, "EngineController", "engine")
    declare_no_support(model_root, "engine", "EngineController")

    approvers = _install_recording_approver(monkeypatch)
    scenario_path = str(_SCENARIOS_DIR / "04-requirement-trace.yaml")
    program = [
        [
            ("discover", {}),
            change_set(
                "Add REQ_ENG_001 and trace EngineController to it",
                [
                    {
                        "operation": "create",
                        "address": {"layer": "requirements", "element": "REQ_ENG_001"},
                        "value": {
                            "id": "REQ_ENG_001",
                            "title": "Engine speed control shall be implemented",
                            "description": "The engine speed control function shall be provided by EngineController.",
                            "req_type": "req",
                            "version": 1,
                            "source": "architecture",
                        },
                    },
                    {
                        "operation": "create",
                        "address": {
                            "layer": "swc",
                            "element": "EngineController",
                            "path": [{"field": "satisfies"}, {"member": "REQ_ENG_001@1"}],
                        },
                        "value": "REQ_ENG_001@1",
                    },
                ],
            ),
        ],
        "Added requirement REQ_ENG_001 and linked it to EngineController.",
        [("read", {"address": {"layer": "swc", "element": "EngineController"}})],
        "EngineController is traced to REQ_ENG_001.",
    ]

    with _review_client(scaffolded_project) as mcp:
        transcript, turns, fake = _replay(scenario_path, mcp, program, tmp_path, monkeypatch)
        assert len(transcript) == len(turns) == 2
        assert [name for name, _ in fake.calls] == ["discover", "change_set", "read"]
        assert json.loads(fake.results[1])["status"] == "committed", fake.results[1]

        assert [approver.answers_given for approver in approvers] == [["y"], []]
        assert json.loads(fake.results[2])["value"]["satisfies"] == ["REQ_ENG_001@1"]

        requirements = _read(mcp, {"layer": "requirements"}, form="full")
        assert [requirement["id"] for requirement in requirements] == ["REQ_ENG_001"]
        engine = _read(mcp, {"layer": "swc", "element": "EngineController"})
        assert engine["satisfies"] == ["REQ_ENG_001@1"]


# ---------------------------------------------------------------------------
# Retry mechanism: fail -> fix -> retry -> success within one chat() call
# ---------------------------------------------------------------------------


class _RetryStubMcp:
    """MCP stand-in: the first ``build`` fails, the second succeeds; every call is recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        # The real build tool's result shape, in its key order.
        self._build_results = [
            json.dumps(
                {
                    "success": False,
                    "errors": [
                        {
                            "message": "connector references missing port pEngineSpeed on EngineController",
                            "path": "model/connectors/EngineCtrl_to_SpeedDisplay.yaml",
                        }
                    ],
                    "warnings": [],
                    "output": "",
                }
            ),
            json.dumps({"success": True, "errors": [], "warnings": [], "output": "build ok"}),
        ]
        self._build_count = 0

    def list_openai_tools(self) -> list[dict]:
        return []

    def call_tool(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        if name == "build":
            result = self._build_results[self._build_count]
            self._build_count += 1
        elif name == "discover":
            result = json.dumps({"response": "discover", "baseline": "rev1:stub"})
        else:
            result = json.dumps({"response": "change_set", "status": "committed", "baseline": "rev1:stub2"})
        return result


def test_retry_mechanism_fail_fix_retry_success(tmp_path, monkeypatch):
    """Multi-round tool use supports build (fails) -> correction -> build (passes) in one chat() call."""
    from swcraft_agent.core import SwcraftAgent

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    port_operation = {
        "operation": "create",
        "address": {
            "layer": "swc",
            "element": "EngineController",
            "path": [{"field": "ports"}, {"member": "pEngineSpeed"}],
        },
        "value": {"name": "pEngineSpeed", "direction": "provide", "interface": "ISpeed"},
    }
    program = [
        [("build", {})],
        [("discover", {}), change_set("Add the missing provide port pEngineSpeed", [port_operation])],
        [("build", {})],
        "Build failed on the missing port; added pEngineSpeed and the rebuild succeeded.",
    ]
    fake = _ProtocolFake(program)
    stub = _RetryStubMcp()
    agent = SwcraftAgent(project_path="unused", llm=fake, mcp=stub)

    reply = agent.chat("Build the project and fix any errors you hit.")

    assert reply == "Build failed on the missing port; added pEngineSpeed and the rebuild succeeded."

    assert [name for name, _ in stub.calls] == ["build", "discover", "change_set", "build"]
    # The baseline `discover` answered, not the program's sentinel.
    assert stub.calls[2][1]["baseline"] == "rev1:stub"


def _place_in_root(model_root: Path, *names: str) -> None:
    """Append one member per *name*, named after its type, to the model's root composition.

    Edited directly: the writer has no composition-member mutation (`create_composition` writes a whole file).
    """
    path = model_root / "compositions" / "Root.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.setdefault("members", []).extend({"name": name, "type": name} for name in names)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Scenario 5: Build error fix (slow, requires a git worktree checkout)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_scenario5_build_error_fix(worktree_project, tmp_path, monkeypatch):
    """Scenario 5: seed a broken connector; the agent builds, repairs with one reviewed change set, rebuilds.

    The connector is written directly because the writer refuses that state. The turn is
    **unscoped** on purpose: a scoped turn refuses a ``build`` that shells out, so this is the
    path that exercises a real ``SWCRAFT_MCP_BUILD_COMMAND``. Runs under ``dev.py test-slow``.
    """
    from swcraft_tools import writer

    worktree_manifest = worktree_project
    model_root = worktree_manifest.parent / "model"

    # `memory` is mandatory: examples/basic declares a `strict` memory `RAM0`, so an SWC with
    # no budget is a `missing_budget` error that would fail the second build too.
    print("\n  [slow-test] Step 1/4: Seeding model (interface + SWCs + port) ...", flush=True)
    m = str(model_root)
    writer.create_interface(m, "ISpeed", [{"name": "speed", "implementation_type": "float32"}])
    writer.create_swc(m, "EngineController", "engine", memory={"RAM0": 4096})
    writer.create_swc(m, "SpeedDisplay", "display", memory={"RAM0": 2048})
    writer.add_port(m, "SpeedDisplay", "rEngineSpeed", "require", "ISpeed")
    # Without `handle_termination_and_restart`, `rule:swc.undeclared-termination-and-restart` would
    # fail the second build too; `writer.create_swc` takes no such parameter.
    for group, name in (("engine", "EngineController"), ("display", "SpeedDisplay")):
        seeded = model_root / "swc" / group / f"{name}.yaml"
        seeded.write_text(
            seeded.read_text(encoding="utf-8") + "handle_termination_and_restart: no_support\n",
            encoding="utf-8",
            newline="\n",
        )
    # Both SWCs traced, so validate lists nothing beside the one expected failure.
    writer.create_requirement(
        m,
        "REQ-SC5-001",
        "Speed signal routing",
        "EngineController shall provide speed signal to SpeedDisplay.",
    )
    writer.add_trace_link(m, "REQ-SC5-001", "EngineController")
    writer.add_trace_link(m, "REQ-SC5-001", "SpeedDisplay")
    # `composition.connector-endpoint-not-a-member` requires the root to place both endpoint
    # types. This APPENDS: `seed_root_composition`'s whole-file write would drop the members
    # and connector `examples/basic`'s root already has.
    _place_in_root(model_root, "EngineController", "SpeedDisplay")
    # pEngineSpeed is intentionally missing: the connector below references it.

    print("  [slow-test] Step 2/4: Writing broken connector (bypassing validation) ...", flush=True)
    connector_file = model_root / "connectors" / "EngineCtrl_to_SpeedDisplay.yaml"
    connector_file.parent.mkdir(parents=True, exist_ok=True)
    connector_file.write_text(
        "name: EngineCtrl_to_SpeedDisplay\n"
        "from:\n"
        "  swc: EngineController\n"
        "  port: pEngineSpeed\n"
        "to:\n"
        "  swc: SpeedDisplay\n"
        "  port: rEngineSpeed\n"
        "description: Broken connector seeded for build-error scenario\n",
        encoding="utf-8",
    )

    scenario_path = str(_SCENARIOS_DIR / "05-build-error-fix.yaml")
    approvers = _install_recording_approver(monkeypatch)

    program = [
        [("build", {})],
        [
            ("discover", {}),
            change_set(
                "Add the provide port pEngineSpeed the connector already references",
                [
                    {
                        "operation": "create",
                        "address": {
                            "layer": "swc",
                            "element": "EngineController",
                            "path": [{"field": "ports"}, {"member": "pEngineSpeed"}],
                        },
                        "value": {"name": "pEngineSpeed", "direction": "provide", "interface": "ISpeed"},
                    }
                ],
            ),
        ],
        [("build", {})],
        "Build was failing due to a missing port. Added pEngineSpeed and the rebuild succeeded.",
    ]

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    # The Windows Git Credential Manager can block indefinitely during VCS detection.
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GCM_INTERACTIVE", "never")
    # An empty credential.helper per process (git >= 2.31), leaving ~/.gitconfig untouched.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "")

    monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", build_override_command(worktree_manifest))

    with _review_client(worktree_manifest) as mcp:
        print("  [slow-test] Step 3/4: Running agent scenario (build fail -> fix -> build pass) ...", flush=True)
        transcript, turns, fake = _replay(scenario_path, mcp, program, tmp_path, monkeypatch)

    print("  [slow-test] Step 4/4: Verifying build results and the applied correction ...", flush=True)

    assert len(transcript) == len(turns) == 1
    assert [name for name, _ in fake.calls] == ["build", "discover", "change_set", "build"]

    first_build = json.loads(fake.results[0])
    second_build = json.loads(fake.results[3])
    assert "result" not in first_build and "result" not in second_build, (first_build, second_build)
    first_inner, second_inner = first_build, second_build
    assert first_inner["success"] is False, f"Expected first build to fail: {first_inner}"
    assert second_inner["success"] is True, f"Expected second build to succeed: {second_inner}"

    first_text = json.dumps(first_inner)
    assert "pEngineSpeed" in first_text and "EngineController" in first_text, (
        f"First build error must mention the seeded missing port pEngineSpeed and EngineController; got: {first_text}"
    )

    assert [approver.answers_given for approver in approvers] == [["y"]]
    assert "pEngineSpeed" in approvers[0].prompts[0]
    assert json.loads(fake.results[2])["status"] == "committed", fake.results[2]

    swc_file = model_root / "swc" / "engine" / "EngineController.yaml"
    assert swc_file.is_file(), f"EngineController.yaml missing after the scenario: {swc_file}"
    engine_swc = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    port_names = [p.get("name") for p in engine_swc.get("ports", [])]
    assert "pEngineSpeed" in port_names, (
        f"The agent's corrective change set must be persisted to the model; ports={port_names}"
    )

    import importlib

    import swcraft_tools as _st

    importlib.reload(_st)
    st_file = _st.__file__ or ""
    assert str(_REPO_ROOT) in st_file, (
        f"swcraft_tools is no longer importable from the real repo after scenario 5. "
        f"The worktree build likely rewrote the editable install. "
        f"swcraft_tools.__file__ = {st_file!r}, expected under {_REPO_ROOT}"
    )


# ---------------------------------------------------------------------------
# Scenario 6: task-scoped mutation authorization
# ---------------------------------------------------------------------------


@pytest.fixture()
def scoped_project(tmp_path: Path) -> Path:
    """A real project seeded with exactly what scenario 6's four turns touch.

    Turn 3 weakens a memory threshold, so the minimal hardware topology is declared. The
    baseline is **free of every finding** (a ``RAM0`` budget and a requirement link on the
    existing SWC): the engine validates the whole staged result, so an invalid baseline would
    refuse every change set.
    """
    from swcraft_tools import writer

    root = tmp_path / "scoped_proj"
    model_root = root / "model"
    for subdir in MODEL_SUBDIRS:
        (model_root / subdir).mkdir(parents=True, exist_ok=True)
    (root / "dist").mkdir(parents=True, exist_ok=True)

    m = str(model_root)
    writer.create_data_type(m, "uint32_base", "base_type", size=32, encoding="NONE", native_declaration="uint32_t")
    writer.create_data_type(m, "uint32", "value_type", base="uint32_base")
    writer.create_interface(m, "ISpeed", [{"name": "speed", "implementation_type": "uint32"}])
    writer.create_hardware_element(m, "CPU", "processor")
    writer.create_hardware_element(m, "CORE0", "core", threshold=0.7)
    writer.create_hardware_element(
        m, "RAM0", "memory", memory_type="RAM", size=65536, accessible=["CORE0"], threshold=0.5
    )
    seed_root_composition(m)
    writer.create_swc(m, "EngineController", "powertrain", memory={"RAM0": 4096})
    declare_no_support(m, "powertrain", "EngineController")
    writer.create_requirement(m, "REQ-001", "Speed", "The system shall measure the vehicle speed")
    writer.add_trace_link(m, "REQ-001", "EngineController")

    manifest = root / "swcraft.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "name": "scenario6-project",
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


def _scope(summary: str, entries: list[dict]) -> dict:
    return {"summary": summary, "entries": entries}


def _entry(operation: str, layer: str, element: str) -> dict:
    return {"operation": operation, "address": {"layer": layer, "element": element}}


def _split_result(result: str) -> tuple[dict, str]:
    """One tool result as the model sees it: the protocol body and any host note appended behind it."""
    payload, end = json.JSONDecoder().raw_decode(result)
    return payload, result[end:].strip()


def _protocol_half(result: str) -> dict:
    return _split_result(result)[0]


def _note_half(result: str) -> str:
    return _split_result(result)[1]


def test_scenario6_scoped_authorization(scoped_project, tmp_path, monkeypatch):
    """Scenario 6: one scoped run covering all four outcomes, on the protocol.

    Turn 1 commits in scope. Turn 2's unauthorized "for consistency" twin is refused by the
    engine's gate 1, reported, never retried or re-proposed wider. Turn 3 approves the delta
    and **declines the separate safety question**, the only thing that can authorize a
    weakening. Turn 4 is declined at the delta review and leaves the model byte-identical.
    Everything below the LLM and the human is real, with an MCP subprocess per turn.
    """
    from swcraft_agent.cli import run_script
    from swcraft_agent.core import SwcraftAgent
    from swcraft_tools.session import model_fingerprint

    model_root = str(scoped_project.parent / "model")
    approvers = _install_recording_approver(monkeypatch, model_root)

    scenario_path = str(_SCENARIOS_DIR / "06-scoped-authorization.yaml")

    brake_swc = {
        "operation": "create",
        "address": {"layer": "swc", "element": "BrakeController"},
        "value": {
            "name": "BrakeController",
            "group": "chassis",
            "type": "application",
            "memory": {"RAM0": 2048},
            "handle_termination_and_restart": "no_support",
            "satisfies": ["REQ-002@1"],
        },
    }
    brake_requirement = {
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
    dashboard_swc = {
        "operation": "create",
        "address": {"layer": "swc", "element": "Dashboard"},
        "value": {
            "name": "Dashboard",
            "group": "body",
            "type": "application",
            "memory": {"RAM0": 1024},
            "handle_termination_and_restart": "no_support",
            "satisfies": ["REQ-003@1"],
        },
    }
    dashboard_requirement = {
        "operation": "create",
        "address": {"layer": "requirements", "element": "REQ-003"},
        "value": {
            "id": "REQ-003",
            "title": "Dashboard",
            "description": "The system shall display the vehicle state",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    }

    program = [
        [
            ("discover", {}),
            change_set("Add the SWC BrakeController with its RAM0 budget", [brake_swc, brake_requirement]),
        ],
        "Added BrakeController in the chassis group with a 2048-byte RAM0 budget.",
        # Turn 2: the authorized port, then the same port on an SWC nobody approved.
        [
            ("discover", {}),
            change_set(
                "Add pBrakePressure to BrakeController",
                [
                    {
                        "operation": "create",
                        "address": {
                            "layer": "swc",
                            "element": "BrakeController",
                            "path": [{"field": "ports"}, {"member": "pBrakePressure"}],
                        },
                        "value": {"name": "pBrakePressure", "direction": "provide", "interface": "ISpeed"},
                    }
                ],
            ),
            ("discover", {}),
            change_set(
                "Add pBrakePressure to EngineController for consistency",
                [
                    {
                        "operation": "create",
                        "address": {
                            "layer": "swc",
                            "element": "EngineController",
                            "path": [{"field": "ports"}, {"member": "pBrakePressure"}],
                        },
                        "value": {"name": "pBrakePressure", "direction": "provide", "interface": "ISpeed"},
                    }
                ],
            ),
        ],
        (
            "Added pBrakePressure to BrakeController. Adding the same port to EngineController was "
            "refused as outside the approved scope, so I stopped there and did not retry it."
        ),
        # Turn 3: the weakening. The delta is approved; the safety question is not.
        [
            ("discover", {}),
            change_set(
                "Raise the RAM0 threshold",
                [
                    {
                        "operation": "update",
                        "address": {"layer": "hardware", "element": "RAM0", "path": [{"field": "threshold"}]},
                        "value": 0.9,
                    }
                ],
            ),
        ],
        "Raising the RAM0 threshold was not applied: you declined the weakening it would make.",
        # Turn 4: an in-scope creation the user then declines.
        [
            ("discover", {}),
            change_set(
                "Add the SWC Dashboard with its RAM0 budget",
                [dashboard_swc, dashboard_requirement],
            ),
        ],
        "Dashboard was not added: you declined the change.",
    ]
    structured = [
        _scope(
            "Add the SWC BrakeController with its RAM0 budget",
            [_entry("create", "swc", "BrakeController"), _entry("create", "requirements", "REQ-002")],
        ),
        _scope(
            "Add the provide port pBrakePressure to BrakeController",
            [_entry("create", "swc", "BrakeController")],
        ),
        _scope("Raise the RAM0 threshold", [_entry("update", "hardware", "RAM0")]),
        _scope(
            "Add the SWC Dashboard with its RAM0 budget",
            [_entry("create", "swc", "Dashboard"), _entry("create", "requirements", "REQ-003")],
        ),
    ]

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are a test assistant.", encoding="utf-8")
    monkeypatch.setenv("SWCRAFT_AGENT_SYSTEM_PROMPT", str(prompt_file))

    fake = _ProtocolFake(program, structured=structured)
    # No client is injected: the approved scope travels in each turn's server environment.
    agent = SwcraftAgent(project_path=str(scoped_project), llm=fake, scoped=True, review="elicit")
    transcript = run_script(agent, scenario_path)

    swc_dir = scoped_project.parent / "model" / "swc"
    results = [_protocol_half(result) for result in fake.results]
    notes = [_note_half(result) for result in fake.results]

    assert len(transcript) == 4
    assert len(approvers) == 4, "run_script installs one approver per turn"
    # One proposal per turn: a refusal or a decline is never re-proposed.
    assert len(fake.structured_calls) == 4
    assert len(fake.calls) == 10, f"unexpected tool calls: {[name for name, _ in fake.calls]}"

    # -- Turn 1: the approved change reaches the real model -----------------
    brake_file = swc_dir / "chassis" / "BrakeController.yaml"
    assert brake_file.is_file(), "the approved in-scope change must be committed"
    assert results[1]["status"] == "committed", results[1]
    assert approvers[0].answers_given == ["y", "y"], "exactly two questions: the scope, then the delta"

    # -- Turn 2: the authorized half lands, the unauthorized half is refused --
    assert results[3]["status"] == "committed", results[3]
    assert results[5]["response"] == "error"
    assert results[5]["error"] == "unauthorized-delta", results[5]
    assert "EngineController" in results[5]["message"]
    assert "EngineController" in transcript[1][1], "the refusal is reported to the user"
    # Gate 1 runs before staging, so the refused change set asked no question.
    assert approvers[1].answers_given == ["y", "y"]

    brake = yaml.safe_load(brake_file.read_text(encoding="utf-8"))
    assert [p["name"] for p in brake.get("ports") or []] == ["pBrakePressure"]

    engine = yaml.safe_load((swc_dir / "powertrain" / "EngineController.yaml").read_text(encoding="utf-8"))
    engine_ports = [p["name"] for p in engine.get("ports") or []]
    assert "pBrakePressure" not in engine_ports, "the unauthorized port never reached the real model"

    # -- Turn 3: the delta is approved and the weakening is not --------------
    # Approving a delta is not approving the protection it removes, so the safety question is separate.
    assert approvers[2].answers_given == ["y", "y", "n"]
    assert len(approvers[2].prompts) == 3
    assert "0.9" in approvers[2].prompts[1], "the delta prompt states the value that would land"
    assert "0.9" in approvers[2].prompts[2] and "0.5" in approvers[2].prompts[2], (
        "the safety prompt states the weakening itself, old value and new"
    )
    # The note tells the model which question was refused, so it can re-author without the weakening.
    assert results[7]["status"] == "abandoned", results[7]
    assert "did not approve the safety change it carries" in notes[7], notes[7]
    assert "RAM0" in notes[7], notes[7]
    memory = yaml.safe_load((scoped_project.parent / "model" / "hardware" / "RAM0.yaml").read_text(encoding="utf-8"))
    assert memory["threshold"] == 0.5, "a declined weakening leaves the limit exactly as it was"

    # -- Turn 4: the declined turn changes nothing --------------------------
    assert approvers[3].answers_given == ["y", "n"]
    assert results[9]["status"] == "abandoned", results[9]
    assert not (swc_dir / "body" / "Dashboard.yaml").exists()
    assert model_fingerprint(model_root) == approvers[3].fingerprint_at_start, (
        "the declined turn must leave the real model byte-identical to how it found it"
    )

    # -- The audit trail records every decision -----------------------------
    log = scoped_project.parent / "dist" / "audit.jsonl"
    operations = [
        json.loads(line)["operation"] for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    for expected in (
        "scope_proposed",
        "scope_activated",
        "change_set_staged",
        "change_set_rejected",
        "change_set_committed",
        "change_set_abandoned",
    ):
        assert expected in operations, f"missing audit record {expected}: {operations}"
