"""Tests for the component-exchange MCP tools.

``export_component`` (one component with ``swc_name``, every component without it) and ``import_component`` answer
``{"success": bool, "errors": [{"message": str, "path": str}], "output": str}``, with any exception converted into
the error dict. ``import_component`` only plans: its draft is applied by ``change_set``
(``test_import_change_set.py``); its planning decisions are asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import swcraft_mcp.server as _server
import yaml
from conftest import model_element_path
from protocol_client import call, list_tools
from swcraft_mcp.server import export_component, import_component
from swcraft_tools import component_exchange


def _dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _default_export_path(swc_name: str) -> Path:
    project = _server._get_project()
    return Path(project.resolved_dist_root) / "components" / f"{swc_name}.arxml"


def _exported_speed_sensor() -> Path:
    """Export SpeedSensorSWC from the fixture project and return the file path."""
    result = export_component(swc_name="SpeedSensorSWC")
    assert result["success"] is True, result
    path = _default_export_path("SpeedSensorSWC")
    assert path.is_file()
    return path


# ---------------------------------------------------------------------------
# export_component
# ---------------------------------------------------------------------------


def test_export_component_default_output_path(model_root):
    result = export_component(swc_name="SpeedSensorSWC")
    assert result["success"] is True
    assert result["errors"] == []
    assert "STATUS ok" in result["output"]
    assert "ISpeed" in result["output"]
    out = _default_export_path("SpeedSensorSWC")
    assert out.is_file()
    content = out.read_text(encoding="utf-8")
    assert "SpeedSensorSWC" in content
    # closure only — the other fixture SWC must not leak into the export
    assert "SpeedCtrlSWC" not in content


def test_export_component_explicit_output_path(model_root, tmp_path):
    out = tmp_path / "exports" / "speed.arxml"
    result = export_component(swc_name="SpeedSensorSWC", output_path=str(out))
    assert result["success"] is True
    assert out.is_file()
    assert not _default_export_path("SpeedSensorSWC").exists()


def test_export_component_unknown_swc_returns_error_dict(model_root):
    result = export_component(swc_name="GhostSWC")
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "GhostSWC" in result["errors"][0]["message"]
    assert result["errors"][0]["path"] == ""
    assert not _default_export_path("GhostSWC").exists()


def test_export_component_bad_manifest_returns_error_dict(monkeypatch, tmp_path):
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(tmp_path / "does-not-exist.yaml"))
    result = export_component(swc_name="SpeedSensorSWC")
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "manifest" in result["errors"][0]["message"]
    assert "does-not-exist.yaml" in result["errors"][0]["message"]


def _add_a_case_only_twin(model_root: Path) -> None:
    """``speedsensorswc`` beside ``SpeedSensorSWC``, each in its own ``package`` — a legal pair."""
    sensor = model_root / "swc" / "example" / "SpeedSensorSWC.yaml"
    data = yaml.safe_load(sensor.read_text(encoding="utf-8"))
    _dump(sensor, {**data, "package": "Main"})
    _dump(
        model_root / "swc" / "other" / "speedsensorswc.yaml",
        {
            "name": "speedsensorswc",
            "type": "application",
            "package": "Alt",
            "ports": [],
            "handle_termination_and_restart": "no_support",
        },
    )


def test_export_component_default_names_a_case_only_twin_as_the_batch_does(model_root):
    """Without an output path the single export takes the batch's file name.

    Both members of a case-only pair thus survive in one directory, as on a case-insensitive filesystem they
    otherwise would not.
    """
    _add_a_case_only_twin(Path(model_root))
    components = _default_export_path("SpeedSensorSWC").parent

    first = export_component(swc_name="SpeedSensorSWC")
    second = export_component(swc_name="speedsensorswc")

    assert first["success"] is True, first
    assert second["success"] is True, second
    assert "OUTPUT dist/components/speedsensorswc~1.arxml" in second["output"]
    assert sorted(p.name for p in components.iterdir()) == ["SpeedSensorSWC.arxml", "speedsensorswc~1.arxml"]
    assert "<SHORT-NAME>SpeedSensorSWC</SHORT-NAME>" in (components / "SpeedSensorSWC.arxml").read_text(
        encoding="utf-8"
    )
    lower = (components / "speedsensorswc~1.arxml").read_text(encoding="utf-8")
    assert "<SHORT-NAME>speedsensorswc</SHORT-NAME>" in lower
    assert "<SHORT-NAME>SpeedSensorSWC</SHORT-NAME>" not in lower

    assert export_component()["success"] is True
    assert sorted(p.name for p in components.iterdir()) == [
        "SpeedCtrlSWC.arxml",
        "SpeedSensorSWC.arxml",
        "export-summary.json",
        "speedsensorswc~1.arxml",
    ]


def test_export_component_explicit_output_path_is_not_renamed(model_root, tmp_path):
    _add_a_case_only_twin(Path(model_root))
    out = tmp_path / "exports" / "speedsensorswc.arxml"
    result = export_component(swc_name="speedsensorswc", output_path=str(out))
    assert result["success"] is True, result
    assert [p.name for p in out.parent.iterdir()] == ["speedsensorswc.arxml"]


# ---------------------------------------------------------------------------
# export_component without swc_name: every component
# ---------------------------------------------------------------------------


def test_export_every_component_default_output_dir(model_root):
    result = export_component()
    assert result["success"] is True
    assert result["errors"] == []
    out_dir = _default_export_path("SpeedSensorSWC").parent
    assert (out_dir / "SpeedSensorSWC.arxml").is_file()
    assert (out_dir / "SpeedCtrlSWC.arxml").is_file()
    assert (out_dir / "export-summary.json").is_file()
    assert "SpeedSensorSWC" in result["output"]
    assert "SpeedCtrlSWC" in result["output"]


def test_export_every_component_explicit_output_dir(model_root, tmp_path):
    out_dir = tmp_path / "batch-out"
    result = export_component(output_dir=str(out_dir))
    assert result["success"] is True
    assert (out_dir / "SpeedSensorSWC.arxml").is_file()
    assert (out_dir / "export-summary.json").is_file()


def test_both_export_forms_write_the_data_types_marker(model_root, tmp_path):
    """Every component export states that its exporter invents no ``{B}_impl`` twin."""
    marker = '<SDG GID="SWCRAFT">'
    single = tmp_path / "single" / "speed.arxml"
    assert export_component(swc_name="SpeedSensorSWC", output_path=str(single))["success"] is True
    out_dir = tmp_path / "batch-out"
    assert export_component(output_dir=str(out_dir))["success"] is True

    for exported in (single, *sorted(out_dir.glob("*.arxml"), key=lambda path: path.as_posix())):
        text = exported.read_text(encoding="utf-8")
        assert marker in text, exported.name
        assert '<SD GID="DATA-TYPES">base and implementation types declared, no invented twin</SD>' in text


def test_export_every_component_partial_failure_reports_per_swc(model_root):
    # BrokenSWC's export fails alone; the others still succeed, success is False overall.
    _dump(
        Path(model_root) / "swc" / "example" / "BrokenSWC.yaml",
        {
            "name": "BrokenSWC",
            "type": "application",
            "ports": [{"name": "rGhost", "direction": "require", "interface": "IGhost"}],
            "runnables": [],
        },
    )
    result = export_component()
    assert result["success"] is False
    assert any("BrokenSWC" in e["message"] for e in result["errors"])
    out_dir = _default_export_path("SpeedSensorSWC").parent
    assert (out_dir / "SpeedSensorSWC.arxml").is_file()
    assert (out_dir / "SpeedCtrlSWC.arxml").is_file()
    assert not (out_dir / "BrokenSWC.arxml").exists()
    assert (out_dir / "export-summary.json").is_file()


def test_export_every_component_bad_manifest_returns_error_dict(monkeypatch, tmp_path):
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(tmp_path / "does-not-exist.yaml"))
    result = export_component()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "manifest" in result["errors"][0]["message"]
    assert "does-not-exist.yaml" in result["errors"][0]["message"]


# ---------------------------------------------------------------------------
# One tool, two widths: the oracle without the wrapper, the reply pin and the
# two refusals of a path argument of the other form
# ---------------------------------------------------------------------------


def _tree(directory: Path) -> dict[str, bytes]:
    """Every file under *directory*, by its POSIX path relative to it."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_every_component_is_the_bytes_the_function_writes(model_root, tmp_path, version):
    """The tool without ``swc_name`` writes what ``component_exchange.export_all_components()`` writes.

    File for file, the summary included.
    """
    _set_manifest_arxml_version(model_root, version)
    through_the_tool = tmp_path / "tool"
    oracle = tmp_path / "oracle"

    result = export_component(output_dir=str(through_the_tool))
    reports = component_exchange.export_all_components(str(model_root), str(oracle), version=version)

    assert result["success"] is True, result
    assert all(report.ok for report in reports)
    written = _tree(through_the_tool)
    assert sorted(written) == ["SpeedCtrlSWC.arxml", "SpeedSensorSWC.arxml", "export-summary.json"]
    assert written == _tree(oracle)


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_one_component_is_the_bytes_the_function_writes(model_root, tmp_path, version):
    """The tool with ``swc_name`` writes what ``component_exchange.export_component()`` writes."""
    _set_manifest_arxml_version(model_root, version)
    through_the_tool = tmp_path / "tool" / "speed.arxml"
    oracle = tmp_path / "oracle" / "speed.arxml"

    result = export_component(swc_name="SpeedSensorSWC", output_path=str(through_the_tool))
    report = component_exchange.export_component(str(model_root), "SpeedSensorSWC", str(oracle), version=version)

    assert result["success"] is True, result
    assert report.ok
    assert through_the_tool.read_bytes() == oracle.read_bytes()


def test_the_one_component_reply_is_the_reply_before_the_merge(model_root):
    """The reply to a named component is pinned: a client that names a component sees no change."""
    assert export_component(swc_name="SpeedSensorSWC") == {
        "success": True,
        "errors": [],
        "output": (
            "Component export report: SpeedSensorSWC\n"
            "STATUS ok\n"
            "OUTPUT dist/components/SpeedSensorSWC.arxml\n"
            "EXPORTED 1 interface(s)\n"
            "  - ISpeed\n"
            "EXPORTED 2 data type(s)\n"
            "  - float32\n"
            "  - float32_base\n"
            "Generated at AUTOSAR 4.2.2."
        ),
    }


# Copied, not imported: the test is the oracle for the server's sentence.
_DIR_WITH_A_NAME = (
    "output_dir is the directory of an export of every component; "
    "with swc_name, give output_path, the file, or neither."
)
_FILE_WITHOUT_A_NAME = (
    "output_path is the file of one component's export; without swc_name, give output_dir, the directory, or neither."
)


def _assert_refused(result: dict, sentence: str, given: tuple[Path, ...], default_dir: Path | None) -> None:
    """The whole reply is the refusal, it repeats no path given, and nothing was written."""
    assert result == {"success": False, "errors": [{"message": sentence, "path": ""}], "output": ""}
    wire = json.dumps(result)
    for path in given:
        for spelling in (path.as_posix(), str(path), json.dumps(str(path))[1:-1]):
            assert spelling not in wire
        assert not path.exists()
    if default_dir is not None:
        assert not default_dir.exists()


def _default_components_dir(model_root) -> Path:
    return Path(model_root).parent / "dist" / "components"


#: Every refused combination of the three arguments, with the sentence it gets. The
#: ``output_dir``-with-a-name check runs first, so all three arguments get its sentence;
#: a whitespace ``swc_name`` is a name (the tool does not strip it).
_REFUSED_COMBINATIONS = [
    pytest.param({"swc_name": "SpeedSensorSWC", "output_dir": "batch-out"}, _DIR_WITH_A_NAME, id="name-and-dir"),
    pytest.param({"output_path": "exports/speed.arxml"}, _FILE_WITHOUT_A_NAME, id="file-without-a-name"),
    pytest.param(
        {"swc_name": "", "output_path": "exports/speed.arxml"}, _FILE_WITHOUT_A_NAME, id="empty-name-and-file"
    ),
    pytest.param(
        {"swc_name": "SpeedSensorSWC", "output_path": "exports/speed.arxml", "output_dir": "batch-out"},
        _DIR_WITH_A_NAME,
        id="all-three",
    ),
    pytest.param({"swc_name": " ", "output_dir": "batch-out"}, _DIR_WITH_A_NAME, id="whitespace-name-and-dir"),
    pytest.param(
        {"output_path": "exports/speed.arxml", "output_dir": "batch-out"}, _FILE_WITHOUT_A_NAME, id="file-and-dir"
    ),
]


def _under(tmp_path: Path, arguments: dict) -> tuple[dict, tuple[Path, ...]]:
    """*arguments* with each path argument made absolute under *tmp_path*, and those paths."""
    placed = dict(arguments)
    given: list[Path] = []
    for key in ("output_path", "output_dir"):
        if key in placed:
            path = tmp_path / placed[key]
            placed[key] = str(path)
            given.append(path)
    return placed, tuple(given)


@pytest.mark.parametrize(("arguments", "sentence"), _REFUSED_COMBINATIONS)
def test_a_path_argument_of_the_other_form_is_refused(model_root, tmp_path, arguments, sentence):
    placed, given = _under(tmp_path, arguments)
    result = export_component(**placed)
    _assert_refused(result, sentence, given, _default_components_dir(model_root))
    assert not (tmp_path / "exports").exists()


def test_an_explicitly_empty_name_beside_output_dir_is_the_export_of_every_component(model_root, tmp_path):
    """``swc_name=""`` is no name: ``output_dir`` is then the second form's own argument, not a refusal."""
    given = tmp_path / "batch-out"
    result = export_component(swc_name="", output_dir=str(given))
    assert result["success"] is True, result
    assert sorted(p.name for p in given.iterdir()) == [
        "SpeedCtrlSWC.arxml",
        "SpeedSensorSWC.arxml",
        "export-summary.json",
    ]


@pytest.mark.parametrize(("arguments", "sentence"), _REFUSED_COMBINATIONS)
def test_the_refusals_come_before_the_project_is_read(monkeypatch, tmp_path, arguments, sentence):
    """A manifest that cannot be read still gets the refusal, not the manifest error, and nothing is written."""
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(tmp_path / "does-not-exist.yaml"))
    monkeypatch.setattr(_server, "_PROJECT", None)
    elsewhere = tmp_path / "elsewhere"
    placed, given = _under(elsewhere, arguments)
    result = export_component(**placed)
    _assert_refused(result, sentence, given, None)
    assert not elsewhere.exists()


# ---------------------------------------------------------------------------
# import_component: plans a draft `change_set` against a `baseline` and writes nothing
# ---------------------------------------------------------------------------


def _baseline() -> str:
    """A real token from `discover`, which is where a client gets one."""
    return call("discover").structured["baseline"]


def _model_files(model_root) -> dict[str, bytes]:
    root = Path(model_root)
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if p.is_file()
    }


def test_import_component_reimport_is_a_no_op_with_no_change_set(model_root):
    source = _exported_speed_sensor()
    result = import_component(source=str(source), baseline=_baseline())
    assert result["success"] is True
    assert result["errors"] == []
    assert "unchanged" in result["output"]
    assert result["change_set"] is None
    assert not (Path(model_root) / "swc" / "components").exists()


def test_import_component_plans_an_absent_swc_without_creating_it(model_root):
    source = _exported_speed_sensor()
    swc_file = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    swc_file.unlink()

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is True
    assert "created" in result["output"]
    assert [op["address"] for op in result["change_set"]["operations"]] == [
        {"layer": "swc", "element": "SpeedSensorSWC"}
    ]
    # only `change_set` writes the file
    assert not (Path(model_root) / "swc" / "components" / "SpeedSensorSWC.yaml").exists()


def test_import_component_prints_a_descriptive_loss_and_plans_past_it(model_root):
    """A value that only documents (an interface's ``UUID``) is printed and does not refuse the import.

    It appears in the plan's preview under *Not carried from the file*; a behavioural one still refuses.
    """
    source = _exported_speed_sensor()
    text = source.read_text(encoding="utf-8")
    source.write_text(
        text.replace("<SENDER-RECEIVER-INTERFACE>", '<SENDER-RECEIVER-INTERFACE UUID="1234">', 1),
        encoding="utf-8",
        newline="\n",
    )

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is True, result
    assert "Not carried from the file (1; " in result["output"]
    assert "UUID not carried" in result["output"]


def test_import_component_writes_neither_model_nor_audit_bytes(model_root):
    source = _exported_speed_sensor()
    swc_file = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    swc_file.unlink()
    project = _server._get_project()
    audit_path = Path(project.resolved_dist_root) / "audit.jsonl"
    audit_before = audit_path.read_text(encoding="utf-8").splitlines() if audit_path.exists() else []
    model_before = _model_files(model_root)

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is True
    assert _model_files(model_root) == model_before
    assert (audit_path.read_text(encoding="utf-8").splitlines() if audit_path.exists() else []) == audit_before


def _diverge_target_swc(model_root) -> None:
    """Make the target's SpeedSensorSWC diverge from the exported one."""
    swc_file = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    data["runnables"] = [{"name": "ExtraRun", "min_start_interval": 0.0, "events": []}]
    _dump(swc_file, data)


def test_import_component_divergent_swc_rejected_without_update(model_root):
    source = _exported_speed_sensor()
    _diverge_target_swc(model_root)
    before = (Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml").read_bytes()
    result = import_component(source=str(source), baseline=_baseline())
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "SpeedSensorSWC" in result["errors"][0]["message"]
    assert result["change_set"] is None
    # nothing written — the divergent target file is untouched
    assert (Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml").read_bytes() == before


def test_import_component_divergent_swc_update_plans_leaf_operations_with_a_diff_preview(model_root):
    source = _exported_speed_sensor()
    _diverge_target_swc(model_root)
    swc_file = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    before = swc_file.read_bytes()

    result = import_component(source=str(source), baseline=_baseline(), update=True)

    assert result["success"] is True
    assert "Update diff preview" in result["output"]
    # leaf by leaf: a protocol `update` cannot name a whole element
    assert result["change_set"]["operations"] == [
        {
            "operation": "remove",
            "address": {
                "layer": "swc",
                "element": "SpeedSensorSWC",
                "path": [{"field": "runnables"}, {"member": "ExtraRun"}],
            },
        }
    ]
    assert swc_file.read_bytes() == before
    assert not (Path(model_root) / "swc" / "components" / "SpeedSensorSWC.yaml").exists()


def test_import_component_divergent_shared_interface_rejected_even_with_update(model_root):
    source = _exported_speed_sensor()
    _dump(
        Path(model_root) / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "velocity", "implementation_type": "float32"}],
        },
    )
    result = import_component(source=str(source), baseline=_baseline(), update=True)
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "ISpeed" in result["errors"][0]["message"]
    assert result["change_set"] is None


def _name_a_compu_method(model_root) -> Path:
    """``float32`` (the type of ``ISpeed.speed``) names a texttable element; the element's file is returned.

    The component export's closure follows ``compu_method_ref`` into the ``compu_methods`` layer.
    """
    table = model_element_path(model_root, "ProbeSpeedTable", layer="compu_methods")
    _dump(table, {"name": "ProbeSpeedTable", "kind": "texttable", "entries": [{"name": "ProbeStill", "value": 0}]})
    float32 = model_element_path(model_root, "float32", kind="value_type")
    _dump(
        float32,
        {"name": "float32", "kind": "value_type", "base": "float32_base", "compu_method_ref": "ProbeSpeedTable"},
    )
    return table


def test_import_component_plans_an_absent_compu_method_element_without_writing_it(model_root):
    """The plan creates the element in its own layer and names it by layer and name, never by a file."""
    table = _name_a_compu_method(model_root)
    source = _exported_speed_sensor()
    table.unlink()
    before = _model_files(model_root)

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is True, result
    assert "CREATED 1 compu method(s)\n  - ProbeSpeedTable" in result["output"]
    assert [op["address"] for op in result["change_set"]["operations"]] == [
        {"layer": "compu_methods", "element": "ProbeSpeedTable"}
    ]
    assert str(model_root) not in result["output"]
    assert _model_files(model_root) == before


def test_import_component_differing_compu_method_element_rejected_by_layer_and_name(model_root):
    """A shared element that diverges is a conflict; the refusal names no file."""
    table = _name_a_compu_method(model_root)
    source = _exported_speed_sensor()
    _dump(table, {"name": "ProbeSpeedTable", "kind": "texttable", "entries": [{"name": "ProbeStill", "value": 1}]})

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is False
    assert len(result["errors"]) == 1
    message = result["errors"][0]["message"]
    assert "shared compu method 'ProbeSpeedTable' diverges" in message
    assert str(model_root) not in message and ".yaml" not in message
    assert result["change_set"] is None


def test_import_component_refuses_an_export_whose_swcraft_group_carries_an_unknown_sd(model_root):
    """An unknown ``SD`` beside the data-types marker aborts the import.

    The marker group is the writer's own only as a whole; such an ``SD`` is vendor metadata.
    """
    source = _exported_speed_sensor()
    anchor = '<SD GID="DATA-TYPES">'
    text = source.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    source.write_text(text.replace(anchor, '<SD GID="VENDOR-FLAG">1</SD>' + anchor), encoding="utf-8", newline="\n")
    before = _model_files(model_root)

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "vendor metadata" in result["errors"][0]["message"]
    assert "SWC-INTERNAL-BEHAVIOR 'SpeedSensorSWC_InternalBehavior'" in result["errors"][0]["message"]
    assert result["change_set"] is None
    assert _model_files(model_root) == before


def test_import_component_refuses_an_export_with_text_after_an_sd_of_its_swcraft_group(model_root):
    """Character data beside the marker is content the exporter did not write, and the import aborts on it."""
    source = _exported_speed_sensor()
    anchor = "no invented twin</SD>"
    text = source.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    source.write_text(text.replace(anchor, anchor + "tailpayload"), encoding="utf-8", newline="\n")
    before = _model_files(model_root)

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "vendor metadata" in result["errors"][0]["message"]
    assert "SWC-INTERNAL-BEHAVIOR 'SpeedSensorSWC_InternalBehavior'" in result["errors"][0]["message"]
    assert result["change_set"] is None
    assert _model_files(model_root) == before


def test_import_component_refuses_an_export_with_a_second_admin_data_beside_its_marker(model_root):
    """A second ``ADMIN-DATA`` beside the marker's is reported, and the import aborts.

    An element's ``ADMIN-DATA`` is admitted only as the writer's one block.
    """
    source = _exported_speed_sensor()
    anchor = "</ADMIN-DATA>"
    text = source.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    source.write_text(
        text.replace(
            anchor, anchor + '<ADMIN-DATA><SDGS><SDG GID="VENDOR"><SD GID="X">1</SD></SDG></SDGS></ADMIN-DATA>'
        ),
        encoding="utf-8",
        newline="\n",
    )
    before = _model_files(model_root)

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "vendor metadata" in result["errors"][0]["message"]
    assert "<second ADMIN-DATA>, VENDOR" in result["errors"][0]["message"]
    assert result["change_set"] is None
    assert _model_files(model_root) == before


def test_import_component_reads_back_an_export_carrying_both_marker_sds(model_root):
    """The control: an export carrying both marker ``SD``s re-imports unchanged.

    A component with an access point exports ``ACCESS-POINTS`` and ``DATA-TYPES`` in one group.
    """
    swc_file = Path(model_root) / "swc" / "example" / "SpeedSensorSWC.yaml"
    data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    data["runnables"] = [
        {"name": "SpeedRun", "min_start_interval": 0.01, "writes": [{"port": "pSpeed", "element": "speed"}]}
    ]
    _dump(swc_file, data)
    source = _exported_speed_sensor()
    text = source.read_text(encoding="utf-8")
    assert '<SD GID="ACCESS-POINTS">' in text
    assert '<SD GID="DATA-TYPES">' in text

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is True, result
    assert "unchanged" in result["output"]
    assert result["change_set"] is None


def test_import_component_refuses_an_export_with_an_admin_data_outside_the_autosar_namespace(model_root):
    """An element outside the AUTOSAR namespace is reported and not walked.

    An ``ADMIN-DATA`` in a vendor's namespace beside the marker aborts the import.
    """
    source = _exported_speed_sensor()
    anchor = "</ADMIN-DATA>"
    text = source.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    source.write_text(
        text.replace(
            anchor,
            anchor
            + '<ADMIN-DATA xmlns="urn:vendor"><SDGS><SDG GID="VENDOR"><SD GID="X">1</SD></SDG></SDGS></ADMIN-DATA>',
        ),
        encoding="utf-8",
        newline="\n",
    )
    before = _model_files(model_root)

    result = import_component(source=str(source), baseline=_baseline())

    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "element ADMIN-DATA outside the AUTOSAR namespace (urn:vendor)" in result["errors"][0]["message"]
    assert result["change_set"] is None
    assert _model_files(model_root) == before


def test_import_component_missing_source_returns_error_dict(model_root):
    result = import_component(source=str(Path(model_root).parent / "nope.arxml"), baseline=_baseline())
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["message"] != ""
    assert result["change_set"] is None


def test_import_component_bad_manifest_returns_error_dict(monkeypatch, tmp_path):
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(tmp_path / "does-not-exist.yaml"))
    result = import_component(source=str(tmp_path / "x.arxml"), baseline="rev1:AAAAAAAAAAAAAAAAAAAAAA")
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "manifest" in result["errors"][0]["message"]
    assert "does-not-exist.yaml" in result["errors"][0]["message"]
    assert result["change_set"] is None


# ---------------------------------------------------------------------------
# AUTOSAR version: the manifest is the single source (no per-call argument), so the CLI and the agent cannot
# disagree. Asserted on xsi:schemaLocation, the whole observable difference between the versions.
# ---------------------------------------------------------------------------


def _set_manifest_arxml_version(model_root, version: str) -> None:
    """Declare ``arxml_version`` in the test project's manifest.

    Safe before the first tool call only: ``_get_project()`` caches the loaded project.
    """
    manifest_path = Path(model_root).parent / "swcraft.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["arxml_version"] = version
    manifest_path.write_text(yaml.safe_dump(manifest, default_flow_style=False, sort_keys=False), encoding="utf-8")


def test_export_component_honors_manifest_arxml_version(model_root):
    _set_manifest_arxml_version(model_root, "4.3.0")

    result = export_component(swc_name="SpeedSensorSWC")

    assert result["success"] is True, result["errors"]
    exported = _default_export_path("SpeedSensorSWC").read_text(encoding="utf-8")
    assert "AUTOSAR_4-3-0.xsd" in exported
    assert "AUTOSAR_4-2-2.xsd" not in exported
    assert "4.3.0" in result["output"]


def test_export_component_without_manifest_arxml_version_targets_4_2_2(model_root):
    """A manifest with no ``arxml_version`` exports 4.2.2 components."""
    result = export_component(swc_name="SpeedSensorSWC")

    assert result["success"] is True, result["errors"]
    exported = _default_export_path("SpeedSensorSWC").read_text(encoding="utf-8")
    assert "AUTOSAR_4-2-2.xsd" in exported
    assert "AUTOSAR_4-3-0.xsd" not in exported
    assert "4.2.2" in result["output"]


def test_export_component_unsupported_manifest_version_returns_error_dict(model_root):
    """An unsupported manifest value fails at load time as the normal error dict, and nothing is exported."""
    _set_manifest_arxml_version(model_root, "4.9.9")

    result = export_component(swc_name="SpeedSensorSWC")

    assert result["success"] is False
    assert len(result["errors"]) == 1
    # The sentence names the field and the kind of error, never the value.
    message = result["errors"][0]["message"]
    assert "field `arxml_version` is invalid" in message
    assert "4.9.9" not in message
    # Not via _default_export_path(): that helper loads the project, which is
    # exactly what fails here. The manifest's dist_root is a literal "dist".
    assert not (Path(model_root).parent / "dist" / "components").exists()


def test_export_every_component_honors_manifest_arxml_version(model_root):
    _set_manifest_arxml_version(model_root, "4.3.0")

    result = export_component()

    assert result["success"] is True, result["errors"]
    out_dir = _default_export_path("SpeedSensorSWC").parent
    exported = sorted(out_dir.glob("*.arxml"), key=lambda path: path.as_posix())
    assert [p.name for p in exported] == ["SpeedCtrlSWC.arxml", "SpeedSensorSWC.arxml"]
    for arxml in exported:
        text = arxml.read_text(encoding="utf-8")
        assert "AUTOSAR_4-3-0.xsd" in text
        assert "AUTOSAR_4-2-2.xsd" not in text
    assert "4.3.0" in result["output"]
    # The summary names the exported components, not their version.
    assert "4.3.0" not in (out_dir / "export-summary.json").read_text(encoding="utf-8")


def test_export_every_component_without_manifest_arxml_version_targets_4_2_2(model_root):
    result = export_component()

    assert result["success"] is True, result["errors"]
    out_dir = _default_export_path("SpeedSensorSWC").parent
    for arxml in sorted(out_dir.glob("*.arxml"), key=lambda path: path.as_posix()):
        assert "AUTOSAR_4-2-2.xsd" in arxml.read_text(encoding="utf-8")
    assert "4.2.2" in result["output"]


def test_export_every_component_unsupported_manifest_version_returns_error_dict(model_root):
    _set_manifest_arxml_version(model_root, "4.9.9")

    result = export_component()

    assert result["success"] is False
    assert len(result["errors"]) == 1
    # The sentence names the field and the kind of error, never the value.
    message = result["errors"][0]["message"]
    assert "field `arxml_version` is invalid" in message
    assert "4.9.9" not in message
    assert not (Path(model_root).parent / "dist" / "components").exists()


#: The guide section holding the regimes an update follows; the tool description, which a host cuts at 4 096
#: characters, points the host there.
_IMPORT_GUIDE_PAGE = Path(__file__).resolve().parents[3] / "docs" / "guide" / "how-to" / "import-a-component.md"
_IMPORT_GUIDE_SECTION = "how-to/import-a-component#what-the-plan-decides"


def _linked_section() -> str:
    """The section the tool description links: its heading down to the next heading of its level or above.

    A line inside a fenced code block is never a heading.
    """
    anchor = "{ #" + _IMPORT_GUIDE_SECTION.split("#", 1)[1] + " }"
    lines = _IMPORT_GUIDE_PAGE.read_text(encoding="utf-8").splitlines()
    headings: list[tuple[int, int]] = []
    fenced = False
    for number, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            headings.append((number, len(line) - len(line.lstrip("#"))))
    start = next((number for number, _ in headings if lines[number].endswith(anchor)), None)
    assert start is not None, f"no heading of {_IMPORT_GUIDE_PAGE.name} carries {anchor}"
    level = next(depth for number, depth in headings if number == start)
    end = next((number for number, depth in headings if number > start and depth <= level), len(lines))
    return "\n".join(lines[start:end])


def test_import_component_points_the_host_to_the_rules_section():
    """The published description links the section that holds the update's rules, and the page holds its anchor.

    A link check from what a host receives (``tools/list``) into the guide. What this does not reach: whether
    the built corpus serves it.
    """
    (tool,) = [tool for tool in list_tools() if tool.name == "import_component"]
    assert f'guide(section="{_IMPORT_GUIDE_SECTION}")' in (tool.description or "")
    anchor = _IMPORT_GUIDE_SECTION.split("#", 1)[1]
    assert f"{{ #{anchor} }}" in _IMPORT_GUIDE_PAGE.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "regime",
    [
        ("`INTER-RUNNABLE-VARIABLES`", "`EXCLUSIVE-AREAS`", "`can_enter`", "`runs_inside`"),
        ("`DATA-EVENTS`",),
    ],
    ids=["variables-and-areas", "data-received-events"],
)
def test_import_component_states_each_regime_in_the_section_it_links(regime: tuple[str, ...]):
    """The section the host is pointed to names each regime's marker and the model keywords it decides."""
    section = _linked_section()
    assert [term for term in regime if term not in section] == []
