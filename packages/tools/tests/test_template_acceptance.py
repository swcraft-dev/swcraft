"""Local standalone integration acceptance.

A standalone architecture project, scaffolded from the packaged template outside the SWCraft checkout, builds with a
real ``moon run :build`` and fails loudly, leaving nothing behind, when its model is wrong. The real
``dist/workstation/`` archive is installed into a temporary home with ``--no-path --no-vsix``, and
``<home>/current/bin`` is put first on the ``PATH`` of the ``moon`` subprocess.

1. **Success.** ``moon run :build`` exits 0 and writes every artifact the manifest's ``outputs:`` declares,
   non-empty.
2. **Failure.** A cross-layer inconsistency exits non-zero with the validator's own message and leaves the
   generated outputs absent. A failure before generation leaves the previous ARXML as ``model_old.arxml`` and no
   ``model.arxml``; one after generation leaves the refused ``model.arxml`` beside the previous ``_old``, never
   certified as the last good one.
3. **The cache never hides a change**: a failing build after a successful one, and a changed
   ``toolchain_version``, each first asserting the cache is warm.
4. **The build runs the installed toolchain and nothing of the checkout**, with every checkout directory and every
   other ``swcraft`` scrubbed from its environment.

**Nothing here may be skipped**: a missing ``moon``, workstation archive or ``.venv`` is a failure
(:func:`test_this_module_never_skips`, :class:`TestTheWorkstationArchiveIsMandatory`). The real cases carry
``@pytest.mark.integration`` and run only under ``python scripts/dev.py test-model-integration``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    CONFTEST_PATH,
    REPO_ROOT,
    break_model,
    build_artifacts_module,
    build_environment,
    declared_outputs,
    dev_context,
    dev_module,
    make_venv_marker,
    moon_build,
    no_skip_offenders,
    output_text,
    pin_build_platform,
    require_workstation_archive,
    run_captured,
    scaffold,
    seed_valid_model,
    toolchain_home,
    toolchain_python,
)
from swcraft_tools import writer
from swcraft_tools.writer.arxml_xml import previous_arxml_path

#: The validator message the deliberate inconsistency must produce: the message,
#: not merely a non-zero exit, proves the build failed at model validation. The
#: ghost link `break_model` writes into `SpeedSensor`'s `satisfies`.
EXPECTED_VALIDATOR_MESSAGE = "SWC 'SpeedSensor' satisfies 'REQ-GHOST-001@1': no requirement declares 'REQ-GHOST-001'."

#: The second error the seed carries: the identifier rule's em-dash sentence for
#: `Bad-Swc`. It is the one check that a non-ASCII finding line survives moon on
#: Windows (moon drops an undecodable line silently); no warning sentence carries
#: a non-ASCII character.
EXPECTED_NON_ASCII_LINE = (
    "ERROR: SWC 'Bad-Swc' in the swc layer is not a legal AUTOSAR Identifier: must be an AUTOSAR "
    "Identifier matching [a-zA-Z][a-zA-Z0-9_]* (a letter first, then letters, digits or underscores only). "
    "Rename 'Bad-Swc' — for example to 'Bad_Swc' ('swcraft impact \"Bad-Swc\"' lists what references it)."
)

#: The warning the same seed carries beside the errors: a requirement nothing satisfies.
EXPECTED_VALIDATOR_WARNING = (
    "WARNING: Requirement 'REQ-UNALLOCATED-001' is satisfied by no element: add "
    "`satisfies: [REQ-UNALLOCATED-001@1]` on the component, port, runnable, composition, interface, task "
    "or hardware element that realizes it."
)

_DEV = dev_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Moon's own wording for a task served from its cache. Asserted rather than
#: assumed: a suite running entirely cold would otherwise pass every cache case.
CACHED_MARKER = "cached"

#: One moon status line: ``architecture:validate-arxml (cached, e5ebefaa)``, or
#: ``(210ms, 8c0b5a09)``, or ``(skipped)``. Moon prints the ``cached`` marker per
#: task (moon 2.3.5), so the cases can name a task instead of counting them.
_TASK_STATUS_LINE = re.compile(r"\b(?P<project>[A-Za-z0-9_.-]+):(?P<task>[A-Za-z0-9_.-]+) \((?P<detail>[^)]*)\)")

#: Moon's wording for a task it never started because a dependency failed.
_SKIPPED_DETAIL = "skipped"


def _task_details(completed: subprocess.CompletedProcess) -> dict[str, list[str]]:
    """Every ``<project>:<task>`` moon reported, with the detail of each line."""
    details: dict[str, list[str]] = {}
    for match in _TASK_STATUS_LINE.finditer(output_text(completed)):
        details.setdefault(match["task"], []).append(match["detail"].strip().lower())
    return details


def cached_tasks(completed: subprocess.CompletedProcess) -> set[str]:
    """The tasks moon served from its cache, by name."""
    return {task for task, lines in _task_details(completed).items() if any(CACHED_MARKER in line for line in lines)}


def freshly_run_tasks(completed: subprocess.CompletedProcess) -> set[str]:
    """The tasks moon actually executed -- neither cached nor skipped.

    The positive form of "not served from the cache": absence from
    :func:`cached_tasks` also holds for a task moon never reported.
    """
    return {
        task
        for task, lines in _task_details(completed).items()
        if any(CACHED_MARKER not in line and line != _SKIPPED_DETAIL for line in lines)
    }


def reports_cached_tasks(completed: subprocess.CompletedProcess) -> bool:
    """True when moon reported at least one task served from its cache."""
    return bool(cached_tasks(completed))


def assert_outputs_absent(project: Path) -> None:
    """Every declared artifact is absent -- the failed-build guarantee."""
    present = [str(path) for path in declared_outputs(project).values() if path.exists()]
    assert not present, f"a failing build left generated outputs behind: {present}"


# ---------------------------------------------------------------------------
# The moon prerequisite -- fast, unmarked: it must hold in every gate
# ---------------------------------------------------------------------------


class TestMoonIsMandatory:
    """An unresolvable ``moon`` fails this stage; it never quietly disappears."""

    def test_the_stage_refuses_to_plan_without_moon(self, tmp_path):
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.plan_test_model_integration(dev_context(tmp_path, has_moon=False))
        message = str(excinfo.value)
        assert "moon" in message
        assert "skip" in message.lower(), "the message must say the acceptance may not be skipped"
        assert "dev.py test-model-integration" in message, "the message must name the command to re-run"

    def test_missing_moon_exits_non_zero_and_runs_nothing(self, tmp_path, capsys, monkeypatch):
        make_venv_marker(tmp_path)
        monkeypatch.setattr(_DEV, "moon_available", lambda: False)

        calls: list[list[str]] = []

        def runner(argv, cwd, stdin=None, env=()):
            calls.append([str(part) for part in argv])
            return 0

        exit_code = _DEV.main(["--repo-root", str(tmp_path), "test-model-integration"], run=runner)

        assert exit_code != 0
        assert calls == [], "a missing moon must be reported before anything runs"
        assert "moon" in capsys.readouterr().err

    def test_the_stage_is_the_final_gate_of_verify(self):
        """`verify` must run this acceptance -- otherwise it can be forgotten."""
        stages = _DEV.COMMANDS["verify"].stages
        assert stages[-1] == "test-model-integration"

    def test_this_module_never_skips(self):
        """No skip in this module or in `conftest.py`, where this acceptance's preconditions live."""
        for module in (Path(__file__), CONFTEST_PATH):
            offenders = no_skip_offenders(module)
            assert not offenders, f"this acceptance may never be skipped ({module.name}): {offenders}"


# ---------------------------------------------------------------------------
# The archive prerequisite -- fast, unmarked and hermetic
# ---------------------------------------------------------------------------


class TestTheWorkstationArchiveIsMandatory:
    """A missing or foreign archive fails this stage; it never skips.

    Hermetic: ``conftest.WORKSTATION_DIR`` is redirected at a ``tmp_path`` and
    ``sysconfig.get_platform()`` at :data:`conftest.MAPPED_BUILD_TAG`, so the cases
    run in the fast gate and read neither ``dist/`` nor the host. The platform pin
    is load-bearing: ``workstation_platform_token()`` refuses a tag outside the three
    the archive is built for, so an unpinned host could error here.

    Does not reach an archive's content (the precondition stats a filename) or the
    host's own token (``test_build_artifacts.py``'s
    ``test_the_default_token_is_the_running_interpreters_platform``).
    """

    def _pin_the_host(self, tmp_path, monkeypatch) -> None:
        pin_build_platform(monkeypatch)
        monkeypatch.setattr("conftest.WORKSTATION_DIR", tmp_path)

    def _expected_name(self) -> str:
        """The archive name these hermetic cases demand, written out rather than derived.

        Not :func:`conftest.require_workstation_archive`'s own expression: an oracle
        that recomputes the value the way the code does cannot detect a wrong
        derivation. The host is pinned to ``macosx-11.0-arm64``, so the platform half
        is the literal ``macos-arm64``; only the version is read.
        """
        return f"swcraft-{build_artifacts_module().read_version(REPO_ROOT)}-macos-arm64.zip"

    def test_an_empty_directory_fails_naming_the_command_that_builds_one(self, tmp_path, monkeypatch):
        self._pin_the_host(tmp_path, monkeypatch)

        with pytest.raises(pytest.fail.Exception) as excinfo:
            require_workstation_archive()

        message = str(excinfo.value)
        assert self._expected_name() in message, message
        assert "dev.py workstation-archive" in message, "the message must name the command to run"
        assert "nothing" in message, "the message must say what it found instead"

    def test_an_archive_of_another_version_or_platform_is_refused_by_name(self, tmp_path, monkeypatch):
        """The archive is resolved by exact name, never "the newest zip"."""
        self._pin_the_host(tmp_path, monkeypatch)
        artifacts = build_artifacts_module()
        version = artifacts.read_version(REPO_ROOT)
        token = artifacts.workstation_platform_token()
        foreign = [
            artifacts.workstation_archive_filename("9999.0.0", token),
            artifacts.workstation_archive_filename(version, "windows-x64" if token != "windows-x64" else "linux-x64"),
        ]
        for name in foreign:
            (tmp_path / name).write_bytes(b"")

        with pytest.raises(pytest.fail.Exception) as excinfo:
            require_workstation_archive()

        message = str(excinfo.value)
        assert self._expected_name() in message, message
        for name in foreign:
            assert name in message, f"the message must list what it found instead: {message}"

    def test_the_archive_of_this_version_and_platform_is_accepted(self, tmp_path, monkeypatch):
        """The other direction, so the cases above cannot pass on a precondition that refuses everything."""
        self._pin_the_host(tmp_path, monkeypatch)
        archive = tmp_path / self._expected_name()
        archive.write_bytes(b"")

        assert require_workstation_archive() == archive


# ---------------------------------------------------------------------------
# The model gate, end to end -- fast, unmarked: no moon, no archive, no build
# ---------------------------------------------------------------------------


def _swcraft(project: Path, *argv: str) -> subprocess.CompletedProcess:
    """One ``swcraft`` subcommand against *project*, from this checkout's CLI.

    This interpreter: the scaffolded project has none of its own, and these cases
    do not build the workstation archive.
    """
    return run_captured(
        [sys.executable, "-m", "swcraft_tools.cli", *argv, "--project", str(project / "swcraft.yaml")],
        cwd=REPO_ROOT,
    )


class TestTheSeededProjectPassesTheModelGate:
    """``swcraft init`` -> the seed -> ``check-model`` -> ``validate``, in the fast gate.

    The project shape the integration matrix builds with moon, driven with CLI
    calls so a broken seed is reported in the fast gate (the matrix is deselected
    there). Unmarked: no moon and no workstation archive needed.

    Does not reach the build graph, the cache, the installed toolchain or the
    artifact set; those stay the matrix's subject.
    """

    def test_the_seeded_model_passes_check_model(self, tmp_path: Path) -> None:
        """The gate a build graph runs first, over the seed the matrix builds."""
        project = tmp_path / "architecture"
        scaffold(project)
        seed_valid_model(project)

        completed = _swcraft(project, "check-model")

        assert completed.returncode == 0, output_text(completed)

    def test_the_seeded_model_passes_validate_and_the_arxml_is_written(self, tmp_path: Path) -> None:
        """``validate`` passes on the seed and leaves the ARXML the manifest declares on disk."""
        project = tmp_path / "architecture"
        scaffold(project)
        seed_valid_model(project)

        completed = _swcraft(project, "validate")

        assert completed.returncode == 0, output_text(completed)
        arxml = declared_outputs(project)["arxml"]
        assert arxml.is_file() and arxml.stat().st_size > 0, f"{arxml} was not written"

    def test_the_broken_model_is_refused_for_the_matrix_s_own_reason(self, tmp_path: Path) -> None:
        """The broken model is refused for the two deliberate errors and nothing else.

        The exact ``ERROR`` set is asserted, so a seed already invalid on its own
        cannot hide behind the expected message.
        """
        project = tmp_path / "architecture"
        scaffold(project)
        seed_valid_model(project)
        break_model(project)

        completed = _swcraft(project, "check-model")

        assert completed.returncode != 0, output_text(completed)
        text = output_text(completed)
        assert EXPECTED_VALIDATOR_MESSAGE in text, text
        errors = {line.strip() for line in text.splitlines() if "ERROR:" in line}
        assert errors == {f"ERROR: {EXPECTED_VALIDATOR_MESSAGE}", EXPECTED_NON_ASCII_LINE}, (
            f"the seed carries an error of its own beside the two deliberate ones: {errors}"
        )
        warnings = [line.strip() for line in text.splitlines() if "WARNING:" in line]
        assert warnings == [EXPECTED_VALIDATOR_WARNING], f"the seed's one warning is the requirement's: {warnings}"


# ---------------------------------------------------------------------------
# The acceptance matrix -- real scaffold, real installation, real moon
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStandaloneArchitectureBuild:
    def test_success_case(self, architecture_project: Path):
        """A valid model builds, and every manifest-declared artifact is written."""
        seed_valid_model(architecture_project)

        completed = moon_build(architecture_project)

        assert completed.returncode == 0, output_text(completed)
        for name, path in declared_outputs(architecture_project).items():
            assert path.is_file(), f"{name} was not generated at {path}\n{output_text(completed)}"
            assert path.stat().st_size > 0, f"{name} was generated empty at {path}"

    def test_failure_case_leaves_no_outputs(self, architecture_project: Path):
        """A cross-layer inconsistency fails the build at model validation."""
        seed_valid_model(architecture_project)
        break_model(architecture_project)

        completed = moon_build(architecture_project)

        assert completed.returncode != 0, output_text(completed)
        assert EXPECTED_VALIDATOR_MESSAGE in output_text(completed), output_text(completed)
        assert EXPECTED_NON_ASCII_LINE in output_text(completed), output_text(completed)
        assert EXPECTED_VALIDATOR_WARNING in output_text(completed), output_text(completed)
        assert_outputs_absent(architecture_project)

    def test_failure_after_success_leaves_no_stale_outputs(self, architecture_project: Path):
        """The previous run's artifacts do not survive a failing rebuild.

        The Moon cache is not cleared between the runs (a cached ``clean-outputs``
        is the defect this catches) and is proven warm first, so a working
        ``cache: false`` is told apart from a project moon never cached.
        """
        seed_valid_model(architecture_project)
        first = moon_build(architecture_project)
        assert first.returncode == 0, output_text(first)
        outputs = declared_outputs(architecture_project)
        assert all(path.is_file() for path in outputs.values()), output_text(first)

        cache = architecture_project / ".moon" / "cache"
        assert cache.is_dir(), "the Moon cache must be left in place -- that is what this case tests"

        # Moon must serve tasks from the cache here, or the failing run below
        # tests nothing about `cache: false`.
        warm = moon_build(architecture_project)
        assert warm.returncode == 0, output_text(warm)
        assert reports_cached_tasks(warm), (
            "moon reported no cached task on an unchanged rebuild -- this project is not being "
            f"cached at all, so the case below proves nothing:\n{output_text(warm)}"
        )
        assert all(path.is_file() for path in outputs.values()), (
            f"a cached rebuild must still leave every artifact in place:\n{output_text(warm)}"
        )

        break_model(architecture_project)
        second = moon_build(architecture_project)

        assert second.returncode != 0, output_text(second)
        assert EXPECTED_VALIDATOR_MESSAGE in output_text(second), output_text(second)
        assert EXPECTED_NON_ASCII_LINE in output_text(second), output_text(second)
        assert EXPECTED_VALIDATOR_WARNING in output_text(second), output_text(second)
        assert_outputs_absent(architecture_project)

    def test_the_previous_arxml_is_kept_across_the_whole_graph(self, architecture_project: Path):
        """`model_old.arxml` is the previous build's ARXML, through the real graph.

        `validate`'s byte-identical regeneration moves nothing, so `_old` is the
        previous build's file. A build failing at `check-model` leaves the last
        written ARXML as `_old` and no `model.arxml`.
        """
        seed_valid_model(architecture_project)
        arxml = declared_outputs(architecture_project)["arxml"]
        old = previous_arxml_path(arxml)
        old.unlink(missing_ok=True)

        first = moon_build(architecture_project)
        assert first.returncode == 0, output_text(first)
        first_bytes = arxml.read_bytes()

        writer.create_interface(
            str(architecture_project / "model"), "IOdometer", [{"name": "distance", "implementation_type": "float32"}]
        )
        second = moon_build(architecture_project)
        assert second.returncode == 0, output_text(second)
        second_bytes = arxml.read_bytes()
        assert second_bytes != first_bytes, "the model change did not reach the ARXML"
        assert old.read_bytes() == first_bytes, output_text(second)

        break_model(architecture_project)
        third = moon_build(architecture_project)
        assert third.returncode != 0, output_text(third)
        assert not arxml.exists(), output_text(third)
        assert old.read_bytes() == second_bytes, output_text(third)

    def test_the_audit_log_survives_the_build(self, architecture_project: Path):
        """`dist/audit.jsonl`, the MCP server's mutation record in the directory the build cleans, survives.

        `.gitignore` excludes `dist/`, so a clean step that wiped it would destroy
        the history irrecoverably."""
        seed_valid_model(architecture_project)
        audit = architecture_project / "dist" / "audit.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text('{"operation": "create_swc", "result": "SpeedSensor.yaml"}\n', encoding="utf-8")

        completed = moon_build(architecture_project)

        assert completed.returncode == 0, output_text(completed)
        assert audit.is_file(), f"the build destroyed the MCP audit log\n{output_text(completed)}"
        assert "create_swc" in audit.read_text(encoding="utf-8")
        for path in declared_outputs(architecture_project).values():
            assert path.is_file(), output_text(completed)

    def test_a_changed_toolchain_pin_is_not_served_from_the_cache(self, architecture_project: Path):
        """A changed `toolchain_version` re-runs every cacheable task, `validate-arxml` included.

        The pin is re-spelled, not re-pointed: `X` and `X.0` are one release
        (`project.toolchain_versions_agree` normalises PEP 440), so the bytes change
        and the build still completes; another release is the next case.

        Does not reach `validate-arxml`'s own declaration: moon's dependency-chain
        hashing re-runs it anyway. The declaration is pinned by
        `test_template_contents.py::test_every_cacheable_task_invalidates_on_a_toolchain_upgrade`.
        """
        seed_valid_model(architecture_project)
        assert moon_build(architecture_project).returncode == 0

        # The cache must be warm, or "no task was cached" below is trivially true.
        warm = moon_build(architecture_project)
        assert warm.returncode == 0, output_text(warm)
        assert cached_tasks(warm), (
            f"moon cached nothing on an unchanged rebuild -- this case proves nothing:\n{output_text(warm)}"
        )

        manifest = architecture_project / "swcraft.yaml"
        original = manifest.read_text(encoding="utf-8")
        version = build_artifacts_module().read_version(REPO_ROOT)
        respelled = original.replace(f'toolchain_version: "{version}"', f'toolchain_version: "{version}.0"')
        assert respelled != original, f"the scaffolded manifest does not pin {version}:\n{original}"
        try:
            manifest.write_text(respelled, encoding="utf-8", newline="\n")
            completed = moon_build(architecture_project)
        finally:
            manifest.write_text(original, encoding="utf-8", newline="\n")

        assert completed.returncode == 0, output_text(completed)
        assert not cached_tasks(completed), (
            "a changed toolchain pin was served from the cache -- an upgrade would be invisible "
            f"to the build:\n{output_text(completed)}"
        )
        assert "validate-arxml" in freshly_run_tasks(completed), (
            "the gate itself did not re-run on a changed pin -- `validate-arxml` is the task whose "
            f"`inputs:` gained `@group(model)` for exactly this:\n{output_text(completed)}"
        )

    def test_a_pin_naming_another_release_refuses_the_build(self, architecture_project: Path):
        """A pin naming another release stops the build at `clean-outputs`, before any generator.

        The refusal names both versions. The previous run's artifacts survive
        deliberately: the failed-build rule is about a failing generation, and here
        the refusal comes before the deletion, so the last successful build's output
        stays unchanged.
        """
        seed_valid_model(architecture_project)
        assert moon_build(architecture_project).returncode == 0

        manifest = architecture_project / "swcraft.yaml"
        original = manifest.read_text(encoding="utf-8")
        version = build_artifacts_module().read_version(REPO_ROOT)
        foreign = original.replace(f'toolchain_version: "{version}"', 'toolchain_version: "9999.0.0"')
        assert foreign != original, f"the scaffolded manifest does not pin {version}:\n{original}"
        try:
            manifest.write_text(foreign, encoding="utf-8", newline="\n")
            completed = moon_build(architecture_project)
        finally:
            manifest.write_text(original, encoding="utf-8", newline="\n")

        assert completed.returncode != 0, output_text(completed)
        report = output_text(completed)
        assert "9999.0.0" in report and version in report, report

        ran = freshly_run_tasks(completed)
        assert "clean-outputs" in ran, report
        assert not ran & {"check-model", "generate-arxml", "generate-diagrams", "generate-needs", "validate-arxml"}, (
            f"a task ran under a toolchain the project refuses:\n{report}"
        )


@pytest.mark.integration
class TestNoDependencyOnTheSwcraftCheckout:
    """Rule 4: the build runs the installed toolchain and nothing of the checkout.

    Does not reach moon's own resolution of the bare name with
    ``options.shell: false``: these cases depend on it, so their passing witnesses
    it on this platform, and nothing here witnesses it on Windows.
    """

    def test_the_swcraft_the_build_runs_resolves_inside_the_installation(self, architecture_project: Path):
        """The ``swcraft`` on the build's own ``PATH`` is the installation's, ahead of anything else."""
        environ = build_environment(architecture_project)
        resolved = shutil.which("swcraft", path=environ["PATH"])
        assert resolved is not None, f"no swcraft on the build's PATH: {environ['PATH']}"

        executable = Path(resolved).resolve()
        home = toolchain_home(architecture_project).resolve()
        assert home in executable.parents, f"{executable} is not inside the installation at {home}"
        assert REPO_ROOT not in executable.parents, f"{executable} is inside the SWCraft checkout"

        completed = run_captured([executable, "--version"], cwd=architecture_project)
        assert completed.returncode == 0, output_text(completed)
        assert build_artifacts_module().read_version(REPO_ROOT) in completed.stdout, completed.stdout

    def test_the_installed_interpreter_sees_no_swcraft_checkout_path(self, architecture_project: Path):
        """The installation's interpreter, run in the build's environment, reports no checkout path."""
        probe = (
            "import json, os, sys, swcraft_tools, swcraft_mcp;"
            "print(json.dumps({"
            "'sys_path': [p for p in sys.path if p],"
            "'PATH': os.environ.get('PATH', '').split(os.pathsep),"
            "'PYTHONPATH': os.environ.get('PYTHONPATH', '').split(os.pathsep),"
            "'tools': swcraft_tools.__file__,"
            "'mcp': swcraft_mcp.__file__}))"
        )
        completed = run_captured(
            [toolchain_python(architecture_project), "-c", probe],
            cwd=architecture_project,
            env=build_environment(architecture_project),
        )
        assert completed.returncode == 0, output_text(completed)
        report = json.loads(completed.stdout)

        checkout = str(REPO_ROOT)
        for key in ("sys_path", "PATH", "PYTHONPATH"):
            offenders = [entry for entry in report[key] if entry and Path(entry).resolve().is_relative_to(checkout)]
            assert not offenders, f"{key} reaches into the SWCraft checkout: {offenders}"

        home = toolchain_home(architecture_project).resolve()
        for key in ("tools", "mcp"):
            module_path = Path(report[key]).resolve()
            assert module_path.is_relative_to(home), f"{key} resolved outside the installation: {module_path}"

    def test_the_build_succeeds_with_every_swcraft_path_scrubbed(self, architecture_project: Path):
        """The build succeeds with the checkout and every other ``swcraft`` scrubbed from ``PATH``.

        Every other one, so an ambient installation cannot be what succeeded.
        """
        seed_valid_model(architecture_project)

        environ = dict(os.environ)
        environ.pop("PYTHONPATH", None)
        environ.pop("VIRTUAL_ENV", None)
        environ["PATH"] = os.pathsep.join(
            entry
            for entry in environ.get("PATH", "").split(os.pathsep)
            if entry
            and not Path(entry).resolve().is_relative_to(str(REPO_ROOT))
            and shutil.which("swcraft", path=entry) is None
        )
        assert shutil.which("swcraft", path=environ["PATH"]) is None, (
            "a swcraft survived the scrub -- this case could not tell the installed archive "
            f"from an ambient toolchain: {environ['PATH']}"
        )

        completed = moon_build(architecture_project, env=environ)

        assert completed.returncode == 0, output_text(completed)
        for path in declared_outputs(architecture_project).values():
            assert path.is_file() and path.stat().st_size > 0, output_text(completed)
