"""The locked development environment: ``constraints/dev.txt`` and how ``dev.py`` applies it.

Two halves. The plans are pinned without running anything: ``Step.stdin`` reaching the runner,
what ``install`` carries, ``install --unlocked`` and ``update-dev-lock``. The lock's own content
is read from the committed file.

What this does not reach: whether pip honours the two options in a real install; and whether the
lock's versions work together, which a gate run on the installed environment answers. A missing
lock **fails** every content case rather than skipping it: an absent lock is an ``install`` that
refuses.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import REPO_ROOT, dev_context, dev_module, make_venv_marker

_DEV = dev_module()

_LOCK = REPO_ROOT / _DEV.DEV_LOCK
_REQUIRED_PINS = ("pip", "pytest", "pytest-xdist", "pytest-timeout", "pytest-benchmark", "hatchling")
_LOCK_OPTIONS = ("-c", "--constraint", "--build-constraint")


class _Runner:
    """Records every call the executor makes, with the fields a :class:`Step` adds."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, object]] = []
        self.envs: list[tuple[tuple[str, str], ...]] = []

    def __call__(self, argv, cwd, stdin=None, env=()) -> int:
        self.calls.append(([str(part) for part in argv], cwd, stdin))
        self.envs.append(env)
        return 0


def _pip_steps(steps):
    return [step for step in steps if step.argv[1:3] == ("-m", "pip")]


def _carries_the_lock(step) -> bool:
    return any(option in step.argv for option in _LOCK_OPTIONS)


def _write_lock(repo_root: Path) -> None:
    lock = _DEV.dev_lock_path(repo_root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8", newline="\n")


class TestTheStepFields:
    def test_a_steps_stdin_reaches_the_runner(self, tmp_path):
        runner = _Runner()
        step = _DEV.Step(label="one", argv=("a",), cwd=tmp_path, stdin="text\n")
        assert _DEV.run_steps([step], run=runner) == 0
        assert runner.calls == [(["a"], tmp_path, "text\n")]

    def test_a_plain_step_adds_nothing(self, tmp_path):
        runner = _Runner()
        assert _DEV.run_steps([_DEV.Step(label="one", argv=("a",), cwd=tmp_path)], run=runner) == 0
        assert runner.calls == [(["a"], tmp_path, None)]
        assert runner.envs == [()]

    def test_a_steps_variables_reach_the_runner(self, tmp_path):
        runner = _Runner()
        step = _DEV.Step(label="one", argv=("a",), cwd=tmp_path, env=(("SWCRAFT_BIN", "/x/swcraft"),))
        assert _DEV.run_steps([step], run=runner) == 0
        assert runner.envs == [(("SWCRAFT_BIN", "/x/swcraft"),)]

    def test_the_real_runner_sets_a_steps_variables_over_the_inherited_ones(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SWCRAFT_LOCK_TEST_INHERITED", "kept")
        monkeypatch.setenv("SWCRAFT_LOCK_TEST_SET", "inherited")
        answer = tmp_path / "answer.txt"
        script = (
            "import os, pathlib; "
            f"pathlib.Path({str(answer)!r}).write_text("
            "os.environ['SWCRAFT_LOCK_TEST_INHERITED'] + '/' + os.environ['SWCRAFT_LOCK_TEST_SET'], "
            "encoding='utf-8', newline='\\n')"
        )
        code = _DEV._run([sys.executable, "-c", script], tmp_path, None, (("SWCRAFT_LOCK_TEST_SET", "set"),))
        assert code == 0
        assert answer.read_text(encoding="utf-8") == "kept/set"
        assert os.environ["SWCRAFT_LOCK_TEST_SET"] == "inherited", "this process's environment is untouched"

    def test_the_real_runner_keeps_the_environment_and_feeds_stdin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SWCRAFT_LOCK_TEST_INHERITED", "kept")
        answer = tmp_path / "answer.txt"
        script = (
            "import os, pathlib, sys; "
            f"pathlib.Path({str(answer)!r}).write_text("
            "os.environ['SWCRAFT_LOCK_TEST_INHERITED'] + sys.stdin.read(), "
            "encoding='utf-8', newline='\\n'); "
            "sys.exit(7)"
        )
        code = _DEV._run([sys.executable, "-c", script], tmp_path, "fed")
        assert code == 7
        assert answer.read_text(encoding="utf-8") == "keptfed"

    def test_an_aggregate_keeps_the_lock(self, tmp_path):
        """`plan_stages` rewrites every label: `verify`'s `install` must keep the lock."""
        verify = _pip_steps(_DEV.plan_verify(dev_context(tmp_path)))
        install = _pip_steps(_DEV.plan_install(dev_context(tmp_path)))
        assert [step.argv for step in verify[: len(install)]] == [step.argv for step in install]
        assert _carries_the_lock(verify[1])


class TestTheLockedInstall:
    def test_the_first_pip_step_installs_the_locked_pip(self, tmp_path):
        first = _pip_steps(_DEV.plan_install(dev_context(tmp_path)))[0]
        assert first.argv[3:] == ("install", "--constraint", str(tmp_path / "constraints" / "dev.txt"), "pip")
        assert first == _DEV.plan_install(dev_context(tmp_path))[1], "straight after the venv"

    def test_every_later_pip_install_carries_both_options_with_the_absolute_path(self, tmp_path):
        later = _pip_steps(_DEV.plan_install(dev_context(tmp_path)))[1:-1]
        assert len(later) == len(_DEV.PYTHON_PROJECTS)
        lock = str(tmp_path / "constraints" / "dev.txt")
        for step in later:
            assert step.argv[3:] == (
                "install",
                "--constraint",
                lock,
                "--build-constraint",
                lock,
                "-e",
                ".",
                "--group",
                "dev",
            ), step.label

    def test_pip_check_takes_neither_option(self, tmp_path):
        """`pip check` has no such option: passing the lock to it would fail the step."""
        assert not _carries_the_lock(_pip_steps(_DEV.plan_install(dev_context(tmp_path)))[-1])

    def test_a_space_in_the_repository_path_stays_inside_one_argument(self, tmp_path):
        """The reason the lock is argv: pip splits a multi-value variable on whitespace."""
        repo_root = tmp_path / "a checkout with spaces"
        repo_root.mkdir()
        lock = str(repo_root / "constraints" / "dev.txt")
        (pin, *installs) = _pip_steps(_DEV.plan_install(dev_context(repo_root)))[:-1]
        assert pin.argv[pin.argv.index("--constraint") + 1] == lock
        assert len(installs) == len(_DEV.PYTHON_PROJECTS)
        for step in installs:
            assert step.argv[step.argv.index("--constraint") + 1] == lock, step.label
            assert step.argv[step.argv.index("--build-constraint") + 1] == lock, step.label

    def test_pip_check_is_the_last_pip_step_and_nothing_pip_follows(self, tmp_path):
        assert _pip_steps(_DEV.plan_install(dev_context(tmp_path)))[-1].argv[3:] == ("check",)

    def test_no_step_outside_pip_carries_the_lock(self, tmp_path):
        steps = _DEV.plan_install(dev_context(tmp_path))
        pip = _pip_steps(steps)
        assert not [step.label for step in steps if step not in pip and _carries_the_lock(step)]

    def test_a_missing_lock_is_a_failure_with_a_sentence(self, tmp_path):
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_install(dev_context(tmp_path, dev_lock_missing=True))
        assert "constraints/dev.txt is missing" in str(excinfo.value)
        assert "update-dev-lock" in str(excinfo.value)

    def test_main_reads_the_lock_from_the_checkout(self, tmp_path, capsys):
        runner = _Runner()
        assert _DEV.main(["--repo-root", str(tmp_path), "--skip-node", "install"], run=runner) == 1
        assert runner.calls == [], "refused before the first subprocess"
        assert "constraints/dev.txt is missing" in capsys.readouterr().err

        _write_lock(tmp_path)
        assert _DEV.main(["--repo-root", str(tmp_path), "--skip-node", "install"], run=runner) == 0
        assert runner.calls


class TestTheUnlockedInstall:
    def test_it_omits_the_pin_and_both_options(self, tmp_path):
        steps = _DEV.plan_install(dev_context(tmp_path, unlocked=True))
        assert not [step.label for step in steps if _carries_the_lock(step)]
        assert "install the locked pip" not in [step.label for step in steps]

    def test_it_upgrades_pip_before_the_first_project(self, tmp_path):
        """The bundled pip may predate ``--group``: the canary's first pip step takes the newest pip."""
        pip = _pip_steps(_DEV.plan_install(dev_context(tmp_path, unlocked=True)))
        assert pip[0].argv[3:] == ("install", "--upgrade", "pip")
        assert pip[1].argv[3:] == ("install", "-e", ".", "--group", "dev")
        assert pip[1].cwd == tmp_path / _DEV.PACKAGES_DIR / _DEV.PYTHON_PROJECTS[0]

    def test_it_keeps_pip_check_and_prints_pip_freeze(self, tmp_path):
        pip = _pip_steps(_DEV.plan_install(dev_context(tmp_path, unlocked=True)))
        assert [step.argv[3:] for step in pip[-2:]] == [("check",), ("freeze",)]

    def test_it_needs_no_lock(self, tmp_path):
        assert _DEV.plan_install(dev_context(tmp_path, unlocked=True, dev_lock_missing=True))

    def test_it_refuses_an_existing_venv(self, tmp_path, capsys):
        make_venv_marker(tmp_path)
        runner = _Runner()
        code = _DEV.main(["--repo-root", str(tmp_path), "--skip-node", "--unlocked", "install"], run=runner)
        assert code == 1
        assert runner.calls == []
        assert "--unlocked refuses an existing .venv" in capsys.readouterr().err

    def test_it_runs_on_a_fresh_checkout_with_the_flag_on_either_side(self, tmp_path):
        for argv in (["--unlocked", "install"], ["install", "--unlocked"]):
            runner = _Runner()
            assert _DEV.main(["--repo-root", str(tmp_path), "--skip-node", *argv], run=runner) == 0
            assert not any(option in call[0] for call in runner.calls for option in _LOCK_OPTIONS)

    @pytest.mark.parametrize("name", sorted(set(_DEV.COMMANDS) - {_DEV.UNLOCKED_COMMAND}))
    def test_it_is_refused_on_every_other_command(self, tmp_path, capsys, name):
        make_venv_marker(tmp_path)
        runner = _Runner()
        assert _DEV.main(["--repo-root", str(tmp_path), "--unlocked", name], run=runner) == 1
        assert runner.calls == [], "refused before the first subprocess"
        assert f"--unlocked applies to `install` and not to `{name}`" in capsys.readouterr().err

    def test_the_usage_names_the_flag_and_its_command(self):
        usage = _DEV.usage()
        assert "[--unlocked]" in usage.splitlines()[0]
        assert f"--unlocked applies to `{_DEV.UNLOCKED_COMMAND}` alone" in usage


class TestUpdateDevLock:
    def test_it_compiles_the_four_projects_hatchling_and_pip_for_every_platform(self, tmp_path):
        (step,) = _DEV.plan_update_dev_lock(dev_context(tmp_path))
        assert step.argv[:3] == ("/usr/local/bin/uv", "pip", "compile")
        assert step.cwd == tmp_path, "the input's relative paths are the repo root's"
        assert step.argv[step.argv.index("--python-version") + 1] == "3.11"
        assert "--universal" in step.argv
        assert step.argv[step.argv.index("--output-file") + 1] == "constraints/dev.txt"
        assert step.argv[-1] == "-", "the input arrives on standard input"
        assert step.stdin.splitlines() == [
            "-e ./packages/tools",
            "-e ./packages/mcp",
            "-e ./packages/agent",
            "-e ./packages/specimens",
            "hatchling",
            "pip",
        ]

    def test_it_compiles_each_projects_dev_group(self, tmp_path):
        """A dependency group is not part of a distribution: the editable lines do not bring it."""
        (step,) = _DEV.plan_update_dev_lock(dev_context(tmp_path))
        groups = [step.argv[i + 1] for i, part in enumerate(step.argv) if part == "--group"]
        assert groups == [f"packages/{project}/pyproject.toml:dev" for project in _DEV.PYTHON_PROJECTS]
        assert step.argv.index("--group") < step.argv.index("-"), "options precede the standard-input source"

    def test_a_refresh_moves_the_pins(self, tmp_path):
        """uv keeps an output file's pins without `--upgrade`: a refresh would change nothing."""
        (step,) = _DEV.plan_update_dev_lock(dev_context(tmp_path))
        assert "--upgrade" in step.argv

    def test_it_emits_none_of_the_four_projects(self, tmp_path):
        (step,) = _DEV.plan_update_dev_lock(dev_context(tmp_path))
        emitted = [step.argv[i + 1] for i, part in enumerate(step.argv) if part == "--no-emit-package"]
        assert emitted == ["swcraft-tools", "swcraft-mcp", "swcraft-agent", "swcraft-specimens"]

    def test_it_compiles_every_python_project(self):
        """The lock input names every Python project, and each one's distribution is kept out of the output.

        A project missing from the input would leave its dependencies unpinned, and ``install`` would
        resolve them freely.
        """
        assert [line[len("-e ./packages/") :] for line in _DEV.DEV_LOCK_INPUT.splitlines()[:-2]] == list(
            _DEV.PYTHON_PROJECTS
        )
        assert len(_DEV.DEV_LOCK_NO_EMIT) == len(_DEV.PYTHON_PROJECTS)

    def test_the_header_records_the_uv_version(self, tmp_path):
        (step,) = _DEV.plan_update_dev_lock(dev_context(tmp_path, uv_version="uv 1.2.3"))
        assert step.argv[step.argv.index("--custom-compile-command") + 1] == (
            "python scripts/dev.py update-dev-lock (uv 1.2.3)"
        )

    def test_it_refuses_without_uv_with_a_sentence(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(_DEV, "resolve_uv", lambda: None)
        runner = _Runner()
        assert _DEV.main(["--repo-root", str(tmp_path), "update-dev-lock"], run=runner) == 1
        assert runner.calls == []
        assert "uv was not found on PATH" in capsys.readouterr().err

    def test_main_reads_the_version_and_runs_uv_without_a_venv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_DEV, "resolve_uv", lambda: "/fake/uv")
        monkeypatch.setattr(_DEV, "read_uv_version", lambda uv: f"uv 9.9.9 from {uv}")
        runner = _Runner()
        assert _DEV.main(["--repo-root", str(tmp_path), "update-dev-lock"], run=runner) == 0
        ((argv, cwd, stdin),) = runner.calls
        assert argv[0] == "/fake/uv"
        assert "python scripts/dev.py update-dev-lock (uv 9.9.9 from /fake/uv)" in argv
        assert cwd == tmp_path
        assert stdin == _DEV.DEV_LOCK_INPUT

    def test_no_other_command_asks_uv_for_its_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_DEV, "resolve_uv", lambda: "/fake/uv")

        def refused(uv):
            raise AssertionError("a plain command must not run uv")

        monkeypatch.setattr(_DEV, "read_uv_version", refused)
        _write_lock(tmp_path)
        assert _DEV.main(["--repo-root", str(tmp_path), "--skip-node", "install"], run=_Runner()) == 0

    def test_it_is_never_a_verify_stage(self):
        assert "update-dev-lock" not in _DEV.VERIFY_STAGES
        assert "not part of verify" in _DEV.COMMANDS["update-dev-lock"].summary

    def test_a_version_answer_without_a_version_is_a_sentence(self, tmp_path, monkeypatch):
        class Answer:
            returncode = 0
            stdout = "uv\n"

        monkeypatch.setattr(_DEV.subprocess, "run", lambda *args, **kwargs: Answer())
        with pytest.raises(_DEV.DevError, match="did not name a version"):
            _DEV.read_uv_version("/fake/uv")


def _requirement_lines() -> list[str]:
    assert _LOCK.is_file(), f"{_DEV.DEV_LOCK} is absent: run `python scripts/dev.py update-dev-lock`"
    lines = _LOCK.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _pins() -> dict[str, str]:
    pins = {}
    for line in _requirement_lines():
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;]+)(\s*;.*)?", line)
        assert match is not None, f"not a pin: {line}"
        pins[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(2)
    return pins


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.match(r"\d+(?:\.\d+)*", version).group().split("."))


class TestTheCommittedLock:
    """The file itself; every case fails, by name, while it is absent."""

    def test_its_header_records_the_uv_version(self):
        assert _LOCK.is_file(), f"{_DEV.DEV_LOCK} is absent: run `python scripts/dev.py update-dev-lock`"
        header = _LOCK.read_text(encoding="utf-8")
        assert re.search(r"^#\s+python scripts/dev\.py update-dev-lock \(uv \d+\.\d+", header, re.MULTILINE)

    @pytest.mark.parametrize("name", _REQUIRED_PINS)
    def test_it_pins_the_tools_the_gate_rests_on_with_an_equality(self, name):
        assert name in _pins()

    @pytest.mark.parametrize("project", ["tools", "mcp", "agent", "specimens"])
    def test_every_direct_dependency_has_a_pin(self, project):
        document = tomllib.loads((REPO_ROOT / "packages" / project / "pyproject.toml").read_text(encoding="utf-8"))
        declared = [*document["project"]["dependencies"], *document["dependency-groups"]["dev"]]
        names = {re.sub(r"[-_.]+", "-", re.match(r"[A-Za-z0-9._-]+", item).group()).lower() for item in declared}
        missing = sorted(name for name in names if not name.startswith("swcraft-") and name not in _pins())
        assert not missing, f"{project} declares what the lock does not pin: {missing}"

    def test_every_line_is_a_pin_and_no_project_of_this_repository_is_one(self):
        assert not [name for name in _pins() if name.startswith("swcraft-")]

    def test_the_locked_pip_reads_the_build_constraint(self):
        assert _release(_pins()["pip"]) >= (25, 3)

    def test_the_declared_pytest_range_admits_the_locked_pytest(self):
        project = tomllib.loads((REPO_ROOT / "packages" / "tools" / "pyproject.toml").read_text(encoding="utf-8"))
        (declared,) = [
            requirement
            for requirement in project["dependency-groups"]["dev"]
            if re.match(r"pytest[<>=!~]", requirement)
        ]
        assert "<9.2" in declared, "the cap this case names moved: read packages/tools/pyproject.toml's comment"
        assert _release(_pins()["pytest"]) < (9, 2)
