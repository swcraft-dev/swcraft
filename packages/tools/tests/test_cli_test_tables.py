"""``swcraft generate test-tables`` on the command line: the bundle, the refusals, the write.

The rows, the hashes and the annotations are ``test_test_tables.py``'s; this module holds what the command adds
around them: the five files and their bytes, the printed lines, the refusals before anything is written (a model
``check-model`` refuses, the manifest's own findings, a pin mismatch, a format flag on another format, an output or
annotations directory in the wrong place) and the two ways a write can fail. Every run is on a copy of
``examples/basic``, a model ``check-model`` passes.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from swcraft_tools import experiment_frame, tables_for_testing
from swcraft_tools._version import __version__
from swcraft_tools.cli import main
from swcraft_tools.model_registry import current_registry
from swcraft_tools.project import require_project
from swcraft_tools.validator import check_cross_layer_consistency
from symlink_support import symlink_or_skip

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "basic"
BUNDLE = [
    "component-placement.csv",
    "components.csv",
    "manifest.json",
    "orphaned-annotations.csv",
    "runnable-scheduling.csv",
]
SCHEDULING_ROW = "occurrence:ExampleConsumerSWC:ExampleConsumerRun"


def _project(tmp_path: Path) -> Path:
    """A copy of ``examples/basic`` under *tmp_path*; returns its manifest."""
    project = tmp_path / "project"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns("dist", "views"))
    return project / "swcraft.yaml"


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """``swcraft generate ...`` in process: ``(exit code, stdout, stderr)``, 0 when it returned."""
    monkeypatch.setattr("sys.argv", ["swcraft", "generate", *argv])
    try:
        main()
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _generate(manifest: Path, monkeypatch, capsys, *extra: str) -> tuple[int, str, str]:
    return _run(["test-tables", "--project", str(manifest), *extra], monkeypatch, capsys)


def _bundle(manifest: Path) -> Path:
    return manifest.parent / "dist" / "test-tables"


def _bytes_of(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir(), key=lambda path: path.as_posix())}


def _write_yaml(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")


def _edit_manifest(manifest: Path, **changes: object) -> None:
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    for key, value in changes.items():
        data[key] = value
    _write_yaml(manifest, data)


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text, newline="")))


# The bundle


def test_a_run_writes_the_five_files_and_prints_the_counts(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)

    code, out, err = _generate(manifest, monkeypatch, capsys)

    directory = _bundle(manifest).resolve()
    assert (code, err) == (0, "")
    assert sorted(path.name for path in directory.iterdir()) == BUNDLE
    assert out == (
        f"Generated test tables: {directory}\n"
        "components.csv: 2 rows (0 accepted, 0 stale, 0 rejected, 2 not reviewed)\n"
        "component-placement.csv: 2 rows (0 accepted, 0 stale, 0 rejected, 2 not reviewed)\n"
        "runnable-scheduling.csv: 2 rows (0 accepted, 0 stale, 0 rejected, 2 not reviewed)\n"
    )
    scheduling = {
        row["row_id"]: row for row in _rows((directory / "runnable-scheduling.csv").read_text(encoding="utf-8"))
    }
    assert scheduling[SCHEDULING_ROW]["task"] == "Task_10ms"
    assert scheduling[SCHEDULING_ROW]["task_period_s"] == "0.01"
    assert scheduling[SCHEDULING_ROW]["task_priority"] == "5"
    assert scheduling[SCHEDULING_ROW]["core"] == "CORE0"
    assert (directory / "orphaned-annotations.csv").read_bytes() == (
        b'"table","row_id","verification_method","comment","accepted","accepted_facts_hash"\n'
    )


def test_the_manifest_states_the_versions_the_digest_and_every_column(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0

    text = (_bundle(manifest) / "manifest.json").read_text(encoding="utf-8")
    document = json.loads(text)
    project = require_project(str(manifest))
    built = tables_for_testing.build_test_tables(project.resolved_model_root, project.name)

    assert str(tmp_path) not in text
    assert {key: document[key] for key in ("schema_version", "swcraft_version", "contract_version", "project")} == {
        "schema_version": "1.0.0",
        "swcraft_version": __version__,
        "contract_version": current_registry().contract_version,
        "project": "swcraft-basic-example",
    }
    assert document["facts_digest"] == tables_for_testing.facts_digest(built.tables)
    assert "not declared in the model" in document["empty_cell"]
    assert [entry["file"] for entry in document["tables"]] == [
        "components.csv",
        "component-placement.csv",
        "runnable-scheduling.csv",
    ]
    for entry, table in zip(document["tables"], built.tables, strict=True):
        assert entry["revision"] == 1
        assert entry["rows"] == len(table.rows)
        assert entry["review_states"] == {"accepted": 0, "stale": 0, "rejected": 0, "not-reviewed": len(table.rows)}
        assert entry["fact_columns"] == list(table.fact_columns)
        assert [column["name"] for column in entry["columns"]] == list(table.columns)
        owners = {column["name"]: column["owner"] for column in entry["columns"]}
        assert {name for name, owner in owners.items() if owner == "test"} == set(tables_for_testing.TEST_COLUMNS)
        assert owners["review_state"] == "swcraft"
        assert all(column["meaning"] for column in entry["columns"])
    assert document["orphaned_annotations"]["rows"] == 0


def test_two_runs_and_a_reversed_reading_give_the_same_bytes(tmp_path, monkeypatch, capsys) -> None:
    """The rows are ordered by the module, never by the order the elements are read in.

    The reader already sorts a layer's elements, so a reversed directory listing reaches no row: each layer is
    reversed after the reader's own sort.
    """
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    first = _bytes_of(_bundle(manifest))
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    assert _bytes_of(_bundle(manifest)) == first

    real_read_layer = tables_for_testing.read_layer
    reversed_layers: list[str] = []

    def reversed_read_layer(model_root, layer, *args, **kwargs):
        elements = list(real_read_layer(model_root, layer, *args, **kwargs))
        if len(elements) > 1:
            reversed_layers.append(layer)
        return elements[::-1]

    monkeypatch.setattr(tables_for_testing, "read_layer", reversed_read_layer)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    assert _bytes_of(_bundle(manifest)) == first
    assert "swc" in reversed_layers, "the reversal reached a layer of more than one element"


def test_every_record_ends_in_a_line_feed(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0

    for name, data in _bytes_of(_bundle(manifest)).items():
        assert data.endswith(b"\n"), name
        assert b"\r" not in data, name


# What the model and the manifest refuse


def test_a_model_check_model_refuses_writes_no_file(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    task = manifest.parent / "model" / "tasks" / "Task_10ms.yaml"
    data = yaml.safe_load(task.read_text(encoding="utf-8"))
    data["name"] = "Task_20ms"
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    before = _bytes_of(_bundle(manifest))
    _write_yaml(task.with_name("Task_20ms.yaml"), data)  # the same runnable mapped to two tasks: an error

    code, out, err = _generate(manifest, monkeypatch, capsys)

    assert (code, out) == (1, "")
    assert err.startswith("Error: the model is refused: check-model reports 1 error (run swcraft check-model")
    assert _bytes_of(_bundle(manifest)) == before, "the previous bundle is left as it was"


def test_a_model_with_warnings_only_is_written(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    _write_yaml(
        manifest.parent / "model" / "requirements" / "REQ_EXAMPLE_009.yaml",
        {
            "id": "REQ_EXAMPLE_009",
            "title": "Unallocated",
            "description": "Nothing satisfies it.",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    )

    checked = check_cross_layer_consistency(str(manifest.parent / "model"))
    assert (checked.issues, bool(checked.warnings)) == ([], True)

    code, _out, err = _generate(manifest, monkeypatch, capsys)

    assert (code, err) == (0, "")
    assert sorted(path.name for path in _bundle(manifest).iterdir()) == BUNDLE


@pytest.mark.parametrize(("version", "refused"), [(None, True), ("4.2.2", True), ("4.3.0", False)])
def test_the_model_is_judged_at_the_manifests_arxml_version(tmp_path, monkeypatch, capsys, version, refused) -> None:
    """The tables refuse what ``check-model`` refuses at the manifest's ``arxml_version`` (4.2.2 when unstated).

    An asynchronous call with no result point is ``[constr_2006]``'s finding at 4.2.2 alone."""
    manifest = _project(tmp_path)
    model = manifest.parent / "model"
    shutil.rmtree(model)
    shutil.copytree(Path(__file__).resolve().parent / "fixtures" / "access_forms" / "model", model)
    controller = model / "swc" / "chassis" / "BrakeController.yaml"
    document = yaml.safe_load(controller.read_text(encoding="utf-8"))
    (runnable,) = [entry for entry in document["runnables"] if entry["name"] == "ComputeBrakeTorque"]
    (call,) = [
        entry for entry in runnable["calls"] if (entry["port"], entry["operation"]) == ("rCalibration", "ReadBlock")
    ]
    call.pop("poll")
    _write_yaml(controller, document)
    if version is not None:
        _edit_manifest(manifest, arxml_version=version)

    code, out, err = _generate(manifest, monkeypatch, capsys)

    if refused:
        assert (code, out) == (1, "")
        assert err.startswith("Error: the model is refused: check-model reports 1 error")
    else:
        assert (code, err) == (0, "")
        assert sorted(path.name for path in _bundle(manifest).iterdir()) == BUNDLE


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"unknown_setting": 1}, id="top-level"),
        pytest.param(
            {"outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json", "tables": "x"}}, id="outputs"
        ),
    ],
)
def test_an_undeclared_manifest_key_refuses(tmp_path, monkeypatch, capsys, changes) -> None:
    manifest = _project(tmp_path)
    _edit_manifest(manifest, **changes)

    code, out, err = _generate(manifest, monkeypatch, capsys)

    assert (code, out) == (1, "")
    assert err.startswith("Error: the model is refused: check-model reports 1 error")
    assert not _bundle(manifest).exists() or not any(_bundle(manifest).iterdir())


def test_a_pin_mismatch_refuses(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    _edit_manifest(manifest, toolchain_version="9.9.9")

    code, out, err = _generate(manifest, monkeypatch, capsys)

    assert (code, out) == (1, "")
    assert "9.9.9" in err
    assert not _bundle(manifest).exists()


@pytest.mark.parametrize("fmt", ["arxml", "diagrams", "needs"])
def test_annotations_with_another_format_are_refused_by_name(tmp_path, monkeypatch, capsys, fmt) -> None:
    manifest = _project(tmp_path)
    (tmp_path / "ann").mkdir()

    code, out, err = _run(
        [fmt, "--project", str(manifest), "--annotations", str(tmp_path / "ann")], monkeypatch, capsys
    )

    assert (code, out, err) == (1, "", "Error: --annotations applies only to 'generate test-tables'.\n")
    assert not (manifest.parent / "dist").exists()


# The annotations


def test_the_round_trip_prints_the_review_and_the_orphans(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    annotations = tmp_path / "review"
    annotations.mkdir()
    rows = _rows((_bundle(manifest) / "runnable-scheduling.csv").read_text(encoding="utf-8"))
    for row in rows:
        row["accepted"] = "yes"
    rows.append({**rows[0], "row_id": "occurrence:gone:Run", "comment": "kept"})
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    (annotations / "runnable-scheduling.csv").write_bytes(buffer.getvalue().encode("utf-8"))
    (annotations / "notes.txt").write_bytes(b"mine\n")
    before = _bytes_of(annotations)
    task = manifest.parent / "model" / "tasks" / "Task_10ms.yaml"
    task.write_text(
        task.read_text(encoding="utf-8").replace("priority: 5", "priority: 6"), encoding="utf-8", newline="\n"
    )

    code, out, err = _generate(manifest, monkeypatch, capsys, "--annotations", str(annotations))

    assert code == 0, err
    assert err == "Warning: the annotations directory holds files that name no table, ignored: notes.txt\n"
    assert "runnable-scheduling.csv: 2 rows (1 accepted, 1 stale, 0 rejected, 0 not reviewed)\n" in out
    assert out.endswith("Orphaned annotations: 1\n")
    assert _bytes_of(annotations) == before
    scheduling = {
        row["row_id"]: row for row in _rows((_bundle(manifest) / "runnable-scheduling.csv").read_text(encoding="utf-8"))
    }
    assert scheduling[SCHEDULING_ROW]["review_state"] == "stale"
    assert scheduling[SCHEDULING_ROW]["accepted"] == "yes"
    orphans = _rows((_bundle(manifest) / "orphaned-annotations.csv").read_text(encoding="utf-8"))
    assert [(row["table"], row["row_id"], row["comment"]) for row in orphans] == [
        ("runnable-scheduling", "occurrence:gone:Run", "kept")
    ]


def test_a_whole_bundle_handed_back_warns_of_nothing_and_orphans_only_annotated_rows(
    tmp_path, monkeypatch, capsys
) -> None:
    manifest = _project(tmp_path)
    component = manifest.parent / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml"
    original = component.read_text(encoding="utf-8")
    data = yaml.safe_load(original)
    data["runnables"] += [{"name": "Spare"}, {"name": "Noted"}]
    _write_yaml(component, data)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    annotations = tmp_path / "review"
    shutil.copytree(_bundle(manifest), annotations)  # manifest.json and all, as the test side hands it back
    rows = _rows((annotations / "runnable-scheduling.csv").read_text(encoding="utf-8"))
    for row in rows:
        if row["row_id"] == "occurrence:ExampleConsumerSWC:Noted":
            row["comment"] = "check on target"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    (annotations / "runnable-scheduling.csv").write_bytes(buffer.getvalue().encode("utf-8"))

    data["runnables"] = [runnable for runnable in data["runnables"] if runnable["name"] != "Spare"]
    _write_yaml(component, data)
    code, out, err = _generate(manifest, monkeypatch, capsys, "--annotations", str(annotations))
    assert (code, err) == (0, "")
    assert out.endswith("Orphaned annotations: 0\n")

    component.write_text(original, encoding="utf-8", newline="\n")
    code, out, err = _generate(manifest, monkeypatch, capsys, "--annotations", str(annotations))
    assert (code, err) == (0, "")
    assert out.endswith("Orphaned annotations: 1\n")
    orphans = _rows((_bundle(manifest) / "orphaned-annotations.csv").read_text(encoding="utf-8"))
    assert [(row["row_id"], row["comment"]) for row in orphans] == [
        ("occurrence:ExampleConsumerSWC:Noted", "check on target")
    ]


def test_an_annotations_file_refused_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    before = _bytes_of(_bundle(manifest))
    annotations = tmp_path / "review"
    annotations.mkdir()
    (annotations / "components.csv").write_bytes(b"row_id,accepted\ncomponent:ExampleSWC,TRUE\n")

    code, out, err = _generate(manifest, monkeypatch, capsys, "--annotations", str(annotations))

    assert (code, out) == (1, "")
    assert err.startswith(f"Error: {annotations / 'components.csv'}: line 2, column 2: accepted is 'TRUE'")
    assert _bytes_of(_bundle(manifest)) == before


def test_a_missing_annotations_directory_is_refused(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    missing = tmp_path / "nowhere"

    code, out, err = _generate(manifest, monkeypatch, capsys, "--annotations", str(missing))

    assert (code, out, err) == (1, "", f"Error: {missing}: the annotations directory does not exist\n")


@pytest.mark.parametrize(
    ("relative", "root"),
    [
        ("dist/review", "dist_root"),
        ("model/review", "model_root"),
        ("views/review", "views_root"),
        ("Dist/review", "dist_root"),
        ("MODEL/review", "model_root"),
    ],
)
def test_an_annotations_directory_inside_a_root_is_refused(tmp_path, monkeypatch, capsys, relative, root) -> None:
    """Another case of a root is the root on APFS and NTFS, and refused on every platform."""
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    before = _bytes_of(_bundle(manifest))
    annotations = manifest.parent / relative
    annotations.mkdir(parents=True, exist_ok=True)

    code, out, err = _generate(manifest, monkeypatch, capsys, "--annotations", str(annotations))

    assert (code, out, err) == (1, "", f"Error: {annotations}: the annotations directory lies inside {root}\n")
    assert _bytes_of(_bundle(manifest)) == before


@pytest.mark.parametrize("blank", ["", "  "])
def test_a_blank_annotations_directory_is_refused_by_name(tmp_path, monkeypatch, capsys, blank) -> None:
    """An empty path would name the working directory: refused before anything is read or written."""
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    before = _bytes_of(_bundle(manifest))

    code, out, err = _generate(manifest, monkeypatch, capsys, f"--annotations={blank}")

    assert (code, out) == (1, "")
    assert err == ("Error: --annotations names no directory; give the test side's directory or leave it out\n")
    assert _bytes_of(_bundle(manifest)) == before


@pytest.mark.parametrize("spelling", ["dist/test-tables", "DIST/Test-Tables"])
def test_the_output_directory_as_the_annotations_directory_is_refused(tmp_path, monkeypatch, capsys, spelling) -> None:
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    before = _bytes_of(_bundle(manifest))
    annotations = manifest.parent / spelling
    annotations.mkdir(parents=True, exist_ok=True)

    code, out, err = _generate(manifest, monkeypatch, capsys, "--annotations", str(annotations))

    assert (code, out) == (1, "")
    assert err == f"Error: {annotations}: the annotations directory overlaps the test tables' output directory\n"
    assert _bytes_of(_bundle(manifest)) == before


def test_an_output_link_to_the_annotations_directory_is_refused_and_leaves_it_untouched(
    tmp_path, monkeypatch, capsys
) -> None:
    manifest = _project(tmp_path)
    annotations = manifest.parent / "review"
    annotations.mkdir()
    (annotations / "components.csv").write_bytes(b'"row_id","comment"\n"component:ExampleSWC","mine"\n')
    before = _bytes_of(annotations)
    (manifest.parent / "dist").mkdir()
    symlink_or_skip(_bundle(manifest), annotations)

    code, out, err = _generate(manifest, monkeypatch, capsys, "--annotations", str(annotations))

    assert (code, out) == (1, "")
    assert err == "Error: the report directory '<dist_root>/test-tables' resolves outside 'dist_root'\n"
    assert _bytes_of(annotations) == before


# The write


def _refuse_rename(monkeypatch: pytest.MonkeyPatch, which: int) -> None:
    """Make the *which*-th ``os.replace`` of the run raise, every other one go through."""
    real = os.replace
    calls = {"n": 0}

    def replace(source, target):
        calls["n"] += 1
        if calls["n"] == which:
            raise PermissionError(13, "held open")
        return real(source, target)

    monkeypatch.setattr(experiment_frame.os, "replace", replace)


def test_a_rename_refused_on_the_second_file_reports_a_partly_replaced_bundle(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    before = _bytes_of(_bundle(manifest))
    task = manifest.parent / "model" / "tasks" / "Task_10ms.yaml"
    task.write_text(
        task.read_text(encoding="utf-8").replace("priority: 5", "priority: 6"), encoding="utf-8", newline="\n"
    )
    _refuse_rename(monkeypatch, 2)

    code, out, err = _generate(manifest, monkeypatch, capsys)

    assert (code, out) == (1, "")
    assert err == "Error: the test tables were only partly replaced (held open); run the command again\n"
    assert not [path for path in _bundle(manifest).iterdir() if path.name.endswith(".partial")]
    after = _bytes_of(_bundle(manifest))
    assert after["manifest.json"] != before["manifest.json"], "the first file was replaced"
    assert {name: data for name, data in after.items() if name != "manifest.json"} == {
        name: data for name, data in before.items() if name != "manifest.json"
    }, "every later file is the previous run's"


def test_a_rename_refused_on_the_first_file_replaces_nothing(tmp_path, monkeypatch, capsys) -> None:
    manifest = _project(tmp_path)
    assert _generate(manifest, monkeypatch, capsys)[0] == 0
    before = _bytes_of(_bundle(manifest))
    task = manifest.parent / "model" / "tasks" / "Task_10ms.yaml"
    task.write_text(
        task.read_text(encoding="utf-8").replace("priority: 5", "priority: 6"), encoding="utf-8", newline="\n"
    )
    _refuse_rename(monkeypatch, 1)

    code, out, err = _generate(manifest, monkeypatch, capsys)

    assert (code, out) == (1, "")
    assert err.startswith("Error: the test tables could not be written: ")
    assert _bytes_of(_bundle(manifest)) == before


def test_the_module_form_reaches_the_command(tmp_path) -> None:
    """``python -m swcraft_tools.cli generate test-tables`` writes the bundle.

    The in-process runs cannot see a helper defined below the ``__main__`` guard, which the module form runs
    before that helper exists.
    """
    manifest = _project(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "generate", "test-tables", "--project", str(manifest)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in (manifest.parent / "dist" / "test-tables").iterdir()) == BUNDLE
