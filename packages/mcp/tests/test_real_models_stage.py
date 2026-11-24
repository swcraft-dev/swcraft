"""The real-export tier's privacy, end to end: the whole stage on a sentinel export, names nowhere.

The stage's output -- terminal and record -- must never carry a name of a private export. Each test
builds ``test_passport.py``'s sentinel model into an export whose directory and file name carry the
sentinel prefix, runs the stage's ``mcp_server`` step (``plan_test_real_models``' own argv, never
copied) in a subprocess, and requires that neither sentinel nor the prefix ``zqsen`` appears in
stdout, stderr or the record. The runs: report-only (the whole record, and a figure injected through
the file name is never read), not configured, blocking, an import failure, an unreadable export
given by a relative path, an uncaught exception in the record fixture's set-up and teardown (its
text withheld), a warning (counted, not shown) and an exception inside a step.

The specimen step takes a rejecting ``sh`` stub, so no run starts the real generator; the runs
that need it skip behind a capability probe where a script file cannot start (the Windows runner).
``slow``: each run is a fresh pytest process over the whole chain.

Does not reach: an unreadable ``check-model`` document (unit-tested in
``functional/test_real_models_classifier.py``); a built specimen, a ``chain green`` verdict and a
disagreeing pair (held at unit level); the stage with a configured generator on Windows. A leaked
numeric value is held only by construction (every recorded figure is a count, a size, a time or a
leaf path), and a real export's own shapes reach the stage only where the real exports are.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import dev_context, dev_module, load_repo_script, specimen_generator_module
from stub_generator import write_sh_wrapper, write_stub
from test_passport import NAME_SENTINEL, TEXT_SENTINEL, sentinel_root  # noqa: F401 - the fixture

from tests.functional.conftest import diagnosed_pytest, run_diagnosed
from tests.functional.real_models import CHAIN_GREEN, ENV_VAR
from tests.functional.test_real_models import FAULT_VAR, RECORD_OPENING, RECORD_VAR

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCH_MODULE = load_repo_script("agent_task_bench", "scripts/agent_task_bench.py")

#: The lowercase prefix both sentinels share: a lowered, truncated or misspelt derivative of either
#: still carries it.
SENTINEL_PREFIX = "zqsen"

#: The specimen generator's variable, read from the invocation module (never spelled here).
GENERATOR_VARIABLE = specimen_generator_module().VARIABLE

#: The rejecting stub's stderr line count.
REJECTED_LINES = 3


@pytest.fixture(scope="module")
def rejecting_generator(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A one-line ``sh`` stub, in a sentinel-named directory, that rejects every passport; else a skip.

    A capability probe, not a platform check: the stub is written and started, and the runs that
    need it skip where the operating system cannot start a script file directly.
    """
    directory = tmp_path_factory.mktemp("generator") / f"{SENTINEL_PREFIX}Generator"
    script = write_stub(directory)
    wrapper = write_sh_wrapper(
        directory / "generator", script, "reject", str(REJECTED_LINES), f"{NAME_SENTINEL} {TEXT_SENTINEL}"
    )
    try:
        probe = subprocess.run(  # noqa: S603 - the stub this fixture wrote
            [
                os.fspath(wrapper),
                "specimen",
                os.fspath(directory / "probe.json"),
                "--out",
                os.fspath(directory / "out"),
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except OSError:
        pytest.skip("this host cannot start a script file directly, which the rejecting stub needs")
    assert probe.returncode == 2, "the rejecting stub did not reject"
    return wrapper


def _no_generator(tmp_path: Path) -> Path:
    """A sentinel-named absolute path that names no file: the generator not configured."""
    return tmp_path / f"{SENTINEL_PREFIX}Missing" / "generator"


def _swcraft(cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", *argv],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture(scope="module")
def sentinel_export(sentinel_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:  # noqa: F811 - the fixture
    """The sentinel model's ARXML, built by ``swcraft generate arxml``; a file named with the prefix."""
    project = sentinel_root.parent
    manifest = project / "swcraft.yaml"
    manifest.write_text(BENCH_MODULE.MANIFEST, encoding="utf-8", newline="\n")
    built = _swcraft(project, "generate", "arxml", "--project", manifest.as_posix())
    assert built.returncode == 0, built.stderr[-2000:]
    export = tmp_path_factory.mktemp("export") / f"{SENTINEL_PREFIX}Export.arxml"
    shutil.copyfile(project / "dist" / "model.arxml", export)
    assert NAME_SENTINEL in export.read_text(encoding="utf-8"), "the export must carry the sentinel it is held to"
    return export


#: An import report's unresolved-references heading with an injected figure, carried in a file name.
INJECTED_HEADING = "\nUnresolved references (987654321 reference(s) to 0 target(s))"
INJECTED_NUMBER = "987654321"


def _injected_name(directory: Path) -> str:
    """``<prefix>A<INJECTED_HEADING>.arxml`` where the file system takes a newline in a name; else ``<prefix>A.arxml``.

    A capability probe, not a platform check: where no file name can hold the newline, no export's
    name can inject the line either.
    """
    name = f"{SENTINEL_PREFIX}A{INJECTED_HEADING}.arxml"
    try:
        (directory / name).touch()
    except OSError:
        return f"{SENTINEL_PREFIX}A.arxml"
    (directory / name).unlink()
    return name


def _exports(directory: Path, export: Path, *, truncated: bool = False, injected: bool = False) -> Path:
    """A sentinel-named directory holding *export*, and a truncated copy sorted after it when asked.

    *injected* names the export with :data:`INJECTED_HEADING` (:func:`_injected_name`).
    """
    directory = directory / f"{SENTINEL_PREFIX}Exports"
    directory.mkdir()
    name = _injected_name(directory) if injected else f"{SENTINEL_PREFIX}A.arxml"
    shutil.copyfile(export, directory / name)
    if truncated:
        text = export.read_text(encoding="utf-8")
        (directory / f"{SENTINEL_PREFIX}B.arxml").write_text(text[: len(text) // 2], encoding="utf-8", newline="\n")
    return directory


def _stage_argv() -> tuple[tuple[str, ...], Path]:
    """The stage's ``mcp_server`` step as ``plan_test_real_models`` plans it, on this interpreter."""
    (step,) = [step for step in dev_module().plan_test_real_models(dev_context(REPO_ROOT)) if step.cwd.name == "mcp"]
    assert step.argv[1:3] == ("-m", "pytest"), "the owning project runs pytest itself, never the no-tests wrapper"
    return (sys.executable, *step.argv[1:]), step.cwd


def _run_stage(
    exports: Path, record: Path, fault: str | None = None, *, generator: Path, relative: bool = False
) -> tuple[subprocess.CompletedProcess[str], str]:
    """The stage on *exports*, the record at *record*; its output and the record's text (or ``""``).

    *generator* is the specimen generator's absolute path in the child's environment (the rejecting
    stub, or a path naming no file), so no run reaches the real generator. *relative* gives the
    exports' variable relative to the stage's working directory.
    """
    argv, cwd = _stage_argv()
    environment = {
        name: setting
        for name, setting in os.environ.items()
        if name not in {ENV_VAR, RECORD_VAR, FAULT_VAR, GENERATOR_VARIABLE, "PYTEST_ADDOPTS", "COLUMNS"}
        and not name.startswith(("PYTEST_XDIST_", "PYTEST_CURRENT_TEST"))
    }
    environment[ENV_VAR] = os.path.relpath(exports, cwd) if relative else str(exports)
    environment[GENERATOR_VARIABLE] = os.fspath(generator)
    environment[RECORD_VAR] = str(record)
    if fault is not None:
        environment[FAULT_VAR] = fault
    # argv is (python, -m, pytest, ...): the same pytest arguments under the hang probe.
    completed = run_diagnosed(
        diagnosed_pytest(list(argv[3:]), record.parent / f"{record.stem}-basetemp"),
        cwd=cwd,
        env=environment,
        timeout=900,
    )
    return completed, record.read_text(encoding="utf-8") if record.is_file() else ""


def _names_nowhere(completed: subprocess.CompletedProcess[str], record: str) -> None:
    for text in (completed.stdout, completed.stderr, record):
        assert NAME_SENTINEL not in text
        assert TEXT_SENTINEL not in text
        assert SENTINEL_PREFIX not in text.lower()


def _fault(text: str) -> str:
    """A fault's text: both sentinels, so a leak of either half is caught."""
    return f"{text} {NAME_SENTINEL} {TEXT_SENTINEL}"


def _verdict(record: str, label: str) -> str:
    """The verdict line of *label* in *record*: the text after ``- <label>: ``."""
    (line,) = [line for line in record.splitlines() if line.startswith(f"- {label}: ")]
    return line.removeprefix(f"- {label}: ")


def test_a_report_only_run_writes_the_whole_record_and_names_nothing(
    sentinel_export: Path, rejecting_generator: Path, tmp_path: Path
) -> None:
    completed, record = _run_stage(
        _exports(tmp_path, sentinel_export, injected=True), tmp_path / "record.md", generator=rejecting_generator
    )
    assert completed.returncode == 0, "the stage was red on the sentinel export"
    for text in (completed.stdout, completed.stderr, record):
        for number in (INJECTED_NUMBER, f"{int(INJECTED_NUMBER):,}", f"{int(INJECTED_NUMBER):_}"):
            assert number not in text, "a figure injected through the export's file name was read"
    assert "1 passed" in completed.stdout
    lines = record.splitlines()
    assert lines[0].startswith("# The passport chain's calibration on 1 real model — ")
    assert lines[2] == RECORD_OPENING
    for heading in ("## Counts", "## real model A", "### Recipe forms, paired", "### Reference calls, paired"):
        assert heading in lines, heading
    assert "### check-model, paired" in lines and "## Verdict per export" in lines
    assert "Import report" not in record, "the import report is not read"
    # The stub's stderr lines carry both sentinels: counted, never read.
    (specimen_row,) = [line for line in lines if line.startswith("Specimen: ")]
    outcome = f"rejected by the generator: {REJECTED_LINES} lines"
    assert specimen_row == f"Specimen: {outcome}"
    rows = [line for line in lines if line.startswith("| ") and "paired" not in line]
    paired = [row for row in rows if row.endswith(" | not run: no specimen | not judged: no specimen |")]
    assert paired, "the forms are measured on the import, the specimen column empty"
    assert all(" B, " in row or "not asked" in row for row in paired), "the real column is always filled"
    assert "| exit | | exit " in record and "| not run: no specimen |" in record
    assert "Calibration finding: " in record
    verdict = _verdict(record, "real model A")
    assert verdict == f"no specimen: {outcome}"
    assert f"Verdict: {verdict}" in lines
    _names_nowhere(completed, record)


def test_a_blocking_calibration_fails_a_verdict_that_is_not_green(
    sentinel_export: Path, rejecting_generator: Path, tmp_path: Path
) -> None:
    completed, record = _run_stage(
        _exports(tmp_path, sentinel_export), tmp_path / "record.md", fault="blocking:", generator=rejecting_generator
    )
    assert completed.returncode == 1
    assert "1 failed" in completed.stdout and "passed" not in completed.stdout
    line = _verdict(record, "real model A")
    verdict = line.removesuffix("; failed: the calibration is blocking")
    assert line != verdict, "the record's verdict line says the export failed"
    assert verdict != CHAIN_GREEN and verdict.startswith("no specimen: ")
    assert f"real model A: {verdict}; the calibration is blocking" in completed.stdout
    _names_nowhere(completed, record)


def test_an_import_failure_and_a_finalizer_exception_name_nothing(
    sentinel_export: Path, rejecting_generator: Path, tmp_path: Path
) -> None:
    completed, record = _run_stage(
        _exports(tmp_path, sentinel_export, truncated=True),
        tmp_path / "record.md",
        fault=f"finalizer:{_fault('the finalizer')}",
        generator=rejecting_generator,
    )
    assert completed.returncode == 1
    assert "1 passed" in completed.stdout and "1 failed" in completed.stdout and "1 error" in completed.stdout
    assert "real model B: red at import: swcraft import model exited 1" in completed.stdout
    assert "raised RuntimeError in teardown, its text withheld" in completed.stdout
    assert _verdict(record, "real model A").startswith("no specimen: ")
    assert _verdict(record, "real model B") == "red at import: swcraft import model exited 1"
    assert "## real model B" not in record, "an export a defect stopped has its verdict line alone"
    _names_nowhere(completed, record)


def test_a_fixture_exception_names_nothing(sentinel_export: Path, tmp_path: Path) -> None:
    completed, record = _run_stage(
        _exports(tmp_path, sentinel_export),
        tmp_path / "record.md",
        fault=f"fixture:{_fault('the fixture')}",
        generator=_no_generator(tmp_path),
    )
    assert completed.returncode == 1
    assert "1 error" in completed.stdout and "passed" not in completed.stdout
    assert "raised RuntimeError in setup, its text withheld" in completed.stdout
    assert record == "", "a run that reached no export writes no record"
    _names_nowhere(completed, record)


def test_a_warning_is_counted_and_names_nothing(
    sentinel_export: Path, rejecting_generator: Path, tmp_path: Path
) -> None:
    completed, record = _run_stage(
        _exports(tmp_path, sentinel_export),
        tmp_path / "record.md",
        fault=f"warning:{_fault('the warning')}",
        generator=rejecting_generator,
    )
    assert completed.returncode == 0, "a warning is swallowed and counted, never a red"
    assert "1 passed" in completed.stdout
    assert _verdict(record, "real model A").endswith("; 1 warnings, not shown")
    _names_nowhere(completed, record)


def test_an_exception_inside_a_step_is_a_red_at_that_step_naming_its_class(
    sentinel_export: Path, tmp_path: Path
) -> None:
    completed, record = _run_stage(
        _exports(tmp_path, sentinel_export),
        tmp_path / "record.md",
        fault=f"specimen:{_fault('the specimen')}",
        generator=_no_generator(tmp_path),
    )
    assert completed.returncode == 1
    assert "1 failed" in completed.stdout and "passed" not in completed.stdout
    assert "real model A: red at specimen: raised RuntimeError" in completed.stdout
    assert _verdict(record, "real model A") == "red at specimen: raised RuntimeError"
    assert "No export reached its verdict; the lines below say where each one stopped." in record
    assert "## real model A" not in record
    _names_nowhere(completed, record)


def test_an_unreadable_export_is_a_red_at_read_and_a_relative_value_resolves(
    sentinel_export: Path, rejecting_generator: Path, tmp_path: Path
) -> None:
    exports = _exports(tmp_path, sentinel_export)
    unreadable = exports / f"{SENTINEL_PREFIX}B.arxml"
    shutil.copyfile(sentinel_export, unreadable)
    mode = unreadable.stat().st_mode
    unreadable.chmod(0)
    try:
        if os.access(unreadable, os.R_OK):
            pytest.skip("this host reads a file without permission bits")
        completed, record = _run_stage(exports, tmp_path / "record.md", relative=True, generator=rejecting_generator)
    finally:
        unreadable.chmod(mode)
    assert completed.returncode == 1
    assert "1 passed" in completed.stdout and "1 failed" in completed.stdout
    assert "real model B: red at read: raised PermissionError" in completed.stdout
    assert _verdict(record, "real model A").startswith("no specimen: ")
    assert _verdict(record, "real model B") == "red at read: raised PermissionError"
    _names_nowhere(completed, record)


def test_a_generator_that_is_not_configured_fails_the_export_naming_the_variable_alone(
    sentinel_export: Path, tmp_path: Path
) -> None:
    completed, record = _run_stage(
        _exports(tmp_path, sentinel_export), tmp_path / "record.md", generator=_no_generator(tmp_path)
    )
    assert completed.returncode == 1
    assert "1 failed" in completed.stdout and "passed" not in completed.stdout
    sentence = f"{GENERATOR_VARIABLE} names no file: the calibration builds each specimen"
    assert f"real model A: red at specimen: {sentence}" in completed.stdout
    assert _verdict(record, "real model A").startswith(f"red at specimen: {sentence}")
    assert "## real model A" not in record
    _names_nowhere(completed, record)
