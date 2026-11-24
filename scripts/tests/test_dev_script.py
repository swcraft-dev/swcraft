"""Tests for ``scripts/dev.py``, whose commands are *plans* checkable without running a build.

* the dispatch table and its usage output (an unknown command exits 2 and lists the commands);
* venv interpreter resolution on **both** platform layouts, and every command but ``install``
  failing with an actionable message when ``.venv`` is absent, never falling back to
  ``sys.executable``;
* the no-shell rule (AST-based): Windows PowerShell 5.x has no ``&&``;
* the network policy: exactly five commands may reach the network; any other command that
  installs passes ``--no-index``.

The ``@pytest.mark.artifact`` case really creates a venv and installs, so only
``dev.py test-artifacts`` runs it.
"""

from __future__ import annotations

import ast
import configparser
import glob
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from conftest import (
    REPO_ROOT,
    build_artifacts_module,
    dev_context,
    dev_module,
    load_repo_script,
    make_venv_marker,
    specimen_generator_module,
)
from swcraft_tools import __version__ as swcraft_version
from tracked_files_support import listed_files

_SCRIPT_PATH = REPO_ROOT / "scripts" / "dev.py"

_DEV = dev_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plannable_everywhere(repo_root: Path) -> None:
    """Make every command plan in *repo_root*: ``test-internal`` refuses a tree without ``internal/``."""
    (repo_root / _DEV.INTERNAL_DIR).mkdir(exist_ok=True)


class RecordingRunner:
    """Records every ``(argv, cwd)`` and returns a scripted exit code."""

    def __init__(self, fail_on=None, returncode: int = 3) -> None:
        self.fail_on = fail_on
        self.returncode = returncode
        self.calls: list[tuple[list[str], Path]] = []
        self.envs: list[tuple[tuple[str, str], ...]] = []

    def __call__(self, argv, cwd, stdin=None, env=()) -> int:
        argv = [str(part) for part in argv]
        self.calls.append((argv, cwd))
        self.envs.append(env)
        if self.fail_on is not None and self.fail_on(argv):
            return self.returncode
        return 0


def _write_dev_lock(repo_root: Path) -> None:
    """Materialize the lock ``main`` looks for, which a plan built by ``dev_context`` never reads."""
    lock = _DEV.dev_lock_path(repo_root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Dispatch table and usage
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_every_entry_has_a_summary_and_a_plan(self):
        assert _DEV.COMMANDS, "the dispatch table must not be empty"
        for name, command in _DEV.COMMANDS.items():
            assert command.summary.strip(), f"{name} has no summary to print in the usage line"
            assert callable(command.plan), f"{name} has no plan callable"

    def test_the_stage_1_commands_exist(self):
        assert {"install", "wheelhouse"} <= set(_DEV.COMMANDS)

    def test_no_subcommand_prints_usage_and_exits_2(self, capsys):
        assert _DEV.main([]) == 2
        stderr = capsys.readouterr().err
        assert "Usage: python scripts/dev.py" in stderr
        for name in _DEV.COMMANDS:
            assert name in stderr, f"{name} is missing from the usage output"

    def test_unknown_subcommand_names_it_and_exits_2(self, capsys):
        assert _DEV.main(["nope"]) == 2
        stderr = capsys.readouterr().err
        assert "Unknown command: nope" in stderr
        for name in _DEV.COMMANDS:
            assert name in stderr

    def test_unknown_subcommand_runs_nothing(self):
        runner = RecordingRunner()
        assert _DEV.main(["nope"], run=runner) == 2
        assert runner.calls == []

    def test_help_prints_usage_and_exits_0(self, capsys):
        assert _DEV.main(["--help"]) == 0
        assert "Commands:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Venv resolution
# ---------------------------------------------------------------------------


class TestVenvResolution:
    def test_posix_layout(self, tmp_path):
        assert _DEV.venv_python(tmp_path, "posix") == tmp_path / ".venv" / "bin" / "python"

    def test_windows_layout(self, tmp_path):
        assert _DEV.venv_python(tmp_path, "nt") == tmp_path / ".venv" / "Scripts" / "python.exe"

    def test_require_returns_the_interpreter_when_it_exists(self, tmp_path):
        interpreter = make_venv_marker(tmp_path)
        assert _DEV.require_venv_python(tmp_path) == interpreter

    def test_require_raises_an_actionable_error_when_the_venv_is_absent(self, tmp_path):
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.require_venv_python(tmp_path)
        message = str(excinfo.value)
        assert "scripts/dev.py install" in message
        assert str(tmp_path) in message

    def test_require_never_falls_back_to_the_running_interpreter(self, tmp_path):
        """A silent ``sys.executable`` fallback is the hazard the venv removes."""
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.require_venv_python(tmp_path)
        assert sys.executable not in str(excinfo.value)


class TestVenvRequiredByEveryCommandButInstall:
    @pytest.mark.parametrize("name", sorted(name for name, command in _DEV.COMMANDS.items() if command.needs_venv))
    def test_missing_venv_fails_before_anything_runs(self, tmp_path, capsys, name):
        runner = RecordingRunner()
        exit_code = _DEV.main(["--repo-root", str(tmp_path), name], run=runner)

        assert exit_code == 1
        assert runner.calls == [], f"{name} started running with no .venv present"
        stderr = capsys.readouterr().err
        assert "scripts/dev.py install" in stderr
        assert sys.executable not in stderr

    def test_install_itself_does_not_require_a_venv(self, tmp_path):
        _write_dev_lock(tmp_path)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), "--skip-node", "install"], run=runner) == 0
        assert runner.calls, "install ran no steps"

    def test_every_command_that_needs_a_venv_uses_that_interpreter(self, tmp_path):
        """The venv interpreter, not ``sys.executable``, drives every planned step."""
        _plannable_everywhere(tmp_path)
        expected = str(_DEV.venv_python(tmp_path, "posix"))
        for name, command in _DEV.COMMANDS.items():
            if not command.needs_venv:
                continue
            steps = command.plan(dev_context(tmp_path))
            pythons = [step.argv[0] for step in steps if "python" in Path(step.argv[0]).name]
            if not pythons:
                # `package` / `test-integration` run npm scripts but still need `install`
                # (it populates node_modules), so they keep `needs_venv`.
                assert all(Path(step.argv[0]).name.startswith("npm") for step in steps), (
                    f"{name} plans neither an interpreter nor an npm invocation"
                )
                continue
            assert set(pythons) == {expected}, f"{name} invokes an interpreter outside .venv: {pythons}"


# ---------------------------------------------------------------------------
# No-shell rule
# ---------------------------------------------------------------------------


class TestNoShellUsage:
    """Every runner script builds a list argv and runs it without a shell: PowerShell 5.x has no ``&&``."""

    def _tree(self) -> ast.Module:
        return ast.parse(_SCRIPT_PATH.read_text(encoding="utf-8"), filename=str(_SCRIPT_PATH))

    def test_no_call_passes_shell_true(self):
        offenders = [
            node.lineno
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "shell" and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
        ]
        assert offenders == [], f"shell=True at line(s) {offenders} in {_SCRIPT_PATH}"

    def test_no_shell_escape_hatches(self):
        forbidden = {"system", "popen", "getoutput", "getstatusoutput"}
        offenders = [
            f"{node.func.attr} (line {node.lineno})"
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in forbidden
        ]
        assert offenders == [], f"shell escape hatches in {_SCRIPT_PATH}: {offenders}"

    def test_no_step_argv_contains_a_shell_operator(self, tmp_path):
        _plannable_everywhere(tmp_path)
        operators = ("&&", "||", "|", ">", ";")
        for name, command in _DEV.COMMANDS.items():
            for step in command.plan(dev_context(tmp_path)):
                for part in step.argv:
                    assert not any(operator in part for operator in operators), (
                        f"{name} builds a shell operator into an argv part: {part!r}"
                    )


# ---------------------------------------------------------------------------
# Network policy
# ---------------------------------------------------------------------------


class TestExecutableResolution:
    """``resolve_npm``/``moon_available`` really consult PATH; every other test injects the value."""

    def test_a_resolvable_program_is_found_on_path(self, monkeypatch):
        monkeypatch.setattr(_DEV.shutil, "which", lambda program: f"/usr/bin/{program}")
        assert _DEV.resolve_npm() == "/usr/bin/npm"
        assert _DEV.moon_available() is True

    def test_an_unresolvable_program_is_none_rather_than_a_guess(self, monkeypatch):
        monkeypatch.setattr(_DEV.shutil, "which", lambda program: None)
        assert _DEV.resolve_npm() is None
        assert _DEV.moon_available() is False


class TestNetworkPolicy:
    def test_exactly_five_commands_are_declared_network_using(self):
        """``install`` and ``wheelhouse`` provision the toolchain; ``runtime`` its payloads; ``update-pins`` their pins.

        ``runtime`` is a command of its own so ``workstation-archive`` downloads nothing.
        ``update-pins`` and ``update-dev-lock`` install nothing but ask the publishers or the package
        index what to pin, so they are declared rather than left to read as offline.
        """
        assert _DEV.NETWORK_COMMANDS == ("install", "wheelhouse", "runtime", "update-pins", "update-dev-lock")
        declared = {name for name, command in _DEV.COMMANDS.items() if command.uses_network}
        assert declared == set(_DEV.NETWORK_COMMANDS)
        assert not _DEV.uses_network("workstation-archive")

    def test_the_network_commands_document_that_they_use_the_network(self):
        for name in _DEV.NETWORK_COMMANDS:
            summary = _DEV.COMMANDS[name].summary
            assert "network" in summary.lower(), f"{name}'s summary does not say it uses the network: {summary}"

    def test_no_offline_command_installs_without_no_index(self, tmp_path):
        """Assertions must resolve from local files; provisioning is the exception."""
        _plannable_everywhere(tmp_path)
        for name in _DEV.COMMANDS:
            if _DEV.uses_network(name):
                continue
            for step in _DEV.COMMANDS[name].plan(dev_context(tmp_path)):
                argv = list(step.argv)
                if "install" in argv and "pip" in argv:
                    assert "--no-index" in argv, f"{name} installs from an index: {argv}"

    def test_an_aggregate_inherits_its_stages_network_use(self):
        """`verify` declares nothing itself but runs `install`, `runtime` and `wheelhouse`, so it inherits."""
        assert _DEV.COMMANDS["verify"].uses_network is False
        assert _DEV.uses_network("verify") is True
        assert _DEV.uses_network("test") is False


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


class TestThePackagesLayout:
    """The four Python projects live under ``packages/``, the label of each its folder there."""

    #: The top-level folders the four projects lived in before they moved under ``packages/``.
    OLD_PROJECT_FOLDERS = frozenset({"tools", "mcp_server", "agent", "specimen_generator"})

    def test_the_project_labels_are_exactly_the_projects_under_packages(self):
        """Both directions: a label without a project, and a project folder no label installs."""
        folders = {
            parts[1]
            for parts in (name.split("/") for name in listed_files("packages", suffixes=("/pyproject.toml",)))
            if len(parts) == 3
        }
        assert folders == set(_DEV.PYTHON_PROJECTS)

    def test_no_listed_top_level_folder_carries_an_old_project_name(self):
        top_level = {name.split("/", 1)[0] for name in listed_files() if "/" in name}
        assert top_level.isdisjoint(self.OLD_PROJECT_FOLDERS), sorted(top_level & self.OLD_PROJECT_FOLDERS)


class TestInstallPlan:
    def test_creates_the_venv_first(self, tmp_path):
        first = _DEV.plan_install(dev_context(tmp_path))[0]
        assert first.argv == ("/usr/bin/python3", "-m", "venv", str(tmp_path / ".venv"))
        assert first.cwd == tmp_path

    def test_installs_the_four_projects_editable_into_the_venv(self, tmp_path):
        steps = _DEV.plan_install(dev_context(tmp_path))
        interpreter = str(_DEV.venv_python(tmp_path, "posix"))
        lock = str(_DEV.dev_lock_path(tmp_path))
        editable = [step for step in steps if "-e" in step.argv]

        assert [step.cwd for step in editable] == [
            tmp_path / _DEV.PACKAGES_DIR / project for project in _DEV.PYTHON_PROJECTS
        ]
        for step in editable:
            assert step.argv == (
                interpreter,
                "-m",
                "pip",
                "install",
                "--constraint",
                lock,
                "--build-constraint",
                lock,
                "-e",
                ".",
                "--group",
                "dev",
            )

    def test_runs_npm_ci_in_the_extension_project_before_the_last_step(self, tmp_path):
        npm = _DEV.plan_install(dev_context(tmp_path))[-1]
        assert npm.argv == ("/usr/local/bin/npm", "ci")
        assert npm.cwd == tmp_path / _DEV.VSCODE_PROJECT

    @pytest.mark.parametrize("skip_node", [False, True])
    def test_no_step_provisions_a_pinned_specimen_generator(self, tmp_path, skip_node):
        """No step provisions a pinned specimen generator.

        The specimen generator is ``packages/specimens/``, installed editable by the projects' loop: no
        fetch of a pinned wheel and no venv of its own follow.
        """
        steps = _DEV.plan_install(dev_context(tmp_path, skip_node=skip_node))
        assert all("provision" not in step.label for step in steps)
        assert all("provision_specimen_generator.py" not in " ".join(map(str, step.argv)) for step in steps)
        installs = [step.label for step in steps if step.label.startswith("install ")]
        assert "install specimens" in installs
        assert steps[-1].label == ("stage the elkjs payload" if skip_node else f"npm ci ({_DEV.VSCODE_PROJECT})")

    def test_skip_node_omits_the_npm_step(self, tmp_path):
        steps = _DEV.plan_install(dev_context(tmp_path, skip_node=True))
        assert all(Path(step.argv[0]).name != "npm" for step in steps)
        assert len(steps) == 4 + len(_DEV.PYTHON_PROJECTS)

    @pytest.mark.parametrize("skip_node", [False, True])
    def test_stages_the_elkjs_payload_after_the_editable_installs_with_or_without_node(self, tmp_path, skip_node):
        """The fetch needs no npm, so `--skip-node install` stages it too, before the Node half."""
        steps = _DEV.plan_install(dev_context(tmp_path, skip_node=skip_node))
        (index,) = [i for i, step in enumerate(steps) if "--elkjs" in step.argv]
        step = steps[index]
        assert step.argv == (
            str(_DEV.venv_python(tmp_path, "posix")),
            str(tmp_path / "scripts" / "build_artifacts.py"),
            "--repo-root",
            str(tmp_path),
            "--elkjs",
        )
        assert step.cwd == tmp_path
        assert index == 3 + len(_DEV.PYTHON_PROJECTS), "after the pin, the editable installs and `pip check`"
        assert len(steps) == index + (1 if skip_node else 2), "and before npm ci, the last step"

    def test_missing_npm_is_an_actionable_error(self, tmp_path):
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_install(dev_context(tmp_path, npm=None))
        message = str(excinfo.value)
        assert "npm" in message
        assert "--skip-node" in message

    def test_missing_npm_is_not_an_error_when_node_is_skipped(self, tmp_path):
        assert _DEV.plan_install(dev_context(tmp_path, npm=None, skip_node=True))


# ---------------------------------------------------------------------------
# A pre-existing venv from another interpreter
# ---------------------------------------------------------------------------


def _write_pyvenv_cfg(repo_root: Path, body: str) -> Path:
    config = _DEV.venv_dir(repo_root) / "pyvenv.cfg"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(body, encoding="utf-8")
    return config


class TestVenvInterpreterMismatch:
    """``python -m venv`` over a venv another Python created leaves it unusable: ``install`` refuses first."""

    def test_no_venv_reports_no_version(self, tmp_path):
        assert _DEV.venv_python_version(tmp_path) is None

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("home = /x\nversion = 3.11.13\n", "3.11"),
            ("version_info = 3.12.4.final.0\n", "3.12"),
            ("version = 3.14.6\nversion_info = 3.14.6.final.0\n", "3.14"),
        ],
    )
    def test_the_recorded_version_is_read_as_major_minor(self, tmp_path, body, expected):
        _write_pyvenv_cfg(tmp_path, body)
        assert _DEV.venv_python_version(tmp_path) == expected

    @pytest.mark.parametrize("body", ["", "home = /x\n", "version = nonsense\n", "version = 3\n"])
    def test_an_unreadable_config_is_no_answer_rather_than_a_mismatch(self, tmp_path, body):
        _write_pyvenv_cfg(tmp_path, body)
        assert _DEV.venv_python_version(tmp_path) is None
        # …and therefore never blocks install.
        assert _DEV.plan_install(dev_context(tmp_path))

    def test_a_matching_venv_is_planned_normally(self, tmp_path):
        _write_pyvenv_cfg(tmp_path, "version = 3.11.13\n")
        steps = _DEV.plan_install(dev_context(tmp_path, bootstrap_version="3.11"))
        assert steps[0].argv[1:3] == ("-m", "venv")

    def test_a_foreign_venv_is_an_actionable_error(self, tmp_path):
        _write_pyvenv_cfg(tmp_path, "version = 3.14.6\n")

        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_install(dev_context(tmp_path, bootstrap_version="3.11"))

        message = str(excinfo.value)
        assert "3.14" in message and "3.11" in message
        assert str(_DEV.venv_dir(tmp_path)) in message
        assert ".prototools" in message

    def test_a_foreign_venv_runs_nothing(self, tmp_path, capsys):
        """Through ``main``: the real ``Context``'s bootstrap version against a recorded one that cannot be it."""
        foreign = f"{sys.version_info.major}.{sys.version_info.minor + 1}"
        _write_pyvenv_cfg(tmp_path, f"version = {foreign}.0\n")
        runner = RecordingRunner()

        code = _DEV.main(["--repo-root", str(tmp_path), "--skip-node", "install"], run=runner)

        assert code == 1
        assert runner.calls == []
        assert f"was created by Python {foreign}" in capsys.readouterr().err


class TestInstallFromInsideTheVenv:
    """``install`` re-creates ``.venv``; it may not be driven by an interpreter in it.

    On Windows the step fails with a bare ``Permission denied`` (a running image is a locked
    file); on POSIX it silently rebuilds the environment from inside itself, so the refusal is
    unconditional. The version guard cannot catch it: one interpreter made the other.
    """

    def test_an_interpreter_outside_the_venv_plans_normally(self, tmp_path):
        """The control: the ordinary invocation must stay ordinary."""
        steps = _DEV.plan_install(dev_context(tmp_path, bootstrap_python="/usr/bin/python3"))
        assert steps[0].argv[1:3] == ("-m", "venv")

    @pytest.mark.parametrize("leaf", ["bin/python", "bin/python3", "Scripts/python.exe", "Scripts/python"])
    def test_any_interpreter_inside_the_venv_is_refused(self, tmp_path, leaf):
        """Every spelling, not only ``venv_python``'s: Windows venvs carry ``python`` and ``python.exe``."""
        inside = _DEV.venv_dir(tmp_path) / leaf

        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_install(dev_context(tmp_path, bootstrap_python=str(inside)))

        message = str(excinfo.value)
        assert str(_DEV.venv_dir(tmp_path)) in message
        assert "python scripts/dev.py" in message, "the message has to name the command that works"

    def test_the_refusal_survives_a_relative_spelling(self, tmp_path, monkeypatch):
        """With ``--repo-root .`` the two paths arrive differently spelled and are the same file."""
        monkeypatch.chdir(tmp_path)
        inside = Path(".venv") / "bin" / "python"

        with pytest.raises(_DEV.DevError):
            _DEV.plan_install(dev_context(Path("."), bootstrap_python=str(inside)))

    def test_an_interpreter_merely_sharing_a_prefix_is_not_refused(self, tmp_path):
        """A substring test would refuse ``.venv-old/bin/python`` too."""
        neighbour = tmp_path / ".venv-old" / "bin" / "python"

        assert _DEV.plan_install(dev_context(tmp_path, bootstrap_python=str(neighbour)))

    def test_only_install_is_refused(self, tmp_path):
        """``test`` and ``lint`` re-create nothing and run from the venv by design: the refusal does not reach them."""
        _write_pyvenv_cfg(tmp_path, "version = 3.11.13\n")
        (_DEV.venv_dir(tmp_path) / "bin").mkdir(parents=True, exist_ok=True)
        (_DEV.venv_dir(tmp_path) / "bin" / "python").write_text("", encoding="utf-8")
        inside = str(_DEV.venv_dir(tmp_path) / "bin" / "python")

        assert _DEV.plan_lint(dev_context(tmp_path, bootstrap_python=inside, skip_node=True))

    def test_verify_refuses_at_plan_time(self, tmp_path):
        """``verify`` opens with ``install``: refused on the *plan*, so nothing runs."""
        inside = str(_DEV.venv_dir(tmp_path) / "bin" / "python")

        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_verify(dev_context(tmp_path, bootstrap_python=inside, skip_node=True))

        assert "python scripts/dev.py" in str(excinfo.value)


# ---------------------------------------------------------------------------
# wheelhouse
# ---------------------------------------------------------------------------


class TestWheelhousePlan:
    def test_delegates_to_build_artifacts(self, tmp_path):
        steps = _DEV.plan_wheelhouse(dev_context(tmp_path))
        assert len(steps) == 1
        argv = list(steps[0].argv)
        assert argv[0] == str(_DEV.venv_python(tmp_path, "posix"))
        assert argv[1] == str(tmp_path / "scripts" / "build_artifacts.py")
        assert "--wheelhouse" in argv
        assert argv[argv.index("--repo-root") + 1] == str(tmp_path)

    def test_runs_from_the_repo_venv_interpreter(self, tmp_path):
        """A wheelhouse is only usable by the interpreter it was resolved for."""
        make_venv_marker(tmp_path)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), "wheelhouse"], run=runner) == 0
        argv, cwd = runner.calls[0]
        assert argv[0] == str(_DEV.venv_python(tmp_path))
        assert cwd == tmp_path


# ---------------------------------------------------------------------------
# test / lint
# ---------------------------------------------------------------------------


def _pytest_projects(steps) -> list[str]:
    """The Python project names, in order, of every pytest step in a plan."""
    return [step.cwd.name for step in steps if any("pytest" in str(part) for part in step.argv)]


def _marker_expression(step) -> str | None:
    """The ``-m <expr>`` marker expression of a pytest step, if it has one.

    Scans from the right: ``python -m pytest`` puts an unrelated ``-m`` first.
    """
    argv = list(step.argv)
    if "-m" not in argv:
        return None
    index = len(argv) - 1 - argv[::-1].index("-m")
    return argv[index + 1] if argv[index + 1] != "pytest" else None


class TestTestPlan:
    """The fast gate: every test project, then the extension's npm test."""

    def test_covers_every_python_project_in_dependency_order(self, tmp_path):
        """The Python projects in dependency order, then the test-only ones (``scripts``)."""
        steps = _DEV.plan_test(dev_context(tmp_path))
        assert _pytest_projects(steps) == [*_DEV.PYTHON_PROJECTS, "scripts"]

    def test_every_python_step_deselects_every_staged_marker(self, tmp_path):
        """Every Python step of the fast gate deselects every staged marker.

        A marker with no stage of its own would never run; a marker left selected here would drag a
        provisioning-heavy suite into the fast gate.
        """
        for step in _DEV.plan_test(dev_context(tmp_path)):
            if step.cwd.name == _DEV.VSCODE_PROJECT:
                continue
            expression = _marker_expression(step)
            assert expression is not None, f"{step.label} runs pytest with no marker deselect"
            for marker in _MARKER_STAGES:
                assert f"not {marker}" in expression, f"{step.label} does not deselect {marker}: {expression}"

    def test_ends_with_npm_run_test(self, tmp_path):
        last = _DEV.plan_test(dev_context(tmp_path))[-1]
        assert last.argv == ("/usr/local/bin/npm", "run", "test")
        assert last.cwd == tmp_path / _DEV.VSCODE_PROJECT

    def test_npm_test_is_the_script_that_cleans_and_compiles(self):
        """`npm run test` delegates to run-tests.mjs, which cleans and compiles.

        A bare `node --test` would grade whatever stale `out/` happened to be lying around.
        """
        scripts = json.loads((REPO_ROOT / _DEV.VSCODE_PROJECT / "package.json").read_text(encoding="utf-8"))["scripts"]
        assert scripts["test"] == "node scripts/run-tests.mjs"

    def test_the_fast_gate_does_not_tolerate_an_empty_collection(self, tmp_path):
        """Only the marker-only stages may collect nothing; here it is a failure."""
        for step in _DEV.plan_test(dev_context(tmp_path)):
            assert not any("pytest_allow_no_tests" in str(part) for part in step.argv), step.label

    def test_every_python_step_runs_under_xdist_with_group_scheduling(self, tmp_path):
        """``-n auto`` travels with ``--dist loadgroup`` on every Python step.

        ``-n auto`` alone would silently ignore an ``xdist_group`` mark: the mark is honoured only under
        ``--dist loadgroup``.
        """
        for step in _DEV.plan_test(dev_context(tmp_path)):
            if step.cwd.name == _DEV.VSCODE_PROJECT:
                continue
            argv = list(step.argv)
            assert argv[argv.index("-n") + 1] == "auto", step.label
            assert argv[argv.index("--dist") + 1] == "loadgroup", step.label

    def test_the_fast_gate_is_the_only_command_that_spawns_workers(self, tmp_path):
        """Only the fast gate spawns xdist workers; ``verify`` re-runs it.

        The marker-gated stages share ``dist/`` and real archives by design and ``benchmark`` measures
        time, so ``-n`` may reach no other plan. ``verify`` aggregates ``test`` and legitimately re-runs it.
        """
        _plannable_everywhere(tmp_path)
        spawning = {
            name
            for name, command in _DEV.COMMANDS.items()
            if not command.stages and any("-n" in step.argv for step in command.plan(dev_context(tmp_path)))
        }
        assert spawning == {"test"}

    def test_missing_npm_is_an_actionable_error(self, tmp_path):
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_test(dev_context(tmp_path, npm=None))
        assert "--skip-node" in str(excinfo.value)

    @pytest.mark.parametrize("skip_node", [False, True])
    def test_skip_node_reaches_the_projects_that_register_it(self, tmp_path, skip_node):
        """``--skip-node`` reaches the ``tools`` and ``scripts`` steps only.

        The diagram layout tests FAIL without Node unless the run said ``--skip-node``, an option of the
        ``tools`` conftest (which ``scripts`` loads); handing it to ``mcp`` or ``agent`` would be an
        argparse error.
        """
        steps = _DEV.plan_test(
            dev_context(tmp_path, npm=None if skip_node else "/usr/local/bin/npm", skip_node=skip_node)
        )
        carrying = [step.cwd.name for step in steps if "--skip-node" in step.argv]
        assert carrying == (["tools", "scripts"] if skip_node else [])


def _scripts_owned_markers() -> set[str]:
    """The marker stages ``dev.py`` gives the test-only project ``scripts``."""
    return {marker for marker, owners in _DEV.MARKER_STAGE_OWNERS.items() if "scripts" in owners}


def _scripts_ini() -> configparser.SectionProxy:
    """The ``[pytest]`` section of ``scripts/pytest.ini``."""
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "scripts" / "pytest.ini", encoding="utf-8")
    return parser["pytest"]


class TestTheScriptsTestProject:
    """``scripts``: the tests whose subject is a repository script, a test project that installs nothing.

    It runs in the fast gate and in the marker stages it owns, from its own folder, whose
    ``pytest.ini`` gives it the ``tools`` project's conftest. What this does not reach: whether a
    module under ``packages/*/tests`` has a script as its subject and should be here (the rule is
    applied by hand).
    """

    def test_it_is_a_test_only_project_and_not_an_installed_one(self):
        assert "scripts" in _DEV.TEST_ONLY_PROJECTS
        assert not set(_DEV.TEST_ONLY_PROJECTS) & set(_DEV.PYTHON_PROJECTS)
        root = _DEV.project_test_dir(REPO_ROOT, "scripts")
        assert root == REPO_ROOT / "scripts"
        assert (root / "pytest.ini").is_file()
        assert (root / "tests" / "conftest.py").is_file()
        assert not (root / "pyproject.toml").exists(), "a test-only project installs nothing"

    def test_the_fast_gate_runs_it_from_its_own_folder_with_the_same_deselect(self, tmp_path):
        steps = _DEV.plan_test(dev_context(tmp_path))
        (step,) = [step for step in steps if step.cwd == tmp_path / "scripts"]
        assert step.label == "pytest scripts"
        assert step.argv[1:4] == ("-m", "pytest", "tests/")
        assert _marker_expression(step) == _DEV.FAST_TEST_DESELECT

    def test_install_and_coverage_take_no_step_there(self, tmp_path):
        for plan in (_DEV.plan_install, _DEV.plan_coverage):
            steps = plan(dev_context(tmp_path))
            assert all(step.cwd.name != "scripts" for step in steps), plan.__name__
            reaching = [
                step.label
                for step in steps
                for part in step.argv
                if any(name in str(part).replace("\\", "/") for name in ("scripts/tests", "scripts/pytest.ini"))
            ]
            assert reaching == [], plan.__name__

    @pytest.mark.parametrize("marker", sorted(_DEV.MARKER_STAGE_OWNERS))
    def test_it_runs_a_marker_stage_exactly_when_it_owns_the_marker(self, tmp_path, marker):
        """A test-only project runs a marker stage exactly when it owns the marker.

        It never needs the no-tests tolerance, and an owned stage that collects nothing there fails.
        """
        _plannable_everywhere(tmp_path)
        steps = _DEV.COMMANDS[_MARKER_STAGES[marker]].plan(dev_context(tmp_path))
        mine = [step for step in steps if step.cwd == tmp_path / "scripts"]
        if "scripts" in _DEV.MARKER_STAGE_OWNERS[marker]:
            (step,) = mine
            assert _marker_expression(step) == marker
            assert _allow_no_tests_wrapper(tmp_path) not in step.argv
        else:
            assert mine == []

    def test_it_owns_the_markers_its_tests_carry(self):
        """The stage markers ``scripts/tests/`` carries are exactly the ones ``scripts`` owns.

        A stage marker is one a stage owns or the fast gate deselects (so a ``benchmark`` mark here fails
        too). Not reached: a mark built by ``getattr``.
        """
        deselected = {term.removeprefix("not ") for term in _DEV.FAST_TEST_DESELECT.split(" and ")}
        stage_markers = set(_DEV.MARKER_STAGE_OWNERS) | deselected
        carried = set()
        for path in sorted((REPO_ROOT / "scripts" / "tests").rglob("*.py"), key=lambda item: item.as_posix()):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            carried |= {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and node.attr in stage_markers
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
            }
        assert carried == _scripts_owned_markers()

    def test_its_ini_declares_the_stage_ceiling_and_the_plugin(self):
        """The same ceiling and plugin floor every project's ``[tool.pytest.ini_options]`` declares."""
        ini = _scripts_ini()
        assert int(ini["timeout"]) == _STAGE_TIMEOUT_CEILING
        assert ini["required_plugins"] == "pytest-timeout>=2.3"
        assert ini["pythonpath"].split() == ["../packages/tools/tests", "../packages/tools/tests/support"]

    def test_its_ini_registers_the_owned_markers_as_the_tools_project_does(self):
        """``scripts`` registers its owned markers as ``packages/tools/pyproject.toml`` defines them.

        Each but ``slow`` is defined exactly as there; its ``slow`` says what is marked here.
        """
        registered = dict(line.split(": ", 1) for line in _scripts_ini()["markers"].strip().splitlines())
        pyproject = tomllib.loads(
            (REPO_ROOT / _DEV.PACKAGES_DIR / "tools" / "pyproject.toml").read_text(encoding="utf-8")
        )
        tools = dict(line.split(": ", 1) for line in pyproject["tool"]["pytest"]["ini_options"]["markers"])
        owned = _scripts_owned_markers()
        assert set(registered) == owned
        assert {name: text for name, text in registered.items() if name != "slow"} == {
            name: tools[name] for name in owned - {"slow"}
        }


#: The ceiling every pytest invocation without a ``--timeout`` of its own takes, from the
#: three projects' ``[tool.pytest.ini_options]``.
_STAGE_TIMEOUT_CEILING = 1200

#: The one sleeping test the timeout witness runs, written into its own ``tmp_path``.
_SLEEPING_TEST = """\
import time


def test_sleeps_past_its_timeout():
    time.sleep(60)
"""


def _passes_a_timeout(part: object) -> bool:
    """An argv part that sets pytest-timeout's limit: the plugin's option or an ini override's value."""
    return str(part).startswith(("--timeout", "timeout="))


def _witness_environment() -> dict[str, str]:
    """The outer run's environment without what would leak the outer pytest into the inner one.

    ``PYTEST_ADDOPTS`` would add the outer run's options; xdist sets ``PYTEST_XDIST_*`` in
    every worker and pytest sets ``PYTEST_CURRENT_TEST``; pytest-cov's ``COV_CORE_*`` would
    start coverage in the inner run under ``dev.py coverage``.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if name != "PYTEST_ADDOPTS" and not name.startswith(("PYTEST_XDIST_", "PYTEST_CURRENT_TEST", "COV_CORE_"))
    }


class TestThePerTestTimeout:
    """Every pytest invocation carries a pytest-timeout limit.

    The fast gate on its argv, every other stage through the ini. A hung test once held the Windows
    fast gate for an hour.
    """

    def test_every_python_step_of_the_fast_gate_carries_its_timeout(self, tmp_path):
        """Every Python step of the fast gate carries ``--timeout=720``, the plugin's own option.

        A ``.venv`` without the plugin refuses it as an argparse error, where ``-o timeout=720`` would be
        accepted and time nothing.
        """
        assert _DEV.FAST_TEST_TIMEOUT == ("--timeout=720",)
        for step in _DEV.plan_test(dev_context(tmp_path)):
            if step.cwd.name == _DEV.VSCODE_PROJECT:
                continue
            assert [part for part in step.argv if _passes_a_timeout(part)] == ["--timeout=720"], step.label

    def test_the_fast_gate_is_the_only_command_that_passes_a_timeout(self, tmp_path):
        """Every other stage takes the ini ceiling, so the value has one copy for them.

        ``test-real-models`` turns the ceiling **off** (``--timeout=0``): the thread method writes a
        real export's captured output to the terminal. The command-line option, because an ini
        override would lose to an exported ``PYTEST_TIMEOUT``.
        """
        _plannable_everywhere(tmp_path)
        passing = {
            name
            for name, command in _DEV.COMMANDS.items()
            if not command.stages
            and any(_passes_a_timeout(part) for step in command.plan(dev_context(tmp_path)) for part in step.argv)
        }
        assert passing == {"test", "test-real-models"}
        for step in _DEV.plan_test_real_models(dev_context(tmp_path)):
            assert [part for part in step.argv if _passes_a_timeout(part)] == ["--timeout=0"], step.label

    @pytest.mark.parametrize("project", _DEV.PYTHON_PROJECTS)
    def test_each_project_declares_the_stage_ceiling_and_the_plugin(self, project):
        document = tomllib.loads(
            (REPO_ROOT / _DEV.PACKAGES_DIR / project / "pyproject.toml").read_text(encoding="utf-8")
        )
        ini_options = document["tool"]["pytest"]["ini_options"]
        assert ini_options["timeout"] == _STAGE_TIMEOUT_CEILING
        dev_group = document["dependency-groups"]["dev"]
        assert any(requirement.startswith("pytest-timeout") for requirement in dev_group), dev_group
        # Without the plugin the ``timeout`` key is only a warning, so the ini requires the
        # plugin at the ``dev`` group's floor -- the one requirement, spelled the same.
        floor = [requirement for requirement in dev_group if requirement.startswith("pytest-timeout")]
        assert ini_options["required_plugins"] == floor == ["pytest-timeout>=2.3"]
        assert not any(requirement.startswith("pytest-timeout") for requirement in document["project"]["dependencies"])

    def test_the_plugin_is_installed_in_this_environment(self):
        """pytest-timeout is installed in this environment.

        A ``.venv`` installed before this dependency was declared would refuse the fast gate's
        ``--timeout``; this names the cause first: re-run ``python scripts/dev.py install``.
        """
        import pytest_timeout  # noqa: F401, PLC0415 -- the import is the assertion

    @pytest.mark.parametrize(
        ("ini", "option"),
        [("[pytest]\n", ("--timeout=1",)), ("[pytest]\ntimeout = 1\n", ())],
        ids=["on-the-argv", "from-the-ini"],
    )
    def test_a_hung_test_is_a_named_failure(self, tmp_path, ini, option):
        """A hung test is a named failure: the run ends non-zero with its node id in the output.

        The witness: one sleeping test, ONE process, a one-second timeout. On POSIX the signal method
        fails the test as ``Timeout``; on Windows the thread method dumps every thread and ends the
        process.

        **No nested xdist**: a child under ``-n 2`` sometimes never returned after its last report,
        a shutdown hang inside pytest-xdist/execnet, not ours to root-cause; a stage's
        ``timeout-minutes`` bounds that. How a worker crash under xdist is named is not witnessed.

        Twice: on the argv, as the fast gate passes it, and from the ini key alone, which shows the
        key is the plugin's (a misspelled key would only be a pytest warning)."""
        (tmp_path / "pytest.ini").write_text(ini, encoding="utf-8", newline="\n")
        (tmp_path / "test_hang.py").write_text(_SLEEPING_TEST, encoding="utf-8", newline="\n")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "test_hang.py", "-v", "-p", "no:cacheprovider", *option],
            cwd=tmp_path,
            env=_witness_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )
        output = completed.stdout + completed.stderr
        assert completed.returncode != 0, output
        assert "test_hang.py::test_sleeps_past_its_timeout" in output, output
        assert "Timeout" in output, output

    def test_a_run_without_the_plugin_is_refused_rather_than_untimed(self, tmp_path):
        """A run without the plugin is refused, exit 4, before any test runs, rather than run untimed.

        A ``.venv`` without pytest-timeout reads ``timeout = 1200`` as an unknown key and runs untimed,
        so the ini lists the plugin in ``required_plugins``; the refusal is shown with ``-p no:timeout``.
        """
        ini = "[pytest]\nrequired_plugins = pytest-timeout>=2.3\ntimeout = 1200\n"
        (tmp_path / "pytest.ini").write_text(ini, encoding="utf-8", newline="\n")
        (tmp_path / "test_hang.py").write_text(_SLEEPING_TEST, encoding="utf-8", newline="\n")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "test_hang.py", "-p", "no:cacheprovider", "-p", "no:timeout"],
            cwd=tmp_path,
            env=_witness_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )
        output = completed.stdout + completed.stderr
        assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output
        assert "Missing required plugins: pytest-timeout>=2.3" in output, output

    def test_no_test_sets_its_own_timeout_mark(self):
        """No test raises the limit with ``@pytest.mark.timeout``: an AST scan of the projects' ``tests/``.

        Not reached: a mark built by ``getattr`` or a ``pytestmark`` assembled from a string, and a
        ``timeout`` override on an argv outside ``dev.py``."""
        offenders = []
        for project in _DEV.TEST_PROJECTS:
            for path in sorted(
                (_DEV.project_test_dir(REPO_ROOT, project) / "tests").rglob("*.py"), key=lambda item: item.as_posix()
            ):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                offenders.extend(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute)
                    and node.attr == "timeout"
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "mark"
                )
        assert offenders == []


class TestLintPlan:
    def test_covers_the_three_projects_plus_scripts(self, tmp_path):
        steps = _DEV.plan_lint(dev_context(tmp_path))
        runner = str(tmp_path / "scripts" / "run_python_lint.py")
        interpreter = str(_DEV.venv_python(tmp_path, "posix"))

        python_steps = [step for step in steps if runner in step.argv]
        assert [step.cwd for step in python_steps] == [
            *(tmp_path / _DEV.PACKAGES_DIR / project for project in _DEV.PYTHON_PROJECTS),
            tmp_path,
        ]
        for step in python_steps[:-1]:
            assert step.argv == (interpreter, runner)
        # repo-root scripts/ has no project of its own, so its mypy configuration is named.
        assert python_steps[-1].argv == (interpreter, runner, "--mypy-config", _DEV.SCRIPTS_MYPY_CONFIG, "scripts")

    def test_runs_the_import_graph_gate_in_tools_through_the_venv_interpreter(self, tmp_path):
        steps = [step for step in _DEV.plan_lint(dev_context(tmp_path)) if step.label == _DEV.IMPORT_LINT_LABEL]
        script = str(_DEV.venv_python(tmp_path, "posix").with_name("lint-imports"))
        assert len(steps) == 1
        assert steps[0].cwd == tmp_path / _DEV.PACKAGES_DIR / "tools"
        assert steps[0].argv == (script, "--no-cache")

    def test_the_import_graph_script_sits_beside_the_interpreter_on_both_platforms(self, tmp_path):
        assert _DEV.import_lint_executable(_DEV.venv_python(tmp_path, "nt")) == str(
            tmp_path / ".venv" / "Scripts" / "lint-imports.exe"
        )
        assert _DEV.import_lint_executable(_DEV.venv_python(tmp_path, "posix")) == str(
            tmp_path / ".venv" / "bin" / "lint-imports"
        )

    def test_a_versioned_interpreter_name_gives_the_script_no_suffix(self, tmp_path):
        assert _DEV.import_lint_executable(tmp_path / "bin" / "python3.11") == str(tmp_path / "bin" / "lint-imports")

    def test_runs_the_quality_ratchet_from_the_root_after_the_import_graph_and_before_biome(self, tmp_path):
        steps = _DEV.plan_lint(dev_context(tmp_path))
        labels = [step.label for step in steps]
        ratchet = steps[labels.index(_DEV.QUALITY_RATCHET_LABEL)]
        assert labels.count(_DEV.QUALITY_RATCHET_LABEL) == 1
        assert labels.index(_DEV.IMPORT_LINT_LABEL) < labels.index(_DEV.QUALITY_RATCHET_LABEL) < len(labels) - 1
        assert ratchet.cwd == tmp_path
        assert ratchet.argv == (
            str(_DEV.venv_python(tmp_path, "posix")),
            str(tmp_path / "scripts" / "quality_ratchet.py"),
            "check",
        )

    def test_ends_with_npm_run_lint(self, tmp_path):
        last = _DEV.plan_lint(dev_context(tmp_path))[-1]
        assert last.argv == ("/usr/local/bin/npm", "run", "lint")
        assert last.cwd == tmp_path / _DEV.VSCODE_PROJECT

    def test_skip_node_omits_the_biome_step(self, tmp_path):
        steps = _DEV.plan_lint(dev_context(tmp_path, npm=None, skip_node=True))
        assert all(Path(step.argv[0]).name != "npm" for step in steps)


_LINT = load_repo_script("run_python_lint", "scripts/run_python_lint.py")

#: The scripts `scripts/mypy-scripts.ini` may exempt by name: the list may only shrink, so a section outside it fails.
_SCRIPTS_MYPY_EXEMPT_CEILING = frozenset(
    {
        "agent_task_bench",
        "agent_task_bind",
        "agent_task_check",
        "build_artifacts",
        "exchange_output_oracle",
        "measure_forms",
        "quality_ratchet",
        "workstation.install",
    }
)


def _scripts_mypy_does_not_reach(root: Path, globs: list[str]) -> list[str]:
    """The scripts under *root*'s ``scripts/`` (their tests aside) that mypy's ``files`` *globs* do not name.

    mypy expands each ``files`` entry with :func:`glob.glob` (``recursive=True``) from the working directory,
    so ``*`` stops at a directory separator: a script one directory below a glob is not reached.
    """
    reached = {Path(match).as_posix() for entry in globs for match in glob.glob(entry, root_dir=root, recursive=True)}
    scripts = sorted(
        (
            path.relative_to(root).as_posix()
            for path in (root / "scripts").rglob("*.py")
            if "tests" not in path.relative_to(root / "scripts").parts
        ),
        key=str,
    )
    return [script for script in scripts if script not in reached]


class TestTheLintRunnersTypeCheck:
    """``run_python_lint.py`` adds mypy where ``pyproject.toml`` declares ``[tool.mypy]``: all four projects.

    The table is the whole configuration; the runner passes only the flags of a cold run.
    """

    def test_the_four_projects_declare_it_and_only_the_specimen_generator_is_strict(self):
        declaring = [
            project for project in _DEV.PYTHON_PROJECTS if _LINT.declares_mypy(REPO_ROOT / _DEV.PACKAGES_DIR / project)
        ]
        assert declaring == list(_DEV.PYTHON_PROJECTS)
        tables = {
            project: tomllib.loads(
                (REPO_ROOT / _DEV.PACKAGES_DIR / project / "pyproject.toml").read_text(encoding="utf-8")
            )
            for project in _DEV.PYTHON_PROJECTS
        }
        strict = [project for project, table in tables.items() if table["tool"]["mypy"].get("strict", False)]
        assert strict == ["specimens"]

    def test_a_directory_without_a_pyproject_declares_nothing(self, tmp_path):
        assert not _LINT.declares_mypy(tmp_path)

    @pytest.mark.parametrize(
        ("declared", "expected"), [(True, ["check", "format", "mypy"]), (False, ["check", "format"])]
    )
    def test_mypy_runs_after_ruff_only_where_declared(self, tmp_path, monkeypatch, declared, expected):
        if declared:
            (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n", encoding="utf-8", newline="\n")
        ran: list[list[str]] = []

        class Done:
            returncode = 0

        def run(argv):
            ran.append(argv)
            return Done()

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_LINT.subprocess, "run", run)
        assert _LINT.main(["."]) == 0
        assert [argv[3] if argv[2] == "ruff" else argv[2] for argv in ran] == expected
        if declared:
            *command, cache_flag, cache = ran[-1]
            assert command == [sys.executable, "-m", "mypy"]
            assert cache_flag == "--cache-dir"
            assert not Path(cache).exists()  # a fresh directory per run, removed after it

    def test_a_named_configuration_runs_mypy_with_it_and_ruff_on_the_rest(self, tmp_path, monkeypatch):
        ran: list[list[str]] = []

        class Done:
            returncode = 0

        def run(argv):
            ran.append(argv)
            return Done()

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_LINT.subprocess, "run", run)
        assert _LINT.main(["--mypy-config", "conf.ini", "scripts"]) == 0
        assert [argv[-1] for argv in ran[:2]] == ["scripts", "scripts"]
        assert ran[-1][2:5] == ["mypy", "--config-file", "conf.ini"]

    def test_the_scripts_configuration_names_only_existing_modules_and_no_hiding_key_but_their_exemption(self):
        config = configparser.ConfigParser()
        config.read(REPO_ROOT / _DEV.SCRIPTS_MYPY_CONFIG, encoding="utf-8")
        assert config["mypy"]["platform"] == "linux" and config["mypy"]["python_version"] == "3.11"
        exempt = [section.removeprefix("mypy-") for section in config.sections() if section != "mypy"]
        missing = [
            module for module in exempt if not (REPO_ROOT / "scripts" / f"{module.replace('.', '/')}.py").is_file()
        ]
        assert missing == []
        assert all(dict(config[f"mypy-{module}"]) == {"ignore_errors": "True"} for module in exempt)
        assert not {"ignore_errors", "disable_error_code", "exclude", "follow_imports"} & set(config["mypy"])
        assert set(exempt) <= _SCRIPTS_MYPY_EXEMPT_CEILING, sorted(set(exempt) - _SCRIPTS_MYPY_EXEMPT_CEILING)

    def test_the_scripts_configuration_reaches_every_script_but_the_tests(self):
        config = configparser.ConfigParser()
        config.read(REPO_ROOT / _DEV.SCRIPTS_MYPY_CONFIG, encoding="utf-8")
        globs = [entry.strip() for entry in config["mypy"]["files"].split(",")]
        assert _scripts_mypy_does_not_reach(REPO_ROOT, globs) == []

    def test_a_script_one_directory_below_a_glob_is_reported_unreached(self, tmp_path):
        for script in ("scripts/top.py", "scripts/workstation/install.py", "scripts/new/deeper.py"):
            (tmp_path / script).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / script).write_text("", encoding="utf-8", newline="\n")
        (tmp_path / "scripts/tests").mkdir()
        (tmp_path / "scripts/tests/test_top.py").write_text("", encoding="utf-8", newline="\n")
        unreached = _scripts_mypy_does_not_reach(tmp_path, ["scripts/*.py", "scripts/workstation/*.py"])
        assert unreached == ["scripts/new/deeper.py"]

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (["--mypy-config", "conf.ini", "scripts"], ("conf.ini", ["scripts"])),
            (["scripts", "--mypy-config", "conf.ini"], ("conf.ini", ["scripts"])),
            (["--mypy-config", "conf.ini"], ("conf.ini", ["."])),
            ([], (None, ["."])),
        ],
        ids=["leading", "trailing", "no-path", "nothing"],
    )
    def test_the_configuration_option_is_read_in_any_position(self, args, expected):
        assert _LINT.split_mypy_config(args) == expected

    def test_a_configuration_option_without_a_value_is_a_usage_error(self):
        with pytest.raises(SystemExit) as exit_info:
            _LINT.split_mypy_config(["scripts", "--mypy-config"])
        assert exit_info.value.code == 2

    def test_a_type_error_fails_the_lint(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n", encoding="utf-8", newline="\n")

        class Done:
            def __init__(self, returncode):
                self.returncode = returncode

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_LINT.subprocess, "run", lambda argv: Done(2 if "mypy" in argv else 0))
        assert _LINT.main(["."]) == 2


# ---------------------------------------------------------------------------
# The staged and on-demand commands
# ---------------------------------------------------------------------------


#: The commands that must stay *outside* the fast ``test`` gate, each with a
#: stage or an on-demand invocation of its own.
_STAGED_COMMANDS = (
    "test-artifacts",
    "coverage",
    "benchmark",
    "test-slow",
    "test-large-bench",
    "test-real-models",
    "test-integration",
    "test-model-integration",
)

#: Marker -> the one command that runs it. ``benchmark`` (report-only, ``tools`` only) has no
#: owning-project rule.
_MARKER_STAGES = {
    "artifact": "test-artifacts",
    "slow": "test-slow",
    "large_bench": "test-large-bench",
    "real_models": "test-real-models",
    "integration": "test-model-integration",
    "benchmark": "benchmark",
}

#: The deselected markers whose one command is **not** a ``verify`` stage: ``benchmark`` is
#: report-only, and ``real_models`` reads exports that exist on one machine alone.
_ON_DEMAND_MARKERS = frozenset({"benchmark", "real_models"})


def _fast_gate_deselects() -> set[str]:
    """The markers ``FAST_TEST_DESELECT`` names: ``"not a and not b"`` -> ``{"a", "b"}``."""
    terms = _DEV.FAST_TEST_DESELECT.split(" and ")
    assert all(term.startswith("not ") for term in terms), _DEV.FAST_TEST_DESELECT
    return {term.removeprefix("not ") for term in terms}


def _allow_no_tests_wrapper(repo_root: Path) -> str:
    """The ``pytest_allow_no_tests.py`` argv entry a tolerated step carries."""
    return str(repo_root / "scripts" / "pytest_allow_no_tests.py")


class TestStagedAndOnDemandCommands:
    @pytest.mark.parametrize("name", _STAGED_COMMANDS)
    def test_exists_as_a_separate_command(self, name):
        assert name in _DEV.COMMANDS

    @pytest.mark.parametrize("name", [*_STAGED_COMMANDS, "package"])
    def test_is_not_invoked_by_test(self, tmp_path, name):
        """`test` is the fast gate: none of these may be folded into it."""
        fast = [tuple(step.argv) for step in _DEV.plan_test(dev_context(tmp_path))]
        other = [tuple(step.argv) for step in _DEV.COMMANDS[name].plan(dev_context(tmp_path))]
        assert not (set(fast) & set(other)), f"{name}'s steps leak into the fast `test` gate"

    def test_the_stage_table_is_the_fast_gates_deselect(self):
        """`_MARKER_STAGES` names exactly the markers `FAST_TEST_DESELECT` deselects, so none is run by nothing."""
        assert set(_MARKER_STAGES) == _fast_gate_deselects()

    def test_every_deselected_marker_has_exactly_one_stage(self, tmp_path):
        """Each marker deselected from `test` has a stage of its own that selects it, and **no other** command does."""
        _plannable_everywhere(tmp_path)
        for marker, name in _MARKER_STAGES.items():
            steps = _DEV.COMMANDS[name].plan(dev_context(tmp_path))
            selecting = [step for step in steps if _marker_expression(step) == marker]
            assert selecting, f"no step in `{name}` selects -m {marker}"

            everywhere = {
                other
                for other in _DEV.COMMANDS
                # `verify` aggregates the stages, so it legitimately re-runs them.
                if not _DEV.COMMANDS[other].stages
                and any(_marker_expression(step) == marker for step in _DEV.COMMANDS[other].plan(dev_context(tmp_path)))
            }
            assert everywhere == {name}, f"-m {marker} is selected by {sorted(everywhere)}, not only by `{name}`"

    def test_the_owning_project_of_a_marker_stage_may_not_collect_nothing(self, tmp_path):
        """The no-tests tolerance is per project, never for the suite's owner.

        Otherwise dropping the last ``integration`` test would leave its stage running nothing and
        exiting 0.
        """
        wrapper = _allow_no_tests_wrapper(tmp_path)
        for marker, name in _MARKER_STAGES.items():
            if marker not in _DEV.MARKER_STAGE_OWNERS:  # `benchmark` owns no marker stage
                continue
            owners = _DEV.MARKER_STAGE_OWNERS[marker]
            assert owners, f"-m {marker} must name at least one owning project"
            for step in _DEV.COMMANDS[name].plan(dev_context(tmp_path)):
                owned = step.cwd.name in owners
                assert (wrapper in step.argv) is not owned, (
                    f"{step.label}: the owning project must fail on an empty collection, "
                    f"every other project must tolerate one"
                )


class TestTestArtifactsPlan:
    def test_runs_the_artifact_marker_across_the_three_projects(self, tmp_path):
        steps = _DEV.plan_test_artifacts(dev_context(tmp_path))
        assert [step.cwd for step in steps] == [
            *(tmp_path / _DEV.PACKAGES_DIR / project for project in _DEV.PYTHON_PROJECTS),
            tmp_path / "scripts",
        ]
        for step in steps:
            assert _marker_expression(step) == "artifact"

    def test_tolerates_a_project_with_no_artifact_tests(self, tmp_path):
        """`-m artifact` collects nothing in mcp and agent; the wrapper maps pytest's exit 5 to 0 there.

        ``tools`` and ``scripts`` own the suite and get no such tolerance.
        """
        wrapper = _allow_no_tests_wrapper(tmp_path)
        for step in _DEV.plan_test_artifacts(dev_context(tmp_path)):
            if step.cwd.name in ("tools", "scripts"):
                assert wrapper not in step.argv, step.label
            else:
                assert wrapper in step.argv, step.label

    def test_runs_no_npm_step(self, tmp_path):
        steps = _DEV.plan_test_artifacts(dev_context(tmp_path))
        assert all(Path(step.argv[0]).name != "npm" for step in steps)


class TestTestSlowPlan:
    def test_selects_the_slow_marker_across_the_python_projects(self, tmp_path):
        steps = _DEV.plan_test_slow(dev_context(tmp_path))
        assert [step.cwd for step in steps] == [
            *(tmp_path / _DEV.PACKAGES_DIR / project for project in _DEV.PYTHON_PROJECTS),
            tmp_path / "scripts",
        ]
        for step in steps:
            assert _marker_expression(step) == "slow"
            assert "-s" in step.argv, "the replay scenario's output must not be captured"
        # Every project owns slow tests, so none of them may be allowed to collect nothing.
        wrapper = _allow_no_tests_wrapper(tmp_path)
        tolerated = [step.cwd.name for step in steps if wrapper in step.argv]
        assert tolerated == []

    def test_it_takes_no_export_flag(self, tmp_path):
        """`test-slow` hands no export flag to any project, whatever the context carries.

        The real-export suites are closed and run under `test-internal`.
        """
        context = dev_context(tmp_path, require_rhapsody_export=True, require_second_export=True)
        for step in _DEV.plan_test_slow(context):
            assert "--require-rhapsody-export" not in step.argv
            assert "--require-second-export" not in step.argv
            assert _marker_expression(step) == "slow"


class TestTheExportFlags:
    """`--require-rhapsody-export` and `--require-second-export`, the closed real-export suites' flags.

    Both belong to `test-internal`, whose conftest registers them. One flag per variable: a host
    makes one gate mandatory without claiming that another ran.
    """

    #: Each flag with the context attribute it sets.
    _FLAGS = (
        ("--require-rhapsody-export", "require_rhapsody_export"),
        ("--require-second-export", "require_second_export"),
    )
    _FLAG_NAMES = tuple(flag for flag, _ in _FLAGS)

    @pytest.mark.parametrize("flag", _FLAG_NAMES)
    def test_the_flag_names_test_internal(self, flag):
        (row,) = [row for row in _DEV.EXPORT_FLAG_COMMANDS if row.flag == flag]
        assert row.command == _DEV.TEST_INTERNAL_COMMAND
        assert row.command in _DEV.COMMANDS

    def test_the_default_run_passes_no_require_flag(self, tmp_path):
        _plannable_everywhere(tmp_path)
        for step in _DEV.plan_test_internal(dev_context(tmp_path)):
            assert "--require-rhapsody-export" not in step.argv
            assert "--require-second-export" not in step.argv

    @pytest.mark.parametrize(("flag", "attribute"), _FLAGS)
    def test_the_flag_reaches_the_pytest_step_alone(self, tmp_path, flag, attribute):
        _plannable_everywhere(tmp_path)
        lint, pytest_step = _DEV.plan_test_internal(dev_context(tmp_path, **{attribute: True}))
        assert flag in pytest_step.argv
        assert flag not in lint.argv, "ruff knows no pytest option"

    def test_both_flags_travel_together_and_in_the_order_they_are_declared(self, tmp_path):
        """One stage, both variables mandatory: a host that holds both files asks for both."""
        _plannable_everywhere(tmp_path)
        context = dev_context(tmp_path, require_rhapsody_export=True, require_second_export=True)
        _, pytest_step = _DEV.plan_test_internal(context)
        argv = pytest_step.argv
        assert argv.index("--require-rhapsody-export") < argv.index("--require-second-export")

    @pytest.mark.parametrize("flag", _FLAG_NAMES)
    def test_the_option_is_registered_by_no_open_conftest(self, flag):
        """No open project registers the option, so no open stage could hand it over."""
        for project in _DEV.TEST_PROJECTS:
            conftest = _DEV.project_test_dir(REPO_ROOT, project) / "tests" / "conftest.py"
            assert flag not in conftest.read_text(encoding="utf-8"), project

    @pytest.mark.parametrize("flag", _FLAG_NAMES)
    def test_the_flag_is_refused_on_every_other_command(self, tmp_path, capsys, flag):
        """Refused rather than ignored: a `verify --require-rhapsody-export` would prove only what `verify` proves."""
        make_venv_marker(tmp_path)
        _plannable_everywhere(tmp_path)
        for command in ("verify", "test-slow"):
            runner = RecordingRunner()
            assert _DEV.main(["--repo-root", str(tmp_path), flag, command], run=runner) == 1
            assert runner.calls == [], "refused before the first subprocess"
            assert flag in capsys.readouterr().err

    @pytest.mark.parametrize("flag", _FLAG_NAMES)
    def test_the_flag_is_accepted_on_the_stage_it_names(self, tmp_path, flag):
        make_venv_marker(tmp_path)
        _plannable_everywhere(tmp_path)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), flag, _DEV.TEST_INTERNAL_COMMAND], run=runner) == 0
        (lint_argv, _), (pytest_argv, _) = runner.calls
        assert flag in pytest_argv and flag not in lint_argv

    def test_the_usage_line_names_both_flags(self):
        usage = _DEV.usage()
        assert "--require-rhapsody-export" in usage.splitlines()[0]
        assert "--require-second-export" in usage.splitlines()[0]
        assert usage.count("--require-second-export") >= 2, "the usage line and a stanza of its own"


class TestTheMaxfailFlag:
    """`--maxfail N`: the fast gate stops early for `quick.yml`, and no other command takes it."""

    def test_the_default_run_passes_no_limit(self, tmp_path):
        for step in _DEV.plan_test(dev_context(tmp_path)):
            assert not any("--maxfail" in part for part in step.argv), step.label

    def test_the_limit_reaches_every_pytest_step_and_not_npm(self, tmp_path):
        steps = _DEV.plan_test(dev_context(tmp_path, maxfail=5))
        carrying = [step for step in steps if "--maxfail=5" in step.argv]
        assert _pytest_projects(carrying) == list(_DEV.TEST_PROJECTS)
        assert [step for step in steps if step not in carrying] == [steps[-1]]
        assert steps[-1].argv == ("/usr/local/bin/npm", "run", "test")

    def test_the_limit_changes_nothing_else_about_the_plan(self, tmp_path):
        plain = _DEV.plan_test(dev_context(tmp_path))
        limited = _DEV.plan_test(dev_context(tmp_path, maxfail=5))
        assert [tuple(part for part in step.argv if part != "--maxfail=5") for step in limited] == [
            step.argv for step in plain
        ]

    @pytest.mark.parametrize("argv", [["test", "--maxfail", "5"], ["--maxfail", "5", "test"]])
    def test_the_flag_is_accepted_on_either_side_of_the_command(self, tmp_path, argv):
        make_venv_marker(tmp_path)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), "--skip-node", *argv], run=runner) == 0
        assert len(runner.calls) == len(_DEV.TEST_PROJECTS)
        assert all("--maxfail=5" in call[0] for call in runner.calls)

    @pytest.mark.parametrize("name", sorted(set(_DEV.COMMANDS) - {_DEV.MAXFAIL_COMMAND}))
    def test_the_flag_is_refused_on_every_other_command(self, tmp_path, capsys, name):
        make_venv_marker(tmp_path)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), "--maxfail", "5", name], run=runner) == 1
        assert runner.calls == [], "refused before the first subprocess"
        assert f"--maxfail applies to `test` and not to `{name}`" in capsys.readouterr().err

    @pytest.mark.parametrize("limit", ["0", "-1"])
    def test_a_limit_below_one_is_refused(self, tmp_path, capsys, limit):
        """pytest reads 0 as no limit: a run that asked for one must not get none."""
        make_venv_marker(tmp_path)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), f"--maxfail={limit}", "test"], run=runner) == 1
        assert runner.calls == []
        assert "--maxfail must be at least 1" in capsys.readouterr().err

    def test_a_limit_that_is_no_number_is_refused(self, tmp_path, capsys):
        runner = RecordingRunner()
        with pytest.raises(SystemExit) as refused:
            _DEV.main(["--repo-root", str(tmp_path), "--maxfail", "x", "test"], run=runner)
        assert runner.calls == []
        assert refused.value.code == 2
        assert "--maxfail" in capsys.readouterr().err

    def test_the_command_it_names_is_a_command(self):
        assert _DEV.MAXFAIL_COMMAND in _DEV.COMMANDS

    def test_the_usage_names_the_flag_and_its_command(self):
        usage = _DEV.usage()
        assert "--maxfail N" in usage.splitlines()[0]
        assert f"--maxfail N applies to `{_DEV.MAXFAIL_COMMAND}` alone" in usage


class TestTestLargeBenchPlan:
    """The large-bench tier's stage: the functional scenarios on the large bench."""

    def test_selects_the_large_bench_marker_across_the_three_projects(self, tmp_path):
        steps = _DEV.plan_test_large_bench(dev_context(tmp_path))
        assert [step.cwd for step in steps] == [
            tmp_path / _DEV.PACKAGES_DIR / project for project in _DEV.PYTHON_PROJECTS
        ]
        for step in steps:
            assert _marker_expression(step) == "large_bench"

    def test_mcp_server_owns_the_suite_and_the_others_may_collect_nothing(self, tmp_path):
        """The scenarios live in packages/mcp/tests/functional/ alone."""
        assert _DEV.MARKER_STAGE_OWNERS["large_bench"] == ("mcp",)
        wrapper = _allow_no_tests_wrapper(tmp_path)
        for step in _DEV.plan_test_large_bench(dev_context(tmp_path)):
            assert (wrapper in step.argv) is (step.cwd.name != "mcp"), step.label

    def test_runs_without_xdist_like_every_marker_stage(self, tmp_path):
        """The bench is generated once per session; workers would each generate it."""
        for step in _DEV.plan_test_large_bench(dev_context(tmp_path)):
            assert "-n" not in step.argv, step.label

    def test_the_usage_names_the_command_and_its_summary(self):
        usage = _DEV.usage()
        line = next(line for line in usage.splitlines() if line.strip().startswith("test-large-bench "))
        assert "`large_bench`" in line and "the only stage that does" in line


#: The real-export tier's pytest arguments, token for token.
_REAL_MODELS_ARGV = (
    "-m real_models -vv -rfEs -p no:cacheprovider -p no:warnings --capture=fd --show-capture=no --tb=no "
    "-o tmp_path_retention_policy=none --timeout=0"
).split()


class TestTestRealModelsPlan:
    """The real-export tier's stage: ``SWCRAFT_REAL_MODELS``, ``--require-real-models``, the argv."""

    @pytest.fixture(autouse=True)
    def _no_pytest_environment(self, monkeypatch):
        """The stage refuses an environment carrying pytest options; these cases plan it without one."""
        for name in (*_DEV.REAL_MODELS_REFUSED_ENVIRONMENT, _DEV.REAL_MODELS_VARIABLE):
            monkeypatch.delenv(name, raising=False)

    def test_runs_the_decided_argv_in_every_project(self, tmp_path):
        steps = _DEV.plan_test_real_models(dev_context(tmp_path))
        assert [step.cwd for step in steps] == [
            tmp_path / _DEV.PACKAGES_DIR / project for project in _DEV.PYTHON_PROJECTS
        ]
        for step in steps:
            argv = list(step.argv)
            assert argv[argv.index("tests/") + 1 :] == _REAL_MODELS_ARGV, step.label
            assert _marker_expression(step) == "real_models"

    def test_never_disables_capture(self, tmp_path):
        """``-s`` would put a real export's output on the terminal; nothing may ask for it."""
        for flagged in (False, True):
            for step in _DEV.plan_test_real_models(dev_context(tmp_path, require_real_models=flagged)):
                assert "-s" not in step.argv and "--capture=no" not in step.argv, step.label
                assert "--capture=fd" in step.argv, step.label

    @pytest.mark.parametrize(
        ("name", "setting"),
        [
            ("PYTEST_ADDOPTS", "-s --basetemp=zqsentinel"),
            ("PYTEST_PLUGINS", "zqsentinel_plugin"),
            ("PYTEST_DEBUG", "zqsentinel"),
            ("PYTEST_DEBUG_TEMPROOT", "/zqsentinel"),
        ],
    )
    def test_an_environment_carrying_pytest_options_is_refused_before_any_step(
        self, tmp_path, capsys, monkeypatch, name, setting
    ):
        """pytest reads ``PYTEST_ADDOPTS`` ahead of the argv, so ``-s`` there would undo it: refused."""
        make_venv_marker(tmp_path)
        monkeypatch.setenv(name, setting)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), _DEV.REQUIRE_REAL_MODELS_COMMAND], run=runner) == 1
        assert runner.calls == [], "refused before the first subprocess"
        error = capsys.readouterr().err
        assert f"Unset {name} and run" in error
        assert "zqsentinel" not in error, "the message names the variable, never its value"

    @pytest.mark.parametrize("setting", ["zqsentinel/exports", "./zqsentinel", "../zqsentinel"])
    def test_a_relative_directory_is_refused_before_any_step(self, tmp_path, capsys, monkeypatch, setting):
        """The stage's pytest runs in ``packages/mcp/``: a relative value would name another directory there."""
        make_venv_marker(tmp_path)
        monkeypatch.setenv(_DEV.REAL_MODELS_VARIABLE, setting)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), _DEV.REQUIRE_REAL_MODELS_COMMAND], run=runner) == 1
        assert runner.calls == [], "refused before the first subprocess"
        error = capsys.readouterr().err
        assert f"{_DEV.REAL_MODELS_VARIABLE} is a relative path" in error
        assert "zqsentinel" not in error, "the message names the variable, never its value"

    def test_an_absolute_or_unset_directory_plans(self, tmp_path, monkeypatch):
        make_venv_marker(tmp_path)
        for setting in (str(tmp_path / "exports"), ""):
            monkeypatch.setenv(_DEV.REAL_MODELS_VARIABLE, setting)
            runner = RecordingRunner()
            assert _DEV.main(["--repo-root", str(tmp_path), _DEV.REQUIRE_REAL_MODELS_COMMAND], run=runner) == 0
            assert len(runner.calls) == len(_DEV.PYTHON_PROJECTS)

    def test_the_relative_refusal_reads_the_context_alone(self, tmp_path):
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_test_real_models(dev_context(tmp_path, real_models_relative=True))
        assert "is a relative path" in str(excinfo.value)

    @pytest.mark.parametrize("setting", ["zqsentinel/specimen-generator", "./zqsentinel", "../zqsentinel/bin/gen"])
    def test_a_relative_specimen_generator_is_refused_before_any_step(self, tmp_path, capsys, monkeypatch, setting):
        """The specimen step runs in ``packages/mcp/``'s pytest: a relative value would name another file there."""
        make_venv_marker(tmp_path)
        monkeypatch.delenv(_DEV.REAL_MODELS_VARIABLE, raising=False)
        monkeypatch.setenv(_DEV.SPECIMEN_GENERATOR_VARIABLE, setting)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), _DEV.REQUIRE_REAL_MODELS_COMMAND], run=runner) == 1
        assert runner.calls == [], "refused before the first subprocess"
        error = capsys.readouterr().err
        assert f"{_DEV.SPECIMEN_GENERATOR_VARIABLE} is a relative path" in error
        assert "zqsentinel" not in error, "the message names the variable, never its value"

    def test_an_absolute_or_unset_specimen_generator_plans(self, tmp_path, monkeypatch):
        make_venv_marker(tmp_path)
        monkeypatch.delenv(_DEV.REAL_MODELS_VARIABLE, raising=False)
        for setting in (str(tmp_path / "bin" / "generator"), ""):
            monkeypatch.setenv(_DEV.SPECIMEN_GENERATOR_VARIABLE, setting)
            runner = RecordingRunner()
            assert _DEV.main(["--repo-root", str(tmp_path), _DEV.REQUIRE_REAL_MODELS_COMMAND], run=runner) == 0
            assert len(runner.calls) == len(_DEV.PYTHON_PROJECTS)

    def test_the_specimen_generator_refusal_reads_the_context_alone_after_the_real_models_one(self, tmp_path):
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_test_real_models(dev_context(tmp_path, specimen_generator_relative=True))
        assert f"{_DEV.SPECIMEN_GENERATOR_VARIABLE} is a relative path" in str(excinfo.value)
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_test_real_models(
                dev_context(tmp_path, real_models_relative=True, specimen_generator_relative=True)
            )
        assert f"{_DEV.REAL_MODELS_VARIABLE} is a relative path" in str(excinfo.value)

    def test_dev_py_names_the_specimen_generator_s_variable_as_the_invocation_module_does(self):
        """``dev.py`` is stdlib-only and loads no script, so it spells the name; held equal here."""
        assert specimen_generator_module().VARIABLE == _DEV.SPECIMEN_GENERATOR_VARIABLE

    def test_the_plan_refuses_from_the_context_alone(self, tmp_path):
        """The plan reads the variables ``build_context`` resolved, never the environment itself."""
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_test_real_models(dev_context(tmp_path, refused_pytest_environment=("PYTEST_DEBUG",)))
        assert "Unset PYTEST_DEBUG and run" in str(excinfo.value)

    def test_the_refused_set_is_the_decided_one(self):
        """The ``real_models`` stage refuses exactly the decided environment variables.

        ``PYTEST_DEBUG`` traces a failed test's exception text past capture; ``PYTEST_DEBUG_TEMPROOT``
        moves the basetemp. ``PYTEST_TIMEOUT`` is answered by the argv's ``--timeout=0`` instead.
        """
        assert _DEV.REAL_MODELS_REFUSED_ENVIRONMENT == (
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
            "PYTEST_DEBUG",
            "PYTEST_DEBUG_TEMPROOT",
        )
        assert "PYTEST_TIMEOUT" not in _DEV.REAL_MODELS_REFUSED_ENVIRONMENT
        assert "--timeout=0" in _DEV.REAL_MODELS_PYTEST_ARGS and "timeout=0" not in _DEV.REAL_MODELS_PYTEST_ARGS

    def test_an_exported_pytest_timeout_does_not_turn_the_timeout_back_on(self, tmp_path, monkeypatch):
        """pytest-timeout's own resolution: the command line's value is read before ``PYTEST_TIMEOUT``."""
        import pytest_timeout

        monkeypatch.setenv("PYTEST_TIMEOUT", "5")

        class _Config:
            def getvalue(self, name):
                return {"timeout": 0.0}.get(name)

            def getini(self, name):
                return {"timeout": "1200"}.get(name, "")

        assert pytest_timeout.get_env_settings(_Config()).timeout == 0

    def test_mcp_server_owns_the_suite_and_the_others_may_collect_nothing(self, tmp_path):
        assert _DEV.MARKER_STAGE_OWNERS["real_models"] == ("mcp",)
        wrapper = _allow_no_tests_wrapper(tmp_path)
        for step in _DEV.plan_test_real_models(dev_context(tmp_path)):
            assert (wrapper in step.argv) is (step.cwd.name != "mcp"), step.label

    def test_the_default_run_passes_no_require_flag(self, tmp_path):
        for step in _DEV.plan_test_real_models(dev_context(tmp_path)):
            assert "--require-real-models" not in step.argv

    def test_the_require_flag_reaches_mcp_server_and_only_mcp_server(self, tmp_path):
        """The option is registered by ``packages/mcp/tests/conftest.py`` alone, and comes last."""
        steps = _DEV.plan_test_real_models(dev_context(tmp_path, require_real_models=True))
        carrying = [step.cwd.name for step in steps if "--require-real-models" in step.argv]
        assert carrying == ["mcp"]
        (mcp_step,) = [step for step in steps if step.cwd.name == "mcp"]
        argv = list(mcp_step.argv)
        assert argv[argv.index("tests/") + 1 :] == [*_REAL_MODELS_ARGV, "--require-real-models"]

    def test_the_option_is_registered_where_the_flag_is_sent(self):
        """Non-vacuity for the row above: `mcp_server` really declares it, the others do not."""
        for project in _DEV.PYTHON_PROJECTS:
            conftest = REPO_ROOT / _DEV.PACKAGES_DIR / project / "tests" / "conftest.py"
            text = conftest.read_text(encoding="utf-8")
            declares = "real_models.FLAG" in text or "--require-real-models" in text
            assert declares is (project == "mcp"), project

    def test_the_export_flags_do_not_reach_it_and_it_does_not_reach_test_slow(self, tmp_path):
        """Each stage takes the rows naming it: a direct call carrying every flag splits them."""
        context = dev_context(
            tmp_path, require_rhapsody_export=True, require_second_export=True, require_real_models=True
        )
        for step in _DEV.plan_test_real_models(context):
            assert "--require-rhapsody-export" not in step.argv and "--require-second-export" not in step.argv
        for step in _DEV.plan_test_slow(context):
            assert "--require-real-models" not in step.argv, step.label

    def test_test_slows_argv_is_unchanged_by_the_third_row(self, tmp_path):
        plain = [step.argv for step in _DEV.plan_test_slow(dev_context(tmp_path))]
        flagged = [step.argv for step in _DEV.plan_test_slow(dev_context(tmp_path, require_real_models=True))]
        assert flagged == plain
        (tools_step,) = [step for step in _DEV.plan_test_slow(dev_context(tmp_path)) if step.cwd.name == "tools"]
        assert list(tools_step.argv[tools_step.argv.index("tests/") + 1 :]) == ["-v", "-s", "-m", "slow"]

    def test_the_flag_is_refused_on_every_other_command(self, tmp_path, capsys):
        make_venv_marker(tmp_path)
        for command in ("verify", "test-slow", "test"):
            runner = RecordingRunner()
            exit_code = _DEV.main(["--repo-root", str(tmp_path), "--require-real-models", command], run=runner)
            assert exit_code == 1, command
            assert runner.calls == [], "refused before the first subprocess"
            assert "--require-real-models" in capsys.readouterr().err

    def test_the_flag_is_accepted_on_the_stage_it_names(self, tmp_path):
        make_venv_marker(tmp_path)
        runner = RecordingRunner()
        argv = ["--repo-root", str(tmp_path), "--require-real-models", _DEV.REQUIRE_REAL_MODELS_COMMAND]
        assert _DEV.main(argv, run=runner) == 0
        carrying = [Path(call[1]).name for call in runner.calls if "--require-real-models" in call[0]]
        assert carrying == ["mcp"]

    def test_the_command_it_names_is_a_command_outside_verify(self):
        assert _DEV.REQUIRE_REAL_MODELS_COMMAND == "test-real-models"
        assert _DEV.REQUIRE_REAL_MODELS_COMMAND in _DEV.COMMANDS
        assert _DEV.REQUIRE_REAL_MODELS_COMMAND not in _DEV.VERIFY_STAGES

    def test_the_usage_names_the_flag_and_the_command(self):
        usage = _DEV.usage()
        assert "--require-real-models" in usage.splitlines()[0]
        assert usage.count("--require-real-models") >= 3, "the usage line, the summary and a stanza of its own"
        line = next(line for line in usage.splitlines() if line.strip().startswith("test-real-models "))
        assert "`real_models`" in line and "SWCRAFT_REAL_MODELS" in line and "not part of verify" in line
        # A calibration of the passport chain, never a gate of a release.
        assert "a calibration of the passport chain" in line and "report-only until" in line
        assert "not a bump step" in line


class TestTestModelIntegrationPlan:
    def test_selects_the_integration_marker_across_the_projects(self, tmp_path):
        steps = _DEV.plan_test_model_integration(dev_context(tmp_path))
        assert [step.cwd for step in steps] == [
            *(tmp_path / _DEV.PACKAGES_DIR / project for project in _DEV.PYTHON_PROJECTS),
            tmp_path / "scripts",
        ]
        for step in steps:
            assert _marker_expression(step) == "integration"

    @pytest.mark.parametrize("os_name", ["posix", "nt"])
    def test_the_specimen_generator_step_alone_names_the_venvs_swcraft(self, tmp_path, os_name):
        """The specimen generator's step alone names the venv's ``swcraft`` as ``SWCRAFT_BIN``.

        Its ``integration`` tests run the CLI ``SWCRAFT_BIN`` names and skip without it: the stage names
        the checkout's own ``swcraft`` beside its interpreter, on either platform's layout.
        """
        interpreter = _DEV.venv_python(tmp_path, os_name)
        steps = _DEV.plan_test_model_integration(dev_context(tmp_path, python=str(interpreter)))
        expected = interpreter.parent / ("swcraft.exe" if os_name == "nt" else "swcraft")
        envs = {step.cwd.name: step.env for step in steps}
        assert envs.pop("specimens") == (("SWCRAFT_BIN", str(expected)),)
        assert set(envs.values()) == {()}, "no other project reads the variable"

    def test_the_specimen_generator_owns_integration_and_slow(self):
        """The specimen generator owns the ``integration`` and ``slow`` stages.

        Its cases of those markers have these stages alone: an empty collection there must fail, never
        pass by running nothing.
        """
        assert "specimens" in _DEV.MARKER_STAGE_OWNERS["integration"]
        assert "specimens" in _DEV.MARKER_STAGE_OWNERS["slow"]

    def test_main_hands_the_variable_to_the_runner(self, tmp_path, monkeypatch):
        make_venv_marker(tmp_path)
        monkeypatch.setattr(_DEV, "moon_available", lambda: True)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), "test-model-integration"], run=runner) == 0
        by_project = {cwd.name: env for (_, cwd), env in zip(runner.calls, runner.envs, strict=True)}
        ((name, value),) = by_project["specimens"]
        assert name == "SWCRAFT_BIN" and Path(value).parent == _DEV.venv_python(tmp_path).parent

    def test_missing_moon_fails_and_never_skips(self, tmp_path):
        """The acceptance this stage carries must not be closable by a skip."""
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_test_model_integration(dev_context(tmp_path, has_moon=False))
        assert "moon" in str(excinfo.value)

    def test_missing_moon_runs_nothing_and_exits_non_zero(self, tmp_path, capsys, monkeypatch):
        make_venv_marker(tmp_path)
        monkeypatch.setattr(_DEV, "moon_available", lambda: False)
        runner = RecordingRunner()
        exit_code = _DEV.main(["--repo-root", str(tmp_path), "test-model-integration"], run=runner)
        assert exit_code == 1
        assert runner.calls == []
        assert "moon" in capsys.readouterr().err


class TestTestXsdPlan:
    """The XSD-conformance gate: what makes it a gate is the flag it passes.

    The suite skips in the fast gate (the licensed schemas are not redistributable); without
    ``--require-autosar-xsd`` this stage would pass having asserted nothing.
    """

    def test_it_runs_the_conformance_suite_with_the_require_flag(self, tmp_path):
        (steps,) = _DEV.plan_test_xsd(dev_context(tmp_path))
        assert steps.cwd == tmp_path / _DEV.PACKAGES_DIR / "tools"
        assert all(path in steps.argv for path in _DEV.XSD_CONFORMANCE_SUITE)
        assert "--require-autosar-xsd" in steps.argv

    def test_the_suite_is_the_conformance_module_and_the_oracle_map(self):
        """Two files by path: the conformance suite and the derivation of the oracle's XSD map."""
        assert _DEV.XSD_CONFORMANCE_SUITE == (
            "tests/test_arxml_xsd_conformance.py",
            "tests/test_io_oracle_xsd_map.py",
        )

    def test_the_suite_it_names_exists(self):
        for path in _DEV.XSD_CONFORMANCE_SUITE:
            assert (REPO_ROOT / "packages" / "tools" / path).is_file(), path

    def test_it_is_not_a_verify_stage(self):
        """``verify`` may not require a license-gated file that is not in the repo."""
        assert "test-xsd" not in _DEV.VERIFY_STAGES

    def test_the_fast_gate_does_not_deselect_it(self):
        """It is selected by path, not by a fifth marker — it still runs (and skips) in `test`."""
        assert "xsd" not in _DEV.FAST_TEST_DESELECT


class TestCoverageAndBenchmarkPlans:
    def test_coverage_targets_each_project_own_package(self, tmp_path):
        steps = _DEV.plan_coverage(dev_context(tmp_path))
        assert [step.cwd for step in steps] == [
            tmp_path / _DEV.PACKAGES_DIR / project for project in _DEV.PYTHON_PROJECTS
        ]
        for project, step in zip(_DEV.PYTHON_PROJECTS, steps, strict=True):
            assert f"--cov={_DEV.COVERAGE_PACKAGES[project]}" in step.argv

    def test_benchmark_routes_through_the_json_path_wrapper(self, tmp_path):
        steps = _DEV.plan_benchmark(dev_context(tmp_path))
        assert len(steps) == 1
        assert str(tmp_path / "scripts" / "run_benchmark.py") in steps[0].argv
        assert steps[0].cwd == tmp_path / _DEV.PACKAGES_DIR / "tools"


class TestNpmOnlyCommands:
    def test_package_runs_the_npm_package_script(self, tmp_path):
        steps = _DEV.plan_package(dev_context(tmp_path))
        assert [tuple(step.argv) for step in steps] == [("/usr/local/bin/npm", "run", "package")]
        assert steps[0].cwd == tmp_path / _DEV.VSCODE_PROJECT

    def test_test_integration_runs_the_npm_integration_script(self, tmp_path):
        steps = _DEV.plan_test_integration(dev_context(tmp_path))
        assert [tuple(step.argv) for step in steps] == [("/usr/local/bin/npm", "run", "test-integration")]

    def test_test_integration_is_documented_as_on_demand(self):
        summary = _DEV.COMMANDS["test-integration"].summary
        assert "on demand" in summary.lower()
        assert "display" in summary.lower()


class TestSchemasPlan:
    """``dev.py schemas`` -- the editor-schema generator, module-form steps."""

    def test_runs_the_generator_module_once_into_the_extension_schemas_dir(self, tmp_path):
        steps = _DEV.plan_schemas(dev_context(tmp_path))
        assert [tuple(step.argv) for step in steps] == [
            (
                str(_DEV.venv_python(tmp_path, "posix")),
                "-m",
                "swcraft_tools.editor_schema",
                "--output-dir",
                str(tmp_path / "vscode_extension" / "schemas"),
                "--package-json",
                str(tmp_path / "vscode_extension" / "package.json"),
            ),
            (
                str(_DEV.venv_python(tmp_path, "posix")),
                "-m",
                "swcraft_tools.editing.agent_schema",
                *(str(tmp_path / profile) for profile in _DEV.AGENT_PROFILES),
            ),
            (
                str(_DEV.venv_python(tmp_path, "posix")),
                "-m",
                "swcraft_tools.agents_md",
                "--profile",
                str(tmp_path / Path(_DEV.AGENTS_MD_PROFILE)),
                *(
                    word
                    for relative in _DEV.KIRO_STEERING_FILES
                    for word in ("--kiro-steering", str(tmp_path / Path(relative)))
                ),
                *(str(tmp_path / Path(relative)) for relative in _DEV.AGENTS_MD_FILES),
            ),
        ]
        assert steps[0].cwd == tmp_path
        assert steps[0].label

    def test_has_no_check_mode(self, tmp_path):
        """The drift gate is the comparison; the generator only ever writes."""
        for step in _DEV.plan_schemas(dev_context(tmp_path)):
            assert "--check" not in step.argv

    def test_is_registered_and_requires_the_venv(self):
        command = _DEV.COMMANDS["schemas"]
        assert command.plan is _DEV.plan_schemas
        assert command.needs_venv
        assert not command.uses_network
        assert _DEV.EDITOR_SCHEMAS_DIR in command.summary


class TestDocsPlan:
    """``dev.py docs`` -- the user-guide site build, one step delegated to ``build_artifacts.py``.

    The point is *where the build is defined*: an mkdocs argv here would build the guide a second
    way, unnoticed. Not reached: whether the rendered site is correct, or mkdocs installed; that is
    ``dev.py docs``'s own exit code under ``--strict``.
    """

    def test_delegates_to_build_artifacts_in_exactly_one_step(self, tmp_path):
        steps = _DEV.plan_docs(dev_context(tmp_path))
        assert len(steps) == 1
        argv = list(steps[0].argv)
        assert argv[1] == str(tmp_path / "scripts" / "build_artifacts.py")
        assert argv[argv.index("--repo-root") + 1] == str(tmp_path)
        assert "--docs-only" in argv
        assert "--wheelhouse" not in argv, "`docs` renders the guide, it does not provision"
        assert steps[0].cwd == tmp_path
        assert steps[0].label

    def test_argv_zero_is_the_venv_interpreter(self, tmp_path):
        """Never ``.venv/bin/mkdocs`` and never ``sys.executable``."""
        (step,) = _DEV.plan_docs(dev_context(tmp_path))
        assert step.argv[0] == str(_DEV.venv_python(tmp_path, "posix"))
        assert sys.executable not in step.argv

    def test_plans_no_mkdocs_invocation_of_its_own(self, tmp_path):
        """The one site-build definition lives in ``build_artifacts.py``.

        Matched per argv part: ``tmp_path`` carries the test's name, which contains ``mkdocs``.
        """
        for step in _DEV.plan_docs(dev_context(tmp_path)):
            assert "mkdocs" not in [str(part) for part in step.argv]
            assert not any(Path(str(part)).name.startswith("mkdocs") for part in step.argv)

    def test_is_registered_and_requires_the_venv(self):
        command = _DEV.COMMANDS["docs"]
        assert command.plan is _DEV.plan_docs
        assert command.needs_venv
        assert not command.uses_network
        assert _DEV.GUIDE_DIR in command.summary
        assert _DEV.DOCS_SITE_DIR in command.summary

    def test_the_quoted_paths_are_the_ones_the_build_actually_uses(self):
        """`dev.py`'s two path constants, which only the summary reads, equal the build's own definitions.

        Compared against `build_artifacts.docs_site_dir()` and `mkdocs.yml`'s `docs_dir`, not each other.
        """
        artifacts = build_artifacts_module()
        expected_site = artifacts.docs_site_dir(Path(artifacts.DIST_DIRNAME)).as_posix()
        assert _DEV.DOCS_SITE_DIR == expected_site

        config = yaml.safe_load((REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
        assert _DEV.GUIDE_DIR == config["docs_dir"]

    def test_a_missing_venv_is_reported_before_anything_runs(self, tmp_path, capsys):
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), "docs"], run=runner) == 1
        assert runner.calls == []
        stderr = capsys.readouterr().err
        assert "scripts/dev.py install" in stderr
        assert sys.executable not in stderr

    def test_needs_no_npm(self, tmp_path):
        """Unlike ``artifacts``, nothing here packages the .vsix."""
        steps = _DEV.plan_docs(dev_context(tmp_path, npm=None))
        assert len(steps) == 1

    def test_adds_no_verify_stage(self):
        """A developer command, not a gate stage -- no new stage and no new marker."""
        assert "docs" not in _DEV.VERIFY_STAGES


class TestDocsReferencePlan:
    """``dev.py docs-reference`` -- the guide reference generator, module-form steps.

    A list because the pages have two owning packages and ``swcraft_tools`` may not import
    ``swcraft_mcp``. Not reached: whether the written pages are right; the drift test compares them.
    """

    def test_runs_both_generator_modules_then_the_profiles_guide_index(self, tmp_path):
        """The guide index comes after both generators: it reads the guide as they left it.

        The project ``AGENTS.md`` comes last, rendered from the template profile the index step has
        just rewritten (``schemas`` ends with the same step, after the schema section).
        """
        steps = _DEV.plan_docs_reference(dev_context(tmp_path))
        assert [tuple(step.argv) for step in steps] == [
            (
                str(_DEV.venv_python(tmp_path, "posix")),
                "-m",
                "swcraft_tools.guide_reference",
                "--output-dir",
                str(tmp_path / "docs" / "guide"),
                "--mkdocs-yml",
                str(tmp_path / "mkdocs.yml"),
            ),
            (
                str(_DEV.venv_python(tmp_path, "posix")),
                "-m",
                "swcraft_mcp.tool_reference",
                "--output-dir",
                str(tmp_path / "docs" / "guide"),
            ),
            (
                str(_DEV.venv_python(tmp_path, "posix")),
                "-m",
                "swcraft_tools.guide_corpus",
                "--guide-dir",
                str(tmp_path / "docs" / "guide"),
                "--mkdocs-config",
                str(tmp_path / "mkdocs.yml"),
                "--profiles",
                *(str(tmp_path / Path(profile)) for profile in _DEV.GUIDE_INDEX_FILES),
            ),
            (
                str(_DEV.venv_python(tmp_path, "posix")),
                "-m",
                "swcraft_tools.agents_md",
                "--profile",
                str(tmp_path / Path(_DEV.AGENTS_MD_PROFILE)),
                *(
                    word
                    for relative in _DEV.KIRO_STEERING_FILES
                    for word in ("--kiro-steering", str(tmp_path / Path(relative)))
                ),
                *(str(tmp_path / Path(relative)) for relative in _DEV.AGENTS_MD_FILES),
            ),
        ]
        assert _DEV.GUIDE_INDEX_FILES == (
            *_DEV.AGENT_PROFILES,
            "packages/agent/src/swcraft_agent/agent-system-prompt.md",
        )
        assert all(step.cwd == tmp_path for step in steps)
        assert all(step.label for step in steps)

    def test_only_the_first_step_rewrites_the_navigation(self, tmp_path):
        """Two generators rewriting one ``nav`` block would fight over it on every run.

        The first rewrite keeps rows it does not own, so the tool page's row survives.
        """
        steps = _DEV.plan_docs_reference(dev_context(tmp_path))
        assert [("--mkdocs-yml" in step.argv) for step in steps] == [True, False, False, False]

    def test_argv_zero_is_the_venv_interpreter(self, tmp_path):
        for step in _DEV.plan_docs_reference(dev_context(tmp_path)):
            assert step.argv[0] == str(_DEV.venv_python(tmp_path, "posix"))
            assert sys.executable not in step.argv

    def test_has_no_check_mode(self, tmp_path):
        """The drift gate is the comparison; the generator only ever writes."""
        for step in _DEV.plan_docs_reference(dev_context(tmp_path)):
            assert "--check" not in step.argv

    def test_is_registered_and_requires_the_venv(self):
        command = _DEV.COMMANDS["docs-reference"]
        assert command.plan is _DEV.plan_docs_reference
        assert command.needs_venv
        assert not command.uses_network
        assert _DEV.GUIDE_REFERENCE_DIR in command.summary
        assert _DEV.MKDOCS_CONFIG in command.summary

    def test_needs_no_npm(self, tmp_path):
        """Nothing here packages the .vsix, so the plan holds on a machine with no Node."""
        assert len(_DEV.plan_docs_reference(dev_context(tmp_path, npm=None))) == 4

    def test_a_missing_venv_is_reported_before_anything_runs(self, tmp_path, capsys):
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), "docs-reference"], run=runner) == 1
        assert runner.calls == []
        stderr = capsys.readouterr().err
        assert "scripts/dev.py install" in stderr
        assert sys.executable not in stderr

    def test_adds_no_verify_stage(self):
        """A generator command, like `schemas`: the drift gate in `test` is what `verify` runs."""
        assert "docs-reference" not in _DEV.VERIFY_STAGES


class TestArtifactsPlan:
    """`artifacts` is the stage `verify` runs before `test-artifacts` consumes it."""

    def test_exists_as_a_separate_command(self):
        assert "artifacts" in _DEV.COMMANDS

    def test_delegates_to_build_artifacts_without_the_wheelhouse_flag(self, tmp_path):
        steps = _DEV.plan_artifacts(dev_context(tmp_path))
        assert len(steps) == 1
        argv = list(steps[0].argv)
        assert argv[0] == str(_DEV.venv_python(tmp_path, "posix"))
        assert argv[1] == str(tmp_path / "scripts" / "build_artifacts.py")
        assert argv[argv.index("--repo-root") + 1] == str(tmp_path)
        assert "--wheelhouse" not in argv, "`artifacts` builds the release artifacts, not the wheelhouse"
        assert steps[0].cwd == tmp_path

    def test_missing_npm_fails_at_plan_time(self, tmp_path):
        """The step it plans resolves npm as its first act -- see the case below."""
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_artifacts(dev_context(tmp_path, npm=None))
        assert "npm" in str(excinfo.value)

    def test_skip_node_is_refused_rather_than_planned(self, tmp_path):
        """There is no Python-only artifact set: dist/vscode/ is a packaged .vsix."""
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_artifacts(dev_context(tmp_path, skip_node=True))
        message = str(excinfo.value)
        assert "--skip-node" in message
        assert ".vsix" in message
        assert "verify" in message

    def test_is_not_one_of_the_commands_skip_node_covers(self):
        assert "artifacts" not in _DEV.SKIP_NODE_COMMANDS
        assert set(_DEV.SKIP_NODE_COMMANDS) == {"install", "lint", "test"}

    def test_the_step_it_plans_really_hard_fails_without_npm(self, tmp_path, monkeypatch):
        """Why the two refusals above exist: the real builder resolves npm before it runs anything.

        The fake runner never executes `build_artifacts.py`, so nothing else would notice a change.
        """
        build = build_artifacts_module()
        monkeypatch.setattr(build.shutil, "which", lambda _name: None)
        runner = RecordingRunner()
        with pytest.raises(build.BuildArtifactsError) as excinfo:
            build.build_all(tmp_path, run=runner)
        assert "npm" in str(excinfo.value)
        assert runner.calls == [], "build_all started building before it resolved npm"


def _write_workstation_outputs(repo_root: Path) -> Path:
    """What a successful `workstation-archive` stage leaves in `dist/workstation/`."""
    directory = repo_root / "dist" / "workstation"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "swcraft-9.9.9-macos-arm64.zip"
    archive.write_text("zip", encoding="utf-8", newline="\n")
    (directory / "SHA256SUMS").write_text(f"digest  {archive.name}\n", encoding="utf-8", newline="\n")
    return archive


def _write_runtime_outputs(repo_root: Path, node_name: str = "node.tar.gz") -> Path:
    """What a real `runtime` step leaves in `dist/runtime/`; returns the directory."""
    directory = _DEV.runtime_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    for name in (
        "RUNTIME.json",
        "python.tar.gz",
        "THIRD-PARTY-NOTICES.md",
        "redhat.vscode-yaml.vsix",
        node_name,
    ):
        (directory / name).write_bytes(b"payload")
    (directory / "python").mkdir(exist_ok=True)
    return directory


class TestUpdatePinsPlan:
    """`update-pins` rewrites the committed pins from the publishers."""

    def test_exists_as_a_network_using_command(self):
        assert "update-pins" in _DEV.COMMANDS
        assert _DEV.uses_network("update-pins")
        assert "network" in _DEV.COMMANDS["update-pins"].summary

    def test_delegates_to_build_artifacts_in_exactly_one_step(self, tmp_path):
        steps = _DEV.plan_update_pins(dev_context(tmp_path))
        assert len(steps) == 1
        argv = list(steps[0].argv)
        assert argv[0] == str(_DEV.venv_python(tmp_path, "posix"))
        assert argv[1] == str(tmp_path / "scripts" / "build_artifacts.py")
        assert "--update-pins" in argv
        assert steps[0].cwd == tmp_path

    def test_is_never_a_verify_stage(self):
        """A gate that rewrote a committed file would leave every checkout it ran in dirty."""
        assert "update-pins" not in _DEV.VERIFY_STAGES
        assert "not part of verify" in _DEV.COMMANDS["update-pins"].summary


class TestRuntimePlan:
    """`runtime` fetches and packs the pinned payloads the archive carries."""

    def test_exists_as_a_network_using_command(self):
        assert "runtime" in _DEV.COMMANDS
        assert _DEV.uses_network("runtime")
        assert "network" in _DEV.COMMANDS["runtime"].summary

    def test_delegates_to_build_artifacts_in_exactly_one_step(self, tmp_path):
        steps = _DEV.plan_runtime(dev_context(tmp_path))
        assert len(steps) == 1
        argv = list(steps[0].argv)
        assert argv[0] == str(_DEV.venv_python(tmp_path, "posix"))
        assert argv[1] == str(tmp_path / "scripts" / "build_artifacts.py")
        assert argv[argv.index("--repo-root") + 1] == str(tmp_path)
        assert "--runtime" in argv
        assert steps[0].cwd == tmp_path

    def test_needs_no_npm_and_is_planned_under_skip_node(self, tmp_path):
        """The stage only downloads files: neither an absent npm nor `--skip-node` refuses it; one step either way."""
        expected = _DEV.plan_runtime(dev_context(tmp_path))
        assert _DEV.plan_runtime(dev_context(tmp_path, npm=None)) == expected
        assert _DEV.plan_runtime(dev_context(tmp_path, skip_node=True)) == expected
        assert "likec4" not in _DEV.COMMANDS["runtime"].summary.lower()
        assert "likec4" not in (_DEV.plan_runtime.__doc__ or "").lower()

    def test_runs_from_the_repo_venv_interpreter(self, tmp_path):
        make_venv_marker(tmp_path)
        runner = RecordingRunner()

        def building(argv, cwd, stdin=None, env=()):
            code = runner(argv, cwd)
            _write_runtime_outputs(tmp_path)
            return code

        assert _DEV.main(["--repo-root", str(tmp_path), "runtime"], run=building) == 0
        argv, cwd = runner.calls[0]
        assert argv[0] == str(_DEV.venv_python(tmp_path))
        assert cwd == tmp_path

    def test_its_post_condition_reports_a_stage_that_produced_nothing(self, tmp_path):
        hook = _DEV.COMMANDS["runtime"].verify
        assert hook is not None
        with pytest.raises(_DEV.DevError) as excinfo:
            hook(dev_context(tmp_path))
        assert "dist/runtime" in str(excinfo.value).replace("\\", "/")

    def test_its_post_condition_accepts_the_payload_files_and_the_unpacked_interpreter(self, tmp_path):
        _write_runtime_outputs(tmp_path)

        _DEV.COMMANDS["runtime"].verify(dev_context(tmp_path))

    @pytest.mark.parametrize("missing", ["RUNTIME.json", "node.tar.gz", "python.tar.gz", "THIRD-PARTY-NOTICES.md"])
    def test_its_post_condition_names_a_missing_payload_file(self, tmp_path, missing):
        (_write_runtime_outputs(tmp_path) / missing).unlink()

        with pytest.raises(_DEV.DevError, match="Node payload" if missing.startswith("node") else missing):
            _DEV.COMMANDS["runtime"].verify(dev_context(tmp_path))

    def test_its_post_condition_names_a_payload_that_carries_no_companion_extension(self, tmp_path):
        (_write_runtime_outputs(tmp_path) / "redhat.vscode-yaml.vsix").unlink()

        with pytest.raises(_DEV.DevError, match=r"no companion extension \(\*\.vsix\)"):
            _DEV.COMMANDS["runtime"].verify(dev_context(tmp_path))

    def test_its_post_condition_names_a_missing_interpreter_tree(self, tmp_path):
        """`wheelhouse`, the next stage, resolves on `dist/runtime/python/`."""
        (_write_runtime_outputs(tmp_path) / "python").rmdir()

        with pytest.raises(_DEV.DevError, match="no interpreter to resolve for"):
            _DEV.COMMANDS["runtime"].verify(dev_context(tmp_path))

    def test_its_post_condition_reports_an_empty_payload_file(self, tmp_path):
        (_write_runtime_outputs(tmp_path) / "python.tar.gz").write_bytes(b"")

        with pytest.raises(_DEV.DevError, match="empty"):
            _DEV.COMMANDS["runtime"].verify(dev_context(tmp_path))

    def test_its_post_condition_reports_two_node_payloads(self, tmp_path):
        """A payload of another platform beside this one's is not one payload."""
        (_write_runtime_outputs(tmp_path) / "node.zip").write_bytes(b"payload")

        with pytest.raises(_DEV.DevError, match="2 Node payload"):
            _DEV.COMMANDS["runtime"].verify(dev_context(tmp_path))


class TestWorkstationArchivePlan:
    """`workstation-archive` composes what `artifacts` and `wheelhouse` produced."""

    def test_exists_as_a_separate_command(self):
        assert "workstation-archive" in _DEV.COMMANDS

    def test_delegates_to_build_artifacts_in_exactly_one_step(self, tmp_path):
        steps = _DEV.plan_workstation_archive(dev_context(tmp_path))
        assert len(steps) == 1
        argv = list(steps[0].argv)
        assert argv[0] == str(_DEV.venv_python(tmp_path, "posix"))
        assert argv[1] == str(tmp_path / "scripts" / "build_artifacts.py")
        assert argv[argv.index("--repo-root") + 1] == str(tmp_path)
        assert "--workstation-archive" in argv
        assert steps[0].cwd == tmp_path

    def test_runs_from_the_repo_venv_interpreter(self, tmp_path):
        """The archive carries the wheelhouse this interpreter resolved."""
        make_venv_marker(tmp_path)
        runner = RecordingRunner()

        def building(argv, cwd, stdin=None, env=()):
            # The command has a post-condition, so the fake step has to leave what a
            # real one would; `test_its_post_condition_*` below are what assert on it.
            code = runner(argv, cwd)
            _write_workstation_outputs(tmp_path)
            return code

        assert _DEV.main(["--repo-root", str(tmp_path), "workstation-archive"], run=building) == 0
        argv, cwd = runner.calls[0]
        assert argv[0] == str(_DEV.venv_python(tmp_path))
        assert cwd == tmp_path

    def test_downloads_nothing_of_its_own(self):
        """It composes; the one networked step before it is `wheelhouse`.

        The second assertion is the positive control: without it a `uses_network` answering `False`
        for everything would pass.
        """
        assert not _DEV.uses_network("workstation-archive")
        assert _DEV.uses_network("wheelhouse"), "the control: uses_network must answer True for something"

    def test_needs_no_npm(self, tmp_path):
        """The .vsix is already built: `artifacts` is the stage that packages it.

        Asserted as an equality between two plans, not a step count: the field must not be read at all.
        """
        without = _DEV.plan_workstation_archive(dev_context(tmp_path, npm=None))
        with_npm = _DEV.plan_workstation_archive(dev_context(tmp_path, npm="npm"))

        assert len(without) == 1
        assert [(step.argv, step.cwd) for step in without] == [(step.argv, step.cwd) for step in with_npm]

    def test_its_post_condition_reports_a_stage_that_produced_no_archive(self, tmp_path):
        hook = _DEV.COMMANDS["workstation-archive"].verify
        assert hook is not None
        with pytest.raises(_DEV.DevError) as excinfo:
            hook(dev_context(tmp_path))
        assert "dist/workstation" in str(excinfo.value).replace("\\", "/")

    def test_its_post_condition_accepts_an_archive_with_its_checksums(self, tmp_path):
        _write_workstation_outputs(tmp_path)

        _DEV.COMMANDS["workstation-archive"].verify(dev_context(tmp_path))

    def test_its_post_condition_reports_an_archive_with_no_checksums_beside_it(self, tmp_path):
        (_write_workstation_outputs(tmp_path).parent / "SHA256SUMS").unlink()

        with pytest.raises(_DEV.DevError, match="SHA256SUMS"):
            _DEV.COMMANDS["workstation-archive"].verify(dev_context(tmp_path))

    def test_its_post_condition_reports_a_truncated_archive(self, tmp_path):
        """A stage that exits 0 having written an empty file is not a stage that passed."""
        _write_workstation_outputs(tmp_path).write_text("", encoding="utf-8", newline="\n")

        with pytest.raises(_DEV.DevError, match="empty"):
            _DEV.COMMANDS["workstation-archive"].verify(dev_context(tmp_path))


# ---------------------------------------------------------------------------
# verify -- the aggregate gate
# ---------------------------------------------------------------------------


def _verify_steps(tmp_path: Path):
    return _DEV.plan_verify(dev_context(tmp_path))


class TestVerifyPlan:
    """`verify` is the one command that has to run every gate, in the right order.

    An *aggregate*: it owns no steps and concatenates other commands' plans, so what it runs reads
    as a tuple.
    """

    @pytest.fixture(autouse=True)
    def _toolchain_on_path(self, monkeypatch, tmp_path):
        """`verify` cannot be *planned* without moon or npm; pinned here, since the machine's PATH is not the subject.

        The real prerequisites are asserted in `TestTestModelIntegrationPlan` and `TestArtifactsPlan`.
        """
        monkeypatch.setattr(_DEV, "moon_available", lambda: True)
        monkeypatch.setattr(_DEV, "resolve_npm", lambda: "/usr/local/bin/npm")
        _write_dev_lock(tmp_path)

    def test_exists_and_is_an_aggregate_of_other_commands(self):
        command = _DEV.COMMANDS["verify"]
        assert command.stages, "`verify` must declare the commands it aggregates"
        for stage in command.stages:
            assert stage in _DEV.COMMANDS, f"`verify` names a stage that is not a command: {stage}"

    def test_runs_the_stages_in_the_documented_order(self):
        assert _DEV.COMMANDS["verify"].stages == (
            "install",
            "lint",
            "test",
            "build",
            "artifacts",
            "runtime",
            "wheelhouse",
            "workstation-archive",
            "test-artifacts",
            "test-slow",
            "test-large-bench",
            "test-model-integration",
        )

    def test_the_large_bench_stage_sits_between_the_slow_gate_and_the_integration_acceptance(self):
        """Adjacent on both sides, so a stage slipped in between cannot pass unnoticed."""
        stages = _DEV.COMMANDS["verify"].stages
        position = stages.index("test-large-bench")
        assert stages[position - 1] == "test-slow"
        assert stages[position + 1] == "test-model-integration"

    def test_the_runtime_payloads_sit_between_the_artifacts_and_the_wheelhouse(self):
        """`runtime` directly after `artifacts` and directly before `wheelhouse`.

        Before `wheelhouse`, which is downloaded *for* the interpreter the archive bundles; before
        `workstation-archive`, which composes the payloads. Asserted as adjacency, so a stage slipped
        in between cannot pass unnoticed.
        """
        stages = _DEV.COMMANDS["verify"].stages
        position = stages.index("runtime")
        assert stages[position - 1] == "artifacts"
        assert stages[position + 1] == "wheelhouse"
        assert position < stages.index("workstation-archive")

    def test_artifacts_and_wheelhouse_precede_the_artifact_suite(self):
        """The clean-install acceptance consumes what those two stages produce."""
        stages = _DEV.COMMANDS["verify"].stages
        assert stages.index("artifacts") < stages.index("test-artifacts")
        assert stages.index("wheelhouse") < stages.index("test-artifacts")

    def test_the_workstation_archive_sits_between_the_wheelhouse_and_the_artifact_suite(self):
        """The `artifact`-marked install test runs against the real archive."""
        stages = _DEV.COMMANDS["verify"].stages
        assert stages.index("wheelhouse") < stages.index("workstation-archive")
        assert stages.index("workstation-archive") < stages.index("test-artifacts")

    def test_the_wheelhouse_precedes_the_standalone_integration_acceptance(self):
        """The acceptance installs the workstation archive, which is composed from the wheelhouse."""
        stages = _DEV.COMMANDS["verify"].stages
        assert stages.index("wheelhouse") < stages.index("test-model-integration")

    def test_ends_with_the_mandatory_integration_acceptance(self):
        """The standalone integration acceptance is `verify`'s final stage."""
        assert _DEV.COMMANDS["verify"].stages[-1] == "test-model-integration"

    def test_includes_the_slow_gate_and_the_artifact_stage(self, tmp_path):
        """`verify` includes every marker stage, except the on-demand `benchmark` and `real_models`.

        Every marker deselected from `test` has its stage here; the two on-demand markers are held out
        explicitly.
        """
        gated = {marker: name for marker, name in _MARKER_STAGES.items() if marker not in _ON_DEMAND_MARKERS}
        stages = _DEV.COMMANDS["verify"].stages
        assert set(gated.values()) <= set(stages)
        assert not {_MARKER_STAGES[marker] for marker in _ON_DEMAND_MARKERS} & set(stages)

        selected = {
            _marker_expression(step) for step in _verify_steps(tmp_path) if _marker_expression(step) is not None
        }
        assert set(gated) <= selected

    def test_excludes_the_vscode_integration_suite(self, tmp_path):
        """`test-integration` downloads a VS Code build and needs a display."""
        assert "test-integration" not in _DEV.COMMANDS["verify"].stages
        for step in _verify_steps(tmp_path):
            assert tuple(step.argv[-2:]) != ("run", "test-integration"), step.label

    def test_excludes_the_report_only_commands(self):
        """`coverage` and `benchmark` stay on demand."""
        stages = set(_DEV.COMMANDS["verify"].stages)
        assert "coverage" not in stages
        assert "benchmark" not in stages

    def test_concatenates_the_stage_plans_in_order(self, tmp_path):
        expected: list[tuple[str, ...]] = []
        for stage in _DEV.COMMANDS["verify"].stages:
            expected.extend(tuple(step.argv) for step in _DEV.COMMANDS[stage].plan(dev_context(tmp_path)))
        assert [tuple(step.argv) for step in _verify_steps(tmp_path)] == expected

    def test_every_step_is_labelled_with_its_stage(self, tmp_path):
        """A failure message has to name the stage, not just the step inside it."""
        labelled = [step.label for step in _verify_steps(tmp_path)]
        for stage in _DEV.COMMANDS["verify"].stages:
            assert any(label.startswith(f"{stage}: ") for label in labelled), f"no step is attributed to {stage}"

    def test_does_not_require_a_pre_existing_venv(self, tmp_path):
        """`install` is `verify`'s first stage -- it *creates* the environment."""
        assert _DEV.COMMANDS["verify"].needs_venv is False
        runner = RecordingRunner()
        _DEV.main(["--repo-root", str(tmp_path), "verify"], run=runner)
        assert runner.calls, "verify refused to run without a pre-existing .venv"

    def test_skip_node_is_refused_before_the_first_subprocess(self, tmp_path, capsys):
        """The gate cannot run Python-only, and must say so before `install`, `lint`, `test` and `build` run."""
        runner = RecordingRunner()
        exit_code = _DEV.main(["--repo-root", str(tmp_path), "--skip-node", "verify"], run=runner)

        assert exit_code == 1
        assert runner.calls == [], "verify started running with a flag it cannot honour"
        stderr = capsys.readouterr().err
        assert "--skip-node" in stderr
        assert ".vsix" in stderr

    def test_missing_npm_is_refused_before_the_first_subprocess(self, tmp_path, capsys, monkeypatch):
        """The same fail-fast rule for the plain "no Node on this machine" case."""
        monkeypatch.setattr(_DEV, "resolve_npm", lambda: None)
        runner = RecordingRunner()
        exit_code = _DEV.main(["--repo-root", str(tmp_path), "verify"], run=runner)

        assert exit_code == 1
        assert runner.calls == [], "verify started running with no npm to package the .vsix"
        assert "npm" in capsys.readouterr().err

    def test_first_step_creates_the_venv(self, tmp_path):
        first = _verify_steps(tmp_path)[0]
        assert first.argv[1:3] == ("-m", "venv")

    def test_runs_the_build_post_condition(self, tmp_path):
        """`build`'s outputs check must not be lost by folding it into an aggregate."""
        hook = _DEV.COMMANDS["verify"].verify
        assert hook is not None
        with pytest.raises(_DEV.DevError) as excinfo:
            hook(dev_context(tmp_path))
        assert str(_DEV.reference_outputs(tmp_path)[0]) in str(excinfo.value)

    def test_stops_at_the_first_failing_stage_and_names_it(self, tmp_path, capsys):
        runner = RecordingRunner()

        def failing(argv, cwd, stdin=None, env=()):
            runner(argv, cwd)
            return 6 if any("run_python_lint.py" in str(part) for part in argv) else 0

        exit_code = _DEV.main(["--repo-root", str(tmp_path), "verify"], run=failing)

        assert exit_code == 6
        stderr = capsys.readouterr().err
        assert "lint: " in stderr, stderr

        steps = _DEV.plan_verify(dev_context(tmp_path))
        failed = next(index for index, step in enumerate(steps) if any("run_python_lint.py" in p for p in step.argv))
        # Keyed on (argv, cwd): the per-project steps share an argv and differ only
        # in their working directory.
        later = {(tuple(step.argv), step.cwd) for step in steps[failed + 1 :]}
        ran = {(tuple(argv), cwd) for argv, cwd in runner.calls}
        assert not (ran & later), "verify continued past the failing lint stage"

    def test_a_failing_stage_skips_the_post_conditions(self, tmp_path, capsys):
        """A build that never ran must not be reported as one that produced nothing."""
        runner = RecordingRunner(fail_on=lambda argv: "venv" in argv, returncode=9)
        assert _DEV.main(["--repo-root", str(tmp_path), "verify"], run=runner) == 9
        assert len(runner.calls) == 1
        assert "did not produce its declared outputs" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class TestRunSteps:
    def test_runs_every_step_in_order(self, tmp_path):
        runner = RecordingRunner()
        steps = [
            _DEV.Step(label="one", argv=("a",), cwd=tmp_path),
            _DEV.Step(label="two", argv=("b",), cwd=tmp_path),
        ]
        assert _DEV.run_steps(steps, run=runner) == 0
        assert [argv for argv, _ in runner.calls] == [["a"], ["b"]]

    def test_stops_at_the_first_failure_and_propagates_its_exit_code(self, tmp_path, capsys):
        runner = RecordingRunner(fail_on=lambda argv: argv == ["b"], returncode=7)
        steps = [
            _DEV.Step(label="one", argv=("a",), cwd=tmp_path),
            _DEV.Step(label="two", argv=("b",), cwd=tmp_path),
            _DEV.Step(label="three", argv=("c",), cwd=tmp_path),
        ]
        assert _DEV.run_steps(steps, run=runner) == 7
        assert [argv for argv, _ in runner.calls] == [["a"], ["b"]]
        assert "`two` failed (exit 7)" in capsys.readouterr().err

    def test_a_failing_project_stops_the_rest_of_the_sequence(self, tmp_path, capsys):
        """One project's failure must not be papered over by the next one passing."""
        make_venv_marker(tmp_path)
        # The project is the step's cwd rather than an argv part, so this keys on it.
        runner = RecordingRunner()

        def failing(argv, cwd, stdin=None, env=()):
            runner(argv, cwd)
            return 4 if cwd.name == "mcp" else 0

        exit_code = _DEV.main(["--repo-root", str(tmp_path), "--skip-node", "test"], run=failing)
        assert exit_code == 4
        assert [cwd.name for _, cwd in runner.calls] == ["tools", "mcp"]
        assert "pytest mcp" in capsys.readouterr().err

    def test_a_failing_step_stops_a_dispatched_command(self, tmp_path, capsys):
        _write_dev_lock(tmp_path)
        runner = RecordingRunner(fail_on=lambda argv: "venv" in argv, returncode=5)
        exit_code = _DEV.main(["--repo-root", str(tmp_path), "--skip-node", "install"], run=runner)
        assert exit_code == 5
        assert len(runner.calls) == 1
        assert "create .venv" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Artifact-marked: a real venv, a real editable install
# ---------------------------------------------------------------------------


def _copy_python_repo(tmp_path: Path) -> Path:
    """Copy the Python half of the checkout (no ``.venv``, no caches) into tmp."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.egg-info", ".venv", "dist", "views", ".benchmarks")
    for project in _DEV.PYTHON_PROJECTS:
        shutil.copytree(REPO_ROOT / _DEV.PACKAGES_DIR / project, repo_root / _DEV.PACKAGES_DIR / project, ignore=ignore)
    shutil.copytree(REPO_ROOT / "scripts", repo_root / "scripts", ignore=ignore)
    shutil.copytree(REPO_ROOT / "constraints", repo_root / "constraints")
    shutil.copy2(REPO_ROOT / "ruff.toml", repo_root / "ruff.toml")
    return repo_root


@pytest.mark.artifact
def test_install_creates_a_venv_that_imports_the_projects_from_that_copy(tmp_path):
    """``dev.py install`` really provisions a repo-local environment.

    ``--skip-node`` keeps the case to the venv and the editable installs. The imports are asked of
    the new interpreter itself.
    """
    repo_root = _copy_python_repo(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "dev.py"),
            "--repo-root",
            str(repo_root),
            "--skip-node",
            "install",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    interpreter = _DEV.venv_python(repo_root)
    assert interpreter.exists(), f"{interpreter} was not created"

    probe = subprocess.run(
        [
            str(interpreter),
            "-c",
            "import json, swcraft_agent, swcraft_mcp, swcraft_specimens, swcraft_tools;"
            "print(json.dumps({m.__name__: m.__file__ "
            "for m in (swcraft_tools, swcraft_mcp, swcraft_agent, swcraft_specimens)}))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr

    resolved = json.loads(probe.stdout)
    for module, path in resolved.items():
        assert Path(path).is_relative_to(repo_root), f"{module} resolved outside the copied repo: {path}"
        assert not Path(path).is_relative_to(REPO_ROOT), f"{module} resolved from the source checkout: {path}"

    # The console script the specimen machinery's `configured()` looks for beside `.venv`'s interpreter,
    # answering the product version: an import alone would pass with the `[project.scripts]` entry gone.
    project = tomllib.loads((repo_root / "packages" / "specimens" / "pyproject.toml").read_text(encoding="utf-8"))
    (script,) = project["project"]["scripts"]
    executable = interpreter.parent / (f"{script}.exe" if os.name == "nt" else script)
    assert executable.is_file(), f"{executable} was not installed"
    answered = subprocess.run([str(executable), "--version"], capture_output=True, text=True, encoding="utf-8")
    assert answered.returncode == 0, answered.stdout + answered.stderr
    assert answered.stdout == f"{script} {swcraft_version}\n"


class TestTestInternal:
    """``test-internal``: the work repository's internal tests, outside ``verify`` (the public tree lacks them)."""

    def test_the_stage_is_outside_verify(self):
        assert _DEV.TEST_INTERNAL_COMMAND in _DEV.COMMANDS
        assert _DEV.TEST_INTERNAL_COMMAND not in _DEV.VERIFY_STAGES
        assert not _DEV.uses_network(_DEV.TEST_INTERNAL_COMMAND)

    def test_it_fails_naming_the_work_repository_without_internal(self, tmp_path):
        with pytest.raises(_DEV.DevError) as refused:
            _DEV.plan_test_internal(dev_context(tmp_path))
        assert "internal/ is missing" in str(refused.value)
        assert "work repository" in str(refused.value)

    def test_it_fails_through_main_before_any_step(self, tmp_path, capsys):
        make_venv_marker(tmp_path)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), _DEV.TEST_INTERNAL_COMMAND], run=runner) == 1
        assert runner.calls == []
        assert "internal/ is missing" in capsys.readouterr().err

    def test_it_runs_ruff_first_then_pytest_serially_in_internal(self, tmp_path):
        _plannable_everywhere(tmp_path)
        lint, pytest_step = _DEV.plan_test_internal(dev_context(tmp_path))
        internal = tmp_path / _DEV.INTERNAL_DIR
        assert lint.cwd == internal and pytest_step.cwd == internal
        assert Path(lint.argv[1]).name == "run_python_lint.py" and lint.argv[2:] == (".",)
        assert pytest_step.argv[1:3] == ("-m", "pytest")
        assert "tests" in pytest_step.argv
        assert "-n" not in pytest_step.argv, "serial, like every stage but the fast gate"

    def test_it_passes_python_and_the_paths_through(self, tmp_path):
        _plannable_everywhere(tmp_path)
        make_venv_marker(tmp_path)
        interpreter = tmp_path / "other" / "python"
        interpreter.parent.mkdir()
        interpreter.write_text("", encoding="utf-8", newline="\n")
        target = tmp_path / _DEV.INTERNAL_DIR / "tests" / "test_x.py"
        runner = RecordingRunner()
        argv = ["--repo-root", str(tmp_path), _DEV.TEST_INTERNAL_COMMAND, "--python", str(interpreter), str(target)]
        assert _DEV.main(argv, run=runner) == 0
        (lint, lint_cwd), (pytest_argv, pytest_cwd) = runner.calls
        assert lint[0] == str(interpreter) and pytest_argv[0] == str(interpreter)
        assert lint_cwd == pytest_cwd == tmp_path / _DEV.INTERNAL_DIR
        assert str(target) in pytest_argv and "tests" not in pytest_argv

    def test_python_needs_no_venv_of_the_checkout(self, tmp_path):
        """A Gitea run installs ``<public>/.venv`` only; the checkout running the stage has none."""
        _plannable_everywhere(tmp_path)
        assert not (tmp_path / _DEV.VENV_DIRNAME).exists()
        interpreter = tmp_path / "public" / "python"
        interpreter.parent.mkdir()
        interpreter.write_text("", encoding="utf-8", newline="\n")
        runner = RecordingRunner()
        argv = ["--repo-root", str(tmp_path), _DEV.TEST_INTERNAL_COMMAND, "--python", str(interpreter)]
        assert _DEV.main(argv, run=runner) == 0
        assert [call[0][0] for call in runner.calls] == [str(interpreter)] * 2

    @pytest.mark.parametrize(
        "outside",
        [
            pytest.param("packages/tools/tests/test_x.py", id="another-project"),
            pytest.param("packages/tools/tests/test_x.py::TestX::test_y", id="a-node-id"),
            pytest.param("internal/../packages/tools/tests/test_x.py", id="dot-dot-out"),
            pytest.param("internals/tests/test_x.py", id="a-sibling-with-the-prefix"),
        ],
    )
    def test_a_path_outside_internal_is_refused_before_any_step(self, tmp_path, monkeypatch, capsys, outside):
        _plannable_everywhere(tmp_path)
        make_venv_marker(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = RecordingRunner()
        argv = ["--repo-root", str(tmp_path), _DEV.TEST_INTERNAL_COMMAND, outside]
        assert _DEV.main(argv, run=runner) == 1
        assert runner.calls == []
        assert f"{outside} is not under internal/" in capsys.readouterr().err

    def test_a_node_id_under_internal_is_accepted(self, tmp_path, monkeypatch):
        _plannable_everywhere(tmp_path)
        make_venv_marker(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = RecordingRunner()
        argv = ["--repo-root", str(tmp_path), _DEV.TEST_INTERNAL_COMMAND, "internal/tests/test_x.py::TestX"]
        assert _DEV.main(argv, run=runner) == 0

    def test_a_relative_path_is_made_absolute_against_the_caller(self, tmp_path, monkeypatch):
        _plannable_everywhere(tmp_path)
        make_venv_marker(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = RecordingRunner()
        argv = ["--repo-root", str(tmp_path), _DEV.TEST_INTERNAL_COMMAND, "internal/tests/test_x.py"]
        assert _DEV.main(argv, run=runner) == 0
        pytest_argv = runner.calls[1][0]
        assert str(Path(tmp_path, "internal", "tests", "test_x.py").absolute()) in pytest_argv

    def test_a_node_id_keeps_its_suffix_verbatim(self, tmp_path, monkeypatch):
        # A parameter id may hold a `/`: only the file part goes through Path, which on Windows would
        # turn that `/` into a backslash and leave pytest with no such test.
        _plannable_everywhere(tmp_path)
        make_venv_marker(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = RecordingRunner()
        suffix = "::test_a_path_is_open[tools/x.py]"
        argv = ["--repo-root", str(tmp_path), _DEV.TEST_INTERNAL_COMMAND, f"internal/tests/test_x.py{suffix}"]
        assert _DEV.main(argv, run=runner) == 0
        pytest_argv = runner.calls[1][0]
        assert str(Path(tmp_path, "internal", "tests", "test_x.py").absolute()) + suffix in pytest_argv

    def test_python_naming_no_file_is_refused(self, tmp_path, capsys):
        _plannable_everywhere(tmp_path)
        runner = RecordingRunner()
        argv = ["--repo-root", str(tmp_path), "--python", str(tmp_path / "none"), _DEV.TEST_INTERNAL_COMMAND]
        assert _DEV.main(argv, run=runner) == 1
        assert runner.calls == []
        assert "--python names" in capsys.readouterr().err

    @pytest.mark.parametrize("name", sorted(set(_DEV.COMMANDS) - {"test-internal"}))
    def test_python_and_paths_are_refused_on_every_other_command(self, tmp_path, capsys, name):
        make_venv_marker(tmp_path)
        runner = RecordingRunner()
        assert _DEV.main(["--repo-root", str(tmp_path), name, "extra"], run=runner) == 1
        assert f"a PATH argument applies to `test-internal` and not to `{name}`" in capsys.readouterr().err
        assert _DEV.main(["--repo-root", str(tmp_path), "--python", sys.executable, name], run=runner) == 1
        assert f"--python applies to `test-internal` and not to `{name}`" in capsys.readouterr().err
        assert runner.calls == [], "refused before the first subprocess"

    def test_the_usage_names_the_options_and_their_command(self):
        usage = _DEV.usage()
        assert "[--python EXE] <command> [PATH ...]" in usage.splitlines()[0]
        assert "--python EXE and PATH ... apply to `test-internal` alone" in usage
