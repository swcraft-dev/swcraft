"""The archive's specimen generator against the archive's SWCraft: the generator's own integration suite.

The workstation archive installs ``swcraft-specimens``, built from ``packages/specimens/``, beside
``swcraft``, so a release is red when the generator it ships does not work with the SWCraft it ships.
The ``artifact`` half (:class:`TestTheArchivesGeneratorWorksWithTheArchivesSwcraft`, in ``dev.py
test-artifacts`` after ``workstation-archive``) takes the session's archive install (``conftest.py``'s
``archive_install``, shared with ``test_workstation_install.py``) and:

1. runs ``packages/specimens/tests -m integration`` with ``.venv``'s pytest, ``SWCRAFT_BIN`` and the pin
   file's ``generator.bin_variable`` naming the installed executables, and a start log of its own;
2. runs ``packages/specimens/tests/scenarios/run_scenarios.py --all-presets`` against the installed
   ``swcraft`` and ``swcraft-specimens``;
3. fails on an integration run that passed no case or skipped one for want of either variable, and on a
   start log that is empty or names any command but the installed executable (every integration case
   starts the generator through ``packages/specimens/tests/oracle/generator_start.py``, which logs it, so
   a case that ran the editable source instead of the archive's wheel is a finding);
4. imports every module of ``generator.imports`` with the installation's own interpreter, ``-I``, so an
   install lacking the HiGHS binding is red here, not at a user's first specimen.

It fails, never skips, when the install or ``.venv``'s interpreter is absent; every failure starts with
one sentence (:func:`failure_sentence`), then the suite's output. A child past
:data:`CHILD_TIMEOUT_SECONDS` has its process tree killed and fails with the output it had written. The
fast half holds the plan, the refusals, the start-log reading and the message form on fakes.

What this does not reach: the generator's correctness beyond its integration and scenario suites; an
integration case's in-process calls into ``swcraft_specimens`` (they start no process, so the start log
cannot see them); any platform but the one the stage runs on; the suite's other skips of its own, read as
passes; and which side of a failure is wrong.
"""

from __future__ import annotations

import ast
import json
import os
import re
import signal
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import (
    CONFTEST_PATH,
    LOADER_MODULES,
    REPO_ROOT,
    installed_executable,
    load_repo_script,
    no_skip_offenders,
    output_text,
    specimen_generator_module,
)

_GENERATOR = specimen_generator_module()

#: The generator's project, whose integration suite and scenario runner the run starts.
PROJECT = Path("packages") / "specimens"

#: The variable the generator's integration suite reads for the SWCraft it gates.
SWCRAFT_BIN = "SWCRAFT_BIN"

#: The variable naming the file the integration tests' start helper logs each start to.
START_LOG_VARIABLE = "SWCRAFT_SPECIMENS_START_LOG"

#: Environment the suite's children never inherit: the run is ``-I`` and starts installed console
#: scripts, and a source path from the developer's shell would be an import path the test does not see.
DROPPED_ENVIRONMENT = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE")

#: Each child's deadline, below the stages' 1200 s per-test ceiling so a hang is reported with its
#: output, not as pytest-timeout's crash (on Windows that ends the whole stage).
CHILD_TIMEOUT_SECONDS = 1100

#: The bound on each cleanup step after a deadline (the tree kill, then the reap), so the deadline plus
#: both stays below the stages' 1200 s per-test ceiling.
CHILD_CLEANUP_SECONDS = 30

#: The longest finding a sentence carries; the suite's full output follows the sentences.
FINDING_WIDTH = 300

#: Imports each module named on its command line; the first that fails ends it with its traceback.
IMPORT_PROBE = "import importlib, sys\nfor name in sys.argv[1:]:\n    importlib.import_module(name)\n"

_PARAMETER = re.compile(r"\[([^\]]+)\]$")
_SCENARIO_ERROR = re.compile(r"^([A-Za-z0-9_.-]+(?:/[^:\s]+)?): (.+)$")


class Unavailable(Exception):
    """A precondition of the run is missing: the test **fails** with this sentence."""


@dataclass(frozen=True)
class ArchiveRun:
    """The run of the archive's generator against the archive's SWCraft, planned and not yet executed."""

    bin_variable: str
    project: Path
    python: Path
    swcraft: Path
    generator: Path
    environ: dict[str, str]
    start_log: Path
    suite_argv: list[str]
    suite_report: Path
    scenarios_argv: list[str]
    imports_argv: list[str]


def failure_sentence(where: str, finding: str) -> str:
    """The one sentence every failure of this run starts with."""
    return (
        f"The specimen generator the archive installs fails against the SWCraft it installs: {where}: "
        f"{finding.rstrip('.')}. Fix the side that is wrong, in packages/specimens/ or in SWCraft."
    )


def plan_run(repo_root: Path, home: Path, work: Path, environ: Mapping[str, str]) -> ArchiveRun:
    """The run against the installation in *home*; :class:`Unavailable` naming what is missing.

    Nothing is started. The names under *work* are short because Windows' path budget is shared with
    the scenario suite's build tree.
    """
    try:
        generator = _GENERATOR.load_pins(repo_root)["generator"]
    except (OSError, ValueError) as error:
        raise Unavailable(f"the specimens' pin file cannot be read: {error}") from None
    python = _GENERATOR.venv_python(repo_root / _GENERATOR.VENV_DIR)
    if not python.is_file():
        raise Unavailable(f"{_GENERATOR.VENV_DIR.as_posix()} holds no interpreter: run `python scripts/dev.py install`")
    project = repo_root / PROJECT
    swcraft = installed_executable(home, "swcraft")
    installed_generator = installed_executable(home, generator["executable"])
    installed_python = installed_executable(home, "python")
    for executable in (swcraft, installed_generator, installed_python):
        if not executable.is_file():
            raise Unavailable(f"the workstation installation holds no {executable.name} in {executable.parent}")
    start_log = work / "r" / "starts.jsonl"
    child = {name: value for name, value in environ.items() if name not in DROPPED_ENVIRONMENT}
    child.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            SWCRAFT_BIN: os.fspath(swcraft),
            generator["bin_variable"]: os.fspath(installed_generator),
            START_LOG_VARIABLE: os.fspath(start_log),
        }
    )
    interpreter = os.fspath(python)
    suite_report = work / "r" / "suite.xml"
    return ArchiveRun(
        bin_variable=generator["bin_variable"],
        project=project,
        python=python,
        swcraft=swcraft,
        generator=installed_generator,
        environ=child,
        start_log=start_log,
        suite_argv=[
            interpreter,
            "-I",
            "-m",
            "pytest",
            "-m",
            "integration",
            "-p",
            "no:cacheprovider",
            "-rfE",
            "--junitxml",
            os.fspath(suite_report),
            os.fspath(project / "tests"),
        ],
        suite_report=suite_report,
        scenarios_argv=[
            interpreter,
            "-I",
            os.fspath(project / "tests" / "scenarios" / "run_scenarios.py"),
            "--all-presets",
            "--swcraft-bin",
            os.fspath(swcraft),
            "--specimens-bin",
            os.fspath(installed_generator),
            "--build-root",
            os.fspath(work / "b"),
            "--results",
            os.fspath(work / "r" / "scenario-results.json"),
        ],
        imports_argv=[os.fspath(installed_python), "-I", "-c", IMPORT_PROBE, *generator["imports"]],
    )


def prepare_work(run: ArchiveRun) -> None:
    """Create the directories the run's children write into, before any of them starts.

    The start helper does not create the log's parent, and pytest creates the report's only at session end.
    """
    for directory in (run.start_log.parent, run.suite_report.parent):
        directory.mkdir(parents=True, exist_ok=True)


def _case_node(case: ElementTree.Element) -> str:
    """A JUnit case's node id: its class name and its name joined by ``::``, an empty part left out."""
    return "::".join(part for part in (case.get("classname", ""), case.get("name", "")) if part)


def _folded_message(outcome: ElementTree.Element) -> str:
    """A JUnit outcome's ``message`` with its whitespace folded into one line (``""`` when it has none)."""
    return " ".join((outcome.get("message") or "").split())


def pytest_unexercised(report: str, variables: Sequence[str]) -> list[tuple[str, str]]:
    """``(where, finding)`` for a suite that tested nothing.

    That is: no case passed, or a case skipped naming one of *variables* (the ones the run sets, so such a skip
    means they did not reach the suite).
    """
    findings = []
    passed = 0
    for case in ElementTree.fromstring(report).iter("testcase"):
        outcomes = [outcome for outcome in case if outcome.tag in ("failure", "error", "skipped")]
        if not outcomes:
            passed += 1
        for outcome in outcomes:
            text = f"{outcome.get('message') or ''} {outcome.text or ''}"
            named = [variable for variable in variables if variable in text]
            if outcome.tag == "skipped" and named:
                findings.append(
                    (
                        _case_node(case),
                        f"skipped for want of {', '.join(named)}, which this run sets: {_folded_message(outcome)}",
                    )
                )
    if not passed:
        findings.append(("the integration suite", "no case passed, so it tested nothing"))
    return findings


def pytest_failures(report: str) -> list[tuple[str, str]]:
    """``(where, finding)`` per failed or erroring case of the suite's JUnit XML report.

    The report, not the console: the suite's ``addopts = "-q"`` drops the message from the summary.
    *where* is the case's parameter (preset or specimen) with the case beside it; *finding* is the
    message folded into one line and cut at :data:`FINDING_WIDTH`.
    """
    failures = []
    for case in ElementTree.fromstring(report).iter("testcase"):
        for outcome in case:
            if outcome.tag not in ("failure", "error"):
                continue
            node = _case_node(case)
            message = _folded_message(outcome) or f"{outcome.tag} with no message"
            if len(message) > FINDING_WIDTH:
                message = message[: FINDING_WIDTH - len(" (cut)")] + " (cut)"
            parameter = _PARAMETER.search(case.get("name", ""))
            failures.append((f"{parameter.group(1)} ({node})" if parameter else node, message))
    return failures


def start_findings(run: ArchiveRun) -> list[tuple[str, str]]:
    """``(where, finding)`` for a start log that does not show the installed generator, and only it.

    An absent or empty log is one finding; any other logged command names the case that started it.
    """
    lines = run.start_log.read_text(encoding="utf-8").splitlines() if run.start_log.is_file() else []
    if not lines:
        return [("the start log", "no integration case started the generator through the start helper")]
    expected = [os.fspath(run.generator)]
    findings = []
    for line in lines:
        entry = json.loads(line)
        if entry.get("command") != expected:
            findings.append(
                (
                    entry.get("test") or "a case with no test id",
                    f"started {entry.get('command')!r}, not the installed {run.generator.name}",
                )
            )
    return findings


def scenario_failure(stderr: str) -> tuple[str, str]:
    """``(where, finding)`` of a failed ``run_scenarios.py`` run: its first ``error:`` line at column 0.

    The ``<preset>/<scenario>`` or ``<preset>`` prefix is *where*; a child's ``error:`` line after the
    script's is output, not the finding.
    """
    errors = [line[len("error: ") :].strip() for line in stderr.splitlines() if line.startswith("error: ")]
    if not errors:
        return "the scenario suite", "it exited non-zero with no error line"
    matched = _SCENARIO_ERROR.match(errors[0])
    if matched is None:
        return "the scenario suite", errors[0]
    return f"{matched.group(1)} (tests/scenarios/run_scenarios.py)", matched.group(2)


def failure_message(failures: Sequence[tuple[str, str]], output: str) -> str:
    """One :func:`failure_sentence` per failure, then the suite's output."""
    sentences = [failure_sentence(where, finding) for where, finding in failures]
    return "\n".join(sentences) + "\n--- the suite's output ---\n" + output


def _stream_text(stream: str | bytes | None) -> str:
    """A captured stream as text: ``TimeoutExpired`` may carry bytes although the run asked for text."""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream or ""


def _group_options() -> dict[str, object]:
    """Popen options that start the child as the root of a tree :func:`_kill_tree` can reach.

    A new session on POSIX, a new process group on Windows; read at call time, so no module-level name
    is one the other platform lacks.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill *process* and its descendants: ``taskkill /T /F`` on Windows, ``os.killpg`` elsewhere.

    The child itself is killed afterwards in any case. Not reached: a POSIX descendant that started a
    session of its own, and a Windows descendant whose parent exited before the walk.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=CHILD_CLEANUP_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        killpg = getattr(os, "killpg", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if killpg is not None and sigkill is not None:
            try:
                killpg(process.pid, sigkill)
            except OSError:
                pass
    try:
        process.kill()
    except OSError:
        pass


def run_child(run: ArchiveRun, argv: Sequence[str], where: str) -> subprocess.CompletedProcess:
    """*argv* from the generator's project, in the run's environment, captured, with :data:`CHILD_TIMEOUT_SECONDS`.

    A child past its deadline has its tree killed and fails the case with the sentence for *where* and
    the output a bounded reap collects, rather than as pytest-timeout's crash (on Windows that ends the
    whole stage). On Windows a reap that times out again carries no partial output.
    """
    process = subprocess.Popen(
        list(argv),
        cwd=run.project,
        env=run.environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_group_options(),
    )
    try:
        stdout, stderr = process.communicate(timeout=CHILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as expired:
        _kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=CHILD_CLEANUP_SECONDS)
        except subprocess.TimeoutExpired as escaped:
            stdout, stderr = escaped.stdout or expired.stdout, escaped.stderr or expired.stderr
        output = f"{_stream_text(stdout)}\n{_stream_text(stderr)}"
        pytest.fail(
            failure_message([(where, f"it did not finish within {CHILD_TIMEOUT_SECONDS} s")], output),
            pytrace=False,
        )
    except BaseException:
        _kill_tree(process)
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def suite_finding(run: ArchiveRun, completed: subprocess.CompletedProcess) -> str | None:
    """``None`` for a green integration suite that tested something through the installed generator.

    On exit 0 too, a run that tested nothing (:func:`pytest_unexercised`) or a start log that does not
    show the installed generator alone (:func:`start_findings`) is a finding.
    """
    output = output_text(completed)
    report = run.suite_report.read_text(encoding="utf-8") if run.suite_report.is_file() else ""
    if completed.returncode == 0:
        if not report:
            return failure_message([("the integration suite", "pytest exited 0 and wrote no report")], output)
        findings = pytest_unexercised(report, (SWCRAFT_BIN, run.bin_variable)) + start_findings(run)
        return failure_message(findings, output) if findings else None
    failures = (pytest_failures(report) if report else []) or [
        ("the integration suite", f"pytest exited {completed.returncode} with no failure in its summary")
    ]
    return failure_message(failures, output)


def scenarios_finding(completed: subprocess.CompletedProcess) -> str | None:
    """``None`` for a green scenario suite; else :func:`failure_message` over its error line."""
    if completed.returncode == 0:
        return None
    return failure_message([scenario_failure(completed.stderr)], output_text(completed))


# ---------------------------------------------------------------------------
# The real run (deselected; `dev.py test-artifacts`, after `workstation-archive`)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def archive_run(archive_install: Path, tmp_path_factory: pytest.TempPathFactory) -> ArchiveRun:
    """The run planned against the session's installation; a failure otherwise."""
    try:
        run = plan_run(REPO_ROOT, archive_install, tmp_path_factory.mktemp("specimens"), os.environ)
    except Unavailable as missing:
        pytest.fail(str(missing))
    prepare_work(run)
    return run


@pytest.mark.artifact
class TestTheArchivesGeneratorWorksWithTheArchivesSwcraft:
    """The run on the real installation; each case is one child, so each has its own deadline."""

    def test_the_generators_integration_suite_passes_through_the_installed_executable(
        self, archive_run: ArchiveRun
    ) -> None:
        completed = run_child(archive_run, archive_run.suite_argv, "the integration suite")
        finding = suite_finding(archive_run, completed)
        assert finding is None, finding

    def test_the_generators_scenario_suite_passes_against_the_installed_swcraft(self, archive_run: ArchiveRun) -> None:
        completed = run_child(archive_run, archive_run.scenarios_argv, "the scenario suite")
        finding = scenarios_finding(completed)
        assert finding is None, finding

    def test_the_installation_imports_every_module_the_pin_file_names(self, archive_run: ArchiveRun) -> None:
        completed = run_child(archive_run, archive_run.imports_argv, "the pinned imports")
        assert completed.returncode == 0, failure_message(
            [("the pinned imports", f"the installation's interpreter exited {completed.returncode}")],
            output_text(completed),
        )


# ---------------------------------------------------------------------------
# The plan, the refusals and the message form, on fakes (fast)
# ---------------------------------------------------------------------------

_PINS = _GENERATOR.load_pins(REPO_ROOT)


class _Fake:
    """A fake repository (the pin file and ``.venv``'s interpreter) and a fake installation."""

    def __init__(self, root: Path) -> None:
        self.repo = root / "repo"
        self.home = root / "home"
        self.work = root / "work"
        self.work.mkdir(parents=True)
        self.pins = json.loads((REPO_ROOT / _GENERATOR.PIN_FILE).read_text(encoding="utf-8"))
        target = self.repo / _GENERATOR.PIN_FILE
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(self.pins, indent=2) + "\n", encoding="utf-8", newline="\n")
        self.python = _GENERATOR.venv_python(self.repo / _GENERATOR.VENV_DIR)
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"")
        for name in ("swcraft", self.pins["generator"]["executable"], "python"):
            executable = installed_executable(self.home, name)
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"")

    def plan(self) -> ArchiveRun:
        return plan_run(self.repo, self.home, self.work, {"PATH": "/bin", "PYTHONPATH": "/source", "KEEP": "1"})


@pytest.fixture
def fake(tmp_path: Path) -> _Fake:
    return _Fake(tmp_path)


class TestTheRunsPlan:
    def test_the_suite_runs_isolated_from_the_project_with_the_three_variables(self, fake: _Fake) -> None:
        run = fake.plan()
        python = os.fspath(fake.python)
        assert run.project == fake.repo / PROJECT
        assert run.suite_argv == [
            python,
            "-I",
            "-m",
            "pytest",
            "-m",
            "integration",
            "-p",
            "no:cacheprovider",
            "-rfE",
            "--junitxml",
            os.fspath(fake.work / "r" / "suite.xml"),
            os.fspath(fake.repo / PROJECT / "tests"),
        ]
        assert run.suite_report == fake.work / "r" / "suite.xml"
        swcraft = installed_executable(fake.home, "swcraft")
        generator = installed_executable(fake.home, fake.pins["generator"]["executable"])
        assert run.environ["PYTHONDONTWRITEBYTECODE"] == "1"
        assert run.environ[SWCRAFT_BIN] == os.fspath(swcraft)
        assert run.environ[fake.pins["generator"]["bin_variable"]] == os.fspath(generator)
        assert run.environ[START_LOG_VARIABLE] == os.fspath(fake.work / "r" / "starts.jsonl")
        assert run.environ["KEEP"] == "1" and "PYTHONPATH" not in run.environ

    def test_the_planned_run_creates_nothing_and_preparing_it_creates_the_log_directory(self, fake: _Fake) -> None:
        """The start helper opens the log for appending from the first start; the directory must exist."""
        run = fake.plan()
        assert not run.start_log.parent.exists()
        prepare_work(run)
        assert run.start_log.parent.is_dir() and run.suite_report.parent.is_dir()
        with open(run.start_log, "a", encoding="utf-8", newline="\n") as stream:
            stream.write("{}\n")

    def test_the_variables_are_the_ones_the_generators_tests_read(self) -> None:
        """The start helper's two variables, read from its source, are the pin's and this module's."""
        helper = REPO_ROOT / PROJECT / "tests" / "oracle" / "generator_start.py"
        constants = {
            node.targets[0].id: node.value.value
            for node in ast.parse(helper.read_text(encoding="utf-8")).body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
        }
        assert constants["BIN_VARIABLE"] == _PINS["generator"]["bin_variable"]
        assert constants["START_LOG_VARIABLE"] == START_LOG_VARIABLE

    def test_the_scenarios_take_the_installed_generator_and_report_under_the_temporary_directory(
        self, fake: _Fake
    ) -> None:
        run = fake.plan()
        assert run.scenarios_argv == [
            os.fspath(fake.python),
            "-I",
            os.fspath(fake.repo / PROJECT / "tests" / "scenarios" / "run_scenarios.py"),
            "--all-presets",
            "--swcraft-bin",
            os.fspath(installed_executable(fake.home, "swcraft")),
            "--specimens-bin",
            os.fspath(installed_executable(fake.home, fake.pins["generator"]["executable"])),
            "--build-root",
            os.fspath(fake.work / "b"),
            "--results",
            os.fspath(fake.work / "r" / "scenario-results.json"),
        ]
        results = Path(run.scenarios_argv[-1])
        assert fake.work in results.parents and results.is_relative_to(Path(run.scenarios_argv[-3]).parent)

    def test_the_pinned_imports_run_on_the_installations_interpreter_isolated(self, fake: _Fake) -> None:
        run = fake.plan()
        assert run.imports_argv == [
            os.fspath(installed_executable(fake.home, "python")),
            "-I",
            "-c",
            IMPORT_PROBE,
            *fake.pins["generator"]["imports"],
        ]

    def test_the_import_probe_imports_each_name_and_fails_on_a_missing_one(self) -> None:
        good = subprocess.run([sys.executable, "-I", "-c", IMPORT_PROBE, "json", "xml.etree"], check=False)
        bad = subprocess.run(
            [sys.executable, "-I", "-c", IMPORT_PROBE, "json", "no_such_module_here"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert good.returncode == 0
        assert bad.returncode != 0 and "no_such_module_here" in bad.stderr

    def test_the_runner_takes_the_specimens_bin_option(self, fake: _Fake, monkeypatch: pytest.MonkeyPatch) -> None:
        """The runner's own parser accepts every option the plan passes and hands each value on.

        Not reached: the suite itself running those binaries, and an option the plan does not pass.
        """
        runner = load_repo_script(
            "swcraft_test_run_scenarios", (PROJECT / "tests" / "scenarios" / "run_scenarios.py").as_posix()
        )
        received: list[tuple[list[str], str, Path, Path, list[str]]] = []

        def record(presets: list[str], swcraft: str, build_root: Path, results: Path, generator: list[str]) -> None:
            received.append((presets, swcraft, build_root, results, generator))

        monkeypatch.setattr(runner, "run_suite", record)
        monkeypatch.delenv(runner.SPECIMENS_BIN_VARIABLE, raising=False)
        monkeypatch.delenv(SWCRAFT_BIN, raising=False)
        argv = fake.plan().scenarios_argv
        assert Path(argv[2]).name == "run_scenarios.py"

        assert runner.main(argv[3:]) == 0
        assert received == [
            (
                ["small", "medium", "large"],
                os.fspath(installed_executable(fake.home, "swcraft")),
                fake.work / "b",
                fake.work / "r" / "scenario-results.json",
                [os.fspath(installed_executable(fake.home, fake.pins["generator"]["executable"]))],
            )
        ]


class TestAMissingPreconditionFails:
    """Each precondition is an :class:`Unavailable`, which the fixture turns into ``pytest.fail``."""

    def test_an_absent_venv_interpreter(self, fake: _Fake) -> None:
        fake.python.unlink()
        with pytest.raises(Unavailable, match=r"\.venv holds no interpreter: run `python scripts/dev.py install`"):
            fake.plan()

    @pytest.mark.parametrize("which", ["swcraft", "generator", "python"])
    def test_an_absent_installation(self, fake: _Fake, which: str) -> None:
        name = fake.pins["generator"]["executable"] if which == "generator" else which
        installed_executable(fake.home, name).unlink()
        with pytest.raises(Unavailable, match="the workstation installation holds no"):
            fake.plan()

    def test_an_unreadable_pin_file(self, fake: _Fake) -> None:
        (fake.repo / _GENERATOR.PIN_FILE).write_bytes(b"\xff\xfe")
        with pytest.raises(Unavailable, match="the specimens' pin file cannot be read"):
            fake.plan()

    def test_this_module_never_skips(self) -> None:
        """The release's gate may not turn into a skip here or in the ``tools`` conftest's modules.

        That covers this module, the ``tools`` ``conftest.py`` and the modules that load that conftest under this
        root.
        """
        for module in (Path(__file__), CONFTEST_PATH, *LOADER_MODULES):
            offenders = no_skip_offenders(module)
            assert not offenders, f"the archive's generator test may never be skipped ({module.name}): {offenders}"

    def test_the_real_cases_take_the_planned_fixture_and_are_artifact_marked(self) -> None:
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        real = classes["TestTheArchivesGeneratorWorksWithTheArchivesSwcraft"]
        assert [ast.unparse(decorator) for decorator in real.decorator_list] == ["pytest.mark.artifact"]
        cases = [node for node in real.body if isinstance(node, ast.FunctionDef)]
        assert len(cases) == 3
        assert all("archive_run" in [argument.arg for argument in case.args.args] for case in cases)


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


class TestTheStartLog:
    """Every integration case starts the generator through the helper, which logs each start."""

    def _log(self, run: ArchiveRun, *commands: tuple[str, list[str]]) -> None:
        run.start_log.parent.mkdir(parents=True, exist_ok=True)
        run.start_log.write_text(
            "".join(json.dumps({"test": test, "command": command}) + "\n" for test, command in commands),
            encoding="utf-8",
            newline="\n",
        )

    def test_the_installed_executable_alone_is_green(self, fake: _Fake) -> None:
        run = fake.plan()
        self._log(run, ("tests/test_tiny_model.py::test_external_swcraft_gate (call)", [os.fspath(run.generator)]))
        assert start_findings(run) == []

    def test_a_module_start_is_a_finding_naming_its_case(self, fake: _Fake) -> None:
        run = fake.plan()
        module = ["/venv/bin/python", "-m", "swcraft_specimens"]
        self._log(
            run,
            ("tests/test_tiny_model.py::test_external_swcraft_gate (call)", [os.fspath(run.generator)]),
            ("tests/test_placement_scales.py::test_general_swcraft_gate (call)", module),
        )
        ((where, finding),) = start_findings(run)
        assert where == "tests/test_placement_scales.py::test_general_swcraft_gate (call)"
        assert "'-m', 'swcraft_specimens'" in finding and f"not the installed {run.generator.name}" in finding

    @pytest.mark.parametrize("written", [None, ""])
    def test_an_absent_or_empty_log_is_a_finding(self, fake: _Fake, written: str | None) -> None:
        run = fake.plan()
        if written is not None:
            run.start_log.parent.mkdir(parents=True, exist_ok=True)
            run.start_log.write_text(written, encoding="utf-8", newline="\n")
        assert start_findings(run) == [
            ("the start log", "no integration case started the generator through the start helper")
        ]


class TestTheFailureMessage:
    """Every failure starts with the sentence, then the suite's output follows."""

    _OPENING = "The specimen generator the archive installs fails against the SWCraft it installs: "
    _CLOSING = ". Fix the side that is wrong, in packages/specimens/ or in SWCraft."

    def _report(self, run: ArchiveRun, cases: str) -> None:
        run.suite_report.parent.mkdir(parents=True, exist_ok=True)
        run.suite_report.write_text(
            '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
            f"{cases}</testsuite></testsuites>",
            encoding="utf-8",
            newline="\n",
        )

    def _started_as_installed(self, run: ArchiveRun) -> None:
        run.start_log.parent.mkdir(parents=True, exist_ok=True)
        run.start_log.write_text(
            json.dumps({"test": "t", "command": [os.fspath(run.generator)]}) + "\n", encoding="utf-8", newline="\n"
        )

    def test_a_failing_preset_and_a_failing_specimen_each_give_the_sentence(self, fake: _Fake) -> None:
        run = fake.plan()
        self._report(
            run,
            '<testcase classname="tests.test_production_preset_golden" name="test_production_swcraft_gate[medium]"/>'
            '<testcase classname="tests.test_production_preset_golden" name="test_production_swcraft_gate[small]">'
            '<failure message="AssertionError: check-model: W012 rule&#10;  on SWC_Ctrl">trace</failure></testcase>'
            '<testcase classname="tests.test_specimen_acceptance" '
            'name="test_swcraft_meets_the_acceptance_specimen[map-bound]">'
            '<error message="validate exited 1: unmapped type T_Speed">trace</error></testcase>'
            '<testcase classname="tests.test_tiny_model" name="test_external_swcraft_gate">'
            '<failure message="">trace</failure></testcase>'
            '<testcase classname="tests.test_placement_scales" name="test_general_swcraft_gate">'
            f'<failure message="AssertionError: {"x" * 400}">trace</failure></testcase>',
        )
        stdout = "..F.E\n2 failed, 1 error, 38 passed in 12.00s\n"
        message = suite_finding(run, _completed(1, stdout))
        assert message is not None
        lines = message.splitlines()
        assert lines[0] == (
            self._OPENING
            + "small (tests.test_production_preset_golden::test_production_swcraft_gate[small]): "
            + "AssertionError: check-model: W012 rule on SWC_Ctrl"
            + self._CLOSING
        )
        assert lines[1] == (
            self._OPENING
            + "map-bound (tests.test_specimen_acceptance::test_swcraft_meets_the_acceptance_specimen[map-bound]): "
            + "validate exited 1: unmapped type T_Speed"
            + self._CLOSING
        )
        assert lines[2] == (
            self._OPENING + "tests.test_tiny_model::test_external_swcraft_gate: failure with no message" + self._CLOSING
        )
        long_finding = "AssertionError: " + "x" * (FINDING_WIDTH - len(" (cut)") - len("AssertionError: ")) + " (cut)"
        assert lines[3] == (
            self._OPENING + "tests.test_placement_scales::test_general_swcraft_gate: " + long_finding + self._CLOSING
        )
        assert lines[4] == "--- the suite's output ---" and stdout in message

    def test_a_red_suite_with_no_summary_line_still_gives_the_sentence(self, fake: _Fake) -> None:
        message = suite_finding(fake.plan(), _completed(4, "", "ERROR: usage"))
        assert message is not None
        assert message.startswith(self._OPENING + "the integration suite: pytest exited 4")
        assert "ERROR: usage" in message

    def test_the_scenario_finding_is_the_scripts_first_column_zero_error_line(self) -> None:
        """A child's ``error:`` line after the script's, indented or not, is output, never the finding."""
        stderr = (
            "error: small/validate: expected exit 0, got 1\n"
            "  swcraft stderr:\n"
            "    error: E042 unresolved port on SWC_A\n"
            "error: child said this later\n"
        )
        where, finding = scenario_failure(stderr)
        assert (where, finding) == ("small/validate (tests/scenarios/run_scenarios.py)", "expected exit 0, got 1")

    def test_a_failing_scenario_preset_gives_the_sentence(self) -> None:
        stderr = "error: medium/resource-report: expected exit 0, got 1: over budget on ECU_A\n"
        message = scenarios_finding(_completed(1, "", stderr))
        assert message is not None
        assert message.splitlines()[0] == (
            self._OPENING
            + "medium/resource-report (tests/scenarios/run_scenarios.py): expected exit 0, got 1: over budget on ECU_A"
            + self._CLOSING
        )

    def test_a_green_run_has_no_finding(self, fake: _Fake) -> None:
        run = fake.plan()
        self._report(run, '<testcase classname="tests.test_tiny_model" name="test_external_swcraft_gate"/>')
        self._started_as_installed(run)
        assert suite_finding(run, _completed(0, "40 passed")) is None
        assert scenarios_finding(_completed(0, "scenario suite passed")) is None

    def test_a_green_suite_whose_starts_were_not_logged_is_a_finding(self, fake: _Fake) -> None:
        run = fake.plan()
        self._report(run, '<testcase classname="tests.test_tiny_model" name="test_external_swcraft_gate"/>')
        message = suite_finding(run, _completed(0, "40 passed"))
        assert message is not None
        assert message.splitlines()[0] == (
            self._OPENING
            + "the start log: no integration case started the generator through the start helper"
            + self._CLOSING
        )

    def test_a_known_optional_skip_beside_a_pass_is_green(self, fake: _Fake) -> None:
        run = fake.plan()
        self._report(
            run,
            '<testcase classname="tests.test_tiny_model" name="test_external_swcraft_gate"/>'
            '<testcase classname="tests.test_swcraft_gate" name="test_real_passports">'
            '<skipped type="pytest.skip" message="no real passport is read">'
            # pytest's skip location; assembled, so the module carries no line citation.
            "tests/test_swcraft_gate.py" + ":220</skipped></testcase>",
        )
        self._started_as_installed(run)
        assert suite_finding(run, _completed(0, "1 passed, 1 skipped")) is None

    def test_an_all_skipped_run_that_exits_zero_is_a_finding(self, fake: _Fake) -> None:
        """Every integration case skips when ``SWCRAFT_BIN`` does not reach it, and pytest exits 0."""
        run = fake.plan()
        self._report(
            run,
            '<testcase classname="tests.test_tiny_model" name="test_external_swcraft_gate">'
            '<skipped type="pytest.skip" message="SWCRAFT_BIN is not set">tests/test_tiny_model.py' + ":268</skipped>"
            "</testcase>",
        )
        message = suite_finding(run, _completed(0, "1 skipped"))
        assert message is not None
        lines = message.splitlines()
        assert lines[0] == (
            self._OPENING
            + "tests.test_tiny_model::test_external_swcraft_gate: skipped for want of SWCRAFT_BIN, which this run "
            + "sets: SWCRAFT_BIN is not set"
            + self._CLOSING
        )
        assert lines[1] == self._OPENING + "the integration suite: no case passed, so it tested nothing" + self._CLOSING

    def test_a_skip_naming_the_generators_variable_is_a_finding_beside_passes(self, fake: _Fake) -> None:
        run = fake.plan()
        variable = fake.pins["generator"]["bin_variable"]
        self._report(
            run,
            '<testcase classname="tests.test_tiny_model" name="test_external_swcraft_gate"/>'
            '<testcase classname="tests.test_placement_scales" name="test_generator_gate">'
            f'<skipped type="pytest.skip" message="{variable} is not set">x</skipped></testcase>',
        )
        self._started_as_installed(run)
        message = suite_finding(run, _completed(0, "1 passed, 1 skipped"))
        assert message is not None and f"skipped for want of {variable}" in message
        assert "no case passed" not in message

    def test_a_zero_exit_with_no_report_is_a_finding(self, fake: _Fake) -> None:
        message = suite_finding(fake.plan(), _completed(0, "40 passed"))
        assert message is not None
        assert message.startswith(self._OPENING + "the integration suite: pytest exited 0 and wrote no report")

    class _HangingPopen:
        """A child past its deadline: the first ``communicate`` times out with part of the output.

        The reap returns the whole output, or, with *escaped*, times out again. Timeouts and kills are recorded.
        """

        def __init__(self, record: dict[str, list], *, escaped: bool) -> None:
            self._record = record
            self._escaped = escaped
            self.pid = 4242

        def __call__(self, argv, **kwargs):
            self.args = argv
            self._record["popen"].append(kwargs)
            return self

        def communicate(self, timeout=None):
            self._record["timeouts"].append(timeout)
            if len(self._record["timeouts"]) == 1:
                raise subprocess.TimeoutExpired(self.args, timeout, output=b"collected 41 items\n", stderr=b"")
            if self._escaped:
                raise subprocess.TimeoutExpired(
                    self.args, timeout, output=b"collected 41 items\n.", stderr=b"hung here"
                )
            return "collected 41 items\n..", "hung here"

        def kill(self) -> None:
            self._record["kills"].append(("child", self.pid))

    def _hang(self, monkeypatch: pytest.MonkeyPatch, platform: str, *, escaped: bool = False) -> dict[str, list]:
        record: dict[str, list] = {"popen": [], "timeouts": [], "kills": []}
        real_run = subprocess.run

        def taskkill(argv, **kwargs):
            if argv[0] != "taskkill":
                return real_run(argv, **kwargs)
            record["kills"].append(("taskkill", tuple(argv), kwargs.get("timeout")))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(subprocess, "Popen", self._HangingPopen(record, escaped=escaped))
        monkeypatch.setattr(subprocess, "run", taskkill)
        monkeypatch.setattr(os, "killpg", lambda pid, sig: record["kills"].append(("killpg", pid, sig)), raising=False)
        monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
        monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
        monkeypatch.setattr(os, "name", platform)
        return record

    def _deadline_failure(self, run: ArchiveRun) -> str:
        with pytest.raises(pytest.fail.Exception) as failed:
            run_child(run, ["python", "-m", "pytest"], "the integration suite")
        message = str(failed.value)
        assert message.startswith(
            self._OPENING + f"the integration suite: it did not finish within {CHILD_TIMEOUT_SECONDS} s" + self._CLOSING
        )
        return message

    def test_a_child_past_its_deadline_has_its_group_killed_and_fails_with_the_sentence_and_its_output(
        self, fake: _Fake, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = fake.plan()
        record = self._hang(monkeypatch, "posix")
        message = self._deadline_failure(run)
        assert record["popen"][0]["start_new_session"] is True
        assert record["popen"][0]["cwd"] == fake.repo / PROJECT, "the suite runs from the generator's project"
        assert record["kills"] == [("killpg", 4242, 9), ("child", 4242)]
        assert record["timeouts"] == [CHILD_TIMEOUT_SECONDS, CHILD_CLEANUP_SECONDS]
        assert "collected 41 items\n.." in message and "hung here" in message

    def test_on_windows_the_tree_is_killed_by_taskkill_within_the_cleanup_bound(
        self, fake: _Fake, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = fake.plan()
        record = self._hang(monkeypatch, "nt")
        self._deadline_failure(run)
        assert record["popen"][0]["creationflags"] == 0x200
        assert "start_new_session" not in record["popen"][0]
        assert record["kills"] == [
            ("taskkill", ("taskkill", "/T", "/F", "/PID", "4242"), CHILD_CLEANUP_SECONDS),
            ("child", 4242),
        ]
        assert record["timeouts"] == [CHILD_TIMEOUT_SECONDS, CHILD_CLEANUP_SECONDS]

    def test_a_descendant_holding_the_pipe_past_the_bound_fails_with_what_was_read(
        self, fake: _Fake, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = fake.plan()
        record = self._hang(monkeypatch, "posix", escaped=True)
        message = self._deadline_failure(run)
        assert record["timeouts"] == [CHILD_TIMEOUT_SECONDS, CHILD_CLEANUP_SECONDS]
        assert "collected 41 items\n." in message and "hung here" in message
