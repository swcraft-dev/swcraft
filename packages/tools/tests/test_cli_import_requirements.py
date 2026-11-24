"""``swcraft import requirements`` through a subprocess: the report, the files, the refusals, the exit codes.

The planner's and the apply's rules are ``test_requirements_import.py``'s; this
module holds the command around them -- what it prints on which stream, what it
leaves on disk and with which exit code (0 on an applied or a dry run, 1 on any
refusal), the usage refusals decided above the pin, and the pin itself. Each case
runs a real ``python -m swcraft_tools.cli`` on a copy of ``examples/basic``, whose
one requirement is the architecture ``REQ_EXAMPLE_001``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, need, needs_document, run_captured
from swcraft_tools import cli
from swcraft_tools._version import __version__
from swcraft_tools.cli import import_commands
from symlink_support import symlink_or_skip

SIBLINGS_SOURCE_SENTENCE = "Error: --source is required.  Run: swcraft import requirements --source FILE --project PATH"


# Helpers


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    """A copy of ``examples/basic`` without its build outputs; its manifest path."""
    project = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    return project / "swcraft.yaml"


def requirement_files(manifest: Path) -> dict[str, str]:
    directory = manifest.parent / "model" / "requirements"
    return {
        path.name: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.yaml"), key=lambda p: p.name)
    }


def project_files(manifest: Path) -> dict[str, bytes]:
    root = manifest.parent
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def write_needs(directory: Path, *needs: dict[str, Any], baseline: str = "SRS-1", name: str = "needs.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(needs_document(*needs, baseline=baseline), indent=2), encoding="utf-8", newline="\n")
    return path


def run(*argv: str, cwd: Path, console: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the CLI in UTF-8 mode, or with *console* as the inherited stream encoding and no UTF-8 mode.

    Either way the output is decoded as UTF-8: ``main`` re-encodes both streams first
    (``env.force_utf8_streams``), which is what the *console* cases hold.
    """
    if console is None:
        env = {**os.environ, "PYTHONUTF8": "1"}
    else:
        env = {key: value for key, value in os.environ.items() if key != "PYTHONUTF8"}
        env.update(PYTHONUTF8="0", PYTHONIOENCODING=console)
    return run_captured([sys.executable, "-m", "swcraft_tools.cli", *argv], cwd, env=env)


def import_requirements(
    manifest: Path, source: Path | str, *extra: str, console: str | None = None
) -> subprocess.CompletedProcess[str]:
    return run(
        "import",
        "requirements",
        "--source",
        str(source),
        "--project",
        str(manifest),
        *extra,
        cwd=manifest.parent,
        console=console,
    )


SWR_12 = (
    "id: SWR-12\n"
    "title: Title of SWR-12\n"
    "description: Text of SWR-12.\n"
    "req_type: req\n"
    "version: 1\n"
    "source: external\n"
    "derived_from:\n"
    "- SG-1@1\n"
    "baseline: SRS-1\n"
)

SG_1_STUB = (
    "id: SG-1\n"
    "title: SG-1\n"
    "description: Placeholder - named as a parent by SWR-12, not in the import.\n"
    "req_type: req\n"
    "version: 1\n"
    "source: external\n"
    "stub: true\n"
)

APPLIED_REPORT = (
    "Requirements import: baseline SRS-1 (from the file)\n"
    "Mapping: none (SWCraft's own names)\n"
    "Summary: 1 added, 0 changed, 0 removed, 1 stubs created, 0 source updates\n"
    "\n"
    "Added:\n"
    "  SWR-12 (v1)\n"
    "\n"
    "Stubs created:\n"
    "  SG-1 (v1), named by SWR-12\n"
)


# The runs that succeed


class TestARunThatIsNotRefused:
    def test_a_dry_run_prints_the_report_writes_nothing_and_exits_0(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))
        before = project_files(manifest)

        completed = import_requirements(manifest, source, "--dry-run")

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == APPLIED_REPORT.replace("Summary:", "Dry run: nothing was written.\nSummary:", 1)
        assert completed.stderr == ""
        assert project_files(manifest) == before

    def test_an_applied_run_writes_the_files_and_no_audit_record_and_exits_0(
        self, manifest: Path, tmp_path: Path
    ) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))
        before = project_files(manifest)

        completed = import_requirements(manifest, source)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == APPLIED_REPORT
        assert completed.stderr == ""
        assert requirement_files(manifest) == {
            "REQ_EXAMPLE_001.yaml": before["model/requirements/REQ_EXAMPLE_001.yaml"].decode("utf-8"),
            "SG-1.yaml": SG_1_STUB,
            "SWR-12.yaml": SWR_12,
        }
        after = project_files(manifest)
        assert set(after) - set(before) == {"model/requirements/SG-1.yaml", "model/requirements/SWR-12.yaml"}
        assert not any(path.endswith("audit.jsonl") for path in after)
        assert not any(path.endswith(".tmp") for path in after)

    def test_a_second_identical_run_changes_nothing(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))
        assert import_requirements(manifest, source).returncode == 0
        before = project_files(manifest)

        completed = import_requirements(manifest, source)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (
            "Requirements import: baseline SRS-1 (from the file)\n"
            "Mapping: none (SWCraft's own names)\n"
            "Summary: 0 added, 0 changed, 0 removed, 0 stubs created, 0 source updates\n"
            "No change.\n"
        )
        assert project_files(manifest) == before

    @pytest.mark.parametrize("dry_run", [False, True], ids=["applied", "dry"])
    def test_report_writes_the_printed_report(self, manifest: Path, tmp_path: Path, dry_run: bool) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))
        report = tmp_path / "report.txt"

        completed = import_requirements(manifest, source, "--report", str(report), *(["--dry-run"] if dry_run else []))

        assert completed.returncode == 0, completed.stderr
        assert report.read_text(encoding="utf-8") == completed.stdout

    @pytest.mark.parametrize("dry_run", [True, False])
    @pytest.mark.parametrize("name", ["report.md", "Report.Md"])
    def test_a_markdown_report_path_gets_the_markdown_form(
        self, manifest: Path, tmp_path: Path, dry_run: bool, name: str
    ) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))
        report = tmp_path / name

        completed = import_requirements(manifest, source, "--report", str(report), *(["--dry-run"] if dry_run else []))

        assert completed.returncode == 0, completed.stderr
        markdown = report.read_bytes().decode("utf-8")
        assert markdown.startswith(
            "# Requirements import\\: baseline SRS-1 (from the file)\n\nMapping\\: none (SWCraft's own names)\n"
        )
        assert "\n**Summary\\: 1 added, 0 changed, 0 removed, 1 stubs created, 0 source updates**\n" in markdown
        assert "\n## Added\n\n- SWR-12 (v1)\n" in markdown
        assert ("Dry run\\: nothing was written\\." in markdown) is dry_run
        assert "\r" not in markdown
        assert completed.stdout.startswith("Requirements import: baseline SRS-1 (from the file)\n"), (
            "stdout keeps the text"
        )

    def test_a_second_run_changes_one_requirement_and_removes_another(self, manifest: Path, tmp_path: Path) -> None:
        first = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]), name="first.json")
        assert import_requirements(manifest, first).returncode == 0
        second = write_needs(
            tmp_path,
            need("SWR-12", content="The new text.", derived_from=["SG-1"]),
            need("SG-1", status="rejected"),
            name="second.json",
        )

        completed = import_requirements(manifest, second)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (
            "Requirements import: baseline SRS-1 (from the file)\n"
            "Mapping: none (SWCraft's own names)\n"
            "Summary: 0 added, 1 changed, 1 removed, 0 stubs created, 0 source updates\n"
            "\n"
            "Changed:\n"
            "  SWR-12 v1 → v2 (description)\n"
            "    text: numbers removed 12; terms added: new; terms removed: swr\n"
            "\n"
            "Removed:\n"
            "  SG-1 (was v1; status 'rejected')\n"
            "\n"
            "Links now naming no requirement:\n"
            "  Requirement 'SWR-12' derives from 'SG-1@1'\n"
        )
        files = requirement_files(manifest)
        assert "SG-1.yaml" not in files
        assert files["SWR-12.yaml"] == SWR_12.replace("Text of SWR-12.", "The new text.").replace(
            "version: 1", "version: 2"
        )

    def test_a_later_delivery_is_a_complete_set(self, manifest: Path, tmp_path: Path) -> None:
        first = write_needs(
            tmp_path, need("SWR-12", derived_from=["SG-1"]), need("SWR-13"), need("SG-1"), name="first.json"
        )
        assert import_requirements(manifest, first).returncode == 0
        second = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]), name="second.json")

        completed = import_requirements(manifest, second)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (
            "Requirements import: baseline SRS-1 (from the file)\n"
            "Mapping: none (SWCraft's own names)\n"
            "Summary: 0 added, 1 changed, 1 removed, 0 stubs created, 0 source updates\n"
            "\n"
            "Changed:\n"
            "  SG-1 v1 → v2 (replaced by a stub: absent from this delivery)\n"
            "\n"
            "Removed:\n"
            "  SWR-13 (was v1; absent from this delivery)\n"
            "\n"
            "Parent versions updated:\n"
            "  SWR-12 (SG-1@2)\n"
        )
        files = requirement_files(manifest)
        assert set(files) == {"REQ_EXAMPLE_001.yaml", "SG-1.yaml", "SWR-12.yaml"}
        assert files["SG-1.yaml"] == SG_1_STUB.replace("version: 1", "version: 2")
        assert files["SWR-12.yaml"] == SWR_12.replace("SG-1@1", "SG-1@2")


class TestAConsoleThatIsNotUtf8:
    """A Windows console or a redirected stream in cp1252, without Python's UTF-8 mode."""

    def test_a_changed_requirement_is_reported_and_the_run_exits_0(self, manifest: Path, tmp_path: Path) -> None:
        first = write_needs(tmp_path, need("SWR-12"), name="first.json")
        assert import_requirements(manifest, first, console="cp1252").returncode == 0
        second = write_needs(tmp_path, need("SWR-12", content="The new text."), name="second.json")

        completed = import_requirements(manifest, second, console="cp1252")

        assert completed.returncode == 0, completed.stderr
        assert "  SWR-12 v1 → v2 (description)\n" in completed.stdout
        assert "version: 2\n" in requirement_files(manifest)["SWR-12.yaml"]

    def test_an_id_the_console_cannot_encode_is_printed_and_written_as_utf8(
        self, manifest: Path, tmp_path: Path
    ) -> None:
        source = write_needs(tmp_path, need("SWR-\u03a9"))
        report = tmp_path / "report.txt"

        completed = import_requirements(manifest, source, "--report", str(report), console="cp1252")

        assert completed.returncode == 0, completed.stderr
        assert "Added:\n  SWR-\u03a9 (v1)\n" in completed.stdout
        assert "SWR-\u03a9.yaml" in requirement_files(manifest)
        assert "  SWR-\u03a9 (v1)\n" in report.read_text(encoding="utf-8")


# Refusals: every one exits 1 and writes nothing


class TestARefusedRun:
    def _refused(self, manifest: Path, source: Path | str, *extra: str) -> str:
        before = project_files(manifest)
        completed = import_requirements(manifest, source, *extra)
        assert completed.returncode == 1, completed.stdout
        assert completed.stdout == ""
        assert project_files(manifest) == before
        return completed.stderr

    def test_a_source_that_does_not_exist_is_named_as_given(self, manifest: Path) -> None:
        error = self._refused(manifest, "no-such-needs.json")
        assert error.startswith("Error: no-such-needs.json: cannot read the file"), error

    def test_a_file_that_is_not_json_is_refused(self, manifest: Path, tmp_path: Path) -> None:
        source = tmp_path / "broken.json"
        source.write_text("{not json", encoding="utf-8", newline="\n")
        error = self._refused(manifest, source)
        assert error.startswith(f"Error: {source}: "), error

    def test_a_profile_violation_names_its_json_key_path(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-1", version=3))
        error = self._refused(manifest, source)
        assert error.startswith(f"Error: {source}: /versions/SRS-1/needs/SWR-1/version: "), error

    def test_an_id_an_architecture_requirement_holds_is_refused_and_named(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("REQ_EXAMPLE_001"), need("SWR-1"))
        error = self._refused(manifest, source)
        assert error == (
            "Error: the requirements import is refused and nothing was written:\n"
            "  'REQ_EXAMPLE_001' is the id of an architecture requirement: the import changes only external "
            "requirements, and that requirement is left untouched\n"
        )

    def test_a_dry_run_is_refused_the_same_way(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("REQ_EXAMPLE_001"))
        error = self._refused(manifest, source, "--dry-run")
        assert "'REQ_EXAMPLE_001' is the id of an architecture requirement" in error

    def test_no_refusal_names_the_project_s_absolute_path(self, manifest: Path, tmp_path: Path) -> None:
        # The two-files refusal is the one that names model files: by their model-root-relative names.
        requirements = manifest.parent / "model" / "requirements"
        (requirements / "SWR-1-copy.yaml").write_text(
            "id: SWR-1\ntitle: T\ndescription: D.\nreq_type: req\nversion: 1\nsource: external\n",
            encoding="utf-8",
            newline="\n",
        )
        (requirements / "SWR-1.yaml").write_text(
            "id: SWR-1\ntitle: T\ndescription: D.\nreq_type: req\nversion: 1\nsource: external\n",
            encoding="utf-8",
            newline="\n",
        )
        source = write_needs(tmp_path, need("SWR-1"))
        error = self._refused(manifest, source)
        assert "(requirements/SWR-1-copy.yaml, requirements/SWR-1.yaml)" in error
        assert str(manifest.parent) not in error
        assert manifest.parent.as_posix() not in error

    def test_a_baseline_no_report_file_could_hold_is_refused_before_a_removal_commits(
        self, manifest: Path, tmp_path: Path
    ) -> None:
        # A rejection-only import writes nothing the read-back would see, so the reader is what
        # keeps a lone surrogate out of the report: refused, the removal never made, the old
        # --report file left as it was.
        assert import_requirements(manifest, write_needs(tmp_path, need("SWR-1"))).returncode == 0
        report = tmp_path / "report.txt"
        report.write_text("the previous report\n", encoding="utf-8", newline="\n")
        source = write_needs(tmp_path, need("SWR-1", status="rejected"), baseline="SRS-\ud800", name="reject.json")
        error = self._refused(manifest, source, "--report", str(report))
        assert error.startswith(f"Error: {source}: /current_version: holds a lone surrogate (U+D800)"), error
        assert report.read_text(encoding="utf-8") == "the previous report\n"

    def test_a_removal_whose_report_cannot_be_rendered_is_refused_before_it_commits(
        self, manifest: Path, tmp_path: Path
    ) -> None:
        # A hexadecimal `version` of 4,000 digits is an integer YAML and the model accept and no
        # decimal string can hold (CPython's 4,300-digit limit): the report is rendered before the
        # apply, so the removal is never made.
        stored = manifest.parent / "model" / "requirements" / "SWR-1.yaml"
        stored.write_text(
            "id: SWR-1\ntitle: T\ndescription: D.\nreq_type: req\nversion: 0x" + "f" * 4000 + "\nsource: external\n",
            encoding="utf-8",
            newline="\n",
        )
        report = tmp_path / "report.txt"
        source = write_needs(tmp_path, need("SWR-1", status="rejected"))
        error = self._refused(manifest, source, "--report", str(report))
        assert error.startswith("Error: the import's report cannot be rendered, so nothing was written ("), error
        assert "Traceback" not in error
        assert stored.exists()
        assert not report.exists()

    @pytest.mark.parametrize("name", ["report.txt", "report.md"])
    def test_a_refused_run_writes_no_report(self, manifest: Path, tmp_path: Path, name: str) -> None:
        source = write_needs(tmp_path, need("REQ_EXAMPLE_001"))
        report = tmp_path / name
        self._refused(manifest, source, "--report", str(report))
        assert not report.exists()

    def test_a_report_directory_that_does_not_exist_is_refused_before_anything_is_written(
        self, manifest: Path, tmp_path: Path
    ) -> None:
        source = write_needs(tmp_path, need("SWR-1"))
        report = tmp_path / "absent" / "report.txt"
        error = self._refused(manifest, source, "--report", str(report))
        assert error == f"Error: --report {report}: the directory {report.parent} does not exist.\n"


class TestAFailedApply:
    """The report is rendered before the apply and printed after it: a failure of either prints none (in process)."""

    def test_a_failed_apply_exits_1_prints_no_report_and_writes_nothing(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))
        report = tmp_path / "report.txt"
        before = project_files(manifest)

        def replace(src: Any, dst: Any) -> None:
            raise OSError("injected rename failure")

        monkeypatch.setattr(os, "replace", replace)
        argv = ["swcraft", "import", "requirements", "--source", str(source), "--project", str(manifest)]
        monkeypatch.setattr("sys.argv", [*argv, "--report", str(report)])
        with pytest.raises(SystemExit) as caught:
            cli.main()
        monkeypatch.undo()

        assert caught.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("Error: ") and "injected rename failure" in captured.err
        assert project_files(manifest) == before
        assert not report.exists()

    @pytest.mark.parametrize(
        ("name", "renderer"), [("report.txt", "render_report"), ("report.md", "render_report_markdown")]
    )
    def test_a_report_that_cannot_be_rendered_refuses_before_the_apply(
        self,
        manifest: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        name: str,
        renderer: str,
    ) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))
        report = tmp_path / name
        before = project_files(manifest)

        def render(plan: Any, *, dry_run: bool, mapping: str | None) -> str:
            raise ValueError("injected render failure")

        monkeypatch.setattr(import_commands, renderer, render)
        argv = ["swcraft", "import", "requirements", "--source", str(source), "--project", str(manifest)]
        monkeypatch.setattr("sys.argv", [*argv, "--report", str(report)])
        with pytest.raises(SystemExit) as caught:
            cli.main()
        monkeypatch.undo()

        assert caught.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == (
            "Error: the import's report cannot be rendered, so nothing was written (injected render failure).\n"
        )
        assert project_files(manifest) == before
        assert not report.exists()

    @pytest.mark.parametrize("dry_run", [False, True], ids=["applied", "dry"])
    @pytest.mark.parametrize("name", ["report.txt", "report.md"])
    def test_a_report_that_cannot_be_written_after_the_run_says_the_run_completed(
        self,
        manifest: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        name: str,
        dry_run: bool,
    ) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))
        report = tmp_path / name
        real_write_text = Path.write_text

        def write_text(self: Path, *args: Any, **kwargs: Any) -> int:
            if self == report:
                raise OSError(28, "No space left on device")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write_text)
        argv = ["swcraft", "import", "requirements", "--source", str(source), "--project", str(manifest)]
        monkeypatch.setattr("sys.argv", [*argv, "--report", str(report), *(["--dry-run"] if dry_run else [])])
        with pytest.raises(SystemExit) as caught:
            cli.main()
        monkeypatch.undo()

        assert caught.value.code == 1
        captured = capsys.readouterr()
        assert captured.out.startswith("Requirements import: baseline SRS-1 (from the file)\n")
        done = "the dry run completed" if dry_run else "the import was applied"
        assert captured.err == (
            f"Error: {done} and its report printed, but --report {report} could not be written "
            "(No space left on device).\n"
        )
        assert ("SWR-12.yaml" in requirement_files(manifest)) is not dry_run
        assert not report.exists()

    def test_a_restore_that_fails_is_named_on_stderr_by_its_storage_name(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = write_needs(tmp_path, need("SWR-12"), need("SWR-13"))
        real_replace, real_unlink = os.replace, Path.unlink
        replaced: list[Path] = []

        def replace(src: Any, dst: Any) -> None:
            if replaced:  # the first rename lands, the second fails: the rollback has one to undo
                raise OSError("injected rename failure")
            real_replace(src, dst)
            replaced.append(Path(dst))

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self in replaced:
                raise PermissionError("injected restore failure")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(Path, "unlink", unlink)
        argv = ["swcraft", "import", "requirements", "--source", str(source), "--project", str(manifest)]
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as caught:
            cli.main()
        monkeypatch.undo()

        assert caught.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        unrestored = replaced[0].relative_to(manifest.parent / "model").as_posix()
        assert captured.err.splitlines() == [
            "Error: injected rename failure",
            f"the rollback could not restore: {unrestored}",
        ]
        assert str(manifest.parent) not in captured.err


# Usage refusals, decided above the pin


def _pin(manifest: Path, version: str) -> None:
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text + f'toolchain_version: "{version}"\n', encoding="utf-8", newline="\n")


class TestTheUsageRefusals:
    @pytest.mark.parametrize(
        ("flags", "sentence"),
        [
            (["--packages", "P"], "Error: --packages applies only to 'import model'.\n"),
            (["--force"], "Error: --force applies only to 'import model'.\n"),
            (["--verbose"], "Error: --verbose applies only to 'import model'.\n"),
            (["--update"], "Error: --update applies only to 'import component'.\n"),
            (["--replace-access-lists"], "Error: --replace-access-lists applies only to 'import component'.\n"),
        ],
        ids=["packages", "force", "verbose", "update", "replace-access-lists"],
    )
    def test_each_foreign_flag_is_refused_by_name_above_the_pin(
        self, manifest: Path, tmp_path: Path, flags: list[str], sentence: str
    ) -> None:
        source = write_needs(tmp_path, need("SWR-1"))
        _pin(manifest, "9.9.9")
        before = project_files(manifest)

        completed = import_requirements(manifest, source, *flags)

        assert completed.returncode == 1
        assert completed.stderr == sentence
        assert project_files(manifest) == before

    def test_a_missing_source_is_refused_with_the_siblings_sentence_above_the_pin(self, manifest: Path) -> None:
        _pin(manifest, "9.9.9")
        completed = run("import", "requirements", "--project", str(manifest), cwd=manifest.parent)
        assert completed.returncode == 1
        assert completed.stderr == SIBLINGS_SOURCE_SENTENCE + "\n"

    def test_a_missing_project_is_refused(self, tmp_path: Path) -> None:
        completed = run("import", "requirements", "--source", "needs.json", cwd=tmp_path)
        assert completed.returncode == 1
        assert completed.stderr == "Error: --project is required.  Run: swcraft import requirements --project PATH\n"


class TestThePin:
    @pytest.mark.parametrize("dry_run", [False, True], ids=["applied", "dry"])
    def test_a_mismatched_pin_refuses_dry_run_included(self, manifest: Path, tmp_path: Path, dry_run: bool) -> None:
        source = write_needs(tmp_path, need("SWR-1"))
        _pin(manifest, "9.9.9")
        before = project_files(manifest)

        completed = import_requirements(manifest, source, *(["--dry-run"] if dry_run else []))

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert completed.stderr.startswith("Error: this project's swcraft.yaml pins toolchain_version 9.9.9")
        assert __version__ in completed.stderr
        assert project_files(manifest) == before

    def test_a_matching_pin_imports(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-1"))
        _pin(manifest, __version__)

        completed = import_requirements(manifest, source)

        assert completed.returncode == 0, completed.stderr
        assert "SWR-1.yaml" in requirement_files(manifest)


# --mapping and --baseline

#: The committed Sphinx-Needs 9.2.0 build and the mapping beside it.
SPHINX_BUILD = (
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "requirements-interchange" / "sphinx-build"
)

SPHINX_REPORT = (
    "Requirements import: baseline SYS-2.1 (from the file)\n"
    "Mapping: team.needs-mapping.yaml\n"
    "Not imported: 1 need of a type the mapping ignores (test)\n"
    "Summary: 4 added, 0 changed, 0 removed, 0 stubs created, 0 source updates\n"
    "\n"
    "Added:\n"
    "  SW_001 (v1)\n"
    "  SW_002 (v1)\n"
    "  SW_003 (v1)\n"
    "  SYS_001 (v1)\n"
    "\n"
    "Removed by status, not in the model:\n"
    "  SYS_002 (status 'obsolete')\n"
)

SW_002 = (
    "id: SW_002\n"
    "title: Brake request plausibility\n"
    "description: The software shall reject a brake request outside 0 to 100 %.\n"
    "req_type: req\n"
    "version: 1\n"
    "source: external\n"
    "derived_from:\n"
    "- SW_001@1\n"
    "- SYS_001@1\n"
    "document: index\n"
    "baseline: SYS-2.1\n"
)


def _with_mapping(manifest: Path) -> str:
    """The team's mapping copied next to ``swcraft.yaml``, as the guide recommends; its name as spelled."""
    shutil.copyfile(SPHINX_BUILD / "team.needs-mapping.yaml", manifest.parent / "team.needs-mapping.yaml")
    return "team.needs-mapping.yaml"


class TestTheMappingAndTheBaseline:
    def test_the_sphinx_build_through_its_mapping_as_a_dry_run(self, manifest: Path) -> None:
        mapping = _with_mapping(manifest)
        before = project_files(manifest)

        completed = import_requirements(manifest, SPHINX_BUILD / "needs.json", "--mapping", mapping, "--dry-run")

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == SPHINX_REPORT.replace("Summary:", "Dry run: nothing was written.\nSummary:", 1)
        assert completed.stderr == ""
        assert project_files(manifest) == before

    def test_the_sphinx_build_through_its_mapping_applied(self, manifest: Path) -> None:
        mapping = _with_mapping(manifest)

        completed = import_requirements(manifest, SPHINX_BUILD / "needs.json", "--mapping", mapping)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == SPHINX_REPORT
        files = requirement_files(manifest)
        assert set(files) == {"REQ_EXAMPLE_001.yaml", "SW_001.yaml", "SW_002.yaml", "SW_003.yaml", "SYS_001.yaml"}
        assert files["SW_002.yaml"] == SW_002
        assert all("baseline: SYS-2.1\n" in files[name] for name in set(files) - {"REQ_EXAMPLE_001.yaml"})

    def test_baseline_overrides_a_non_empty_current_version(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))

        completed = import_requirements(manifest, source, "--baseline", "abc123")

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.splitlines()[0] == "Requirements import: baseline abc123"
        files = requirement_files(manifest)
        assert files["SWR-12.yaml"] == SWR_12.replace("baseline: SRS-1", "baseline: abc123")
        assert files["SG-1.yaml"] == SG_1_STUB

    def test_the_file_s_version_is_used_without_the_flag(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-12"), baseline="SRS-7")

        completed = import_requirements(manifest, source)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.splitlines()[0] == "Requirements import: baseline SRS-7 (from the file)"
        assert "baseline: SRS-7\n" in requirement_files(manifest)["SWR-12.yaml"]

    def test_a_baseline_only_move_is_a_source_update_with_no_bump(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-12", derived_from=["SG-1"]))
        assert import_requirements(manifest, source).returncode == 0

        completed = import_requirements(manifest, source, "--baseline", "abc123")

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (
            "Requirements import: baseline abc123\n"
            "Mapping: none (SWCraft's own names)\n"
            "Summary: 0 added, 0 changed, 0 removed, 0 stubs created, 1 source updates\n"
            "\n"
            "Source information updated:\n"
            "  SWR-12 (baseline)\n"
        )
        assert requirement_files(manifest)["SWR-12.yaml"] == SWR_12.replace("baseline: SRS-1", "baseline: abc123")

    def test_an_empty_current_version_with_the_flag_imports(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-12"), baseline="")

        completed = import_requirements(manifest, source, "--baseline", "abc123")

        assert completed.returncode == 0, completed.stderr
        assert "baseline: abc123\n" in requirement_files(manifest)["SWR-12.yaml"]


def _mapping_in(directory: Path) -> Path:
    """The team's mapping copied into *directory*; its absolute path."""
    directory.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copyfile(SPHINX_BUILD / "team.needs-mapping.yaml", directory / "team.needs-mapping.yaml"))


class TestTheMappingAsTheReportNamesIt:
    """The report's ``Mapping:`` line: a file inside the project relative to it, any other as spelled."""

    @pytest.mark.parametrize("dry_run", [False, True], ids=["applied", "dry"])
    def test_an_absolute_spelling_inside_the_project_is_named_relative_to_it_in_both_reports(
        self, manifest: Path, tmp_path: Path, dry_run: bool
    ) -> None:
        mapping = _mapping_in(manifest.parent / "conf")
        report = tmp_path / "report.md"

        completed = import_requirements(
            manifest,
            SPHINX_BUILD / "needs.json",
            "--mapping",
            str(mapping),
            "--report",
            str(report),
            *(["--dry-run"] if dry_run else []),
        )

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.splitlines()[1] == "Mapping: conf/team.needs-mapping.yaml"
        assert report.read_text(encoding="utf-8").split("\n")[2] == "Mapping\\: conf\\/team\\.needs-mapping\\.yaml"

    def test_a_relative_spelling_inside_the_project_is_named_relative_to_the_project(self, manifest: Path) -> None:
        mapping = _mapping_in(manifest.parent / "conf")
        # Spelled through the parent folder, relative to the run's working directory (`import_requirements` runs
        # in the manifest's folder), whatever the fixture names that folder.
        cwd = manifest.parent
        spelled = Path(os.path.relpath(cwd.parent, cwd), cwd.name, os.path.relpath(mapping, cwd)).as_posix()

        completed = import_requirements(manifest, SPHINX_BUILD / "needs.json", "--mapping", spelled, "--dry-run")

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.splitlines()[1] == "Mapping: conf/team.needs-mapping.yaml"

    def test_a_link_inside_the_project_to_a_mapping_outside_is_named_where_it_is(
        self, manifest: Path, tmp_path: Path
    ) -> None:
        outside = _mapping_in(tmp_path / "shared")
        link = manifest.parent / "conf" / "team.needs-mapping.yaml"
        link.parent.mkdir()
        symlink_or_skip(link, outside)

        completed = import_requirements(manifest, SPHINX_BUILD / "needs.json", "--mapping", str(link), "--dry-run")

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.splitlines()[1] == "Mapping: conf/team.needs-mapping.yaml"

    def test_a_mapping_outside_the_project_is_named_as_spelled(self, manifest: Path, tmp_path: Path) -> None:
        mapping = _mapping_in(tmp_path / "shared")

        completed = import_requirements(manifest, SPHINX_BUILD / "needs.json", "--mapping", str(mapping), "--dry-run")

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.splitlines()[1] == f"Mapping: {mapping}"


class TestTheMappingAndTheBaselineRefused:
    def _refused(self, manifest: Path, source: Path | str, *extra: str) -> str:
        before = project_files(manifest)
        completed = import_requirements(manifest, source, *extra)
        assert completed.returncode == 1, completed.stdout
        assert completed.stdout == ""
        assert project_files(manifest) == before
        return completed.stderr

    def test_a_blank_baseline(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-1"))
        assert self._refused(manifest, source, "--baseline", " ") == "Error: --baseline must not be blank.\n"

    def test_a_baseline_holding_a_lone_surrogate(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # In process: the value POSIX argv decodes from an undecodable byte, on every platform.
        source = write_needs(tmp_path, need("SWR-1"))
        before = project_files(manifest)
        argv = ["swcraft", "import", "requirements", "--source", str(source), "--project", str(manifest)]
        monkeypatch.setattr("sys.argv", [*argv, "--baseline", "abc\udcff"])
        with pytest.raises(SystemExit) as caught:
            cli.main()
        monkeypatch.undo()

        assert caught.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ("Error: --baseline holds a lone surrogate (U+DCFF), which no UTF-8 text can carry.\n")
        assert project_files(manifest) == before

    @pytest.mark.parametrize("name", ["", "  "], ids=["empty", "blank"])
    def test_an_empty_current_version_without_the_flag(self, manifest: Path, tmp_path: Path, name: str) -> None:
        # A blank name records nothing, as a blank --baseline and the panel's box: the same refusal.
        source = write_needs(tmp_path, need("SWR-1"), baseline=name)
        assert self._refused(manifest, source) == (
            f"Error: {source}: the delivery records its version: pass --baseline (the requirements team's "
            "commit or baseline); the file's current_version is empty.\n"
        )

    def test_a_refused_mapping_names_the_file_as_spelled_and_the_yaml_key_path(
        self, manifest: Path, tmp_path: Path
    ) -> None:
        source = write_needs(tmp_path, need("SWR-1"))
        (manifest.parent / "bad.needs-mapping.yaml").write_text(
            "fields:\n  asill: safety\n", encoding="utf-8", newline="\n"
        )
        error = self._refused(manifest, source, "--mapping", "bad.needs-mapping.yaml")
        assert error.startswith("Error: bad.needs-mapping.yaml: fields/asill: "), error
        assert len(error.splitlines()) == 1

    def test_a_missing_mapping_file(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-1"))
        error = self._refused(manifest, source, "--mapping", "absent.needs-mapping.yaml")
        assert error.startswith("Error: absent.needs-mapping.yaml: cannot read the file ("), error

    @pytest.mark.parametrize(
        "spelled",
        [
            "model/team.needs-mapping.yaml",
            "model/requirements/team.needs-mapping.yaml",
            "x/../model/team.needs-mapping.yaml",
        ],
    )
    def test_a_mapping_inside_the_model_root_is_refused_before_it_is_read(self, manifest: Path, spelled: str) -> None:
        # The file does not exist: the place alone refuses it, before any read could fail.
        error = self._refused(manifest, SPHINX_BUILD / "needs.json", "--mapping", spelled)
        assert error == (
            f"Error: {spelled}: the mapping file lies inside the model root, "
            "where the model's loader would read it as an element.\n"
        )

    def test_a_mapping_reached_through_a_symlink_into_the_model_root_is_refused(self, manifest: Path) -> None:
        symlink_or_skip(manifest.parent / "linked", manifest.parent / "model")
        spelled = "linked/team.needs-mapping.yaml"
        error = self._refused(manifest, SPHINX_BUILD / "needs.json", "--mapping", spelled)
        assert error == (
            f"Error: {spelled}: the mapping file lies inside the model root, "
            "where the model's loader would read it as an element.\n"
        )

    def test_a_mapping_beside_the_manifest_is_not_inside_the_model_root(self, manifest: Path) -> None:
        completed = import_requirements(
            manifest, SPHINX_BUILD / "needs.json", "--mapping", _with_mapping(manifest), "--dry-run"
        )
        assert completed.returncode == 0, completed.stderr


class TestTheRequirementsFlagsOnTheSiblings:
    @pytest.mark.parametrize("form", ["model", "component"])
    @pytest.mark.parametrize(
        ("flag", "value"),
        [("--mapping", "m.needs-mapping.yaml"), ("--baseline", "abc"), ("--mapping", ""), ("--baseline", "")],
    )
    def test_each_is_refused_by_name_above_the_pin(self, manifest: Path, form: str, flag: str, value: str) -> None:
        _pin(manifest, "9.9.9")
        before = project_files(manifest)

        completed = run(
            "import", form, "--source", "x.arxml", "--project", str(manifest), flag, value, cwd=manifest.parent
        )

        assert completed.returncode == 1
        assert completed.stderr == f"Error: {flag} applies only to 'import requirements'.\n"
        assert project_files(manifest) == before


class TestTheOrder:
    def test_a_blank_baseline_is_refused_before_the_pin(self, manifest: Path, tmp_path: Path) -> None:
        source = write_needs(tmp_path, need("SWR-1"))
        _pin(manifest, "9.9.9")
        completed = import_requirements(manifest, source, "--baseline", "")
        assert completed.returncode == 1
        assert completed.stderr == "Error: --baseline must not be blank.\n"

    @pytest.mark.parametrize("spelled", ["model/team.needs-mapping.yaml", "absent.needs-mapping.yaml"])
    def test_the_pin_is_decided_before_the_mapping(self, manifest: Path, tmp_path: Path, spelled: str) -> None:
        source = write_needs(tmp_path, need("SWR-1"))
        _pin(manifest, "9.9.9")
        completed = import_requirements(manifest, source, "--mapping", spelled)
        assert completed.returncode == 1
        assert completed.stderr.startswith("Error: this project's swcraft.yaml pins toolchain_version 9.9.9")

    def test_the_mapping_is_read_before_the_source(self, manifest: Path) -> None:
        completed = import_requirements(manifest, "no-such-needs.json", "--mapping", "absent.needs-mapping.yaml")
        assert completed.returncode == 1
        assert completed.stderr.startswith("Error: absent.needs-mapping.yaml: cannot read the file ("), completed.stderr

    def test_the_source_is_read_before_the_baseline_is_decided(self, manifest: Path, tmp_path: Path) -> None:
        source = tmp_path / "broken.json"
        source.write_text('{"current_version": ""', encoding="utf-8", newline="\n")
        completed = import_requirements(manifest, source)
        assert completed.returncode == 1
        assert "not a JSON document" in completed.stderr
