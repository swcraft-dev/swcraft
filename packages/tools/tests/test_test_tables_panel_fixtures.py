"""The *Test Tables* tab's recorded bundle: what the CLI writes, the TypeScript side reads.

The five files under ``vscode_extension/src/fixtures/test-tables/`` are one bundle of ``swcraft generate test-tables
--annotations DIR``, and ``testTablesTab.test.ts`` parses and renders them; every test here generates the bundle
afresh and holds each committed file byte-equal to it, so the bundle's form cannot move under the tab unseen.

The model is ``examples/basic`` with two descriptions holding every character the CSV quoting must survive (``,``,
``"``, ``\\r``, ``\\n``, ``\\r\\n``); the annotations give each review state and one orphan:

- ``components.csv``: ``ExampleSWC`` accepted, ``ExampleConsumerSWC`` rejected (both pinned to their ``facts_hash``);
- ``runnable-scheduling.csv``: ``ExampleRun`` accepted against a hash no row has (``stale``); ``ExampleConsumerRun``
  commented only, with a ``\\r\\n`` (``not-reviewed``);
- one annotation for a runnable no model declares (the orphan).

The bundle names no storage path, so the scratch location never reaches a file.

These are **change detectors, never correctness proofs**: regenerate them deliberately, after reading the
difference, with ``python packages/tools/tests/test_test_tables_panel_fixtures.py``. The comparison masks only the
manifest's top-level ``swcraft_version`` and ``contract_version`` (``_comparable``), which a bump moves and the tab
renders neither of.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from swcraft_tools.cli import main

FIXTURES = REPO_ROOT / "vscode_extension" / "src" / "fixtures" / "test-tables"
EXAMPLE = REPO_ROOT / "examples" / "basic"
BUNDLE = (
    "component-placement.csv",
    "components.csv",
    "manifest.json",
    "orphaned-annotations.csv",
    "runnable-scheduling.csv",
)
#: The manifest's top-level fields the drift comparison masks: a bump moves them, the tab renders neither.
IGNORED_MANIFEST_FIELDS = ("swcraft_version", "contract_version")
MASK = b'"<ignored by the drift test>"'
#: Every character the CSV quoting must carry inside one field.
AWKWARD = 'Publishes, "quoted" state\rafter a CR\nafter an LF\r\nafter a CRLF'
COMMENT = 'Check on target,\r\nwith the bench\'s "long" run'


def _generate(manifest: Path, *extra: str) -> None:
    """``swcraft generate test-tables --project MANIFEST [EXTRA]`` in this process; it must exit 0."""
    saved = sys.argv
    sys.argv = ["swcraft", "generate", "test-tables", "--project", str(manifest), *extra]
    code = 0
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    finally:
        sys.argv = saved
    assert code == 0, "generate test-tables refused the fixture model"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_bytes(buffer.getvalue().encode("utf-8"))


def _describe(component_file: Path, description: str, runnable_description: str) -> None:
    data = yaml.safe_load(component_file.read_text(encoding="utf-8"))
    data["description"] = description
    data["runnables"][0]["description"] = runnable_description
    component_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")


def generated(root: Path) -> dict[str, bytes]:
    """The bundle's five files as the CLI writes them today, by name, built in the empty directory *root*."""
    project = root / "project"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns("dist", "views"))
    manifest = project / "swcraft.yaml"
    swc = project / "model" / "swc" / "example"
    _describe(swc / "ExampleSWC.yaml", AWKWARD, AWKWARD)
    _describe(swc / "ExampleConsumerSWC.yaml", "A plain description", 'Consumes, then "stores"')
    _generate(manifest)
    bundle = project / "dist" / "test-tables"

    annotations = root / "review"
    annotations.mkdir()
    components = {row["row_id"]: row for row in _rows(bundle / "components.csv")}
    _write_rows(
        annotations / "components.csv",
        ["row_id", "accepted", "accepted_facts_hash", "comment"],
        [
            {
                "row_id": "component:ExampleSWC",
                "accepted": "yes",
                "accepted_facts_hash": components["component:ExampleSWC"]["facts_hash"],
                "comment": "",
            },
            {
                "row_id": "component:ExampleConsumerSWC",
                "accepted": "no",
                "accepted_facts_hash": components["component:ExampleConsumerSWC"]["facts_hash"],
                "comment": "Budget too tight",
            },
        ],
    )
    _write_rows(
        annotations / "runnable-scheduling.csv",
        ["row_id", "verification_method", "accepted", "accepted_facts_hash", "comment"],
        [
            {
                "row_id": "occurrence:ExampleSWC:ExampleRun",
                "verification_method": "HIL",
                "accepted": "yes",
                "accepted_facts_hash": "0000000000000000",
                "comment": "",
            },
            {
                "row_id": "occurrence:ExampleConsumerSWC:ExampleConsumerRun",
                "verification_method": "",
                "accepted": "",
                "accepted_facts_hash": "",
                "comment": COMMENT,
            },
            {
                "row_id": "occurrence:RetiredSWC:RetiredRun",
                "verification_method": "review",
                "accepted": "",
                "accepted_facts_hash": "",
                "comment": "kept, though the runnable is gone",
            },
        ],
    )
    _generate(manifest, "--annotations", str(annotations))
    return {name: (bundle / name).read_bytes() for name in BUNDLE}


def _comparable(name: str, data: bytes) -> bytes:
    """``data`` as the drift check compares it: ``manifest.json`` with the two ignored values masked.

    Each ignored field's one ``"field": "value"`` occurrence becomes ``MASK``; the
    layout and every other byte stay as written. Every other file is unchanged.
    """
    if name != "manifest.json":
        return data
    manifest = json.loads(data.decode("utf-8"))
    for field in IGNORED_MANIFEST_FIELDS:
        value = manifest.get(field) if isinstance(manifest, dict) else None
        if not isinstance(value, str):
            continue
        needle = f'"{field}": {json.dumps(value)}'.encode()
        assert data.count(needle) == 1, f"{field} is not written once as {needle!r}"
        data = data.replace(needle, f'"{field}": '.encode() + MASK)
    return data


def _drifted(committed: dict[str, bytes], fresh: dict[str, bytes]) -> list[str]:
    """The names whose committed bytes differ from the fresh ones, the ignored values aside."""
    return [
        name
        for name in BUNDLE
        if name not in committed
        or name not in fresh
        or _comparable(name, committed[name]) != _comparable(name, fresh[name])
    ]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    return generated(tmp_path_factory.mktemp("test-tables-bundle"))


@pytest.mark.parametrize("name", BUNDLE)
def test_the_committed_file_is_the_cli_s(name: str, bundle: dict[str, bytes]) -> None:
    committed = (FIXTURES / name).read_bytes()
    assert _comparable(name, committed) == _comparable(name, bundle[name])


def _set_manifest_field(data: bytes, field: str, value: str) -> bytes:
    old = json.loads(data.decode("utf-8"))[field]
    needle = f'"{field}": {json.dumps(old)}'.encode()
    assert data.count(needle) == 1, field
    return data.replace(needle, f'"{field}": {json.dumps(value)}'.encode())


def test_only_the_two_version_fields_are_ignored(bundle: dict[str, bytes]) -> None:
    """A bump of both fields passes; a changed other manifest field or one CSV byte still fails."""
    assert _drifted(bundle, dict(bundle)) == []

    bumped = dict(bundle)
    bumped["manifest.json"] = _set_manifest_field(
        _set_manifest_field(bundle["manifest.json"], "swcraft_version", "99.0.0"),
        "contract_version",
        "99.0.0",
    )
    assert bumped["manifest.json"] != bundle["manifest.json"]
    assert _drifted(bundle, bumped) == []

    other_field = dict(bundle)
    other_field["manifest.json"] = _set_manifest_field(bundle["manifest.json"], "schema_version", "9.9.9")
    assert _drifted(bundle, other_field) == ["manifest.json"]

    project = dict(bumped)
    project["manifest.json"] = _set_manifest_field(bumped["manifest.json"], "project", "another-project")
    assert _drifted(bundle, project) == ["manifest.json"]

    csv_byte = dict(bundle)
    original = bundle["components.csv"]
    csv_byte["components.csv"] = original[:-2] + bytes([original[-2] ^ 0x01]) + original[-1:]
    assert _drifted(bundle, csv_byte) == ["components.csv"]


def test_the_fixture_directory_holds_the_bundle_and_nothing_else() -> None:
    assert sorted(path.name for path in FIXTURES.iterdir()) == list(BUNDLE)


def test_the_bundle_holds_what_the_tab_tests_need(bundle: dict[str, bytes]) -> None:
    """Each case shows what the module docstring says, so a model edit cannot hollow one out."""

    def rows(name: str) -> list[dict[str, str]]:
        return list(csv.DictReader(io.StringIO(bundle[name].decode("utf-8"), newline="")))

    tables = ("components.csv", "component-placement.csv", "runnable-scheduling.csv")
    states = {row["row_id"]: row["review_state"] for name in tables for row in rows(name)}
    assert states["component:ExampleSWC"] == "accepted"
    assert states["component:ExampleConsumerSWC"] == "rejected"
    assert states["occurrence:ExampleSWC:ExampleRun"] == "stale"
    assert states["occurrence:ExampleConsumerSWC:ExampleConsumerRun"] == "not-reviewed"
    assert [row["row_id"] for row in rows("orphaned-annotations.csv")] == ["occurrence:RetiredSWC:RetiredRun"]
    descriptions = [row["description"] for row in rows("components.csv") + rows("runnable-scheduling.csv")]
    assert AWKWARD in descriptions
    comments = [row["comment"] for row in rows("runnable-scheduling.csv")]
    assert COMMENT in comments
    for character in (b",", b'""', b"\r", b"\n", b"\r\n"):
        assert character in bundle["runnable-scheduling.csv"], character


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        for fixture_name, data in generated(Path(scratch)).items():
            (FIXTURES / fixture_name).write_bytes(data)
