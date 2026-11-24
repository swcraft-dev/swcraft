"""Tests for component_exchange.export: the single-component and the batch ARXML export.

A single export materializes one SWC's forward dependency closure as a temp YAML tree, validates only that
closure (no requirement-link/connector/task checks), runs the writer on it and touches the real
``output_path`` only by an atomic ``os.replace`` after generation succeeds. ``export_all_components`` runs it
once per SWC into an output dir, reports one SWC's failure without aborting the batch, and writes a
deterministic ``export-summary.json``.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import _write_manifest, model_element_path, write_model_element
from external_stubs_support import change, interface_stub, type_stub
from swcraft_tools.arxml_validation import MODE_XSD, check_arxml
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.capability_markers import (
    ASYNCHRONOUS_CALLS,
    DATA_EVENTS,
    DATA_TYPES,
    EXCLUSIVE_AREAS,
    IMPLICIT_ACCESS,
    INTER_RUNNABLE_VARIABLES,
    OPERATION_EVENTS,
)
from swcraft_tools.cli import main
from swcraft_tools.component_exchange import (
    EXPORT_SUMMARY_FILENAME,
    AmbiguousComponentError,
    ClosureValidationError,
    ComponentExchangeError,
    ComponentNotFoundError,
    default_component_file_name,
    default_component_output,
    export_all_components,
    export_component,
    validate_component_closure,
)
from swcraft_tools.component_exchange.closure import MissingReferenceError, compute_closure
from swcraft_tools.component_exchange.export import _batch_file_names
from swcraft_tools.diagnostics import Subject
from swcraft_tools.reader import find_swc_file
from swcraft_tools.schema import ExternalDataType, ExternalInterface
from swcraft_tools.validator import check_cross_layer_consistency, check_name_space_collisions
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_generation import (
    generate_component_arxml,
)
from xsd_support import require_licensed_schema

# Fixture model: TargetSwc uses both interface kinds and six data type kinds: base type, value type, application
# type, enum, record and array (``test_component_closure.py``'s chain cases cover the reference type);
# OtherSwc/IOther/Other_T exist only to prove unrelated content is excluded. No requirements/ dir (scoped
# validation must not demand a requirement link) and no connectors/tasks/sequences.


def _dump(path: Path, data: dict[str, Any]) -> None:
    """Write *data* at *path*; a type kind spelled under ``data_types/`` goes to its own layer, picked by ``kind``."""
    if path.parent.name == "data_types" and data.get("kind") is not None:
        path = model_element_path(path.parent.parent, path.stem, kind=data["kind"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _build_model(tmp_path: Path) -> str:
    model_root = tmp_path / "model"
    dt = model_root / "data_types"
    # Each base type declares its C type name, since the writer never derives one.
    _dump(
        model_element_path(model_root, "uint8_base", kind="base_type"),
        {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
    )
    _dump(dt / "uint8.yaml", {"name": "uint8", "kind": "value_type", "base": "uint8_base"})
    _dump(
        model_element_path(model_root, "float32_base", kind="base_type"),
        {
            "name": "float32_base",
            "kind": "base_type",
            "size": 32,
            "encoding": "IEEE754",
            "native_declaration": "float32",
        },
    )
    _dump(dt / "float32.yaml", {"name": "float32", "kind": "value_type", "base": "float32_base"})
    _dump(dt / "Speed_T.yaml", {"name": "Speed_T", "kind": "value_type", "base": "float32_base"})
    _dump(
        dt / "Gear_T.yaml",
        {
            "name": "Gear_T",
            "kind": "enum",
            "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
        },
    )
    _dump(
        dt / "Pose_T.yaml",
        {
            "name": "Pose_T",
            "kind": "record",
            "fields": [{"name": "x", "type": "Distance_T"}, {"name": "gear", "type": "Gear_T"}],
        },
    )
    # A composite member names an application type and the mapping set carries the implementation type it is
    # stored as. `Distance_T` declares no physics, `Velocity_T` all three optional fields: both shapes are in
    # the closure.
    _dump(dt / "Samples_T.yaml", {"name": "Samples_T", "kind": "array", "element_type": "Velocity_T", "length": 4})
    _dump(dt / "Distance_T.yaml", {"name": "Distance_T", "kind": "application_type"})
    _dump(
        dt / "Velocity_T.yaml",
        {
            "name": "Velocity_T",
            "kind": "application_type",
            "unit": "km/h",
            "conversion": {"factor": 0.1, "offset": 0.0},
            "range": {"min": 0.0, "max": 400.0},
        },
    )
    write_model_element(
        model_root,
        {
            "name": "PhysicalTypes",
            "maps": [
                {"application": "Distance_T", "implementation": "float32"},
                {"application": "Velocity_T", "implementation": "Speed_T"},
            ],
        },
        layer="mapping_sets",
    )
    _dump(
        model_element_path(model_root, "Other_T_base", kind="base_type"),
        {"name": "Other_T_base", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "Other_T"},
    )
    _dump(dt / "Other_T.yaml", {"name": "Other_T", "kind": "value_type", "base": "Other_T_base"})

    ifaces = model_root / "interfaces"
    _dump(
        ifaces / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "speed", "implementation_type": "Speed_T"},
                {"name": "pose", "application_type": "Pose_T"},
            ],
        },
    )
    _dump(
        ifaces / "ICalib.yaml",
        {
            "name": "ICalib",
            "kind": "client_server",
            "operations": [
                {
                    "name": "SetOffset",
                    "arguments": [
                        {"name": "samples", "direction": "in", "application_type": "Samples_T"},
                        {"name": "raw", "direction": "out", "implementation_type": "uint8"},
                    ],
                }
            ],
        },
    )
    _dump(
        ifaces / "IOther.yaml",
        {
            "name": "IOther",
            "kind": "sender_receiver",
            "data_elements": [{"name": "v", "implementation_type": "Other_T"}],
        },
    )

    swc = model_root / "swc"
    _dump(
        swc / "body" / "TargetSwc.yaml",
        {
            "name": "TargetSwc",
            "type": "application",
            "ports": [
                {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
                {"name": "rCalib", "direction": "require", "interface": "ICalib"},
            ],
            "runnables": [{"name": "TargetRunnable", "events": [{"type": "timing", "period": 0.01}]}],
            "handle_termination_and_restart": "no_support",
        },
    )
    _dump(
        swc / "body" / "OtherSwc.yaml",
        {
            "name": "OtherSwc",
            "ports": [{"name": "pOther", "direction": "provide", "interface": "IOther"}],
            "handle_termination_and_restart": "no_support",
        },
    )
    return str(model_root)


def _short_names(arxml_path: Path) -> set[str]:
    """Every SHORT-NAME value in the generated ARXML (namespace-agnostic)."""
    root = ET.parse(arxml_path).getroot()
    return {el.text for el in root.iter() if el.tag.split("}")[-1] == "SHORT-NAME" and el.text}


def _local_tags(arxml_path: Path) -> set[str]:
    root = ET.parse(arxml_path).getroot()
    return {el.tag.split("}")[-1] for el in root.iter()}


class TestExportContent:
    def test_export_writes_exactly_the_closure(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert out.exists()
        names = _short_names(out)
        # `float32` is not in the closure: `Pose_T`'s field names the application type `Distance_T`, which rests
        # on nothing, so the value type it is stored as is reached by no edge of the closure walk.
        assert "TargetSwc" in names
        assert {"ISpeed", "ICalib"} <= names
        assert {"uint8", "Speed_T", "Gear_T", "Pose_T", "Samples_T", "Distance_T", "Velocity_T"} <= names
        assert "float32" not in names
        assert "OtherSwc" not in names
        assert "IOther" not in names
        assert "Other_T" not in names

    def test_export_contains_no_composition_or_connector(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        tags = _local_tags(out)
        assert not any("COMPOSITION" in t for t in tags)
        assert not any("CONNECTOR" in t for t in tags)

    def test_export_returns_the_export_report(self, tmp_path: Path) -> None:
        """The export returns a name-based ComponentExportReport."""
        model_root = _build_model(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        result = export_component(model_root, "TargetSwc", str(out))

        assert result.swc == "TargetSwc"
        assert result.status == "ok"
        assert result.output_path == str(out)
        assert result.error is None
        assert result.interfaces == ["ICalib", "ISpeed"]
        assert result.data_types == [
            "Distance_T",
            "Gear_T",
            "Pose_T",
            "Samples_T",
            "Speed_T",
            "Velocity_T",
            "float32_base",
            "uint8",
            "uint8_base",
        ]
        rendered = result.render()
        assert "TargetSwc" in rendered
        assert "ok" in rendered

    def test_no_tmp_sibling_left_after_success(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        out_dir = tmp_path / "out"
        out = out_dir / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert [p.name for p in out_dir.iterdir()] == ["TargetSwc.arxml"]

    def test_successful_export_overwrites_pre_existing_output(self, tmp_path: Path) -> None:
        """A stale file already at ``output_path`` is atomically replaced by a successful export."""
        model_root = _build_model(tmp_path)
        output = tmp_path / "TargetSwc.arxml"
        output.write_text("stale pre-existing content", encoding="utf-8")

        report = export_component(model_root, "TargetSwc", str(output))

        assert report.status == "ok"
        content = output.read_text(encoding="utf-8")
        assert "stale pre-existing content" not in content
        assert "TargetSwc" in content
        assert not Path(f"{output}.tmp").exists()


class TestExportDeterminism:
    def test_two_exports_of_unchanged_model_are_byte_identical(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        out1 = tmp_path / "out" / "first.arxml"
        out2 = tmp_path / "out" / "second.arxml"

        export_component(model_root, "TargetSwc", str(out1))
        export_component(model_root, "TargetSwc", str(out2))

        assert out1.read_bytes() == out2.read_bytes()


class TestScopedValidation:
    def test_export_succeeds_despite_broken_unrelated_content(self, tmp_path: Path) -> None:
        """A dangling reference and a broken connector outside the closure do not block the export."""
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "swc" / "body" / "BrokenSwc.yaml",
            {
                "name": "BrokenSwc",
                "ports": [{"name": "pGone", "direction": "provide", "interface": "IUndeclared"}],
            },
        )
        bad_connector = Path(model_root) / "connectors" / "malformed.yaml"
        bad_connector.parent.mkdir(parents=True, exist_ok=True)
        bad_connector.write_text("{ this is: [ not valid yaml", encoding="utf-8")
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert out.exists()
        assert "TargetSwc" in _short_names(out)

    def test_export_succeeds_with_no_requirements_at_all(self, tmp_path: Path) -> None:
        """With no requirements/ dir the export succeeds: no trace-link check runs at closure scope."""
        model_root = _build_model(tmp_path)
        assert not (Path(model_root) / "requirements").exists()
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert out.exists()

    def test_export_succeeds_for_a_component_no_requirement_links_to(self, tmp_path: Path) -> None:
        """A component no requirement links to is no finding for the whole-model gate and exports fine."""
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "requirements" / "REQ-OTHER-001.yaml",
            {
                "id": "REQ-OTHER-001",
                "title": "Other",
                "description": "OtherSwc alone satisfies it.",
                "version": 1,
                "source": "architecture",
            },
        )
        other = Path(find_swc_file(model_root, "OtherSwc"))
        _dump(other, {**yaml.safe_load(other.read_text(encoding="utf-8")), "satisfies": ["REQ-OTHER-001@1"]})
        whole = check_cross_layer_consistency(model_root)
        assert whole.warnings == ()
        assert not any(Subject("swc", "TargetSwc") in record.subjects for record in whole.records)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert out.exists()
        assert "TargetSwc" in _short_names(out)

    def test_validate_component_closure_flags_bad_data_type_reference(self, tmp_path: Path) -> None:
        """A value type over a value type fails validate_component_closure: its data-type checks run.

        A value type's `base` resolves in the `base_types` layer alone, so `A_T` is an undeclared base."""
        closure_root = tmp_path / "closure"
        write_model_element(closure_root, {"name": "f32", "kind": "base_type", "size": 32, "encoding": "IEEE754"})
        write_model_element(closure_root, {"name": "A_T", "kind": "value_type", "base": "f32"})
        write_model_element(closure_root, {"name": "B_T", "kind": "value_type", "base": "A_T"})

        with pytest.raises(ClosureValidationError, match="not declared in the base_types layer") as exc_info:
            validate_component_closure(str(closure_root))
        assert any("B_T" in issue and "'A_T'" in issue for issue in exc_info.value.issues)


class TestExportFailures:
    def test_unknown_swc_fails_and_leaves_no_output(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ComponentNotFoundError, match="NoSuchSwc"):
            export_component(model_root, "NoSuchSwc", str(out_dir / "NoSuchSwc.arxml"))

        assert list(out_dir.iterdir()) == []

    def test_ambiguous_swc_fails_and_leaves_no_output(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "swc" / "powertrain" / "TargetSwcCopy.yaml",
            {"name": "TargetSwc", "description": "duplicate of body/TargetSwc.yaml"},
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(AmbiguousComponentError, match="TargetSwc"):
            export_component(model_root, "TargetSwc", str(out_dir / "TargetSwc.arxml"))

        assert list(out_dir.iterdir()) == []

    def test_closure_validation_failure_is_typed_and_leaves_no_output(self, tmp_path: Path) -> None:
        """A closure that passes the reference walk but fails scoped validation raises a typed error, no output."""
        model_root = _build_model(tmp_path)
        write_model_element(
            Path(model_root),
            {
                "name": "DoubleAlias_T",
                "kind": "enum",
                "literals": [{"name": "A", "value": 0}, {"name": "B", "value": 0}],
            },
        )
        _dump(
            Path(model_root) / "interfaces" / "IAliased.yaml",
            {
                "name": "IAliased",
                "kind": "sender_receiver",
                "data_elements": [{"name": "d", "application_type": "DoubleAlias_T"}],
            },
        )
        _dump(
            Path(model_root) / "swc" / "body" / "AliasSwc.yaml",
            {
                "name": "AliasSwc",
                "ports": [{"name": "pAliased", "direction": "provide", "interface": "IAliased"}],
            },
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ClosureValidationError, match="duplicate literal value"):
            export_component(model_root, "AliasSwc", str(out_dir / "AliasSwc.arxml"))

        assert list(out_dir.iterdir()) == []

    def test_a_value_type_over_a_value_type_is_a_missing_reference_and_leaves_no_output(self, tmp_path: Path) -> None:
        """`base` names no declared base type, so the closure walk itself refuses it."""
        model_root = _build_model(tmp_path)
        write_model_element(Path(model_root), {"name": "DoubleAlias_T", "kind": "value_type", "base": "Speed_T"})
        _dump(
            Path(model_root) / "interfaces" / "IAliased.yaml",
            {
                "name": "IAliased",
                "kind": "sender_receiver",
                "data_elements": [{"name": "d", "implementation_type": "DoubleAlias_T"}],
            },
        )
        _dump(
            Path(model_root) / "swc" / "body" / "AliasSwc.yaml",
            {"name": "AliasSwc", "ports": [{"name": "pAliased", "direction": "provide", "interface": "IAliased"}]},
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(MissingReferenceError, match="Speed_T") as exc_info:
            export_component(model_root, "AliasSwc", str(out_dir / "AliasSwc.arxml"))

        assert exc_info.value.missing == frozenset({"Speed_T"})
        assert list(out_dir.iterdir()) == []

    def test_generation_failure_never_creates_output_path(self, tmp_path: Path) -> None:
        """A failure inside generation leaves neither ``output_path`` nor its ``.tmp`` sibling."""
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "swc" / "body" / "ServiceSwc.yaml",
            {
                "name": "ServiceSwc",
                "type": "service",
                "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
                "handle_termination_and_restart": "no_support",
            },
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ValueError, match="not supported"):
            export_component(model_root, "ServiceSwc", str(out_dir / "ServiceSwc.arxml"))

        assert list(out_dir.iterdir()) == []

    def test_partially_written_tmp_is_removed_and_existing_output_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Generation dying mid-write removes the ``.tmp`` and leaves an existing output's bytes as they were."""
        import swcraft_tools.component_exchange.export as export_module

        model_root = _build_model(tmp_path)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        out = out_dir / "TargetSwc.arxml"
        out.write_bytes(b"previous export content")

        def _dying_generate(model_root: str, output_path: str, **kwargs: object) -> str:
            # **kwargs absorbs the keyword-only `version=`: the double stands in for the writer, not its signature.
            Path(output_path).write_bytes(b"partial garbage")
            raise RuntimeError("disk full mid-save")

        monkeypatch.setattr(export_module, "generate_component_arxml", _dying_generate)

        with pytest.raises(RuntimeError, match="disk full"):
            export_component(model_root, "TargetSwc", str(out))

        assert [p.name for p in out_dir.iterdir()] == ["TargetSwc.arxml"]
        assert out.read_bytes() == b"previous export content"


def _add_broken_swc(model_root: str) -> None:
    """Add a SWC whose port references an undeclared interface: it fails at closure time."""
    _dump(
        Path(model_root) / "swc" / "body" / "BrokenSwc.yaml",
        {
            "name": "BrokenSwc",
            "ports": [{"name": "pGone", "direction": "provide", "interface": "IUndeclared"}],
        },
    )


class TestBatchExport:
    def test_batch_export_writes_one_file_per_swc_and_reports(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        assert [r.swc for r in reports] == ["OtherSwc", "TargetSwc"]
        assert all(r.status == "ok" for r in reports)
        assert all(r.error is None for r in reports)
        assert (out_dir / "OtherSwc.arxml").exists()
        assert (out_dir / "TargetSwc.arxml").exists()
        assert "TargetSwc" not in _short_names(out_dir / "OtherSwc.arxml")
        assert "OtherSwc" not in _short_names(out_dir / "TargetSwc.arxml")

    def test_batch_export_is_deterministic(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        dir1 = tmp_path / "batch1"
        dir2 = tmp_path / "batch2"

        export_all_components(model_root, str(dir1))
        export_all_components(model_root, str(dir2))

        names1 = sorted(p.name for p in dir1.iterdir())
        names2 = sorted(p.name for p in dir2.iterdir())
        assert names1 == names2
        for name in names1:
            assert (dir1 / name).read_bytes() == (dir2 / name).read_bytes()

    def test_one_failed_swc_does_not_abort_the_batch(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _add_broken_swc(model_root)
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        by_name = {r.swc: r for r in reports}
        assert set(by_name) == {"BrokenSwc", "OtherSwc", "TargetSwc"}
        assert by_name["BrokenSwc"].status == "failed"
        assert by_name["BrokenSwc"].error is not None
        assert "IUndeclared" in by_name["BrokenSwc"].error
        assert by_name["BrokenSwc"].output_path is None
        assert not (out_dir / "BrokenSwc.arxml").exists()
        rendered = by_name["BrokenSwc"].render()
        assert "failed" in rendered
        assert "IUndeclared" in rendered
        assert by_name["OtherSwc"].status == "ok"
        assert by_name["TargetSwc"].status == "ok"
        assert (out_dir / "OtherSwc.arxml").exists()
        assert (out_dir / "TargetSwc.arxml").exists()
        summary = json.loads((out_dir / EXPORT_SUMMARY_FILENAME).read_text(encoding="utf-8"))
        entry = next(e for e in summary if e["swc"] == "BrokenSwc")
        assert entry["status"] == "failed"
        assert entry["output_path"] is None
        assert "IUndeclared" in entry["error"]

    def test_export_summary_json_exact_content_and_key_order(self, tmp_path: Path) -> None:
        """The summary's text is fixed: its keys, their order, entries by SWC name, paths relative to the dir."""
        model_root = _build_model(tmp_path)
        out_dir = tmp_path / "batch"

        export_all_components(model_root, str(out_dir))

        expected = [
            {
                "swc": "OtherSwc",
                "output_path": "OtherSwc.arxml",
                "status": "ok",
                "interfaces": ["IOther"],
                "data_types": ["Other_T", "Other_T_base"],
                "error": None,
            },
            {
                "swc": "TargetSwc",
                "output_path": "TargetSwc.arxml",
                "status": "ok",
                "interfaces": ["ICalib", "ISpeed"],
                "data_types": [
                    "Distance_T",
                    "Gear_T",
                    "Pose_T",
                    "Samples_T",
                    "Speed_T",
                    "Velocity_T",
                    "float32_base",
                    "uint8",
                    "uint8_base",
                ],
                "error": None,
            },
        ]
        raw = (out_dir / EXPORT_SUMMARY_FILENAME).read_text(encoding="utf-8")
        assert raw == json.dumps(expected, indent=2) + "\n"

    def test_untyped_error_does_not_abort_the_batch_and_summary_is_written(self, tmp_path: Path) -> None:
        """An untyped per-SWC failure (a YAML parser error) is still one failed report and the summary is written.

        Guards a catch narrowed to typed errors, which aborted the loop mid-way with no summary."""
        model_root = _build_model(tmp_path)
        (Path(model_root) / "data_types" / "implementation" / "malformed.yaml").write_text(
            "kind: [unclosed\n  ] : nope\n", encoding="utf-8"
        )
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        assert [r.swc for r in reports] == ["OtherSwc", "TargetSwc"]
        assert all(r.status == "failed" for r in reports)
        assert all(r.output_path is None for r in reports)
        summary = json.loads((out_dir / EXPORT_SUMMARY_FILENAME).read_text(encoding="utf-8"))
        assert [e["swc"] for e in summary] == ["OtherSwc", "TargetSwc"]
        assert all(e["status"] == "failed" for e in summary)

    def test_duplicate_swc_names_are_exported_once_per_name_as_failed(self, tmp_path: Path) -> None:
        """Two SWC files sharing one ``name`` are one failed entry, never two exports racing onto one path."""
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "swc" / "powertrain" / "TargetSwc.yaml",
            {
                "name": "TargetSwc",
                "ports": [{"name": "pOther", "direction": "provide", "interface": "IOther"}],
                "handle_termination_and_restart": "no_support",
            },
        )
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        assert [r.swc for r in reports] == ["OtherSwc", "TargetSwc"]
        by_name = {r.swc: r for r in reports}
        assert by_name["TargetSwc"].status == "failed"
        assert "declared by 2" in by_name["TargetSwc"].error
        assert by_name["TargetSwc"].output_path is None
        assert not (out_dir / "TargetSwc.arxml").exists()
        assert by_name["OtherSwc"].status == "ok"
        assert (out_dir / "OtherSwc.arxml").exists()

    def test_a_case_only_pair_is_exported_whole_to_two_files(self, tmp_path: Path) -> None:
        """A case-only pair (legal in two packages) is exported to ``<name>.arxml`` and ``<name>~1.arxml``.

        The YAML store's file-name rule, so a case-insensitive filesystem holds both files; the ``~1`` is the
        file's alone, never the declared component's name."""
        model_root = _build_model(tmp_path)
        root = Path(model_root)
        target = yaml.safe_load((root / "swc" / "body" / "TargetSwc.yaml").read_text(encoding="utf-8"))
        _dump(root / "swc" / "body" / "TargetSwc.yaml", {**target, "package": "Main"})
        # A different group dir, so the model YAML files never collide on a case-insensitive filesystem.
        _dump(
            root / "swc" / "powertrain" / "targetswc.yaml",
            {
                "name": "targetswc",
                "type": "application",
                "package": "Alt",
                "ports": [],
                "handle_termination_and_restart": "no_support",
            },
        )
        assert check_name_space_collisions(model_root).consistent
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        assert [(r.swc, r.status) for r in reports] == [("OtherSwc", "ok"), ("TargetSwc", "ok"), ("targetswc", "ok")]
        by_name = {r.swc: r for r in reports}
        assert Path(by_name["OtherSwc"].output_path).name == "OtherSwc.arxml"
        assert Path(by_name["TargetSwc"].output_path).name == "TargetSwc.arxml"
        assert Path(by_name["targetswc"].output_path).name == "targetswc~1.arxml"
        assert sorted(p.name for p in out_dir.iterdir()) == sorted(
            ["OtherSwc.arxml", "TargetSwc.arxml", "targetswc~1.arxml", EXPORT_SUMMARY_FILENAME]
        )
        upper = _short_names(out_dir / "TargetSwc.arxml")
        lower = _short_names(out_dir / "targetswc~1.arxml")
        assert "TargetSwc" in upper and "targetswc" not in upper
        assert "targetswc" in lower and "TargetSwc" not in lower
        assert not any("~" in name for name in upper | lower)
        summary = json.loads((out_dir / EXPORT_SUMMARY_FILENAME).read_text(encoding="utf-8"))
        assert [(e["swc"], e["status"], e["output_path"]) for e in summary] == [
            ("OtherSwc", "ok", "OtherSwc.arxml"),
            ("TargetSwc", "ok", "TargetSwc.arxml"),
            ("targetswc", "ok", "targetswc~1.arxml"),
        ]

    @pytest.mark.parametrize(
        ("names", "expected"),
        [
            (["A", "B"], ["A.arxml", "B.arxml"]),
            (["TargetSwc", "targetswc"], ["TargetSwc.arxml", "targetswc~1.arxml"]),
            (["FOO", "Foo", "foo"], ["FOO.arxml", "Foo~1.arxml", "foo~2.arxml"]),
            # a name that already spells a suffix holds that file like any other
            (["Foo", "Foo~1", "foo"], ["Foo.arxml", "Foo~1.arxml", "foo~2.arxml"]),
        ],
    )
    def test_batch_file_names_take_the_nearest_free_suffix(self, names: list[str], expected: list[str]) -> None:
        """``<name>.arxml`` unless an earlier file has the same name ignoring case, then the nearest free ``~N``."""
        planned = _batch_file_names(names)
        assert [planned[name] for name in names] == expected
        assert len({file_name.casefold() for file_name in planned.values()}) == len(names)

    def test_a_single_component_keeps_its_file_name(self, tmp_path: Path) -> None:
        """A component with no case-only twin exports to ``<name>.arxml``; nothing else beside the summary."""
        model_root = _build_model(tmp_path)
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        assert [Path(r.output_path).name for r in reports] == ["OtherSwc.arxml", "TargetSwc.arxml"]
        assert sorted(p.name for p in out_dir.iterdir()) == sorted(
            ["OtherSwc.arxml", "TargetSwc.arxml", EXPORT_SUMMARY_FILENAME]
        )

    @staticmethod
    def _with_a_case_only_twin(tmp_path: Path) -> str:
        """Return the model root of ``_build_model`` plus ``targetswc`` beside ``TargetSwc`` in another package."""
        model_root = _build_model(tmp_path)
        root = Path(model_root)
        target = yaml.safe_load((root / "swc" / "body" / "TargetSwc.yaml").read_text(encoding="utf-8"))
        _dump(root / "swc" / "body" / "TargetSwc.yaml", {**target, "package": "Main"})
        _dump(
            root / "swc" / "powertrain" / "targetswc.yaml",
            {
                "name": "targetswc",
                "type": "application",
                "package": "Alt",
                "ports": [],
                "handle_termination_and_restart": "no_support",
            },
        )
        assert check_name_space_collisions(model_root).consistent
        return model_root

    def test_the_single_export_default_is_the_batch_file_name(self, tmp_path: Path) -> None:
        """A single export's default file name is the batch's; an unknown name gets ``<name>.arxml``."""
        model_root = self._with_a_case_only_twin(tmp_path)
        batch = export_all_components(model_root, str(tmp_path / "batch"))
        from_batch = {r.swc: Path(r.output_path).name for r in batch}

        defaults = {name: default_component_file_name(model_root, name) for name in from_batch}

        assert (
            defaults
            == from_batch
            == {
                "OtherSwc": "OtherSwc.arxml",
                "TargetSwc": "TargetSwc.arxml",
                "targetswc": "targetswc~1.arxml",
            }
        )
        assert default_component_file_name(model_root, "GhostSwc") == "GhostSwc.arxml"

    def test_a_no_twin_default_stays_the_plain_name(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        assert default_component_file_name(model_root, "TargetSwc") == "TargetSwc.arxml"
        assert default_component_file_name(model_root, "OtherSwc") == "OtherSwc.arxml"

    def test_the_default_output_joins_the_components_directory(self, tmp_path: Path) -> None:
        """The one composition both front ends call: ``<dist_root>/components/`` and the batch's file name."""
        model_root = self._with_a_case_only_twin(tmp_path)
        output = default_component_output(model_root, str(tmp_path / "dist"), "targetswc")

        assert Path(output).as_posix() == (tmp_path / "dist" / "components" / "targetswc~1.arxml").as_posix()

    @staticmethod
    def _cli_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> None:
        manifest = _write_manifest(tmp_path)
        monkeypatch.setattr("sys.argv", ["swcraft", "generate", "arxml", "--project", str(manifest), *extra])
        main()

    def test_two_single_cli_exports_of_a_twin_pair_both_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exporting each member of a case-only pair by default leaves two files, each with its own component.

        Guards the second export silently replacing the first on a case-insensitive filesystem."""
        self._with_a_case_only_twin(tmp_path)
        components = tmp_path / "dist" / "components"

        self._cli_export(tmp_path, monkeypatch, "--component", "TargetSwc")
        self._cli_export(tmp_path, monkeypatch, "--component", "targetswc")

        assert sorted(p.name for p in components.iterdir()) == ["TargetSwc.arxml", "targetswc~1.arxml"]
        upper = _short_names(components / "TargetSwc.arxml")
        lower = _short_names(components / "targetswc~1.arxml")
        assert "TargetSwc" in upper and "targetswc" not in upper
        assert "targetswc" in lower and "TargetSwc" not in lower
        assert "targetswc~1.arxml" in capsys.readouterr().out

    def test_the_cli_default_agrees_with_the_batch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A single export into the batch's directory rewrites the batch's file for that component and adds none."""
        self._with_a_case_only_twin(tmp_path)
        components = tmp_path / "dist" / "components"
        self._cli_export(tmp_path, monkeypatch, "--all-components", "--output-dir", str(components))
        batch_files = sorted(p.name for p in components.iterdir())
        batch_twin = (components / "TargetSwc.arxml").read_bytes()

        self._cli_export(tmp_path, monkeypatch, "--component", "targetswc")

        assert sorted(p.name for p in components.iterdir()) == batch_files
        assert (components / "TargetSwc.arxml").read_bytes() == batch_twin
        assert "targetswc" in _short_names(components / "targetswc~1.arxml")

    def test_an_explicit_output_is_used_exactly_as_given(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--output`` is used as spelled and the default directory is not made."""
        self._with_a_case_only_twin(tmp_path)
        out = tmp_path / "elsewhere" / "targetswc.arxml"

        self._cli_export(tmp_path, monkeypatch, "--component", "targetswc", "--output", str(out))

        assert [p.name for p in out.parent.iterdir()] == ["targetswc.arxml"]
        assert "targetswc" in _short_names(out)
        assert not (tmp_path / "dist" / "components").exists()

    def test_schema_invalid_swc_file_is_reported_without_raising_out_of_the_batch(self, tmp_path: Path) -> None:
        """A schema-invalid SWC file fails per SWC and the summary is written; the batch never raises.

        Guards the name-listing read before the loop raising ``ValidationError`` with no reports. Not reached:
        ``swc/`` is read whole-layer, so the malformed file fails every other SWC's export too."""
        model_root = _build_model(tmp_path)
        _dump(
            Path(model_root) / "swc" / "body" / "InvalidSwc.yaml",
            {"name": "InvalidSwc", "ports": "not-a-list"},
        )
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        assert [r.swc for r in reports] == ["InvalidSwc", "OtherSwc", "TargetSwc"]
        by_name = {r.swc: r for r in reports}
        assert by_name["InvalidSwc"].status == "failed"
        assert by_name["InvalidSwc"].output_path is None
        assert by_name["InvalidSwc"].error is not None
        assert not (out_dir / "InvalidSwc.arxml").exists()
        assert by_name["OtherSwc"].status == "failed"
        assert by_name["TargetSwc"].status == "failed"
        assert not (out_dir / "OtherSwc.arxml").exists()
        assert not (out_dir / "TargetSwc.arxml").exists()
        assert (out_dir / EXPORT_SUMMARY_FILENAME).is_file()
        summary = json.loads((out_dir / EXPORT_SUMMARY_FILENAME).read_text(encoding="utf-8"))
        assert [e["swc"] for e in summary] == ["InvalidSwc", "OtherSwc", "TargetSwc"]
        assert all(e["status"] == "failed" for e in summary)
        entry = next(e for e in summary if e["swc"] == "InvalidSwc")
        assert entry["output_path"] is None


def _add_external_stub(model_root: str, name: str = "GhostSwc") -> None:
    """Add one `external: true` stub with no ports and no runnables.

    A stub with ports is refused the same way in `test_stub_ports_arxml.py`'s
    `TestTheComponentExportOfAStubWithPorts`.
    """
    _dump(
        Path(model_root) / "swc" / "body" / f"{name}.yaml",
        {
            "name": name,
            "external": True,
            "external_ref": "/Foreign/ComponentTypes/" + name,
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
        },
    )


class TestExternalStubsAreNotExportable:
    """A stub is a reference, not a component type, so it is not an export.

    The writer emits no ``APPLICATION-SW-COMPONENT-TYPE`` for a stub; guards an export with an empty
    ``ComponentTypes`` package reported ok. Naming a stub is refused; ``--all-components`` skips it silently,
    as the whole-model exporter does, since a ``failed`` row would fail every correctly imported project.
    """

    def test_exporting_a_stub_by_name_is_refused(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _add_external_stub(model_root)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ComponentExchangeError) as excinfo:
            export_component(model_root, "GhostSwc", str(out_dir / "GhostSwc.arxml"))

        message = str(excinfo.value)
        assert "GhostSwc" in message
        assert "external" in message
        assert list(out_dir.iterdir()) == []

    def test_the_refusal_is_not_one_of_the_other_typed_errors(self, tmp_path: Path) -> None:
        """The stub resolves: the refusal is neither not-found nor ambiguous, so it never reports one absent."""
        model_root = _build_model(tmp_path)
        _add_external_stub(model_root)

        with pytest.raises(ComponentExchangeError) as excinfo:
            export_component(model_root, "GhostSwc", str(tmp_path / "GhostSwc.arxml"))

        assert not isinstance(excinfo.value, ComponentNotFoundError)
        assert not isinstance(excinfo.value, AmbiguousComponentError)

    def test_the_batch_skips_a_stub_entirely(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _add_external_stub(model_root)
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        assert [r.swc for r in reports] == ["OtherSwc", "TargetSwc"]
        assert all(r.ok for r in reports)
        assert not (out_dir / "GhostSwc.arxml").exists()
        summary = json.loads((out_dir / EXPORT_SUMMARY_FILENAME).read_text(encoding="utf-8"))
        assert [entry["swc"] for entry in summary] == ["OtherSwc", "TargetSwc"]

    def test_a_model_of_nothing_but_stubs_is_an_empty_batch_and_not_a_failure(self, tmp_path: Path) -> None:
        """A stubs-only batch is an empty report list, since the CLI exits 1 on any report that is not ``ok``."""
        model_root = tmp_path / "stubs-only"
        (model_root / "swc" / "body").mkdir(parents=True)
        _add_external_stub(str(model_root), "GhostOne")
        _add_external_stub(str(model_root), "GhostTwo")
        out_dir = tmp_path / "batch"

        reports = export_all_components(str(model_root), str(out_dir))

        assert reports == []
        summary = json.loads((out_dir / EXPORT_SUMMARY_FILENAME).read_text(encoding="utf-8"))
        assert summary == []

    def test_a_raised_exchange_error_would_still_be_a_per_swc_failed_row(self, tmp_path: Path) -> None:
        """The batch renders a raised exchange error as a failed row, its fallback if the stub filter moved."""
        import swcraft_tools.component_exchange.export as export_module

        model_root = _build_model(tmp_path)
        out_dir = tmp_path / "batch"

        def _refusing_export(*args: object, **kwargs: object) -> None:
            raise ComponentExchangeError("refused for the sake of the argument")

        monkeypatch_target = export_module.export_component
        assert monkeypatch_target is not None
        export_module.export_component = _refusing_export  # type: ignore[assignment]
        try:
            reports = export_all_components(model_root, str(out_dir))
        finally:
            export_module.export_component = monkeypatch_target  # type: ignore[assignment]

        assert [r.status for r in reports] == ["failed", "failed"]
        assert all("refused for the sake of the argument" in (r.error or "") for r in reports)


# External interface and type stubs: a closure that reaches one.
_IFACE_STUB = ("IForeignCS", "/Foreign/PortInterfaces/IForeignCS", "CLIENT-SERVER-INTERFACE")
_TYPE_STUB = ("ForeignT", "/Foreign/ImplementationDataTypes/ForeignT", "IMPLEMENTATION-DATA-TYPE")


def _with_interface_stub_port(model_root: str) -> None:
    """Declare the interface stub and give ``TargetSwc`` a require port on it."""
    root = Path(model_root)
    interface_stub(root, *_IFACE_STUB)
    change(
        root,
        "swc/body/TargetSwc.yaml",
        lambda d: d["ports"].append({"name": "rForeign", "direction": "require", "interface": _IFACE_STUB[0]}),
    )


def _with_type_stub_argument(model_root: str) -> None:
    """Declare the type stub and type one more ``ICalib`` argument by it."""
    root = Path(model_root)
    type_stub(root, *_TYPE_STUB)
    change(
        root,
        "interfaces/ICalib.yaml",
        lambda d: d["operations"][0]["arguments"].append(
            {"name": "foreign", "direction": "in", "implementation_type": _TYPE_STUB[0]}
        ),
    )


class TestAClosureThatReachesAnExternalStubIsNotExportable:
    """A component whose closure reaches a stub is refused: one sentence, nothing written.

    The writer emits a stub's ``external_ref`` and no element for it, so the file would reference a definition
    it does not contain. Guards the closure walk raising ``AttributeError`` on an interface stub.
    """

    def test_a_port_on_an_interface_stub_is_refused_naming_the_stub(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_interface_stub_port(model_root)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ComponentExchangeError) as excinfo:
            export_component(model_root, "TargetSwc", str(out_dir / "TargetSwc.arxml"))

        message = str(excinfo.value)
        assert "external interface 'IForeignCS'" in message
        assert _IFACE_STUB[1] in message
        assert message.endswith("Nothing was written.")
        assert list(out_dir.iterdir()) == []

    def test_an_argument_on_a_type_stub_is_refused_naming_the_stub(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_type_stub_argument(model_root)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ComponentExchangeError) as excinfo:
            export_component(model_root, "TargetSwc", str(out_dir / "TargetSwc.arxml"))

        message = str(excinfo.value)
        assert "external type 'ForeignT'" in message
        assert _TYPE_STUB[1] in message
        assert "IForeignCS" not in message
        assert list(out_dir.iterdir()) == []

    def test_a_type_stub_reached_through_a_reference_type_is_refused(self, tmp_path: Path) -> None:
        """The walk follows a reference type's ``base`` to the stub, so the chain is caught."""
        model_root = _build_model(tmp_path)
        root = Path(model_root)
        type_stub(root, *_TYPE_STUB)
        write_model_element(root, {"name": "RefForeign_T", "kind": "reference_type", "base": "ForeignT"})
        change(
            root,
            "interfaces/ISpeed.yaml",
            lambda d: d["data_elements"].append({"name": "via", "implementation_type": "RefForeign_T"}),
        )

        with pytest.raises(ComponentExchangeError, match="external type 'ForeignT'"):
            export_component(model_root, "TargetSwc", str(tmp_path / "TargetSwc.arxml"))

        assert not (tmp_path / "TargetSwc.arxml").exists()

    def test_both_stubs_are_named_in_one_sentence(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_interface_stub_port(model_root)
        _with_type_stub_argument(model_root)

        with pytest.raises(ComponentExchangeError) as excinfo:
            export_component(model_root, "TargetSwc", str(tmp_path / "TargetSwc.arxml"))

        message = str(excinfo.value)
        assert message.index("external interface 'IForeignCS'") < message.index("external type 'ForeignT'")
        assert message.count("Nothing was written.") == 1
        assert "\n" not in message

    def test_the_refusal_is_not_one_of_the_other_typed_errors(self, tmp_path: Path) -> None:
        """A stub resolves: the closure reaches a declared element, not a missing one."""
        model_root = _build_model(tmp_path)
        _with_interface_stub_port(model_root)

        with pytest.raises(ComponentExchangeError) as excinfo:
            export_component(model_root, "TargetSwc", str(tmp_path / "TargetSwc.arxml"))

        assert not isinstance(excinfo.value, (ComponentNotFoundError, AmbiguousComponentError, MissingReferenceError))

    def test_the_closure_holds_the_stubs_rather_than_failing_on_them(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_interface_stub_port(model_root)
        _with_type_stub_argument(model_root)

        closure = compute_closure(model_root, "TargetSwc")

        assert [type(i) for i in closure.interfaces if i.name == "IForeignCS"] == [ExternalInterface]
        assert [type(d) for d in closure.data_types if d.name == "ForeignT"] == [ExternalDataType]

    def test_a_stub_the_component_does_not_reach_blocks_nothing(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        interface_stub(Path(model_root), *_IFACE_STUB)
        type_stub(Path(model_root), *_TYPE_STUB)
        output = tmp_path / "TargetSwc.arxml"

        report = export_component(model_root, "TargetSwc", str(output))

        assert report.ok
        assert "IForeignCS" not in report.interfaces
        assert "ForeignT" not in report.data_types
        assert output.is_file()

    def test_a_stub_that_lists_its_members_is_refused_all_the_same(self, tmp_path: Path) -> None:
        """A stub listing its members by name is refused too: it still holds none of their content."""
        model_root = _build_model(tmp_path)
        _with_interface_stub_port(model_root)
        change(Path(model_root), f"interfaces/{_IFACE_STUB[0]}.yaml", lambda d: d.update(operations=["ReadForeign"]))
        change(
            Path(model_root),
            "swc/body/TargetSwc.yaml",
            lambda d: d["runnables"][0].update(calls=[{"port": "rForeign", "operation": "ReadForeign"}]),
        )
        (stub,) = [i for i in compute_closure(model_root, "TargetSwc").interfaces if i.name == _IFACE_STUB[0]]
        assert stub == ExternalInterface(
            name=_IFACE_STUB[0],
            kind="external_interface",
            external_ref=_IFACE_STUB[1],
            external_dest=_IFACE_STUB[2],
            operations=["ReadForeign"],
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ComponentExchangeError) as excinfo:
            export_component(model_root, "TargetSwc", str(out_dir / "TargetSwc.arxml"))

        assert "external interface 'IForeignCS'" in str(excinfo.value)
        assert list(out_dir.iterdir()) == []

    def test_the_batch_reports_the_component_failed_and_exports_the_rest(self, tmp_path: Path) -> None:
        """A real component that cannot be exported is a ``failed`` row, unlike a component stub's skip."""
        model_root = _build_model(tmp_path)
        _with_interface_stub_port(model_root)
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        by_name = {r.swc: r for r in reports}
        assert by_name["OtherSwc"].ok
        assert by_name["TargetSwc"].status == "failed"
        assert "external interface 'IForeignCS'" in (by_name["TargetSwc"].error or "")
        assert not (out_dir / "TargetSwc.arxml").exists()


def _with_stated_names(model_root: str, **runnable: Any) -> None:
    """Give ``TargetSwc`` a stated behaviour name and set *runnable*'s keys on ``TargetRunnable``."""

    def state(document: dict[str, Any]) -> None:
        document["behavior_name"] = "TargetBehaviour"
        document["runnables"][0].update(runnable)

    change(Path(model_root), "swc/body/TargetSwc.yaml", state)


class TestTheExportWritesAndJudgesTheStatedNames:
    """A stated behaviour, event or access point name is what the export writes, judged as written.

    ``rule:name.duplicate-event`` and ``rule:swc.access.name-collision`` are the closure check's, so a clash is
    a :class:`ClosureValidationError` in ``check-model``'s words, not the writer's bare ``ValueError``;
    ``export_all_components`` lists such a component as failed and exports the rest. A case-only pair is
    ``rule:name.name-space-collision``'s.

    **What it does not reach:** a stated name that is not a legal identifier is the writer's refusal
    (``check_identifier_conformance`` is not composed).
    """

    def test_every_stated_name_is_written_and_no_derived_form_it_replaces(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_stated_names(
            model_root,
            events=[{"type": "timing", "period": 0.01, "name": "TargetTick"}],
            writes=[{"port": "pSpeed", "element": "speed", "name": "WriteSpeed"}],
            calls=[{"port": "rCalib", "operation": "SetOffset", "name": "CallSetOffset"}],
        )
        output = tmp_path / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(output))

        written = _short_names(output)
        assert {"TargetBehaviour", "TargetTick", "WriteSpeed", "CallSetOffset"} <= written
        assert (
            not {
                "TargetSwc_InternalBehavior",
                "TMT_TargetRunnable",
                "W_pSpeed_speed",
                "Call_rCalib_SetOffset",
            }
            & written
        )
        root = ET.parse(output).getroot()
        behaviour_refs = [el.text for el in root.iter() if el.tag.split("}")[-1] == "BEHAVIOR-REF"]
        assert [ref.rsplit("/", 1)[-1] for ref in behaviour_refs] == ["TargetBehaviour"]

    def test_two_events_written_with_one_name_are_a_closure_finding(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_stated_names(
            model_root,
            events=[{"type": "timing", "period": 0.01, "name": "TargetTick"}, {"type": "init", "name": "TargetTick"}],
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ClosureValidationError) as excinfo:
            export_component(model_root, "TargetSwc", str(out_dir / "TargetSwc.arxml"))

        (issue,) = excinfo.value.issues
        assert "'TargetTick'" in issue
        assert list(out_dir.iterdir()) == []

    def test_two_access_points_written_with_one_name_are_a_closure_finding(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_stated_names(
            model_root,
            writes=[
                {"port": "pSpeed", "element": "speed", "name": "WriteBoth"},
                {"port": "pSpeed", "element": "pose", "name": "WriteBoth"},
            ],
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ClosureValidationError) as excinfo:
            export_component(model_root, "TargetSwc", str(out_dir / "TargetSwc.arxml"))

        (issue,) = excinfo.value.issues
        assert "'WriteBoth'" in issue
        assert list(out_dir.iterdir()) == []

    def test_two_event_names_differing_in_case_only_are_a_closure_finding(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_stated_names(
            model_root,
            events=[{"type": "timing", "period": 0.01, "name": "TargetTick"}, {"type": "init", "name": "targettick"}],
        )

        with pytest.raises(ClosureValidationError) as excinfo:
            export_component(model_root, "TargetSwc", str(tmp_path / "TargetSwc.arxml"))

        (issue,) = excinfo.value.issues
        assert "'TargetTick'" in issue and "'targettick'" in issue
        assert not (tmp_path / "TargetSwc.arxml").exists()

    def test_the_batch_lists_the_component_failed_and_exports_the_rest(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_stated_names(
            model_root,
            events=[{"type": "timing", "period": 0.01, "name": "TargetTick"}, {"type": "init", "name": "TargetTick"}],
        )
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        by_name = {r.swc: r for r in reports}
        assert by_name["OtherSwc"].ok
        assert by_name["TargetSwc"].status == "failed"
        assert (by_name["TargetSwc"].error or "").startswith("component closure validation failed: ")
        assert not (out_dir / "TargetSwc.arxml").exists()


# The closure runs check_operation_events.
_UNKNOWN_OPERATION = "swc.operation-event.unknown-operation"
_UNKNOWN_EVENT_PORT = "swc.operation-event.unknown-port"


def _with_operation_event(model_root: str, port: str, operation: str) -> None:
    """Make ``TargetSwc`` provide ``ICalib`` on ``pCalib`` and start ``TargetRunnable`` by *operation* on *port*.

    The timing event is replaced, so one event keeps the finding's count obvious.
    """

    def serve(document: dict[str, Any]) -> None:
        document["ports"].append({"name": "pCalib", "direction": "provide", "interface": "ICalib"})
        document["runnables"][0]["events"] = [{"type": "operation", "port": port, "operation": operation}]

    change(Path(model_root), "swc/body/TargetSwc.yaml", serve)


def _validator_messages(model_root: str, rule: str) -> list[str]:
    """``check-model``'s own messages for *rule* on the whole project, non-empty."""
    messages = [record.message for record in check_cross_layer_consistency(model_root).records if record.rule == rule]
    assert messages, rule
    return messages


class TestTheClosureJudgesTheOperationEvents:
    """An unresolved operation event is a :class:`ClosureValidationError` in ``check-model``'s words.

    Not the writer's single ``ValueError``; :attr:`ClosureValidationError.rules` names each issue's rule at its
    position, for every composed check."""

    @pytest.mark.parametrize(
        ("port", "operation", "rule"),
        [
            ("pCalib", "Missing", _UNKNOWN_OPERATION),
            ("pNowhere", "SetOffset", _UNKNOWN_EVENT_PORT),
            ("rCalib", "SetOffset", "swc.operation-event.direction-mismatch"),
            ("pSpeed", "SetOffset", "swc.operation-event.interface-kind-mismatch"),
        ],
        ids=["unknown-operation", "unknown-port", "not-a-provide-port", "not-client-server"],
    )
    def test_an_unresolved_operation_event_is_the_closure_check_s_refusal(
        self, tmp_path: Path, port: str, operation: str, rule: str
    ) -> None:
        model_root = _build_model(tmp_path)
        _with_operation_event(model_root, port, operation)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ClosureValidationError) as excinfo:
            export_component(model_root, "TargetSwc", str(out_dir / "TargetSwc.arxml"))

        assert excinfo.value.issues == _validator_messages(model_root, rule)
        assert excinfo.value.rules == (rule,)
        assert list(out_dir.iterdir()) == []

    def test_an_operation_bound_twice_is_the_closure_check_s_refusal(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_operation_event(model_root, "pCalib", "SetOffset")
        rule = "swc.operation-event.duplicate-binding"

        def bind_again(document: dict[str, Any]) -> None:
            document["runnables"].append(
                {
                    "name": "SecondRunnable",
                    "events": [{"type": "operation", "port": "pCalib", "operation": "SetOffset"}],
                }
            )

        change(Path(model_root), "swc/body/TargetSwc.yaml", bind_again)

        with pytest.raises(ClosureValidationError) as excinfo:
            export_component(model_root, "TargetSwc", str(tmp_path / "TargetSwc.arxml"))

        assert excinfo.value.issues == _validator_messages(model_root, rule)
        assert excinfo.value.rules == (rule,) * len(excinfo.value.issues)

    def test_the_finding_stands_beside_another_check_s_at_the_same_positions(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_operation_event(model_root, "pCalib", "Missing")
        change(Path(model_root), "swc/body/TargetSwc.yaml", lambda d: d.pop("handle_termination_and_restart"))
        output = tmp_path / "TargetSwc.arxml"

        with pytest.raises(ClosureValidationError) as excinfo:
            export_component(model_root, "TargetSwc", str(output))

        termination = "swc.undeclared-termination-and-restart"
        assert excinfo.value.rules == (termination, _UNKNOWN_OPERATION)
        assert excinfo.value.issues == [
            *_validator_messages(model_root, termination),
            *_validator_messages(model_root, _UNKNOWN_OPERATION),
        ]
        assert not output.exists()

    def test_a_closure_refused_by_another_check_names_a_rule_per_issue(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_stated_names(
            model_root,
            events=[{"type": "timing", "period": 0.01, "name": "TargetTick"}, {"type": "init", "name": "TargetTick"}],
        )
        change(Path(model_root), "swc/body/TargetSwc.yaml", lambda d: d.pop("handle_termination_and_restart"))

        with pytest.raises(ClosureValidationError) as excinfo:
            export_component(model_root, "TargetSwc", str(tmp_path / "TargetSwc.arxml"))

        assert len(excinfo.value.rules) == len(excinfo.value.issues) == 2
        assert excinfo.value.rules == ("swc.undeclared-termination-and-restart", "name.duplicate-event")

    def test_the_batch_lists_the_component_failed_and_exports_the_rest(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _with_operation_event(model_root, "pCalib", "Missing")
        out_dir = tmp_path / "batch"

        by_name = {report.swc: report for report in export_all_components(model_root, str(out_dir))}

        assert by_name["OtherSwc"].ok
        assert by_name["TargetSwc"].status == "failed"
        (message,) = _validator_messages(model_root, _UNKNOWN_OPERATION)
        assert (by_name["TargetSwc"].error or "") == f"component closure validation failed: {message}"
        assert not (out_dir / "TargetSwc.arxml").exists()

    def test_a_resolving_operation_event_exports_the_bytes_the_unchecked_export_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check refuses and never changes a byte: the export equals the one with the check bypassed."""
        model_root = _build_model(tmp_path)
        _with_operation_event(model_root, "pCalib", "SetOffset")
        checked = tmp_path / "checked" / "TargetSwc.arxml"
        unchecked = tmp_path / "unchecked" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(checked))
        monkeypatch.setattr(
            "swcraft_tools.component_exchange.export.validate_component_closure", lambda _root, **_version: None
        )
        export_component(model_root, "TargetSwc", str(unchecked))

        assert checked.read_bytes() == unchecked.read_bytes()
        assert "<OPERATION-INVOKED-EVENT>" in checked.read_text(encoding="utf-8")


class TestExportReportRenderer:
    def test_success_render_has_status_output_and_counts(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        output = tmp_path / "TargetSwc.arxml"

        report = export_component(model_root, "TargetSwc", str(output))

        rendered = report.render()
        lines = rendered.splitlines()
        assert lines[0] == "Component export report: TargetSwc"
        assert "STATUS ok" in lines
        assert f"OUTPUT {output}" in lines
        assert "EXPORTED 2 interface(s)" in lines
        # Nine: the closure's data types, the two base types its value types rest on included.
        assert "EXPORTED 9 data type(s)" in lines
        assert "  - ISpeed" in lines
        assert "  - Speed_T" in lines
        assert "ERROR" not in rendered

    def test_failed_render_has_error_and_no_output_line(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        _add_broken_swc(model_root)
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        failed = next(r for r in reports if r.swc == "BrokenSwc")
        rendered = failed.render()
        lines = rendered.splitlines()
        assert "STATUS failed" in lines
        assert not any(line.startswith("OUTPUT") for line in lines)
        assert "EXPORTED 0 interface(s)" in lines
        assert "EXPORTED 0 data type(s)" in lines
        assert any(line.startswith("ERROR ") and "IUndeclared" in line for line in lines)


class TestEdgeCases:
    def test_swc_with_no_ports_exports_self_contained_arxml(self, tmp_path: Path) -> None:
        """A port-less SWC's closure is the SWC alone, and the export still succeeds."""
        model_root = tmp_path / "model"
        _dump(
            model_root / "swc" / "body" / "LonelySwc.yaml",
            {"name": "LonelySwc", "type": "application", "ports": [], "handle_termination_and_restart": "no_support"},
        )
        out = tmp_path / "out" / "LonelySwc.arxml"

        report = export_component(str(model_root), "LonelySwc", str(out))

        assert report.status == "ok"
        assert report.interfaces == []
        assert report.data_types == []
        names = _short_names(out)
        assert "LonelySwc" in names

    def test_interface_with_zero_argument_operations_exports(self, tmp_path: Path) -> None:
        """A CS interface with argument-less operations exports with no data types.

        The SR analogue is not constructible: SRInterface rejects empty data_elements."""
        model_root = tmp_path / "model"
        _dump(
            model_root / "interfaces" / "ITrigger.yaml",
            {
                "name": "ITrigger",
                "kind": "client_server",
                "operations": [{"name": "Fire", "arguments": []}],
            },
        )
        _dump(
            model_root / "swc" / "body" / "TriggerSwc.yaml",
            {
                "name": "TriggerSwc",
                "type": "application",
                "ports": [{"name": "rTrigger", "direction": "require", "interface": "ITrigger"}],
                "handle_termination_and_restart": "no_support",
            },
        )
        out = tmp_path / "out" / "TriggerSwc.arxml"

        report = export_component(str(model_root), "TriggerSwc", str(out))

        assert report.status == "ok"
        assert report.interfaces == ["ITrigger"]
        assert report.data_types == []
        names = _short_names(out)
        assert {"TriggerSwc", "ITrigger", "Fire"} <= names

    def test_circular_reference_type_references_fail_closure_validation(self, tmp_path: Path) -> None:
        """Two reference types forming a cycle fail closure validation, with no output.

        Pydantic rejects only a self-reference; a reference type is the chain that can close a cycle, since a
        value type's ``base`` resolves in ``base_types`` alone."""
        model_root = tmp_path / "model"
        write_model_element(model_root, {"name": "A_T", "kind": "reference_type", "base": "B_T"})
        write_model_element(model_root, {"name": "B_T", "kind": "reference_type", "base": "A_T"})
        _dump(
            model_root / "interfaces" / "ILoop.yaml",
            {
                "name": "ILoop",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "A_T"}],
            },
        )
        _dump(
            model_root / "swc" / "body" / "LoopSwc.yaml",
            {
                "name": "LoopSwc",
                "type": "application",
                "ports": [{"name": "pLoop", "direction": "provide", "interface": "ILoop"}],
            },
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        out = out_dir / "LoopSwc.arxml"

        with pytest.raises(ClosureValidationError) as excinfo:
            export_component(str(model_root), "LoopSwc", str(out))

        assert any("Cycle detected" in issue for issue in excinfo.value.issues)
        assert list(out_dir.iterdir()) == []

    def test_two_value_types_naming_each_other_as_base_are_undeclared_bases_not_a_cycle(self, tmp_path: Path) -> None:
        """Two value types naming each other as ``base`` are refused as undeclared bases, not as a cycle."""
        model_root = tmp_path / "model"
        write_model_element(model_root, {"name": "A_T", "kind": "value_type", "base": "B_T"})
        write_model_element(model_root, {"name": "B_T", "kind": "value_type", "base": "A_T"})
        _dump(
            model_root / "interfaces" / "ILoop.yaml",
            {
                "name": "ILoop",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "A_T"}],
            },
        )
        _dump(
            model_root / "swc" / "body" / "LoopSwc.yaml",
            {
                "name": "LoopSwc",
                "type": "application",
                "ports": [{"name": "pLoop", "direction": "provide", "interface": "ILoop"}],
            },
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises((ClosureValidationError, MissingReferenceError)) as excinfo:
            export_component(str(model_root), "LoopSwc", str(out_dir / "LoopSwc.arxml"))

        assert "Cycle detected" not in str(excinfo.value)
        assert "B_T" in str(excinfo.value)
        assert list(out_dir.iterdir()) == []


# A component export carries the closure's init values: the export path has no code of its own for them, so
# only these tests would notice the closure materialization dropping `init` on the way into the temp tree.


def _build_model_with_init(tmp_path: Path) -> str:
    """Build :func:`_build_model`'s tree reaching all three ``INIT-VALUE`` owners in one export.

    Both ``ISpeed`` data elements carry an ``init`` (a scalar, and a record of a float and an enum literal), and
    ``TargetSwc`` gains a require port on ``ISpeed``, without which no ``NONQUEUED-RECEIVER-COM-SPEC`` exists.
    """
    model_root = _build_model(tmp_path)
    root = Path(model_root)
    _dump(
        root / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "speed", "implementation_type": "Speed_T", "init": 1.5},
                {"name": "pose", "application_type": "Pose_T", "init": {"x": 2.5, "gear": "DRIVE"}},
            ],
        },
    )
    _dump(
        root / "swc" / "body" / "TargetSwc.yaml",
        {
            "name": "TargetSwc",
            "type": "application",
            "ports": [
                {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
                {"name": "rSpeed", "direction": "require", "interface": "ISpeed"},
                {"name": "rCalib", "direction": "require", "interface": "ICalib"},
            ],
            "runnables": [{"name": "TargetRunnable", "events": [{"type": "timing", "period": 0.01}]}],
            "handle_termination_and_restart": "no_support",
        },
    )
    return model_root


def _local(element: ET.Element) -> str:
    return element.tag.split("}")[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in element if _local(c) == name), None)


def _constants_in_port_interfaces(arxml_path: Path) -> dict[str, ET.Element]:
    """Every ``CONSTANT-SPECIFICATION`` directly in the ``PortInterfaces`` package, by ``SHORT-NAME``."""
    root = ET.parse(arxml_path).getroot()
    package = next(
        p
        for p in root.iter()
        if _local(p) == "AR-PACKAGE"
        and (short := _child(p, "SHORT-NAME")) is not None
        and short.text == "PortInterfaces"
    )
    elements = _child(package, "ELEMENTS")
    assert elements is not None
    return {
        _child(child, "SHORT-NAME").text: child  # type: ignore[union-attr]
        for child in elements
        if _local(child) == "CONSTANT-SPECIFICATION"
    }


def _init_value_references(arxml_path: Path) -> list[tuple[str, str]]:
    """Every ``INIT-VALUE`` in the document as ``(owner tag, CONSTANT-REF text)``."""
    root = ET.parse(arxml_path).getroot()
    found: list[tuple[str, str]] = []
    for owner in root.iter():
        for child in owner:
            if _local(child) != "INIT-VALUE":
                continue
            reference = _child(child, "CONSTANT-REFERENCE")
            assert reference is not None
            ref = _child(reference, "CONSTANT-REF")
            assert ref is not None and ref.text
            found.append((_local(owner), ref.text))
    return found


class TestTheExportCarriesTheClosuresInitValues:
    """The component export carries the closure's init values; the writer's suites would not see them dropped.

    **What this class does not reach:** the reuse half of the exchange (``test_component_import.py``'s) and the
    value grammar itself (``test_arxml_init_writer.py``'s).
    """

    def test_the_constants_are_siblings_of_the_interfaces(self, tmp_path: Path) -> None:
        model_root = _build_model_with_init(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert sorted(_constants_in_port_interfaces(out)) == ["ISpeed_pose_Init", "ISpeed_speed_Init"]

    def test_the_scalar_constant_carries_the_declared_value(self, tmp_path: Path) -> None:
        model_root = _build_model_with_init(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        specification = _constants_in_port_interfaces(out)["ISpeed_speed_Init"]
        value_spec = _child(specification, "VALUE-SPEC")
        assert value_spec is not None
        numerical = _child(value_spec, "NUMERICAL-VALUE-SPECIFICATION")
        assert numerical is not None
        assert _child(numerical, "VALUE").text == "1.5"  # type: ignore[union-attr]

    def test_the_record_constant_carries_a_labelled_member_per_declared_field(self, tmp_path: Path) -> None:
        """The record constant carries one labelled member per declared field.

        The float member is a numerical value; the enum member is the literal's text, one ``VT`` of an application
        value.
        """
        model_root = _build_model_with_init(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        specification = _constants_in_port_interfaces(out)["ISpeed_pose_Init"]
        record = _child(_child(specification, "VALUE-SPEC"), "RECORD-VALUE-SPECIFICATION")  # type: ignore[arg-type]
        assert record is not None
        fields = _child(record, "FIELDS")
        assert fields is not None
        assert [
            (
                _child(member, "SHORT-LABEL").text,  # type: ignore[union-attr]
                member.tag.rsplit("}", 1)[-1],
                member.findtext("{*}VALUE") or member.findtext("{*}SW-VALUE-CONT/{*}SW-VALUES-PHYS/{*}VT"),
            )
            for member in fields
        ] == [("x", "NUMERICAL-VALUE-SPECIFICATION", "2.5"), ("gear", "APPLICATION-VALUE-SPECIFICATION", "DRIVE")]

    def test_each_element_is_referenced_from_its_prototype_and_from_both_com_specs(self, tmp_path: Path) -> None:
        """The prototype and both com-specs reference the element's own constant, never another element's."""
        model_root = _build_model_with_init(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert sorted(_init_value_references(out)) == [
            ("NONQUEUED-RECEIVER-COM-SPEC", "/PortInterfaces/ISpeed_pose_Init"),
            ("NONQUEUED-RECEIVER-COM-SPEC", "/PortInterfaces/ISpeed_speed_Init"),
            ("NONQUEUED-SENDER-COM-SPEC", "/PortInterfaces/ISpeed_pose_Init"),
            ("NONQUEUED-SENDER-COM-SPEC", "/PortInterfaces/ISpeed_speed_Init"),
            ("VARIABLE-DATA-PROTOTYPE", "/PortInterfaces/ISpeed_pose_Init"),
            ("VARIABLE-DATA-PROTOTYPE", "/PortInterfaces/ISpeed_speed_Init"),
        ]

    def test_an_element_without_a_value_is_exported_as_before(self, tmp_path: Path) -> None:
        """An export without values carries no constant and no ``INIT-VALUE``."""
        model_root = _build_model(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert _constants_in_port_interfaces(out) == {}
        assert _init_value_references(out) == []

    @pytest.mark.parametrize("version", list(ArxmlVersion), ids=lambda v: v.value)
    def test_the_export_is_xsd_valid_at_both_versions(self, tmp_path: Path, version: ArxmlVersion) -> None:
        """The licensed schema, the authoritative oracle, accepts the component export with its constants."""
        schema = require_licensed_schema(version)
        model_root = _build_model_with_init(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out), version=version)

        outcome = check_arxml(str(out), schema_path=schema)
        assert outcome.mode == MODE_XSD
        assert outcome.schema_error is False, outcome.messages
        assert outcome.valid, "\n".join((f"{out} does not conform to {schema}:", *outcome.messages))

    def test_the_export_summary_is_byte_identical_with_and_without_the_values(self, tmp_path: Path) -> None:
        """The summary has no lossiness channel: with or without values it is byte-identical."""
        plain_dir = tmp_path / "plain"
        with_values_dir = tmp_path / "with_values"

        # the second builder rewrites the first's tree in place, so the plain export goes first
        export_all_components(_build_model(tmp_path), str(plain_dir))
        export_all_components(_build_model_with_init(tmp_path), str(with_values_dir))

        plain = (plain_dir / EXPORT_SUMMARY_FILENAME).read_bytes()
        with_values = (with_values_dir / EXPORT_SUMMARY_FILENAME).read_bytes()
        assert with_values == plain
        # Non-vacuity: the component file does differ, so the equal summaries are not trivially equal.
        assert (with_values_dir / "TargetSwc.arxml").read_bytes() != (plain_dir / "TargetSwc.arxml").read_bytes()


def _build_model_with_access(tmp_path: Path) -> str:
    """Build :func:`_build_model`'s tree with one access entry per container on ``TargetRunnable``.

    ``TargetSwc`` gains a require port on ``ISpeed``, without which no read has a port to name.
    """
    model_root = _build_model(tmp_path)
    root = Path(model_root)
    _dump(
        root / "swc" / "body" / "TargetSwc.yaml",
        {
            "name": "TargetSwc",
            "type": "application",
            "ports": [
                {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
                {"name": "rSpeed", "direction": "require", "interface": "ISpeed"},
                {"name": "rCalib", "direction": "require", "interface": "ICalib"},
            ],
            "runnables": [
                {
                    "name": "TargetRunnable",
                    "events": [{"type": "timing", "period": 0.01}],
                    "reads": [
                        {"port": "rSpeed", "element": "speed"},
                        {"port": "rSpeed", "element": "pose", "by": "value"},
                    ],
                    "writes": [{"port": "pSpeed", "element": "speed"}],
                    "calls": [{"port": "rCalib", "operation": "SetOffset"}],
                }
            ],
            "handle_termination_and_restart": "no_support",
        },
    )
    return model_root


def _behavior(arxml_path: Path) -> ET.Element:
    """``TargetSwc``'s one ``SWC-INTERNAL-BEHAVIOR``."""
    root = ET.parse(arxml_path).getroot()
    return next(el for el in root.iter() if _local(el) == "SWC-INTERNAL-BEHAVIOR")


def _access_marker_data(arxml_path: Path) -> list[tuple[str, str, str]]:
    """Every ``ADMIN-DATA/SDGS/SDG/SD`` on a behaviour as ``(SDG GID, SD GID, text)``, read from the file."""
    root = ET.parse(arxml_path).getroot()
    found: list[tuple[str, str, str]] = []
    for owner in root.iter():
        if _local(owner) != "SWC-INTERNAL-BEHAVIOR":
            continue
        admin = _child(owner, "ADMIN-DATA")
        if admin is None:
            continue
        groups = _child(admin, "SDGS")
        assert groups is not None
        for group in groups:
            for datum in group:
                found.append((group.get("GID") or "", datum.get("GID") or "", datum.text or ""))
    return found


def _access_containers(arxml_path: Path) -> dict[str, list[str]]:
    """The access containers under ``TargetRunnable``, each with its points' ``SHORT-NAME``s in order."""
    root = ET.parse(arxml_path).getroot()
    runnable = next(
        el
        for el in root.iter()
        if _local(el) == "RUNNABLE-ENTITY"
        and (short := _child(el, "SHORT-NAME")) is not None
        and short.text == "TargetRunnable"
    )
    return {
        _local(container): [_child(point, "SHORT-NAME").text for point in container]  # type: ignore[union-attr]
        for container in runnable
        if _local(container).endswith(("-POINTS", "-POINT-BY-ARGUMENTS", "-POINT-BY-VALUES"))
    }


class TestTheExportCarriesTheClosuresAccessPoints:
    """The component export carries the closure's access points and the access-points marker.

    **What this class does not reach:** the name derivation and the container order
    (``test_arxml_access_writer.py``'s), and the import side (``test_component_import.py``'s).
    """

    def test_every_container_carries_its_points(self, tmp_path: Path) -> None:
        model_root = _build_model_with_access(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert _access_containers(out) == {
            "DATA-RECEIVE-POINT-BY-ARGUMENTS": ["R_rSpeed_speed"],
            "DATA-RECEIVE-POINT-BY-VALUES": ["R_rSpeed_pose"],
            "DATA-SEND-POINTS": ["W_pSpeed_speed"],
            "SERVER-CALL-POINTS": ["Call_rCalib_SetOffset"],
        }

    def test_the_capability_marker_sits_on_the_behaviour(self, tmp_path: Path) -> None:
        """The marker lets `import component --update` tell "accesses nothing" from "cannot say"."""
        model_root = _build_model_with_access(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert _access_marker_data(out) == [
            ("SWCRAFT", "ACCESS-POINTS", "explicit"),
            ("SWCRAFT", DATA_TYPES.sd_gid, DATA_TYPES.text),
            ("SWCRAFT", OPERATION_EVENTS.sd_gid, OPERATION_EVENTS.text),
            ("SWCRAFT", INTER_RUNNABLE_VARIABLES.sd_gid, INTER_RUNNABLE_VARIABLES.text),
            ("SWCRAFT", EXCLUSIVE_AREAS.sd_gid, EXCLUSIVE_AREAS.text),
            ("SWCRAFT", DATA_EVENTS.sd_gid, DATA_EVENTS.text),
            ("SWCRAFT", IMPLICIT_ACCESS.sd_gid, IMPLICIT_ACCESS.text),
            ("SWCRAFT", ASYNCHRONOUS_CALLS.sd_gid, ASYNCHRONOUS_CALLS.text),
        ]
        behavior = _behavior(out)
        assert [_local(child) for child in behavior][:2] == ["SHORT-NAME", "ADMIN-DATA"]
        assert [_local(child) for child in behavior].count("ADMIN-DATA") == 1

    def test_a_component_with_no_entry_exports_no_container_and_no_access_marker(self, tmp_path: Path) -> None:
        """A component with empty access lists exports unmarked for access points, so its re-import is `carried`.

        The behaviour still carries the seven markers every component export writes."""
        model_root = _build_model(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        assert _access_containers(out) == {}
        assert _access_marker_data(out) == [
            ("SWCRAFT", DATA_TYPES.sd_gid, DATA_TYPES.text),
            ("SWCRAFT", OPERATION_EVENTS.sd_gid, OPERATION_EVENTS.text),
            ("SWCRAFT", INTER_RUNNABLE_VARIABLES.sd_gid, INTER_RUNNABLE_VARIABLES.text),
            ("SWCRAFT", EXCLUSIVE_AREAS.sd_gid, EXCLUSIVE_AREAS.text),
            ("SWCRAFT", DATA_EVENTS.sd_gid, DATA_EVENTS.text),
            ("SWCRAFT", IMPLICIT_ACCESS.sd_gid, IMPLICIT_ACCESS.text),
            ("SWCRAFT", ASYNCHRONOUS_CALLS.sd_gid, ASYNCHRONOUS_CALLS.text),
        ]
        assert [_local(child) for child in _behavior(out)][:2] == ["SHORT-NAME", "ADMIN-DATA"]

    @pytest.mark.parametrize("version", list(ArxmlVersion), ids=lambda v: v.value)
    def test_the_export_is_xsd_valid_at_both_versions(self, tmp_path: Path, version: ArxmlVersion) -> None:
        """The licensed schema accepts the component export with its access containers and marker."""
        schema = require_licensed_schema(version)
        model_root = _build_model_with_access(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out), version=version)

        outcome = check_arxml(str(out), schema_path=schema)
        assert outcome.mode == MODE_XSD
        assert outcome.schema_error is False, outcome.messages
        assert outcome.valid, "\n".join((f"{out} does not conform to {schema}:", *outcome.messages))

    def test_the_export_summary_is_byte_identical_with_and_without_the_points(self, tmp_path: Path) -> None:
        """The summary is byte-identical with or without access points: the marker is no report channel."""
        plain_dir = tmp_path / "plain"
        with_points_dir = tmp_path / "with_points"

        # the second builder rewrites the first's tree in place, so the plain export goes first
        export_all_components(_build_model(tmp_path), str(plain_dir))
        export_all_components(_build_model_with_access(tmp_path), str(with_points_dir))

        plain = (plain_dir / EXPORT_SUMMARY_FILENAME).read_bytes()
        with_points = (with_points_dir / EXPORT_SUMMARY_FILENAME).read_bytes()
        assert with_points == plain
        # Non-vacuity: the component file does differ, so the equal summaries are not trivially equal.
        assert (with_points_dir / "TargetSwc.arxml").read_bytes() != (plain_dir / "TargetSwc.arxml").read_bytes()


class TestAWriterRefusalNamesNoTemporaryTree:
    """A writer refusal inside the export's temporary tree names the layer directory, never the tree.

    A base type without ``native_declaration`` passes the closure check and reaches the writer; its sentence is
    propagated unchanged to every surface.
    """

    def _without_native_declaration(self, tmp_path: Path) -> str:
        model_root = _build_model(tmp_path)
        base_type = {"name": "float32_base", "kind": "base_type", "size": 32, "encoding": "IEEE754"}
        _dump(model_element_path(Path(model_root), "float32_base", kind="base_type"), base_type)
        return model_root

    def test_the_writers_own_sentence_names_the_layer_directory_and_no_tree(self, tmp_path: Path) -> None:
        model_root = self._without_native_declaration(tmp_path)

        with pytest.raises(
            ValueError, match=r"^Base type\(s\) \['float32_base'\] in data_types/base/ declare no "
        ) as raised:
            export_component(model_root, "TargetSwc", str(tmp_path / "out" / "TargetSwc.arxml"))

        assert type(raised.value) is ValueError, "the writer's own refusal, not a wrapper rewriting it"
        assert "swcraft-component-export-" not in str(raised.value)
        assert str(tmp_path) not in str(raised.value)
        assert not (tmp_path / "out" / "TargetSwc.arxml").exists()

    def test_the_batch_report_and_the_summary_carry_the_shared_form(self, tmp_path: Path) -> None:
        model_root = self._without_native_declaration(tmp_path)
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        (failed,) = [report for report in reports if report.swc == "TargetSwc"]
        assert failed.error is not None and failed.error.startswith(
            "Base type(s) ['float32_base'] in data_types/base/ "
        )
        summary = json.loads((out_dir / EXPORT_SUMMARY_FILENAME).read_text(encoding="utf-8"))
        assert "swcraft-component-export-" not in json.dumps(summary)


#: The marker block a component with no access point carries, exactly as the writer serializes it.
DATA_TYPES_ONLY_BLOCK = (
    "              <ADMIN-DATA>\n"
    "                <SDGS>\n"
    '                  <SDG GID="SWCRAFT">\n'
    '                    <SD GID="DATA-TYPES">base and implementation types declared, no invented twin</SD>\n'
    '                    <SD GID="OPERATION-EVENTS">operation events and concurrency declared</SD>\n'
    '                    <SD GID="INTER-RUNNABLE-VARIABLES">inter-runnable variables and local variable accesses '
    "declared</SD>\n"
    '                    <SD GID="EXCLUSIVE-AREAS">exclusive areas and exclusive area references declared</SD>\n'
    '                    <SD GID="DATA-EVENTS">data-received events declared</SD>\n'
    '                    <SD GID="IMPLICIT-ACCESS">implicit reads and writes declared</SD>\n'
    '                    <SD GID="ASYNCHRONOUS-CALLS">asynchronous calls, result points and call-returned events '
    "declared</SD>\n"
    "                  </SDG>\n"
    "                </SDGS>\n"
    "              </ADMIN-DATA>\n"
)


class TestEveryComponentExportCarriesTheDataTypesMarker:
    """Every component export, and nothing else, states it invents no ``{B}_impl`` twin.

    ``import component`` refuses an unmarked file carrying a ``{B}_impl`` twin, the way an earlier release wrote
    one; only the exporter's statement tells a model's own such types apart.

    **What this class does not reach:** the import side (``test_component_import.py``'s) and the MCP and CLI
    routes (``test_component_exchange_tools.py`` and ``test_cli.py``).
    """

    def test_the_block_sits_after_the_behaviours_short_name(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out))

        text = out.read_text(encoding="utf-8")
        assert "              <SHORT-NAME>TargetSwc_InternalBehavior</SHORT-NAME>\n" + DATA_TYPES_ONLY_BLOCK in text
        assert text.count("<ADMIN-DATA>") == 1

    def test_the_batch_route_marks_every_file(self, tmp_path: Path) -> None:
        model_root = _build_model(tmp_path)
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        assert [r.swc for r in reports] == ["OtherSwc", "TargetSwc"]
        for report in reports:
            assert _access_marker_data(out_dir / f"{report.swc}.arxml") == [
                ("SWCRAFT", DATA_TYPES.sd_gid, DATA_TYPES.text),
                ("SWCRAFT", OPERATION_EVENTS.sd_gid, OPERATION_EVENTS.text),
                ("SWCRAFT", INTER_RUNNABLE_VARIABLES.sd_gid, INTER_RUNNABLE_VARIABLES.text),
                ("SWCRAFT", EXCLUSIVE_AREAS.sd_gid, EXCLUSIVE_AREAS.text),
                ("SWCRAFT", DATA_EVENTS.sd_gid, DATA_EVENTS.text),
                ("SWCRAFT", IMPLICIT_ACCESS.sd_gid, IMPLICIT_ACCESS.text),
                ("SWCRAFT", ASYNCHRONOUS_CALLS.sd_gid, ASYNCHRONOUS_CALLS.text),
            ]

    @pytest.mark.parametrize("builder", [_build_model, _build_model_with_access], ids=["no-point", "points"])
    def test_the_marker_is_the_only_difference_from_the_whole_model_document(self, tmp_path: Path, builder) -> None:
        """Whole-model ``generate arxml`` is untouched: one tree generated both ways differs by its seven markers."""
        model_root = builder(tmp_path)
        component = tmp_path / "component.arxml"
        whole = tmp_path / "whole.arxml"

        generate_component_arxml(model_root, str(component))
        generate_arxml(model_root, str(whole))

        whole_text = whole.read_text(encoding="utf-8")
        assert DATA_TYPES.sd_gid not in whole_text
        assert OPERATION_EVENTS.sd_gid not in whole_text
        assert f'<SD GID="{INTER_RUNNABLE_VARIABLES.sd_gid}">' not in whole_text
        assert f'<SD GID="{EXCLUSIVE_AREAS.sd_gid}">' not in whole_text
        assert f'<SD GID="{DATA_EVENTS.sd_gid}">' not in whole_text
        assert f'<SD GID="{IMPLICIT_ACCESS.sd_gid}">' not in whole_text
        assert f'<SD GID="{ASYNCHRONOUS_CALLS.sd_gid}">' not in whole_text
        stripped = re.sub(
            r'\n *<SD GID="(DATA-TYPES|OPERATION-EVENTS|INTER-RUNNABLE-VARIABLES|EXCLUSIVE-AREAS|DATA-EVENTS'
            r'|IMPLICIT-ACCESS|ASYNCHRONOUS-CALLS)">'
            r"[^<]*</SD>",
            "",
            component.read_text(encoding="utf-8"),
        )
        stripped = re.sub(
            r'\n *<ADMIN-DATA>\n *<SDGS>\n *<SDG GID="SWCRAFT">\n *</SDG>\n *</SDGS>\n *</ADMIN-DATA>', "", stripped
        )
        assert stripped == whole_text

    def test_the_marker_carries_no_version_string(self) -> None:
        from swcraft_tools._version import __version__

        assert DATA_TYPES.sd_gid == "DATA-TYPES"
        assert __version__ not in DATA_TYPES.text
        assert not re.search(r"\d", DATA_TYPES.text)

    @pytest.mark.parametrize("version", list(ArxmlVersion), ids=lambda v: v.value)
    def test_an_export_with_no_access_point_is_xsd_valid_at_both_versions(
        self, tmp_path: Path, version: ArxmlVersion
    ) -> None:
        """The licensed schema accepts the marker group of an export with no access point."""
        schema = require_licensed_schema(version)
        model_root = _build_model(tmp_path)
        out = tmp_path / "out" / "TargetSwc.arxml"

        export_component(model_root, "TargetSwc", str(out), version=version)

        outcome = check_arxml(str(out), schema_path=schema)
        assert outcome.mode == MODE_XSD
        assert outcome.schema_error is False, outcome.messages
        assert outcome.valid, "\n".join((f"{out} does not conform to {schema}:", *outcome.messages))
