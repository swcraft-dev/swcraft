"""``swcraft change-report`` on the command line: the write, stdout, the exit codes.

The facts of the report are ``test_change_report.py``'s; this module holds what the
command adds around them -- where the files go, what stdout and stderr carry, when
the run exits 1 and that nothing is written then, and that the files of an earlier
run are replaced. Every run passes ``--no-diagrams`` (the pictures need Node.js and
are ``test_change_report.py``'s and ``test_diagram_change_marks.py``'s), except the
one that says why a picture was not drawn.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from swcraft_tools.cli import main
from test_change_report import build_project


def _run(argv: list[str], monkeypatch, capsys) -> tuple[int, str, str]:
    """Run ``swcraft change-report ...`` in process: ``(exit code, stdout, stderr)``, 0 when it returned."""
    monkeypatch.setattr("sys.argv", ["swcraft", "change-report", *argv])
    try:
        main()
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _report_dir(manifest: Path) -> Path:
    return manifest.parent / "dist" / "change-report"


def test_a_run_writes_the_report_and_prints_its_resolved_path(tmp_path, monkeypatch, capsys) -> None:
    manifest = build_project(tmp_path)

    code, out, err = _run(["--project", str(manifest), "--no-diagrams"], monkeypatch, capsys)

    directory = _report_dir(manifest).resolve()
    assert code == 0, err
    assert out == f"Generated report: {directory / 'change-report.md'}\n"
    assert sorted(path.name for path in directory.iterdir()) == [
        "change-report-pr.md",
        "change-report.json",
        "change-report.md",
    ]
    assert (directory / "change-report.md").read_text(encoding="utf-8").startswith("# Change Report\n")
    pr = (directory / "change-report-pr.md").read_text(encoding="utf-8")
    assert pr.startswith("# Change Report\n")
    assert "## Diagrams" not in pr and ".svg" not in pr


def test_json_prints_the_document_it_writes(tmp_path, monkeypatch, capsys) -> None:
    manifest = build_project(tmp_path)

    code, out, _err = _run(["--project", str(manifest), "--no-diagrams", "--format", "json"], monkeypatch, capsys)

    written = (_report_dir(manifest) / "change-report.json").read_text(encoding="utf-8")
    assert code == 0
    assert out == written
    document = json.loads(out)
    assert document["schemaVersion"] == 1
    assert document["kind"] == "change-report"
    assert document["diagrams"] is None


def test_fail_on_exits_1_after_writing_and_counts_what_was_introduced(tmp_path, monkeypatch, capsys) -> None:
    """The fixture's change introduces warnings and no error: `warning` fails, `error` and `never` do not."""
    manifest = build_project(tmp_path)
    document = json.loads(
        _run(["--project", str(manifest), "--no-diagrams", "--format", "json"], monkeypatch, capsys)[1]
    )
    errors, warnings = document["verdict"]["errors"], document["verdict"]["warnings"]
    assert errors == 0 and warnings > 0

    for name in ("change-report.md", "change-report.json"):
        (_report_dir(manifest) / name).unlink()
    code, out, err = _run(["--project", str(manifest), "--no-diagrams", "--fail-on", "warning"], monkeypatch, capsys)

    assert code == 1
    assert out.startswith("Generated report: ")
    assert (_report_dir(manifest) / "change-report.md").is_file()
    assert err == (f"Error: the change introduces {errors} error(s) and {warnings} warning(s) (--fail-on warning).\n")
    for level in ("error", "never"):
        code, _out, err = _run(["--project", str(manifest), "--no-diagrams", "--fail-on", level], monkeypatch, capsys)
        assert (code, err) == (0, "")


def test_an_unknown_revision_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    manifest = build_project(tmp_path)

    code, out, err = _run(["--project", str(manifest), "--no-diagrams", "no-such-revision"], monkeypatch, capsys)

    assert code == 1
    assert out == ""
    assert err.startswith("Error: ")
    assert not _report_dir(manifest).exists()


def test_an_option_like_revision_is_refused(tmp_path, monkeypatch, capsys) -> None:
    manifest = build_project(tmp_path)

    code, _out, err = _run(["--project", str(manifest), "--no-diagrams", "--", "-p"], monkeypatch, capsys)

    assert code == 1
    assert err.startswith("Error: ")
    assert not _report_dir(manifest).exists()


def test_a_duplicate_identity_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    """Two files declaring one interface: the report cannot pair elements it cannot tell apart."""
    manifest = build_project(tmp_path)
    interfaces = manifest.parent / "model" / "interfaces"
    (interfaces / "IExampleCopy.yaml").write_text(
        (interfaces / "IExample.yaml").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )

    code, _out, err = _run(["--project", str(manifest), "--no-diagrams"], monkeypatch, capsys)

    assert code == 1
    assert err.startswith("Error: ")
    assert not _report_dir(manifest).exists()


def test_a_pin_mismatch_refuses(tmp_path, monkeypatch, capsys) -> None:
    manifest = build_project(tmp_path)
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["toolchain_version"] = "9.9.9"
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")

    code, out, err = _run(["--project", str(manifest), "--no-diagrams"], monkeypatch, capsys)

    assert code == 1
    assert out == ""
    assert "9.9.9" in err
    assert not _report_dir(manifest).exists()


def test_a_missing_project_flag_is_refused(monkeypatch, capsys) -> None:
    code, out, err = _run(["--no-diagrams"], monkeypatch, capsys)

    assert code == 1
    assert out == ""
    assert "--project" in err


def test_an_earlier_runs_files_are_replaced_and_a_foreign_file_kept(tmp_path, monkeypatch, capsys) -> None:
    manifest = build_project(tmp_path)
    directory = _report_dir(manifest)
    directory.mkdir(parents=True)
    for name in ("overview-Gone-before.svg", "overview-Gone-after.svg", "change-report-pr.md", "notes.txt"):
        (directory / name).write_text("old", encoding="utf-8", newline="\n")

    code, _out, err = _run(["--project", str(manifest), "--no-diagrams"], monkeypatch, capsys)

    assert code == 0, err
    assert sorted(path.name for path in directory.iterdir()) == [
        "change-report-pr.md",
        "change-report.json",
        "change-report.md",
        "notes.txt",
    ]
    assert (directory / "notes.txt").read_text(encoding="utf-8") == "old"
    assert (directory / "change-report-pr.md").read_text(encoding="utf-8").startswith("# Change Report\n")


def test_two_runs_write_the_same_bytes(tmp_path, monkeypatch, capsys) -> None:
    manifest = build_project(tmp_path)
    directory = _report_dir(manifest)

    _run(["--project", str(manifest), "--no-diagrams"], monkeypatch, capsys)
    first = {path.name: path.read_bytes() for path in directory.iterdir()}
    _run(["--project", str(manifest), "--no-diagrams"], monkeypatch, capsys)
    second = {path.name: path.read_bytes() for path in directory.iterdir()}

    assert first == second


def test_no_written_file_names_where_the_project_is_stored(tmp_path, monkeypatch, capsys) -> None:
    manifest = build_project(tmp_path)

    _run(["--project", str(manifest), "--no-diagrams"], monkeypatch, capsys)

    root = manifest.parent.resolve().as_posix()
    for path in _report_dir(manifest).iterdir():
        assert root not in path.read_text(encoding="utf-8"), path.name


def test_a_layout_that_cannot_run_is_a_warning_and_the_report_is_still_written(tmp_path, monkeypatch, capsys) -> None:
    """No Node is a sentence in the report and a `Warning:` line, never a failure."""
    from swcraft_tools.writer.diagrams import change, layout

    def refuse(*_args, **_kwargs):
        raise layout.DiagramLayoutError("Node.js was not found")

    monkeypatch.setattr(change.layout, "layout", refuse)
    manifest = build_project(tmp_path)

    code, out, err = _run(["--project", str(manifest)], monkeypatch, capsys)

    assert code == 0
    assert out.startswith("Generated report: ")
    assert err.startswith("Warning: diagrams not drawn: ")
    assert "Not drawn: " in (_report_dir(manifest) / "change-report.md").read_text(encoding="utf-8")
