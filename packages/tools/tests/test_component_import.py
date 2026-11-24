"""Tests for ``component_exchange.import_``: importing a component export into a project.

A component-scoped ARXML export goes through the ARXML import pipeline (``read_arxml`` +
``map_data_types`` / ``map_interfaces`` / ``map_swcs``), with:

- input shape validation first (exactly one SWC, no composition/connector content);
- any mapper skip touching the imported component aborting the import, while a skip of
  out-of-scope content (``DEFERRED_ELEMENT_TAGS``) is report-only;
- the target project's own name ambiguity aborting before any create/reuse/conflict decision;
- identical shared interfaces/data types reused silently, and ``dry_run`` writing nothing;
- the round trip Pydantic-equal to the source closure;
- a divergent shared interface/data type rejecting the whole import, ``update=True`` included;
- a divergent existing SWC rejected by default and replaced only with ``update=True``, writing
  only that SWC's own file, with a semantic diff preview in the report;
- the staged-write-then-rename apply rolling back completely on a mid-loop rename failure.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    ACCESS_POINTS_MODEL_ROOT,
    REPO_ROOT,
    model_element_path,
    model_element_storage_name,
    relabel_arxml_version,
    write_model_element,
)
from enum_category_support import rewrite_enum_type_category
from external_stubs_support import interface_stub, type_stub
from fidelity_strip_support import parse, restore_port_api_options, serialize
from project_support import files_of, init_project
from swcraft_tools.arxml_import import map_data_types, map_interfaces, map_swcs
from swcraft_tools.arxml_import.pipeline import run_import
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.capability_markers import ACCESS_POINTS, DATA_TYPES
from swcraft_tools.cli import main
from swcraft_tools.cli.init_command import _default_file_plan
from swcraft_tools.component_exchange import (
    AmbiguousTargetProjectError,
    ClosureValidationError,
    ComponentExchangeError,
    ComponentImportConflictError,
    ComponentImportReport,
    InvalidComponentArxmlError,
    LossyComponentImportError,
    ReservedNameError,
    export_all_components,
    export_component,
    import_component,
    plan_component_import,
)
from swcraft_tools.component_exchange.import_ import _planned_create_path
from swcraft_tools.component_exchange.name_policies import (
    access_point_collisions,
    illegal_identifiers,
    reserved_name_violations,
)
from swcraft_tools.data_description import read_data_description
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH, REASON_LENGTH, REASON_PATTERN
from swcraft_tools.reader import read_data_types, read_interfaces, read_layer, read_swc_model
from swcraft_tools.schema import (
    ApplicationDataType,
    BaseDataType,
    Conversion,
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    EnumDataType,
    EnumLiteral,
    Port,
    RecordDataType,
    RecordField,
    ReferenceDataType,
    Runnable,
    SRInterface,
    SWComponent,
    TimingEvent,
    ValueDataType,
    ValueRange,
)
from swcraft_tools.validator import (
    check_cross_layer_consistency,
    check_name_uniqueness,
    check_unknown_keys,
    data_type_layer,
)
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_generation import (
    CALL_POINT_PREFIX,
    IMPLEMENTATION_SUFFIX,
    INTERNAL_BEHAVIOR_SUFFIX,
    READ_ACCESS_PREFIX,
    TIMING_EVENT_PREFIX,
    WRITE_ACCESS_PREFIX,
    access_point_name,
    init_constant_name,
)
from windows_newline_support import assert_no_crlf, windows_newline_translation

#: The element two tests place under ANOTHER element's file name, to exercise
#: the storage collision alone. It must be outside the export's closure: an
#: occupant the import also brings is reused or aborts on a divergence, neither
#: of which is the collision; a self-contained base type no closure reaches.
_UNRELATED_OCCUPANT = {
    "name": "Unrelated_T",
    "kind": "base_type",
    "size": 16,
    "encoding": "NONE",
    "native_declaration": "uint16",
}

# Fixture model: TargetSwc exercises both interface kinds and six data type kinds: base type, value type,
# application type, enum (via a record field; a direct enum-typed element is TestDirectEnumInterface's), record
# and array. OtherSwc/IOther/Other_T prove unrelated content never leaks into the exchange.


def _dump(path: Path, data: dict[str, Any]) -> None:
    """Write *data* as the source model's own authored YAML, LF on every host.

    ``newline="\\n"`` matters: with CRLF on Windows the digest test that hashes these
    files disagreed with the LF the importer writes. A ``data_types/<stem>.yaml`` path for
    a typed payload is routed to its kind's layer, keeping the stem.
    """
    if path.parent.name == "data_types" and data.get("kind") is not None:
        path = model_element_path(path.parent.parent, path.stem, kind=data["kind"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))


def _both_type_layers(model_root: str) -> list[Any]:
    """Every element of the base, application and implementation type layers."""
    return [
        *read_layer(model_root, "base_types"),
        *read_layer(model_root, "application_types"),
        *read_data_types(model_root),
    ]


def _build_source_model(tmp_path: Path, *, speed_init: object | None = None) -> str:
    """Build the source closure; ``speed_init``, when given, goes on ``ISpeed``'s first data element."""
    model_root = tmp_path / "source_model"
    dt = model_root / "data_types"
    # Each value type rests on a `{name}_base` base type, which declares its C
    # type name: the writer never derives one.
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
    # A composite member names the application (physical) type; the mapping set
    # carries the implementation type each stores as. `Distance_T` declares no
    # physics, `Velocity_T` all three optional fields, so both shapes are in the closure.
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
                {"name": "speed", "implementation_type": "Speed_T"}
                if speed_init is None
                else {"name": "speed", "implementation_type": "Speed_T", "init": speed_init},
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
            "runnables": [
                {
                    "name": "TargetRunnable",
                    "min_start_interval": 0.005,
                    "events": [{"type": "timing", "period": 0.01}, {"type": "init"}],
                }
            ],
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


def _export_target(tmp_path: Path) -> tuple[str, Path]:
    """Build the source model and export TargetSwc; return (source_model_root, arxml_path)."""
    source_root = _build_source_model(tmp_path)
    arxml = tmp_path / "TargetSwc.arxml"
    export_component(source_root, "TargetSwc", str(arxml))
    return source_root, arxml


#: The value :func:`_export_target_with_init` puts on ``ISpeed.speed``: exactly
#: representable in binary, so the ARXML text, the read value and the written
#: YAML are all ``1.5`` and the assertions are about the exchange, not float formatting.
_SPEED_INIT = 1.5


def _export_target_with_init(tmp_path: Path, value: object = _SPEED_INIT) -> tuple[str, Path]:
    """:func:`_export_target` over a source whose ``ISpeed.speed`` declares *value* as its init."""
    source_root = _build_source_model(tmp_path, speed_init=value)
    arxml = tmp_path / "TargetSwcWithInit.arxml"
    export_component(source_root, "TargetSwc", str(arxml))
    return source_root, arxml


def _renamed_export(tmp_path: Path, old: str, new: str) -> Path:
    """The TargetSwc export with *old* replaced by *new* throughout, references included."""
    _, arxml = _export_target(tmp_path)
    tampered = tmp_path / "tampered.arxml"
    tampered.write_text(arxml.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    return tampered


def _dir_snapshot(root: Path) -> set[str]:
    """Every directory under ``root``, POSIX-spelled: a rolled-back import leaves no new one."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir()}


_CLOSURE_TYPE_NAMES = [
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

#: The closure's base types -- the two ``{name}_base`` files ``_build_source_model`` writes.
_CLOSURE_BASE_TYPE_NAMES = frozenset({"float32_base", "uint8_base"})


#: The closure's application types (layer ``application_types``).
_CLOSURE_APPLICATION_TYPE_NAMES = frozenset({"Distance_T", "Gear_T", "Pose_T", "Samples_T", "Velocity_T"})


def _closure_type_layer(name: str) -> str:
    """The layer closure type *name* is stored in: base, application or implementation types."""
    if name in _CLOSURE_BASE_TYPE_NAMES:
        return "base_types"
    if name in _CLOSURE_APPLICATION_TYPE_NAMES:
        return "application_types"
    return "implementation_types"


def _closure_type_storage_name(name: str) -> str:
    """The model-root-relative file of closure type *name*, in the layer :func:`_closure_type_layer` names."""
    return model_element_storage_name(name, layer=_closure_type_layer(name))


#: The same names in the order an import report lists and plans them: the export's
#: source paths compared ordinally, which begin with the layer package
#: (``/DataTypes/Application/``, ``/DataTypes/Base/``, ``/DataTypes/Implementation/``).
_CLOSURE_TYPES_IN_SOURCE_ORDER = [
    "Distance_T",
    "Gear_T",
    "Pose_T",
    "Samples_T",
    "Velocity_T",
    "float32_base",
    "uint8_base",
    "Speed_T",
    "uint8",
]


# Round trip: export -> fresh-project import -> Pydantic-equal model


class TestRoundTrip:
    def test_round_trip_into_fresh_project_is_pydantic_equal(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        target_root = str(project / "model")
        # the SWC round-trips Pydantic-equal (ports, runnables, events)
        source_swcs = {s.name: s for s in read_swc_model(source_root)}
        target_swcs = read_swc_model(target_root)
        assert [s.name for s in target_swcs] == ["TargetSwc"]
        assert target_swcs[0] == source_swcs["TargetSwc"]
        # both interfaces round-trip Pydantic-equal — and nothing else came along
        source_ifaces = {i.name: i for i in read_interfaces(source_root)}
        target_ifaces = {i.name: i for i in read_interfaces(target_root)}
        assert sorted(target_ifaces) == ["ICalib", "ISpeed"]
        assert target_ifaces["ISpeed"] == source_ifaces["ISpeed"]
        assert target_ifaces["ICalib"] == source_ifaces["ICalib"]
        # the six fixture kinds (base type, value type, application type, enum,
        # record, array) round-trip Pydantic-equal; the reference type's kind is
        # TestAnImplementationTypeReadsBackAsTheKindItsCategoryDeclares'
        source_types = {d.name: d for d in _both_type_layers(source_root)}
        target_types = {d.name: d for d in _both_type_layers(target_root)}
        assert sorted(target_types) == _CLOSURE_TYPE_NAMES
        for name in _CLOSURE_TYPE_NAMES:
            assert target_types[name] == source_types[name], name

        assert isinstance(report, ComponentImportReport)
        assert report.swc == "TargetSwc"
        assert report.swc_action == "created"
        assert report.created_interfaces == ["ICalib", "ISpeed"]
        assert report.created_data_types == _CLOSURE_TYPES_IN_SOURCE_ORDER
        assert report.reused_interfaces == []
        assert report.reused_data_types == []
        # the closure's types + 2 interfaces + 1 SWC
        assert len(report.written_paths) == len(_CLOSURE_TYPE_NAMES) + 3
        for written in report.written_paths:
            assert Path(written).is_file()
        rendered = report.render()
        assert "TargetSwc" in rendered
        # the renderer's conditional WROTE section lists every written path
        assert f"WROTE {len(_CLOSURE_TYPE_NAMES) + 3} file(s)" in rendered
        for written in report.written_paths:
            assert f"  - {written}" in rendered

    def test_every_written_element_is_lf_even_where_the_platform_would_translate(self, tmp_path: Path) -> None:
        """The model YAML the import writes is LF everywhere, the staged ``.tmp`` write included.

        On Linux ``newline=None`` writes LF anyway, so :func:`windows_newline_translation`
        supplies a translating platform at the ``io.open`` boundary.
        """
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")

        with windows_newline_translation():
            report = import_component(str(arxml), str(project / "model"))

        assert report.written_paths, "the assertion is vacuous unless the import wrote something"
        for written in report.written_paths:
            path = Path(written)
            payload = assert_no_crlf(path, what=f"the written element {path.name}")
            assert payload.endswith(b"\n"), f"{path.name} does not end in a newline"

    def test_new_swc_file_lands_under_the_components_group(self, tmp_path: Path) -> None:
        """A newly created SWC is written to the fixed group ``swc/components/<name>.yaml``."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")

        import_component(str(arxml), str(project / "model"))

        assert (project / "model" / "swc" / "components" / "TargetSwc.yaml").is_file()


#: The committed models whose every component exports whole: the exchange's own
#: model, the inter-runnable variables, the exclusive areas, the operation
#: events and the access points. ``data_events`` is not one: its component
#: reaches external stubs, which the exporter refuses.
_EXPORTING_MODELS = (
    "component_exchange",
    "inter_runnable_variables",
    "exclusive_areas",
    "operation_events",
    "access_points",
)


class TestTheImportWritesNoUndeclaredKey:
    """`import component` writes no undeclared key for any component of the exporting models.

    Does not reach: a component shape none of these models holds, and real tool exports.
    """

    @pytest.mark.parametrize("model", _EXPORTING_MODELS)
    def test_no_written_file_holds_an_undeclared_key(self, tmp_path: Path, model: str) -> None:
        exported = tmp_path / "exported"
        source = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / model / "model"
        reports = export_all_components(str(source), str(exported))
        assert reports
        assert [report.error for report in reports if report.status == "failed"] == []
        for arxml in sorted(exported.glob("*.arxml"), key=lambda path: path.as_posix()):
            project = init_project(tmp_path / f"proj_{arxml.stem}")
            report = import_component(str(arxml), str(project / "model"))
            assert report.written_paths, arxml.name
            assert check_unknown_keys(str(project / "model")).issues == [], arxml.name


# Input shape: exactly one SWC, no composition/connector content


class TestInputShape:
    def test_zero_swc_arxml_is_rejected(self, tmp_path: Path) -> None:
        source_root = _build_source_model(tmp_path)
        # a model with no SWCs at all still generates valid ARXML
        for swc_file in (Path(source_root) / "swc").rglob("*.yaml"):
            swc_file.unlink()
        arxml = tmp_path / "no_swc.arxml"
        generate_arxml(source_root, str(arxml))
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(InvalidComponentArxmlError, match="exactly one SWC"):
            import_component(str(arxml), str(project / "model"))

        assert files_of(project / "model") == before

    def test_multi_swc_arxml_is_rejected(self, tmp_path: Path) -> None:
        """A whole-model export (two SWCs) is caught by the single-SWC shape check."""
        source_root = _build_source_model(tmp_path)
        arxml = tmp_path / "whole_model.arxml"
        generate_arxml(source_root, str(arxml))
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(InvalidComponentArxmlError, match="exactly one SWC"):
            import_component(str(arxml), str(project / "model"))

        assert files_of(project / "model") == before

    def test_composition_connector_content_is_rejected(self, tmp_path: Path) -> None:
        """Composition/connector content is integration-owned and rejected outright."""
        source_root = _build_source_model(tmp_path)
        # keep exactly one SWC, self-connected, so ONLY the composition
        # check can be what rejects the file
        (Path(source_root) / "swc" / "body" / "OtherSwc.yaml").unlink()
        _dump(
            Path(source_root) / "swc" / "body" / "TargetSwc.yaml",
            {
                "name": "TargetSwc",
                "ports": [
                    {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
                    {"name": "rSpeed", "direction": "require", "interface": "ISpeed"},
                ],
                "handle_termination_and_restart": "no_support",
            },
        )
        _dump(
            Path(source_root) / "connectors" / "selfLoop.yaml",
            {
                "name": "selfLoop",
                "from": {"swc": "TargetSwc", "port": "pSpeed"},
                "to": {"swc": "TargetSwc", "port": "rSpeed"},
            },
        )
        # A connector belongs to a composition, and one that names none belongs
        # to the root, so the source needs a root declaring the self-connected
        # component before it can be generated at all.
        _dump(
            Path(source_root) / "compositions" / "Root.yaml",
            {
                "name": "Root",
                "title": "Root composition",
                "root": True,
                "members": [{"name": "TargetSwc", "type": "TargetSwc"}],
            },
        )
        arxml = tmp_path / "with_composition.arxml"
        generate_arxml(source_root, str(arxml))
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(InvalidComponentArxmlError, match="composition/connector"):
            import_component(str(arxml), str(project / "model"))

        assert files_of(project / "model") == before


# Reserved names and identifier conformance


class TestReservedNames:
    """An import may not produce a model the writer refuses to emit.

    The import reaches the model through ``map_swcs()``/``map_data_types()``, never
    through ``create_swc``'s guard; the component-side reserved set is
    ``{A}_Implementation`` beside a declared ``A``.
    """

    def test_an_implementation_suffixed_name_colliding_with_the_target_is_refused(self, tmp_path: Path) -> None:
        """Only visible on the **merged** model: legal in isolation, reserved beside ``Base``."""
        tampered = _renamed_export(tmp_path, "TargetSwc", f"Base{IMPLEMENTATION_SUFFIX}")
        project = init_project(tmp_path / "proj")
        _dump(
            project / "model" / "swc" / "existing" / "Base.yaml",
            {"name": "Base", "type": "application", "ports": [], "runnables": []},
        )
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="SWC-IMPLEMENTATION"):
            import_component(str(tampered), str(project / "model"))

        assert files_of(project / "model") == before

    def test_an_implementation_suffixed_name_alone_is_allowed(self, tmp_path: Path) -> None:
        """``_Implementation`` is not a reserved suffix on its own."""
        tampered = _renamed_export(tmp_path, "TargetSwc", f"Unrelated{IMPLEMENTATION_SUFFIX}")
        project = init_project(tmp_path / "proj")

        report = import_component(str(tampered), str(project / "model"))

        assert report.swc_action == "created"

    def test_the_former_fixed_name_is_no_longer_reserved(self, tmp_path: Path) -> None:
        """A declared type named ``DataTypeMappingSet`` imports: the writer owns no fixed data type name.

        A mapping set is a declared element of its own layer, so the name collides with nothing.
        """
        source_root = _build_source_model(tmp_path)
        arxml = tmp_path / "OtherSwc.arxml"
        export_component(source_root, "OtherSwc", str(arxml))
        tampered = tmp_path / "tampered.arxml"
        tampered.write_text(
            arxml.read_text(encoding="utf-8").replace("Other_T", "DataTypeMappingSet"), encoding="utf-8"
        )
        project = init_project(tmp_path / "proj")

        report = import_component(str(tampered), str(project / "model"))

        assert "DataTypeMappingSet" in report.created_data_types

    def test_a_composition_of_the_incoming_components_name_is_refused(self, tmp_path: Path) -> None:
        """A composition named like the incoming component is refused: one `SHORT-NAME` namespace.

        The import reads `compositions/`; otherwise it writes a project `check-model` and `generate arxml` refuse.
        """
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        _dump(
            project / "model" / "compositions" / "TargetSwc.yaml",
            {"name": "TargetSwc", "title": "A composition claiming the component's name"},
        )
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="TargetSwc"):
            import_component(str(arxml), str(project / "model"))

        assert files_of(project / "model") == before

    def test_a_composition_named_after_the_incoming_components_implementation_is_refused(self, tmp_path: Path) -> None:
        """The other half of the same namespace: the generated name, not the declared one."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        colliding = f"TargetSwc{IMPLEMENTATION_SUFFIX}"
        _dump(
            project / "model" / "compositions" / f"{colliding}.yaml",
            {"name": colliding, "title": "A composition claiming the generated implementation name"},
        )
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="SWC-IMPLEMENTATION"):
            import_component(str(arxml), str(project / "model"))

        assert files_of(project / "model") == before

    def test_the_refused_composition_model_really_is_one_the_writer_rejects(self, tmp_path: Path) -> None:
        """The claim behind the two refusals above, on the writer itself."""
        source_root = _build_source_model(tmp_path)
        _dump(
            Path(source_root) / "compositions" / "TargetSwc.yaml",
            {"name": "TargetSwc", "title": "A composition claiming the component's name"},
        )
        with pytest.raises(ValueError, match="Colliding /ComponentTypes"):
            generate_arxml(source_root, str(tmp_path / "never-written.arxml"))
        assert not (tmp_path / "never-written.arxml").exists()

    def test_an_external_stubs_implementation_name_is_free(self, tmp_path: Path) -> None:
        """A stub's `{name}_Implementation` is free: a stub emits no `SWC-IMPLEMENTATION`.

        Refusing it would make the guard stricter than the writer.
        """
        tampered = _renamed_export(tmp_path, "TargetSwc", f"Stub{IMPLEMENTATION_SUFFIX}")
        project = init_project(tmp_path / "proj")
        _dump(
            project / "model" / "swc" / "external" / "Stub.yaml",
            {
                "name": "Stub",
                "type": "application",
                "external": True,
                "external_ref": "/Other/Project/Rte/Stub",
                "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                "ports": [],
                "runnables": [],
            },
        )

        report = import_component(str(tampered), str(project / "model"))

        assert report.swc_action == "created"

    def test_the_refused_model_really_is_one_the_writer_rejects(self, tmp_path: Path) -> None:
        """The claim behind the refusal, on the writer itself.

        A component named ``{A}_Implementation`` beside ``A`` shares a ``SHORT-NAME``
        with the ``SWC-IMPLEMENTATION`` the writer generates for ``A``.
        """
        source_root = _build_source_model(tmp_path)
        swc_file = next((Path(source_root) / "swc").rglob("TargetSwc.yaml"))
        collides = f"OtherSwc{IMPLEMENTATION_SUFFIX}"
        swc_file.write_text(swc_file.read_text(encoding="utf-8").replace("TargetSwc", collides), encoding="utf-8")
        with pytest.raises(ValueError, match=collides):
            generate_arxml(source_root, str(tmp_path / "never-written.arxml"))
        assert not (tmp_path / "never-written.arxml").exists()


class TestReservedDataTypeNamesInBothDirections:
    """Reserved data type names are checked in both directions over the merged model.

    Iterating the imported names only would miss a target holding the derived name while the import brings the base
    name.
    """

    @staticmethod
    def _target_with(tmp_path: Path, data_type: dict[str, Any]) -> Path:
        project = init_project(tmp_path / "proj")
        _dump(model_element_path(project / "model", data_type["name"], kind="application_type"), data_type)
        return project

    def test_the_target_holding_the_suffixed_name_is_refused(self, tmp_path: Path) -> None:
        """Reverse direction: the import brings the enum, the target holds ``{enum}_CompuMethod``.

        The form collides only with a type of the enum's own package, so the occupant is
        an application type.
        """
        _, arxml = _export_target(tmp_path)
        project = self._target_with(tmp_path, {"name": "Gear_T_CompuMethod", "kind": "application_type"})
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="Gear_T_CompuMethod"):
            import_component(str(arxml), str(project / "model"))

        assert files_of(project / "model") == before

    def test_the_model_the_reverse_direction_refuses_really_is_ungenerable(self, tmp_path: Path) -> None:
        """The claim behind the refusal: what the import would have produced is ungenerable.

        The occupant is an application type; a base type would be refused only for its
        missing ``native_declaration``.
        """
        source_root = _build_source_model(tmp_path)
        write_model_element(Path(source_root), {"name": "Gear_T_CompuMethod", "kind": "application_type"})

        with pytest.raises(ValueError, match=r"\['Gear_T_CompuMethod'\] collide with auto-generated"):
            generate_arxml(source_root, str(tmp_path / "never-written.arxml"))

    def test_a_suffixed_name_over_a_composite_base_is_allowed(self, tmp_path: Path) -> None:
        """Kind-scoped, like ``_check_data_type_names``: ``Pose_T_CompuMethod`` beside a record is free.

        The writer emits a ``COMPU-METHOD`` only for an enum.
        """
        source_root = _build_source_model(tmp_path)
        write_model_element(Path(source_root), {"name": "Pose_T_CompuMethod", "kind": "application_type"})
        # the writer's own verdict on the same merged model
        generate_arxml(source_root, str(tmp_path / "generated.arxml"))

        violations = reserved_name_violations(
            SWComponent(name="Unrelated", ports=[], runnables=[]),
            [ApplicationDataType(name="Pose_T_CompuMethod", kind="application_type")],
            {},
            set(),
            # Keyed `(layer, name)` over all three layers, as `import_component` reads its target.
            {(data_type_layer(d), d.name): d for d in read_data_description(source_root).data_types},
            [],
            {},
        )

        assert violations == []

    def test_a_pair_wholly_inside_the_target_is_not_blamed_on_the_import(self, tmp_path: Path) -> None:
        """An already-broken target must not make every component unimportable."""
        enum = EnumDataType(name="Mode_T", kind="enum", literals=[EnumLiteral(name="OFF", value=0)])
        derived = ApplicationDataType(name="Mode_T_CompuMethod", kind="application_type")

        violations = reserved_name_violations(
            SWComponent(name="Unrelated", ports=[], runnables=[]),
            [],
            {},
            set(),
            {("application_types", enum.name): enum, ("application_types", derived.name): derived},
            [],
            {},
        )

        assert violations == []


class TestIdentifierViolations:
    """``illegal_identifiers``: every declared or generated ``SHORT-NAME`` an import writes is legal.

    A legal AUTOSAR ``Identifier`` is ``[a-zA-Z][a-zA-Z0-9_]*``, at most 128 characters.
    Driven on the mapped objects so each nested offender (a CS argument, a record field)
    is reachable one at a time.
    """

    _LONG = "A" * 124  # 124 + '_impl' == 129: one over the limit

    @staticmethod
    def _swc(name: str = "TargetSwc", **overrides: Any) -> SWComponent:
        return SWComponent(name=name, **{"ports": [], "runnables": [], **overrides})

    def test_a_conforming_closure_reports_nothing(self, tmp_path: Path) -> None:
        """The exported closure, mapped exactly as the import maps it."""
        _, arxml = _export_target(tmp_path)
        model = read_arxml(str(arxml))
        forms: list = []
        data_types, registry, _skips = map_data_types(model, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
        swcs, _ = map_swcs(model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})

        assert illegal_identifiers(swcs[0].swc, interfaces, data_types) == []

    def test_every_nested_name_is_covered_and_all_are_reported_at_once(self) -> None:
        """One call, one full list — the sender fixes their export in one pass."""
        swc = self._swc(
            "My SWC",
            ports=[Port(name="p Speed", direction="provide", interface="ISpeed")],
            runnables=[Runnable(name="9Run", events=[])],
        )
        interfaces: list[Any] = [
            SRInterface(
                name="I Speed",
                kind="sender_receiver",
                data_elements=[DataElement(name="sp eed", implementation_type="uint8")],
            ),
            CSInterface(
                name="ICalib",
                kind="client_server",
                operations=[
                    CSOperation(
                        name="Set Offset",
                        arguments=[CSArgument(name="1raw", direction="in", implementation_type="uint8")],
                    )
                ],
            ),
        ]
        data_types: list[Any] = [
            BaseDataType(name="ui nt8", kind="base_type", size=8, encoding="NONE"),
            EnumDataType(name="Gear_T", kind="enum", literals=[EnumLiteral(name="PARK 1", value=0)]),
            RecordDataType(name="Pose_T", kind="record", fields=[RecordField(name="x-coord", type="uint8")]),
        ]

        violations = illegal_identifiers(swc, interfaces, data_types)

        offenders = [
            "My SWC",
            "p Speed",
            "9Run",
            "I Speed",
            "sp eed",
            "Set Offset",
            "1raw",
            "ui nt8",
            "PARK 1",
            "x-coord",
        ]
        assert len(violations) == len(offenders)
        for offender in offenders:
            assert any(f"'{offender}'" in line for line in violations), offender
        assert all(REASON_PATTERN in line for line in violations)

    def test_a_declared_offender_is_reported_once_not_once_per_generated_form(self) -> None:
        """A primitive reserves three generated forms; a bad name is still one root cause."""
        bad = BaseDataType(name="ui nt8", kind="base_type", size=8, encoding="NONE")

        violations = illegal_identifiers(self._swc(), [], [bad])

        assert len(violations) == 1

    def test_a_legal_swc_name_with_an_illegal_internal_behavior_form_is_reported(self) -> None:
        """``_Implementation`` fits at 112 characters and ``_InternalBehavior`` (17) does not.

        The declared name does not predict it.
        """
        name = "S" * 112
        assert len(f"{name}{IMPLEMENTATION_SUFFIX}") <= MAX_IDENTIFIER_LENGTH, "precondition"

        violations = illegal_identifiers(self._swc(name), [], [])

        assert len(violations) == 1
        assert f"{name}{INTERNAL_BEHAVIOR_SUFFIX}" in violations[0]
        assert REASON_LENGTH in violations[0]

    def test_event_names_are_checked_after_the_writers_retroactive_counter(self) -> None:
        """``TMT_{runnable}`` fits; ``TMT_{runnable}_0`` does not.

        Two timing events on one runnable make the writer rename the first one
        retroactively; checking ``TMT_{runnable}`` would report nothing.
        """
        name = "R" * 124  # TMT_ + 124 == 128 exactly
        runnable = Runnable(
            name=name, events=[TimingEvent(type="timing", period=0.01), TimingEvent(type="timing", period=0.02)]
        )
        assert len(f"{TIMING_EVENT_PREFIX}{name}") == MAX_IDENTIFIER_LENGTH, "precondition"

        violations = illegal_identifiers(self._swc(runnables=[runnable]), [], [])

        assert len(violations) == 2
        assert f"{TIMING_EVENT_PREFIX}{name}_0" in violations[0]
        assert f"{TIMING_EVENT_PREFIX}{name}_1" in violations[1]

    def test_the_generated_data_type_forms_are_kind_scoped(self) -> None:
        """The generated data type forms follow the writer's own scope.

        ``_CompuMethod`` is scoped to the enum kind, the three application forms to the
        fields an application type declares, so naming all three unconditionally would be
        stricter than the writer. A value or reference type mints nothing.
        """
        primitive = BaseDataType(name=self._LONG, kind="base_type", size=8, encoding="NONE")
        enum = EnumDataType(name=self._LONG, kind="enum", literals=[EnumLiteral(name="PARK", value=0)])
        alias = ValueDataType(name=self._LONG, kind="value_type", base="uint8")
        reference = ReferenceDataType(name=self._LONG, kind="reference_type", base="uint8")
        record = RecordDataType(name=self._LONG, kind="record", fields=[RecordField(name="x", type="uint8")])
        bare_application = ApplicationDataType(name=self._LONG, kind="application_type")
        full_application = ApplicationDataType(
            name=self._LONG,
            kind="application_type",
            unit="km/h",
            conversion=Conversion(factor=0.1, offset=0.0),
            range=ValueRange(min=0.0, max=400.0),
        )

        def _forms(dt: Any) -> list[str]:
            return [
                suffix
                for suffix in ("_impl", "_CompuMethod", "_AppView", "_Unit", "_Compu", "_Range")
                if _reported(dt, suffix)
            ]

        def _reported(dt: Any, suffix: str) -> bool:
            return any(f"{self._LONG}{suffix}'" in line for line in illegal_identifiers(self._swc(), [], [dt]))

        assert _forms(primitive) == []
        assert _forms(enum) == ["_CompuMethod"]
        assert _forms(alias) == []
        assert _forms(reference) == []
        assert _forms(record) == []
        assert _forms(bare_application) == []
        assert _forms(full_application) == ["_Unit", "_Compu", "_Range"]

    def test_a_registry_renamed_type_is_reported_under_the_name_the_export_declared(self) -> None:
        """A type ``map_data_types()`` renamed is reported under the name the export declared.

        Two same-named types in two packages leave the loser as ``{bare}_app``
        (``PathRegistry._reserve()``); this message precedes the rename's refusal, so
        ``PathRegistry.source_names`` maps it back and the emitted name is named as context.
        """
        renamed = BaseDataType(name="My Type_app", kind="base_type", size=8, encoding="NONE")

        violations = illegal_identifiers(self._swc(), [], [renamed], {("base_types", "My Type_app"): "My Type"})

        assert violations == [
            f"Data type name 'My Type' (mapped to 'My Type_app') is not a legal AUTOSAR Identifier: {REASON_PATTERN}"
        ]


# The init constant `{I}_{E}_Init` on the component-import path, which never
# renames: its length is `illegal_identifiers`' (a joint budget over two
# declared names), both collision halves are `reserved_name_violations`', over
# the merged model.


def _sr_iface(name: str, element_name: str = "value", type_name: str = "uint8") -> SRInterface:
    return SRInterface(
        name=name, kind="sender_receiver", data_elements=[DataElement(name=element_name, implementation_type=type_name)]
    )


class TestTheInitConstantCollisionsFailClosed:
    """Both collision halves, over the merged model, in both directions.

    A pair wholly inside the target is never blamed on the import.
    """

    @staticmethod
    def _violations(*, interfaces: list[Any] | None = None, target: list[Any] | None = None) -> list[str]:
        return reserved_name_violations(
            SWComponent(name="Unrelated", ports=[], runnables=[]),
            [],
            {},
            set(),
            {},
            list(interfaces or []),
            {iface.name: iface for iface in (target or [])},
        )

    def test_the_import_brings_the_interface_named_like_a_target_pairs_constant(self) -> None:
        colliding = init_constant_name("ISpeed", "speed")

        violations = self._violations(interfaces=[_sr_iface(colliding, "x")], target=[_sr_iface("ISpeed", "speed")])

        assert len(violations) == 1, violations
        assert colliding in violations[0] and "CONSTANT-SPECIFICATION" in violations[0]

    def test_the_import_brings_the_pair_whose_constant_the_target_declares(self) -> None:
        colliding = init_constant_name("ISpeed", "speed")

        violations = self._violations(interfaces=[_sr_iface("ISpeed", "speed")], target=[_sr_iface(colliding, "x")])

        assert len(violations) == 1, violations
        assert colliding in violations[0]

    def test_a_client_server_interface_named_like_a_constant_is_refused_too(self) -> None:
        """A CS interface derives no constant and is still a sibling of one."""
        colliding = init_constant_name("ISpeed", "speed")
        cs = CSInterface(
            name=colliding,
            kind="client_server",
            operations=[
                CSOperation(name="Op", arguments=[CSArgument(name="a", direction="in", implementation_type="uint8")])
            ],
        )

        violations = self._violations(interfaces=[cs], target=[_sr_iface("ISpeed", "speed")])

        assert len(violations) == 1, violations

    def test_the_underscore_ambiguity_across_the_merge_is_refused(self) -> None:
        """The underscore ambiguity across the merge is refused.

        Interface ``A_B`` element ``C`` and interface ``A`` element ``B_C``
        derive one ``SHORT-NAME``; the merge is what makes them meet."""
        assert init_constant_name("A_B", "C") == init_constant_name("A", "B_C")

        violations = self._violations(interfaces=[_sr_iface("A_B", "C")], target=[_sr_iface("A", "B_C")])

        assert len(violations) == 1, violations
        assert init_constant_name("A_B", "C") in violations[0]
        assert "'A_B'" in violations[0] and "'B_C'" in violations[0]

    def test_a_pair_wholly_inside_the_target_is_not_blamed_on_the_import(self) -> None:
        colliding = init_constant_name("ISpeed", "speed")

        assert self._violations(target=[_sr_iface("ISpeed", "speed"), _sr_iface(colliding, "x")]) == []
        assert self._violations(target=[_sr_iface("A_B", "C"), _sr_iface("A", "B_C")]) == []

    def test_a_conforming_merge_reports_nothing(self) -> None:
        assert self._violations(interfaces=[_sr_iface("ISpeed", "speed")], target=[_sr_iface("IOther", "v")]) == []


class TestTheInitConstantJointBudgetOnTheImportPath:
    """The length half: ``{I}_{E}_Init`` spends six characters, leaving the two declared names 122."""

    @staticmethod
    def _swc() -> SWComponent:
        return SWComponent(name="TargetSwc", ports=[], runnables=[])

    def test_an_over_long_pair_is_reported(self) -> None:
        interface_name, element_name = "I" * 62, "E" * 61
        emitted = init_constant_name(interface_name, element_name)
        assert len(emitted) == MAX_IDENTIFIER_LENGTH + 1, "precondition"

        violations = illegal_identifiers(self._swc(), [_sr_iface(interface_name, element_name)], [])

        assert len(violations) == 1, violations
        assert emitted in violations[0]
        assert REASON_LENGTH in violations[0]

    def test_a_pair_on_the_joint_budget_is_accepted(self) -> None:
        interface_name, element_name = "I" * 61, "E" * 61
        assert len(init_constant_name(interface_name, element_name)) == MAX_IDENTIFIER_LENGTH, "precondition"

        assert illegal_identifiers(self._swc(), [_sr_iface(interface_name, element_name)], []) == []

    def test_the_pair_form_is_checked_only_when_both_declared_names_conform(self) -> None:
        """One bad declared name is one root cause, not two findings."""
        violations = illegal_identifiers(self._swc(), [_sr_iface("I" * 62, "E E")], [])

        assert len(violations) == 1, violations
        assert REASON_PATTERN in violations[0]

    def test_a_client_server_interface_derives_no_constant(self) -> None:
        cs = CSInterface(
            name="I" * 128,
            kind="client_server",
            operations=[
                CSOperation(name="Op", arguments=[CSArgument(name="a", direction="in", implementation_type="uint8")])
            ],
        )

        assert illegal_identifiers(self._swc(), [cs], []) == []


class TestTheInitConstantAbortsAComponentImportAtomically:
    """End to end through ``import_component``: refused, nothing written."""

    def test_a_declared_interface_named_like_a_constant_aborts(self, tmp_path: Path) -> None:
        tampered = _renamed_export(tmp_path, "ICalib", init_constant_name("ISpeed", "speed"))
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="CONSTANT-SPECIFICATION"):
            import_component(str(tampered), str(project / "model"))

        assert files_of(project / "model") == before

    def test_the_underscore_ambiguity_against_the_target_aborts(self, tmp_path: Path) -> None:
        """The underscore ambiguity against the target fails closed: this path never renames.

        ``import model`` writes such a project and leaves the refusal to ``check-model``;
        the two paths differ on the repair, never on the verdict.
        """
        _source_root, arxml = _export_target(tmp_path)
        tampered = tmp_path / "tampered.arxml"
        tampered.write_text(arxml.read_text(encoding="utf-8").replace("pose", "speed_x"), encoding="utf-8")
        project = init_project(tmp_path / "proj")
        write_model_element(project / "model", {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE"})
        # A data element names an implementation type, so a value type over the base type.
        write_model_element(project / "model", {"name": "uint8_v", "kind": "value_type", "base": "uint8"})
        _dump(
            project / "model" / "interfaces" / "ISpeed_speed.yaml",
            {
                "name": "ISpeed_speed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "x", "implementation_type": "uint8_v"}],
            },
        )
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="CONSTANT-SPECIFICATION") as raised:
            import_component(str(tampered), str(project / "model"))

        assert init_constant_name("ISpeed", "speed_x") in str(raised.value)
        assert files_of(project / "model") == before


# The three access-point forms on the component-import path: the length of
# `R_{port}_{element}`, `W_{port}_{element}` and `Call_{port}_{operation}` is
# `illegal_identifiers`' (joint budgets over two declared names), the collision
# `access_point_collisions`' -- a guard of its own, because an access point is no
# package-level name and the clash needs no target state.


def _access_swc(
    port_name: str,
    member_name: str,
    *,
    list_name: str = "reads",
    runnable_names: tuple[str, ...] = ("Run",),
    swc_name: str = "TargetSwc",
) -> SWComponent:
    """One component whose runnables each declare the same single access entry."""
    member_field = "operation" if list_name == "calls" else "element"
    return SWComponent(
        name=swc_name,
        ports=[
            Port(
                name=port_name,
                direction="provide" if list_name == "writes" else "require",
                interface="IAccess",
            )
        ],
        runnables=[
            Runnable(name=name, **{list_name: [{"port": port_name, member_field: member_name}]})
            for name in runnable_names
        ],
    )


def _access_pair(prefix: str, *, over: int = 0) -> tuple[str, str]:
    """A port name and a member name summing to *prefix*' joint budget plus *over*."""
    budget = MAX_IDENTIFIER_LENGTH - len(prefix) - 1 + over
    port_length = budget // 2
    return "P" * port_length, "M" * (budget - port_length)


#: Each access-point form with the list it derives from and its entry's member field.
_ACCESS_FORMS = [
    (READ_ACCESS_PREFIX, "reads", "element"),
    (WRITE_ACCESS_PREFIX, "writes", "element"),
    (CALL_POINT_PREFIX, "calls", "operation"),
]


class TestTheAccessPointJointBudgetsOnTheImportPath:
    """The length half: ``R_``/``W_`` leave a port and its member 125 characters, ``Call_`` 122.

    The two names live in different files (the port on the component, the member on
    the interface), so the message carries both.
    """

    @staticmethod
    def _violations(swc: SWComponent) -> list[str]:
        return illegal_identifiers(swc, [], [])

    @pytest.mark.parametrize("prefix, list_name, member_field", _ACCESS_FORMS, ids=lambda value: str(value))
    def test_an_over_budget_pair_is_reported(self, prefix: str, list_name: str, member_field: str) -> None:
        port_name, member_name = _access_pair(prefix, over=1)
        emitted = access_point_name(prefix, port_name, member_name)
        assert len(emitted) == MAX_IDENTIFIER_LENGTH + 1, "precondition"

        violations = self._violations(_access_swc(port_name, member_name, list_name=list_name))

        assert len(violations) == 1, violations
        assert emitted in violations[0]
        assert f"'{port_name}'" in violations[0] and f"'{member_name}'" in violations[0]
        assert member_field in violations[0]
        assert REASON_LENGTH in violations[0]

    @pytest.mark.parametrize("prefix, list_name, member_field", _ACCESS_FORMS, ids=lambda value: str(value))
    def test_a_pair_on_the_joint_budget_is_accepted(self, prefix: str, list_name: str, member_field: str) -> None:
        port_name, member_name = _access_pair(prefix)
        assert len(access_point_name(prefix, port_name, member_name)) == MAX_IDENTIFIER_LENGTH, "precondition"

        assert self._violations(_access_swc(port_name, member_name, list_name=list_name)) == []

    def test_the_pair_form_is_checked_only_when_both_declared_names_conform(self) -> None:
        """One bad declared name is one root cause; the port name is reported once, by the port walk."""
        port_name = "p Speed"
        violations = self._violations(_access_swc(port_name, "M" * 124))

        assert len(violations) == 1, violations
        assert REASON_PATTERN in violations[0]
        assert "generates the SHORT-NAME" not in violations[0]

    def test_a_runnable_declaring_no_access_derives_nothing(self) -> None:
        swc = SWComponent(name="TargetSwc", ports=[], runnables=[Runnable(name="R" * 128, events=[])])

        assert self._violations(swc) == []


class TestTheAccessPointCollisionFailsClosed:
    """The collision half: two different entries of one runnable deriving one ``SHORT-NAME``.

    ``access_point_name`` is not injective (port ``a_b`` element ``c`` and port ``a``
    element ``b_c`` both derive ``W_a_b_c``) and neither legal pair can be renamed, so
    the import fails closed. The scope is one runnable; no target state takes part.
    """

    #: Two entries of one runnable deriving one name.
    PAIRS = (("a_b", "c"), ("a", "b_c"))

    @classmethod
    def _colliding(cls, *, runnable_names: tuple[str, ...] = ("Run",)) -> SWComponent:
        return SWComponent(
            name="TargetSwc",
            ports=[Port(name=port, direction="provide", interface="IAccess") for port, _member in cls.PAIRS],
            runnables=[
                Runnable(name=name, writes=[{"port": port, "element": member} for port, member in cls.PAIRS])
                for name in runnable_names
            ],
        )

    def test_the_two_entries_really_derive_one_name(self) -> None:
        assert access_point_name(WRITE_ACCESS_PREFIX, "a_b", "c") == access_point_name(WRITE_ACCESS_PREFIX, "a", "b_c")

    def test_the_collision_is_reported_naming_both_entries_and_the_name(self) -> None:
        violations = access_point_collisions(self._colliding())

        assert len(violations) == 1, violations
        assert access_point_name(WRITE_ACCESS_PREFIX, "a_b", "c") in violations[0]
        for port, member in self.PAIRS:
            assert f"'{port}'" in violations[0] and f"'{member}'" in violations[0], violations[0]

    def test_the_same_pair_in_two_runnables_is_not_a_collision(self) -> None:
        one_entry = _access_swc("a_b", "c", list_name="writes", runnable_names=("One", "Two"))

        assert access_point_collisions(one_entry) == []

    def test_two_runnables_each_colliding_are_two_findings(self) -> None:
        assert len(access_point_collisions(self._colliding(runnable_names=("One", "Two")))) == 2

    def test_a_conforming_component_reports_nothing(self) -> None:
        assert access_point_collisions(_access_swc("pSpeed", "speed", list_name="writes")) == []

    def test_the_identifier_guard_is_not_the_one_that_finds_it(self) -> None:
        """The two guards divide: a collision is not an illegal ``Identifier``.

        The message a sender reads has to say which of the two it is."""
        assert illegal_identifiers(self._colliding(), [], []) == []


def _export_with_foreign_access(tmp_path: Path, ports: list[str], elements: list[str], *points: str) -> Path:
    """A ``TargetSwc`` export carrying send points no SWCraft writer would emit.

    The source gains the ports and ``ISpeed`` elements the points name, so every
    reference resolves, but no access list; the points are spliced in as text, the
    only way to build shapes the writer refuses to generate.
    """
    root = Path(_build_source_model(tmp_path))
    interface_file = root / "interfaces" / "ISpeed.yaml"
    interface = yaml.safe_load(interface_file.read_text(encoding="utf-8"))
    interface["data_elements"] += [{"name": name, "implementation_type": "uint8"} for name in elements]
    _dump(interface_file, interface)
    swc_file = root / "swc" / "body" / "TargetSwc.yaml"
    swc = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    swc["ports"] += [{"name": name, "direction": "provide", "interface": "ISpeed"} for name in ports]
    _dump(swc_file, swc)

    arxml = tmp_path / "TargetSwc.arxml"
    export_component(str(root), "TargetSwc", str(arxml))
    anchor = "<SYMBOL>TargetRunnable</SYMBOL>"
    text = arxml.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    text = text.replace(anchor, anchor + "<DATA-SEND-POINTS>" + "".join(points) + "</DATA-SEND-POINTS>")
    arxml.write_text(text, encoding="utf-8", newline="\n")
    return arxml


class TestAnAccessPointRefusalAbortsAComponentImportAtomically:
    """End to end through ``import_component``: refused, nothing written.

    Both shapes come only from a foreign export and would otherwise leave a project
    ``check-model`` refuses. A stated access-point name replaces the derived form both
    refusals judge, so each point is named as the writer derives it (or, for the second
    of the colliding pair, alike ignoring case, a name the settling pass declines).
    """

    def test_an_over_budget_pair_aborts_with_nothing_written(self, tmp_path: Path) -> None:
        port_name, member_name = _access_pair(WRITE_ACCESS_PREFIX, over=1)
        derived = access_point_name(WRITE_ACCESS_PREFIX, port_name, member_name)
        source = _export_with_foreign_access(
            tmp_path, [port_name], [member_name], _send_point(derived, port_name, member_name)
        )
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError) as raised:
            import_component(str(source), str(project / "model"))

        assert access_point_name(WRITE_ACCESS_PREFIX, port_name, member_name) in str(raised.value)
        assert f"'{port_name}'" in str(raised.value) and f"'{member_name}'" in str(raised.value)
        assert files_of(project / "model") == before

    def test_the_underscore_ambiguity_aborts_with_nothing_written(self, tmp_path: Path) -> None:
        source = _export_with_foreign_access(
            tmp_path,
            ["pA_B", "pA"],
            ["C", "B_C"],
            _send_point("W_pA_B_C", "pA_B", "C"),
            _send_point("w_pa_b_c", "pA", "B_C"),
        )
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="access point SHORT-NAME") as raised:
            import_component(str(source), str(project / "model"))

        assert access_point_name(WRITE_ACCESS_PREFIX, "pA_B", "C") in str(raised.value)
        # One header for the three guards: the entries are *written* with one name.
        assert "two access entries of one runnable are written with one access point SHORT-NAME" in str(raised.value)
        assert files_of(project / "model") == before

    def test_the_same_export_without_the_second_point_imports(self, tmp_path: Path) -> None:
        """The control: one of the two entries alone is an ordinary import.

        So the refusals above are about the pair and not about the splice."""
        source = _export_with_foreign_access(
            tmp_path, ["pA_B", "pA"], ["C", "B_C"], _send_point("Foreign_1", "pA_B", "C")
        )
        project = init_project(tmp_path / "proj")

        report = import_component(str(source), str(project / "model"))

        assert report.swc_action == "created"
        assert [(w.port, w.element) for w in read_swc_model(str(project / "model"))[0].runnables[0].writes] == [
            ("pA_B", "C")
        ]


class TestIdentifierConformanceFailsClosed:
    """Component import refuses a non-conforming name; it never renames.

    Unlike ``import model``, which renames and reports it: silently renaming an element
    inside a populated project would be worse than refusing the file.
    """

    def test_a_non_conforming_swc_name_aborts_atomically(self, tmp_path: Path) -> None:
        tampered = _renamed_export(tmp_path, "TargetSwc", "My SWC")
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="My SWC") as raised:
            import_component(str(tampered), str(project / "model"))

        assert raised.value.violations
        assert files_of(project / "model") == before
        assert not list((project / "model").rglob("*.tmp")), "a staged sibling survived the abort"

    def test_an_over_long_name_is_refused_under_the_name_the_export_declared(self, tmp_path: Path) -> None:
        """A 200-character record name is refused by name, never truncated.

        A record has no generated form, so only the length check refuses it; the shared ``PathRegistry`` does not cap
        the name at 128 characters before the check.
        """
        long_name = "P" * 200
        tampered = _renamed_export(tmp_path, "Pose_T", long_name)
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="not a legal AUTOSAR Identifier") as raised:
            import_component(str(tampered), str(project / "model"))

        assert any(long_name in violation and REASON_LENGTH in violation for violation in raised.value.violations), (
            raised.value.violations
        )
        assert files_of(project / "model") == before
        assert not list((project / "model").glob("data_types/**/P*.yaml")), "no truncated name may reach the target"

    def test_a_non_conforming_nested_name_aborts_the_whole_import(self, tmp_path: Path) -> None:
        """Not only the SWC name: one illegal nested name aborts the whole import.

        A legal component with one illegal CS operation argument is refused just the same."""
        tampered = _renamed_export(tmp_path, "<SHORT-NAME>samples<", "<SHORT-NAME>1samples<")
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="1samples"):
            import_component(str(tampered), str(project / "model"))

        assert files_of(project / "model") == before

    def test_the_message_names_the_element_the_name_and_the_reason(self, tmp_path: Path) -> None:
        """The sender has to be able to fix their export from the message alone."""
        tampered = _renamed_export(tmp_path, "TargetSwc", "My SWC")
        project = init_project(tmp_path / "proj")

        with pytest.raises(ReservedNameError) as raised:
            import_component(str(tampered), str(project / "model"))

        message = str(raised.value)
        assert "SWC name 'My SWC'" in message
        assert REASON_PATTERN in message
        assert "nothing written" in message

    def test_the_refused_export_really_is_one_the_writer_rejects(self, tmp_path: Path) -> None:
        """The claim behind the refusal, not just the refusal."""
        source_root = _build_source_model(tmp_path)
        swc_file = next((Path(source_root) / "swc").rglob("TargetSwc.yaml"))
        swc_file.write_text(swc_file.read_text(encoding="utf-8").replace("TargetSwc", "My SWC"), encoding="utf-8")

        with pytest.raises(ValueError, match="My SWC"):
            generate_arxml(source_root, str(tmp_path / "never-written.arxml"))

        assert not (tmp_path / "never-written.arxml").exists()

    def test_a_non_conforming_name_the_target_already_had_is_not_blamed_on_the_import(self, tmp_path: Path) -> None:
        """An already-broken target does not make every component unimportable.

        The target's own names are ``check-model``'s to report.
        """
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        write_model_element(project / "model", {"name": "bad name", "kind": "base_type", "size": 8, "encoding": "NONE"})

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"

    def test_a_conforming_component_still_imports_pydantic_equal(self, tmp_path: Path) -> None:
        """The negative half: the new check must not touch the working case."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        target_root = str(project / "model")
        source_swcs = {s.name: s for s in read_swc_model(source_root)}
        assert read_swc_model(target_root)[0] == source_swcs["TargetSwc"]
        source_types = {d.name: d for d in _both_type_layers(source_root)}
        target_types = {d.name: d for d in _both_type_layers(target_root)}
        assert sorted(target_types) == _CLOSURE_TYPE_NAMES
        for name in _CLOSURE_TYPE_NAMES:
            assert target_types[name] == source_types[name], name
        assert report.swc_action == "created"


class TestNothingBetweenTheMapperAndTheComparisonRebuildsADataElement:
    """No hand-written rebuild stands between ``map_interfaces`` and the conflict comparison.

    A data element's ``init`` therefore arrives in the written model. Does not reach:
    ``asil``, which the writer puts into no ARXML, so no round trip can witness it.
    """

    def test_a_data_elements_init_survives_the_import_of_a_real_export(self, tmp_path: Path) -> None:
        """The writer emits the value; the mapper delivers it unchanged."""
        _source_root, arxml = _export_target_with_init(tmp_path)

        model = read_arxml(str(arxml))
        forms: list = []
        data_types, registry, _dt_skips = map_data_types(model, elements=forms)
        interfaces, _iface_skips = map_interfaces(model, registry, data_types, elements=forms)

        speed = next(iface for iface in interfaces if iface.name == "ISpeed")
        assert [(de.name, de.init) for de in speed.data_elements] == [("speed", _SPEED_INIT), ("pose", None)]

    def test_the_value_reaches_the_written_model(self, tmp_path: Path) -> None:
        """End to end, because the mapper's answer is only half of what a user sees."""
        _source_root, arxml = _export_target_with_init(tmp_path)
        project = init_project(tmp_path / "proj")

        import_component(str(arxml), str(project / "model"))

        written = yaml.safe_load((project / "model" / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
        assert written["data_elements"][0] == {"name": "speed", "implementation_type": "Speed_T", "init": _SPEED_INIT}


def _with_mapping_set(text: str, pairs: list[tuple[str, str]]) -> str:
    """Return *text* with one ``DATA-TYPE-MAPPING-SET`` appended to the last ``ELEMENTS`` of ``/DataTypes``.

    A component export carries no mapping set, so a test that needs one injects it; the
    set goes into the last layer package so the references still point backwards.

    Args:
        text: a whole ARXML document.
        pairs: ``(application path, implementation path)`` per map, each below
            ``/DataTypes`` (``Application/Gear_T``).
    """
    marker = text.index("<SHORT-NAME>PortInterfaces</SHORT-NAME>")
    close = text.rindex("</ELEMENTS>", 0, marker)
    maps = "".join(
        "            <DATA-TYPE-MAP>\n"
        '              <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
        f"/DataTypes/{application}</APPLICATION-DATA-TYPE-REF>\n"
        '              <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
        f"/DataTypes/{implementation}</IMPLEMENTATION-DATA-TYPE-REF>\n"
        "            </DATA-TYPE-MAP>\n"
        for application, implementation in pairs
    )
    block = (
        "        <DATA-TYPE-MAPPING-SET>\n"
        "          <SHORT-NAME>DataTypeMappingSet</SHORT-NAME>\n"
        "          <DATA-TYPE-MAPS>\n" + maps + "          </DATA-TYPE-MAPS>\n"
        "        </DATA-TYPE-MAPPING-SET>\n"
    )
    return text[:close] + block + text[close:]


class TestAForeignApplicationEnumNamedLikeTheRetiredViewImportsWhole:
    """A TEXTTABLE enum named ``{B}_AppView`` keeps its literals.

    Such a foreign, XSD-valid file imports as the enum, never as a plain type over ``B`` with its
    literals silently dropped; asserted end to end through the written model.
    """

    #: The suffix of an input written by ``0.12.0`` or earlier; nothing this tree writes carries it.
    _APP_VIEW_SUFFIX = "_AppView"

    _ENUM = f"Gear_T{_APP_VIEW_SUFFIX}"

    @classmethod
    def _foreign_export(cls, tmp_path: Path) -> Path:
        """TargetSwc's export with its enum renamed to ``{B}_AppView`` and paired: a file the writer never emits."""
        _, arxml = _export_target(tmp_path)
        text = arxml.read_text(encoding="utf-8")
        text = text.replace("<SHORT-NAME>Gear_T</SHORT-NAME>", f"<SHORT-NAME>{cls._ENUM}</SHORT-NAME>")
        text = text.replace("/DataTypes/Application/Gear_T<", f"/DataTypes/Application/{cls._ENUM}<")
        text = text.replace(
            "        <APPLICATION-ARRAY-DATA-TYPE>",
            "        <IMPLEMENTATION-DATA-TYPE>\n"
            "          <SHORT-NAME>Gear_T</SHORT-NAME>\n"
            "          <CATEGORY>VALUE</CATEGORY>\n"
            "          <SW-DATA-DEF-PROPS>\n"
            "            <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "              <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '                <BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/Base/uint8_base</BASE-TYPE-REF>\n'
            "                <SW-CALIBRATION-ACCESS>NOT-ACCESSIBLE</SW-CALIBRATION-ACCESS>\n"
            "              </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "            </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "          </SW-DATA-DEF-PROPS>\n"
            "        </IMPLEMENTATION-DATA-TYPE>\n"
            "        <APPLICATION-ARRAY-DATA-TYPE>",
            1,
        )
        # The inserted implementation type lands in the application layer's package, beside the array.
        text = _with_mapping_set(text, [(f"Application/{cls._ENUM}", "Application/Gear_T")])
        foreign = tmp_path / "foreign-enum.arxml"
        foreign.write_text(text, encoding="utf-8")
        return foreign

    def test_the_enum_is_mapped_with_its_literals(self, tmp_path: Path) -> None:
        """The mapper keeps the enum; its literals are the entries of the ``texttable`` it names."""
        model = read_arxml(str(self._foreign_export(tmp_path)))

        forms: list = []
        data_types, _registry, _skips = map_data_types(model, elements=forms)

        survivor = {d.name: d for d in data_types}.get(self._ENUM)
        assert isinstance(survivor, EnumDataType), f"the enum was collapsed away: {sorted(d.name for d in data_types)}"
        (table,) = [form for form in forms if form.name == survivor.compu_method_ref]
        assert [entry.name for entry in table.entries] == ["PARK", "DRIVE"]

    def test_the_import_writes_the_enum_and_the_pair_that_named_it(self, tmp_path: Path) -> None:
        """End to end: the enum, the implementation type beside it and the set that pairs them.

        The file renames the enum and not its computation method, so ``Gear_T_CompuMethod``
        is not the enum's private form and arrives as a ``texttable`` element the enum names.
        """
        foreign = self._foreign_export(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(foreign), str(project / "model"))

        assert self._ENUM in report.created_data_types
        written = yaml.safe_load(
            (model_element_path(project / "model", self._ENUM, kind="enum")).read_text(encoding="utf-8")
        )
        assert written["kind"] == "enum"
        assert written["compu_method_ref"] == "Gear_T_CompuMethod"
        table = yaml.safe_load(
            model_element_path(project / "model", "Gear_T_CompuMethod", layer="compu_methods").read_text(
                encoding="utf-8"
            )
        )
        assert [entry["name"] for entry in table["entries"]] == ["PARK", "DRIVE"]
        assert report.created_elements == {"compu_methods": ["Gear_T_CompuMethod"]}
        assert report.created_mapping_sets == ["DataTypeMappingSet"]


class TestSkipSafety:
    def test_component_touching_skip_aborts_with_skip_surfaced(self, tmp_path: Path) -> None:
        """A mapper skip on the imported SWC's own port (a PR port) aborts the whole import.

        A lossy import of the very thing being imported is unsafe.
        """
        _, arxml = _export_target(tmp_path)
        # turn the provide port into a PR port: map_swcs skip+reports it
        arxml.write_text(
            arxml.read_text(encoding="utf-8").replace("P-PORT-PROTOTYPE", "PR-PORT-PROTOTYPE"), encoding="utf-8"
        )
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(LossyComponentImportError) as exc_info:
            import_component(str(arxml), str(project / "model"))

        assert any("PR-PORT-PROTOTYPE" in line for line in exc_info.value.skip_lines)
        assert "PR-PORT-PROTOTYPE" in str(exc_info.value)
        assert files_of(project / "model") == before

    def test_deferred_tag_content_is_report_only(self, tmp_path: Path) -> None:
        """Out-of-scope content matching DEFERRED_ELEMENT_TAGS is report-only.

        Such content (here a SW-ADDR-METHOD) is reported but never aborts the import."""
        _, arxml = _export_target(tmp_path)
        arxml.write_text(
            arxml.read_text(encoding="utf-8").replace(
                "<ELEMENTS>",
                "<ELEMENTS><SW-ADDR-METHOD><SHORT-NAME>CODE</SHORT-NAME></SW-ADDR-METHOD>",
                1,
            ),
            encoding="utf-8",
        )
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc == "TargetSwc"
        assert report.swc_action == "created"
        assert any("SW-ADDR-METHOD" in line for line in report.skip_lines)
        assert (project / "model" / "swc" / "components" / "TargetSwc.yaml").is_file()


class TestTheAbortMessageIsGroupedBySection:
    """``LossyComponentImportError``'s message groups its blocking lines by section.

    Each section that holds a line is the ``import model`` report's heading and its
    lines joined by ``"; "``, in the order defects, not supported, outside the file.
    ``skip_lines`` stays flat in log order; only a defect's line gains its citation.
    A category is not a severity: which entries abort does not change.
    """

    PREFIX = (
        "component import aborted: the following content could not be mapped, and a component "
        "export contains nothing but the component's own closure, so importing without it "
        "would be lossy: "
    )
    DUPLICATE_PORT = (
        "R-PORT-PROTOTYPE /ComponentTypes/TargetSwc/pSpeed ({release} [constr_2508]): duplicate full AUTOSAR "
        "path (already indexed as 'P-PORT-PROTOTYPE') -- malformed export, this declaration is dropped"
    )
    DUPLICATE_EVENT = (
        "INIT-EVENT /ComponentTypes/TargetSwc/TargetSwc_InternalBehavior/IT_TargetRunnable ({release} "
        "[constr_2508]): duplicate full AUTOSAR path (already indexed as 'TIMING-EVENT') -- malformed export, "
        "this declaration is dropped"
    )
    # The mapper's line, keyed at the option's PORT-REF; the export carries the options
    # an older writer wrote (``_edited_export``).
    API_OPTION = (
        "PORT-API-OPTION /ComponentTypes/TargetSwc/rCalib: "
        "PORT-API-OPTION's PORT-REF names no port of this component -- SWCraft carries no port API option"
    )
    DUPLICATE_PORT_NAME = (
        "R-PORT-PROTOTYPE /ComponentTypes/TargetSwc/pSpeed ({release} [constr_2508]): duplicate port name "
        "'pSpeed' on SWC 'TargetSwc'"
    )
    PR_PORT = (
        "PR-PORT-PROTOTYPE /ComponentTypes/TargetSwc/pSpeed: PR (combined provide+require) ports have no "
        "SWCraft equivalent (Port.direction is provide|require only)"
    )
    GHOST_INTERFACE = (
        "R-PORT-PROTOTYPE /ComponentTypes/TargetSwc/rCalib: unresolved reference /PortInterfaces/IGhost "
        "(the file declares no interface at that path)"
    )
    GHOST_ACCESS = (
        "VARIABLE-ACCESS /ComponentTypes/TargetSwc/TargetSwc_InternalBehavior/TargetRunnable/W_ghost: "
        "port 'pGhost' is not declared on SWC 'TargetSwc' (reference /ComponentTypes/TargetSwc/pGhost)"
    )

    @staticmethod
    def _edited_export(tmp_path: Path, *replacements: tuple[str, str], old_options: bool = False) -> Path:
        """``TargetSwc``'s export with *replacements* applied to its text.

        *old_options* puts back the port API options an older writer wrote
        (``fidelity_strip_support``), so a renamed port leaves an option whose
        ``PORT-REF`` names no port -- the not-supported line.
        """
        _, arxml = _export_target(tmp_path)
        if old_options:
            root = parse(arxml.read_bytes())
            restore_port_api_options(root)
            arxml.write_bytes(serialize(root))
        text = arxml.read_text(encoding="utf-8")
        for old, new in replacements:
            assert old in text, old
            text = text.replace(old, new)
        arxml.write_text(text, encoding="utf-8", newline="\n")
        return arxml

    def _two_sections_export(self, tmp_path: Path, schema: str = "AUTOSAR_4-2-2.xsd") -> Path:
        """Three defects and one unsupported line, met in that order: 1, 1, 1, 2."""
        return self._edited_export(
            tmp_path,
            ("<SHORT-NAME>rCalib</SHORT-NAME>", "<SHORT-NAME>pSpeed</SHORT-NAME>"),
            ("<SHORT-NAME>TMT_TargetRunnable</SHORT-NAME>", "<SHORT-NAME>IT_TargetRunnable</SHORT-NAME>"),
            ("AUTOSAR_4-2-2.xsd", schema),
            old_options=True,
        )

    def _abort(self, tmp_path: Path, arxml: Path) -> LossyComponentImportError:
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")
        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(arxml), str(project / "model"))
        assert files_of(project / "model") == before
        return caught.value

    def _two_section_lines(self, release: str) -> list[str]:
        return [
            self.DUPLICATE_PORT.format(release=release),
            self.DUPLICATE_EVENT.format(release=release),
            self.DUPLICATE_PORT_NAME.format(release=release),
            self.API_OPTION,
        ]

    def test_skip_lines_stay_flat_in_log_order_and_a_defect_carries_its_citation(self, tmp_path: Path) -> None:
        error = self._abort(tmp_path, self._two_sections_export(tmp_path))

        assert error.skip_lines == self._two_section_lines("R4.2.2")

    def test_the_message_groups_the_lines_by_section(self, tmp_path: Path) -> None:
        error = self._abort(tmp_path, self._two_sections_export(tmp_path))
        port, event, name, option = self._two_section_lines("R4.2.2")

        assert str(error) == (
            self.PREFIX
            + f"Defects of the export (3; the file breaks a rule of the standard, cited): {port}; {event}; {name}; "
            + f"Not supported by SWCraft (1): {option}"
        )

    def test_the_three_sections_print_in_order(self, tmp_path: Path) -> None:
        """The skip log meets the three sections 1, 2, 3; the outside-the-file heading says what it means.

        The outside-the-file line is a read through a port the component does not declare
        (invalid file content, refused at step 2); a port on an undeclared interface is
        answered by the late pass instead.
        """
        arxml = self._edited_export(
            tmp_path,
            ("<SHORT-NAME>TMT_TargetRunnable</SHORT-NAME>", "<SHORT-NAME>IT_TargetRunnable</SHORT-NAME>"),
            ("P-PORT-PROTOTYPE", "PR-PORT-PROTOTYPE"),
            (
                "<CAN-BE-INVOKED-CONCURRENTLY>false</CAN-BE-INVOKED-CONCURRENTLY>",
                "<CAN-BE-INVOKED-CONCURRENTLY>false</CAN-BE-INVOKED-CONCURRENTLY>"
                "<DATA-SEND-POINTS><VARIABLE-ACCESS><SHORT-NAME>W_ghost</SHORT-NAME><ACCESSED-VARIABLE>"
                "<AUTOSAR-VARIABLE-IREF>"
                '<PORT-PROTOTYPE-REF DEST="P-PORT-PROTOTYPE">/ComponentTypes/TargetSwc/pGhost</PORT-PROTOTYPE-REF>'
                '<TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/ISpeed/speed'
                "</TARGET-DATA-PROTOTYPE-REF></AUTOSAR-VARIABLE-IREF></ACCESSED-VARIABLE></VARIABLE-ACCESS>"
                "</DATA-SEND-POINTS>",
            ),
        )
        error = self._abort(tmp_path, arxml)
        event = self.DUPLICATE_EVENT.format(release="R4.2.2")

        assert error.skip_lines[:2] == [event, self.PR_PORT]
        assert len(error.skip_lines) == 3
        ghost = error.skip_lines[2]
        assert ghost == self.GHOST_ACCESS
        assert str(error) == (
            self.PREFIX
            + f"Defects of the export (1; the file breaks a rule of the standard, cited): {event}; "
            + f"Not supported by SWCraft (1): {self.PR_PORT}; "
            + "Outside the file (1; the file references content it does not declare): "
            + ghost
        )

    def test_a_port_on_an_interface_the_file_lacks_refuses_after_the_target_read(self, tmp_path: Path) -> None:
        """The ghost port's line is met by the late pass, once no target interface answers it."""
        error = self._abort(
            tmp_path, self._edited_export(tmp_path, (">/PortInterfaces/ICalib<", ">/PortInterfaces/IGhost<"))
        )

        assert self.GHOST_INTERFACE in error.skip_lines

    def test_a_section_with_no_line_is_left_out(self, tmp_path: Path) -> None:
        """A single-line abort: one unsupported port, one section, no defect heading."""
        error = self._abort(tmp_path, self._edited_export(tmp_path, ("P-PORT-PROTOTYPE", "PR-PORT-PROTOTYPE")))

        assert str(error) == self.PREFIX + f"Not supported by SWCraft ({len(error.skip_lines)}): " + "; ".join(
            error.skip_lines
        )
        assert "Defects of the export" not in str(error)
        assert "Outside the file" not in str(error)

    def test_a_4_3_0_file_cites_r4_3_0(self, tmp_path: Path) -> None:
        error = self._abort(tmp_path, self._two_sections_export(tmp_path, schema="AUTOSAR_4-3-0.xsd"))

        assert error.skip_lines == self._two_section_lines("R4.3.0")
        assert "R4.2.2" not in str(error)

    def test_a_file_declaring_another_release_cites_the_fallback_and_says_so(self, tmp_path: Path) -> None:
        error = self._abort(tmp_path, self._two_sections_export(tmp_path, schema="AUTOSAR_00051.xsd"))

        assert error.skip_lines == self._two_section_lines("R4.3.0")
        assert (
            "Defects of the export (3; the file breaks a rule of the standard, cited; cited from R4.3.0 — "
            "the file declares AUTOSAR_00051.xsd): "
        ) in str(error)

    def test_a_file_declaring_no_schema_cites_the_fallback_and_says_so(self, tmp_path: Path) -> None:
        arxml = self._edited_export(
            tmp_path,
            ("<SHORT-NAME>rCalib</SHORT-NAME>", "<SHORT-NAME>pSpeed</SHORT-NAME>"),
            ("<SHORT-NAME>TMT_TargetRunnable</SHORT-NAME>", "<SHORT-NAME>IT_TargetRunnable</SHORT-NAME>"),
            (' xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd"', ""),
            old_options=True,
        )
        error = self._abort(tmp_path, arxml)

        assert error.skip_lines == self._two_section_lines("R4.3.0")
        assert (
            "Defects of the export (3; the file breaks a rule of the standard, cited; cited from R4.3.0 — "
            "the file declares no schema): "
        ) in str(error)

    def test_a_data_elements_type_declared_nowhere_is_outside_the_file(self, tmp_path: Path) -> None:
        """The interface, and the port on it, are outside the file."""
        error = self._abort(
            tmp_path,
            self._edited_export(
                tmp_path,
                (">/DataTypes/Implementation/Speed_T</TYPE-TREF>", ">/DataTypes/Implementation/Ghost_T</TYPE-TREF>"),
            ),
        )

        interface_line = (
            "SENDER-RECEIVER-INTERFACE /PortInterfaces/ISpeed: data element 'speed': unresolved reference "
            "/DataTypes/Implementation/Ghost_T (the file declares no data type at that path)"
        )
        assert interface_line in error.skip_lines
        assert "Outside the file (" in str(error)
        assert "Not supported by SWCraft" not in str(error)
        assert "Defects of the export" not in str(error)

    def test_a_base_type_declared_nowhere_is_outside_the_file(self, tmp_path: Path) -> None:
        """The value type is outside the file; the interface typed by it stays SWCraft's, as map_interfaces decides."""
        error = self._abort(
            tmp_path,
            self._edited_export(
                tmp_path,
                (">/DataTypes/Base/float32_base</BASE-TYPE-REF>", ">/DataTypes/Base/ghost_base</BASE-TYPE-REF>"),
            ),
        )

        assert (
            "IMPLEMENTATION-DATA-TYPE /DataTypes/Implementation/Speed_T: unresolved reference "
            "/DataTypes/Base/ghost_base (the file "
            "declares no base type at that path)"
        ) in error.skip_lines
        outside = str(error).split("Outside the file (", 1)[1]
        assert "IMPLEMENTATION-DATA-TYPE /DataTypes/Implementation/Speed_T:" in outside

    def test_the_report_only_lines_are_unchanged(self, tmp_path: Path) -> None:
        """Deferred-tag lines are all not-supported: flat, uncited, rendered as ``"  - line"``."""
        arxml = self._edited_export(
            tmp_path,
            ("<ELEMENTS>", "<ELEMENTS><SW-ADDR-METHOD><SHORT-NAME>CODE</SHORT-NAME></SW-ADDR-METHOD>"),
        )
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"), dry_run=True)

        # Every ``ELEMENTS`` gains one; the first is the base types' package.
        assert "SW-ADDR-METHOD /DataTypes/Base/CODE: SW-ADDR-METHOD has no SWCraft representation" in report.skip_lines
        assert not any("[constr_" in candidate or "[TPS_" in candidate for candidate in report.skip_lines)
        rendered = report.render().splitlines()
        heading = rendered.index("Skipped out-of-scope elements:")
        assert rendered[heading + 1 : heading + 1 + len(report.skip_lines)] == [
            f"  - {candidate}" for candidate in report.skip_lines
        ]

    def test_every_blocking_entry_still_aborts(self, tmp_path: Path) -> None:
        """One skip line per blocking entry, whatever its section: nothing dropped, nothing added.

        Met here are sections 1 and 2: the reader's two defects and the mapper's
        duplicate port name and unsupported option.
        """
        arxml = self._two_sections_export(tmp_path)
        model = read_arxml(str(arxml))
        error = self._abort(tmp_path, arxml)

        assert [(entry.tag, entry.path) for entry in model.skip_log] == [
            (line.split(" ", 2)[0], line.split(" ", 2)[1].rstrip(":")) for line in error.skip_lines[:2]
        ]
        assert len(error.skip_lines) == 4


# Target-project ambiguity: duplicate names abort before any decision


class TestTargetAmbiguity:
    @pytest.mark.parametrize(
        ("layer_files", "dup_name"),
        [
            pytest.param(
                [
                    ("swc/body/DupA.yaml", {"name": "DupSwc"}),
                    ("swc/powertrain/DupB.yaml", {"name": "DupSwc"}),
                ],
                "DupSwc",
                id="swc",
            ),
            pytest.param(
                [
                    (
                        "interfaces/DupA.yaml",
                        {
                            "name": "IDup",
                            "kind": "sender_receiver",
                            "data_elements": [{"name": "v", "implementation_type": "uint8"}],
                        },
                    ),
                    (
                        "interfaces/DupB.yaml",
                        {
                            "name": "IDup",
                            "kind": "sender_receiver",
                            "data_elements": [{"name": "w", "implementation_type": "uint8"}],
                        },
                    ),
                ],
                "IDup",
                id="interface",
            ),
            pytest.param(
                [
                    # Two base types, so both files are in the base types' directory.
                    (
                        "data_types/base/DupA.yaml",
                        {"name": "Dup_T", "kind": "base_type", "size": 8, "encoding": "NONE"},
                    ),
                    (
                        "data_types/base/DupB.yaml",
                        {"name": "Dup_T", "kind": "base_type", "size": 16, "encoding": "NONE"},
                    ),
                ],
                "Dup_T",
                id="data_type",
            ),
            # The application types are a layer of their own, scanned in their own
            # directory (`import_.py`'s `application_type_files`).
            pytest.param(
                [
                    ("data_types/application/DupA.yaml", {"name": "DupApp_T", "kind": "application_type"}),
                    (
                        "data_types/application/DupB.yaml",
                        {"name": "DupApp_T", "kind": "enum", "literals": [{"name": "OFF", "value": 0}]},
                    ),
                ],
                "DupApp_T",
                id="application_type",
            ),
            # The scan reads this layer too, so the reserved-name check sees it.
            pytest.param(
                [
                    ("compositions/DupA.yaml", {"name": "DupComp", "title": "A"}),
                    ("compositions/DupB.yaml", {"name": "DupComp", "title": "B"}),
                ],
                "DupComp",
                id="composition",
            ),
        ],
    )
    def test_duplicate_names_in_target_abort_with_no_write(
        self, tmp_path: Path, layer_files: list[tuple[str, dict[str, Any]]], dup_name: str
    ) -> None:
        """Two target files sharing one `name` abort the import before any create/reuse/conflict decision."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        for rel_path, data in layer_files:
            _dump(model_root / rel_path, data)
        before = files_of(model_root)

        with pytest.raises(AmbiguousTargetProjectError, match=dup_name) as refused:
            import_component(str(arxml), str(project / "model"))

        assert files_of(model_root) == before
        # Each file by its storage name in the shared form, and in full only for a local terminal.
        shared, full = str(refused.value), refused.value.render(full_path=True)
        for rel_path, _data in layer_files:
            assert rel_path in shared and str(model_root / rel_path) not in shared, shared
            assert str(model_root / rel_path) in full, full
        assert str(tmp_path) not in shared and "\n".join(refused.value.duplicate_lines).count(str(tmp_path)) == 0

    def test_a_name_repeated_in_the_implementation_types_is_named_by_its_layer(self, tmp_path: Path) -> None:
        """The line names the group by its layer, ``implementation type``, never ``data type``."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _dump(
            model_root / "data_types/base/ProbeDup_base.yaml",
            {"name": "ProbeDup_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
        )
        for stem in ("DupA", "DupB"):
            _dump(
                model_root / f"data_types/implementation/{stem}.yaml",
                {"name": "ProbeDup", "kind": "value_type", "base": "ProbeDup_base"},
            )

        with pytest.raises(AmbiguousTargetProjectError) as refused:
            import_component(str(arxml), str(model_root))

        assert refused.value.duplicate_lines == [
            "implementation type name 'ProbeDup' is declared by 2 files: "
            "data_types/implementation/DupA.yaml, data_types/implementation/DupB.yaml"
        ]

    def test_an_application_type_and_an_implementation_type_of_one_name_are_not_ambiguous(self, tmp_path: Path) -> None:
        """One name declared once in each of the application and implementation layers is a pair, not a repeat."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        write_model_element(
            model_root,
            {"name": "ProbeShared_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
        )
        write_model_element(model_root, {"name": "ProbeShared", "kind": "value_type", "base": "ProbeShared_base"})
        write_model_element(model_root, {"name": "ProbeShared", "kind": "application_type"})

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        assert (model_root / "data_types" / "application" / "ProbeShared.yaml").is_file()
        assert (model_root / "data_types" / "implementation" / "ProbeShared.yaml").is_file()


class TestThePlannedCreatesFailClosedTwin:
    """``_planned_create_path``'s refusal, called directly.

    ``plan_component_import`` plans a create only for a name no file declares, so it
    cannot reach this branch; the refusal is the fail-closed twin a wrongly planning
    caller must meet rather than a file written over an element.
    """

    def test_a_plain_file_that_already_declares_the_name_is_refused_and_nothing_is_planned(
        self, tmp_path: Path
    ) -> None:
        model_root = tmp_path / "model"
        write_model_element(model_root, {"name": "Speed_T", "kind": "value_type", "base": "float32"})
        before = files_of(model_root)
        planned: list[str] = []
        suffix_lines: list[str] = []

        with pytest.raises(
            AmbiguousTargetProjectError, match="data_types/implementation/Speed_T.yaml already declares it"
        ):
            _planned_create_path(model_root, "implementation_types", "Speed_T", "data type", planned, suffix_lines)

        assert (planned, suffix_lines) == ([], [])
        assert files_of(model_root) == before


class TestAPathAnotherElementHoldsTakesASuffix:
    """A planned create whose plain file name another element's file holds takes the nearest free ``~N``.

    The file name is decided by the kernel's rule over the directory listing compared
    case-insensitively, so the occupant keeps its file and the answer is the same on
    every filesystem.
    """

    @pytest.mark.parametrize(
        ("rel_path", "data", "incoming_name", "suffixed", "label"),
        [
            pytest.param(
                "interfaces/ISpeed.yaml",
                {
                    "name": "IUnrelated",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "v", "implementation_type": "uint8"}],
                },
                "ISpeed",
                "interfaces/ISpeed~1.yaml",
                "interface",
                id="interface",
            ),
            # The occupant holds a base type's file; the incoming base type `float32_base` is the create it blocks.
            pytest.param(
                model_element_storage_name("float32_base", kind="base_type"),
                _UNRELATED_OCCUPANT,
                "float32_base",
                "data_types/base/float32_base~1.yaml",
                "data type",
                id="data_type",
            ),
            pytest.param(
                "swc/components/TargetSwc.yaml",
                {"name": "SomeOtherSwc"},
                "TargetSwc",
                "swc/components/TargetSwc~1.yaml",
                "SWC",
                id="swc",
            ),
        ],
    )
    def test_a_create_onto_a_held_path_keeps_the_occupant_and_takes_the_first_suffix(
        self,
        tmp_path: Path,
        rel_path: str,
        data: dict[str, Any],
        incoming_name: str,
        suffixed: str,
        label: str,
    ) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _dump(model_root / rel_path, data)
        occupant = (model_root / rel_path).read_bytes()

        report = import_component(str(arxml), str(model_root))

        assert (model_root / rel_path).read_bytes() == occupant, "the element already stored keeps its file"
        written = yaml.safe_load((model_root / suffixed).read_text(encoding="utf-8"))
        assert written["name"] == incoming_name
        assert suffixed in {Path(path).relative_to(model_root).as_posix() for path in report.written_paths}
        assert report.file_suffix_lines == [
            f"{label} '{incoming_name}' written as {suffixed} -- its plain file name {rel_path} is already "
            "taken, compared case-insensitively; the name and its references are unchanged"
        ]
        assert report.render().count("Files suffixed for a case-insensitive collision (1;") == 1

    def test_a_file_differing_only_in_case_takes_the_same_suffix_on_every_filesystem(self, tmp_path: Path) -> None:
        """``ispeed.yaml`` holds ``ISpeed.yaml``'s name whatever the filesystem says: SWCraft's rule decides."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        # The target's `ispeed` is placed in a sub-package so it shares no package
        # with the incoming `ISpeed`; unplaced, the two are one name ignoring case
        # in one package and the import is refused. This case is about the file.
        _dump(
            model_root / "interfaces" / "ispeed.yaml",
            {
                "name": "ispeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "uint8"}],
                "package": "Legacy",
            },
        )

        import_component(str(arxml), str(model_root))

        assert {interface.name for interface in read_interfaces(str(model_root))} == {"ispeed", "ISpeed", "ICalib"}
        assert yaml.safe_load((model_root / "interfaces" / "ISpeed~1.yaml").read_text(encoding="utf-8"))["name"] == (
            "ISpeed"
        )

    def test_the_plan_the_protocol_derives_from_carries_no_file_name(self, tmp_path: Path) -> None:
        """The report the MCP tool renders names no file; the CLI's does.

        The protocol decides the file at its commit, so the plan keeps its suffix lines
        beside the report; ``import_component``'s report carries them, on a dry run too.
        """
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _dump(model_element_path(model_root, "float32_base", kind="base_type"), _UNRELATED_OCCUPANT)

        plan = plan_component_import(str(arxml), str(model_root), dry_run=True)
        dry_run = import_component(str(arxml), str(model_root), dry_run=True)

        assert len(plan.file_suffix_lines) == 1
        assert plan.report.file_suffix_lines == []
        assert "~1" not in plan.report.render()
        assert dry_run.file_suffix_lines == list(plan.file_suffix_lines)
        assert [
            target.path.relative_to(model_root).as_posix()
            for target in plan.targets
            if target.layer in ("implementation_types", "base_types", "application_types")
        ] == [
            "data_types/base/float32_base~1.yaml" if name == "float32_base" else _closure_type_storage_name(name)
            for name in _CLOSURE_TYPES_IN_SOURCE_ORDER
        ]


# Two of this import's own planned creates differing only in case (the class
# above is about a file that existed before the import)


_AR_NS = "http://autosar.org/schema/r4.0"


def _duplicate_element_with_new_name(arxml_path: Path, old_name: str, new_name: str) -> None:
    """Insert a deep copy of the element whose ``SHORT-NAME`` is *old_name*, renamed *new_name*, after it.

    Builds an export holding two names that collide only by case, which
    case-sensitive source tooling can produce.
    """
    ET.register_namespace("", _AR_NS)
    tree = ET.parse(arxml_path)
    root = tree.getroot()
    short_name_tag = f"{{{_AR_NS}}}SHORT-NAME"
    for parent in root.iter():
        for idx, child in enumerate(list(parent)):
            short_name = child.find(short_name_tag)
            if short_name is not None and short_name.text == old_name:
                clone = copy.deepcopy(child)
                clone.find(short_name_tag).text = new_name
                parent.insert(idx + 1, clone)
                tree.write(arxml_path, xml_declaration=True, encoding="UTF-8")
                return
    raise AssertionError(f"no element with SHORT-NAME {old_name!r} found in {arxml_path}")


def _export_collide_component(tmp_path: Path, tag: str) -> Path:
    """Export the smallest closure where a data type and an interface are both new creates.

    One value type over one base type, one sender-receiver interface typed by it and one
    SWC providing it; the clone that makes the collision is each caller's own.
    """
    model_root = tmp_path / f"source_model_{tag}"
    dt = model_root / "data_types"
    _dump(
        model_element_path(model_root, "float32", kind="base_type"),
        {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754", "native_declaration": "float"},
    )
    _dump(dt / "Speed_T.yaml", {"name": "Speed_T", "kind": "value_type", "base": "float32"})
    ifaces = model_root / "interfaces"
    _dump(
        ifaces / "ICollide.yaml",
        {
            "name": "ICollide",
            "kind": "sender_receiver",
            "data_elements": [{"name": "v", "implementation_type": "Speed_T"}],
        },
    )
    swc = model_root / "swc"
    _dump(
        swc / "body" / "CollideSwc.yaml",
        {
            "name": "CollideSwc",
            "ports": [{"name": "pOut", "direction": "provide", "interface": "ICollide"}],
            "handle_termination_and_restart": "no_support",
        },
    )
    arxml = tmp_path / f"CollideSwc_{tag}.arxml"
    export_component(str(model_root), "CollideSwc", str(arxml))
    return arxml


#: The colliding pair each layer contributes: the exported name and the clone's.
_COLLIDING_PAIR = {"data_type": ("Speed_T", "speed_t"), "interface": ("ICollide", "icollide")}


def _export_self_colliding_component(tmp_path: Path, layer: str) -> Path:
    """A component export introducing two new same-layer elements whose names differ only in case.

    The clone is declared in a second package, ``Zeta``, which sorts after the
    original's, so source-path order and name order agree here; the import places
    the clone in ``Zeta``. In one package the pair is the source's own
    ``[constr_2508]`` defect (:func:`_export_same_package_colliding_component`).
    """
    arxml = _export_collide_component(tmp_path, layer)
    declared, clone = _COLLIDING_PAIR[layer]
    _duplicate_element_into_an_earlier_package(arxml, declared, clone, "Zeta")
    return arxml


def _export_same_package_colliding_component(tmp_path: Path, layer: str) -> Path:
    """The self-colliding export with the clone the original's sibling in ONE package."""
    arxml = _export_collide_component(tmp_path, f"{layer}_same_package")
    declared, clone = _COLLIDING_PAIR[layer]
    _duplicate_element_with_new_name(arxml, declared, clone)
    return arxml


#: What each self-colliding export writes: the two creates, in planning order
#: (name order, ``'S' < 's'`` and ``'I' < 'i'``), under the plain name and ``~1``.
_SELF_COLLIDING_FILES = {
    "data_type": (
        "data type",
        {"data_types/implementation/Speed_T.yaml": "Speed_T", "data_types/implementation/speed_t~1.yaml": "speed_t"},
    ),
    "interface": ("interface", {"interfaces/ICollide.yaml": "ICollide", "interfaces/icollide~1.yaml": "icollide"}),
}


class TestSelfCollidingCreatesTakeTheirOwnFiles:
    """Two brand-new names of one import differing only in case are two files.

    Each create asks the kernel for its file with the paths already planned as
    *reserved*, so the second sees the first's planned name before anything is written.
    """

    @pytest.mark.parametrize("layer", ["data_type", "interface"])
    def test_two_new_creates_differing_only_in_case_are_written_to_the_plain_name_and_the_first_suffix(
        self, tmp_path: Path, layer: str
    ) -> None:
        arxml = _export_self_colliding_component(tmp_path, layer)
        project = init_project(tmp_path / f"proj_{layer}")
        model_root = project / "model"
        label, files = _SELF_COLLIDING_FILES[layer]

        report = import_component(str(arxml), str(model_root))

        for relative, identity in files.items():
            assert yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))["name"] == identity
        (_plain, first), (suffixed, second) = files.items()
        assert report.file_suffix_lines == [
            f"{label} '{second}' written as {suffixed} -- its plain file name "
            f"{suffixed.replace('~1', '')} is already taken, compared case-insensitively; the name and its "
            "references are unchanged"
        ]
        names = read_data_types(str(model_root)) if layer == "data_type" else read_interfaces(str(model_root))
        assert {first, second} <= {element.name for element in names}
        # The clone's own package names it, the original stays in the root.
        assert {element.name: element.package for element in names if element.name in (first, second)} == {
            first: None,
            second: "Zeta",
        }

    @pytest.mark.parametrize("layer", ["data_type", "interface"])
    def test_the_same_pair_in_one_package_is_refused_with_nothing_written(self, tmp_path: Path, layer: str) -> None:
        """Two case-only spellings of ONE source package break the source's own ``[constr_2508]``."""
        arxml = _export_same_package_colliding_component(tmp_path, layer)
        project = init_project(tmp_path / f"proj_{layer}_same")
        model_root = project / "model"
        before = files_of(model_root)

        with pytest.raises(ReservedNameError, match=r"\[constr_2508\]"):
            import_component(str(arxml), str(model_root))

        assert files_of(model_root) == before

    @pytest.mark.parametrize("layer", ["data_type", "interface"])
    def test_a_dry_run_reports_the_suffix_and_writes_nothing(self, tmp_path: Path, layer: str) -> None:
        """The planner decides the file names, so a dry run reports what the apply would write."""
        arxml = _export_self_colliding_component(tmp_path, layer)
        project = init_project(tmp_path / f"proj_{layer}_dry")
        model_root = project / "model"
        before = files_of(model_root)

        report = import_component(str(arxml), str(model_root), dry_run=True)

        assert len(report.file_suffix_lines) == 1
        assert "~1.yaml" in report.file_suffix_lines[0]
        assert files_of(model_root) == before


# The two importers plan their creates in one order, the export's own
# source-path order. The pair below is declared in a package that sorts BEFORE
# the original's, so path order is the reverse of name order -- the one shape
# in which the two orders give different answers.


def _duplicate_element_into_an_earlier_package(arxml_path: Path, old_name: str, new_name: str, package: str) -> None:
    """Deep-copy the element named *old_name* into a new ``AR-PACKAGE`` *package*, renamed *new_name*.

    The package is appended last in document order while its name sorts first, so the
    source-path order is neither the document order nor the name order.
    """
    ET.register_namespace("", _AR_NS)
    tree = ET.parse(arxml_path)
    root = tree.getroot()
    short_name_tag = f"{{{_AR_NS}}}SHORT-NAME"
    packages = root.find(f"{{{_AR_NS}}}AR-PACKAGES")
    for parent in root.iter():
        for child in list(parent):
            short_name = child.find(short_name_tag)
            if short_name is not None and short_name.text == old_name:
                clone = copy.deepcopy(child)
                clone.find(short_name_tag).text = new_name
                new_package = ET.SubElement(packages, f"{{{_AR_NS}}}AR-PACKAGE")
                ET.SubElement(new_package, short_name_tag).text = package
                ET.SubElement(new_package, f"{{{_AR_NS}}}ELEMENTS").append(clone)
                tree.write(arxml_path, xml_declaration=True, encoding="UTF-8")
                return
    raise AssertionError(f"no element with SHORT-NAME {old_name!r} found in {arxml_path}")


#: ``AppIfaces`` and ``AppTypes`` both sort before ``DataTypes`` and
#: ``PortInterfaces``, the packages a component export declares its closure in.
_EARLIER_PACKAGE = {"data_type": "AppTypes", "interface": "AppIfaces"}

#: What the export's path order gives each pair: the CLONE holds the plain file
#: name and the exported element takes ``~1`` — the opposite of what the name
#: order (``'S' < 's'``, ``'I' < 'i'``) gives, which is what makes the case red.
_PATH_ORDER_FILES = {
    "data_type": (
        "data type",
        {"data_types/implementation/speed_t.yaml": "speed_t", "data_types/implementation/Speed_T~1.yaml": "Speed_T"},
    ),
    "interface": ("interface", {"interfaces/icollide.yaml": "icollide", "interfaces/ICollide~1.yaml": "ICollide"}),
}


def _export_cross_package_colliding_component(tmp_path: Path, layer: str) -> Path:
    """The self-colliding export again, with the clone declared in an earlier package."""
    arxml = _export_collide_component(tmp_path, f"{layer}_cross_package")
    declared, clone = _COLLIDING_PAIR[layer]
    _duplicate_element_into_an_earlier_package(arxml, declared, clone, _EARLIER_PACKAGE[layer])
    return arxml


def _layer_files(model_root: Path, layer_directory: str) -> dict[str, str]:
    """Every file of one layer directory, by storage name, to the identity it declares."""
    return {
        path.relative_to(model_root).as_posix(): yaml.safe_load(path.read_text(encoding="utf-8"))["name"]
        for path in model_root.glob(f"{layer_directory}/*.yaml")
    }


class TestBothImportersPlanTheirCreatesInTheExportsOwnOrder:
    """``import component`` takes the export's source-path order, as ``import model`` does.

    The first of a case-insensitively matching pair gets the plain file and the next
    ``~1``, decided by the export's source-path order; a plan in name order would hand the plain file to the other
    element.

    Does not reach: an element an earlier import stored (found by its declared
    identity, not re-ordered); the file-name rule itself
    (``arxml_import.report.allocated_file``); any case-sensitive filesystem (the
    suffix is decided with ``str.casefold()``); a tree already holding two files
    differing only in case (``rule:storage.filename-case-collision``).
    """

    @pytest.mark.parametrize("layer", ["data_type", "interface"])
    def test_the_clone_declared_in_an_earlier_package_takes_the_plain_file(self, tmp_path: Path, layer: str) -> None:
        arxml = _export_cross_package_colliding_component(tmp_path, layer)
        project = init_project(tmp_path / f"proj_{layer}_paths")
        model_root = project / "model"
        label, files = _PATH_ORDER_FILES[layer]

        report = import_component(str(arxml), str(model_root))

        for relative, identity in files.items():
            assert yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))["name"] == identity
        # The pair shares one root package ignoring case, so the clone -- the
        # member not at `/{Root}/{name}` -- is placed in its source package's last
        # segment, and the exported element stays in the root.
        (plain, _first), (suffixed, second) = files.items()
        assert yaml.safe_load((model_root / plain).read_text(encoding="utf-8"))["package"] == _EARLIER_PACKAGE[layer]
        assert "package" not in yaml.safe_load((model_root / suffixed).read_text(encoding="utf-8"))
        assert report.file_suffix_lines == [
            f"{label} '{second}' written as {suffixed} -- its plain file name "
            f"{suffixed.replace('~1', '')} is already taken, compared case-insensitively; the name and its "
            "references are unchanged"
        ]

    @pytest.mark.parametrize(
        "layer,directory", [("data_type", "data_types/implementation"), ("interface", "interfaces")]
    )
    def test_import_model_over_the_same_export_writes_the_same_files(
        self, tmp_path: Path, layer: str, directory: str
    ) -> None:
        """The oracle: ``run_import`` over the same bytes writes the same files.

        A component closure is a model with one SWC and no composition content, so
        ``run_import`` accepts the file.
        """
        arxml = _export_cross_package_colliding_component(tmp_path, layer)
        component_project = init_project(tmp_path / f"proj_{layer}_component")
        model_project = init_project(tmp_path / f"proj_{layer}_model")

        import_component(str(arxml), str(component_project / "model"))
        # `force`: the scaffolded project is not empty (Root.yaml and the two
        # hardware elements), and the import is not what is under test here.
        run_import(str(arxml), str(model_project / "swcraft.yaml"), force=True)

        expected = dict(_PATH_ORDER_FILES[layer][1])
        component_files = _layer_files(component_project / "model", directory)
        assert component_files == _layer_files(model_project / "model", directory)
        assert component_files == expected
        if layer == "data_type":
            # The base type, no collision of its own, in its own layer's directory.
            base_directory = model_element_storage_name("float32", kind="base_type").rsplit("/", 1)[0]
            base_files = _layer_files(component_project / "model", base_directory)
            assert base_files == _layer_files(model_project / "model", base_directory)
            assert base_files == {"data_types/base/float32.yaml": "float32"}

    def test_the_report_lists_the_creates_in_that_order_with_no_collision_anywhere(self, tmp_path: Path) -> None:
        """The planning order is the report's listing order too, visible without a collision.

        Three data types whose path order and name order disagree and none collides, so
        the listing alone tells the orders apart.
        """
        arxml = _export_collide_component(tmp_path, "listing_order")
        _duplicate_element_into_an_earlier_package(arxml, "Speed_T", "Zulu_T", "AppTypes")
        project = init_project(tmp_path / "proj_listing_order")

        report = import_component(str(arxml), str(project / "model"))

        # /AppTypes/Zulu_T < /DataTypes/Base/float32 < /DataTypes/Implementation/Speed_T (the layer packages).
        assert report.created_data_types == ["Zulu_T", "float32", "Speed_T"]
        assert sorted(report.created_data_types) != report.created_data_types
        # No name in it collides with another case-insensitively, so nothing
        # here is the suffix rule's business.
        assert report.file_suffix_lines == []
        assert len({name.casefold() for name in report.created_data_types}) == 3
        rendered = report.render().split("\n")
        listed = rendered[rendered.index("CREATED 3 data type(s)") + 1 :][:3]
        assert listed == ["  - Zulu_T", "  - float32", "  - Speed_T"]


# Identical shared definitions: silent reuse, no duplicate files


class TestSharedReuse:
    def test_identical_shared_defs_are_reused_silently(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        # the target project already declares the exact shared interfaces
        # and data types the component references (authored form)
        for name in ("ISpeed", "ICalib"):
            (model_root / "interfaces" / f"{name}.yaml").write_bytes(
                (Path(source_root) / "interfaces" / f"{name}.yaml").read_bytes()
            )
        for name in _CLOSURE_TYPE_NAMES:
            target = model_root / _closure_type_storage_name(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((Path(source_root) / _closure_type_storage_name(name)).read_bytes())
        shared_before = {
            p.relative_to(model_root).as_posix(): p.read_bytes()
            for p in [
                *model_root.glob("interfaces/*.yaml"),
                *model_root.glob("data_types/implementation/*.yaml"),
                *model_root.glob("data_types/base/*.yaml"),
                *model_root.glob("data_types/application/*.yaml"),
            ]
        }

        report = import_component(str(arxml), str(project / "model"))

        # everything shared was reused — nothing created, nothing rewritten
        assert report.reused_interfaces == ["ICalib", "ISpeed"]
        assert report.reused_data_types == _CLOSURE_TYPES_IN_SOURCE_ORDER
        assert report.created_interfaces == []
        assert report.created_data_types == []
        assert report.swc_action == "created"
        assert len(report.written_paths) == 1
        assert report.written_paths[0].endswith("TargetSwc.yaml")
        shared_after = {
            p.relative_to(model_root).as_posix(): p.read_bytes()
            for p in [
                *model_root.glob("interfaces/*.yaml"),
                *model_root.glob("data_types/implementation/*.yaml"),
                *model_root.glob("data_types/base/*.yaml"),
                *model_root.glob("data_types/application/*.yaml"),
            ]
        }
        assert shared_after == shared_before  # no duplicate/rewritten files

    def test_reimporting_the_same_export_is_a_full_noop(self, tmp_path: Path) -> None:
        """A second import of the same export is a full no-op: SWC unchanged, every shared definition reused."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        import_component(str(arxml), str(project / "model"))
        before = files_of(project / "model")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "unchanged"
        assert report.reused_interfaces == ["ICalib", "ISpeed"]
        assert report.reused_data_types == _CLOSURE_TYPES_IN_SOURCE_ORDER
        assert report.written_paths == []
        assert files_of(project / "model") == before


class TestATargetInitValueIsProjectedOutOfTheComparison:
    """An init the target declares is projected out when the incoming side declares none.

    An absent incoming value is no evidence (a third-party export need not carry
    values), and the shared-definition conflict admits no override, so comparing it
    would fail the import on a value nobody contradicted. The incoming side declaring
    a value is ``TestAnIncomingInitValueIsEvidenceAndNotProjectedAway``'s.
    """

    def _target_with_an_init(self, tmp_path: Path) -> tuple[Path, Path]:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        authored = yaml.safe_load((model_root / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
        authored["data_elements"][0]["init"] = 42
        _dump(model_root / "interfaces" / "ISpeed.yaml", authored)
        return project, arxml

    def test_the_interface_is_reused_and_not_reported_as_a_conflict(self, tmp_path: Path) -> None:
        project, arxml = self._target_with_an_init(tmp_path)

        report = import_component(str(arxml), str(project / "model"))

        assert report.reused_interfaces == ["ICalib", "ISpeed"]
        assert report.created_interfaces == []

    def test_the_targets_init_value_survives_the_import_untouched(self, tmp_path: Path) -> None:
        """Reuse means no write: the authored value is still there."""
        project, arxml = self._target_with_an_init(tmp_path)
        interface_file = project / "model" / "interfaces" / "ISpeed.yaml"
        before = interface_file.read_bytes()

        import_component(str(arxml), str(project / "model"))

        assert interface_file.read_bytes() == before
        assert yaml.safe_load(interface_file.read_text(encoding="utf-8"))["data_elements"][0]["init"] == 42

    def test_a_real_difference_beside_an_init_is_still_a_conflict(self, tmp_path: Path) -> None:
        """The projection blanks the init and nothing else: a real divergence still rejects."""
        project, arxml = self._target_with_an_init(tmp_path)
        model_root = project / "model"
        authored = yaml.safe_load((model_root / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
        authored["data_elements"][0]["implementation_type"] = "float32"
        _dump(model_root / "interfaces" / "ISpeed.yaml", authored)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="ISpeed") as exc_info:
            import_component(str(arxml), str(project / "model"))

        assert any("ISpeed" in entry for entry in exc_info.value.conflicts)
        assert any("data element init values" in entry for entry in exc_info.value.conflicts)
        assert files_of(model_root) == before


# An init the INCOMING side declares: a third-party component ARXML arrives
# with a real value, and a symmetric projection would erase that evidence
# before the comparison saw it.


def _inject_element_init_value(arxml_path: Path, element_name: str, value: str) -> None:
    """Give one VARIABLE-DATA-PROTOTYPE an ``INIT-VALUE`` referring to a new ``CONSTANT-SPECIFICATION``.

    The constant lives in a package of its own under a name no SWCraft writer derives,
    which makes the file foreign. The shape is the standard's reference form
    (``INIT-VALUE/CONSTANT-REFERENCE/CONSTANT-REF`` to a ``CONSTANT-SPECIFICATION``
    holding one ``NUMERICAL-VALUE-SPECIFICATION``).
    """
    ET.register_namespace("", _AR_NS)
    tree = ET.parse(arxml_path)
    root = tree.getroot()

    def tag(name: str) -> str:
        return f"{{{_AR_NS}}}{name}"

    prototype = next(
        (
            candidate
            for candidate in root.iter(tag("VARIABLE-DATA-PROTOTYPE"))
            if (short_name := candidate.find(tag("SHORT-NAME"))) is not None and short_name.text == element_name
        ),
        None,
    )
    if prototype is None:
        raise AssertionError(f"no VARIABLE-DATA-PROTOTYPE named {element_name!r} in {arxml_path}")

    constant_path = f"/Constants/C_{element_name}_Init"
    init_value = ET.SubElement(prototype, tag("INIT-VALUE"))
    reference = ET.SubElement(ET.SubElement(init_value, tag("CONSTANT-REFERENCE")), tag("CONSTANT-REF"))
    reference.set("DEST", "CONSTANT-SPECIFICATION")
    reference.text = constant_path

    package = ET.SubElement(root.find(tag("AR-PACKAGES")), tag("AR-PACKAGE"))
    ET.SubElement(package, tag("SHORT-NAME")).text = "Constants"
    specification = ET.SubElement(ET.SubElement(package, tag("ELEMENTS")), tag("CONSTANT-SPECIFICATION"))
    ET.SubElement(specification, tag("SHORT-NAME")).text = f"C_{element_name}_Init"
    numerical = ET.SubElement(ET.SubElement(specification, tag("VALUE-SPEC")), tag("NUMERICAL-VALUE-SPECIFICATION"))
    ET.SubElement(numerical, tag("VALUE")).text = value

    tree.write(arxml_path, xml_declaration=True, encoding="UTF-8")


class TestAnIncomingInitValueIsEvidenceAndNotProjectedAway:
    """An incoming init value is evidence: the projection is asymmetric.

    An incoming ``init=None`` is the absence of evidence; an incoming value was read
    from a real ``INIT-VALUE``, and blanking it would lose it silently on the reuse
    branch, so a value the target does not match is a conflict like any other
    divergent shared definition.

    Does not reach: the port-level COM-SPEC init, the port's own value, compared on its
    ``com_specs`` entry (``test_port_init_import.py``).
    """

    def _target_and_third_party_export(
        self, tmp_path: Path, *, incoming: str, target_init: object | None
    ) -> tuple[Path, Path]:
        source_root, arxml = _export_target(tmp_path)
        _inject_element_init_value(arxml, "speed", incoming)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        if target_init is not None:
            authored = yaml.safe_load((model_root / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
            authored["data_elements"][0]["init"] = target_init
            _dump(model_root / "interfaces" / "ISpeed.yaml", authored)
        return project, arxml

    def test_the_reader_really_delivers_the_incoming_value(self, tmp_path: Path) -> None:
        """Premise: the injected INIT-VALUE reaches the comparison as 255, or the class passes vacuously."""
        _source_root, arxml = _export_target(tmp_path)
        _inject_element_init_value(arxml, "speed", "255")

        model = read_arxml(str(arxml))
        forms: list = []
        data_types, registry, _dt_skips = map_data_types(model, elements=forms)
        interfaces, _iface_skips = map_interfaces(model, registry, data_types, elements=forms)

        speed = next(iface for iface in interfaces if iface.name == "ISpeed")
        assert [(de.name, de.init) for de in speed.data_elements] == [("speed", 255), ("pose", None)]

    def test_an_incoming_value_the_target_contradicts_is_a_conflict(self, tmp_path: Path) -> None:
        project, arxml = self._target_and_third_party_export(tmp_path, incoming="255", target_init=42)
        model_root = project / "model"
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="ISpeed") as exc_info:
            import_component(str(arxml), str(model_root))

        assert any("'speed'" in entry and "255" in entry and "42" in entry for entry in exc_info.value.conflicts)
        assert files_of(model_root) == before

    def test_an_incoming_value_the_target_declares_nothing_for_is_a_conflict(self, tmp_path: Path) -> None:
        """The reuse branch writes nothing, so without the conflict the declared value would vanish unreported."""
        project, arxml = self._target_and_third_party_export(tmp_path, incoming="255", target_init=None)
        model_root = project / "model"
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="ISpeed") as exc_info:
            import_component(str(arxml), str(model_root))

        assert any("'speed'" in entry and "255" in entry for entry in exc_info.value.conflicts)
        assert files_of(model_root) == before

    def test_an_incoming_value_the_target_agrees_with_is_reused(self, tmp_path: Path) -> None:
        """Agreement is not divergence: an agreeing incoming value is reused.

        The asymmetric rule must not turn every third-party init into a conflict either."""
        project, arxml = self._target_and_third_party_export(tmp_path, incoming="255", target_init=255)
        interface_file = project / "model" / "interfaces" / "ISpeed.yaml"
        before = interface_file.read_bytes()

        report = import_component(str(arxml), str(project / "model"))

        assert report.reused_interfaces == ["ICalib", "ISpeed"]
        assert report.created_interfaces == []
        assert interface_file.read_bytes() == before

    def test_an_incoming_value_lands_in_a_created_interface(self, tmp_path: Path) -> None:
        """Where the target declares no such interface the value is written rather than refused."""
        _source_root, arxml = _export_target(tmp_path)
        _inject_element_init_value(arxml, "speed", "255")
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        assert "ISpeed" in report.created_interfaces
        written = yaml.safe_load((model_root / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
        assert written["data_elements"][0] == {"name": "speed", "implementation_type": "Speed_T", "init": 255}


# SWCraft's own export declares init values: the same decisions measured end
# to end, with no hand-built XML.


class TestASwcraftExportCarriesItsInitValuesThroughTheImport:
    """What `swcraft import component` does with a value SWCraft's own export declares.

    Created writes it, an equal target reuses, a contradicting one conflicts; an
    incoming absence beside a target value is
    ``TestATargetInitValueIsProjectedOutOfTheComparison``'s.

    Does not reach: the value grammar (``test_arxml_init_writer.py``'s and
    ``test_component_export.py``'s) and any value shape but a scalar.
    """

    def test_the_export_really_declares_the_value(self, tmp_path: Path) -> None:
        """Premise: this export carries an ``INIT-VALUE`` and the plain one does not.

        A regression that stopped emitting it fails here rather than turning the class green vacuously.
        """
        _source_root, with_init = _export_target_with_init(tmp_path)
        _plain_root, plain = _export_target(tmp_path)

        assert init_constant_name("ISpeed", "speed") in with_init.read_text(encoding="utf-8")
        assert "INIT-VALUE" in with_init.read_text(encoding="utf-8")
        assert "INIT-VALUE" not in plain.read_text(encoding="utf-8")

    def test_an_empty_target_is_created_with_the_value_in_its_yaml(self, tmp_path: Path) -> None:
        """The created branch: with no target definition to disagree with, the declared value is written."""
        _source_root, arxml = _export_target_with_init(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        assert "ISpeed" in report.created_interfaces
        written = yaml.safe_load((model_root / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
        assert written["data_elements"] == [
            {"name": "speed", "implementation_type": "Speed_T", "init": _SPEED_INIT},
            {"name": "pose", "application_type": "Pose_T"},
        ]

    def test_an_equal_target_value_is_reused_and_nothing_is_written(self, tmp_path: Path) -> None:
        """An equal target value is reused silently and the target file's bytes are untouched."""
        source_root, arxml = _export_target_with_init(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        interface_file = model_root / "interfaces" / "ISpeed.yaml"
        before = interface_file.read_bytes()

        report = import_component(str(arxml), str(model_root))

        assert report.reused_interfaces == ["ICalib", "ISpeed"]
        assert report.created_interfaces == []
        assert interface_file.read_bytes() == before
        # the only init line anywhere is the constant's report-only skip: no
        # `incoming_init_disagreements` line was minted for an agreeing value
        assert not any("arrives with init" in line for line in report.skip_lines)

    def test_a_contradicting_target_value_is_a_conflict_naming_both(self, tmp_path: Path) -> None:
        """A contradicting target value is a conflict naming the element and both values; nothing is written."""
        source_root, arxml = _export_target_with_init(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        authored = yaml.safe_load((model_root / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
        authored["data_elements"][0]["init"] = 2.5
        _dump(model_root / "interfaces" / "ISpeed.yaml", authored)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="ISpeed") as exc_info:
            import_component(str(arxml), str(model_root))

        assert any("'speed'" in entry and "1.5" in entry and "2.5" in entry for entry in exc_info.value.conflicts), (
            exc_info.value.conflicts
        )
        assert files_of(model_root) == before

    def test_the_constants_are_report_only_skips_with_the_import_reason(self, tmp_path: Path) -> None:
        """The exported constants are report-only skips, not a lossy import, and say where the value went.

        ``CONSTANT-SPECIFICATION`` is a ``DEFERRED_ELEMENT_TAGS`` member.
        """
        _source_root, arxml = _export_target_with_init(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        constant_lines = [line for line in report.skip_lines if init_constant_name("ISpeed", "speed") in line]
        assert len(constant_lines) == 1, report.skip_lines
        assert "CONSTANT-SPECIFICATION is not an SWCraft element" in constant_lines[0]
        assert "imported onto that element's `init`" in constant_lines[0]


class TestAWideTargetInitIsDescribedRatherThanCrashingTheConflict:
    """The conflict line renders the target's authored value through ``rendering.render_model_scalar``.

    A hand-authored ``init: 0x<4000 f>`` loads (``int(v, 16)`` is exempt from
    ``sys.get_int_max_str_digits()``), and a bare ``repr`` raises ``ValueError`` while formatting the refusal, reaching
    the CLI as a traceback.

    Does not reach: the substitution itself (`test_rendering.py`'s), and that such a
    model still cannot be written (`test_validator_init.py`'s): the import refuses
    before any write here.
    """

    #: The 4 000 hexadecimal digits `test_validator_init.py` uses, as TEXT:
    #: ``yaml.safe_dump`` would raise on the integer, and the file stands for a hand-authored one.
    WIDE = "0x" + "f" * 4000
    WIDE_BITS = 16000

    #: A value ``yaml.safe_dump`` is forced to quote (``@`` is a reserved
    #: indicator), so the substitution below cannot collide with anything the
    #: seeded document already contains.
    _PLACEHOLDER = "@WIDE@"

    def _target_with_a_wide_init(self, tmp_path: Path, incoming: str) -> tuple[Path, Path]:
        source_root, arxml = _export_target(tmp_path)
        _inject_element_init_value(arxml, "speed", incoming)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)

        interface_file = model_root / "interfaces" / "ISpeed.yaml"
        authored = yaml.safe_load(interface_file.read_text(encoding="utf-8"))
        authored["data_elements"][0]["init"] = self._PLACEHOLDER
        _dump(interface_file, authored)
        text = interface_file.read_text(encoding="utf-8")
        assert self._PLACEHOLDER in text
        for spelling in (f"'{self._PLACEHOLDER}'", f'"{self._PLACEHOLDER}"', self._PLACEHOLDER):
            text = text.replace(spelling, self.WIDE)
        interface_file.write_text(text, encoding="utf-8", newline="\n")
        return project, arxml

    def test_the_target_really_carries_the_unprintable_value(self, tmp_path: Path) -> None:
        """Premise: the authored file loads as an integer whose own ``repr`` raises."""
        project, _arxml = self._target_with_a_wide_init(tmp_path, incoming="255")

        interface_file = project / "model" / "interfaces" / "ISpeed.yaml"
        loaded = yaml.safe_load(interface_file.read_text(encoding="utf-8"))["data_elements"][0]["init"]

        assert isinstance(loaded, int)
        assert loaded.bit_length() == self.WIDE_BITS
        with pytest.raises(ValueError, match="Exceeds the limit"):
            repr(loaded)

    def test_the_conflict_is_reported_with_the_value_described(self, tmp_path: Path) -> None:
        project, arxml = self._target_with_a_wide_init(tmp_path, incoming="255")
        model_root = project / "model"
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="ISpeed") as exc_info:
            import_component(str(arxml), str(model_root))

        assert any(
            "'speed'" in entry
            and "255" in entry
            and f"{self.WIDE_BITS} bits" in entry
            and "too wide to render" in entry
            for entry in exc_info.value.conflicts
        )
        assert files_of(model_root) == before

    def test_an_ordinary_target_value_is_still_printed_as_the_number(self, tmp_path: Path) -> None:
        """The control: an ordinary target init is still printed as its digits."""
        source_root, arxml = _export_target(tmp_path)
        _inject_element_init_value(arxml, "speed", "255")
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        interface_file = model_root / "interfaces" / "ISpeed.yaml"
        authored = yaml.safe_load(interface_file.read_text(encoding="utf-8"))
        authored["data_elements"][0]["init"] = 42
        _dump(interface_file, authored)

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(str(arxml), str(model_root))

        assert any("42" in entry and "too wide to render" not in entry for entry in exc_info.value.conflicts)


# dry_run: accurate report, zero writes


class TestDryRun:
    def test_dry_run_writes_nothing_and_reports_accurately(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        dry_report = import_component(str(arxml), str(project / "model"), dry_run=True)

        assert dry_report.dry_run is True
        assert dry_report.swc == "TargetSwc"
        assert dry_report.swc_action == "created"
        assert dry_report.created_interfaces == ["ICalib", "ISpeed"]
        assert dry_report.created_data_types == _CLOSURE_TYPES_IN_SOURCE_ORDER
        assert dry_report.written_paths == []
        assert files_of(project / "model") == before
        assert "DRY RUN" in dry_report.render()

        # the real run then performs exactly what the dry run reported
        real_report = import_component(str(arxml), str(project / "model"))
        assert real_report.created_interfaces == dry_report.created_interfaces
        assert real_report.created_data_types == dry_report.created_data_types
        assert real_report.swc_action == dry_report.swc_action
        # the closure's types + 2 interfaces + 1 SWC
        assert len(real_report.written_paths) == len(_CLOSURE_TYPE_NAMES) + 3


# Conflict detection, --update diff preview, atomic apply

# A TargetSwc that is valid SWCraft YAML but diverges from the exported one
# (fewer ports, no runnables) — the update-eligible collision fixture.
_DIVERGENT_TARGET_SWC = {
    "name": "TargetSwc",
    "type": "application",
    "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
}


def _seed_identical_shared_defs(source_root: str, model_root: Path) -> None:
    """Copy the component's shared interfaces/data types into the target, leaving the SWC the only decision."""
    for name in ("ISpeed", "ICalib"):
        target = model_root / "interfaces" / f"{name}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((Path(source_root) / "interfaces" / f"{name}.yaml").read_bytes())
    for name in _CLOSURE_TYPE_NAMES:
        target = model_root / _closure_type_storage_name(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((Path(source_root) / _closure_type_storage_name(name)).read_bytes())


class TestSharedDefinitionConflicts:
    @pytest.mark.parametrize("update", [False, True], ids=["default", "update=True"])
    @pytest.mark.parametrize(
        ("rel_path", "divergent", "conflict_name"),
        [
            # A base type and a value type may share a name, so the divergent target
            # base type is the closure's own `float32_base`.
            pytest.param(
                model_element_storage_name("float32_base", kind="base_type"),
                {"name": "float32_base", "kind": "base_type", "size": 16, "encoding": "NONE"},
                "float32_base",
                id="data_type",
            ),
            pytest.param(
                "interfaces/ISpeed.yaml",
                {
                    "name": "ISpeed",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "speed", "implementation_type": "float32"}],
                },
                "ISpeed",
                id="interface",
            ),
        ],
    )
    def test_divergent_shared_definition_always_rejects(
        self,
        tmp_path: Path,
        rel_path: str,
        divergent: dict[str, Any],
        conflict_name: str,
        update: bool,
    ) -> None:
        """A divergent shared interface/data type rejects the whole import, ``update=True`` included.

        Updating a shared definition needs impact analysis over every consumer, so there
        is no per-import override.
        """
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _dump(model_root / rel_path, divergent)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match=conflict_name) as exc_info:
            import_component(str(arxml), str(project / "model"), update=update)

        assert any(conflict_name in entry for entry in exc_info.value.conflicts)
        assert files_of(model_root) == before


class TestSwcUpdate:
    def test_divergent_swc_rejected_by_default(self, tmp_path: Path) -> None:
        """The imported SWC's own file already exists and diverges: rejected by default.

        It is rejected unless the caller explicitly opts into ``update=True``."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _DIVERGENT_TARGET_SWC)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="TargetSwc") as exc_info:
            import_component(str(arxml), str(project / "model"))

        assert any("TargetSwc" in entry for entry in exc_info.value.conflicts)
        assert files_of(model_root) == before

    def test_update_replaces_only_the_swc_own_file(self, tmp_path: Path) -> None:
        """``update=True`` writes only the SWC's own file, leaving every other file byte-identical.

        The path is the one the target scan recorded, never a re-derived ``swc/components/`` one.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        existing_swc_file = model_root / "swc" / "body" / "TargetSwc.yaml"
        _dump(existing_swc_file, _DIVERGENT_TARGET_SWC)
        before = files_of(model_root)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        assert report.written_paths == [str(existing_swc_file)]
        assert not (model_root / "swc" / "components" / "TargetSwc.yaml").exists()
        # the replaced file now round-trips Pydantic-equal to the source SWC
        source_swcs = {s.name: s for s in read_swc_model(source_root)}
        target_swcs = read_swc_model(str(model_root))
        assert [s.name for s in target_swcs] == ["TargetSwc"]
        assert target_swcs[0] == source_swcs["TargetSwc"]
        # only that one file changed; nothing appeared or disappeared
        after = files_of(model_root)
        assert set(after) == set(before)
        changed = {rel for rel in before if before[rel] != after[rel]}
        assert changed == {existing_swc_file.relative_to(model_root).as_posix()}

    def test_update_report_includes_semantic_diff_preview(self, tmp_path: Path) -> None:
        """An ``update=True`` replacement carries a semantic diff preview of what changed on that SWC."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _DIVERGENT_TARGET_SWC)

        report = import_component(str(arxml), str(project / "model"), update=True)

        preview = report.swc_diff_preview
        assert "~ TargetSwc" in preview
        # The preview renders the item that appeared, not both whole lists; asserting
        # on the field names alone would stay green if the detail were lost.
        assert "ports:\n        + rCalib (added)" in preview
        assert "runnables:\n        + TargetRunnable (added)" in preview
        assert "pSpeed" not in preview, "the port both sides share is restated"
        rendered = report.render()
        assert "diff preview" in rendered
        assert "~ TargetSwc" in rendered

    def test_no_diff_preview_without_an_update(self, tmp_path: Path) -> None:
        """A plain create carries no diff preview.

        The preview exists only for the explicit replacement path."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        assert report.swc_diff_preview == ""
        assert "diff preview" not in report.render()

    def test_shared_conflict_aborts_even_with_update_eligible_swc(self, tmp_path: Path) -> None:
        """A shared conflict aborts the import even beside an update-eligible SWC, ``update=True`` included."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        # A base type named like a value type is no conflict, so the divergent shared
        # definition is a base type of the closure's own name.
        write_model_element(model_root, {"name": "float32_base", "kind": "base_type", "size": 16, "encoding": "NONE"})
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _DIVERGENT_TARGET_SWC)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="float32_base") as exc_info:
            import_component(str(arxml), str(project / "model"), update=True)

        assert any("float32_base" in entry for entry in exc_info.value.conflicts)
        assert files_of(model_root) == before


class TestAtomicApply:
    def test_rename_failure_rolls_back_every_touched_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rename failure mid-``os.replace`` loop leaves the model root exactly as it was.

        The plan holds new files and one ``--update``-replaced file: its bytes are
        restored, every new file is absent, no ``.tmp`` is left, over the full listing.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        # one existing file the update will replace (the SWC, divergent) --
        # shared defs stay absent so the plan also creates brand-new files
        existing_swc_file = model_root / "swc" / "body" / "TargetSwc.yaml"
        _dump(existing_swc_file, _DIVERGENT_TARGET_SWC)
        before = files_of(model_root)

        dirs_before = _dir_snapshot(model_root)
        real_replace = os.replace
        calls = {"count": 0}
        replaced_dsts: list[str] = []

        def flaky_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
            calls["count"] += 1
            if calls["count"] == 3:
                raise PermissionError("injected rename failure")
            real_replace(src, dst, *args, **kwargs)
            replaced_dsts.append(str(dst))

        monkeypatch.setattr("os.replace", flaky_replace)
        with pytest.raises(PermissionError, match="injected rename failure"):
            import_component(str(arxml), str(project / "model"), update=True)
        monkeypatch.undo()

        # some replaces really happened before the injected failure, so the
        # rollback (not a never-started apply) is what's being asserted
        assert calls["count"] == 3
        # The update-replaced SWC was replaced before the failure, so the restore-prior-bytes
        # branch ran; a write-plan order that leaves it unreplaced fails here.
        assert str(existing_swc_file) in replaced_dsts
        after = files_of(model_root)
        assert after == before  # full listing + bytes, nothing else
        assert existing_swc_file.read_bytes() == before[existing_swc_file.relative_to(model_root).as_posix()]
        assert list(model_root.rglob("*.tmp")) == []
        # no brand-new (now empty) directories left behind either
        assert _dir_snapshot(model_root) == dirs_before

    def test_staged_write_failure_cleans_tmp_files_and_created_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure while staging the ``.tmp`` writes removes every staged sibling and every new directory."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        before = files_of(model_root)
        dirs_before = _dir_snapshot(model_root)

        real_write_text = Path.write_text
        calls = {"count": 0}

        def flaky_write_text(self: Path, *args: Any, **kwargs: Any) -> int:
            if self.name.endswith(".tmp"):
                calls["count"] += 1
                if calls["count"] == 3:
                    raise OSError("injected staged-write failure")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", flaky_write_text)
        with pytest.raises(OSError, match="injected staged-write failure"):
            import_component(str(arxml), str(project / "model"))
        monkeypatch.undo()

        assert calls["count"] == 3  # two staged writes really happened first
        assert list(model_root.rglob("*.tmp")) == []
        assert files_of(model_root) == before
        assert _dir_snapshot(model_root) == dirs_before
        assert not (model_root / "swc" / "components").exists()


# Direct enum-typed interface references: SWCraft's own writer emits them, so
# the paired importer accepts them too.


class TestDirectEnumInterface:
    """An enumeration a port speaks is paired by a mapping set the component names.

    Without the set the component is ``rule:data-type.application-type-unmapped``'s and
    its export is refused, so the round trip is claimed on a model the gate accepts.
    """

    def _build_enum_model(self, tmp_path: Path) -> str:
        model_root = tmp_path / "enum_model"
        write_model_element(
            model_root,
            {
                "name": "Gear_T",
                "kind": "enum",
                "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}],
            },
        )
        write_model_element(
            model_root,
            {"name": "GearByte", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        )
        write_model_element(model_root, {"name": "GearRaw", "kind": "value_type", "base": "GearByte"})
        write_model_element(
            model_root,
            {"name": "GearMaps", "maps": [{"application": "Gear_T", "implementation": "GearRaw"}]},
            layer="mapping_sets",
        )
        _dump(
            model_root / "interfaces" / "IGear.yaml",
            {
                "name": "IGear",
                "kind": "sender_receiver",
                "data_elements": [{"name": "gear", "application_type": "Gear_T"}],
            },
        )
        _dump(
            model_root / "interfaces" / "IGearCmd.yaml",
            {
                "name": "IGearCmd",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "RequestGear",
                        "arguments": [{"name": "target", "direction": "in", "application_type": "Gear_T"}],
                    }
                ],
            },
        )
        _dump(
            model_root / "swc" / "body" / "GearSwc.yaml",
            {
                "name": "GearSwc",
                "type": "application",
                "ports": [
                    {"name": "pGear", "direction": "provide", "interface": "IGear"},
                    {"name": "rGearCmd", "direction": "require", "interface": "IGearCmd"},
                ],
                "data_type_mappings": ["GearMaps"],
                "handle_termination_and_restart": "no_support",
            },
        )
        return str(model_root)

    def test_direct_enum_data_element_and_argument_round_trip(self, tmp_path: Path) -> None:
        """An SWC with directly enum-typed SR data element and CS argument round-trips.

        An SWC whose SR data element AND CS argument are enum-typed
        directly (no record wrapper) exports AND imports — Pydantic-equal
        round trip, the enum arrives as an enum."""
        source_root = self._build_enum_model(tmp_path)
        arxml = tmp_path / "GearSwc.arxml"
        export_component(source_root, "GearSwc", str(arxml))
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        assert report.created_interfaces == ["IGear", "IGearCmd"]
        assert sorted(report.created_data_types) == ["GearByte", "GearRaw", "Gear_T"]
        assert report.created_mapping_sets == ["GearMaps"]
        target_root = str(project / "model")
        source_ifaces = {i.name: i for i in read_interfaces(source_root)}
        target_ifaces = {i.name: i for i in read_interfaces(target_root)}
        assert target_ifaces["IGear"] == source_ifaces["IGear"]
        assert target_ifaces["IGearCmd"] == source_ifaces["IGearCmd"]
        source_types = {d.name: d for d in _both_type_layers(source_root)}
        target_types = {d.name: d for d in _both_type_layers(target_root)}
        assert target_types["Gear_T"] == source_types["Gear_T"]
        source_swcs = {s.name: s for s in read_swc_model(source_root)}
        assert read_swc_model(target_root)[0] == source_swcs["GearSwc"]


# ARXML-representable projection: fields the generated ARXML cannot carry
# (description, a value type's unit, authored port order) never turn an
# identical target definition into a conflict.


class TestRepresentableProjection:
    def test_target_descriptions_units_and_limits_do_not_conflict(self, tmp_path: Path) -> None:
        """Identical shared defs carrying metadata ARXML cannot represent are a silent, byte-identical reuse.

        The metadata: an interface description, a value type's unit, data type descriptions.
        A value type's ``value_min``/``value_max`` are written, so compared
        (``test_value_type_range.py``). A ``native_declaration`` equal to the type's name
        is ``TestANativeDeclarationEqualToTheTypeNameSurvivesTheExchange``'s.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        # layer authored, ARXML-unrepresentable metadata on top
        _dump(
            model_root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "description": "Vehicle speed (authored by the integrator)",
                "data_elements": [
                    {"name": "speed", "implementation_type": "Speed_T"},
                    {"name": "pose", "application_type": "Pose_T"},
                ],
            },
        )
        write_model_element(
            model_root,
            {
                "name": "Speed_T",
                "kind": "value_type",
                "base": "float32_base",
                "unit": "km/h",
                "description": "Speed in km/h",
            },
        )
        # The target holds the source's split -- a value type `float32` over the
        # base type `float32_base` -- with its own prose.
        write_model_element(
            model_root,
            {"name": "float32", "kind": "value_type", "base": "float32_base", "description": "IEEE754 single"},
        )
        write_model_element(
            model_root,
            {
                "name": "float32_base",
                "kind": "base_type",
                "size": 32,
                "encoding": "IEEE754",
                # The writer emits the declared value and derives none, so the
                # authored value that is no divergence is the source's own.
                "native_declaration": "float32",
                "description": "IEEE754 single",
            },
        )
        before = files_of(model_root)

        report = import_component(str(arxml), str(project / "model"))

        assert report.reused_interfaces == ["ICalib", "ISpeed"]
        assert report.reused_data_types == _CLOSURE_TYPES_IN_SOURCE_ORDER
        assert report.created_interfaces == []
        assert report.created_data_types == []
        after = files_of(model_root)
        # the authored files (metadata included) were never rewritten
        for rel in (
            "interfaces/ISpeed.yaml",
            "data_types/implementation/Speed_T.yaml",
            "data_types/implementation/float32.yaml",
        ):
            assert after[rel] == before[rel]

    def test_swc_with_metadata_and_require_first_port_order_is_unchanged(self, tmp_path: Path) -> None:
        """A target SWC differing only in descriptions and require-first port order compares ``unchanged``.

        No forced ``--update`` strips the authored metadata.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(
            model_root / "swc" / "body" / "TargetSwc.yaml",
            {
                "name": "TargetSwc",
                "type": "application",
                "description": "authored description",
                "ports": [
                    # require port FIRST -- the writer always emits P-ports
                    # before R-ports (`_ordered_ports()`)
                    {
                        "name": "rCalib",
                        "direction": "require",
                        "interface": "ICalib",
                        "description": "calibration input",
                    },
                    {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
                ],
                "runnables": [
                    {
                        "name": "TargetRunnable",
                        "min_start_interval": 0.005,
                        "events": [{"type": "timing", "period": 0.01}, {"type": "init"}],
                    }
                ],
                "handle_termination_and_restart": "no_support",
            },
        )
        before = files_of(model_root)

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(model_root) == before

    def test_sub_millisecond_timing_round_trips_exactly(self, tmp_path: Path) -> None:
        """Sub-millisecond timing is written as itself, so re-importing the unchanged export is a no-op."""
        source_root = _build_source_model(tmp_path)
        swc_file = Path(source_root) / "swc" / "body" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"][0]["events"][0]["period"] = 0.0125
        data["runnables"][0]["min_start_interval"] = 0.0004
        _dump(swc_file, data)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        text = arxml.read_text(encoding="utf-8")
        assert "<PERIOD>0.0125</PERIOD>" in text
        assert "<MINIMUM-START-INTERVAL>0.0004</MINIMUM-START-INTERVAL>" in text
        before = files_of(Path(source_root))

        report = import_component(str(arxml), source_root)

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(Path(source_root)) == before

    @pytest.mark.parametrize(
        ("field", "exported", "target"),
        [("period", 0.0125, 0.012), ("min_start_interval", 0.0125, 0.012), ("period", 0.0004, 0.001)],
    )
    def test_a_sub_millisecond_timing_difference_is_a_divergence(
        self, tmp_path: Path, field: str, exported: float, target: float
    ) -> None:
        """A sub-millisecond timing difference is a divergence: the comparison does not quantize timing.

        A millisecond projection would read 0.0125 s and 0.012 s as equal and call the import a no-op.
        """
        source_root = _build_source_model(tmp_path)
        swc_file = Path(source_root) / "swc" / "body" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))

        def set_timing(value: float) -> None:
            runnable = data["runnables"][0]
            if field == "period":
                runnable["events"][0]["period"] = value
            else:
                runnable["min_start_interval"] = value
            _dump(swc_file, data)

        set_timing(exported)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        set_timing(target)
        before = files_of(Path(source_root))

        with pytest.raises(ComponentImportConflictError, match="TargetSwc") as exc_info:
            import_component(str(arxml), source_root)

        assert any("TargetSwc" in entry for entry in exc_info.value.conflicts)
        assert files_of(Path(source_root)) == before


# dry_run x update / conflicts


class TestDryRunWithUpdate:
    def test_dry_run_with_update_reports_replacement_without_writing(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _DIVERGENT_TARGET_SWC)
        before = files_of(model_root)

        report = import_component(str(arxml), str(project / "model"), update=True, dry_run=True)

        assert report.dry_run is True
        assert report.swc_action == "updated"
        assert "~ TargetSwc" in report.swc_diff_preview
        assert report.written_paths == []
        assert files_of(model_root) == before

    def test_dry_run_with_divergent_shared_def_still_raises(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        write_model_element(model_root, {"name": "float32_base", "kind": "base_type", "size": 16, "encoding": "NONE"})
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="float32_base"):
            import_component(str(arxml), str(project / "model"), dry_run=True)

        assert files_of(model_root) == before


# Malformed target-project YAML


class TestMalformedTargetYaml:
    def test_malformed_target_yaml_aborts_with_typed_error(self, tmp_path: Path) -> None:
        """An unparseable target YAML aborts with the reader's typed refusal naming its storage name."""
        from swcraft_tools.model_state import ElementParseError

        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        broken = model_root / "data_types" / "implementation" / "broken.yaml"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("name: [unclosed\n  ] : nope\n", encoding="utf-8")
        before = files_of(model_root)

        with pytest.raises(
            ElementParseError, match=r"^Invalid implementation type YAML in data_types/implementation/broken\.yaml: "
        ) as exc_info:
            import_component(str(arxml), str(project / "model"))

        assert str(broken) not in str(exc_info.value)
        assert exc_info.value.render(full_path=True).startswith(f"Invalid implementation type YAML in {broken}: ")
        assert files_of(model_root) == before


# SWComponent.memory across the exchange: a budget the ARXML cannot carry,
# excluded from the divergence comparison like `description` and never taken
# from the source on `--update` (the imported absence means "unknown", not "zero").


_TARGET_SWC_WITH_MEMORY = {
    "name": "TargetSwc",
    "type": "application",
    "ports": [
        {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
        {"name": "rCalib", "direction": "require", "interface": "ICalib"},
    ],
    "runnables": [
        {
            "name": "TargetRunnable",
            "min_start_interval": 0.005,
            "events": [{"type": "timing", "period": 0.01}, {"type": "init"}],
        }
    ],
    "memory": {"RAM": 2048, "ROM": 8192},
}


class TestMemoryBudgetExchange:
    def test_memory_budget_alone_is_not_a_divergence(self, tmp_path: Path) -> None:
        """A `memory` budget alone never makes the round trip divergent, which would force `--update`."""
        source_root = _build_source_model(tmp_path)
        swc_file = Path(source_root) / "swc" / "body" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["memory"] = {"RAM": 2048, "ROM": 8192}
        _dump(swc_file, data)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        before = files_of(Path(source_root))

        report = import_component(str(arxml), source_root)

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(Path(source_root)) == before

    def test_update_preserves_the_target_memory_budget(self, tmp_path: Path) -> None:
        """`--update` keeps the target's budget: the imported absence means "unknown", not "zero"."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        # divergent in ports (so --update is genuinely needed) AND carrying a
        # memory budget the source ARXML knows nothing about
        divergent = copy.deepcopy(_TARGET_SWC_WITH_MEMORY)
        divergent["ports"] = [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}]
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        updated = read_swc_model(str(model_root))[0]
        assert updated.memory == {"RAM": 2048, "ROM": 8192}
        # everything else really was replaced by the imported definition
        assert [p.name for p in updated.ports] == ["pSpeed", "rCalib"]
        # The carry happens before the diff preview is computed, or the preview
        # advertises a destructive `memory: {...} -> {}` change that is not happening.
        assert "memory" not in report.swc_diff_preview, report.swc_diff_preview

    def test_update_report_states_the_budget_was_preserved(self, tmp_path: Path) -> None:
        """The report records the carried budget and says it came from the target project."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = copy.deepcopy(_TARGET_SWC_WITH_MEMORY)
        divergent["ports"] = [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}]
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.preserved_memory == {"RAM": 2048, "ROM": 8192}
        rendered = report.render()
        assert "preserved" in rendered
        assert "RAM=2048" in rendered
        assert "ROM=8192" in rendered

    def test_update_without_a_target_budget_reports_nothing_preserved(self, tmp_path: Path) -> None:
        """No budget in the target project: the update reports nothing preserved.

        There is nothing to carry and nothing to say about it."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _DIVERGENT_TARGET_SWC)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        assert report.preserved_memory == {}
        assert "preserved" not in report.render()
        assert read_swc_model(str(model_root))[0].memory == {}

    def test_created_swc_has_no_memory_budget(self, tmp_path: Path) -> None:
        """A brand-new SWC in a fresh project has no prior budget to carry.

        The created file must simply omit `memory`, not invent one."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        assert report.preserved_memory == {}
        created = project / "model" / "swc" / "components" / "TargetSwc.yaml"
        assert "memory" not in yaml.safe_load(created.read_text(encoding="utf-8"))


# SWComponent.asil across the exchange: like `memory`, the writer keeps the
# level out of ARXML, so a level alone is no divergence and `--update` carries
# the target's declaration.


_TARGET_SWC_WITH_ASIL = {
    "name": "TargetSwc",
    "type": "application",
    "ports": [
        {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
        {"name": "rCalib", "direction": "require", "interface": "ICalib"},
    ],
    "runnables": [
        {
            "name": "TargetRunnable",
            "min_start_interval": 0.005,
            "events": [{"type": "timing", "period": 0.01}, {"type": "init"}],
        }
    ],
    "asil": "D",
}


class TestAsilExchange:
    def test_asil_alone_is_not_a_divergence(self, tmp_path: Path) -> None:
        """An `asil` alone never makes the round trip divergent: no level is written into ARXML."""
        source_root = _build_source_model(tmp_path)
        swc_file = Path(source_root) / "swc" / "body" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["asil"] = "D"
        _dump(swc_file, data)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        assert "asil" not in arxml.read_text(encoding="utf-8").lower()
        before = files_of(Path(source_root))

        report = import_component(str(arxml), source_root)

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(Path(source_root)) == before

    def test_update_preserves_the_target_asil(self, tmp_path: Path) -> None:
        """`--update` keeps the target's `asil`: the imported absence means "unknown".

        The loss would be invisible to the gate: a component declaring nothing derives its
        level and `safety.component-below-requirements` stops firing.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        # divergent in ports (so --update is genuinely needed) AND carrying a
        # declared level the source ARXML knows nothing about
        divergent = copy.deepcopy(_TARGET_SWC_WITH_ASIL)
        divergent["ports"] = [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}]
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        updated = read_swc_model(str(model_root))[0]
        assert updated.asil == "D"
        # everything else really was replaced by the imported definition
        assert [p.name for p in updated.ports] == ["pSpeed", "rCalib"]
        # The carry happens before the diff preview is computed, or the preview
        # advertises a destructive `asil: 'D' -> None` change (`diff_layer`
        # compares the whole `model_dump`).
        assert "asil" not in report.swc_diff_preview, report.swc_diff_preview

    def test_update_report_states_the_asil_was_preserved(self, tmp_path: Path) -> None:
        """The report records the carried level and says it came from the target project."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = copy.deepcopy(_TARGET_SWC_WITH_ASIL)
        divergent["ports"] = [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}]
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.preserved_asil == "D"
        rendered = report.render()
        assert "ASIL" in rendered
        assert "preserved" in rendered

    def test_update_without_a_target_asil_reports_nothing_preserved(self, tmp_path: Path) -> None:
        """No declaration in the target means nothing to carry and nothing to say."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _DIVERGENT_TARGET_SWC)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        assert report.preserved_asil is None
        assert "ASIL" not in report.render()
        updated = read_swc_model(str(model_root))[0]
        assert updated.asil is None
        written = (model_root / "swc" / "body" / "TargetSwc.yaml").read_text(encoding="utf-8")
        assert "asil" not in written

    def test_update_preserves_a_declared_qm(self, tmp_path: Path) -> None:
        """`QM` is a value of the enum and a real declaration, distinguishable from an omission.

        So it is carried, and the file keeps saying it."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = copy.deepcopy(_TARGET_SWC_WITH_ASIL)
        divergent["asil"] = "QM"
        divergent["ports"] = [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}]
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.preserved_asil == "QM"
        assert read_swc_model(str(model_root))[0].asil == "QM"

    def test_created_swc_has_no_asil(self, tmp_path: Path) -> None:
        """A brand-new SWC in a fresh project has no prior declaration to carry.

        The created file must simply omit `asil`, not invent one."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        assert report.preserved_asil is None
        created = project / "model" / "swc" / "components" / "TargetSwc.yaml"
        assert "asil" not in yaml.safe_load(created.read_text(encoding="utf-8"))


# A runnable's budget (`wcet`, `wcet_type`) across the exchange: the export
# carries it in the component's `SWC-TIMING`, the comparison takes it as it
# stands and the file's budget wins. A file runnable stating no `wcet` (an
# export from before the writer emitted the budget) keeps the target's pair,
# carried before the comparison, so that silence is no difference; the carry
# is reported only when the file is written.


def _with_cost(document: dict[str, Any], cost: str = "250us", wcet_type: str = "gross") -> dict[str, Any]:
    costed = copy.deepcopy(document)
    costed["runnables"][0]["wcet"] = cost
    costed["runnables"][0]["wcet_type"] = wcet_type
    return costed


def _budgeted_source(tmp_path: Path, cost: str = "250us", wcet_type: str = "gross") -> tuple[str, Path]:
    """The source model with ``TargetRunnable`` budgeted, and the path of its ``TargetSwc.yaml``."""
    source_root = _build_source_model(tmp_path)
    swc_file = Path(source_root) / "swc" / "body" / "TargetSwc.yaml"
    _dump(swc_file, _with_cost(yaml.safe_load(swc_file.read_text(encoding="utf-8")), cost, wcet_type))
    return source_root, swc_file


def _strip_swc_timing(arxml: Path, tmp_path: Path) -> Path:
    """*arxml* with its one ``SWC-TIMING`` removed: an export from before the writer emitted a budget."""
    text = arxml.read_text(encoding="utf-8")
    stripped, count = re.subn(r"\s*<SWC-TIMING>.*?</SWC-TIMING>", "", text, flags=re.DOTALL)
    assert count == 1, count
    assert "TIMING" not in stripped.replace("TIMING-EVENT", "")
    target = tmp_path / "TargetSwc_without_timing.arxml"
    target.write_text(stripped, encoding="utf-8", newline="\n")
    return target


class TestRunnableCostExchange:
    def test_a_cost_alone_is_not_a_divergence(self, tmp_path: Path) -> None:
        """The export carries the budget, and reading it back diverges nothing.

        The component's ``SWC-TIMING`` carries it (``CSE-CODE`` 0 is 1 us, TPS GST R4.3.0
        Table 4.92) and the importer reads it back to the same text.
        """
        source_root = _build_source_model(tmp_path)
        swc_file = Path(source_root) / "swc" / "body" / "TargetSwc.yaml"
        _dump(swc_file, _with_cost(yaml.safe_load(swc_file.read_text(encoding="utf-8"))))
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        exported = arxml.read_text(encoding="utf-8")
        assert "<SHORT-NAME>TargetSwc_Timing</SHORT-NAME>" in exported
        assert "<CSE-CODE>0</CSE-CODE>" in exported and "<CSE-CODE-FACTOR>250</CSE-CODE-FACTOR>" in exported
        before = files_of(Path(source_root))

        report = import_component(str(arxml), source_root)

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(Path(source_root)) == before

    @pytest.mark.parametrize("update", [False, True], ids=["default", "update=True"])
    def test_an_export_without_budgets_is_no_difference_and_writes_nothing(self, tmp_path: Path, update: bool) -> None:
        """A file runnable stating no ``wcet`` keeps the target's pair, carried before the comparison.

        The stripped export compares ``unchanged`` with and without ``--update``: no file
        rewritten and no ``WCET preserved`` line, which names a carry only where a file is written.
        """
        source_root, _ = _budgeted_source(tmp_path)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        stripped = _strip_swc_timing(arxml, tmp_path)
        before = files_of(Path(source_root))

        report = import_component(str(stripped), source_root, update=update)

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert report.preserved_costs == {}
        assert "WCET" not in report.render()
        assert files_of(Path(source_root)) == before

    def test_the_files_budget_differing_from_the_targets_is_a_difference(self, tmp_path: Path) -> None:
        """The file's budget wins: a different one is a conflict, and under ``--update`` it is written."""
        source_root, swc_file = _budgeted_source(tmp_path, "850ns", "net")
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        _dump(swc_file, _with_cost(yaml.safe_load(swc_file.read_text(encoding="utf-8")), "1200us", "gross"))
        before = files_of(Path(source_root))

        with pytest.raises(ComponentImportConflictError, match="SWC 'TargetSwc' already exists"):
            import_component(str(arxml), source_root)
        assert files_of(Path(source_root)) == before

        report = import_component(str(arxml), source_root, update=True)

        assert report.swc_action == "updated"
        assert "wcet" in report.swc_diff_preview, report.swc_diff_preview
        assert report.preserved_costs == {}
        assert "WCET" not in report.render()
        updated = next(s for s in read_swc_model(source_root) if s.name == "TargetSwc")
        assert (updated.runnables[0].wcet, updated.runnables[0].wcet_type) == ("850ns", "net")

    def test_a_runnable_only_the_file_has_takes_the_files_budget(self, tmp_path: Path) -> None:
        """A runnable the target does not declare has no budget to keep: the file's is written."""
        source_root, _ = _budgeted_source(tmp_path, "3ms", "gross")
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _DIVERGENT_TARGET_SWC)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert report.preserved_costs == {}
        assert "WCET" not in report.render()
        updated = read_swc_model(str(model_root))[0]
        assert (updated.runnables[0].wcet, updated.runnables[0].wcet_type) == ("3ms", "gross")

    def test_update_preserves_and_reports_the_target_runnables_cost(self, tmp_path: Path) -> None:
        """An export with no budget and another change: ``--update`` writes the target's pair and names it.

        The port difference makes the action ``updated``, the one case the ``WCET preserved``
        line reports.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = _with_cost(_TARGET_SWC_WITH_ASIL)
        divergent["ports"] = [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}]
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        updated = read_swc_model(str(model_root))[0]
        assert (updated.runnables[0].wcet, updated.runnables[0].wcet_type) == ("250us", "gross")
        assert [p.name for p in updated.ports] == ["pSpeed", "rCalib"]
        assert "wcet" not in report.swc_diff_preview, report.swc_diff_preview
        assert report.preserved_costs == {"TargetRunnable": "250us"}
        assert "WCET preserved from the target project (not carried by the source ARXML): TargetRunnable=250us" in (
            report.render()
        )

    def test_a_composition_claiming_the_timing_name_refuses_the_import_with_nothing_written(
        self, tmp_path: Path
    ) -> None:
        """The budgeted component emits ``TargetSwc_Timing``; a composition claiming it is refused first."""
        source_root, _ = _budgeted_source(tmp_path)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        project = init_project(tmp_path / "proj")
        _dump(
            project / "model" / "compositions" / "TargetSwc_Timing.yaml",
            {"name": "TargetSwc_Timing", "title": "A composition claiming the generated timing name"},
        )
        before = files_of(project / "model")

        with pytest.raises(ReservedNameError, match="SWC-TIMING name auto-generated for 'TargetSwc'"):
            import_component(str(arxml), str(project / "model"))

        assert files_of(project / "model") == before

    def test_a_budget_carried_from_the_target_claims_the_timing_name_too(self, tmp_path: Path) -> None:
        """A budget carried from the target emits ``TargetSwc_Timing`` too; the guard judges it after the carry."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _with_cost(_TARGET_SWC_WITH_ASIL))
        _dump(
            model_root / "compositions" / "TargetSwc_Timing.yaml",
            {"name": "TargetSwc_Timing", "title": "A composition claiming the generated timing name"},
        )
        before = files_of(model_root)

        with pytest.raises(ReservedNameError, match="SWC-TIMING name auto-generated for 'TargetSwc'"):
            import_component(str(arxml), str(model_root), update=True)

        assert files_of(model_root) == before

    def test_the_same_runnables_budget_type_alone_differing_is_a_difference(self, tmp_path: Path) -> None:
        """``wcet_type`` is compared on its own: the same ``wcet`` with another type is a difference."""
        source_root, swc_file = _budgeted_source(tmp_path, "850ns", "net")
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        _dump(swc_file, _with_cost(yaml.safe_load(swc_file.read_text(encoding="utf-8")), "850ns", "gross"))
        before = files_of(Path(source_root))

        with pytest.raises(ComponentImportConflictError, match="SWC 'TargetSwc' already exists"):
            import_component(str(arxml), source_root)
        assert files_of(Path(source_root)) == before

        report = import_component(str(arxml), source_root, update=True)

        assert report.swc_action == "updated"
        assert "wcet_type" in report.swc_diff_preview, report.swc_diff_preview
        assert report.preserved_costs == {}
        updated = next(s for s in read_swc_model(source_root) if s.name == "TargetSwc")
        assert (updated.runnables[0].wcet, updated.runnables[0].wcet_type) == ("850ns", "net")

    def test_a_budgeted_runnable_the_file_removes_is_removed_with_its_budget(self, tmp_path: Path) -> None:
        """A target budget is carried by runnable name, so a runnable the file drops goes with its budget."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = _with_cost(_TARGET_SWC_WITH_ASIL)
        divergent["runnables"].append({**divergent["runnables"][0], "name": "GoneRunnable"})
        divergent["runnables"][0].pop("wcet")
        divergent["runnables"][0].pop("wcet_type")
        divergent["runnables"][1]["events"] = [{"type": "init"}]
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert report.preserved_costs == {}
        assert "WCET" not in report.render()
        updated = read_swc_model(str(model_root))[0]
        assert [(r.name, r.wcet, r.wcet_type) for r in updated.runnables] == [("TargetRunnable", None, None)]

    def test_update_without_a_target_cost_reports_nothing(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _DIVERGENT_TARGET_SWC)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        assert report.preserved_costs == {}
        assert "WCET" not in report.render()
        written = (model_root / "swc" / "body" / "TargetSwc.yaml").read_text(encoding="utf-8")
        assert "wcet" not in written


# The port's `measures` and the data element's `asil`, on the terms of `asil`
# and `memory`. A shared interface has no `--update` exit, so a rated element
# that made it diverge would block `swcraft import component` for every
# component sharing that interface.


_TARGET_SWC_WITH_MEASURES = {
    "name": "TargetSwc",
    "type": "application",
    "ports": [
        {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
        {
            "name": "rCalib",
            "direction": "require",
            "interface": "ICalib",
            "measures": {"Calib": "End-to-end protection profile 4."},
        },
    ],
    "runnables": [
        {
            "name": "TargetRunnable",
            "min_start_interval": 0.005,
            "events": [{"type": "timing", "period": 0.01}, {"type": "init"}],
        }
    ],
}


def _measures_of_port(model_root: Path, port_name: str) -> object:
    swc = read_swc_model(str(model_root))[0]
    for port in swc.ports:
        if port.name == port_name:
            return port.measures
    raise AssertionError(f"no port {port_name!r}")


class TestTheCoexistenceDeclarationsExchange:
    def test_measures_alone_are_not_a_divergence(self, tmp_path: Path) -> None:
        """A declared measure alone never makes the round trip divergent: no ARXML byte carries one."""
        source_root = _build_source_model(tmp_path)
        swc_file = Path(source_root) / "swc" / "body" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        for port in data["ports"]:
            if port["name"] == "rCalib":
                port["measures"] = {"Calib": "End-to-end protection profile 4."}
        _dump(swc_file, data)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        assert "measures" not in arxml.read_text(encoding="utf-8").lower()
        before = files_of(Path(source_root))

        report = import_component(str(arxml), source_root)

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(Path(source_root)) == before

    def test_a_rated_shared_interface_is_not_a_divergence_and_does_not_abort(self, tmp_path: Path) -> None:
        """A rated data element on a shared interface is reused, not a conflict.

        A shared interface has no `--update` exit: without the projection blanking `asil`
        the import would abort for every component sharing the interface.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        iface_file = model_root / "interfaces" / "ISpeed.yaml"
        iface = yaml.safe_load(iface_file.read_text(encoding="utf-8"))
        iface["data_elements"][0]["asil"] = "D"
        _dump(iface_file, iface)

        report = import_component(str(arxml), str(model_root))

        assert "ISpeed" in report.reused_interfaces
        assert yaml.safe_load(iface_file.read_text(encoding="utf-8"))["data_elements"][0]["asil"] == "D"

    def test_update_preserves_the_target_measures_and_reports_them(self, tmp_path: Path) -> None:
        """`memory`'s rule one level down, and never silent."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = copy.deepcopy(_TARGET_SWC_WITH_MEASURES)
        divergent["ports"][0] = {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}
        divergent["runnables"] = []
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert _measures_of_port(model_root, "rCalib") == {"Calib": "End-to-end protection profile 4."}
        assert report.preserved_measures == {"rCalib": {"Calib": "End-to-end protection profile 4."}}
        rendered = report.render()
        assert "MEASURES preserved" in rendered
        # Carried BEFORE the mandatory diff preview, or the preview advertises a
        # destructive change that is not happening.
        assert "measures" not in report.swc_diff_preview, report.swc_diff_preview

    def test_update_without_target_measures_reports_nothing(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", _DIVERGENT_TARGET_SWC)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.preserved_measures == {}
        assert report.dropped_measures == []
        assert "MEASURES" not in report.render()

    def test_a_port_the_export_no_longer_declares_carries_nothing_and_the_preview_says_so(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = copy.deepcopy(_TARGET_SWC_WITH_MEASURES)
        divergent["ports"].append(
            {
                "name": "rGone",
                "direction": "require",
                "interface": "ICalib",
                "measures": {"Calib": "Plausibility check."},
            }
        )
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(model_root), update=True)

        assert "rGone" not in [port.name for port in read_swc_model(str(model_root))[0].ports]
        assert report.preserved_measures == {"rCalib": {"Calib": "End-to-end protection profile 4."}}
        assert any("rGone" in line and "no longer declares it" in line for line in report.dropped_measures)
        assert "MEASURES NOT carried" in report.render()

    def test_a_port_the_export_flips_to_provide_carries_nothing_and_the_preview_says_so(self, tmp_path: Path) -> None:
        """The map is dropped and the drop is named.

        Carrying it there would mint `safety.measure-on-provide-port` in a
        file the user never wrote, so the map is dropped and the drop is named."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = copy.deepcopy(_TARGET_SWC_WITH_MEASURES)
        # The export declares `pSpeed` as a PROVIDE port; the target has it as a
        # require port carrying a measure, so the update flips it.
        divergent["ports"][0] = {
            "name": "pSpeed",
            "direction": "require",
            "interface": "ISpeed",
            "measures": {"Speed": "Range check."},
        }
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(model_root), update=True)

        assert _measures_of_port(model_root, "pSpeed") == {}
        assert "pSpeed" not in report.preserved_measures
        assert any(
            "pSpeed" in line and "flips it from 'require' to 'provide'" in line for line in report.dropped_measures
        )


# The target's trace links, carried across a replacement. An export has no
# place for a link (requirements are integrator-owned and never exported), so
# an imported component, port or runnable always says `satisfies: []`, meaning
# "unknown". The projections blank `satisfies` so a link never makes a round
# trip diverge; the replacement path carries the target's links by site.


def _linked_target() -> dict[str, object]:
    """``_TARGET_SWC_WITH_MEASURES`` with a link on the component, a port and the runnable."""
    linked = copy.deepcopy(_TARGET_SWC_WITH_MEASURES)
    linked["satisfies"] = ["REQ_SWC@1"]
    linked["ports"][1]["satisfies"] = ["REQ_PORT@2"]
    linked["runnables"][0]["satisfies"] = ["REQ_RUN@1"]
    return linked


class TestTheTraceLinksExchange:
    def test_a_linked_component_reimported_from_its_own_export_is_unchanged(self, tmp_path: Path) -> None:
        source_root = _build_source_model(tmp_path)
        swc_file = Path(source_root) / "swc" / "body" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["satisfies"] = ["REQ_SWC@1"]
        data["ports"][0]["satisfies"] = ["REQ_PORT@2"]
        data["runnables"][0]["satisfies"] = ["REQ_RUN@1"]
        _dump(swc_file, data)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        assert "REQ_" not in arxml.read_text(encoding="utf-8")
        before = files_of(Path(source_root))

        report = import_component(str(arxml), source_root)

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(Path(source_root)) == before

    def test_an_interface_carrying_links_is_reused_not_diverged(self, tmp_path: Path) -> None:
        """A shared interface has no ``--update`` exit: a link on it must never abort the import."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        iface_file = model_root / "interfaces" / "ISpeed.yaml"
        iface = yaml.safe_load(iface_file.read_text(encoding="utf-8"))
        iface["satisfies"] = ["REQ_SIGNAL@1"]
        _dump(iface_file, iface)

        report = import_component(str(arxml), str(model_root))

        assert "ISpeed" in report.reused_interfaces
        assert yaml.safe_load(iface_file.read_text(encoding="utf-8"))["satisfies"] == ["REQ_SIGNAL@1"]

    def test_update_keeps_the_components_a_ports_and_a_runnables_links(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = _linked_target()
        divergent["ports"][0] = {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        (swc,) = read_swc_model(str(model_root))
        assert swc.satisfies == ["REQ_SWC@1"]
        assert {port.name: port.satisfies for port in swc.ports}["rCalib"] == ["REQ_PORT@2"]
        assert {runnable.name: runnable.satisfies for runnable in swc.runnables}["TargetRunnable"] == ["REQ_RUN@1"]
        assert report.preserved_links == {
            "component": ["REQ_SWC@1"],
            "port 'rCalib'": ["REQ_PORT@2"],
            "runnable 'TargetRunnable'": ["REQ_RUN@1"],
        }
        assert report.dropped_links == []
        assert "LINKS preserved from the target project" in report.render()
        # Carried before the mandatory diff preview, so the preview advertises no lost link.
        assert "satisfies" not in report.swc_diff_preview, report.swc_diff_preview

    def test_a_link_on_a_port_the_export_removed_is_dropped_with_its_reason(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = _linked_target()
        divergent["ports"].append(
            {"name": "rGone", "direction": "require", "interface": "ICalib", "satisfies": ["REQ_GONE@1"]}
        )
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(model_root), update=True)

        assert "rGone" not in [port.name for port in read_swc_model(str(model_root))[0].ports]
        assert any("rGone" in line and "REQ_GONE@1" in line for line in report.dropped_links), report.dropped_links
        assert "LINKS NOT carried from the target project" in report.render()

    def test_a_fresh_import_writes_no_link(self, tmp_path: Path) -> None:
        _source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        (swc,) = read_swc_model(str(model_root))
        assert swc.satisfies == []
        assert all(port.satisfies == [] for port in swc.ports)
        assert all(runnable.satisfies == [] for runnable in swc.runnables)
        assert report.preserved_links == {}
        assert "LINKS" not in report.render()


class TestTheCarryComparesThePortAndNotOnlyItsName:
    """`carry_target_measures` at the unit level.

    Called directly: two cases need an incoming component the exporter cannot produce,
    the third a target port shape no round trip reaches. An imported port's `measures`
    is always empty, so there is no rule for an incoming port that declares one.
    """

    @staticmethod
    def _pair(target_port: dict[str, object], incoming_port: dict[str, object]):
        from swcraft_tools.component_exchange.target_carry import carry_target_measures
        from swcraft_tools.schema import Port, SWComponent

        target = SWComponent(name="Swc", type="application", ports=[Port(**target_port)])
        incoming = SWComponent(name="Swc", type="application", ports=[Port(**incoming_port)])
        return carry_target_measures(target, incoming)

    def test_a_port_re_pointed_to_another_interface_carries_nothing(self) -> None:
        """A measure names a data element of the port's own interface, so a re-pointed port carries nothing.

        Matching on name and direction alone would carry the old keys and write a file `check-model` refuses with
        `safety.measure-unknown-element`.
        """
        carried, preserved, dropped = self._pair(
            {"name": "rSig", "direction": "require", "interface": "ISpeed", "measures": {"speed": "E2E"}},
            {"name": "rSig", "direction": "require", "interface": "IOther"},
        )

        assert carried.ports[0].measures == {}
        assert preserved == {}
        assert any("rSig" in line and "IOther" in line and "ISpeed" in line for line in dropped)

    def test_a_provide_port_that_ALREADY_carried_a_map_keeps_it(self) -> None:
        """A provide port that already carried a map keeps it: nothing flipped, so nothing is dropped.

        The map is the integrator's own `safety.measure-on-provide-port` finding.
        """
        carried, preserved, dropped = self._pair(
            {"name": "pSpeed", "direction": "provide", "interface": "ISpeed", "measures": {"speed": "E2E"}},
            {"name": "pSpeed", "direction": "provide", "interface": "ISpeed"},
        )

        assert carried.ports[0].measures == {"speed": "E2E"}
        assert preserved == {"pSpeed": {"speed": "E2E"}}
        assert dropped == []

    def test_a_flip_towards_require_carries_nothing_either(self) -> None:
        """A flip towards require drops the map too.

        Carried onto the `require` port the map would close `safety.consumer-above-element`
        for every key in a file the integrator never wrote; an import may not answer a
        safety finding on the user's behalf, so any direction flip drops, and says so.
        """
        carried, preserved, dropped = self._pair(
            {"name": "pSpeed", "direction": "provide", "interface": "ISpeed", "measures": {"speed": "E2E"}},
            {"name": "pSpeed", "direction": "require", "interface": "ISpeed"},
        )

        assert carried.ports[0].measures == {}
        assert preserved == {}
        assert any("pSpeed" in line and "'provide' to 'require'" in line for line in dropped)


# A port's `observed_interface`, on `measures`' terms. The model importer
# writes it on an external stub's ports only, and a stub is never exported; a
# declared component may still carry one (a hand edit), where it is inert. No
# ARXML carries it, so without the projection blanking it such a component
# would diverge against its own unchanged export.


def _hint_target_port(source_root: str, port_name: str, hint: str) -> None:
    swc_file = Path(source_root) / "swc" / "body" / "TargetSwc.yaml"
    data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    for port in data["ports"]:
        if port["name"] == port_name:
            port["observed_interface"] = hint
    _dump(swc_file, data)


class TestAnObservedInterfaceIsNotExchanged:
    def test_a_hint_alone_is_not_a_divergence(self, tmp_path: Path) -> None:
        source_root = _build_source_model(tmp_path)
        _hint_target_port(source_root, "pSpeed", "ISpeed")
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        assert "observed" not in arxml.read_text(encoding="utf-8").lower()
        before = files_of(Path(source_root))

        report = import_component(str(arxml), source_root)

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(Path(source_root)) == before

    def test_an_update_does_not_carry_the_hint_and_the_preview_names_its_loss(self, tmp_path: Path) -> None:
        """Unlike `measures`, an update does not carry the hint, and the mandatory preview names its loss.

        The hint is inert on a declared owner, so a carried copy would protect nothing.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = copy.deepcopy(_TARGET_SWC_WITH_MEASURES)
        divergent["ports"] = [
            {"name": "pSpeed", "direction": "provide", "interface": "ISpeed", "observed_interface": "ISpeed"},
            {"name": "rCalib", "direction": "require", "interface": "ICalib"},
        ]
        divergent["runnables"] = []
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        written = yaml.safe_load((model_root / "swc" / "body" / "TargetSwc.yaml").read_text(encoding="utf-8"))
        assert all("observed_interface" not in port for port in written["ports"]), written["ports"]
        assert "observed_interface" in report.swc_diff_preview, report.swc_diff_preview

    def test_the_projection_blanks_the_hint_and_keeps_every_other_port_field(self) -> None:
        from swcraft_tools.component_exchange.divergence import projected_swc
        from swcraft_tools.schema import Port, SWComponent

        hinted = SWComponent(
            name="Swc",
            type="application",
            ports=[Port(name="pSig", direction="provide", interface="ISig", observed_interface="IOther")],
        )

        (port,) = projected_swc(hinted).ports
        assert port.observed_interface is None
        assert (port.name, port.direction, port.interface) == ("pSig", "provide", "ISig")


# AUTOSAR 4.3.0 input


def _export_target_at_4_3_0(tmp_path: Path) -> tuple[str, Path]:
    """``_export_target``'s twin, relabelled AUTOSAR 4.3.0.

    A relabel of a real export keeps both inputs byte-for-byte the same but for the
    schema token, which is what "the declared version is no input to the conflict
    policy" is tested against. ``relabel_arxml_version`` refuses a no-op substitution,
    so this cannot hand back a 4.2.2 document.
    """
    source_root, arxml_4_2_2 = _export_target(tmp_path)
    arxml = tmp_path / "TargetSwc-4-3-0.arxml"
    arxml.write_text(
        relabel_arxml_version(
            arxml_4_2_2.read_text(encoding="utf-8"),
            source=ArxmlVersion.V4_2_2.schema_filename,
            target=ArxmlVersion.V4_3_0.schema_filename,
        ),
        encoding="utf-8",
    )
    return source_root, arxml


class TestImportComponentAt4_3_0:
    """``import component`` accepts a 4.3.0-labelled component export, with the same policy.

    The round trip reaches Pydantic equality, and the conflict policy is unchanged: the
    declared version is no input to any of its decisions. The input is SWCraft's own
    output relabelled, so this says nothing about a real 4.3.0 export's dialect.
    """

    def test_the_input_really_declares_4_3_0(self, tmp_path: Path) -> None:
        """Guards the guard: an unrelabelled input would make every test below vacuous."""
        _, arxml = _export_target_at_4_3_0(tmp_path)
        root = ET.parse(arxml).getroot()
        schema_location = root.attrib["{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"]
        assert ArxmlVersion.V4_3_0.schema_filename in schema_location
        assert ArxmlVersion.V4_2_2.schema_filename not in schema_location

    def test_round_trip_into_fresh_project_is_pydantic_equal(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target_at_4_3_0(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        target_root = str(project / "model")
        source_swcs = {s.name: s for s in read_swc_model(source_root)}
        target_swcs = read_swc_model(target_root)
        assert [s.name for s in target_swcs] == ["TargetSwc"]
        assert target_swcs[0] == source_swcs["TargetSwc"]
        source_ifaces = {i.name: i for i in read_interfaces(source_root)}
        target_ifaces = {i.name: i for i in read_interfaces(target_root)}
        assert sorted(target_ifaces) == ["ICalib", "ISpeed"]
        assert target_ifaces["ISpeed"] == source_ifaces["ISpeed"]
        assert target_ifaces["ICalib"] == source_ifaces["ICalib"]
        source_types = {d.name: d for d in _both_type_layers(source_root)}
        target_types = {d.name: d for d in _both_type_layers(target_root)}
        assert sorted(target_types) == _CLOSURE_TYPE_NAMES
        for name in _CLOSURE_TYPE_NAMES:
            assert target_types[name] == source_types[name], name
        assert report.swc_action == "created"
        assert report.created_interfaces == ["ICalib", "ISpeed"]
        assert report.created_data_types == _CLOSURE_TYPES_IN_SOURCE_ORDER

    def test_identical_shared_defs_are_still_reused_silently(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target_at_4_3_0(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        shared_before = files_of(model_root)

        report = import_component(str(arxml), str(project / "model"))

        assert report.reused_interfaces == ["ICalib", "ISpeed"]
        assert report.reused_data_types == _CLOSURE_TYPES_IN_SOURCE_ORDER
        assert report.created_interfaces == []
        assert report.created_data_types == []
        assert len(report.written_paths) == 1
        assert report.written_paths[0].endswith("TargetSwc.yaml")
        # the seeded shared files are untouched — reuse never rewrites
        after = files_of(model_root)
        for rel, content in shared_before.items():
            assert after[rel] == content, rel

    @pytest.mark.parametrize("update", [False, True], ids=["default", "update=True"])
    @pytest.mark.parametrize(
        ("rel_path", "divergent", "conflict_name"),
        [
            # A base type and a value type may share a name, so the divergent target
            # base type is the closure's own `float32_base`.
            pytest.param(
                model_element_storage_name("float32_base", kind="base_type"),
                {"name": "float32_base", "kind": "base_type", "size": 16, "encoding": "NONE"},
                "float32_base",
                id="data_type",
            ),
            pytest.param(
                "interfaces/ISpeed.yaml",
                {
                    "name": "ISpeed",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "speed", "implementation_type": "float32"}],
                },
                "ISpeed",
                id="interface",
            ),
        ],
    )
    def test_divergent_shared_definition_still_always_rejects(
        self,
        tmp_path: Path,
        rel_path: str,
        divergent: dict[str, Any],
        conflict_name: str,
        update: bool,
    ) -> None:
        """A divergent shared definition rejects regardless of the declared version, ``update=True`` included."""
        _, arxml = _export_target_at_4_3_0(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _dump(model_root / rel_path, divergent)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match=conflict_name) as exc_info:
            import_component(str(arxml), str(project / "model"), update=update)

        assert any(conflict_name in entry for entry in exc_info.value.conflicts)
        assert files_of(model_root) == before

    def test_divergent_swc_rejected_by_default_and_replaced_with_update(self, tmp_path: Path) -> None:
        """A divergent SWC is rejected by default and replaced, with the diff preview, only under ``update=True``."""
        source_root, arxml = _export_target_at_4_3_0(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        existing_swc_file = model_root / "swc" / "body" / "TargetSwc.yaml"
        _dump(existing_swc_file, _DIVERGENT_TARGET_SWC)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="TargetSwc"):
            import_component(str(arxml), str(project / "model"))
        assert files_of(model_root) == before

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.swc_action == "updated"
        assert report.written_paths == [str(existing_swc_file)]
        assert "~ TargetSwc" in report.swc_diff_preview
        source_swcs = {s.name: s for s in read_swc_model(source_root)}
        assert read_swc_model(str(model_root))[0] == source_swcs["TargetSwc"]

    def test_reimporting_a_4_2_2_export_over_a_4_3_0_one_is_a_full_noop(self, tmp_path: Path) -> None:
        """The same component under both labels is the same definition: re-importing it is a no-op.

        Were the version to leak into the comparison, this would report a divergence.
        """
        _, arxml_4_3_0 = _export_target_at_4_3_0(tmp_path)
        arxml_4_2_2 = tmp_path / "TargetSwc.arxml"  # written by _export_target_at_4_3_0's setup
        project = init_project(tmp_path / "proj")
        import_component(str(arxml_4_3_0), str(project / "model"))
        before = files_of(project / "model")

        report = import_component(str(arxml_4_2_2), str(project / "model"))

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(project / "model") == before


# The planner extraction: `import_component()` applies the plan
# `plan_component_import()` computes, through `_atomic_apply`, and the MCP tool
# turns the same plan into a draft change-set request, so the two callers never
# disagree about what an import of one file does.
#
# The values below pin the CLI's report, conflict behaviour, planned order,
# written paths and resulting model bytes. `_PARITY_MODEL_DIGEST` is taken over
# every file under the model root (its POSIX-relative path plus its exact
# bytes); `test_the_recorded_digest_is_the_source_models_own_authored_bytes`
# recomputes it from the source's own authored files, without the importer.


#: The layers the digest below skips: the ones ``swcraft init`` scaffolds, which
#: the component importer never reads or writes.
#:
#: Derived from the scaffold's own file plan, never listed: a new scaffolded layer
#: joins with no edit here, and a hand-written layer tuple is what
#: ``test_layer_vocabulary_gate.py`` refuses in a fixture module. The exclusion
#: stays safe only because :func:`_excluded_layer_digest` pins those layers separately.
_DIGEST_EXCLUDED_LAYERS: frozenset[str] = frozenset(
    relative.split("/")[1] for relative in _default_file_plan("proj") if relative.count("/") >= 2
)


def _digest_of(paths: list[Path], root: Path) -> str:
    """A digest over *paths* — each one's POSIX-relative path plus its exact bytes."""
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda path: path.as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _model_inventory(root: Path) -> str:
    """The per-file breakdown a bare digest mismatch cannot give.

    Every file under *root* (the excluded layers included), its size, its own digest and
    a newline census, so a CRLF and an extra file look different on a failure.
    """
    lines = [f"model inventory under {root.as_posix()}:"]
    for path in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        crlf = raw.count(b"\r\n")
        bare_lf = raw.count(b"\n") - crlf
        lines.append(
            f"  {path.relative_to(root).as_posix()}  {len(raw)}B  "
            f"sha256={hashlib.sha256(raw).hexdigest()[:16]}  crlf={crlf} lf={bare_lf}"
        )
    return "\n".join(lines)


def _model_digest(root: Path) -> str:
    """A digest over every file under *root* but the scaffolded layers' (:data:`_DIGEST_EXCLUDED_LAYERS`)."""
    return _digest_of(
        [
            path
            for path in root.rglob("*")
            if path.is_file() and not (_DIGEST_EXCLUDED_LAYERS & set(path.relative_to(root).parts))
        ],
        root,
    )


def _excluded_layer_digest(root: Path) -> str:
    """A digest over exactly the files :func:`_model_digest` leaves out, so a stray write there moves it."""
    return _digest_of(
        [
            path
            for path in root.rglob("*")
            if path.is_file() and (_DIGEST_EXCLUDED_LAYERS & set(path.relative_to(root).parts))
        ],
        root,
    )


def _assert_model_bytes(model_root: Path, tmp_path: Path) -> None:
    """Assert every byte under *model_root*, in the two halves the parity digest is split into.

    The importer's half is the recorded digest; the excluded layers' half is compared
    with a freshly scaffolded project, so it states that the importer leaves them as
    ``swcraft init`` wrote them. Both report through :func:`_model_inventory`.
    """
    assert _model_digest(model_root) == _PARITY_MODEL_DIGEST, _model_inventory(model_root)
    pristine = init_project(tmp_path / f"pristine-{uuid.uuid4().hex[:8]}")
    assert _excluded_layer_digest(model_root) == _excluded_layer_digest(pristine / "model"), (
        f"the importer wrote into one of {sorted(_DIGEST_EXCLUDED_LAYERS)}, the layers `_model_digest` excludes\n"
        f"{_model_inventory(model_root)}"
    )


def _report_fields(report: ComponentImportReport) -> dict[str, Any]:
    """Every report field except the two that carry absolute temp paths."""
    return {
        "swc": report.swc,
        "swc_action": report.swc_action,
        "dry_run": report.dry_run,
        "swc_diff_preview": report.swc_diff_preview,
        "preserved_memory": report.preserved_memory,
        "preserved_access_lists": report.preserved_access_lists,
        "created_interfaces": report.created_interfaces,
        "reused_interfaces": report.reused_interfaces,
        "created_data_types": report.created_data_types,
        "reused_data_types": report.reused_data_types,
        "skip_lines": report.skip_lines,
        "access_collapse_lines": report.access_collapse_lines,
    }


#: The closure's data types in the export's source-path order (layer package first),
#: and its two interfaces, name-sorted.
_PARITY_TYPES = _CLOSURE_TYPES_IN_SOURCE_ORDER
_PARITY_INTERFACES = ["ICalib", "ISpeed"]
#: The writer mints no mapping set of its own, so the only skip is the implementation.
_PARITY_SKIPS = [
    "SWC-IMPLEMENTATION /ComponentTypes/TargetSwc_Implementation: SWC-IMPLEMENTATION has no SWCraft representation",
]

#: The model tree an import of `TargetSwc` into a freshly scaffolded project leaves
#: behind; the no-op re-import and the `update=True` repair of an emptied
#: `runnables` list end at the same bytes. It moves with the source model's
#: authored files and their paths, and the test below recomputes it from them.
_PARITY_MODEL_DIGEST = "2cbe04cf01edc8c643203802962f2d73bcb9a713915dae8ea330c9d8a664b02f"

#: The planned write order: the SWC's own file first (so the atomic apply's
#: rename loop touches the one possibly-pre-existing file first), then the
#: data types in the export's source-path order, then the name-sorted interfaces.
_PARITY_WRITE_ORDER = [
    "TargetSwc.yaml",
    *[f"{name}.yaml" for name in _PARITY_TYPES],
    *[f"{name}.yaml" for name in _PARITY_INTERFACES],
]


class TestThePlanExtractionLeavesTheCliPathIdentical:
    def test_a_create_reports_and_writes_exactly_what_it_did_before(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert _report_fields(report) == {
            "swc": "TargetSwc",
            "swc_action": "created",
            "dry_run": False,
            "swc_diff_preview": "",
            "preserved_memory": {},
            "preserved_access_lists": {},
            "created_interfaces": _PARITY_INTERFACES,
            "reused_interfaces": [],
            "created_data_types": _PARITY_TYPES,
            "reused_data_types": [],
            "skip_lines": _PARITY_SKIPS,
            "access_collapse_lines": [],
        }
        assert [Path(p).name for p in report.written_paths] == _PARITY_WRITE_ORDER
        _assert_model_bytes(project / "model", tmp_path)

    def test_a_reimport_is_still_the_same_no_op(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        import_component(str(arxml), str(project / "model"))

        report = import_component(str(arxml), str(project / "model"))

        assert _report_fields(report) == {
            "swc": "TargetSwc",
            "swc_action": "unchanged",
            "dry_run": False,
            "swc_diff_preview": "",
            "preserved_memory": {},
            "preserved_access_lists": {},
            "created_interfaces": [],
            "reused_interfaces": _PARITY_INTERFACES,
            "created_data_types": [],
            "reused_data_types": _PARITY_TYPES,
            "skip_lines": _PARITY_SKIPS,
            "access_collapse_lines": [],
        }
        assert report.written_paths == []
        _assert_model_bytes(project / "model", tmp_path)

    def test_an_update_reports_and_writes_exactly_what_it_did_before(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        import_component(str(arxml), str(project / "model"))
        swc_file = project / "model" / "swc" / "components" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"] = []
        _dump(swc_file, data)

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert _report_fields(report) == {
            "swc": "TargetSwc",
            "swc_action": "updated",
            "dry_run": False,
            "swc_diff_preview": "SWCs:\n  ~ TargetSwc\n      runnables:\n        + TargetRunnable (added)",
            "preserved_memory": {},
            "preserved_access_lists": {},
            "created_interfaces": [],
            "reused_interfaces": _PARITY_INTERFACES,
            "created_data_types": [],
            "reused_data_types": _PARITY_TYPES,
            "skip_lines": _PARITY_SKIPS,
            "access_collapse_lines": [],
        }
        assert report.written_paths == [str(swc_file)]
        _assert_model_bytes(project / "model", tmp_path)

    def test_the_planner_reports_what_the_dry_run_reports_and_writes_nothing(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = str(project / "model")
        before = files_of(project / "model")

        plan = plan_component_import(str(arxml), model_root)
        dry = import_component(str(arxml), model_root, dry_run=True)

        assert _report_fields(plan.report) == _report_fields(dry) | {"dry_run": False}
        assert plan.report.written_paths == []
        assert files_of(project / "model") == before

    def test_the_planned_writes_are_the_paths_the_apply_step_writes(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = str(project / "model")

        plan = plan_component_import(str(arxml), model_root)
        report = import_component(str(arxml), model_root)

        assert [str(path) for path, _ in plan.writes] == report.written_paths
        assert [path.name for path, _ in plan.writes] == _PARITY_WRITE_ORDER
        assert [target.element.name for target in plan.targets] == [
            "TargetSwc",
            *_PARITY_TYPES,
            *_PARITY_INTERFACES,
        ]
        assert [target.layer for target in plan.targets] == [
            "swc",
            *[_closure_type_layer(name) for name in _PARITY_TYPES],
            *["interfaces"] * len(_PARITY_INTERFACES),
        ]

    def test_a_planned_payload_is_the_yaml_the_apply_step_writes(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = str(project / "model")

        plan = plan_component_import(str(arxml), model_root)
        import_component(str(arxml), model_root)

        for path, payload in plan.writes:
            assert yaml.safe_load(path.read_text(encoding="utf-8")) == payload

    def test_the_recorded_digest_is_the_source_models_own_authored_bytes(self, tmp_path: Path) -> None:
        """The oracle for ``_PARITY_MODEL_DIGEST``, recomputed without the importer.

        Every file the import writes equals the source's authored file for that element, so
        the source files placed at the written paths reproduce the digest; a constant copied
        from the importer's output instead would fail here.
        """
        source_root, _arxml = _export_target(tmp_path)
        source = Path(source_root)
        placed = tmp_path / "placed"
        written = [
            f"swc/components/{_PARITY_WRITE_ORDER[0]}",
            *[_closure_type_storage_name(name) for name in _PARITY_TYPES],
            *[f"interfaces/{name}.yaml" for name in _PARITY_INTERFACES],
        ]
        for relative in written:
            layer, file_name = relative.split("/", 1)[0], relative.rsplit("/", 1)[1]
            (authored,) = sorted(source.glob(f"{layer}/**/{file_name}"), key=lambda path: path.as_posix())
            target = placed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(authored.read_bytes())

        assert _model_digest(placed) == _PARITY_MODEL_DIGEST, _model_inventory(placed)

    def test_a_conflict_aborts_the_planner_exactly_as_it_aborts_the_import(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        import_component(str(arxml), str(model_root))
        _dump(
            model_root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "other", "implementation_type": "float32"}],
            },
        )
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as planned:
            plan_component_import(str(arxml), str(model_root), update=True)
        with pytest.raises(ComponentImportConflictError) as applied:
            import_component(str(arxml), str(model_root), update=True)

        assert str(planned.value) == str(applied.value)
        assert planned.value.conflicts == applied.value.conflicts
        assert "shared interface 'ISpeed' diverges" in str(planned.value)
        assert files_of(model_root) == before


# The runnable's three access lists across the exchange. The writer emits the
# access points and marks the behaviour it emitted them for, so the comparison
# is regime-dependent: `marked` and `replace-flag` compare the lists (sorted),
# `carried` nulls them on both sides, which is what an unmarked older or
# foreign export gets. An export carrying the containers lands its points on a
# `created` SWC, and a duplicated pair collapses to one entry as a report line
# rather than a blocking skip.

_TARGET_ACCESSES: dict[str, Any] = {
    "reads": [{"port": "rSpeedIn", "element": "speed"}, {"port": "rSpeedIn", "element": "pose", "by": "value"}],
    "writes": [{"port": "pSpeed", "element": "speed"}],
    "calls": [{"port": "rCalib", "operation": "SetOffset"}],
}


#: The other side of the `marked` regime: a set of entries that differs from
#: :data:`_TARGET_ACCESSES` in every list, so a replacement removes named
#: entries rather than shuffling them.
_OTHER_ACCESSES: dict[str, Any] = {
    "reads": [{"port": "rSpeedIn", "element": "pose"}],
    "writes": [{"port": "pSpeed", "element": "pose"}],
    "calls": [],
}


def _declare_accesses_on_target(
    model_root: Path, group: str = "body", *, accesses: dict[str, Any] | None = None
) -> Path:
    """Give `TargetSwc` a require port on `ISpeed` and *accesses* on `TargetRunnable`, in its YAML.

    `group` is `body` in the source fixture and `components` in an imported one. `accesses`
    defaults to :data:`_TARGET_ACCESSES`; `{}` adds the port alone, whose export carries
    no access point and so no marker: the only way to drive the `carried` regime from
    the exchange's own writer.
    """
    swc_file = model_root / "swc" / group / "TargetSwc.yaml"
    data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    # idempotent in the port: a target seeded from an export that already
    # carries `rSpeedIn` would otherwise declare it twice
    if not any(port["name"] == "rSpeedIn" for port in data["ports"]):
        data["ports"].append({"name": "rSpeedIn", "direction": "require", "interface": "ISpeed"})
    data["runnables"][0].update(copy.deepcopy(_TARGET_ACCESSES if accesses is None else accesses))
    _dump(swc_file, data)
    return swc_file


def _send_point(name: str, port: str, element: str) -> str:
    return (
        f"<VARIABLE-ACCESS><SHORT-NAME>{name}</SHORT-NAME><ACCESSED-VARIABLE><AUTOSAR-VARIABLE-IREF>"
        f'<PORT-PROTOTYPE-REF DEST="P-PORT-PROTOTYPE">/ComponentTypes/TargetSwc/{port}</PORT-PROTOTYPE-REF>'
        f'<TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/ISpeed/{element}'
        "</TARGET-DATA-PROTOTYPE-REF></AUTOSAR-VARIABLE-IREF></ACCESSED-VARIABLE></VARIABLE-ACCESS>"
    )


def _export_with_send_points(tmp_path: Path, *points: str) -> Path:
    """The TargetSwc export with a DATA-SEND-POINTS container spliced into its runnable, at the writer's paths."""
    _, arxml = _export_target(tmp_path)
    anchor = "<SYMBOL>TargetRunnable</SYMBOL>"
    text = arxml.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    text = text.replace(anchor, anchor + "<DATA-SEND-POINTS>" + "".join(points) + "</DATA-SEND-POINTS>")
    arxml.write_text(text, encoding="utf-8", newline="\n")
    return arxml


def _plain_then_send_points(tmp_path: Path, *points: str) -> tuple[Path, Path]:
    """Return (the plain export copied to `plain.arxml`, the send-point export), built in that order.

    Both helpers write `tmp_path / "TargetSwc.arxml"`; the other order overwrites the
    send-point file with the plain one.
    """
    _source_root, exported = _export_target(tmp_path)
    plain = tmp_path / "plain.arxml"
    plain.write_bytes(exported.read_bytes())
    return plain, _export_with_send_points(tmp_path, *points)


class TestAccessListsAcrossTheExchange:
    def test_declared_accesses_alone_are_not_a_divergence(self, tmp_path: Path) -> None:
        """Declared accesses alone are not a divergence.

        The source declares every list, exports itself and re-imports the
        result: a full no-op, exactly as the `memory` budget's case is."""
        source_root = _build_source_model(tmp_path)
        _declare_accesses_on_target(Path(source_root))
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        before = files_of(Path(source_root))

        report = import_component(str(arxml), source_root)

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(Path(source_root)) == before

    def test_a_target_with_declared_accesses_is_unchanged_never_conflict_and_needs_no_update(
        self, tmp_path: Path
    ) -> None:
        """The export carries no lists and the target does: ``unchanged``, no conflict.

        A conflict's only exit, `--update`, would rewrite the runnable without its lists.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        import_component(str(arxml), str(model_root))
        # the target's own ports/interfaces are now the export's; add the
        # require port and the lists the way an architect would
        _declare_accesses_on_target(model_root, group="components")
        # the export must carry the same require port, or the port list is
        # the representable difference
        _declare_accesses_on_target(Path(source_root))
        export_component(source_root, "TargetSwc", str(arxml))
        before = files_of(model_root)

        planned = plan_component_import(str(arxml), str(model_root))
        report = import_component(str(arxml), str(model_root))

        assert planned.report.swc_action == "unchanged"
        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(model_root) == before
        runnable = read_swc_model(str(model_root))[0].runnables[0]
        assert [(r.port, r.element, r.by) for r in runnable.reads] == [
            ("rSpeedIn", "speed", None),
            ("rSpeedIn", "pose", "value"),
        ]
        assert [(w.port, w.element) for w in runnable.writes] == [("pSpeed", "speed")]
        assert [(c.port, c.operation) for c in runnable.calls] == [("rCalib", "SetOffset")]

    def test_a_representable_divergence_names_the_lists_among_what_is_ignored(self, tmp_path: Path) -> None:
        """A genuine divergence still conflicts, and names the lists among what is ignored.

        Its message tells the reader that the access lists were already ignored by the comparison."""
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        import_component(str(arxml), str(model_root))
        swc_file = model_root / "swc" / "components" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"][0]["min_start_interval"] = 0.02
        data["runnables"][0]["writes"] = [{"port": "pSpeed", "element": "speed"}]
        _dump(swc_file, data)

        with pytest.raises(ComponentImportConflictError) as exc:
            import_component(str(arxml), str(model_root))

        assert "SWC 'TargetSwc' already exists in the target project and diverges" in str(exc.value)
        assert "reads/writes/calls" in str(exc.value)

    def test_an_export_carrying_accesses_onto_a_component_the_project_has_is_unchanged_and_lands_none(
        self, tmp_path: Path
    ) -> None:
        """An unmarked export's points onto a component the project has: ``unchanged``, none landed.

        An unmarked export's lists are no part of the comparison, so nothing is reported.
        """
        # The plain export first, copied aside: both helpers write one path, and
        # building the send-point export first leaves `first` without a point.
        plain, first = _plain_then_send_points(tmp_path, _send_point("W_pSpeed_speed", "pSpeed", "speed"))
        assert "DATA-SEND-POINTS" in first.read_text(encoding="utf-8")
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        assert import_component(str(plain), str(model_root)).swc_action == "created"
        before = files_of(model_root)

        report = import_component(str(first), str(model_root))

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(model_root) == before
        assert read_swc_model(str(model_root))[0].runnables[0].writes == []
        assert report.access_collapse_lines == []

    def test_update_preserves_the_target_access_lists_when_the_export_is_unmarked(self, tmp_path: Path) -> None:
        """The `carried` regime end to end: `--update` keeps the target runnable's lists.

        With an unmarked export and no `--replace-access-lists`, its silence about the
        lists means "unknown", and the carry happens before the preview is rendered, so
        the preview advertises no removal. The source declares the require port and no
        list, so its export is unmarked.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        import_component(str(arxml), str(model_root))
        _declare_accesses_on_target(model_root, group="components")
        # the export carries the same require port (so the ports are not the
        # divergence) and NO list of its own (so it is unmarked), plus a
        # different interval, which is the representable difference that makes
        # `--update` genuinely needed
        _declare_accesses_on_target(Path(source_root), accesses={})
        export_component(source_root, "TargetSwc", str(arxml))
        swc_file = model_root / "swc" / "components" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"][0]["min_start_interval"] = 0.02
        _dump(swc_file, data)
        before = files_of(model_root)

        preview = import_component(str(arxml), str(model_root), update=True, dry_run=True)

        assert preview.swc_action == "updated"
        assert files_of(model_root) == before
        assert "min_start_interval" in preview.swc_diff_preview
        for key in ("reads", "writes", "calls"):
            assert key not in preview.swc_diff_preview, (key, preview.swc_diff_preview)
        assert preview.preserved_access_lists == {"TargetRunnable": {"reads": 2, "writes": 1, "calls": 1}}
        assert preview.access_list_regime == "carried"

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert report.access_list_regime == "carried"
        assert report.preserved_access_lists == preview.preserved_access_lists
        rendered = report.render()
        assert "ACCESS LISTS preserved from the target project" in rendered
        assert "TargetRunnable: reads=2, writes=1, calls=1" in rendered
        assert "--replace-access-lists" in rendered
        updated = read_swc_model(str(model_root))[0]
        # the representable difference really was replaced by the export's
        assert updated.runnables[0].min_start_interval == 0.005
        assert [p.name for p in updated.ports] == ["pSpeed", "rCalib", "rSpeedIn"]
        runnable = updated.runnables[0]
        assert [(r.port, r.element, r.by) for r in runnable.reads] == [
            ("rSpeedIn", "speed", None),
            ("rSpeedIn", "pose", "value"),
        ]
        assert [(w.port, w.element) for w in runnable.writes] == [("pSpeed", "speed")]
        assert [(c.port, c.operation) for c in runnable.calls] == [("rCalib", "SetOffset")]

    def test_update_takes_the_export_lists_when_it_carries_any(self, tmp_path: Path) -> None:
        """An export that speaks about a runnable's accesses is the truth: the target's lists are replaced.

        Nothing is preserved and the report says nothing was.
        """
        plain, arxml = _plain_then_send_points(tmp_path, _send_point("W_pSpeed_speed", "pSpeed", "speed"))
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        import_component(str(plain), str(model_root))
        # the added require port is the representable divergence; the target's
        # lists ride on it and on the ports the export declares
        _declare_accesses_on_target(model_root, group="components")

        preview = import_component(str(arxml), str(model_root), update=True, dry_run=True)

        assert preview.swc_action == "updated"
        assert preview.preserved_access_lists == {}
        for key in ("reads", "calls"):
            assert key in preview.swc_diff_preview, (key, preview.swc_diff_preview)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.preserved_access_lists == {}
        assert "ACCESS LISTS preserved" not in report.render()
        runnable = read_swc_model(str(model_root))[0].runnables[0]
        assert [(w.port, w.element) for w in runnable.writes] == [("pSpeed", "speed")]
        assert (runnable.reads, runnable.calls) == ([], [])

    def test_update_preserves_per_runnable_and_a_removed_runnable_takes_its_lists_with_it(self, tmp_path: Path) -> None:
        """The unit of the rule is the runnable name: a runnable the export drops goes with its lists.

        Only the runnable that had lists to carry is reported.
        """
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        import_component(str(arxml), str(model_root))
        swc_file = _declare_accesses_on_target(model_root, group="components")
        # The source declares the require port and NO list, so its export is
        # unmarked and the regime is `carried`.
        _declare_accesses_on_target(Path(source_root), accesses={})
        export_component(source_root, "TargetSwc", str(arxml))
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"].append(
            {"name": "ExtraRun", "min_start_interval": 0.01, "writes": [{"port": "pSpeed", "element": "pose"}]}
        )
        _dump(swc_file, data)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert report.access_list_regime == "carried"
        assert report.preserved_access_lists == {"TargetRunnable": {"reads": 2, "writes": 1, "calls": 1}}
        assert "ExtraRun" in report.swc_diff_preview
        updated = read_swc_model(str(model_root))[0]
        assert [r.name for r in updated.runnables] == ["TargetRunnable"]
        assert len(updated.runnables[0].reads) == 2

    def test_update_of_a_target_without_lists_reports_nothing_preserved(self, tmp_path: Path) -> None:
        source_root, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _seed_identical_shared_defs(source_root, model_root)
        divergent = copy.deepcopy(_TARGET_SWC_WITH_MEMORY)
        del divergent["memory"]
        divergent["runnables"][0]["min_start_interval"] = 0.02
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", divergent)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert report.preserved_access_lists == {}
        assert "preserved" not in report.render()

    def test_an_export_carrying_a_duplicated_access_imports_with_a_report_line(self, tmp_path: Path) -> None:
        """Two send points naming the same (port, element) collapse to one `writes` entry with a report line.

        Never a skip: a non-deferred skip aborts the import, and the collapse loses no
        model content.
        """
        arxml = _export_with_send_points(
            tmp_path,
            _send_point("W_pSpeed_speed", "pSpeed", "speed"),
            _send_point("W_pSpeed_speed_1", "pSpeed", "speed"),
            _send_point("W_pSpeed_pose", "pSpeed", "pose"),
        )
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        # an import always carries the deferred-tag lines (the writer's
        # SWC-IMPLEMENTATION); no access point is among them, and none aborted
        assert not [line for line in report.skip_lines if "VARIABLE-ACCESS" in line]
        assert len(report.access_collapse_lines) == 1
        (line,) = report.access_collapse_lines
        assert "W_pSpeed_speed_1" in line and "pSpeed" in line
        assert "Collapsed duplicate access points:" in report.render()
        runnable = read_swc_model(str(model_root))[0].runnables[0]
        assert [(w.port, w.element) for w in runnable.writes] == [("pSpeed", "speed"), ("pSpeed", "pose")]
        assert runnable.reads == [] and runnable.calls == []
        written = (model_root / "swc" / "components" / "TargetSwc.yaml").read_text(encoding="utf-8")
        assert "W_pSpeed" not in written

    def test_an_export_without_duplicates_reports_no_collapse(self, tmp_path: Path) -> None:
        arxml = _export_with_send_points(tmp_path, _send_point("W_pSpeed_speed", "pSpeed", "speed"))
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.access_collapse_lines == []
        assert "Collapsed duplicate access points" not in report.render()

    def test_the_dry_run_plans_the_same_collapse(self, tmp_path: Path) -> None:
        arxml = _export_with_send_points(
            tmp_path,
            _send_point("W_pSpeed_speed", "pSpeed", "speed"),
            _send_point("W_pSpeed_speed_1", "pSpeed", "speed"),
        )
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        before = files_of(model_root)

        report = import_component(str(arxml), str(model_root), dry_run=True)

        assert report.dry_run is True
        assert len(report.access_collapse_lines) == 1
        assert files_of(model_root) == before


def _export_with_call_points(tmp_path: Path, *points: str) -> Path:
    """`_export_with_send_points`'s twin for a SERVER-CALL-POINTS container."""
    _, arxml = _export_target(tmp_path)
    anchor = "<SYMBOL>TargetRunnable</SYMBOL>"
    text = arxml.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    text = text.replace(anchor, anchor + "<SERVER-CALL-POINTS>" + "".join(points) + "</SERVER-CALL-POINTS>")
    arxml.write_text(text, encoding="utf-8", newline="\n")
    return arxml


def _call_point(name: str, port: str, operation: str, tag: str = "SYNCHRONOUS-SERVER-CALL-POINT") -> str:
    return (
        f"<{tag}><SHORT-NAME>{name}</SHORT-NAME><OPERATION-IREF>"
        f'<CONTEXT-R-PORT-REF DEST="R-PORT-PROTOTYPE">/ComponentTypes/TargetSwc/{port}</CONTEXT-R-PORT-REF>'
        f'<TARGET-REQUIRED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/PortInterfaces/ICalib/{operation}'
        f"</TARGET-REQUIRED-OPERATION-REF></OPERATION-IREF><TIMEOUT>0</TIMEOUT></{tag}>"
    )


class TestAnAccessSkipBlocksTheComponentImport:
    """Every access point the mapper or the reader skips is a lossy import, refused.

    Its tag (`VARIABLE-ACCESS`, `SYNCHRONOUS-SERVER-CALL-POINT`,
    `ASYNCHRONOUS-SERVER-CALL-POINT`) is not deferred, and a component export holds
    nothing but the component's own closure. The collapse, by contrast, is a report
    line (`TestAccessListsAcrossTheExchange`).
    """

    def test_an_access_on_a_port_the_component_does_not_declare_refuses(self, tmp_path: Path) -> None:
        arxml = _export_with_send_points(tmp_path, _send_point("W_ghost_speed", "ghost", "speed"))
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        before = files_of(model_root)

        with pytest.raises(LossyComponentImportError) as exc:
            plan_component_import(str(arxml), str(model_root))

        assert "port 'ghost' is not declared on SWC 'TargetSwc'" in str(exc.value)
        assert "VARIABLE-ACCESS" in str(exc.value)
        assert files_of(model_root) == before

    def test_an_asynchronous_call_point_is_read_and_imported_asynchronous(self, tmp_path: Path) -> None:
        # The component import maps through the shared mapper, so the call lands
        # asynchronous; the exchange's regime for the forms is `test_access_forms_exchange.py`'s.
        arxml = _export_with_call_points(
            tmp_path, _call_point("Call_rCalib_SetOffset", "rCalib", "SetOffset", tag="ASYNCHRONOUS-SERVER-CALL-POINT")
        )
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        runnable = read_swc_model(str(model_root))[0].runnables[0]
        assert [(c.port, c.operation, c.asynchronous) for c in runnable.calls] == [("rCalib", "SetOffset", True)]

    def test_a_synchronous_call_point_that_resolves_imports(self, tmp_path: Path) -> None:
        # The control: the same shape with the one tag the reader reads lands.
        arxml = _export_with_call_points(tmp_path, _call_point("Call_rCalib_SetOffset", "rCalib", "SetOffset"))
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        runnable = read_swc_model(str(model_root))[0].runnables[0]
        assert [(c.port, c.operation) for c in runnable.calls] == [("rCalib", "SetOffset")]


# The access-list regime: `--update` decides what happens to a target
# runnable's three lists by a distinguishing signal, never by the export's
# silence alone:
#
#   marker present            -> `marked`        the export is the truth
#   no marker, flag given     -> `replace-flag`  the user is the truth
#   no marker, no flag        -> `carried`       the target keeps its lists
#   the SWC was not updated   -> `none`
#
# An update never silently merges or overwrites: an ARXML with no access
# container is written both by an exporter that means *none* and by one that
# cannot say, and on the non-dry-run path the loss would show only in the
# report after the write.


def _marked_export_with(tmp_path: Path, accesses: dict[str, Any]) -> tuple[str, Path]:
    """Return (source root, export) of `TargetSwc` declaring *accesses*: containers and marker included."""
    source_root, arxml = _export_target(tmp_path)
    _declare_accesses_on_target(Path(source_root), accesses=accesses)
    export_component(source_root, "TargetSwc", str(arxml))
    assert 'GID="SWCRAFT"' in arxml.read_text(encoding="utf-8")
    return source_root, arxml


def _unmarked_export(tmp_path: Path) -> tuple[str, Path]:
    """The same closure with the require port and no list: no access container, no access-points marker.

    It still carries the data-types marker every component export writes.
    """
    source_root, arxml = _export_target(tmp_path)
    _declare_accesses_on_target(Path(source_root), accesses={})
    export_component(source_root, "TargetSwc", str(arxml))
    text = arxml.read_text(encoding="utf-8")
    assert ACCESS_POINTS.sd_gid not in text
    assert f'<SD GID="{DATA_TYPES.sd_gid}">' in text
    return source_root, arxml


def _target_with(tmp_path: Path, arxml: Path, accesses: dict[str, Any]) -> Path:
    """A fresh project holding `TargetSwc` from a plain export of the same closure.

    *accesses* are declared on its runnable afterwards."""
    plain = tmp_path / "plain.arxml"
    plain.write_bytes(arxml.read_bytes())
    project = init_project(tmp_path / "proj")
    model_root = project / "model"
    assert import_component(str(plain), str(model_root)).swc_action == "created"
    _declare_accesses_on_target(model_root, group="components", accesses=accesses)
    return model_root


def _entries_of(model_root: Path) -> dict[str, list[tuple[str, ...]]]:
    runnable = read_swc_model(str(model_root))[0].runnables[0]
    return {
        "reads": [(r.port, r.element, r.by) for r in runnable.reads],
        "writes": [(w.port, w.element) for w in runnable.writes],
        "calls": [(c.port, c.operation) for c in runnable.calls],
    }


class TestTheMarkedRegimeReplacesTheTargetLists:
    """The export carries the marker, so it is the truth.

    `projected_swc` compares the lists under this regime, so differing lists are a
    conflict, and `--update` replaces them through the diff preview.
    """

    def _diverging_pair(self, tmp_path: Path) -> tuple[Path, Path]:
        _source, arxml = _marked_export_with(tmp_path, _TARGET_ACCESSES)
        model_root = _target_with(tmp_path, arxml, _OTHER_ACCESSES)
        return arxml, model_root

    def test_differing_lists_alone_are_a_conflict(self, tmp_path: Path) -> None:
        """Differing access lists alone are a conflict.

        Under `carried` they would be nulled out of the comparison and the
        answer would be `unchanged`; the marker is what makes them visible."""
        arxml, model_root = self._diverging_pair(tmp_path)

        with pytest.raises(ComponentImportConflictError) as exc:
            import_component(str(arxml), str(model_root))

        assert "SWC 'TargetSwc' already exists in the target project and diverges" in str(exc.value)
        assert "reads/writes/calls are compared" in str(exc.value)
        assert "marker" in str(exc.value)

    def test_the_dry_run_names_every_removed_entry_before_any_write(self, tmp_path: Path) -> None:
        """The dry run shows every removed entry before any write: the differ reports member-level changes."""
        arxml, model_root = self._diverging_pair(tmp_path)
        before = files_of(model_root)

        preview = import_component(str(arxml), str(model_root), update=True, dry_run=True)

        assert preview.swc_action == "updated"
        assert preview.access_list_regime == "marked"
        assert preview.preserved_access_lists == {}
        assert files_of(model_root) == before
        # every entry the target declared and the export does not
        for removed in ("pose", "rSpeedIn", "pSpeed"):
            assert removed in preview.swc_diff_preview, preview.swc_diff_preview
        for key in ("reads", "writes", "calls"):
            assert key in preview.swc_diff_preview, (key, preview.swc_diff_preview)
        assert "ACCESS LISTS regime: marked" in preview.render()

    def test_the_update_lands_the_exports_lists(self, tmp_path: Path) -> None:
        arxml, model_root = self._diverging_pair(tmp_path)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert report.access_list_regime == "marked"
        assert report.preserved_access_lists == {}
        assert "ACCESS LISTS preserved" not in report.render()
        assert _entries_of(model_root) == {
            "reads": [("rSpeedIn", "speed", None), ("rSpeedIn", "pose", "value")],
            "writes": [("pSpeed", "speed")],
            "calls": [("rCalib", "SetOffset")],
        }

    def test_equal_lists_are_a_no_op_and_the_regime_is_none(self, tmp_path: Path) -> None:
        """Equal lists in another order are ``unchanged``; a non-``updated`` action reports regime ``none``."""
        _source, arxml = _marked_export_with(tmp_path, _TARGET_ACCESSES)
        shuffled = {
            "reads": list(reversed(_TARGET_ACCESSES["reads"])),
            "writes": list(_TARGET_ACCESSES["writes"]),
            "calls": list(_TARGET_ACCESSES["calls"]),
        }
        model_root = _target_with(tmp_path, arxml, shuffled)
        before = files_of(model_root)

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "unchanged"
        assert report.access_list_regime == "none"
        assert report.written_paths == []
        assert files_of(model_root) == before
        assert "ACCESS LISTS regime" not in report.render()

    def test_into_an_empty_project_the_lists_land_in_the_written_yaml(self, tmp_path: Path) -> None:
        """The `created` branch, asserted on the bytes.

        Nothing about the regime touches a component the target does not have."""
        _source, arxml = _marked_export_with(tmp_path, _TARGET_ACCESSES)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        assert report.access_list_regime == "none"
        written = (model_root / "swc" / "components" / "TargetSwc.yaml").read_text(encoding="utf-8")
        assert "reads:" in written and "writes:" in written and "calls:" in written
        assert _entries_of(model_root) == {
            "reads": [("rSpeedIn", "speed", None), ("rSpeedIn", "pose", "value")],
            "writes": [("pSpeed", "speed")],
            "calls": [("rCalib", "SetOffset")],
        }


class TestTheReplaceFlagRegimeReplacesAnUnmarkedExportsLists:
    """No marker, but the user passed `--replace-access-lists`: the same replacement as `marked`.

    The report says which of the two decided, because "the export told us" and "you
    told us" are different claims about the same bytes.
    """

    def _pair(self, tmp_path: Path) -> tuple[Path, Path]:
        _source, arxml = _unmarked_export(tmp_path)
        model_root = _target_with(tmp_path, arxml, _TARGET_ACCESSES)
        # a representable divergence that is not the lists, so the update is
        # genuinely needed under both regimes
        swc_file = model_root / "swc" / "components" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"][0]["min_start_interval"] = 0.02
        _dump(swc_file, data)
        return arxml, model_root

    def test_the_dry_run_lists_the_removed_entries_before_any_write(self, tmp_path: Path) -> None:
        arxml, model_root = self._pair(tmp_path)
        before = files_of(model_root)

        preview = import_component(str(arxml), str(model_root), update=True, dry_run=True, replace_access_lists=True)

        assert preview.access_list_regime == "replace-flag"
        assert preview.preserved_access_lists == {}
        assert files_of(model_root) == before
        for key in ("reads", "writes", "calls"):
            assert key in preview.swc_diff_preview, (key, preview.swc_diff_preview)
        rendered = preview.render()
        assert "ACCESS LISTS regime: replace-flag" in rendered
        assert "--replace-access-lists" in rendered

    def test_the_update_removes_them(self, tmp_path: Path) -> None:
        arxml, model_root = self._pair(tmp_path)

        report = import_component(str(arxml), str(model_root), update=True, replace_access_lists=True)

        assert report.swc_action == "updated"
        assert report.access_list_regime == "replace-flag"
        assert report.preserved_access_lists == {}
        assert _entries_of(model_root) == {"reads": [], "writes": [], "calls": []}

    def test_without_the_flag_the_same_export_carries_them(self, tmp_path: Path) -> None:
        """The control that makes the two assertions above about the flag and not the fixture."""
        arxml, model_root = self._pair(tmp_path)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.access_list_regime == "carried"
        assert report.preserved_access_lists == {"TargetRunnable": {"reads": 2, "writes": 1, "calls": 1}}
        assert _entries_of(model_root)["writes"] == [("pSpeed", "speed")]

    def test_the_flag_alone_is_not_an_update(self, tmp_path: Path) -> None:
        """The flag never makes a conflict updatable: without `update=True` the divergence is a conflict.

        The CLI refuses the combination outright; this is the library's own behaviour.
        """
        arxml, model_root = self._pair(tmp_path)

        with pytest.raises(ComponentImportConflictError):
            import_component(str(arxml), str(model_root), replace_access_lists=True)


class TestTheCarriedRegimeKeepsTheTargetLists:
    """No marker, no flag: the target keeps its lists.

    Without it an `--update` from an older or third-party export would delete
    declarations the source cannot express, with `check-model` still green and the
    report after the write the only witness.
    """

    def test_the_report_names_the_regime_and_the_flag(self, tmp_path: Path) -> None:
        _source, arxml = _unmarked_export(tmp_path)
        model_root = _target_with(tmp_path, arxml, _TARGET_ACCESSES)
        swc_file = model_root / "swc" / "components" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"][0]["min_start_interval"] = 0.02
        _dump(swc_file, data)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.access_list_regime == "carried"
        rendered = report.render()
        assert "ACCESS LISTS regime: carried" in rendered
        assert "pass --replace-access-lists to replace them" in rendered
        assert "TargetRunnable: reads=2, writes=1, calls=1" in rendered

    def test_an_unmarked_export_that_is_otherwise_identical_is_still_a_no_op(self, tmp_path: Path) -> None:
        """An otherwise identical unmarked export is a no-op: the lists are nulled out of the comparison."""
        _source, arxml = _unmarked_export(tmp_path)
        model_root = _target_with(tmp_path, arxml, _TARGET_ACCESSES)
        before = files_of(model_root)

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "unchanged"
        assert report.written_paths == []
        assert files_of(model_root) == before

    def test_a_foreign_admin_data_still_aborts_the_import(self, tmp_path: Path) -> None:
        """A foreign `ADMIN-DATA` (a `ResourceBudget` SDG) still aborts the import as vendor metadata.

        Its tag is not deferred, which is why the `carried` fixtures run without it; the
        group sits beside the data-types marker in the behaviour's one `ADMIN-DATA`.
        """
        _source, arxml = _unmarked_export(tmp_path)
        anchor = '<SDG GID="SWCRAFT">'
        text = arxml.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        arxml.write_text(
            text.replace(
                anchor,
                '<SDG GID="ResourceBudget"><SD GID="RAMBudget">512</SD></SDG>' + anchor,
            ),
            encoding="utf-8",
            newline="\n",
        )
        project = init_project(tmp_path / "proj")

        with pytest.raises(LossyComponentImportError) as exc:
            plan_component_import(str(arxml), str(project / "model"))

        assert "ADMIN-DATA" in str(exc.value)
        assert "vendor metadata" in str(exc.value)

    def test_the_marker_block_on_the_COMPONENT_aborts_the_import_as_vendor_metadata(self, tmp_path: Path) -> None:
        """The marker block on the component, not the behaviour, aborts the import as vendor metadata.

        Only the marker on the behaviour is exempt; one on the component is reported, so its payload never vanishes
        silently.
        """
        _source, arxml = _unmarked_export(tmp_path)
        anchor = "<SHORT-NAME>TargetSwc</SHORT-NAME>"
        text = arxml.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        arxml.write_text(
            text.replace(
                anchor,
                anchor + '<ADMIN-DATA><SDGS><SDG GID="SWCRAFT">'
                '<SD GID="ACCESS-POINTS">explicit</SD></SDG></SDGS></ADMIN-DATA>',
            ),
            encoding="utf-8",
            newline="\n",
        )
        project = init_project(tmp_path / "proj")

        with pytest.raises(LossyComponentImportError) as exc:
            plan_component_import(str(arxml), str(project / "model"))

        assert "ADMIN-DATA" in str(exc.value)
        assert "vendor metadata" in str(exc.value)
        assert "APPLICATION-SW-COMPONENT-TYPE 'TargetSwc'" in str(exc.value)

    @pytest.mark.parametrize(
        "with_access_points", [False, True], ids=["data-types-alone", "access-points-and-data-types"]
    )
    def test_an_unknown_sd_inside_the_exports_own_swcraft_group_aborts_the_import(
        self, tmp_path: Path, with_access_points: bool
    ) -> None:
        """An unknown `SD` in the export's own marker group aborts: the group is the writer's only as a whole.

        It never lands in `swcraft_markers`, read by nothing, where the payload would vanish.
        """
        if with_access_points:
            _source, arxml = _marked_export_with(tmp_path, _TARGET_ACCESSES)
        else:
            _source, arxml = _unmarked_export(tmp_path)
        anchor = f'<SD GID="{DATA_TYPES.sd_gid}">'
        text = arxml.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        assert (f'<SD GID="{ACCESS_POINTS.sd_gid}">' in text) is with_access_points
        arxml.write_text(text.replace(anchor, '<SD GID="VENDOR-FLAG">1</SD>' + anchor), encoding="utf-8", newline="\n")
        project = init_project(tmp_path / "proj")

        with pytest.raises(LossyComponentImportError) as exc:
            plan_component_import(str(arxml), str(project / "model"))

        assert "vendor metadata" in str(exc.value)
        assert "SWC-INTERNAL-BEHAVIOR 'TargetSwc_InternalBehavior'" in str(exc.value)

    @pytest.mark.parametrize(
        ("anchor", "spliced"),
        [
            pytest.param('<SDG GID="SWCRAFT">', '<SDG GID="SWCRAFT">payload', id="text-inside-the-group"),
            pytest.param("no invented twin</SD>", "no invented twin</SD>tailpayload", id="a-tail-after-an-sd"),
            pytest.param("<SDGS>", "<SDGS>payload", id="text-inside-sdgs"),
        ],
    )
    @pytest.mark.parametrize(
        "with_access_points", [False, True], ids=["data-types-alone", "access-points-and-data-types"]
    )
    def test_character_data_in_the_exports_own_marker_block_aborts_the_import(
        self, tmp_path: Path, with_access_points: bool, anchor: str, spliced: str
    ) -> None:
        """Character data in the export's own marker block aborts: the verdict judges the text too.

        Such text reaches neither the model nor the report, so it is never accepted.
        """
        if with_access_points:
            _source, arxml = _marked_export_with(tmp_path, _TARGET_ACCESSES)
        else:
            _source, arxml = _unmarked_export(tmp_path)
        text = arxml.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        arxml.write_text(text.replace(anchor, spliced), encoding="utf-8", newline="\n")
        project = init_project(tmp_path / "proj")

        with pytest.raises(LossyComponentImportError) as exc:
            plan_component_import(str(arxml), str(project / "model"))

        assert "vendor metadata" in str(exc.value)
        assert "SWC-INTERNAL-BEHAVIOR 'TargetSwc_InternalBehavior'" in str(exc.value)

    @pytest.mark.parametrize(
        ("anchor", "spliced"),
        [
            pytest.param(
                "</ADMIN-DATA>",
                '</ADMIN-DATA><ADMIN-DATA><SDGS><SDG GID="VENDOR"><SD GID="X">payload</SD></SDG></SDGS></ADMIN-DATA>',
                id="a-second-admin-data-holding-a-foreign-sdg",
            ),
            pytest.param("</ADMIN-DATA>", "</ADMIN-DATA><ADMIN-DATA/>", id="a-second-admin-data-that-is-empty"),
            pytest.param("<ADMIN-DATA>", '<ADMIN-DATA T="2026-01-01">', id="an-attribute-on-admin-data"),
            pytest.param("<SDGS>", '<SDGS T="2026-01-01">', id="an-attribute-on-sdgs"),
            pytest.param("</SDGS>", "</SDGS><SDGS/>", id="an-extra-empty-sdgs"),
        ],
    )
    @pytest.mark.parametrize(
        "with_access_points", [False, True], ids=["data-types-alone", "access-points-and-data-types"]
    )
    def test_a_wrapper_around_the_exports_own_marker_aborts_the_import(
        self, tmp_path: Path, with_access_points: bool, anchor: str, spliced: str
    ) -> None:
        """A wrapper around the export's own marker aborts: the verdict judges every wrapper.

        Judging the first `ADMIN-DATA` alone would let each splice vanish silently.
        """
        if with_access_points:
            _source, arxml = _marked_export_with(tmp_path, _TARGET_ACCESSES)
        else:
            _source, arxml = _unmarked_export(tmp_path)
        text = arxml.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        arxml.write_text(text.replace(anchor, spliced), encoding="utf-8", newline="\n")
        project = init_project(tmp_path / "proj")

        with pytest.raises(LossyComponentImportError) as exc:
            plan_component_import(str(arxml), str(project / "model"))

        assert "vendor metadata" in str(exc.value)
        assert "SWC-INTERNAL-BEHAVIOR 'TargetSwc_InternalBehavior'" in str(exc.value)
        assert "the SWCRAFT group beside it is read" in str(exc.value)

    def test_whitespace_in_the_exports_own_marker_block_still_imports(self, tmp_path: Path) -> None:
        """The control for the test above: tab and carriage return are XML white space too.

        A file carrying them where the pretty-printer puts newlines still imports."""
        _source, arxml = _marked_export_with(tmp_path, _TARGET_ACCESSES)
        text = arxml.read_text(encoding="utf-8")
        for anchor in ('<SDG GID="SWCRAFT">', "no invented twin</SD>"):
            assert text.count(anchor) == 1
            text = text.replace(anchor, anchor + "\t&#13; ")
        arxml.write_text(text, encoding="utf-8", newline="\n")
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"

    @pytest.mark.parametrize(
        ("anchor", "spliced", "line"),
        [
            pytest.param(
                "</ADMIN-DATA>",
                '</ADMIN-DATA><ADMIN-DATA xmlns=""><SDGS><SDG GID="VENDOR"><SD GID="X">payload</SD></SDG></SDGS>'
                "</ADMIN-DATA>",
                "element ADMIN-DATA outside the AUTOSAR namespace (no namespace)",
                id="a-no-namespace-admin-data-beside-the-marker",
            ),
            pytest.param(
                "</ADMIN-DATA>",
                '</ADMIN-DATA><ADMIN-DATA xmlns="urn:vendor"><SDGS><SDG GID="VENDOR"><SD GID="X">payload</SD>'
                "</SDG></SDGS></ADMIN-DATA>",
                "element ADMIN-DATA outside the AUTOSAR namespace (urn:vendor)",
                id="a-foreign-namespace-admin-data-beside-the-marker",
            ),
            pytest.param(
                "<RUNNABLES>",
                '<RUNNABLES><x:EXTRA xmlns:x="urn:vendor">payload</x:EXTRA>',
                "element EXTRA outside the AUTOSAR namespace (urn:vendor)",
                id="a-foreign-element-inside-runnables",
            ),
            pytest.param(
                "<DATA-ELEMENTS>",
                '<DATA-ELEMENTS><x:EXTRA xmlns:x="urn:vendor">payload</x:EXTRA>',
                "element EXTRA outside the AUTOSAR namespace (urn:vendor)",
                id="a-foreign-element-inside-data-elements",
            ),
        ],
    )
    def test_content_outside_the_autosar_namespace_aborts_the_import(
        self, tmp_path: Path, anchor: str, spliced: str, line: str
    ) -> None:
        """Content outside the AUTOSAR namespace, or in none, aborts with a line; the walk never passes it silently."""
        _source, arxml = _unmarked_export(tmp_path)
        text = arxml.read_text(encoding="utf-8")
        assert anchor in text
        arxml.write_text(text.replace(anchor, spliced, 1), encoding="utf-8", newline="\n")
        project = init_project(tmp_path / "proj")

        with pytest.raises(LossyComponentImportError) as exc:
            plan_component_import(str(arxml), str(project / "model"))

        assert line in str(exc.value)
        assert "has no SWCraft representation -- not imported or preserved" in str(exc.value)


def _export_marked_with_value(tmp_path: Path, value: str) -> Path:
    """An export carrying no access point and an access-points marker of *value*, where the writer puts one.

    A trusted marker plus three empty lists says *this runnable accesses nothing*, so
    trusting a value this release does not recognize deletes the target's declarations.
    """
    _source, arxml = _unmarked_export(tmp_path)
    anchor = '<SDG GID="SWCRAFT">'
    text = arxml.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    arxml.write_text(
        text.replace(
            anchor,
            anchor + f'<SD GID="{ACCESS_POINTS.sd_gid}">{value}</SD>',
        ),
        encoding="utf-8",
        newline="\n",
    )
    return arxml


class TestTheReplaceFlagDecidesNothingWithoutUpdate:
    """`replace_access_lists` is ignored without `update`.

    The flag does not feed the `unchanged`/`conflict` verdict: on an unmarked export whose lists are the only
    divergence the call says `unchanged` with or without it. Does not change: the marker at `update=False`, under
    which differing lists are still a conflict `--update` resolves.
    """

    def _pair(self, tmp_path: Path) -> tuple[Path, Path]:
        """An unmarked export against a target whose lists are the ONLY difference.

        No second divergence, so the verdict is about the lists."""
        _source, arxml = _unmarked_export(tmp_path)
        return arxml, _target_with(tmp_path, arxml, _TARGET_ACCESSES)

    def test_the_flag_without_update_is_a_no_op_on_the_planner(self, tmp_path: Path) -> None:
        arxml, model_root = self._pair(tmp_path)
        before = files_of(model_root)

        report = import_component(str(arxml), str(model_root), replace_access_lists=True)

        assert report.swc_action == "unchanged"
        assert report.access_list_regime == "none"
        assert report.written_paths == []
        assert files_of(model_root) == before
        assert _entries_of(model_root)["writes"] == [("pSpeed", "speed")]

    def test_the_same_call_without_the_flag_answers_identically(self, tmp_path: Path) -> None:
        """The control: the flag changes nothing at all here, which is what "ignored" means."""
        arxml, model_root = self._pair(tmp_path)

        with_flag = import_component(str(arxml), str(model_root), replace_access_lists=True)
        without = import_component(str(arxml), str(model_root))

        assert _report_fields(with_flag) == _report_fields(without)

    def test_the_flag_still_decides_an_update(self, tmp_path: Path) -> None:
        """Non-vacuity: gating it on `update` did not disable it."""
        arxml, model_root = self._pair(tmp_path)

        report = import_component(str(arxml), str(model_root), update=True, replace_access_lists=True)

        assert report.swc_action == "updated"
        assert report.access_list_regime == "replace-flag"
        assert _entries_of(model_root) == {"reads": [], "writes": [], "calls": []}


class TestAMarkerValueThisReleaseDoesNotRecognizeIsUnmarked:
    """The marker is a value, not a key: only the writer's own ``ACCESS_POINTS.text`` is trusted.

    Any other value (a typo, a later release's, a third-party tool's) is unmarked: the
    conservative `carried` regime, never a refusal, and the report names the value.
    """

    def _pair(self, tmp_path: Path, value: str) -> Path:
        """A target declaring every list, diverging from *value*'s export by one non-list field."""
        arxml = _export_marked_with_value(tmp_path, value)
        model_root = _target_with(tmp_path, arxml, _TARGET_ACCESSES)
        swc_file = model_root / "swc" / "components" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"][0]["min_start_interval"] = 0.02
        _dump(swc_file, data)
        return model_root

    def test_the_target_lists_survive_an_update_and_the_report_names_the_value(self, tmp_path: Path) -> None:
        """An unrecognized value does not select `marked`: every entry survives and the report names the value."""
        model_root = self._pair(tmp_path, "wrong")
        arxml = tmp_path / "TargetSwc.arxml"

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert report.access_list_regime == "carried"
        assert report.preserved_access_lists == {"TargetRunnable": {"reads": 2, "writes": 1, "calls": 1}}
        assert _entries_of(model_root) == {
            "reads": [("rSpeedIn", "speed", None), ("rSpeedIn", "pose", "value")],
            "writes": [("pSpeed", "speed")],
            "calls": [("rCalib", "SetOffset")],
        }
        rendered = report.render()
        assert (
            "ACCESS-POINTS marker present with value 'wrong' -- not 'explicit'; "
            "the export is treated as unmarked" in rendered
        ), rendered

    def test_the_writers_own_value_is_the_one_that_is_trusted(self, tmp_path: Path) -> None:
        """The control: the writer's own value is the marked regime, with no unrecognized-value line."""
        model_root = self._pair(tmp_path, ACCESS_POINTS.text)
        arxml = tmp_path / "TargetSwc.arxml"

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.access_list_regime == "marked"
        assert report.access_marker_value is None
        assert _entries_of(model_root) == {"reads": [], "writes": [], "calls": []}
        assert "ACCESS-POINTS marker present with value" not in report.render()

    def test_an_empty_marker_value_is_unmarked_too(self, tmp_path: Path) -> None:
        """An empty marker value is unmarked too, and reported as ``''`` (``None`` means absent)."""
        model_root = self._pair(tmp_path, "")
        arxml = tmp_path / "TargetSwc.arxml"

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.access_list_regime == "carried"
        assert report.access_marker_value == ""
        assert "ACCESS-POINTS marker present with value ''" in report.render()


class TestTheImportRefusalAndTheModelRuleNameTheSameEntries:
    """`import component`'s refusal and `rule:swc.access.name-collision` name the same entries.

    Both judge one closure: the export is built from a source declaring the ports and
    members but no list (the writer refuses the collision), and the same source then
    declares the two entries. Does not reach: the wording, which each surface keeps.
    """

    PORTS = ["pA_B", "pA"]
    MEMBERS = ["C", "B_C"]

    #: `(list, port, member field, member)` as both surfaces spell a claimant.
    _CLAIMANT = re.compile(r"(reads|writes|calls) port '([^']+)' (element|operation) '([^']+)'")

    def _fixture(self, tmp_path: Path) -> tuple[Path, str]:
        """(the foreign export, the source root that declares the same pairs)."""
        source = _export_with_foreign_access(
            tmp_path,
            self.PORTS,
            self.MEMBERS,
            # Named as the writer derives them, the second alike ignoring case: the
            # settling pass states neither, so the derived form collides.
            _send_point("W_pA_B_C", self.PORTS[0], self.MEMBERS[0]),
            _send_point("w_pa_b_c", self.PORTS[1], self.MEMBERS[1]),
        )
        source_root = tmp_path / "source_model"
        swc_file = source_root / "swc" / "body" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"][0]["writes"] = [
            {"port": port, "element": member} for port, member in zip(self.PORTS, self.MEMBERS, strict=True)
        ]
        _dump(swc_file, data)
        return source, str(source_root)

    def test_both_surfaces_name_the_same_derived_name_and_the_same_entries(self, tmp_path: Path) -> None:
        export, source_root = self._fixture(tmp_path)
        project = init_project(tmp_path / "proj")
        emitted = access_point_name(WRITE_ACCESS_PREFIX, self.PORTS[0], self.MEMBERS[0])

        with pytest.raises(ReservedNameError) as raised:
            import_component(str(export), str(project / "model"))
        findings = [
            record
            for record in check_name_uniqueness(source_root).records
            if record.rule == "swc.access.name-collision"
        ]

        assert len(findings) == 1, [record.message for record in findings]
        refusal, finding = str(raised.value), findings[0].message
        assert emitted in refusal and emitted in finding
        assert set(self._CLAIMANT.findall(refusal)) == set(self._CLAIMANT.findall(finding))
        assert set(self._CLAIMANT.findall(finding)) == {
            ("writes", self.PORTS[0], "element", self.MEMBERS[0]),
            ("writes", self.PORTS[1], "element", self.MEMBERS[1]),
        }

    def test_the_conforming_fixture_is_clean_on_both_surfaces(self, tmp_path: Path) -> None:
        """Non-vacuity: with one entry gone, neither surface says anything."""
        export = _export_with_foreign_access(
            tmp_path, self.PORTS, self.MEMBERS, _send_point("Foreign_1", self.PORTS[0], self.MEMBERS[0])
        )
        source_root = tmp_path / "source_model"
        swc_file = source_root / "swc" / "body" / "TargetSwc.yaml"
        data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        data["runnables"][0]["writes"] = [{"port": self.PORTS[0], "element": self.MEMBERS[0]}]
        _dump(swc_file, data)
        project = init_project(tmp_path / "proj")

        assert import_component(str(export), str(project / "model")).swc_action == "created"
        assert [
            record
            for record in check_name_uniqueness(str(source_root)).records
            if record.rule == "swc.access.name-collision"
        ] == []


class TestAnUpdateWhoseClosureCarriesAReferenceType:
    """``import component --update`` reaches ``data_type_to_dict``'s reference-type arm.

    ``component_exchange/import_.py::_plan_type_creates``'s ``payload=data_type_to_dict(dt)`` of a
    created type is the one production path that reaches the serializer inside an update; the export
    hands it a ``ReferenceDataType`` written as ``TYPE_REFERENCE``, and the import hands one back. A
    created type is the only data type the update serializes (an existing one is reused or refused), so
    the file-level check covers every call.
    """

    @staticmethod
    def _source(tmp_path: Path) -> str:
        root = tmp_path / "reference_source"
        types = root / "data_types"
        _dump(
            model_element_path(root, "float32_base", kind="base_type"),
            {
                "name": "float32_base",
                "kind": "base_type",
                "size": 32,
                "encoding": "IEEE754",
                "native_declaration": "float32",
            },
        )
        _dump(types / "float32.yaml", {"name": "float32", "kind": "value_type", "base": "float32_base"})
        _dump(types / "SpeedRef.yaml", {"name": "SpeedRef", "kind": "reference_type", "base": "float32"})
        _dump(
            root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [
                    {"name": "raw", "implementation_type": "float32"},
                    {"name": "speed", "implementation_type": "SpeedRef"},
                ],
            },
        )
        _dump(
            root / "swc" / "body" / "TargetSwc.yaml",
            {
                "name": "TargetSwc",
                "type": "application",
                "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
                "handle_termination_and_restart": "no_support",
            },
        )
        return str(root)

    def test_the_update_writes_every_created_type_as_its_serializer_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from swcraft_tools.component_exchange import export as export_module
        from swcraft_tools.component_exchange import import_ as import_module

        reached: dict[str, list[tuple[str, Any, dict[str, Any]]]] = {"export": [], "import": []}

        def spy(side: str, real: Any) -> Any:
            def serialize(dt: Any) -> dict[str, Any]:
                payload = real(dt)
                reached[side].append((type(dt).__name__, dt, payload))
                return payload

            return serialize

        monkeypatch.setattr(export_module, "data_type_to_dict", spy("export", export_module.data_type_to_dict))
        monkeypatch.setattr(import_module, "data_type_to_dict", spy("import", import_module.data_type_to_dict))

        arxml = tmp_path / "TargetSwc.arxml"
        export_component(self._source(tmp_path), "TargetSwc", str(arxml))

        assert "ReferenceDataType" in {name for name, _, _ in reached["export"]}
        assert "<CATEGORY>TYPE_REFERENCE</CATEGORY>" in arxml.read_text(encoding="utf-8")

        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _dump(model_root / "swc" / "body" / "TargetSwc.yaml", {"name": "TargetSwc", "type": "application"})

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        written = {dt.name: dt for dt in _both_type_layers(str(model_root))}
        assert sorted(written) == ["SpeedRef", "float32", "float32_base"]
        assert sorted(element.name for _, element, _ in reached["import"]) == sorted(written)
        for _, element, payload in reached["import"]:
            assert written[element.name] == element
            on_disk = yaml.safe_load(
                model_element_path(model_root, element.name, kind=element.kind).read_text(encoding="utf-8")
            )
            assert on_disk == payload
        # The reference type arrives one step and the update serializes it through
        # the reference-type arm.
        assert {name for name, _, _ in reached["import"]} == {"BaseDataType", "ValueDataType", "ReferenceDataType"}
        assert written["SpeedRef"] == ReferenceDataType(name="SpeedRef", kind="reference_type", base="float32")


# What a declared type reads back as, through the exchange


def _data_type_source(tmp_path: Path, data_types: list[dict[str, Any]], elements: list[dict[str, Any]]) -> str:
    """A one-component source model: *data_types*, one interface ``IRaw`` over *elements*, one SWC."""
    root = tmp_path / "data_type_source"
    for data_type in data_types:
        _dump(model_element_path(root, data_type["name"], kind=data_type["kind"]), data_type)
    _dump(
        root / "interfaces" / "IRaw.yaml",
        {"name": "IRaw", "kind": "sender_receiver", "data_elements": elements},
    )
    _dump(
        root / "swc" / "body" / "TargetSwc.yaml",
        {
            "name": "TargetSwc",
            "type": "application",
            "ports": [{"name": "pRaw", "direction": "provide", "interface": "IRaw"}],
            "handle_termination_and_restart": "no_support",
        },
    )
    return str(root)


def _exchange(tmp_path: Path, source_root: str) -> tuple[Path, ComponentImportReport]:
    """Export ``TargetSwc`` from *source_root* and import it into a fresh project."""
    arxml = tmp_path / "TargetSwc.arxml"
    export_component(source_root, "TargetSwc", str(arxml))
    project = init_project(tmp_path / "proj")
    return project / "model", import_component(str(arxml), str(project / "model"))


class TestADeclaredBaseTypeAndImplementationTypeImportUnchanged:
    """A declared base type and a declared implementation type are never collapsed.

    A value type ``uint8_impl`` over a base type ``uint8`` is a declared pair; collapsing it
    would delete a declared type and re-type the data element by a base type, which no
    AUTOSAR release admits. The file carries the data-types marker, which is what lets the
    pair through; an unmarked earlier-release file is refused
    (``TestAComponentFileAnEarlierReleaseWroteIsRefusedByItsTwin``).
    """

    _TYPES: list[dict[str, Any]] = [
        {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
        {"name": "uint8_impl", "kind": "value_type", "base": "uint8"},
    ]

    def test_both_types_and_the_reference_to_the_implementation_type_round_trip(self, tmp_path: Path) -> None:
        source_root = _data_type_source(tmp_path, self._TYPES, [{"name": "raw", "implementation_type": "uint8_impl"}])

        model_root, report = _exchange(tmp_path, source_root)

        exported = (tmp_path / "TargetSwc.arxml").read_text(encoding="utf-8")
        assert f'<SD GID="{DATA_TYPES.sd_gid}">' in exported, "the file under test carries the marker"
        assert report.created_data_types == ["uint8", "uint8_impl"]
        assert {d.name: d for d in _both_type_layers(str(model_root))} == {
            d.name: d for d in _both_type_layers(source_root)
        }
        (iface,) = read_interfaces(str(model_root))
        assert [(de.name, de.implementation_type) for de in iface.data_elements] == [("raw", "uint8_impl")]

    def test_the_mapper_keeps_the_pair(self, tmp_path: Path) -> None:
        """The two kinds and the reference between them, straight off ``map_data_types``."""
        source_root = _data_type_source(tmp_path, self._TYPES, [{"name": "raw", "implementation_type": "uint8_impl"}])
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        model = read_arxml(str(arxml))

        forms: list = []
        data_types, registry, _skips = map_data_types(model, elements=forms)
        interfaces, _ = map_interfaces(model, registry, data_types, elements=forms)

        assert [d.name for d in data_types] == ["uint8", "uint8_impl"]
        assert interfaces[0].data_elements[0].implementation_type == "uint8_impl"


class TestAnImplementationTypeReadsBackAsTheKindItsCategoryDeclares:
    """An implementation type reads back as the kind its ``CATEGORY`` declares.

    ``VALUE`` reads back as a ``value_type`` over the base type its ``BASE-TYPE-REF`` names,
    ``TYPE_REFERENCE`` as a ``reference_type`` over the implementation type its
    ``IMPLEMENTATION-DATA-TYPE-REF`` names: one link each, however long the chain.
    """

    _TYPES: list[dict[str, Any]] = [
        {"name": "raw_base", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16_t"},
        {"name": "Raw", "kind": "value_type", "base": "raw_base"},
        {"name": "RawRef", "kind": "reference_type", "base": "Raw"},
        {"name": "RawRefRef", "kind": "reference_type", "base": "RawRef"},
    ]

    def test_every_link_reads_back_as_the_kind_it_was_declared(self, tmp_path: Path) -> None:
        """Every link named by a data element of its own, so no closure walk is needed to reach it."""
        source_root = _data_type_source(
            tmp_path,
            self._TYPES,
            [
                {"name": "a", "implementation_type": "Raw"},
                {"name": "b", "implementation_type": "RawRef"},
                {"name": "c", "implementation_type": "RawRefRef"},
            ],
        )

        model_root, _report = _exchange(tmp_path, source_root)

        written = {d.name: d for d in _both_type_layers(str(model_root))}
        assert written == {d.name: d for d in _both_type_layers(source_root)}
        assert {name: type(d).__name__ for name, d in written.items()} == {
            "raw_base": "BaseDataType",
            "Raw": "ValueDataType",
            "RawRef": "ReferenceDataType",
            "RawRefRef": "ReferenceDataType",
        }

    def test_a_chain_named_only_by_its_top_link_round_trips_whole(self, tmp_path: Path) -> None:
        """The same chain through the closure: only ``RawRefRef`` is named, and every link below it arrives."""
        source_root = _data_type_source(tmp_path, self._TYPES, [{"name": "c", "implementation_type": "RawRefRef"}])

        model_root, report = _exchange(tmp_path, source_root)

        assert report.created_data_types == [
            "raw_base",
            "Raw",
            "RawRef",
            "RawRefRef",
        ]  # the export's source-path order: Application, Base, Implementation
        assert {d.name: d for d in _both_type_layers(str(model_root))} == {
            d.name: d for d in _both_type_layers(source_root)
        }


#: The ``uint8`` / ``uint8`` shape: a base type whose declared native declaration IS its own name.
_NAME_EQUAL_TYPES: list[dict[str, Any]] = [
    {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
    {"name": "UInt8_T", "kind": "value_type", "base": "uint8"},
]


class TestANativeDeclarationEqualToTheTypeNameSurvivesTheExchange:
    """A ``native_declaration`` equal to its base type's name is a declaration like any other.

    ``import component`` does not reset it to empty on the written element or in the comparison, which would drop a
    real declaration and leave a ``check-model`` finding the architect never caused.
    """

    def test_the_key_survives_export_then_import(self, tmp_path: Path) -> None:
        source_root = _data_type_source(
            tmp_path, _NAME_EQUAL_TYPES, [{"name": "raw", "implementation_type": "UInt8_T"}]
        )

        model_root, report = _exchange(tmp_path, source_root)

        on_disk = yaml.safe_load(
            (model_element_path(model_root, "uint8", kind="base_type")).read_text(encoding="utf-8")
        )
        assert on_disk.get("native_declaration") == "uint8", on_disk
        assert {d.name: d for d in _both_type_layers(str(model_root))} == {
            d.name: d for d in _both_type_layers(source_root)
        }
        assert report.native_declaration_missing_lines == []
        assert "Base types written without a native declaration" not in report.render()
        assert not [
            record
            for record in check_cross_layer_consistency(model_root=str(model_root)).records
            if record.rule == "data-type.base-type-native-declaration-missing"
        ]

    def test_a_reimport_into_the_source_project_is_a_no_op(self, tmp_path: Path) -> None:
        source_root = _data_type_source(
            tmp_path, _NAME_EQUAL_TYPES, [{"name": "raw", "implementation_type": "UInt8_T"}]
        )
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        before = files_of(Path(source_root))

        report = import_component(str(arxml), source_root)

        assert (report.swc_action, report.reused_data_types, report.written_paths) == (
            "unchanged",
            ["uint8", "UInt8_T"],  # the export's source-path order: Application, Base, Implementation
            [],
        )
        assert files_of(Path(source_root)) == before

    def test_a_target_declaring_none_is_a_divergence_and_not_a_silent_reuse(self, tmp_path: Path) -> None:
        """A target declaring no native declaration, against an export declaring ``uint8``, is a conflict.

        Neither side is reset to empty, which would compare them equal: a silent reuse.
        """
        source_root = _data_type_source(
            tmp_path, _NAME_EQUAL_TYPES, [{"name": "raw", "implementation_type": "UInt8_T"}]
        )
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        write_model_element(model_root, {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE"})
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError, match="uint8") as exc_info:
            import_component(str(arxml), str(model_root))

        assert any("'uint8'" in entry for entry in exc_info.value.conflicts), exc_info.value.conflicts
        assert files_of(model_root) == before


class TestAForeignComponentWithoutANativeDeclarationIsListedWrittenAndReported:
    """A foreign component whose ``SW-BASE-TYPE`` carries no ``NATIVE-DECLARATION``, imported.

    Valid AUTOSAR (``minOccurs="0"`` at 4.2.2 and 4.3.0) and never written by SWCraft. The
    base type is written without the key and listed under ``import model``'s heading,
    nothing skipped, and ``check-model`` reports it until one is declared.
    """

    HEADING = "Base types written without a native declaration (check-model reports each)"

    @staticmethod
    def _foreign_export(tmp_path: Path) -> Path:
        source_root = _data_type_source(
            tmp_path, _NAME_EQUAL_TYPES, [{"name": "raw", "implementation_type": "UInt8_T"}]
        )
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        text, removed = re.subn(
            r"\n\s*<NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>", "", arxml.read_text(encoding="utf-8")
        )
        assert removed == 1, "precondition: the export declares exactly one native declaration"
        foreign = tmp_path / "foreign-no-native-declaration.arxml"
        foreign.write_text(text, encoding="utf-8")
        return foreign

    def test_the_report_lists_it_with_its_source_path_and_skips_nothing(self, tmp_path: Path) -> None:
        from swcraft_tools.arxml_import.report import NATIVE_DECLARATION_MISSING_HEADING

        foreign = self._foreign_export(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(foreign), str(project / "model"))

        assert NATIVE_DECLARATION_MISSING_HEADING == self.HEADING
        assert report.native_declaration_missing_lines == ["uint8 (/DataTypes/Base/uint8)"]
        rendered = report.render()
        assert rendered.count(f"{self.HEADING}:") == 1, rendered
        section = rendered.split(f"{self.HEADING}:", 1)[1].splitlines()[1:2]
        assert section == ["  - uint8 (/DataTypes/Base/uint8)"], rendered
        assert not [line for line in report.skip_lines if "uint8" in line], report.skip_lines
        assert report.created_data_types == [
            "uint8",
            "UInt8_T",
        ]  # the export's source-path order: Application, Base, Implementation

    def test_the_plan_the_mcp_tool_renders_and_a_dry_run_list_it_too(self, tmp_path: Path) -> None:
        foreign = self._foreign_export(tmp_path)
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        plan = plan_component_import(str(foreign), str(project / "model"), dry_run=True)
        dry = import_component(str(foreign), str(project / "model"), dry_run=True)

        assert plan.report.native_declaration_missing_lines == ["uint8 (/DataTypes/Base/uint8)"]
        assert dry.native_declaration_missing_lines == ["uint8 (/DataTypes/Base/uint8)"]
        assert f"{self.HEADING}:" in plan.report.render()
        assert files_of(project / "model") == before

    def test_the_written_file_carries_no_native_declaration_key(self, tmp_path: Path) -> None:
        foreign = self._foreign_export(tmp_path)
        project = init_project(tmp_path / "proj")

        import_component(str(foreign), str(project / "model"))

        on_disk = yaml.safe_load(
            (model_element_path(project / "model", "uint8", kind="base_type")).read_text(encoding="utf-8")
        )
        assert "native_declaration" not in on_disk, on_disk

    def test_check_model_reports_the_rule_and_generation_refuses(self, tmp_path: Path) -> None:
        foreign = self._foreign_export(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        import_component(str(foreign), str(model_root))

        # A fresh project links no requirement, so the component's own finding is
        # beside it; the data-type findings are the whole of what this asserts.
        records = check_cross_layer_consistency(model_root=str(model_root)).records
        assert [
            (record.rule, record.subjects[0].element) for record in records if record.rule.startswith("data-type.")
        ] == [("data-type.base-type-native-declaration-missing", "uint8")], records
        output = tmp_path / "out.arxml"
        with pytest.raises(ValueError, match=r"Base type\(s\) \['uint8'\]"):
            generate_arxml(str(model_root), str(output))
        assert not output.exists()

    def test_a_base_type_the_target_already_holds_is_reused_and_not_listed(self, tmp_path: Path) -> None:
        """Only a WRITTEN base type is listed: a reused one writes nothing, and its finding predates the import."""
        foreign = self._foreign_export(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        write_model_element(model_root, {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE"})

        report = import_component(str(foreign), str(model_root))

        assert report.reused_data_types == ["uint8"]
        assert report.native_declaration_missing_lines == []
        assert self.HEADING not in report.render()


class TestAComponentFileAnEarlierReleaseWroteIsRefusedByItsTwin:
    """An earlier writer's ``{B}_impl`` twin over every primitive ``B``: its unmarked file is refused.

    Three hand-trimmed fixtures in that writer's shape: ``ExampleSWC.arxml`` (an interface
    typed by the twin ``float32_impl``), ``TwinBesideAValueType.arxml`` (the twin beside a
    value type, referenced by nothing) and ``TwinBehindARecordView.arxml`` (a record field
    typed by ``float32_AppView``, mapped onto the twin). Nothing is converted: the file is
    re-exported with this release.

    The exporter's word decides, not the bytes: the refusal fires only where the behaviour
    carries no ``SD GID="DATA-TYPES"``, since a model of this release declaring a value type
    ``{B}_impl`` over a base type ``B`` exports the same shape. Does not reach: a file from
    another tool declaring such a twin without a SWCraft marker is refused with the same
    sentence; a hand-written marker is believed; a twin over a base type the file does not
    declare passes to the ordinary import.
    """

    FIXTURES = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "component_exchange" / "earlier_release"
    SOURCE = FIXTURES / "ExampleSWC.arxml"
    #: The two shapes the earlier writer reached its twin by other than typing a prototype by it.
    INDIRECT = ("TwinBesideAValueType.arxml", "TwinBehindARecordView.arxml")
    SENTENCE = (
        "this component file was written by an earlier SWCraft release (it carries the `float32_impl` "
        "implementation-type twin that release invented and no data-types marker); re-export it with this release"
    )

    @pytest.mark.parametrize("fixture", ("ExampleSWC.arxml", *INDIRECT))
    def test_the_fixture_is_not_git_ignored(self, fixture: str) -> None:
        """The fixture is not git-ignored: ``.gitignore`` ignores ``*.arxml``, negations non-recursive.

        Without the negation ``git add`` skips the fixture silently and every test here runs
        against a local copy a fresh clone lacks.
        """
        path = (self.FIXTURES / fixture).relative_to(REPO_ROOT)
        # --no-index so the answer comes from the ignore rules alone, and no --verbose
        # so the exit code stays the verdict (a matched negation makes --verbose exit 0).
        completed = subprocess.run(
            ["git", "check-ignore", "--no-index", path.as_posix()],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode != 0, f"{path} is git-ignored — add a negation to .gitignore"

    @staticmethod
    def _copy_of_examples_basic(tmp_path: Path) -> Path:
        project = tmp_path / "basic"
        shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", project / "model")
        shutil.copyfile(REPO_ROOT / "examples" / "basic" / "swcraft.yaml", project / "swcraft.yaml")
        return project

    def test_a_fresh_project_refuses_it_and_writes_nothing(self, tmp_path: Path) -> None:
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(ComponentExchangeError) as refused:
            import_component(str(self.SOURCE), str(project / "model"))

        assert str(refused.value) == self.SENTENCE
        assert type(refused.value).__name__ == "EarlierReleaseExportError"
        assert files_of(project / "model") == before

    def test_an_upgraded_project_holding_the_component_is_told_to_re_export_not_that_it_diverges(
        self, tmp_path: Path
    ) -> None:
        project = self._copy_of_examples_basic(tmp_path)

        with pytest.raises(ComponentExchangeError) as refused:
            plan_component_import(str(self.SOURCE), str(project / "model"), update=True, dry_run=True)

        assert str(refused.value) == self.SENTENCE

    def test_the_cli_prints_the_sentence_with_no_path_and_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = init_project(tmp_path / "proj")
        manifest = str(project / "swcraft.yaml")
        argv = ["swcraft", "import", "component", "--source", str(self.SOURCE), "--project", manifest]
        monkeypatch.setattr("sys.argv", [*argv, "--dry-run"])

        with pytest.raises(SystemExit) as exited:
            main()

        assert exited.value.code == 1
        err = capsys.readouterr().err
        assert f"Error: {self.SENTENCE}\n" in err, err
        assert str(tmp_path) not in err

    def test_an_export_of_this_release_is_not_mistaken_for_one(self, tmp_path: Path) -> None:
        """``examples/basic`` exported now declares ``float32_base`` and ``float32``: no twin, and it imports."""
        exported = tmp_path / "ExampleSWC.arxml"
        export_component(str(REPO_ROOT / "examples" / "basic" / "model"), "ExampleSWC", str(exported))
        project = init_project(tmp_path / "proj")

        report = import_component(str(exported), str(project / "model"), dry_run=True)

        assert report.swc == "ExampleSWC"

    @pytest.mark.parametrize("fixture", INDIRECT)
    def test_a_twin_no_prototype_is_typed_by_is_refused_too(self, fixture: str, tmp_path: Path) -> None:
        """Whatever reaches the twin is refused, not only a prototype typed by it.

        Neither file imports ``float32_impl`` (or ``float32_AppView`` from the record).
        """
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(ComponentExchangeError) as refused:
            import_component(str(self.FIXTURES / fixture), str(project / "model"))

        assert str(refused.value) == self.SENTENCE
        assert type(refused.value).__name__ == "EarlierReleaseExportError"
        assert files_of(project / "model") == before

    @pytest.mark.parametrize("fixture", INDIRECT)
    def test_an_upgraded_project_is_told_to_re_export_for_either_indirect_shape(
        self, fixture: str, tmp_path: Path
    ) -> None:
        project = self._copy_of_examples_basic(tmp_path)

        with pytest.raises(ComponentExchangeError) as refused:
            plan_component_import(str(self.FIXTURES / fixture), str(project / "model"), update=True, dry_run=True)

        assert str(refused.value) == self.SENTENCE

    def test_a_record_over_a_twin_through_a_view_named_for_the_base_is_decided_by_the_marker(
        self, tmp_path: Path
    ) -> None:
        """On the record-through-view shape the data-types marker is the whole discriminator.

        The record field is typed by ``float32_AppView``, which the mapping set maps onto the
        twin ``float32_impl``, so the twin is reached only through the view and the set. Unmarked, the
        fixture is refused with the re-export sentence; with the data-types marker the same bytes import.
        """
        fixture = self.FIXTURES / "TwinBehindARecordView.arxml"
        anchor = "<SHORT-NAME>ExampleSWC_InternalBehavior</SHORT-NAME>"
        text = fixture.read_text(encoding="utf-8")
        marked = tmp_path / "Marked.arxml"
        marked.write_text(
            text.replace(
                anchor,
                anchor + f'<ADMIN-DATA><SDGS><SDG GID="SWCRAFT"><SD GID="{DATA_TYPES.sd_gid}">'
                "any text</SD></SDG></SDGS></ADMIN-DATA>",
            ),
            encoding="utf-8",
            newline="\n",
        )
        project = init_project(tmp_path / "proj")

        assert import_component(str(marked), str(project / "model"), dry_run=True).swc == "ExampleSWC"
        with pytest.raises(ComponentExchangeError) as refused:
            import_component(str(fixture), str(project / "model"), dry_run=True)

        assert str(refused.value) == self.SENTENCE

    def test_an_access_points_marker_alone_does_not_answer_it(self, tmp_path: Path) -> None:
        """The releases that wrote the access-points marker still invented the twin, so that group alone is refused."""
        source = tmp_path / "ExampleSWC.arxml"
        anchor = "<SHORT-NAME>ExampleSWC_InternalBehavior</SHORT-NAME>"
        text = self.SOURCE.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        source.write_text(
            text.replace(
                anchor,
                anchor + f'<ADMIN-DATA><SDGS><SDG GID="SWCRAFT"><SD GID="{ACCESS_POINTS.sd_gid}">'
                f"{ACCESS_POINTS.text}</SD></SDG></SDGS></ADMIN-DATA>",
            ),
            encoding="utf-8",
            newline="\n",
        )
        project = init_project(tmp_path / "proj")

        with pytest.raises(ComponentExchangeError) as refused:
            import_component(str(source), str(project / "model"), dry_run=True)

        assert str(refused.value) == self.SENTENCE

    def test_the_data_types_marker_on_the_same_bytes_lets_the_twin_through(self, tmp_path: Path) -> None:
        """The marker is the only discriminator, and its key decides: the refused fixture with it imports the twin."""
        source = tmp_path / "ExampleSWC.arxml"
        anchor = "<SHORT-NAME>ExampleSWC_InternalBehavior</SHORT-NAME>"
        text = self.SOURCE.read_text(encoding="utf-8")
        source.write_text(
            text.replace(
                anchor,
                anchor + f'<ADMIN-DATA><SDGS><SDG GID="SWCRAFT"><SD GID="{DATA_TYPES.sd_gid}">'
                "any text</SD></SDG></SDGS></ADMIN-DATA>",
            ),
            encoding="utf-8",
            newline="\n",
        )
        project = init_project(tmp_path / "proj")

        report = import_component(str(source), str(project / "model"), dry_run=True)

        assert report.created_data_types == ["float32", "float32_impl"]

    def test_an_unmarked_file_with_no_twin_imports_as_the_marked_one_does(self, tmp_path: Path) -> None:
        """No false refusal: ``examples/basic`` exported now, its marker removed, answers as the marked file does."""
        marked = tmp_path / "ExampleSWC.arxml"
        export_component(str(REPO_ROOT / "examples" / "basic" / "model"), "ExampleSWC", str(marked))
        unmarked = tmp_path / "Unmarked.arxml"
        unmarked.write_text(
            _without_the_data_types_marker(marked.read_text(encoding="utf-8")), encoding="utf-8", newline="\n"
        )
        project = init_project(tmp_path / "proj")

        from_marked = import_component(str(marked), str(project / "model"), dry_run=True)
        from_unmarked = import_component(str(unmarked), str(project / "model"), dry_run=True)

        assert from_unmarked.render().replace(str(unmarked), str(marked)) == from_marked.render()
        assert from_unmarked.created_data_types == [
            "float32_base",
            "float32",
        ]  # the export's source-path order: Application, Base, Implementation


def _without_the_data_types_marker(text: str) -> str:
    """*text* without its one ``ADMIN-DATA``: the file an exporter writing no marker would write."""
    stripped, count = re.subn(r"\n *<ADMIN-DATA>.*?</ADMIN-DATA>", "", text, flags=re.DOTALL)
    assert count == 1, "precondition: exactly one ADMIN-DATA block"
    assert "ACCESS-POINTS" not in text, "precondition: the block holds the data-types marker alone"
    return stripped


# The mapping sets a component names travel with its closure


def _physical_source(tmp_path: Path, *, named_sets: list[str] | None = None) -> str:
    """A one-component source model whose port speaks the application type ``Spd_T``.

    ``PhysMaps`` also maps ``Other_T``, which nothing in the closure reaches, so the
    exported set is wider than the closure. ``named_sets`` is the component's
    ``data_type_mappings``.
    """
    root = tmp_path / "physical_source"
    types: list[dict[str, Any]] = [
        {"name": "u8b", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        {"name": "u8", "kind": "value_type", "base": "u8b"},
        {"name": "u16b", "kind": "base_type", "size": 16, "encoding": "NONE", "native_declaration": "uint16"},
        {"name": "u16", "kind": "value_type", "base": "u16b"},
        {
            "name": "Spd_T",
            "kind": "application_type",
            "unit": "km/h",
            "conversion": {"factor": 0.1, "offset": 2.0},
            "range": {"min": 0.0, "max": 400.0},
        },
        {"name": "Other_T", "kind": "application_type", "unit": "deg"},
    ]
    for data_type in types:
        _dump(model_element_path(root, data_type["name"], kind=data_type["kind"]), data_type)
    write_model_element(
        root,
        {
            "name": "PhysMaps",
            "maps": [
                {"application": "Spd_T", "implementation": "u8"},
                {"application": "Other_T", "implementation": "u16"},
            ],
        },
        layer="mapping_sets",
    )
    _dump(
        root / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "application_type": "Spd_T"}],
        },
    )
    _dump(
        root / "swc" / "body" / "TargetSwc.yaml",
        {
            "name": "TargetSwc",
            "type": "application",
            "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
            "data_type_mappings": list(named_sets if named_sets is not None else ["PhysMaps"]),
            "handle_termination_and_restart": "no_support",
        },
    )
    return str(root)


class TestAComponentCarriesTheMappingSetsItNames:
    """A closure is the component's types and the mapping sets that pair them.

    Without its set the target would carry a ``rule:data-type.application-type-unmapped``
    finding the exporting project did not have, and a ``DATA-TYPE-MAPPING-REF`` onto an
    undeclared set is a dangling reference the writer refuses. The set travels narrowed to
    the closure, since a map the component does not speak drags in a type it does not own;
    re-importing into the exporting project therefore compares a subset.
    """

    def test_the_export_carries_the_named_set_narrowed_to_the_closure(self, tmp_path: Path) -> None:
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(_physical_source(tmp_path), "TargetSwc", str(arxml))

        text = arxml.read_text(encoding="utf-8")
        assert "<SHORT-NAME>PhysMaps</SHORT-NAME>" in text
        assert "/DataTypes/Application/Spd_T" in text
        assert "Other_T" not in text, "a map the closure does not reach drags in a type the component never speaks"

    def test_the_closure_gains_the_stores_of_the_maps_it_keeps(self, tmp_path: Path) -> None:
        """The implementation side of a kept map is a reference the export must resolve."""
        arxml = tmp_path / "TargetSwc.arxml"
        report = export_component(_physical_source(tmp_path), "TargetSwc", str(arxml))

        assert report.data_types == ["Spd_T", "u8", "u8b"]

    def test_the_round_trip_into_a_fresh_project_writes_the_set(self, tmp_path: Path) -> None:
        source_root = _physical_source(tmp_path)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.created_mapping_sets == ["PhysMaps"]
        written = yaml.safe_load(
            (model_element_path(project / "model", "PhysMaps", layer="mapping_sets")).read_text(encoding="utf-8")
        )
        assert written == {"name": "PhysMaps", "maps": [{"application": "Spd_T", "implementation": "u8"}]}
        swc = yaml.safe_load((project / "model" / "swc" / "components" / "TargetSwc.yaml").read_text(encoding="utf-8"))
        assert swc["data_type_mappings"] == ["PhysMaps"]

    def test_the_imported_project_is_consistent(self, tmp_path: Path) -> None:
        """End to end: the physical type the component speaks is mapped in the target too."""
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(_physical_source(tmp_path), "TargetSwc", str(arxml))
        project = init_project(tmp_path / "proj")

        import_component(str(arxml), str(project / "model"))

        result = check_cross_layer_consistency(str(project / "model"))
        assert [
            issue for issue in result.records if issue.rule.startswith(("data-type.", "mapping-set.", "swc."))
        ] == []

    def test_a_named_set_the_model_does_not_declare_refuses_the_export(self, tmp_path: Path) -> None:
        source_root = _physical_source(tmp_path, named_sets=["NoSuchSet"])
        arxml = tmp_path / "TargetSwc.arxml"

        with pytest.raises(ComponentExchangeError) as refused:
            export_component(source_root, "TargetSwc", str(arxml))

        assert "NoSuchSet" in str(refused.value)
        assert not arxml.exists()

    def test_re_importing_into_the_exporting_project_reuses_the_wider_set(self, tmp_path: Path) -> None:
        """The narrowing's stated cost, answered: a subset of the target's set is not a divergence."""
        source_root = _physical_source(tmp_path)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))

        report = plan_component_import(str(arxml), source_root, dry_run=True).report

        assert report.reused_mapping_sets == ["PhysMaps"]
        assert report.created_mapping_sets == []

    def test_a_map_the_target_set_does_not_carry_extends_it(self, tmp_path: Path) -> None:
        """A pair the target's set says nothing about is added to it, planned as a rewrite of the set's file."""
        source_root = _physical_source(tmp_path)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        target = _physical_source(tmp_path / "target")
        write_model_element(Path(target), {"name": "PhysMaps", "maps": []}, layer="mapping_sets")

        plan = plan_component_import(str(arxml), target, update=True, dry_run=True)

        assert plan.report.extended_mapping_sets == {"PhysMaps": ["Spd_T -> u8"]}
        assert plan.report.reused_mapping_sets == []
        [set_target] = [target for target in plan.targets if target.layer == "mapping_sets"]
        assert set_target.path == model_element_path(Path(target), "PhysMaps", layer="mapping_sets")
        assert set_target.payload == {"name": "PhysMaps", "maps": [{"application": "Spd_T", "implementation": "u8"}]}

    def test_a_map_the_target_set_spells_differently_is_a_conflict(self, tmp_path: Path) -> None:
        source_root = _physical_source(tmp_path)
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(source_root, "TargetSwc", str(arxml))
        target = _physical_source(tmp_path / "target")
        write_model_element(
            Path(target),
            {"name": "PhysMaps", "maps": [{"application": "Spd_T", "implementation": "u16"}]},
            layer="mapping_sets",
        )

        with pytest.raises(ComponentImportConflictError) as refused:
            plan_component_import(str(arxml), target, update=True, dry_run=True)

        assert "PhysMaps" in str(refused.value)


def _two_component_source(tmp_path: Path) -> str:
    """:func:`_physical_source` with a second component ``OtherSwc`` on the same set, speaking ``Other_T``.

    Each export carries a different narrowing of one set under one name, the shape every
    assembly of components exported from one project has.
    """
    root = Path(_physical_source(tmp_path))
    _dump(
        root / "interfaces" / "IOther.yaml",
        {
            "name": "IOther",
            "kind": "sender_receiver",
            "data_elements": [{"name": "other", "application_type": "Other_T"}],
        },
    )
    _dump(
        root / "swc" / "body" / "OtherSwc.yaml",
        {
            "name": "OtherSwc",
            "type": "application",
            "ports": [{"name": "pOther", "direction": "provide", "interface": "IOther"}],
            "data_type_mappings": ["PhysMaps"],
            "handle_termination_and_restart": "no_support",
        },
    )
    return str(root)


def _two_exports(tmp_path: Path) -> tuple[Path, Path]:
    source_root = _two_component_source(tmp_path)
    exports = tmp_path / "exports"
    exports.mkdir()
    for swc in ("TargetSwc", "OtherSwc"):
        export_component(source_root, swc, str(exports / f"{swc}.arxml"))
    return exports / "TargetSwc.arxml", exports / "OtherSwc.arxml"


class TestTwoComponentsExportedFromOneProjectAssemble:
    """Two components exported from one project assemble: a missing map extends the target's set.

    The exporter narrows a shared set to each closure and keeps its name, so two exports
    of one set disagree by construction. What is refused is a contradiction: one
    application type paired with two implementation types inside the set, or by any
    component across the sets it names as the whole import leaves them
    (:class:`TestNoImportLeavesOneApplicationTypeStoredTwoWays`).
    """

    @pytest.mark.parametrize("order", [("TargetSwc", "OtherSwc"), ("OtherSwc", "TargetSwc")])
    def test_both_import_in_either_order_and_the_project_builds(self, tmp_path: Path, order: tuple[str, str]) -> None:
        target_arxml, other_arxml = _two_exports(tmp_path)
        exports = {"TargetSwc": target_arxml, "OtherSwc": other_arxml}
        project = init_project(tmp_path / "proj")
        model = project / "model"

        first = import_component(str(exports[order[0]]), str(model))
        second = import_component(str(exports[order[1]]), str(model))

        assert first.created_mapping_sets == ["PhysMaps"]
        assert second.created_mapping_sets == []
        assert list(second.extended_mapping_sets) == ["PhysMaps"]
        written = yaml.safe_load(
            (model_element_path(model, "PhysMaps", layer="mapping_sets")).read_text(encoding="utf-8")
        )
        pairs = {"TargetSwc": ("Spd_T", "u8"), "OtherSwc": ("Other_T", "u16")}
        assert written == {
            "name": "PhysMaps",
            "maps": [{"application": pairs[name][0], "implementation": pairs[name][1]} for name in order],
        }
        # The one finding a component import always leaves is the requirement
        # link, integration-owned content kept out of an export.
        result = check_cross_layer_consistency(str(model))
        assert [record.rule for record in result.records if "requirement" not in record.message] == []
        generate_arxml(str(model), str(tmp_path / "out.arxml"))

    def test_a_third_import_of_a_component_already_covered_reuses_the_extended_set(self, tmp_path: Path) -> None:
        target_arxml, other_arxml = _two_exports(tmp_path)
        project = init_project(tmp_path / "proj")
        model = project / "model"
        import_component(str(target_arxml), str(model))
        import_component(str(other_arxml), str(model))

        again = import_component(str(target_arxml), str(model))

        assert again.reused_mapping_sets == ["PhysMaps"]
        assert again.extended_mapping_sets == {}

    def test_the_dry_run_names_every_added_map_and_writes_nothing(self, tmp_path: Path) -> None:
        target_arxml, other_arxml = _two_exports(tmp_path)
        project = init_project(tmp_path / "proj")
        model = project / "model"
        import_component(str(target_arxml), str(model))
        before = files_of(model)

        report = import_component(str(other_arxml), str(model), dry_run=True)

        assert files_of(model) == before
        rendered = report.render()
        assert "EXTENDED 1 mapping set(s)\n  - PhysMaps: adds Other_T -> u16" in rendered
        assert report.written_paths == []

    def test_a_superset_is_merged_and_only_the_missing_map_is_added(self, tmp_path: Path) -> None:
        """A component speaking both types carries both maps; the target holds one."""
        source_root = Path(_two_component_source(tmp_path))
        both = yaml.safe_load((source_root / "swc" / "body" / "TargetSwc.yaml").read_text(encoding="utf-8"))
        both["ports"].append({"name": "pOther", "direction": "provide", "interface": "IOther"})
        _dump(source_root / "swc" / "body" / "TargetSwc.yaml", both)
        arxml = tmp_path / "Both.arxml"
        export_component(str(source_root), "TargetSwc", str(arxml))
        _, other_arxml = _two_exports(tmp_path / "second")
        project = init_project(tmp_path / "proj")
        model = project / "model"
        import_component(str(other_arxml), str(model))

        report = import_component(str(arxml), str(model))

        assert report.extended_mapping_sets == {"PhysMaps": ["Spd_T -> u8"]}
        written = yaml.safe_load(
            (model_element_path(model, "PhysMaps", layer="mapping_sets")).read_text(encoding="utf-8")
        )
        assert written["maps"] == [
            {"application": "Other_T", "implementation": "u16"},
            {"application": "Spd_T", "implementation": "u8"},
        ]

    def test_a_contradicting_map_is_refused_and_nothing_is_written(self, tmp_path: Path) -> None:
        target_arxml, other_arxml = _two_exports(tmp_path)
        project = init_project(tmp_path / "proj")
        model = project / "model"
        import_component(str(target_arxml), str(model))
        # The target's set already pairs the second component's type with another store.
        write_model_element(
            model,
            {
                "name": "PhysMaps",
                "maps": [
                    {"application": "Spd_T", "implementation": "u8"},
                    {"application": "Other_T", "implementation": "u8"},
                ],
            },
            layer="mapping_sets",
        )
        write_model_element(model, {"name": "Other_T", "kind": "application_type", "unit": "deg"})
        before = files_of(model)

        with pytest.raises(ComponentImportConflictError) as refused:
            import_component(str(other_arxml), str(model), update=True)

        assert "it maps 'Other_T' onto 'u16' where the target's set maps it onto 'u8'" in str(refused.value)
        assert files_of(model) == before

    def test_a_map_another_component_pairs_differently_through_a_second_set_is_refused(self, tmp_path: Path) -> None:
        """The one way an added map is not inert.

        A target component naming the set also names a second set pairing the same type with another store."""
        target_arxml, other_arxml = _two_exports(tmp_path)
        project = init_project(tmp_path / "proj")
        model = project / "model"
        import_component(str(target_arxml), str(model))
        write_model_element(
            model,
            {"name": "LegacyMaps", "maps": [{"application": "Other_T", "implementation": "u8"}]},
            layer="mapping_sets",
        )
        write_model_element(model, {"name": "Other_T", "kind": "application_type", "unit": "deg"})
        swc_file = model / "swc" / "components" / "TargetSwc.yaml"
        swc = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        swc["data_type_mappings"] = ["PhysMaps", "LegacyMaps"]
        _dump(swc_file, swc)
        before = files_of(model)

        with pytest.raises(ComponentImportConflictError) as refused:
            import_component(str(other_arxml), str(model))

        assert (
            "component 'TargetSwc' would map 'Other_T' onto 'u16' through set 'PhysMaps' "
            "and onto 'u8' through set 'LegacyMaps'"
        ) in str(refused.value)
        assert files_of(model) == before


_OTHER_PORT = {"name": "pOther", "direction": "provide", "interface": "IOther"}
_IOTHER = {
    "name": "IOther",
    "kind": "sender_receiver",
    "data_elements": [{"name": "other", "application_type": "Other_T"}],
}


def _maps(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"application": application, "implementation": implementation} for application, implementation in pairs]


def _export_naming(
    tmp_path: Path,
    sets: dict[str, list[tuple[str, str]]],
    *,
    speaks_other: bool = False,
    unchecked: pytest.MonkeyPatch | None = None,
) -> Path:
    """``TargetSwc`` exported from a source whose sets are exactly *sets*, named in that order.

    ``speaks_other`` adds the second port, on ``Other_T``; a set pairing only a type the
    component does not speak travels empty. ``unchecked`` lifts, for this export alone,
    the closure check and the writer's refusal of sets pairing one application type two
    ways, so a test can build such a file as another tool would send it.
    """
    if unchecked is not None:
        from swcraft_tools.component_exchange import export as export_module
        from swcraft_tools.validator import ConsistencyResult
        from swcraft_tools.writer import arxml_generation

        unchecked.setattr(
            export_module,
            "check_application_type_double_mapping",
            lambda _root: ConsistencyResult(consistent=True, records=()),
        )
        unchecked.setattr(arxml_generation, "_check_application_type_double_mappings", lambda *_args: None)
    root = Path(_physical_source(tmp_path / "importing", named_sets=list(sets)))
    for name, pairs in sets.items():
        write_model_element(root, {"name": name, "maps": _maps(pairs)}, layer="mapping_sets")
    if speaks_other:
        _dump(root / "interfaces" / "IOther.yaml", _IOTHER)
        swc_file = root / "swc" / "body" / "TargetSwc.yaml"
        swc = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        swc["ports"].append(_OTHER_PORT)
        _dump(swc_file, swc)
    arxml = tmp_path / "TargetSwc.arxml"
    export_component(str(root), "TargetSwc", str(arxml))
    return arxml


def _mapping_target(tmp_path: Path, sets: dict[str, list[tuple[str, str]]], components: dict[str, list[str]]) -> Path:
    """A fresh project's model root holding the source's types, *sets* and *components*.

    Every data type is the source's own file, so only the mapping sets are decided; each
    component speaks ``Other_T`` and names the sets *components* gives it.
    """
    model = init_project(tmp_path / "proj") / "model"
    source = Path(_physical_source(tmp_path / "types"))
    # The three data description layers, each in its own directory under `data_types/`.
    for path in sorted(
        [
            *source.glob("data_types/implementation/*.yaml"),
            *source.glob("data_types/base/*.yaml"),
            *source.glob("data_types/application/*.yaml"),
        ],
        key=lambda p: p.as_posix(),
    ):
        _dump(model / path.relative_to(source), yaml.safe_load(path.read_text(encoding="utf-8")))
    _dump(model / "interfaces" / "IOther.yaml", _IOTHER)
    for name, pairs in sets.items():
        write_model_element(model, {"name": name, "maps": _maps(pairs)}, layer="mapping_sets")
    for name, named in components.items():
        _dump(
            model / "swc" / "components" / f"{name}.yaml",
            {
                "name": name,
                "type": "application",
                "ports": [_OTHER_PORT],
                "data_type_mappings": named,
                "handle_termination_and_restart": "no_support",
            },
        )
    return model


def _assert_refused_and_untouched(tmp_path: Path, arxml: Path, model: Path, expected: list[str]) -> None:
    """The import and its dry run both refuse naming *expected*, write nothing, and leave a building target."""
    before = files_of(model)
    for dry_run in (True, False):
        with pytest.raises(ComponentImportConflictError) as refused:
            import_component(str(arxml), str(model), dry_run=dry_run)
        message = str(refused.value)
        assert message.startswith("component import aborted with nothing written: ")
        for sentence in expected:
            assert sentence in message
        assert str(tmp_path) not in message, "a refusal names no storage location (the path-free invariant)"
        assert files_of(model) == before
    result = check_cross_layer_consistency(str(model))
    assert [record.rule for record in result.records if "requirement" not in record.message] == []
    generate_arxml(str(model), str(tmp_path / "out.arxml"))


class TestNoImportLeavesOneApplicationTypeStoredTwoWays:
    """Decided over the sets the whole import leaves: no application type ends up stored two ways.

    Comparing each incoming set alone with the target misses three shapes that pair one application type with two
    implementation types; such a project is ``rule:data-type.application-type-double-mapped`` and a ``generate arxml``
    refusal (``[constr_1004]``'s *exactly one* half). One test per shape, then the cases the rule must not refuse.
    """

    def test_one_import_extending_two_sets_two_ways_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shape 1: both extensions come in one import; a target component names both sets.

        The file violates ``[constr_1004]``, so SWCraft's export refuses to write it (the
        case below); the import's defence is exercised on one built with both refusals lifted.
        """
        arxml = _export_naming(
            tmp_path,
            {"PhysMaps": [("Spd_T", "u8")], "AltMaps": [("Spd_T", "u16")]},
            unchecked=monkeypatch,
        )
        monkeypatch.undo()
        model = _mapping_target(
            tmp_path,
            {"PhysMaps": [("Other_T", "u16")], "AltMaps": [("Other_T", "u16")]},
            {"OtherSwc": ["PhysMaps", "AltMaps"]},
        )

        _assert_refused_and_untouched(
            tmp_path,
            arxml,
            model,
            [
                "component 'OtherSwc' would map 'Spd_T' onto 'u8' through set 'PhysMaps' "
                "and onto 'u16' through set 'AltMaps'",
                "component 'TargetSwc' would map 'Spd_T' onto 'u8' through set 'PhysMaps' "
                "and onto 'u16' through set 'AltMaps'",
            ],
        )

    def test_swcrafts_own_export_refuses_a_component_whose_sets_pair_one_type_two_ways(self, tmp_path: Path) -> None:
        """The closure check refuses shape 1's source before the writer runs."""
        from swcraft_tools.component_exchange.export import ClosureValidationError

        with pytest.raises(ClosureValidationError, match=r"\[constr_1004\]"):
            _export_naming(tmp_path, {"PhysMaps": [("Spd_T", "u8")], "AltMaps": [("Spd_T", "u16")]})
        assert not (tmp_path / "TargetSwc.arxml").exists()

    def test_a_reused_second_set_pairing_the_extended_type_differently_is_refused(self, tmp_path: Path) -> None:
        """Shape 2: the imported component's second set is a plain reuse the target already maps differently."""
        arxml = _export_naming(
            tmp_path, {"PhysMaps": [("Spd_T", "u8")], "AltMaps": [("Other_T", "u16")]}, speaks_other=True
        )
        model = _mapping_target(
            tmp_path,
            {"PhysMaps": [("Other_T", "u16")], "AltMaps": [("Other_T", "u16"), ("Spd_T", "u16")]},
            {"OtherSwc": ["PhysMaps"]},
        )

        _assert_refused_and_untouched(
            tmp_path,
            arxml,
            model,
            [
                "component 'TargetSwc' would map 'Spd_T' onto 'u8' through set 'PhysMaps' "
                "and onto 'u16' through set 'AltMaps'"
            ],
        )

    def test_a_second_set_the_export_carries_empty_does_not_hide_the_targets_pairing(self, tmp_path: Path) -> None:
        """Shape 3: the second set travels empty, so it is reused, and the target's copy maps the type."""
        arxml = _export_naming(tmp_path, {"PhysMaps": [("Spd_T", "u8")], "AltMaps": [("Other_T", "u16")]})
        assert "<SHORT-NAME>AltMaps</SHORT-NAME>" in arxml.read_text(encoding="utf-8")
        model = _mapping_target(tmp_path, {"PhysMaps": [], "AltMaps": [("Spd_T", "u16")]}, {})

        _assert_refused_and_untouched(
            tmp_path,
            arxml,
            model,
            [
                "component 'TargetSwc' would map 'Spd_T' onto 'u8' through set 'PhysMaps' "
                "and onto 'u16' through set 'AltMaps'"
            ],
        )

    def test_two_sets_agreeing_on_one_implementation_type_are_not_refused(self, tmp_path: Path) -> None:
        """Agreement is not a contradiction: shape 1 with both extensions onto one store."""
        arxml = _export_naming(tmp_path, {"PhysMaps": [("Spd_T", "u8")], "AltMaps": [("Spd_T", "u8")]})
        model = _mapping_target(
            tmp_path,
            {"PhysMaps": [("Other_T", "u16")], "AltMaps": [("Other_T", "u16")]},
            {"OtherSwc": ["PhysMaps", "AltMaps"]},
        )

        report = import_component(str(arxml), str(model))

        assert report.extended_mapping_sets == {"PhysMaps": ["Spd_T -> u8"], "AltMaps": ["Spd_T -> u8"]}
        assert report.extended_mapping_set_components == {"PhysMaps": ["OtherSwc"], "AltMaps": ["OtherSwc"]}
        assert "  - PhysMaps: adds Spd_T -> u8 (named by OtherSwc)" in report.render()
        result = check_cross_layer_consistency(str(model))
        assert [record.rule for record in result.records if "requirement" not in record.message] == []
        generate_arxml(str(model), str(tmp_path / "out.arxml"))

    def test_a_double_mapping_that_stood_before_the_import_is_not_this_imports(self, tmp_path: Path) -> None:
        """A double mapping that stood before the import, and that it does not widen, is left as it stood.

        The defect is the target's and not silent: ``check-model`` reports the same component
        under ``rule:data-type.application-type-double-mapped`` before and after.
        """
        from swcraft_tools.validator import check_application_type_double_mapping

        arxml = _export_naming(tmp_path, {"PhysMaps": [("Spd_T", "u8")]})
        model = _mapping_target(
            tmp_path,
            {"PhysMaps": [("Other_T", "u16")], "AltMaps": [("Other_T", "u8")]},
            {"OtherSwc": ["PhysMaps", "AltMaps"]},
        )

        def reported() -> list[tuple[str, str]]:
            return [
                (record.rule, record.subjects[0].element)
                for record in check_application_type_double_mapping(str(model)).records
            ]

        assert reported() == [("data-type.application-type-double-mapped", "OtherSwc")]

        report = import_component(str(arxml), str(model))

        assert report.extended_mapping_sets == {"PhysMaps": ["Spd_T -> u8"]}
        assert reported() == [("data-type.application-type-double-mapped", "OtherSwc")]


class TestAnApplicationTypesPhysicsIsComparedExactly:
    """An application type's unit, conversion and range are compared exactly.

    The exported ARXML carries all three, so a target definition differing in any one is
    a conflict, never a silent reuse, ``--update`` included: a shared data type has no
    override.
    """

    _EXPORTED: dict[str, Any] = {
        "unit": "km/h",
        "conversion": {"factor": 0.1, "offset": 2.0},
        "range": {"min": 0.0, "max": 400.0},
    }

    @staticmethod
    def _source(tmp_path: Path, name: str, physics: dict[str, Any]) -> str:
        root = tmp_path / name
        for data_type in (
            {"name": "u8b", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
            {"name": "u8", "kind": "value_type", "base": "u8b"},
            {"name": "Spd_T", "kind": "application_type", **physics},
        ):
            _dump(model_element_path(root, data_type["name"], kind=data_type["kind"]), data_type)
        write_model_element(
            root, {"name": "PhysMaps", "maps": [{"application": "Spd_T", "implementation": "u8"}]}, layer="mapping_sets"
        )
        _dump(
            root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "application_type": "Spd_T"}],
            },
        )
        _dump(
            root / "swc" / "body" / "TargetSwc.yaml",
            {
                "name": "TargetSwc",
                "type": "application",
                "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
                "data_type_mappings": ["PhysMaps"],
                "handle_termination_and_restart": "no_support",
            },
        )
        return str(root)

    def test_identical_physics_is_reused_silently(self, tmp_path: Path) -> None:
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(self._source(tmp_path, "source", self._EXPORTED), "TargetSwc", str(arxml))
        target = self._source(tmp_path / "t", "target", self._EXPORTED)

        report = plan_component_import(str(arxml), target, update=True, dry_run=True).report

        assert report.reused_data_types == [
            "Spd_T",
            "u8b",
            "u8",
        ]  # the export's source-path order: Application, Base, Implementation
        assert report.created_data_types == []

    @pytest.mark.parametrize(
        ("field", "physics"),
        [
            pytest.param("unit", {**_EXPORTED, "unit": "m/s"}, id="unit"),
            pytest.param("conversion", {**_EXPORTED, "conversion": {"factor": 0.5, "offset": 2.0}}, id="factor"),
            pytest.param("conversion", {**_EXPORTED, "conversion": {"factor": 0.1, "offset": 3.0}}, id="offset"),
            pytest.param("range", {**_EXPORTED, "range": {"min": 0.0, "max": 500.0}}, id="range"),
        ],
    )
    def test_a_difference_in_any_physical_field_is_a_conflict(
        self, tmp_path: Path, field: str, physics: dict[str, Any]
    ) -> None:
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(self._source(tmp_path, "source", self._EXPORTED), "TargetSwc", str(arxml))
        target = self._source(tmp_path / "t", "target", physics)

        with pytest.raises(ComponentImportConflictError) as refused:
            plan_component_import(str(arxml), target, update=True, dry_run=True)

        assert "Spd_T" in str(refused.value), field


class TestAnEarlierReleasesApplicationViewImportsAsAnApplicationType:
    """An earlier release's ``{T}_AppView`` reads back as the ordinary application type it is.

    The view is an ``application_type``, the record field names it, and the set pairing it
    with the implementation type is written as the element it always was. The fixture is
    ``TwinBehindARecordView.arxml`` with the data-types marker added (without it the twin
    refusal fires first). Does not reach: an unmarked earlier-release file, which is
    still refused.
    """

    FIXTURE = (
        REPO_ROOT
        / "packages"
        / "tools"
        / "tests"
        / "fixtures"
        / "component_exchange"
        / "earlier_release"
        / "TwinBehindARecordView.arxml"
    )

    def _marked(self, tmp_path: Path) -> Path:
        anchor = "<SHORT-NAME>ExampleSWC_InternalBehavior</SHORT-NAME>"
        text = self.FIXTURE.read_text(encoding="utf-8")
        assert text.count(anchor) == 1
        marked = tmp_path / "MarkedRecordView.arxml"
        marked.write_text(
            text.replace(
                anchor,
                anchor + f'<ADMIN-DATA><SDGS><SDG GID="SWCRAFT"><SD GID="{DATA_TYPES.sd_gid}">'
                "any text</SD></SDG></SDGS></ADMIN-DATA>",
            ),
            encoding="utf-8",
            newline="\n",
        )
        return marked

    def test_the_view_is_written_as_an_application_type(self, tmp_path: Path) -> None:
        project = init_project(tmp_path / "proj")

        import_component(str(self._marked(tmp_path)), str(project / "model"))

        view = yaml.safe_load(
            (model_element_path(project / "model", "float32_AppView", kind="application_type")).read_text(
                encoding="utf-8"
            )
        )
        assert view == {"name": "float32_AppView", "kind": "application_type"}

    def test_the_record_field_names_the_view_and_the_set_is_written(self, tmp_path: Path) -> None:
        project = init_project(tmp_path / "proj")

        report = import_component(str(self._marked(tmp_path)), str(project / "model"))

        record = yaml.safe_load(
            (model_element_path(project / "model", "Pose_T", kind="record")).read_text(encoding="utf-8")
        )
        assert record["fields"] == [{"name": "x", "type": "float32_AppView"}]
        assert report.created_mapping_sets == ["DataTypeMappingSet"]
        written = yaml.safe_load(
            (model_element_path(project / "model", "DataTypeMappingSet", layer="mapping_sets")).read_text(
                encoding="utf-8"
            )
        )
        assert written["maps"] == [{"application": "float32_AppView", "implementation": "float32_impl"}]


# Three former defects on this path, each claimed end to end on a component
# export of `_physical_source`.


def _physical_export(tmp_path: Path, *, edit: tuple[str, str] | None = None) -> Path:
    """``TargetSwc`` exported from :func:`_physical_source`, optionally with one text edit."""
    arxml = tmp_path / "TargetSwc.arxml"
    export_component(_physical_source(tmp_path), "TargetSwc", str(arxml))
    if edit is not None:
        text = arxml.read_text(encoding="utf-8")
        old, new = edit
        assert old in text
        arxml.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return arxml


class TestTheDataTypesNamespaceIsOpenToAMappingSetOnThisPathToo:
    """A mapping set may take a data type's or a generated form's name on this path.

    A mapping set lies in ``DataTypes/MappingSets``, apart from the data types and their
    generated forms, so each case is accepted: the set is written under its own name and
    the project passes the gate and the writer.
    """

    @staticmethod
    def _assert_accepted_and_built(arxml: Path, model_root: Path, out: Path) -> Any:
        report = import_component(str(arxml), str(model_root))
        # The one finding a component import always leaves is the requirement
        # link, integration-owned content kept out of an export.
        result = check_cross_layer_consistency(str(model_root))
        assert [record.rule for record in result.records if "requirement" not in record.message] == []
        generate_arxml(str(model_root), str(out))
        return report

    def test_an_incoming_set_named_like_a_target_data_type_is_accepted(self, tmp_path: Path) -> None:
        arxml = _physical_export(tmp_path)
        project = init_project(tmp_path / "proj")
        write_model_element(
            project / "model",
            {"name": "PhysMapsBase", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        )
        write_model_element(project / "model", {"name": "PhysMaps", "kind": "value_type", "base": "PhysMapsBase"})

        report = self._assert_accepted_and_built(arxml, project / "model", tmp_path / "out.arxml")

        assert report.created_mapping_sets == ["PhysMaps"]
        assert (model_element_path(project / "model", "PhysMaps", layer="mapping_sets")).is_file()

    def test_an_incoming_set_named_like_a_generated_form_of_a_target_type_is_accepted(self, tmp_path: Path) -> None:
        arxml = _physical_export(tmp_path, edit=("PhysMaps", "Gear_Unit"))
        project = init_project(tmp_path / "proj")
        write_model_element(project / "model", {"name": "Gear", "kind": "application_type", "unit": "1"})

        report = self._assert_accepted_and_built(arxml, project / "model", tmp_path / "out.arxml")

        assert report.created_mapping_sets == ["Gear_Unit"]
        assert (model_element_path(project / "model", "Gear_Unit", layer="mapping_sets")).is_file()

    def test_an_incoming_type_named_like_a_target_set_is_accepted(self, tmp_path: Path) -> None:
        """The reverse direction: the set pre-dates the import, the type is new."""
        arxml = _physical_export(tmp_path)
        project = init_project(tmp_path / "proj")
        write_model_element(project / "model", {"name": "Spd_T", "maps": []}, layer="mapping_sets")

        report = self._assert_accepted_and_built(arxml, project / "model", tmp_path / "out.arxml")

        assert "Spd_T" in report.created_data_types
        assert (model_element_path(project / "model", "Spd_T", layer="mapping_sets")).is_file()

    def test_a_form_an_incoming_type_generates_named_like_a_target_set_is_accepted(self, tmp_path: Path) -> None:
        arxml = _physical_export(tmp_path)
        project = init_project(tmp_path / "proj")
        write_model_element(project / "model", {"name": "Spd_T_Range", "maps": []}, layer="mapping_sets")

        report = self._assert_accepted_and_built(arxml, project / "model", tmp_path / "out.arxml")

        assert "Spd_T" in report.created_data_types
        assert (model_element_path(project / "model", "Spd_T_Range", layer="mapping_sets")).is_file()

    def test_a_collision_wholly_inside_the_target_is_not_this_imports(self, tmp_path: Path) -> None:
        """At least one side must be new.

        An unrelated pre-existing collision does not make this component unimportable."""
        arxml = _physical_export(tmp_path)
        project = init_project(tmp_path / "proj")
        write_model_element(project / "model", {"name": "Old", "kind": "application_type"})
        write_model_element(project / "model", {"name": "Old", "maps": []}, layer="mapping_sets")

        report = import_component(str(arxml), str(project / "model"))

        assert report.created_mapping_sets == ["PhysMaps"]

    def test_the_accepted_import_leaves_a_project_the_gate_and_the_writer_accept(self, tmp_path: Path) -> None:
        arxml = _physical_export(tmp_path)
        project = init_project(tmp_path / "proj")

        import_component(str(arxml), str(project / "model"))

        # The one finding a component import always leaves is the requirement
        # link, integration-owned content kept out of an export.
        result = check_cross_layer_consistency(str(project / "model"))
        assert [record.rule for record in result.records if "requirement" not in record.message] == []
        generate_arxml(str(project / "model"), str(tmp_path / "out.arxml"))


class TestAnUncarriedApplicationCategoryDoesNotAbortTheImport:
    """``CATEGORY`` ``BOOLEAN`` is a property the model does not hold, not a lost element.

    It is a physics note, rendered by both importers, never a skip entry: its tag is not deferred, so a skip entry
    would refuse every such export with a message that says the type is imported.
    """

    NOTE = (
        "/DataTypes/Application/Spd_T: CATEGORY 'BOOLEAN' has no SWCraft kind of its own -- the type is imported and "
        "would be re-exported as CATEGORY 'VALUE'"
    )

    @staticmethod
    def _boolean_export(tmp_path: Path) -> Path:
        arxml = _physical_export(tmp_path)
        text = arxml.read_text(encoding="utf-8")
        start = text.index("<APPLICATION-PRIMITIVE-DATA-TYPE>")
        category = text.index("<CATEGORY>VALUE</CATEGORY>", start)
        assert text.index("<SHORT-NAME>Spd_T</SHORT-NAME>", start) < category
        text = text[:category] + "<CATEGORY>BOOLEAN</CATEGORY>" + text[category + len("<CATEGORY>VALUE</CATEGORY>") :]
        arxml.write_text(text, encoding="utf-8", newline="\n")
        return arxml

    def test_the_component_imports_and_the_category_is_a_note(self, tmp_path: Path) -> None:
        arxml = self._boolean_export(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        assert "Spd_T" in report.created_data_types
        assert report.unread_physics_lines == [self.NOTE]
        assert self.NOTE in report.render()
        assert [line for line in report.skip_lines if "CATEGORY" in line] == []

    def test_a_dry_run_plans_it_the_same_way(self, tmp_path: Path) -> None:
        arxml = self._boolean_export(tmp_path)
        project = init_project(tmp_path / "proj")

        plan = plan_component_import(str(arxml), str(project / "model"), dry_run=True)

        assert plan.report.unread_physics_lines == [self.NOTE]


class TestAnOlderSwcraftEnumExportStillImports:
    """A component export from SWCraft 0.13 or earlier: its enum's ``CATEGORY`` is ``TEXTTABLE``.

    The writer writes ``VALUE`` (``_emit_enum_type`` says why), so the importer adds one physics note per
    enum it creates, never a skip entry. The notes are narrowed to the types this plan writes
    (``created_type_source_paths`` in ``component_exchange/import_.py::_report_lines``), so a project already
    holding the enum reuses it silently. The older file is this release's export with that one category line
    substituted.
    """

    NOTE = (
        "/DataTypes/Application/Gear_T: CATEGORY 'TEXTTABLE' is imported as an enum -- the type would be "
        "re-exported as CATEGORY 'VALUE'"
    )

    @staticmethod
    def _older_export(tmp_path: Path) -> Path:
        _, arxml = _export_target(tmp_path)
        older, substituted = rewrite_enum_type_category(arxml.read_bytes(), "VALUE", "TEXTTABLE")
        assert substituted == 1
        target = tmp_path / "TargetSwc-0.13.arxml"
        target.write_bytes(older)
        return target

    def test_this_releases_export_carries_no_category_note(self, tmp_path: Path) -> None:
        """The negative control: the enum written ``VALUE`` imports with no note."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert "Gear_T" in report.created_data_types
        assert [line for line in report.unread_physics_lines if "CATEGORY" in line] == []

    def test_a_fresh_import_notes_the_enum_once_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        arxml = self._older_export(tmp_path)
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"), dry_run=True)
        assert report.swc_action == "created"
        assert "Gear_T" in report.created_data_types
        assert report.unread_physics_lines == [self.NOTE]
        assert [line for line in report.skip_lines if "CATEGORY" in line] == []

        argv = ["swcraft", "import", "component", "--source", str(arxml), "--project", str(project / "swcraft.yaml")]
        monkeypatch.setattr("sys.argv", argv)
        main()
        out = capsys.readouterr().out
        assert out.count(self.NOTE) == 1, out
        written = yaml.safe_load(
            (model_element_path(project / "model", "Gear_T", kind="enum")).read_text(encoding="utf-8")
        )
        assert written["kind"] == "enum"
        assert [literal["name"] for literal in written["literals"]] == ["PARK", "DRIVE"]

    def test_an_update_over_a_project_holding_the_enum_reuses_it_with_no_note(self, tmp_path: Path) -> None:
        _, current = _export_target(tmp_path / "current")
        arxml = self._older_export(tmp_path)
        project = init_project(tmp_path / "proj")
        import_component(str(current), str(project / "model"))
        before = files_of(project / "model")

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.swc_action == "unchanged"
        assert "Gear_T" in report.reused_data_types
        assert report.created_data_types == []
        assert report.unread_physics_lines == []
        assert report.written_paths == []
        assert files_of(project / "model") == before


class TestAHalfMapRefusesTheComponentImport:
    """A ``DATA-TYPE-MAP`` with one side absent refuses the component import.

    The reader never drops such a map silently, which would leave a project ``check-model`` and ``generate arxml``
    refuse. It records it under the non-deferred ``DATA-TYPE-MAP``
    tag, so this path refuses, as it refuses a map the mapper cannot carry. SWCraft's own
    exports never write a half map, so the refusal reaches a hand-edited or foreign file
    only; ``test_arxml_import_mapping_sets.py`` claims the ``import model`` half, where it
    is a report line.
    """

    LINE = (
        "DATA-TYPE-MAP /DataTypes/MappingSets/PhysMaps/DATA-TYPE-MAPS: a DATA-TYPE-MAP [0] with no "
        "IMPLEMENTATION-DATA-TYPE-REF pairs nothing -- the map is dropped"
    )

    @staticmethod
    def _half_map_export(tmp_path: Path) -> Path:
        arxml = _physical_export(tmp_path)
        text = arxml.read_text(encoding="utf-8")
        start = text.index("<DATA-TYPE-MAP>")
        ref_start = text.index("<IMPLEMENTATION-DATA-TYPE-REF", start)
        ref_end = text.index("</IMPLEMENTATION-DATA-TYPE-REF>", ref_start) + len("</IMPLEMENTATION-DATA-TYPE-REF>")
        assert ref_end < text.index("</DATA-TYPE-MAP>", start)
        arxml.write_text(text[:ref_start] + text[ref_end:], encoding="utf-8", newline="\n")
        return arxml

    def test_the_import_is_refused_and_nothing_is_written(self, tmp_path: Path) -> None:
        arxml = self._half_map_export(tmp_path)
        project = init_project(tmp_path / "proj")
        before = files_of(project / "model")

        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(arxml), str(project / "model"))

        assert self.LINE in caught.value.skip_lines
        assert files_of(project / "model") == before

    def test_a_dry_run_is_refused_the_same_way(self, tmp_path: Path) -> None:
        arxml = self._half_map_export(tmp_path)
        project = init_project(tmp_path / "proj")

        with pytest.raises(LossyComponentImportError) as caught:
            plan_component_import(str(arxml), str(project / "model"), dry_run=True)

        assert self.LINE in caught.value.skip_lines

    def test_the_whole_export_still_imports(self, tmp_path: Path) -> None:
        """The negative control: the same export with both sides imports and carries the pair."""
        project = init_project(tmp_path / "proj")

        report = import_component(str(_physical_export(tmp_path)), str(project / "model"))

        assert report.created_mapping_sets == ["PhysMaps"]


class TestAComponentExportRefusesAnUnmappedPhysicalType:
    """A component export refuses a physical type its sets do not map.

    The closure keeps every map whose application side the component's interfaces reach,
    so narrowing removes nothing this rule asks for; the export never writes a ``[constr_1004]``-violating file with
    ``STATUS ok`` on a project the gate refuses, at the boundary where the file reaches an RTE.
    """

    def test_a_component_naming_no_set_is_refused_and_nothing_is_written(self, tmp_path: Path) -> None:
        source = _physical_source(tmp_path, named_sets=[])
        output = tmp_path / "TargetSwc.arxml"

        with pytest.raises(ClosureValidationError, match="'TargetSwc' speaks type 'Spd_T'"):
            export_component(source, "TargetSwc", str(output))

        assert not output.exists()
        assert not Path(f"{output}.tmp").exists()

    def test_the_all_components_export_reports_it_failed(self, tmp_path: Path) -> None:
        source = _physical_source(tmp_path, named_sets=[])

        reports = export_all_components(source, str(tmp_path / "out"))

        assert [(report.swc, report.status) for report in reports] == [("TargetSwc", "failed")]
        assert "'TargetSwc' speaks type 'Spd_T'" in (reports[0].error or "")

    def test_the_narrowed_set_still_carries_the_pair_the_ports_need(self, tmp_path: Path) -> None:
        """The narrowed set still carries the pair the port needs, so the export is accepted.

        ``PhysMaps`` also maps ``Other_T``, which the closure does not reach and drops.
        """
        output = tmp_path / "TargetSwc.arxml"

        report = export_component(_physical_source(tmp_path), "TargetSwc", str(output))

        assert report.status == "ok"
        assert "Other_T" not in output.read_text(encoding="utf-8")


# External interface and type stubs in the TARGET project


class TestATargetProjectHoldingExternalStubs:
    """``import component`` over a project that holds external stubs.

    A component file carries no stub; a stub reaches the target side only. An incoming
    definition named like a stub cannot be compared with it, so it is a conflict naming
    the stub; a stub emits no ``SHORT-NAME``, so the reserved-name guard does not count
    it, or it would be stricter than the writer.
    """

    @staticmethod
    def _stub(model_root: Path, layer: str, name: str) -> None:
        if layer == "interfaces":
            interface_stub(model_root, name, f"/Foreign/PortInterfaces/{name}", "SENDER-RECEIVER-INTERFACE")
        else:
            type_stub(model_root, name, f"/Foreign/ImplementationDataTypes/{name}", "IMPLEMENTATION-DATA-TYPE")

    def test_an_import_beside_unrelated_stubs_plans_and_writes_what_it_does_without_them(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        plain = init_project(tmp_path / "plain")
        stubbed = init_project(tmp_path / "stubbed")
        self._stub(stubbed / "model", "interfaces", "IForeignSR")
        self._stub(stubbed / "model", "implementation_types", "ForeignT")
        stubs_before = {
            relative: data for relative, data in files_of(stubbed / "model").items() if "Foreign" in relative
        }
        assert len(stubs_before) == 2

        plain_report = import_component(str(arxml), str(plain / "model"))
        stubbed_report = import_component(str(arxml), str(stubbed / "model"))

        for attribute in (
            "swc_action",
            "created_interfaces",
            "created_data_types",
            "reused_interfaces",
            "reused_data_types",
        ):
            assert getattr(stubbed_report, attribute) == getattr(plain_report, attribute), attribute
        plain_tree, stubbed_tree = files_of(plain / "model"), files_of(stubbed / "model")
        assert {k: v for k, v in stubbed_tree.items() if k not in stubs_before} == plain_tree
        assert {k: stubbed_tree[k] for k in stubs_before} == stubs_before

    @pytest.mark.parametrize(("layer", "name"), [("interfaces", "ISpeed"), ("implementation_types", "Speed_T")])
    def test_an_incoming_definition_named_like_a_target_stub_is_a_conflict_naming_the_stub(
        self, tmp_path: Path, layer: str, name: str
    ) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        self._stub(model_root, layer, name)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(str(arxml), str(model_root))

        lines = [entry for entry in exc_info.value.conflicts if f"'{name}'" in entry]
        assert len(lines) == 1, exc_info.value.conflicts
        assert "external stub" in lines[0]
        assert "representable difference" not in lines[0]
        assert files_of(model_root) == before

    @pytest.mark.parametrize(("layer", "name"), [("interfaces", "ISpeed"), ("implementation_types", "Speed_T")])
    def test_an_incoming_definition_whose_stub_import_model_named_ext0_is_still_a_conflict(
        self, tmp_path: Path, layer: str, name: str
    ) -> None:
        """A target stub named ``<leaf>_ext<X>`` is found by its exact ``external_ref``, the incoming path.

        The incoming definition stays a conflict naming both.
        """
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        if layer == "interfaces":
            path = f"/PortInterfaces/{name}"
            interface_stub(model_root, f"{name}_ext0", path, "SENDER-RECEIVER-INTERFACE")
        else:
            path = f"/DataTypes/Implementation/{name}"
            type_stub(model_root, f"{name}_ext0", path, "IMPLEMENTATION-DATA-TYPE")
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(str(arxml), str(model_root))

        lines = [entry for entry in exc_info.value.conflicts if f"'{name}'" in entry]
        assert len(lines) == 1, exc_info.value.conflicts
        assert f"is an external stub in the target project as '{name}_ext0'" in lines[0]
        assert path in lines[0]
        assert files_of(model_root) == before

    @pytest.mark.parametrize(("layer", "name"), [("interfaces", "ISpeed"), ("implementation_types", "Speed_T")])
    def test_a_target_stub_of_the_same_leaf_but_another_path_is_another_element_and_the_incoming_one_is_created(
        self, tmp_path: Path, layer: str, name: str
    ) -> None:
        """A stub of the same leaf but another path is another element: the import creates ``ISpeed`` beside it."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        if layer == "interfaces":
            interface_stub(model_root, f"{name}_ext0", f"/Foreign/PortInterfaces/{name}", "SENDER-RECEIVER-INTERFACE")
        else:
            type_stub(
                model_root, f"{name}_ext0", f"/Foreign/ImplementationDataTypes/{name}", "IMPLEMENTATION-DATA-TYPE"
            )

        report = import_component(str(arxml), str(model_root))

        created = report.created_interfaces if layer == "interfaces" else report.created_data_types
        assert name in created
        directory = "interfaces" if layer == "interfaces" else "data_types/implementation"
        assert (model_root / directory / f"{name}_ext0.yaml").exists()

    @staticmethod
    def _component_stub(model_root: Path, name: str, external_ref: str = "/Foreign/ComponentTypes/TargetSwc") -> None:
        _dump(
            model_root / "swc" / "external" / f"{name}.yaml",
            {
                "name": name,
                "type": "application",
                "external": True,
                "external_ref": external_ref,
                "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                "ports": [],
                "runnables": [],
            },
        )

    @pytest.mark.parametrize("update", [False, True], ids=["default", "update=True"])
    def test_an_incoming_component_whose_stub_import_model_named_ext0_is_a_conflict_update_does_not_resolve(
        self, tmp_path: Path, update: bool
    ) -> None:
        """A component stub ``TargetSwc_ext0`` for the incoming path is found by ``external_ref``: a conflict.

        ``--update`` does not resolve it: the replacement would take the stub's file while
        the prototypes still name the stub.
        """
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        self._component_stub(model_root, "TargetSwc_ext0", "/ComponentTypes/TargetSwc")
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(str(arxml), str(model_root), update=update)

        lines = [entry for entry in exc_info.value.conflicts if "'TargetSwc'" in entry]
        assert len(lines) == 1, exc_info.value.conflicts
        assert "is an external stub in the target project as 'TargetSwc_ext0'" in lines[0]
        assert "--update does not replace a stub of another name" in lines[0]
        assert "/ComponentTypes/TargetSwc" in lines[0]
        assert files_of(model_root) == before

    def test_a_component_stub_of_the_same_leaf_but_another_path_is_another_element(self, tmp_path: Path) -> None:
        """A component stub of the same leaf but another path is another element, created beside it."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        self._component_stub(model_root, "TargetSwc_ext0", "/Foreign/ComponentTypes/TargetSwc")

        import_component(str(arxml), str(model_root))

        assert (model_root / "swc" / "external" / "TargetSwc_ext0.yaml").exists()
        assert list(model_root.glob("swc/*/TargetSwc.yaml"))

    def test_a_component_stub_of_the_incoming_name_keeps_the_diverges_conflict_and_update_replaces_it(
        self, tmp_path: Path
    ) -> None:
        """The contrast: a stub named ``TargetSwc`` (by hand) is found by name, as it always was.

        It takes the *diverges* conflict, and ``--update`` replaces the stub with the component."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        self._component_stub(model_root, "TargetSwc")

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(str(arxml), str(model_root))
        assert any("SWC 'TargetSwc' already exists" in entry for entry in exc_info.value.conflicts)

        report = import_component(str(arxml), str(model_root), update=True)

        assert report.swc_action == "updated"
        replaced = {swc.name: swc for swc in read_swc_model(model_root)}["TargetSwc"]
        assert not replaced.external

    @pytest.mark.parametrize(
        ("layer", "name"),
        [("implementation_types", "Gear_T_CompuMethod"), ("interfaces", "ISpeed_speed_Init")],
        ids=["compu-method-form", "init-constant-form"],
    )
    def test_a_stub_named_like_a_generated_form_is_no_reserved_name_violation(
        self, tmp_path: Path, layer: str, name: str
    ) -> None:
        """The writer emits nothing for a stub, so its name occupies no ``SHORT-NAME``.

        The merged project generates, and the import accepts it the same way."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        self._stub(model_root, layer, name)

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        generate_arxml(str(model_root), str(tmp_path / "merged.arxml"))
        assert (tmp_path / "merged.arxml").is_file()


# The stated names are compared as they stand


def _access_model_stating(tmp_path: Path, **names: str) -> Path:
    """``access_points`` with ``AccessSwc`` stating *names*: ``behavior_name``, ``event``, ``read``, ``call``.

    ``event`` names ``AccessMain``'s timing event, ``read`` its first read and
    ``call`` its call; every name is invented.
    """
    root = tmp_path / "source_model"
    shutil.copytree(ACCESS_POINTS_MODEL_ROOT, root)
    path = root / "swc" / "access" / "AccessSwc.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    main = document["runnables"][0]
    for key, name in names.items():
        if key == "behavior_name":
            document["behavior_name"] = name
        else:
            {"event": main["events"], "read": main["reads"], "call": main["calls"]}[key][0]["name"] = name
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return root


def _access_swc_names(model_root: Path) -> tuple[str | None, str | None, str | None, str | None]:
    """``AccessSwc``'s stated behaviour name and ``AccessMain``'s stated event, first read and call names."""
    (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == "AccessSwc"]
    main = swc.runnables[0]
    return swc.behavior_name, main.events[0].name, main.reads[0].name, main.calls[0].name


class TestTheStatedNamesAreComparedAsTheyStand:
    """The stated names cross the exchange and the comparison takes them as they stand.

    The export writes a stated name as the element's ``SHORT-NAME``; the import states it
    again where it differs from the derived name (``settle_stated_names``), so equal names
    are no difference, a target stating none against a file stating one is a divergence,
    and ``--update`` carries the file's names. Limit: a stated name equal to the derived
    one writes the same bytes but reads back as none, so importing the target's own
    unchanged export is a conflict.
    """

    NAMES = {
        "behavior_name": "AccessBehaviour",
        "event": "Tick10ms",
        "read": "ReadSpeed",
        "call": "CallApply",
    }

    def test_equal_names_are_no_difference_and_a_target_stating_none_is_a_conflict(self, tmp_path: Path) -> None:
        project = init_project(tmp_path / "proj")
        plain = tmp_path / "plain.arxml"
        export_component(str(_access_model_stating(tmp_path / "a")), "AccessSwc", str(plain))
        stated = tmp_path / "stated.arxml"
        export_component(str(_access_model_stating(tmp_path / "b", **self.NAMES)), "AccessSwc", str(stated))
        import_component(str(plain), str(project / "model"))

        assert import_component(str(plain), str(project / "model")).swc_action == "unchanged"
        with pytest.raises(ComponentImportConflictError):
            import_component(str(stated), str(project / "model"))
        assert _access_swc_names(project / "model") == (None, None, None, None)

        assert import_component(str(stated), str(project / "model"), update=True).swc_action == "updated"
        assert _access_swc_names(project / "model") == tuple(self.NAMES.values())
        assert import_component(str(stated), str(project / "model")).swc_action == "unchanged"

    def test_a_target_name_equal_to_the_derived_one_differs_from_its_own_export(self, tmp_path: Path) -> None:
        target = _access_model_stating(tmp_path, behavior_name="AccessSwc_InternalBehavior", event="TMT_AccessMain")
        arxml = tmp_path / "AccessSwc.arxml"
        export_component(str(target), "AccessSwc", str(arxml))
        plain = tmp_path / "plain.arxml"
        export_component(str(_access_model_stating(tmp_path / "plain")), "AccessSwc", str(plain))
        assert arxml.read_bytes() == plain.read_bytes(), "a stated name equal to the derived one writes the same bytes"

        with pytest.raises(ComponentImportConflictError):
            import_component(str(arxml), str(target))

        assert _access_swc_names(target)[:2] == ("AccessSwc_InternalBehavior", "TMT_AccessMain")


class TestAnInfinitePhysicalLimitIsRepresentedInTheComponentReport:
    """An own-side infinite ``PHYS-CONSTRS`` limit is carried as the absent bound and never aborts.

    Its line renders under *Represented in another form* below the type's lines; it is no
    skip entry. Two shapes: ``Spd_T``'s inline range, and a standalone ``data_constr``
    element keyed by the constraint's path, which a filter keeping created type paths
    alone would drop.
    """

    REPRESENTED = "infinite physical limit represented as an absent bound"
    HEADING = "Represented in another form (the value is carried, spelled as the model spells it):"

    @staticmethod
    def _standalone_export(tmp_path: Path, upper: str) -> Path:
        """``TargetSwc``'s export, ``Spd_T`` naming a ``data_constr`` element, its upper limit respelled *upper*."""
        root = Path(_physical_source(tmp_path))
        speed = model_element_path(root, "Spd_T", kind="application_type")
        document = yaml.safe_load(speed.read_text(encoding="utf-8"))
        del document["range"]
        document["data_constr_ref"] = "SpdLimits"
        _dump(speed, document)
        write_model_element(root, {"name": "SpdLimits", "min": 0, "max": 400}, layer="data_constraints")
        arxml = tmp_path / "TargetSwc.arxml"
        export_component(str(root), "TargetSwc", str(arxml))
        text = arxml.read_text(encoding="utf-8")
        closed = '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">400</UPPER-LIMIT>'
        assert text.count(closed) == 1
        arxml.write_text(text.replace(closed, upper), encoding="utf-8", newline="\n")
        return arxml

    @pytest.mark.parametrize(
        "upper",
        [
            pytest.param("<UPPER-LIMIT>INF</UPPER-LIMIT>", id="inf"),
            pytest.param('<UPPER-LIMIT INTERVAL-TYPE="INFINITE">400</UPPER-LIMIT>', id="infinite-with-a-number"),
        ],
    )
    def test_an_inline_range(self, tmp_path: Path, upper: str) -> None:
        arxml = _physical_export(tmp_path, edit=('<UPPER-LIMIT INTERVAL-TYPE="CLOSED">400</UPPER-LIMIT>', upper))
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.swc_action == "created"
        assert "Spd_T" in report.created_data_types
        speed = yaml.safe_load(
            model_element_path(project / "model", "Spd_T", kind="application_type").read_text(encoding="utf-8")
        )
        assert speed["range"] == {"min": 0}
        assert report.represented_lines == [
            "/DataTypes/Application/Spd_T: DATA-CONSTR /DataTypes/Application/Spd_T_Range PHYS-CONSTRS "
            f"UPPER-LIMIT: {self.REPRESENTED}"
        ]
        assert report.unread_physics_lines == []
        assert [line for line in report.skip_lines if "LIMIT" in line] == []
        rendered = report.render()
        assert rendered.index("Spd_T") < rendered.index(self.HEADING)
        assert f"  - {report.represented_lines[0]}" in rendered.split(self.HEADING, 1)[1]

    def test_a_standalone_constraint(self, tmp_path: Path) -> None:
        arxml = self._standalone_export(tmp_path, "<UPPER-LIMIT>+INF</UPPER-LIMIT>")
        project = init_project(tmp_path / "proj")

        report = import_component(str(arxml), str(project / "model"))

        assert report.created_elements.get("data_constraints") == ["SpdLimits"]
        constraint = yaml.safe_load(
            model_element_path(project / "model", "SpdLimits", layer="data_constraints").read_text(encoding="utf-8")
        )
        assert (constraint.get("min"), "max" in constraint) == (0, False)
        assert report.represented_lines == [
            f"/DataTypes/Constraints/SpdLimits: PHYS-CONSTRS UPPER-LIMIT: {self.REPRESENTED}"
        ]
        assert self.HEADING in report.render()

    def test_a_reused_constraint_carries_no_line(self, tmp_path: Path) -> None:
        """Narrowed to what the plan creates: a reused constraint carries no line.

        A constraint the target already holds equal is reused,
        and a line about it would be a line about the target's own file."""
        arxml = self._standalone_export(tmp_path / "first", "<UPPER-LIMIT>+INF</UPPER-LIMIT>")
        project = init_project(tmp_path / "proj")
        import_component(str(arxml), str(project / "model"))

        report = import_component(str(arxml), str(project / "model"), update=True)

        assert report.represented_lines == []
