"""Acceptance suite for component-scoped ARXML exchange over ``fixtures/component_exchange/model``.

Export determinism, per-SWC scoping (the fixture's connector never leaks), self-contained files,
a semantically equal round trip (Pydantic-equal per layer, *not* byte-identical), malformed or whole-model
input rejected before any write, and the target AUTOSAR version (default 4.2.2; only ``xsi:schemaLocation``
differs at 4.3.0; ``export-summary.json`` byte-identical across versions; an unsupported version writes
nothing; the round trip re-run at 4.3.0). An identical shared definition is reused and a divergent one
rejected, ``update=True`` included; an existing component's name is rejected by default and accepted only
through ``update=True``, with a diff preview, changing only that component's file. The reference type and
a direct enum-typed data element round-trip in ``test_component_import.py``.

The fixture is read-only: anything that needs mutation lives under ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_storage_name, write_model_element
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.cli import main
from swcraft_tools.component_exchange import (
    EXPORT_SUMMARY_FILENAME,
    ComponentImportConflictError,
    InvalidComponentArxmlError,
    export_all_components,
    export_component,
    import_component,
)
from swcraft_tools.reader import read_data_types, read_interfaces, read_layer, read_swc_model
from swcraft_tools.schema import SWComponent
from swcraft_tools.writer import generate_arxml

FIXTURE_MODEL = Path(__file__).parent / "fixtures" / "component_exchange" / "model"

# The two closures, sorted by name, derived by hand from the fixture's declarations and never
# read off an export:
#   SensorSwc   -> IVehicleSpeed: Speed_T -> float32_base; IPose: Pose_T -> Distance_T, Gear_T;
#                  ICalibration: Samples_T -> Velocity_T, uint8 -> uint8_base
#   ActuatorSwc -> IVehicleSpeed: Speed_T -> float32_base; IActuatorStatus: Status_T -> uint16_base
# The value types ``float32`` and ``uint16`` are in neither closure.
SENSOR_IFACES = ["ICalibration", "IPose", "IVehicleSpeed"]
SENSOR_TYPES = [
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
ACTUATOR_IFACES = ["IActuatorStatus", "IVehicleSpeed"]
ACTUATOR_TYPES = ["Speed_T", "Status_T", "float32_base", "uint16_base"]
# What the two closures share — reused, never duplicated, on a second import.
SHARED_IFACES = ["IVehicleSpeed"]
SHARED_TYPES = ["Speed_T", "float32_base"]
# The order an import report lists data types in: the export's source paths compared ordinally,
# so the layer package (``Application``, ``Base``, ``Implementation``) sorts before the name.
SHARED_TYPES_IN_SOURCE_ORDER = ["float32_base", "Speed_T"]
SENSOR_CREATED_IN_SOURCE_ORDER = ["Distance_T", "Gear_T", "Pose_T", "Samples_T", "Velocity_T", "uint8_base", "uint8"]


#: The fixture's ``base_types`` layer elements.
BASE_TYPES = frozenset({"float32_base", "uint8_base", "uint16_base"})
#: The fixture's ``application_types`` layer elements.
APPLICATION_TYPES = frozenset({"Distance_T", "Gear_T", "Pose_T", "Samples_T", "Velocity_T"})


def _type_file(name: str) -> str:
    """The model-root-relative file of fixture data type *name*, in whichever of the three layers holds it."""
    if name in BASE_TYPES:
        return model_element_storage_name(name, layer="base_types")
    if name in APPLICATION_TYPES:
        return model_element_storage_name(name, layer="application_types")
    return model_element_storage_name(name, layer="implementation_types")


def _both_type_layers(model_root: str) -> list[Any]:
    """Every element of the three data description type layers."""
    return [
        *read_layer(model_root, "base_types"),
        *read_layer(model_root, "application_types"),
        *read_data_types(model_root),
    ]


def _dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _unlinked(swc: SWComponent) -> SWComponent:
    """*swc* without a trace link: an export has no place for one (requirements are integrator-owned)."""
    return swc.model_copy(
        update={
            "satisfies": [],
            "ports": [port.model_copy(update={"satisfies": []}) for port in swc.ports],
            "runnables": [runnable.model_copy(update={"satisfies": []}) for runnable in swc.runnables],
        }
    )


def _init_project(tmp_path: Path, name: str) -> Path:
    """Scaffold a fresh SWCraft project via the real ``swcraft init`` CLI path."""
    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if p.is_file()
    }


def _seed_from_fixture(model_root: Path, rel_paths: list[str]) -> None:
    """Copy fixture files byte for byte into a target project's model tree."""
    for rel in rel_paths:
        target = model_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((FIXTURE_MODEL / rel).read_bytes())


def _short_names(arxml_path: Path) -> set[str]:
    root = ET.parse(arxml_path).getroot()
    return {el.text for el in root.iter() if el.tag.split("}")[-1] == "SHORT-NAME" and el.text}


def _local_tags(arxml_path: Path) -> set[str]:
    root = ET.parse(arxml_path).getroot()
    return {el.tag.split("}")[-1] for el in root.iter()}


def _defined_paths(root: ET.Element) -> set[str]:
    """Every AUTOSAR path defined in the file, by SHORT-NAME ancestry (/Pkg/Sub/Element)."""
    paths: set[str] = set()

    def collect(el: ET.Element, prefix: str) -> None:
        short = next((c.text for c in el if c.tag.split("}")[-1] == "SHORT-NAME" and c.text), None)
        if short is not None:
            prefix = f"{prefix}/{short}"
            paths.add(prefix)
        for child in el:
            collect(child, prefix)

    collect(root, "")
    return paths


def _reference_paths(root: ET.Element) -> set[str]:
    """Every AUTOSAR path referenced by a ``*-REF``/``*-TREF`` element."""
    refs: set[str] = set()
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if (tag.endswith("-REF") or tag.endswith("-TREF")) and el.text and el.text.startswith("/"):
            refs.add(el.text)
    return refs


# A SensorSwc that diverges from the fixture's: the update-eligible collision seed.
_DIVERGENT_SENSOR_SWC = {
    "name": "SensorSwc",
    "type": "application",
    "ports": [{"name": "pSpeed", "direction": "provide", "interface": "IVehicleSpeed"}],
}

# Everything SensorSwc's closure needs, seeded identically so a collision test isolates the SWC file.
_SENSOR_CLOSURE_FILES = [f"interfaces/{n}.yaml" for n in SENSOR_IFACES] + [_type_file(n) for n in SENSOR_TYPES]


# Single export: determinism across repeated runs


class TestSingleExportDeterminism:
    def test_repeated_exports_are_byte_identical(self, tmp_path: Path) -> None:
        outputs = [tmp_path / f"run{i}.arxml" for i in range(3)]

        for out in outputs:
            report = export_component(str(FIXTURE_MODEL), "SensorSwc", str(out))
            assert report.status == "ok"

        first = outputs[0].read_bytes()
        assert all(out.read_bytes() == first for out in outputs[1:])


# Batch export: one scoped file per SWC + schema-checked summary


class TestBatchExportAcceptance:
    def test_one_file_per_swc_each_scoped_to_its_own_closure(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "batch"

        reports = export_all_components(str(FIXTURE_MODEL), str(out_dir))

        assert [r.swc for r in reports] == ["ActuatorSwc", "SensorSwc"]
        assert all(r.status == "ok" for r in reports)
        assert sorted(p.name for p in out_dir.iterdir()) == [
            "ActuatorSwc.arxml",
            "SensorSwc.arxml",
            EXPORT_SUMMARY_FILENAME,
        ]
        sensor_names = _short_names(out_dir / "SensorSwc.arxml")
        assert "SensorSwc" in sensor_names
        assert set(SENSOR_IFACES) <= sensor_names
        assert set(SENSOR_TYPES) <= sensor_names
        assert not {"ActuatorSwc", "IActuatorStatus", "Status_T", "uint16", "uint16_base"} & sensor_names
        actuator_names = _short_names(out_dir / "ActuatorSwc.arxml")
        assert "ActuatorSwc" in actuator_names
        assert set(ACTUATOR_IFACES) <= actuator_names
        assert set(ACTUATOR_TYPES) <= actuator_names
        assert (
            not {
                "SensorSwc",
                "IPose",
                "ICalibration",
                "Gear_T",
                "Pose_T",
                "Samples_T",
                "float32",
                "uint8",
                "uint8_base",
            }
            & actuator_names
        )

    def test_no_composition_or_connector_in_any_exported_file(self, tmp_path: Path) -> None:
        """The fixture's connector, integration-owned, leaks into no component export."""
        out_dir = tmp_path / "batch"

        export_all_components(str(FIXTURE_MODEL), str(out_dir))

        for arxml in sorted(out_dir.glob("*.arxml"), key=lambda path: path.as_posix()):
            tags = _local_tags(arxml)
            assert not any("COMPOSITION" in t for t in tags), arxml.name
            assert not any("CONNECTOR" in t for t in tags), arxml.name

    def test_export_summary_json_matches_the_documented_schema_exactly(self, tmp_path: Path) -> None:
        """Exact text: the key set and order, each closure, entry order and byte-level determinism."""
        out_dir = tmp_path / "batch"

        export_all_components(str(FIXTURE_MODEL), str(out_dir))

        expected = [
            {
                "swc": "ActuatorSwc",
                "output_path": "ActuatorSwc.arxml",
                "status": "ok",
                "interfaces": ACTUATOR_IFACES,
                "data_types": ACTUATOR_TYPES,
                "error": None,
            },
            {
                "swc": "SensorSwc",
                "output_path": "SensorSwc.arxml",
                "status": "ok",
                "interfaces": SENSOR_IFACES,
                "data_types": SENSOR_TYPES,
                "error": None,
            },
        ]
        raw = (out_dir / EXPORT_SUMMARY_FILENAME).read_text(encoding="utf-8")
        assert raw == json.dumps(expected, indent=2) + "\n"


# No dangling references in any exported file


class TestNoDanglingReferences:
    def test_every_reference_resolves_within_its_own_file(self, tmp_path: Path) -> None:
        """A component export is self-contained: every *-REF/*-TREF resolves within its own file."""
        out_dir = tmp_path / "batch"

        export_all_components(str(FIXTURE_MODEL), str(out_dir))

        for arxml in sorted(out_dir.glob("*.arxml"), key=lambda path: path.as_posix()):
            root = ET.parse(arxml).getroot()
            refs = _reference_paths(root)
            assert refs, f"{arxml.name}: expected at least one reference (TREFs, type refs)"
            dangling = refs - _defined_paths(root)
            assert dangling == set(), f"{arxml.name}: dangling references {sorted(dangling)}"


# Round trip: export -> fresh-project import -> re-export, semantically equal


class TestRoundTripAcceptance:
    #: The AUTOSAR version this round trip exports at. ``None`` passes no ``version`` at all, to
    #: exercise the default call shape. :class:`TestRoundTripAcceptanceAt4_3_0` overrides it.
    EXPORT_VERSION: ArxmlVersion | None = None

    def _export(self, model_root: str, swc_name: str, output: Path):
        """Export at :attr:`EXPORT_VERSION` — the one seam the 4.3.0 subclass moves."""
        if self.EXPORT_VERSION is None:
            return export_component(model_root, swc_name, str(output))
        return export_component(model_root, swc_name, str(output), version=self.EXPORT_VERSION)

    def test_export_import_reexport_is_semantically_equivalent(self, tmp_path: Path) -> None:
        """Export, import into A, re-export, import into B: A equals the closure and B equals A per layer.

        Semantic equivalence, deliberately NOT byte-identity of the vendor XML.
        """
        exported_once = tmp_path / "SensorSwc.arxml"
        self._export(str(FIXTURE_MODEL), "SensorSwc", exported_once)
        project_a = _init_project(tmp_path, "projA")

        report_a = import_component(str(exported_once), str(project_a / "model"))

        assert report_a.swc_action == "created"
        a_root = str(project_a / "model")
        source_swcs = {s.name: s for s in read_swc_model(str(FIXTURE_MODEL))}
        a_swcs = read_swc_model(a_root)
        assert [s.name for s in a_swcs] == ["SensorSwc"]
        assert a_swcs[0] == _unlinked(source_swcs["SensorSwc"])
        source_ifaces = {i.name: i for i in read_interfaces(str(FIXTURE_MODEL))}
        a_ifaces = {i.name: i for i in read_interfaces(a_root)}
        assert sorted(a_ifaces) == SENSOR_IFACES
        assert all(a_ifaces[n] == source_ifaces[n] for n in SENSOR_IFACES)
        source_types = {d.name: d for d in _both_type_layers(str(FIXTURE_MODEL))}
        a_types = {d.name: d for d in _both_type_layers(a_root)}
        assert sorted(a_types) == SENSOR_TYPES
        assert all(a_types[n] == source_types[n] for n in SENSOR_TYPES)

        exported_again = tmp_path / "SensorSwc.reexport.arxml"
        reexport_report = self._export(a_root, "SensorSwc", exported_again)
        assert reexport_report.status == "ok"
        assert reexport_report.interfaces == SENSOR_IFACES
        assert reexport_report.data_types == SENSOR_TYPES

        project_b = _init_project(tmp_path, "projB")
        import_component(str(exported_again), str(project_b / "model"))
        b_root = str(project_b / "model")
        b_swcs = read_swc_model(b_root)
        assert [s.name for s in b_swcs] == ["SensorSwc"]
        assert b_swcs[0] == a_swcs[0]
        b_ifaces = {i.name: i for i in read_interfaces(b_root)}
        assert sorted(b_ifaces) == SENSOR_IFACES
        assert all(b_ifaces[n] == a_ifaces[n] for n in SENSOR_IFACES)
        b_types = {d.name: d for d in _both_type_layers(b_root)}
        assert sorted(b_types) == SENSOR_TYPES
        assert all(b_types[n] == a_types[n] for n in SENSOR_TYPES)


# Import into a non-empty project: shared-definition policy


class TestNonEmptyProjectImport:
    def test_identical_shared_definitions_are_reused_silently(self, tmp_path: Path) -> None:
        export = tmp_path / "SensorSwc.arxml"
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(export))
        project = _init_project(tmp_path, "proj")
        model_root = project / "model"
        shared_files = [f"interfaces/{n}.yaml" for n in SHARED_IFACES] + [_type_file(n) for n in SHARED_TYPES]
        _seed_from_fixture(model_root, shared_files)
        shared_before = {rel: (model_root / rel).read_bytes() for rel in shared_files}

        report = import_component(str(export), str(project / "model"))

        assert report.reused_interfaces == SHARED_IFACES
        assert report.reused_data_types == SHARED_TYPES_IN_SOURCE_ORDER
        assert report.created_interfaces == ["ICalibration", "IPose"]
        # Derived from the hand-written closures, so a type entering the closure moves one list, not two.
        assert report.created_data_types == SENSOR_CREATED_IN_SOURCE_ORDER
        assert sorted(report.created_data_types) == [name for name in SENSOR_TYPES if name not in SHARED_TYPES]
        assert {rel: (model_root / rel).read_bytes() for rel in shared_files} == shared_before

    def test_two_components_exchange_into_one_project_share_definitions(self, tmp_path: Path) -> None:
        """Two exported components land in one project: the second reuses what the first created."""
        out_dir = tmp_path / "batch"
        export_all_components(str(FIXTURE_MODEL), str(out_dir))
        project = _init_project(tmp_path, "proj")
        model_root = str(project / "model")

        first = import_component(str(out_dir / "SensorSwc.arxml"), model_root)
        second = import_component(str(out_dir / "ActuatorSwc.arxml"), model_root)

        assert first.swc_action == "created"
        assert second.swc_action == "created"
        assert second.reused_interfaces == SHARED_IFACES
        assert second.reused_data_types == SHARED_TYPES_IN_SOURCE_ORDER
        assert second.created_interfaces == ["IActuatorStatus"]
        assert second.created_data_types == ["uint16_base", "Status_T"]
        model_root = str(project / "model")
        source_swcs = {s.name: s for s in read_swc_model(str(FIXTURE_MODEL))}
        target_swcs = {s.name: s for s in read_swc_model(model_root)}
        assert sorted(target_swcs) == ["ActuatorSwc", "SensorSwc"]
        assert all(target_swcs[n] == _unlinked(source_swcs[n]) for n in target_swcs)
        assert sorted(i.name for i in read_interfaces(model_root)) == sorted({*SENSOR_IFACES, *ACTUATOR_IFACES})
        assert sorted(d.name for d in _both_type_layers(model_root)) == sorted({*SENSOR_TYPES, *ACTUATOR_TYPES})

    @pytest.mark.parametrize("update", [False, True], ids=["default", "update=True"])
    def test_divergent_shared_definition_is_always_rejected(self, tmp_path: Path, update: bool) -> None:
        """A divergent shared data type rejects the import with nothing written, ``update=True`` or not.

        No shared definition is ever overridden by an import.
        """
        export = tmp_path / "SensorSwc.arxml"
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(export))
        project = _init_project(tmp_path, "proj")
        model_root = project / "model"
        # A base type named like the value type `Speed_T` is another layer's element and no
        # divergence; the shared base type itself diverges here.
        write_model_element(model_root, {"name": "float32_base", "kind": "base_type", "size": 16, "encoding": "NONE"})
        before = _tree_snapshot(model_root)

        with pytest.raises(ComponentImportConflictError, match="float32_base") as exc_info:
            import_component(str(export), str(project / "model"), update=update)

        assert any("float32_base" in entry for entry in exc_info.value.conflicts)
        assert _tree_snapshot(model_root) == before


# Existing-component-name collision: --update, and only the SWC's own file


def _collision_project(tmp_path: Path) -> tuple[Path, Path]:
    """A target whose closure definitions equal the fixture's and whose SensorSwc file diverges."""
    project = _init_project(tmp_path, "proj")
    model_root = project / "model"
    _seed_from_fixture(model_root, _SENSOR_CLOSURE_FILES)
    existing_swc_file = model_root / "swc" / "custom" / "SensorSwc.yaml"
    _dump(existing_swc_file, _DIVERGENT_SENSOR_SWC)
    return project, existing_swc_file


class TestComponentNameCollision:
    def test_collision_is_rejected_by_default(self, tmp_path: Path) -> None:
        export = tmp_path / "SensorSwc.arxml"
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(export))
        project, _ = _collision_project(tmp_path)
        before = _tree_snapshot(project / "model")

        with pytest.raises(ComponentImportConflictError, match="SensorSwc") as exc_info:
            import_component(str(export), str(project / "model"))

        assert any("SensorSwc" in entry for entry in exc_info.value.conflicts)
        assert _tree_snapshot(project / "model") == before

    def test_update_replaces_only_that_swc_file_with_a_diff_preview(self, tmp_path: Path) -> None:
        """``update=True`` replaces only the SWC's own file, at its existing path, with a diff preview."""
        export = tmp_path / "SensorSwc.arxml"
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(export))
        project, existing_swc_file = _collision_project(tmp_path)
        model_root = project / "model"
        before = _tree_snapshot(model_root)

        report = import_component(str(export), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        assert report.written_paths == [str(existing_swc_file)]
        assert not (model_root / "swc" / "components" / "SensorSwc.yaml").exists()
        after = _tree_snapshot(model_root)
        assert set(after) == set(before)
        changed = {rel for rel in before if before[rel] != after[rel]}
        assert changed == {existing_swc_file.relative_to(model_root).as_posix()}
        source_swcs = {s.name: s for s in read_swc_model(str(FIXTURE_MODEL))}
        target_swcs = read_swc_model(str(model_root))
        assert [s.name for s in target_swcs] == ["SensorSwc"]
        assert target_swcs[0] == _unlinked(source_swcs["SensorSwc"])
        assert "~ SensorSwc" in report.swc_diff_preview
        assert "ports" in report.swc_diff_preview
        assert "runnables" in report.swc_diff_preview
        assert "diff preview" in report.render()


# The refusal --update resolves: its own category and exit status 3


def _run_import_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """``swcraft import component ...`` through ``main()``: the exit status, stdout and stderr."""
    capsys.readouterr()  # what `init` and the setup printed is not this run's
    saved = sys.argv
    code = 0
    try:
        sys.argv = ["swcraft", "import", "component", *argv]
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _diverge_shared_interface(model_root: Path) -> None:
    """Rewrite the target's ``IVehicleSpeed`` so the incoming one diverges from it."""
    _dump(
        model_root / "interfaces" / "IVehicleSpeed.yaml",
        {
            "name": "IVehicleSpeed",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "speed", "implementation_type": "Speed_T"},
                {"name": "accel", "implementation_type": "Speed_T"},
            ],
        },
    )


class TestTheRefusalUpdateResolves:
    """``ComponentImportConflictError.update_resolves`` and ``import component``'s exit 3.

    True exactly when the run lacks ``--update`` and the one conflict is the component's own
    divergence; the CLI exits ``UPDATE_RESOLVES_EXIT`` (3) for it, 1 for every other refusal.
    What this does not reach: the update run itself, whose own report decides.
    """

    def _project_with_a_diverging_sensor(self, tmp_path: Path) -> tuple[Path, Path]:
        export = tmp_path / "SensorSwc.arxml"
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(export))
        project = _collision_project(tmp_path)[0]
        return project, export

    def test_the_component_divergence_alone_exits_three_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project, export = self._project_with_a_diverging_sensor(tmp_path)
        before = _tree_snapshot(project)

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(str(export), str(project / "model"), dry_run=True)
        assert exc_info.value.update_resolves is True

        for extra in ((), ("--dry-run",)):
            code, out, err = _run_import_cli(
                capsys, "--source", str(export), "--project", str(project / "swcraft.yaml"), *extra
            )
            assert code == 3
            assert out == ""
            # Exit 3 changes no stderr: one `Error: ` line naming the divergence.
            assert err.startswith("Error: component import aborted with nothing written: ")
            assert "SWC 'SensorSwc' already exists in the target project and diverges" in err
            assert len(err.strip().splitlines()) == 1
        assert _tree_snapshot(project) == before

    def test_the_same_file_with_dry_run_update_exits_zero_and_reports_updated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project, export = self._project_with_a_diverging_sensor(tmp_path)
        before = _tree_snapshot(project)

        code, out, err = _run_import_cli(
            capsys, "--source", str(export), "--project", str(project / "swcraft.yaml"), "--dry-run", "--update"
        )

        assert code == 0, err
        assert "SWC SensorSwc: updated" in out.splitlines()
        assert _tree_snapshot(project) == before

    def test_the_divergence_beside_a_diverging_shared_interface_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project, export = self._project_with_a_diverging_sensor(tmp_path)
        _diverge_shared_interface(project / "model")

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(str(export), str(project / "model"), dry_run=True)
        assert exc_info.value.update_resolves is False
        assert len(exc_info.value.conflicts) == 2

        for extra in ((), ("--update",)):
            code, out, err = _run_import_cli(
                capsys, "--source", str(export), "--project", str(project / "swcraft.yaml"), "--dry-run", *extra
            )
            assert (code, out) == (1, "")
            assert "IVehicleSpeed" in err

    @pytest.mark.parametrize("update", [False, True], ids=["default", "update"])
    def test_a_diverging_shared_interface_alone_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], update: bool
    ) -> None:
        export = tmp_path / "SensorSwc.arxml"
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(export))
        project = _init_project(tmp_path, "proj")
        _seed_from_fixture(project / "model", _SENSOR_CLOSURE_FILES)
        _diverge_shared_interface(project / "model")

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(str(export), str(project / "model"), update=update, dry_run=True)
        assert exc_info.value.update_resolves is False

        extra = ("--update",) if update else ()
        code, out, err = _run_import_cli(
            capsys, "--source", str(export), "--project", str(project / "swcraft.yaml"), *extra
        )
        assert (code, out) == (1, "")
        assert "IVehicleSpeed" in err

    @pytest.mark.parametrize("update", [False, True], ids=["default", "update"])
    def test_a_component_stub_of_the_incoming_component_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], update: bool
    ) -> None:
        export = tmp_path / "SensorSwc.arxml"
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(export))
        project = _init_project(tmp_path, "proj")
        _dump(
            project / "model" / "swc" / "external" / "SensorSwc_ext0.yaml",
            {
                "name": "SensorSwc_ext0",
                "type": "application",
                "external": True,
                "external_ref": "/ComponentTypes/SensorSwc",
                "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                "ports": [],
                "runnables": [],
            },
        )
        before = _tree_snapshot(project)

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(str(export), str(project / "model"), update=update)
        assert exc_info.value.update_resolves is False

        extra = ("--update",) if update else ()
        code, out, err = _run_import_cli(
            capsys, "--source", str(export), "--project", str(project / "swcraft.yaml"), *extra
        )
        assert (code, out) == (1, "")
        assert "SensorSwc_ext0" in err
        assert _tree_snapshot(project) == before

    def test_every_other_refusal_keeps_exit_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The refusals that are not a conflict exit 1."""
        project = _init_project(tmp_path, "proj")
        manifest = str(project / "swcraft.yaml")
        whole_model = tmp_path / "whole_model.arxml"
        generate_arxml(str(FIXTURE_MODEL), str(whole_model))
        garbage = tmp_path / "garbage.arxml"
        garbage.write_text("<AUTOSAR><AR-PACKAGES>this is not well-formed", encoding="utf-8", newline="\n")
        export = tmp_path / "SensorSwc.arxml"
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(export))

        refusals = [
            ("--source", str(whole_model), "--project", manifest),
            ("--source", str(garbage), "--project", manifest),
            ("--source", str(tmp_path / "missing.arxml"), "--project", manifest),
            ("--source", str(export), "--project", manifest, "--replace-access-lists"),
            ("--project", manifest),
        ]
        for argv in refusals:
            code, out, err = _run_import_cli(capsys, *argv)
            assert (code, out) == (1, ""), argv
            assert err.startswith("Error: "), argv


# Malformed / multi-SWC input: rejected before any write


class TestInvalidInputRejection:
    def test_whole_model_export_is_rejected_before_any_write(self, tmp_path: Path) -> None:
        """A full-model ARXML (two SWCs and a composition) is rejected by the shape check."""
        whole_model = tmp_path / "whole_model.arxml"
        generate_arxml(str(FIXTURE_MODEL), str(whole_model))
        project = _init_project(tmp_path, "proj")
        before = _tree_snapshot(project / "model")

        with pytest.raises(InvalidComponentArxmlError, match="exactly one SWC") as exc_info:
            import_component(str(whole_model), str(project / "model"))

        assert any("composition/connector" in v for v in exc_info.value.violations)
        assert _tree_snapshot(project / "model") == before

    def test_malformed_xml_is_rejected_before_any_write(self, tmp_path: Path) -> None:
        garbage = tmp_path / "garbage.arxml"
        garbage.write_text("<AUTOSAR><AR-PACKAGES>this is not well-formed", encoding="utf-8")
        project = _init_project(tmp_path, "proj")
        before = _tree_snapshot(project / "model")

        with pytest.raises(ValueError, match="Malformed ARXML"):
            import_component(str(garbage), str(project / "model"))

        assert _tree_snapshot(project / "model") == before


# Target AUTOSAR version
#
# export.py never loads the manifest that declares arxml_version (it materializes a temp
# model root), so the version is a keyword-only parameter, defaulted to 4.2.2, that the
# CLI/MCP layers thread.


def _schema_location(arxml_path: Path) -> str:
    root = ET.parse(arxml_path).getroot()
    return root.attrib["{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"]


class TestExportAtAnAutosarVersion:
    """``export_component``/``export_all_components`` emit the version they are given."""

    def test_the_default_is_still_4_2_2(self, tmp_path: Path) -> None:
        """The three-positional call shape exports 4.2.2."""
        out = tmp_path / "SensorSwc.arxml"

        export_component(str(FIXTURE_MODEL), "SensorSwc", str(out))

        assert _schema_location(out).endswith(ArxmlVersion.V4_2_2.schema_filename)

    @pytest.mark.parametrize("version", list(ArxmlVersion))
    def test_an_explicit_version_is_what_the_file_declares(self, tmp_path: Path, version: ArxmlVersion) -> None:
        out = tmp_path / "SensorSwc.arxml"

        export_component(str(FIXTURE_MODEL), "SensorSwc", str(out), version=version)

        assert _schema_location(out) == version.schema_location

    def test_a_version_string_is_accepted_like_the_enum_member(self, tmp_path: Path) -> None:
        """A caller reading a raw manifest value need not convert it first."""
        from_enum = tmp_path / "enum.arxml"
        from_string = tmp_path / "string.arxml"

        export_component(str(FIXTURE_MODEL), "SensorSwc", str(from_enum), version=ArxmlVersion.V4_3_0)
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(from_string), version="4.3.0")

        assert from_enum.read_bytes() == from_string.read_bytes()

    def test_only_the_schema_location_differs_between_the_two_versions(self, tmp_path: Path) -> None:
        """The whole-model claim of ``test_arxml_4_3_0.py`` holds for a component export too."""
        at_4_2_2 = tmp_path / "at-4-2-2.arxml"
        at_4_3_0 = tmp_path / "at-4-3-0.arxml"

        export_component(str(FIXTURE_MODEL), "SensorSwc", str(at_4_2_2), version=ArxmlVersion.V4_2_2)
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(at_4_3_0), version=ArxmlVersion.V4_3_0)

        text_4_2_2 = at_4_2_2.read_text(encoding="utf-8")
        text_4_3_0 = at_4_3_0.read_text(encoding="utf-8")
        # The inequality control: the substitution below alone passes on an exporter ignoring the version.
        assert text_4_2_2 != text_4_3_0
        substituted = text_4_2_2.replace(ArxmlVersion.V4_2_2.schema_filename, ArxmlVersion.V4_3_0.schema_filename)
        assert substituted == text_4_3_0

    def test_exports_stay_deterministic_per_version(self, tmp_path: Path) -> None:
        """A 4.2.2 export *between* two 4.3.0 ones changes neither: no version is held as writer state."""
        first = tmp_path / "first.arxml"
        between = tmp_path / "between.arxml"
        second = tmp_path / "second.arxml"

        export_component(str(FIXTURE_MODEL), "SensorSwc", str(first), version=ArxmlVersion.V4_3_0)
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(between), version=ArxmlVersion.V4_2_2)
        export_component(str(FIXTURE_MODEL), "SensorSwc", str(second), version=ArxmlVersion.V4_3_0)

        assert first.read_bytes() == second.read_bytes()
        assert _schema_location(between).endswith(ArxmlVersion.V4_2_2.schema_filename)

    def test_an_unsupported_version_writes_nothing(self, tmp_path: Path) -> None:
        """Rejected before the closure is computed: neither the output file nor its ``.tmp`` sibling."""
        out = tmp_path / "SensorSwc.arxml"

        with pytest.raises(ValueError, match="4.9.9"):
            export_component(str(FIXTURE_MODEL), "SensorSwc", str(out), version="4.9.9")

        assert list(tmp_path.iterdir()) == []


class TestBatchExportAtAnAutosarVersion:
    """The batch honors the version for every file, and its summary is byte-identical across versions."""

    def test_every_exported_file_declares_the_requested_version(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "batch"

        reports = export_all_components(str(FIXTURE_MODEL), str(out_dir), version=ArxmlVersion.V4_3_0)

        assert [r.status for r in reports] == ["ok", "ok"]
        exported = sorted(out_dir.glob("*.arxml"), key=lambda path: path.as_posix())
        assert [p.name for p in exported] == ["ActuatorSwc.arxml", "SensorSwc.arxml"]
        for arxml in exported:
            assert _schema_location(arxml) == ArxmlVersion.V4_3_0.schema_location

    def test_the_summary_is_byte_identical_across_versions(self, tmp_path: Path) -> None:
        dir_4_2_2 = tmp_path / "at-4-2-2"
        dir_4_3_0 = tmp_path / "at-4-3-0"

        export_all_components(str(FIXTURE_MODEL), str(dir_4_2_2), version=ArxmlVersion.V4_2_2)
        export_all_components(str(FIXTURE_MODEL), str(dir_4_3_0), version=ArxmlVersion.V4_3_0)

        summary_4_2_2 = (dir_4_2_2 / EXPORT_SUMMARY_FILENAME).read_bytes()
        assert summary_4_2_2 == (dir_4_3_0 / EXPORT_SUMMARY_FILENAME).read_bytes()
        # ... and the ARXML really did differ, or the assertion above is vacuous.
        assert (dir_4_2_2 / "SensorSwc.arxml").read_bytes() != (dir_4_3_0 / "SensorSwc.arxml").read_bytes()

    def test_the_batch_default_is_still_4_2_2(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "batch"

        export_all_components(str(FIXTURE_MODEL), str(out_dir))

        for arxml in sorted(out_dir.glob("*.arxml"), key=lambda path: path.as_posix()):
            assert _schema_location(arxml).endswith(ArxmlVersion.V4_2_2.schema_filename)

    def test_an_unsupported_version_creates_no_output_directory(self, tmp_path: Path) -> None:
        """A bad argument is refused up front, never a directory of per-SWC failures reading as a broken model."""
        out_dir = tmp_path / "batch"

        with pytest.raises(ValueError, match="4.9.9"):
            export_all_components(str(FIXTURE_MODEL), str(out_dir), version="4.9.9")

        assert not out_dir.exists()


class TestRoundTripAcceptanceAt4_3_0(TestRoundTripAcceptance):
    """The round trip re-run against 4.3.0 exports: a subclass, so the assertions cannot drift.

    The import gets no version hint and reads the file on the unchanged ``r4.0`` namespace alone.
    """

    EXPORT_VERSION = ArxmlVersion.V4_3_0

    def test_the_exported_file_really_declares_4_3_0(self, tmp_path: Path) -> None:
        """Guards the guard: an inherited round trip over a 4.2.2 file would prove nothing new."""
        out = tmp_path / "SensorSwc.arxml"

        self._export(str(FIXTURE_MODEL), "SensorSwc", out)

        assert _schema_location(out) == ArxmlVersion.V4_3_0.schema_location


# The round trip carries the values (over a COPY: the fixture declares no `init`)

#: The value put on ``IVehicleSpeed.speed``: exact in binary, so a difference is a real loss and
#: never a formatting artefact.
_ROUND_TRIP_INIT = 1.5


def _fixture_copy_with_an_init(tmp_path: Path) -> str:
    """The fixture model, copied, with ``IVehicleSpeed.speed`` declaring an init."""
    model_root = tmp_path / "model_with_init"
    for source in sorted(FIXTURE_MODEL.rglob("*"), key=lambda path: path.as_posix()):
        if source.is_file():
            target = model_root / source.relative_to(FIXTURE_MODEL)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    interface_file = model_root / "interfaces" / "IVehicleSpeed.yaml"
    authored = yaml.safe_load(interface_file.read_text(encoding="utf-8"))
    authored["data_elements"][0]["init"] = _ROUND_TRIP_INIT
    _dump(interface_file, authored)
    return str(model_root)


class TestInitValuesSurviveTheRoundTrip:
    """A declared value survives two exchanges; the committed fixture declares none.

    **What it does not reach:** a composite value, which the importer reads back only within the
    row-order limit in ``fixtures/init_values/README.md``.
    """

    def test_the_value_survives_two_exchanges(self, tmp_path: Path) -> None:
        source_root = _fixture_copy_with_an_init(tmp_path)
        exported_once = tmp_path / "SensorSwc.arxml"
        export_component(source_root, "SensorSwc", str(exported_once))
        project_a = _init_project(tmp_path, "projA")

        report_a = import_component(str(exported_once), str(project_a / "model"))

        assert report_a.swc_action == "created"
        a_root = str(project_a / "model")
        source_ifaces = {i.name: i for i in read_interfaces(source_root)}
        a_ifaces = {i.name: i for i in read_interfaces(a_root)}
        assert sorted(a_ifaces) == SENSOR_IFACES
        assert all(a_ifaces[n] == source_ifaces[n] for n in SENSOR_IFACES)
        assert a_ifaces["IVehicleSpeed"].data_elements[0].init == _ROUND_TRIP_INIT

        exported_again = tmp_path / "SensorSwc.reexport.arxml"
        export_component(a_root, "SensorSwc", str(exported_again))
        project_b = _init_project(tmp_path, "projB")
        import_component(str(exported_again), str(project_b / "model"))

        b_ifaces = {i.name: i for i in read_interfaces(str(project_b / "model"))}
        assert b_ifaces["IVehicleSpeed"] == a_ifaces["IVehicleSpeed"]
        assert b_ifaces["IVehicleSpeed"].data_elements[0].init == _ROUND_TRIP_INIT

    def test_a_second_import_into_the_declaring_project_reuses_it(self, tmp_path: Path) -> None:
        """A target declaring the same value reuses the interface and leaves its file untouched."""
        source_root = _fixture_copy_with_an_init(tmp_path)
        export = tmp_path / "SensorSwc.arxml"
        export_component(source_root, "SensorSwc", str(export))
        project = _init_project(tmp_path, "proj")
        import_component(str(export), str(project / "model"))
        interface_file = project / "model" / "interfaces" / "IVehicleSpeed.yaml"
        before = interface_file.read_bytes()

        report = import_component(str(export), str(project / "model"))

        assert report.swc_action == "unchanged"
        assert "IVehicleSpeed" in report.reused_interfaces
        assert interface_file.read_bytes() == before


#: The record value ``IPose.pose`` states: a number under an application primitive and an
#: enumeration member as the literal's text.
_ROUND_TRIP_RECORD_INIT = {"x": 2.5, "gear": "DRIVE"}


class TestEveryValueFormSurvivesTwoExchanges:
    """A record value of a number and an enumeration survives two exchanges with no line.

    The enumeration member travels as its literal's text, never through a number.

    **What it does not reach:** the forms the fixture's types do not state (an application
    primitive's own value, a table-typed value type, a port's own value); those are
    ``test_component_import_init_values.py``'s ``TestEveryInitKindExchangesUnderTheJudgement``.
    """

    def test_a_records_number_and_enumeration_members_survive_two_exchanges(self, tmp_path: Path) -> None:
        source_root = _fixture_copy_with_an_init(tmp_path)
        pose_file = Path(source_root) / "interfaces" / "IPose.yaml"
        authored = yaml.safe_load(pose_file.read_text(encoding="utf-8"))
        authored["data_elements"][0]["init"] = dict(_ROUND_TRIP_RECORD_INIT)
        _dump(pose_file, authored)
        exported_once = tmp_path / "SensorSwc.arxml"
        export_component(source_root, "SensorSwc", str(exported_once))
        assert "<VT>DRIVE</VT>" in exported_once.read_text(encoding="utf-8")
        project_a = _init_project(tmp_path, "projA")

        report_a = import_component(str(exported_once), str(project_a / "model"))

        # No line at the value (a constant's own report-only line, that it is no SWCraft element, stays).
        lines = [*report_a.not_carried_lines, *report_a.skip_lines]
        assert [line for line in lines if line.startswith(("INIT-VALUE ", "VALUE-SPEC "))] == []
        a_root = str(project_a / "model")
        a_ifaces = {i.name: i for i in read_interfaces(a_root)}
        assert a_ifaces["IPose"].data_elements[0].init == _ROUND_TRIP_RECORD_INIT
        exported_again = tmp_path / "SensorSwc.reexport.arxml"
        export_component(a_root, "SensorSwc", str(exported_again))
        project_b = _init_project(tmp_path, "projB")
        import_component(str(exported_again), str(project_b / "model"))
        b_ifaces = {i.name: i for i in read_interfaces(str(project_b / "model"))}
        assert b_ifaces["IPose"] == a_ifaces["IPose"]
        assert b_ifaces["IVehicleSpeed"].data_elements[0].init == _ROUND_TRIP_INIT


#: The three lists put on ``SensorMain``: a read on the require port the helper adds, a write on
#: the provide port and a call on the require port.
_ROUND_TRIP_ACCESSES: dict[str, Any] = {
    "reads": [{"port": "rSpeed", "element": "speed"}],
    "writes": [{"port": "pSpeed", "element": "speed"}],
    "calls": [{"port": "rCalib", "operation": "SetOffset"}],
}


def _fixture_copy_with_access_lists(tmp_path: Path) -> str:
    """The fixture model, copied, with ``SensorMain`` declaring every list."""
    model_root = tmp_path / "model_with_access"
    for source in sorted(FIXTURE_MODEL.rglob("*"), key=lambda path: path.as_posix()):
        if source.is_file():
            target = model_root / source.relative_to(FIXTURE_MODEL)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    swc_file = next(iter(sorted((model_root / "swc").rglob("SensorSwc.yaml"), key=lambda p: p.as_posix())))
    authored = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    authored["ports"].append({"name": "rSpeed", "direction": "require", "interface": "IVehicleSpeed"})
    authored["runnables"][0].update(_ROUND_TRIP_ACCESSES)
    _dump(swc_file, authored)
    return str(model_root)


def _access_entries(model_root: str, swc_name: str = "SensorSwc") -> dict[str, list[tuple[str, ...]]]:
    runnable = next(s for s in read_swc_model(model_root) if s.name == swc_name).runnables[0]
    return {
        "reads": [(r.port, r.element, r.by) for r in runnable.reads],
        "writes": [(w.port, w.element) for w in runnable.writes],
        "calls": [(c.port, c.operation) for c in runnable.calls],
    }


class TestAccessListsSurviveTheRoundTrip:
    """Every declared access entry survives two exchanges; the committed fixture declares none.

    **What it does not reach:** the regime (`test_component_import.py`'s; every export here is
    SWCraft's own and therefore marked) and the AUTOSAR shapes (`test_arxml_access_writer.py`'s).
    """

    def test_every_entry_survives_two_exchanges(self, tmp_path: Path) -> None:
        source_root = _fixture_copy_with_access_lists(tmp_path)
        exported_once = tmp_path / "SensorSwc.arxml"
        export_component(source_root, "SensorSwc", str(exported_once))
        project_a = _init_project(tmp_path, "projA")

        report_a = import_component(str(exported_once), str(project_a / "model"))

        assert report_a.swc_action == "created"
        a_root = str(project_a / "model")
        assert _access_entries(a_root) == _access_entries(source_root)
        assert _access_entries(a_root) == {
            "reads": [("rSpeed", "speed", None)],
            "writes": [("pSpeed", "speed")],
            "calls": [("rCalib", "SetOffset")],
        }

        exported_again = tmp_path / "SensorSwc.reexport.arxml"
        export_component(a_root, "SensorSwc", str(exported_again))
        project_b = _init_project(tmp_path, "projB")
        import_component(str(exported_again), str(project_b / "model"))

        assert _access_entries(str(project_b / "model")) == _access_entries(a_root)

    def test_a_second_import_of_the_marked_export_is_a_no_op(self, tmp_path: Path) -> None:
        """A marked export's lists ARE compared: equal entries give `unchanged`, file untouched."""
        source_root = _fixture_copy_with_access_lists(tmp_path)
        export = tmp_path / "SensorSwc.arxml"
        export_component(source_root, "SensorSwc", str(export))
        assert 'GID="SWCRAFT"' in export.read_text(encoding="utf-8")
        project = _init_project(tmp_path, "proj")
        import_component(str(export), str(project / "model"))
        swc_file = next(iter(sorted((project / "model" / "swc").rglob("SensorSwc.yaml"), key=lambda p: p.as_posix())))
        before = swc_file.read_bytes()

        report = import_component(str(export), str(project / "model"))

        assert report.swc_action == "unchanged"
        assert report.access_list_regime == "none"
        assert swc_file.read_bytes() == before


#: One stated name of each kind, put on ``SensorSwc`` over the access-list copy; every one
#: differs from the name the writer derives.
_STATED_NAMES: dict[str, str] = {
    "behavior_name": "SensorBehaviour",
    "events/0": "SensorTick",
    "events/1": "SensorStartup",
    "reads/0": "ReadVehicleSpeed",
    "writes/0": "WriteVehicleSpeed",
    "calls/0": "CallSetOffset",
}


def _fixture_copy_with_stated_names(tmp_path: Path) -> str:
    """:func:`_fixture_copy_with_access_lists` with every name of :data:`_STATED_NAMES` stated."""
    model_root = _fixture_copy_with_access_lists(tmp_path)
    swc_file = next(iter(sorted((Path(model_root) / "swc").rglob("SensorSwc.yaml"), key=lambda p: p.as_posix())))
    authored = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    authored["behavior_name"] = _STATED_NAMES["behavior_name"]
    runnable = authored["runnables"][0]
    for where, name in _STATED_NAMES.items():
        if "/" in where:
            collection, index = where.split("/")
            runnable[collection][int(index)]["name"] = name
    _dump(swc_file, authored)
    return model_root


def _stated_names(model_root: str, swc_name: str = "SensorSwc") -> dict[str, str]:
    """Every name *swc_name* states, keyed as :data:`_STATED_NAMES` is."""
    swc = next(s for s in read_swc_model(model_root) if s.name == swc_name)
    runnable = swc.runnables[0]
    stated = {"behavior_name": swc.behavior_name} if swc.behavior_name is not None else {}
    for collection, entries in (
        ("events", runnable.events),
        ("reads", runnable.reads),
        ("writes", runnable.writes),
        ("calls", runnable.calls),
    ):
        stated.update({f"{collection}/{index}": entry.name for index, entry in enumerate(entries) if entry.name})
    return stated


class TestStatedNamesSurviveTwoExchanges:
    """Stated names survive two exchanges, with a byte-identical re-export.

    **What it does not reach:** a stated name equal to the derived one (it reads back as none --
    ``test_component_import.py``'s limit case), a name the settling pass declines
    (``test_stated_names_import.py``) and the closure check on a stated name
    (``test_component_export.py``).
    """

    def test_every_stated_name_survives_two_exchanges(self, tmp_path: Path) -> None:
        source_root = _fixture_copy_with_stated_names(tmp_path)
        assert _stated_names(source_root) == _STATED_NAMES
        exported_once = tmp_path / "SensorSwc.arxml"
        export_component(source_root, "SensorSwc", str(exported_once))
        written = _short_names(exported_once)
        assert set(_STATED_NAMES.values()) <= written
        assert not {"SensorSwc_InternalBehavior", "TMT_SensorMain", "IT_SensorMain"} & written
        project_a = _init_project(tmp_path, "projA")

        report_a = import_component(str(exported_once), str(project_a / "model"))

        assert report_a.swc_action == "created"
        assert not [line for line in report_a.not_carried_lines if "SWCraft names it itself" in line]
        a_root = str(project_a / "model")
        assert _stated_names(a_root) == _STATED_NAMES

        exported_again = tmp_path / "SensorSwc.reexport.arxml"
        export_component(a_root, "SensorSwc", str(exported_again))
        assert exported_again.read_bytes() == exported_once.read_bytes()
        project_b = _init_project(tmp_path, "projB")
        import_component(str(exported_again), str(project_b / "model"))

        assert _stated_names(str(project_b / "model")) == _STATED_NAMES

    def test_a_second_import_into_the_stating_project_is_a_no_op(self, tmp_path: Path) -> None:
        source_root = _fixture_copy_with_stated_names(tmp_path)
        export = tmp_path / "SensorSwc.arxml"
        export_component(source_root, "SensorSwc", str(export))
        project = _init_project(tmp_path, "proj")
        import_component(str(export), str(project / "model"))
        swc_file = next(iter(sorted((project / "model" / "swc").rglob("SensorSwc.yaml"), key=lambda p: p.as_posix())))
        before = swc_file.read_bytes()

        report = import_component(str(export), str(project / "model"))

        assert report.swc_action == "unchanged"
        assert swc_file.read_bytes() == before
