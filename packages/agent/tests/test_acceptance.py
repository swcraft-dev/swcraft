"""Acceptance checks over the agent project's build wiring, its tests and the prompt it sends.

- the agent project is covered by the native `scripts/dev.py` orchestration
  (install into the repo-local `.venv`, the fast test gate, lint, the slow stage),
  and installs are sequenced before tests -- read off the plan `dev.py` builds
- lint fans out to every Python project, the repo-root `scripts/` and Biome, and
  `scripts/run_python_lint.py` fails on a ruff check or format finding
- no test file instantiates a real `AzureOpenAIClient()`
- the system message a `SwcraftAgent` hands the model names no tool the live MCP
  server does not register, and only layers and kinds the shipped registry declares
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path

from swcraft_agent.core import SwcraftAgent
from swcraft_tools.model_registry import production_registry

from tests.conftest import FakeLLMClient, FakeMcp

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENT_ROOT = _REPO_ROOT / "packages" / "agent"
_DEV_SCRIPT = _REPO_ROOT / "scripts" / "dev.py"


def _load_dev_module():
    """Import ``scripts/dev.py`` by path, registered before execution.

    ``dev.py``'s dataclasses resolve their annotations through
    ``sys.modules[cls.__module__]``, which a bare ``exec_module`` leaves unset.
    """
    name = "swcraft_dev_script"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _DEV_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_DEV = _load_dev_module()


def _context(repo_root: Path):
    """A ``dev.py`` Context built without touching the filesystem."""
    return _DEV.Context(
        repo_root=repo_root,
        python=str(_DEV.venv_python(repo_root, "posix")),
        bootstrap_python="/usr/bin/python3",
        npm="/usr/local/bin/npm",
        has_moon=True,
    )


def _projects_of(steps, repo_root: Path) -> list[str]:
    """The project each planned step runs in, in order: its label under ``packages/``, else its directory."""
    packages = repo_root / _DEV.PACKAGES_DIR
    return [
        "."
        if step.cwd == repo_root
        else step.cwd.relative_to(packages if step.cwd.is_relative_to(packages) else repo_root).as_posix()
        for step in steps
    ]


def _marker_expression(step) -> str:
    """The ``-m <expr>`` marker expression of a pytest step.

    Scanned from the right: ``python -m pytest`` puts an unrelated ``-m`` first.
    """
    argv = [str(part) for part in step.argv]
    assert "-m" in argv, f"{step.label} is not a marker-filtered pytest step: {argv}"
    return argv[len(argv) - 1 - argv[::-1].index("-m") + 1]


# ---------------------------------------------------------------------------
# The agent project is covered by the native orchestration
# ---------------------------------------------------------------------------


class TestAgentProjectIsOrchestrated:
    """The agent project's install, fast test and slow stages, asserted against the plan ``dev.py`` builds."""

    def test_install_puts_the_agent_into_the_repo_local_venv(self, tmp_path):
        steps = _DEV.plan_install(_context(tmp_path))
        agent_steps = [step for step in steps if step.cwd == tmp_path / _DEV.PACKAGES_DIR / "agent"]
        assert agent_steps, "dev.py install must install the agent project"
        for step in agent_steps:
            assert step.argv[0] == str(_DEV.venv_python(tmp_path, "posix")), (
                "the editable install must target the repo-local .venv interpreter, not the "
                "interpreter that happens to be running -- that shared-interpreter race is what "
                "the venv (and this rewrite) exists to remove"
            )
            assert "-e" in step.argv

    def test_the_mcp_server_is_installed_too(self, tmp_path):
        """The agent spawns ``python -m swcraft_mcp.server`` from its own interpreter, so the server is installed."""
        projects = _projects_of(_DEV.plan_install(_context(tmp_path)), tmp_path)
        assert "mcp" in projects

    def test_the_fast_gate_runs_the_agent_suite_with_slow_deselected(self, tmp_path):
        steps = [
            step for step in _DEV.plan_test(_context(tmp_path)) if step.cwd == tmp_path / _DEV.PACKAGES_DIR / "agent"
        ]
        assert len(steps) == 1
        assert "not slow" in _marker_expression(steps[0])

    def test_a_dedicated_stage_runs_the_slow_suite(self, tmp_path):
        """The worktree scenario replay needs an explicit stage or it never runs."""
        assert "test-slow" in _DEV.COMMANDS
        steps = [
            step
            for step in _DEV.plan_test_slow(_context(tmp_path))
            if step.cwd == tmp_path / _DEV.PACKAGES_DIR / "agent"
        ]
        assert len(steps) == 1
        assert _marker_expression(steps[0]) == "slow"

    def test_the_agent_is_linted(self, tmp_path):
        projects = _projects_of(_DEV.plan_lint(_context(tmp_path)), tmp_path)
        assert "agent" in projects


# ---------------------------------------------------------------------------
# Install-before-test ordering
# ---------------------------------------------------------------------------


class TestInstallIsSequencedBeforeTests:
    """Installs are planned before tests, and every command but ``install`` requires the ``.venv``."""

    def test_verify_runs_install_before_lint_test_and_build(self):
        stages = list(_DEV.VERIFY_STAGES)
        assert stages[0] == "install"
        for later in ("lint", "test", "build"):
            assert stages.index("install") < stages.index(later)

    def test_install_covers_every_project_in_dependency_order(self, tmp_path):
        projects = _projects_of(_DEV.plan_install(_context(tmp_path)), tmp_path)
        assert [p for p in projects if p in _DEV.PYTHON_PROJECTS] == list(_DEV.PYTHON_PROJECTS)
        assert _DEV.VSCODE_PROJECT in projects
        assert projects.index("agent") < projects.index(_DEV.VSCODE_PROJECT)

    def test_the_fast_gate_covers_every_project_in_dependency_order(self, tmp_path):
        projects = _projects_of(_DEV.plan_test(_context(tmp_path)), tmp_path)
        assert projects == [*_DEV.PYTHON_PROJECTS, *_DEV.TEST_ONLY_PROJECTS, _DEV.VSCODE_PROJECT]

    def test_every_command_but_install_requires_the_venv(self):
        """No command may fall back to ``sys.executable`` when ``.venv`` is absent."""
        exempt = {"install", "update-dev-lock", "verify"}  # update-dev-lock runs uv alone, no Python
        for name, command in _DEV.COMMANDS.items():
            assert command.needs_venv is (name not in exempt), name


# ---------------------------------------------------------------------------
# Lint fan-out: the Python projects, repo-root scripts/, and Biome
# ---------------------------------------------------------------------------


class TestLintFanOut:
    """Lint covers every Python project, the repo-root ``scripts/`` (which has no project of its own) and Biome."""

    def test_every_python_project_is_linted(self, tmp_path):
        projects = _projects_of(_DEV.plan_lint(_context(tmp_path)), tmp_path)
        for project in _DEV.PYTHON_PROJECTS:
            assert project in projects

    def test_repo_root_scripts_are_linted(self, tmp_path):
        steps = [step for step in _DEV.plan_lint(_context(tmp_path)) if step.cwd == tmp_path]
        assert steps, "dev.py lint must lint repo-root scripts/"
        assert any(str(step.argv[-1]) == "scripts" for step in steps)

    def test_the_shared_no_shell_ruff_runner_is_used(self, tmp_path):
        """Every ruff step -- each Python project's and ``scripts/`` -- goes through the runner.

        The import-graph and quality-ratchet steps run no ruff and are left out by their labels;
        Biome is the extension's.
        """
        runner = str(tmp_path / "scripts" / "run_python_lint.py")
        not_ruff = (_DEV.IMPORT_LINT_LABEL, _DEV.QUALITY_RATCHET_LABEL)
        ruff_steps = [
            step
            for step in _DEV.plan_lint(_context(tmp_path))
            if step.cwd != tmp_path / _DEV.VSCODE_PROJECT and step.label not in not_ruff
        ]
        assert len(ruff_steps) == len(_DEV.PYTHON_PROJECTS) + 1, "one ruff step per Python project plus scripts/"
        for step in ruff_steps:
            assert runner in [str(part) for part in step.argv], step.label

    def test_the_extension_is_linted_through_its_npm_script(self, tmp_path):
        steps = [step for step in _DEV.plan_lint(_context(tmp_path)) if step.cwd == tmp_path / _DEV.VSCODE_PROJECT]
        assert [tuple(step.argv[1:]) for step in steps] == [("run", "lint")]

    def test_root_ruff_toml_exists(self):
        assert (_REPO_ROOT / "ruff.toml").is_file(), (
            "repo root must have a ruff.toml shared by every Python project's lint step"
        )


class TestVscodeExtensionBiomeLintWiring:
    """Biome stays the extension's linter, reached through ``npm run lint``."""

    def test_the_lint_npm_script_uses_the_no_shell_runner(self):
        package_json = json.loads((_REPO_ROOT / "vscode_extension" / "package.json").read_text(encoding="utf-8"))
        assert "scripts/run-lint.mjs" in package_json["scripts"]["lint"], (
            "the extension's lint script must use the no-shell scripts/run-lint.mjs runner"
        )

    def test_vscode_extension_biome_config_exists(self):
        assert (_REPO_ROOT / "vscode_extension" / "biome.json").is_file(), (
            "vscode_extension must have a biome.json configuring the Biome lint/format baseline"
        )

    def test_vscode_extension_biome_devdependency_declared(self):
        package_json = json.loads((_REPO_ROOT / "vscode_extension" / "package.json").read_text(encoding="utf-8"))
        assert "@biomejs/biome" in package_json.get("devDependencies", {}), (
            "vscode_extension/package.json must declare @biomejs/biome as a devDependency"
        )


class TestRunPythonLintScript:
    """scripts/run_python_lint.py, the no-shell `ruff check` + `ruff format --check` runner of every lint step."""

    def test_exits_nonzero_on_a_ruff_finding(self, tmp_path):
        import subprocess
        import sys

        (tmp_path / "bad.py").write_text("import os\ndef foo():\n    pass\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "scripts" / "run_python_lint.py"), str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode != 0, (
            f"run_python_lint.py must exit non-zero on a ruff finding; got 0. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_exits_zero_on_clean_input(self, tmp_path):
        import subprocess
        import sys

        (tmp_path / "good.py").write_text('"""A clean module."""\n\n\ndef foo() -> None:\n    pass\n', encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "scripts" / "run_python_lint.py"), str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, (
            f"run_python_lint.py must exit 0 on clean input. stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_exits_nonzero_when_check_passes_but_format_fails(self, tmp_path):
        """A format finding fails the run although `ruff check` passed.

        The input is clean under the repo's lint selection (no quote-style rules), but `ruff format` rewrites its
        single quotes.
        """
        import subprocess
        import sys

        (tmp_path / "unformatted.py").write_text(
            '"""A module that passes ruff check but fails ruff format --check."""\n\n\n'
            "def foo() -> str:\n"
            "    return 'single quotes are lint-clean but format-dirty'\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "scripts" / "run_python_lint.py"), str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode != 0, (
            f"run_python_lint.py must exit non-zero when ruff check passes but ruff format --check "
            f"does not; got 0. stdout={result.stdout!r} stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# No real Azure calls in test files
# ---------------------------------------------------------------------------


class TestNoAzureCallsInTests:
    """AzureOpenAIClient() must not be instantiated in test files (no real Azure calls)."""

    def test_no_azure_openai_client_in_tests(self):
        test_dir = _AGENT_ROOT / "tests"
        # test_llm_client.py constructs the client under monkeypatched env vars; this file holds the pattern.
        allowed = {"test_llm_client.py", "test_acceptance.py"}
        violations = []
        for py_file in sorted(test_dir.rglob("*.py"), key=lambda path: path.as_posix()):
            if py_file.name in allowed:
                continue
            lines = py_file.read_text(encoding="utf-8").splitlines()
            count = sum(
                1 for line in lines if re.search(r"AzureOpenAIClient\s*\(", line) and not line.lstrip().startswith("#")
            )
            if count:
                violations.append(f"{py_file.name}: {count} occurrence(s)")
        assert not violations, (
            "AzureOpenAIClient() instantiation found in non-llm-client test files "
            "(no real Azure calls allowed):\n" + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# The system prompt against the live surface
# ---------------------------------------------------------------------------


def _replaced_tool_names() -> list[str]:
    """Every tool the frozen decision table replaces with the generic protocol.

    Read from the table's `replaced` name set rather than listed here, so a newly replaced tool is covered without
    editing this test.
    """
    projections = json.loads(
        (
            _REPO_ROOT
            / "packages"
            / "tools"
            / "tests"
            / "fixtures"
            / "contracts"
            / "editing-protocol"
            / "tool-projections.json"
        ).read_text(encoding="utf-8")
    )
    names = projections["projections"]["replaced"]
    assert len(names) == 29, f"expected the 29 replaced tools of the frozen table, got {len(names)}"
    return names


def _system_prompt(project, monkeypatch) -> str:
    """The system message a :class:`SwcraftAgent` over *project* hands the model, resolved as an installation would.

    Read off the agent's private ``_conversation``: no public accessor carries the system
    message, so a renamed attribute fails here although the agent still sends the prompt.
    """
    monkeypatch.delenv("SWCRAFT_AGENT_SYSTEM_PROMPT", raising=False)
    agent = SwcraftAgent(str(project), llm=FakeLLMClient([]), mcp=FakeMcp())
    (system,) = [message for message in agent._conversation.messages if message["role"] == "system"]
    return system["content"]


def _live_tool_names() -> set[str]:
    """The names the MCP server's own ``tools/list`` answers."""
    # Imported here, not at the top: the server module costs about a second to import.
    from swcraft_mcp import server

    return {tool.name for tool in asyncio.run(server.mcp.list_tools())}


class TestSystemPromptCoverage:
    """The system message the agent hands the model, checked against what the live surface answers.

    One check against the MCP server's own `tools/list`, one against the kernel registry; neither pins a sentence
    of the prompt.
    """

    def test_the_prompt_names_no_tool_the_protocol_replaced(self, scaffolded_project, monkeypatch):
        """The prompt the agent hands the model names no replaced tool the live server does not register.

        A prompt naming an unlisted tool plans a call that does not exist. Names match as whole identifiers.
        Not reached: a tool name the decision table never held (the prompt's backticked words are mostly field names).
        """
        live = _live_tool_names()
        retired = [name for name in _replaced_tool_names() if name not in live]
        assert retired, "the decision table's replaced tools are all registered again"

        prompt = _system_prompt(scaffolded_project, monkeypatch)
        offenders = sorted(
            name for name in retired if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", prompt)
        )
        assert not offenders, f"the agent's system prompt names tools the server does not register: {offenders}"

    def test_the_prompt_names_only_layers_and_kinds_the_contract_declares(self, scaffolded_project, monkeypatch):
        """The prompt the agent hands the model addresses only layers and kinds the shipped registry declares.

        An undeclared layer or kind in the prompt plans a failing call. Not reached: only a ``kind: <name>`` literal
        and a ``the `<name>` layer`` phrase are scanned, and a sentence naming only declared names can still state
        a rule wrongly.
        """
        registry = production_registry()
        layers = set(registry.layer_names)
        kinds = set(registry.kinds)
        assert "hardware" in layers and "resources" not in layers

        prompt = _system_prompt(scaffolded_project, monkeypatch)
        named_layers = set(re.findall(r"`([a-z_]+)` layer", prompt))
        assert named_layers, "the layer phrase this control scans for is gone; the assertion would be vacuous"
        assert named_layers <= layers, (
            "the agent's system prompt tells the agent to address layers the registry does not "
            f"declare: {', '.join(sorted(named_layers - layers))}"
        )

        named_kinds = set(re.findall(r"`kind: ([a-z_]+)`", prompt))
        assert named_kinds, "the kind literal this control scans for is gone; the assertion would be vacuous"
        assert named_kinds <= kinds, (
            "the agent's system prompt tells the agent to create element kinds the registry does not "
            f"declare: {', '.join(sorted(named_kinds - kinds))}"
        )
