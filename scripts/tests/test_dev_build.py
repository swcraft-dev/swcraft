"""Tests for ``scripts/dev.py build`` — the reference-model chain against ``examples/basic/``.

* the **plan**: the step order, built without running anything — the removal, ``check-model``
  before any generator, and the ARXML validation last, after the optional XSD provisioning;
* the **behaviour**: real runs on a ``tmp_path`` copy of the reference project (fast enough for
  the fast gate). A broken build is seeded by ``scripts/seed_scenario5_build_error.py`` (one
  invalid connector), so it fails at model validation and a failing build is shown to leave no
  output.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import (
    REPO_ROOT,
    dev_context,
    dev_module,
    load_repo_script,
    make_venv_marker,
    output_text,
    run_captured,
)
from node_support import requires_node

_DEV = dev_module()
_CLEAN = load_repo_script("swcraft_clean_outputs", "scripts/clean_outputs.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _labels(steps) -> list[str]:
    return [step.label for step in steps]


def _copy_reference_repo(tmp_path: Path) -> Path:
    """Copy the reference project and ``scripts/`` into tmp, without ``dist/`` or ``views/``."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    shutil.copytree(
        REPO_ROOT / _DEV.REFERENCE_PROJECT,
        repo_root / _DEV.REFERENCE_PROJECT,
        ignore=shutil.ignore_patterns("__pycache__", "dist", "views"),
    )
    shutil.copytree(
        REPO_ROOT / "scripts",
        repo_root / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return repo_root


def _real_context(repo_root: Path):
    """A Context whose interpreter really exists — the one running the tests (the dispatcher would want ``.venv``)."""
    return dev_context(repo_root, python=sys.executable)


def _execute(steps, env: dict[str, str] | None = None) -> list[tuple[str, int, str]]:
    """Run a plan for real, stopping at the first failure like ``run_steps``."""
    log: list[tuple[str, int, str]] = []
    for step in steps:
        result = run_captured(step.argv, step.cwd, env=env)
        log.append((step.label, result.returncode, output_text(result)))
        if result.returncode != 0:
            break
    return log


def _labels_of(log) -> list[str]:
    """The step labels of an ``_execute`` log."""
    return [label for label, _, _ in log]


def _seed_build_error(repo_root: Path) -> None:
    """Break the copied model with one invalid connector, so the build fails at model validation."""
    seeded = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "seed_scenario5_build_error.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr


def _offline_env(**overrides: str) -> dict[str, str]:
    """The current environment with XSD provisioning pinned to "not available".

    Both the provisioning script and the validator resolve the schema from the real repository
    root, so a machine holding ``schemas/AUTOSAR_4-2-2.xsd`` would otherwise take another path.
    """
    env = dict(os.environ)
    env.pop("SWCRAFT_AUTOSAR_XSD_URL", None)
    env["SWCRAFT_AUTOSAR_XSD_PATH"] = str(Path("no-such-directory") / "AUTOSAR_4-2-2.xsd")
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_build_is_a_dispatch_table_command(self):
        assert "build" in _DEV.COMMANDS
        assert _DEV.COMMANDS["build"].plan is _DEV.plan_build

    def test_build_verifies_its_outputs_after_the_steps(self):
        assert _DEV.COMMANDS["build"].verify is _DEV.verify_build_outputs

    def test_build_needs_the_venv_and_no_network(self):
        command = _DEV.COMMANDS["build"]
        assert command.needs_venv
        assert not command.uses_network
        assert "build" not in _DEV.NETWORK_COMMANDS


class TestBuildPlan:
    def test_step_sequence_and_order(self, tmp_path):
        """clean -> check-model -> generate arxml/diagrams/needs -> (schema) -> validate."""
        steps = _DEV.plan_build(dev_context(tmp_path))
        assert _labels(steps) == [
            "remove the previous outputs (examples/basic)",
            "check-model (examples/basic)",
            "generate arxml (examples/basic)",
            "generate diagrams (examples/basic)",
            "generate needs (examples/basic)",
            "provision the AUTOSAR XSD (optional)",
            "validate the generated ARXML",
        ]

    def test_the_removal_runs_first_and_names_exactly_the_declared_outputs(self, tmp_path):
        """After a failing build the outputs are *absent* under their own names.

        The previous ARXML is kept as `model_old.arxml`; "nothing was written" is not that while old files
        remain.
        """
        first = _DEV.plan_build(dev_context(tmp_path))[0]
        assert first.argv[1] == str(tmp_path / "scripts" / "clean_outputs.py")
        assert list(first.argv[2:]) == [str(path) for path in _DEV.reference_outputs(tmp_path)]

    def test_check_model_runs_before_every_generator(self, tmp_path):
        """`check-model` runs before every generator, never `swcraft validate`.

        `check-model` writes nothing; `swcraft validate` would rewrite the ARXML that the last step
        validates.
        """
        steps = _DEV.plan_build(dev_context(tmp_path))
        assert "check-model" in steps[1].argv
        assert all("validate" not in part for part in steps[1].argv)
        generators = [index for index, step in enumerate(steps) if "generate" in step.argv]
        assert generators and min(generators) > 1

    def test_every_step_targets_the_reference_manifest(self, tmp_path):
        manifest = str(tmp_path / "examples" / "basic" / "swcraft.yaml")
        assert _DEV.reference_manifest(tmp_path) == Path(manifest)
        for step in _DEV.plan_build(dev_context(tmp_path)):
            if "swcraft_tools.cli" in step.argv:
                assert step.argv[step.argv.index("--project") + 1] == manifest

    def test_the_arxml_validation_runs_last_on_the_generated_file(self, tmp_path):
        last = _DEV.plan_build(dev_context(tmp_path))[-1]
        assert "swcraft_tools.arxml_validation" in last.argv
        assert last.argv[-1] == str(_DEV.reference_outputs(tmp_path)[0])

    def test_the_optional_schema_is_provisioned_before_the_validation(self, tmp_path):
        steps = _DEV.plan_build(dev_context(tmp_path))
        provision = next(i for i, step in enumerate(steps) if "download_autosar_schema.py" in " ".join(step.argv))
        validate = next(i for i, step in enumerate(steps) if "swcraft_tools.arxml_validation" in step.argv)
        assert provision < validate

    def test_every_step_uses_the_venv_interpreter(self, tmp_path):
        expected = str(_DEV.venv_python(tmp_path, "posix"))
        assert {step.argv[0] for step in _DEV.plan_build(dev_context(tmp_path))} == {expected}

    def test_the_cli_is_invoked_as_a_module_not_as_a_path_lookup(self, tmp_path):
        """A bare `swcraft` would resolve through PATH, possibly to another venv."""
        for step in _DEV.plan_build(dev_context(tmp_path)):
            assert Path(step.argv[0]).name.startswith("python")


class TestReferenceOutputsMatchTheManifest:
    """``dev.py`` cannot read the manifest's YAML, so its mirrored output paths are pinned here."""

    def test_the_mirrored_paths_are_exactly_the_manifest_outputs(self):
        """The mirrored paths are the manifest's ARXML and ``needs.json`` outputs and every diagram file.

        The diagram files are those the generator writes for this model, held to the generator's golden
        output, so a model change that adds or removes a view fails here by name instead of leaving a stale
        view behind a failing build.
        """
        manifest = yaml.safe_load(
            (REPO_ROOT / _DEV.REFERENCE_PROJECT / _DEV.REFERENCE_MANIFEST).read_text(encoding="utf-8")
        )
        golden = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "diagrams_golden" / "basic"
        views = {f"{manifest['views_root']}/{entry.name}" for entry in golden.iterdir()}
        assert set(_DEV.REFERENCE_OUTPUTS) == {manifest["outputs"]["arxml"]} | views | {manifest["outputs"]["needs"]}
        assert len(_DEV.REFERENCE_OUTPUTS) == len(set(_DEV.REFERENCE_OUTPUTS))
        assert "views/model.c4" not in _DEV.REFERENCE_OUTPUTS

    def test_the_views_sit_between_the_arxml_and_the_needs_json_in_the_generators_order(self):
        views = [output for output in _DEV.REFERENCE_OUTPUTS if output.startswith("views/")]
        assert _DEV.REFERENCE_OUTPUTS == ("dist/model.arxml", *views, "dist/needs.json")
        assert not any(output.startswith("dist/needs/") for output in _DEV.REFERENCE_OUTPUTS)
        assert views[-2:] == ["views/report.txt", "views/index.html"]

    def test_the_arxml_is_the_output_the_validation_step_consumes(self):
        assert _DEV.REFERENCE_OUTPUTS[0].endswith(".arxml")

    def test_the_paths_resolve_under_the_reference_project(self, tmp_path):
        project = _DEV.reference_project(tmp_path)
        for path in _DEV.reference_outputs(tmp_path):
            assert path.is_relative_to(project)


# ---------------------------------------------------------------------------
# The removal step itself (scripts/clean_outputs.py)
# ---------------------------------------------------------------------------


class TestCleanOutputsScript:
    """The build's first step: named files removed, nothing else touched."""

    def test_it_removes_the_files_it_is_given(self, tmp_path):
        target = tmp_path / "views" / "report.txt"
        target.parent.mkdir(parents=True)
        target.write_text("report", encoding="utf-8")

        assert _CLEAN.main([str(target)], repo_root=tmp_path) == 0
        assert not target.exists()

    def test_it_moves_the_arxml_aside_and_unlinks_the_rest(self, tmp_path, capsys):
        """The previous ARXML survives under a name no build writes; nothing stays under its own name."""
        arxml = tmp_path / "dist" / "model.arxml"
        report = tmp_path / "views" / "report.txt"
        for path, text in ((arxml, "<ARXML>first</ARXML>"), (report, "report")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")

        assert _CLEAN.main([str(arxml), str(report)], repo_root=tmp_path) == 0

        assert not arxml.exists()
        assert not report.exists()
        assert (tmp_path / "dist" / "model_old.arxml").read_text(encoding="utf-8") == "<ARXML>first</ARXML>"
        assert f"Previous ARXML kept: {tmp_path / 'dist' / 'model_old.arxml'}" in capsys.readouterr().out

    def test_a_second_move_replaces_the_old_file(self, tmp_path):
        arxml = tmp_path / "dist" / "model.arxml"
        arxml.parent.mkdir(parents=True)
        for text in ("<ARXML>first</ARXML>", "<ARXML>second</ARXML>"):
            arxml.write_text(text, encoding="utf-8", newline="\n")
            assert _CLEAN.main([str(arxml)], repo_root=tmp_path) == 0

        assert (tmp_path / "dist" / "model_old.arxml").read_text(encoding="utf-8") == "<ARXML>second</ARXML>"

    def test_a_missing_arxml_leaves_the_old_file_where_it_is(self, tmp_path):
        """The state after a build that failed at `check-model`: the next clean keeps it."""
        old = tmp_path / "dist" / "model_old.arxml"
        old.parent.mkdir(parents=True)
        old.write_text("<ARXML>kept</ARXML>", encoding="utf-8", newline="\n")

        assert _CLEAN.main([str(tmp_path / "dist" / "model.arxml")], repo_root=tmp_path) == 0
        assert old.read_text(encoding="utf-8") == "<ARXML>kept</ARXML>"

    @pytest.mark.parametrize(
        "sample",
        ["dist/model.arxml", "dist/Vehicle.System.arxml", "out/sub/x.arxml", "model", "a.b.c.arxml", "dist/.arxml"],
    )
    def test_the_derivation_equals_the_writers(self, sample, tmp_path):
        """`clean_outputs.py` derives the previous ARXML path exactly as the writer does.

        It is stdlib-only and re-implements the writer's one line; the two copies are held equal so they
        cannot drift unnoticed.
        """
        from swcraft_tools.writer.arxml_xml import previous_arxml_path

        path = tmp_path / sample
        assert _CLEAN.previous_arxml_path(path) == previous_arxml_path(path)

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        """The first build in a fresh checkout has nothing to remove."""
        assert _CLEAN.main([str(tmp_path / "dist" / "model.arxml")], repo_root=tmp_path) == 0

    def test_without_arguments_it_exits_non_zero(self, tmp_path):
        assert _CLEAN.main([], repo_root=tmp_path) == 2

    @pytest.mark.parametrize("escape", ["..", "../sibling/model.arxml", "/etc/hosts"])
    def test_it_refuses_to_leave_the_repository(self, escape, tmp_path):
        """Resolved before the check, so a `..` segment cannot walk out either."""
        with pytest.raises(ValueError):
            _CLEAN.resolve_target(escape, tmp_path)

    def test_it_refuses_a_directory(self, tmp_path):
        """A directory is refused.

        A clean step that recursed into a directory could reach anything kept beside the generated files
        — `dist/audit.jsonl` above all.
        """
        directory = tmp_path / "dist"
        directory.mkdir()

        assert _CLEAN.main([str(directory)], repo_root=tmp_path) == 2
        assert directory.is_dir()

    def test_it_refuses_to_move_a_directory_named_like_an_arxml(self, tmp_path, capsys):
        directory = tmp_path / "dist" / "model.arxml"
        directory.mkdir(parents=True)

        assert _CLEAN.main([str(directory)], repo_root=tmp_path) == 2
        assert "refusing to move a directory" in capsys.readouterr().err
        assert directory.is_dir()
        assert not (tmp_path / "dist" / "model_old.arxml").exists()

    def test_a_move_the_operating_system_refuses_exits_1_and_moves_nothing(self, tmp_path, monkeypatch, capsys):
        arxml = tmp_path / "dist" / "model.arxml"
        arxml.parent.mkdir(parents=True)
        arxml.write_text("<ARXML/>", encoding="utf-8", newline="\n")

        def refuse(self, target):
            raise PermissionError(f"refused: {target}")

        monkeypatch.setattr(Path, "replace", refuse)

        assert _CLEAN.main([str(arxml)], repo_root=tmp_path) == 1
        assert "Error: refused" in capsys.readouterr().err
        assert arxml.read_text(encoding="utf-8") == "<ARXML/>"
        assert not (tmp_path / "dist" / "model_old.arxml").exists()

    def test_an_upper_case_arxml_suffix_is_moved_too(self, tmp_path):
        arxml = tmp_path / "dist" / "MODEL.ARXML"
        arxml.parent.mkdir(parents=True)
        arxml.write_text("<ARXML/>", encoding="utf-8", newline="\n")

        assert _CLEAN.main([str(arxml)], repo_root=tmp_path) == 0
        assert (tmp_path / "dist" / "MODEL_old.ARXML").read_text(encoding="utf-8") == "<ARXML/>"

    def test_an_out_of_repository_path_is_reported_and_nothing_is_removed(self, tmp_path, capsys):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("keep", encoding="utf-8")

        assert _CLEAN.main([str(outside)], repo_root=tmp_path) == 2
        assert "outside the repository" in capsys.readouterr().err
        assert outside.is_file()


# ---------------------------------------------------------------------------
# Output verification
# ---------------------------------------------------------------------------


def _write_outputs(repo_root: Path, *, content: str = "generated") -> None:
    for path in _DEV.reference_outputs(repo_root):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class TestVerifyBuildOutputs:
    def test_passes_when_every_output_is_present_and_non_empty(self, tmp_path):
        _write_outputs(tmp_path)
        _DEV.verify_build_outputs(dev_context(tmp_path))

    @pytest.mark.parametrize("index", range(len(_DEV.REFERENCE_OUTPUTS)))
    def test_a_missing_output_is_reported_by_name(self, tmp_path, index):
        _write_outputs(tmp_path)
        missing = _DEV.reference_outputs(tmp_path)[index]
        missing.unlink()
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.verify_build_outputs(dev_context(tmp_path))
        assert str(missing) in str(excinfo.value)
        assert "missing" in str(excinfo.value)

    @pytest.mark.parametrize("index", range(len(_DEV.REFERENCE_OUTPUTS)))
    def test_an_empty_output_is_reported_by_name(self, tmp_path, index):
        """A generator that exits 0 having written nothing is not a passing build."""
        _write_outputs(tmp_path)
        empty = _DEV.reference_outputs(tmp_path)[index]
        empty.write_text("", encoding="utf-8")
        with pytest.raises(_DEV.DevError) as excinfo:
            _DEV.verify_build_outputs(dev_context(tmp_path))
        assert str(empty) in str(excinfo.value)
        assert "empty" in str(excinfo.value)

    def test_dispatch_fails_when_every_step_passed_but_no_output_was_written(self, tmp_path, capsys):
        make_venv_marker(tmp_path)

        calls: list[list[str]] = []

        def run(argv, cwd, stdin=None, env=()):
            calls.append([str(part) for part in argv])
            return 0

        assert _DEV.main(["--repo-root", str(tmp_path), "build"], run=run) == 1
        assert calls, "the build steps did not run"
        assert "declared outputs" in capsys.readouterr().err

    def test_dispatch_succeeds_when_the_outputs_are_there(self, tmp_path):
        make_venv_marker(tmp_path)
        _write_outputs(tmp_path)
        assert _DEV.main(["--repo-root", str(tmp_path), "build"], run=lambda argv, cwd, stdin, env: 0) == 0


# ---------------------------------------------------------------------------
# Real runs against a copy of the reference project
# ---------------------------------------------------------------------------


class TestRealReferenceBuild:
    """Real runs of the chain; ``generate diagrams`` lays its views out through Node.js."""

    @pytest.fixture(autouse=True)
    def _node(self) -> None:
        requires_node()

    def test_produces_every_output_non_empty(self, tmp_path):
        repo_root = _copy_reference_repo(tmp_path)
        context = _real_context(repo_root)

        log = _execute(_DEV.plan_build(context), env=_offline_env())

        assert [code for _, code, _ in log] == [0] * 7, log
        _DEV.verify_build_outputs(context)
        for path in _DEV.reference_outputs(repo_root):
            assert path.stat().st_size > 0, path

    def test_a_missing_licensed_xsd_is_not_an_error(self, tmp_path):
        """Absence of the licensed schema means a structural fallback, not a failure."""
        repo_root = _copy_reference_repo(tmp_path)
        assert not (repo_root / "schemas").exists()

        log = _execute(_DEV.plan_build(_real_context(repo_root)), env=_offline_env())
        by_label = {label: (code, output) for label, code, output in log}

        provision_code, provision_output = by_label["provision the AUTOSAR XSD (optional)"]
        assert provision_code == 0
        # The script provisions a named schema (default 4.2.2), so the message names it.
        assert "AUTOSAR 4.2.2 XSD not found" in provision_output

        validate_code, validate_output = by_label["validate the generated ARXML"]
        assert validate_code == 0
        assert "structural" in validate_output

    def test_a_broken_model_fails_at_check_model_before_any_generation(self, tmp_path):
        """The model gate runs first, so a failing build writes nothing."""
        repo_root = _copy_reference_repo(tmp_path)
        _seed_build_error(repo_root)

        log = _execute(_DEV.plan_build(_real_context(repo_root)), env=_offline_env())

        assert len(log) == 2, f"a step ran after check-model failed: {_labels_of(log)}"
        assert _labels_of(log)[0] == "remove the previous outputs (examples/basic)"
        label, code, output = log[1]
        assert label == "check-model (examples/basic)"
        assert code != 0
        # The seed's connector must be the ONLY finding: a containment check alone once let the seed
        # fail for six more reasons unnoticed.
        findings = [line.strip() for line in output.splitlines() if "ERROR" in line or "WARNING" in line]
        assert findings == [
            "ERROR: Connector 'EngineCtrl_to_SpeedDisplay' invalid: "
            "Port not found: pEngineSpeed on SWC EngineController"
        ], output

        for path in _DEV.reference_outputs(repo_root):
            assert not path.exists(), f"a failing build left {path} behind"

    def test_a_build_that_breaks_after_a_successful_one_leaves_no_stale_outputs(self, tmp_path):
        """A build that breaks after a successful one leaves no stale outputs.

        The first build succeeds, then the model breaks: without the removal step the previous outputs
        would still be on disk when the rebuild fails, indistinguishable from a passing build.
        """
        repo_root = _copy_reference_repo(tmp_path)
        context = _real_context(repo_root)

        first = _execute(_DEV.plan_build(context), env=_offline_env())
        assert [code for _, code, _ in first] == [0] * 7, first
        outputs = _DEV.reference_outputs(repo_root)
        assert all(path.is_file() for path in outputs)
        (arxml,) = [path for path in outputs if path.suffix == ".arxml"]
        first_arxml = arxml.read_bytes()

        _seed_build_error(repo_root)
        second = _execute(_DEV.plan_build(context), env=_offline_env())

        assert _labels_of(second) == [
            "remove the previous outputs (examples/basic)",
            "check-model (examples/basic)",
        ]
        assert second[-1][1] != 0
        for path in outputs:
            assert not path.exists(), f"a failing rebuild left the previous build's {path} behind"
        # The first step moved the ARXML aside rather than removing it: the last
        # written ARXML -- the first build's, byte for byte -- survives under its `_old` name.
        assert _CLEAN.previous_arxml_path(arxml).read_bytes() == first_arxml

    def test_the_removal_keeps_the_mcp_audit_log(self, tmp_path):
        """The removal step keeps the MCP audit log.

        `dist/audit.jsonl` sits beside the generated ARXML and is the agent's record of every model
        mutation, not build output. The step removes named files only, so no directory walk can reach it.
        """
        repo_root = _copy_reference_repo(tmp_path)
        audit = _DEV.reference_project(repo_root) / "dist" / "audit.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text('{"operation": "create_swc"}\n', encoding="utf-8")

        log = _execute(_DEV.plan_build(_real_context(repo_root))[:1])

        assert [code for _, code, _ in log] == [0], log
        assert audit.read_text(encoding="utf-8") == '{"operation": "create_swc"}\n'

    def test_the_dispatcher_propagates_the_failure(self, tmp_path):
        """Through ``run_steps``, not only through the hand-rolled executor above."""
        repo_root = _copy_reference_repo(tmp_path)
        _seed_build_error(repo_root)
        steps = _DEV.plan_build(_real_context(repo_root))

        assert _DEV.run_steps(steps) != 0
        for path in _DEV.reference_outputs(repo_root):
            assert not path.exists()
