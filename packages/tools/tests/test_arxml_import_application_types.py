"""The importer's application primitives, its enums and the narrowed twin, each on a synthetic fixture.

Each shape is a minimal export under ``fixtures/arxml_import_shapes/``, run through ``swcraft import model``; the claims
read back the YAML the run wrote and the report it rendered.

What this module does **not** reach:

* the mapping sets, ``swc.data_type_mappings`` and ``check-model`` on the written project
  (``test_arxml_import_mapping_sets.py``);
* real tool exports: ``linear_conversion_not_read.arxml`` is the only file here for the two rejected LINEAR shapes;
* what a client of the model does with an application type or enum a port speaks; that it needs a mapping is
  ``rule:data-type.application-type-unmapped``'s;
* groups, selections and unresolved references beyond what one shape needs (``test_arxml_import_merges.py``,
  ``…_package_boundaries.py``, ``…_unresolved.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from arxml_import_shapes_support import shape_path
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.report import ImportReport

#: The fixtures this module claims its shapes on.
_FIXTURES = (
    "bare_application_twin.arxml",
    "application_twin_with_physics.arxml",
    "twinned_texttable_enum.arxml",
    "linear_conversion_with_offset.arxml",
    "linear_conversion_not_read.arxml",
    "non_linear_compu_method.arxml",
    "implementation_typed_data_prototype.arxml",
    "twin_name_already_declared.arxml",
    "map_naming_a_merged_twin_alias.arxml",
)


def _init_project(tmp_path: Path, name: str = "proj") -> Path:
    """Scaffold a fresh project through the real ``swcraft init`` CLI path."""
    from swcraft_tools.cli import main

    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _import(
    tmp_path: Path,
    fixture: str,
    *,
    packages: list[str] | None = None,
    dry_run: bool = False,
    project_name: str = "proj",
) -> tuple[ImportReport, Path]:
    """Run ``import model`` on one shape fixture; return the report and the model root."""
    project = _init_project(tmp_path, project_name)
    report = run_import(
        str(shape_path(fixture)),
        str(project / "swcraft.yaml"),
        packages=packages,
        dry_run=dry_run,
    )
    return report, project / "model"


#: Each data description layer by the ``data_types/`` directory it is written to.
_LAYER_DIRECTORIES = {
    "base": "base_types",
    "application": "application_types",
    "implementation": "implementation_types",
}


def _data_types(model_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Every written data type element of the three layers, keyed ``(layer, name)``.

    The key carries the layer because a kept same-name pair is one name in two layers.
    """
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for directory, layer in _LAYER_DIRECTORIES.items():
        for path in sorted(model_root.glob(f"data_types/{directory}/*.yaml"), key=lambda path: path.as_posix()):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert (layer, document["name"]) not in by_key, f"{document['name']} written twice in {layer}"
            by_key[(layer, document["name"])] = document
    return by_key


def _layer(model_root: Path, layer: str) -> dict[str, dict[str, Any]]:
    """One data description layer by name."""
    return {name: document for (key, name), document in _data_types(model_root).items() if key == layer}


def _application(model_root: Path) -> dict[str, dict[str, Any]]:
    return _layer(model_root, "application_types")


def _implementation(model_root: Path) -> dict[str, dict[str, Any]]:
    return _layer(model_root, "implementation_types")


def _elements(model_root: Path, directory: str) -> dict[str, dict[str, Any]]:
    """The elements of one of the three layers of the forms, by name."""
    documents = (
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(model_root.glob(f"data_types/{directory}/*.yaml"), key=lambda path: path.as_posix())
    )
    return {document["name"]: document for document in documents}


def _stated(model_root: Path, document: dict[str, Any]) -> dict[str, Any]:
    """What an application type or an enumeration states, by either form.

    Unit, conversion, range and literals are read through the referenced element, as ``validator.stated_*`` reads them.
    """
    stated = dict(document)
    if "unit_ref" in document:
        unit = _elements(model_root, "units")[document["unit_ref"]]
        stated["unit"] = unit.get("display_name") or unit["name"]
    if "compu_method_ref" in document:
        method = _elements(model_root, "compu_methods")[document["compu_method_ref"]]
        if method["kind"] == "linear":
            stated["conversion"] = {"factor": method.get("factor", 1.0), "offset": method.get("offset", 0.0)}
        else:
            stated["literals"] = method["entries"]
    if "data_constr_ref" in document:
        constraint = _elements(model_root, "constraints")[document["data_constr_ref"]]
        stated["range"] = {key: constraint[key] for key in ("min", "max") if key in constraint}
    return stated


def _names(model_root: Path) -> set[str]:
    """Every name any data description layer declares."""
    return {name for _, name in _data_types(model_root)}


def _interface(model_root: Path, name: str) -> dict[str, Any]:
    return yaml.safe_load((model_root / "interfaces" / f"{name}.yaml").read_text(encoding="utf-8"))


class TestABareSameNamePairIsCollapsed:
    """A bare same-name pair (no unit, conversion or range) is collapsed to one element, ports re-pointed onto it."""

    FIXTURE = "bare_application_twin.arxml"

    def test_the_whole_file_writes_one_element_for_the_pair(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        types = _data_types(model_root)

        assert sorted(types) == [("base_types", "Word"), ("implementation_types", "Level")]
        assert types[("implementation_types", "Level")]["kind"] == "value_type"
        assert types[("implementation_types", "Level")]["base"] == "Word"

    def test_the_interface_element_is_typed_by_the_surviving_element(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _interface(model_root, "ILevel")["data_elements"][0]["implementation_type"] == "Level"

    def test_the_report_names_the_collapse_as_a_reduction(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE, dry_run=True)

        rendered = report.render()

        assert "Type-name reductions (app/impl twin collapse):" in rendered
        assert "/Acme/AppTypes/Level + /Acme/ImplTypes/Level -> Level (app/impl twin collapse)" in rendered

    def test_the_application_type_has_its_own_line(self, tmp_path: Path) -> None:
        """The reduction line at the application type's own key names the survivor; no element is written there."""
        report, _ = _import(tmp_path, self.FIXTURE, dry_run=True)

        (entry,) = [e for e in report.skip_entries if e.path == "/Acme/AppTypes/Level"]
        assert (entry.tag, entry.effect) == ("APPLICATION-PRIMITIVE-DATA-TYPE", "report")
        assert "'Level'" in entry.reason
        assert "implementation twin" in entry.reason

    def test_a_packages_selection_keeps_the_collapse(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert sorted(_data_types(model_root)) == [("base_types", "Word"), ("implementation_types", "Level")]

    def test_a_dry_run_writes_nothing_and_reports_the_same_count(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert report.imported_data_types == 2
        assert _data_types(model_root) == {}


class TestASameNamePairWithPhysicsKeepsBoth:
    """A same-name pair with physics is kept as two elements, one per layer, under the declared name."""

    FIXTURE = "application_twin_with_physics.arxml"

    def test_both_elements_are_written(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        types = _data_types(model_root)

        assert sorted(types) == [
            ("application_types", "Speed"),
            ("base_types", "Word"),
            ("implementation_types", "Speed"),
        ]
        assert types[("application_types", "Speed")]["kind"] == "application_type"
        assert types[("implementation_types", "Speed")]["kind"] == "value_type"
        assert types[("implementation_types", "Speed")]["base"] == "Word"

    def test_the_application_type_carries_its_physics(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        speed = _application(model_root)["Speed"]

        assert _stated(model_root, speed)["unit"] == "km/h"
        assert _stated(model_root, speed)["conversion"] == {"factor": 0.1, "offset": 0.0}
        assert _stated(model_root, speed)["range"] == {"min": 0.0, "max": 400.0}

    def test_the_interface_element_is_typed_by_the_application_type(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _interface(model_root, "ISpeed")["data_elements"][0]["application_type"] == "Speed"

    def test_the_report_names_no_rename_and_no_reduction(self, tmp_path: Path) -> None:
        """Nothing is renamed: the pair keeps one name in two layers."""
        report, _ = _import(tmp_path, self.FIXTURE, dry_run=True)

        rendered = report.render()

        assert "Type-name reductions (app/impl twin collapse):" not in rendered
        assert report.rename_lines == []
        assert "/Acme/ImplTypes/Speed -> " not in rendered

    def test_a_packages_selection_keeps_the_application_type_alone(self, tmp_path: Path) -> None:
        """The closure walks the interface onto the application type and, through the imported map, its store.

        An application type has no ``base``, so the map is the only relation and the closure follows it one way.
        """
        _, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert sorted(_data_types(model_root)) == [
            ("application_types", "Speed"),
            ("base_types", "Word"),
            ("implementation_types", "Speed"),
        ]

    def test_the_unit_and_the_range_elements_are_not_written_as_elements(self, tmp_path: Path) -> None:
        """A ``UNIT`` and a ``DATA-CONSTR`` are not written as data types, and are not reported as unrepresented."""
        report, model_root = _import(tmp_path, self.FIXTURE)

        assert "SpeedUnit" not in _names(model_root)
        assert "SpeedRange" not in _names(model_root)
        assert "unrecognized element tag inside ELEMENTS" not in report.render()


class TestThePairKeepsItsNameWhenTheMapNamesAMergedGroupsAlias:
    """A pair whose ``DATA-TYPE-MAP`` names the second declaration of a merged implementation type keeps its name.

    Guards a silent defect: the twin table was keyed on the path the map spelled while ``resolve_impl_type`` asks with
    the group's primary, so the application type was pushed onto an ``_app`` suffix.
    """

    FIXTURE = "map_naming_a_merged_twin_alias.arxml"

    def test_the_application_type_keeps_the_declared_name(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        types = _data_types(model_root)

        assert sorted(types) == [
            ("application_types", "Speed"),
            ("base_types", "Word"),
            ("implementation_types", "Speed"),
        ]
        assert types[("application_types", "Speed")]["kind"] == "application_type"
        assert _stated(model_root, types[("application_types", "Speed")])["unit"] == "km/h"
        assert types[("implementation_types", "Speed")]["kind"] == "value_type"
        assert types[("implementation_types", "Speed")]["base"] == "Word"

    def test_the_group_is_reported_merged_and_nothing_renamed(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE, dry_run=True)

        rendered = report.render()

        assert "Type-name reductions (app/impl twin collapse):" not in rendered
        (group,) = report.merged_groups
        assert (group.key.layer, group.key.name) == ("implementation_types", "Speed")
        assert group.paths == ("/Acme/ImplTypes/Speed", "/Acme/ImplTypesCopy/Speed")
        assert report.rename_lines == []
        assert "/Acme/ImplTypes/Speed -> " not in rendered
        assert "/Acme/ImplTypesCopy/Speed -> " not in rendered

    def test_the_interface_element_is_typed_by_the_application_type(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _interface(model_root, "ISpeed")["data_elements"][0]["application_type"] == "Speed"

    def test_a_packages_selection_answers_the_same(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert sorted(_data_types(model_root)) == [
            ("application_types", "Speed"),
            ("base_types", "Word"),
            ("implementation_types", "Speed"),
        ]

    def test_a_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert _data_types(model_root) == {}


class TestATwinnedTextTableEnumKeepsItsLiterals:
    """A TEXTTABLE enum twinned with an implementation type of its own name keeps its literals.

    The TEXTTABLE check runs before the twin collapse, so the enum is never collapsed away.
    """

    FIXTURE = "twinned_texttable_enum.arxml"

    def test_the_enum_is_written_with_its_literals(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        enum = _application(model_root)["DriveMode"]

        assert enum["kind"] == "enum"
        assert [literal["name"] for literal in _stated(model_root, enum)["literals"]] == ["OFF", "ECO", "SPORT"]
        assert [literal["value"] for literal in _stated(model_root, enum)["literals"]] == [0, 1, 2]

    def test_the_implementation_twin_keeps_the_declared_name_in_its_own_layer(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE)

        types = _data_types(model_root)

        assert sorted(types) == [
            ("application_types", "DriveMode"),
            ("base_types", "Byte"),
            ("implementation_types", "DriveMode"),
        ]
        assert types[("implementation_types", "DriveMode")]["kind"] == "value_type"
        assert report.rename_lines == []

    def test_the_interface_reference_names_the_enum_and_not_the_twin(self, tmp_path: Path) -> None:
        """The data element is typed by the enum, and the implementation twin is written beside it."""
        report, model_root = _import(tmp_path, self.FIXTURE)

        assert _interface(model_root, "IDriveMode")["data_elements"][0]["application_type"] == "DriveMode"
        assert "Interface-facing enum-type reductions" not in report.render()

    def test_the_compu_method_is_not_reported_as_unconsumed(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert "TEXTTABLE compu-method not used by any imported enum type" not in report.render()

    def test_a_table_it_cannot_read_leaves_a_line_at_the_type(self, tmp_path: Path) -> None:
        """An unreadable table imports the type without literals and leaves a line at its ``COMPU-METHOD-REF``.

        The implementation type is renamed so no twin collapses the pair.
        """
        text = shape_path(self.FIXTURE).read_text(encoding="utf-8")
        text = text.replace("<VT>ECO</VT>", "<VT>   </VT>")
        # The first DriveMode declared is the implementation type (its package comes first).
        text = text.replace("<SHORT-NAME>DriveMode</SHORT-NAME>", "<SHORT-NAME>DriveModeImpl</SHORT-NAME>", 1)
        text = text.replace("/Acme/ImplTypes/DriveMode<", "/Acme/ImplTypes/DriveModeImpl<")
        source = tmp_path / "unreadable_table.arxml"
        source.write_text(text, encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)
        report = run_import(str(source), str(project / "swcraft.yaml"), dry_run=True)

        (entry,) = [e for e in report.skip_entries if e.path == "/Acme/AppTypes/DriveMode/SW-DATA-DEF-PROPS"]
        assert (entry.tag, entry.effect) == ("COMPU-METHOD-REF", "report")
        assert any(
            e.path == "/Acme/AppTypes/DriveModeCompu" and "not used by any imported enum type" in e.reason
            for e in report.skip_entries
        )
        assert not [e for e in report.skip_entries if e.path == "/Acme/AppTypes/DriveMode"]

    def test_a_packages_selection_keeps_the_enum_and_drops_the_twin(self, tmp_path: Path) -> None:
        """The closure walks the interfaces onto the enum and, through the imported map, its implementation type."""
        _, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert sorted(_data_types(model_root)) == [
            ("application_types", "DriveMode"),
            ("base_types", "Byte"),
            ("implementation_types", "DriveMode"),
        ]


class TestALinearConversionIsReadWithItsOffset:
    """A LINEAR conversion is read with its factor and offset in place.

    ``COMPU-RATIONAL-COEFFS`` lists the lowest power first (numerator ``[offset, factor]``); the offset is non-zero so
    the two cannot be confused.
    """

    FIXTURE = "linear_conversion_with_offset.arxml"

    def test_the_factor_and_the_offset_are_not_swapped(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        conversion = _application(model_root)["Temperature"]["conversion"]

        assert conversion == {"factor": 0.1, "offset": -40.0}
        # The hand-computed physical value: an internal 650 is 25 degrees.
        assert conversion["factor"] * 650 + conversion["offset"] == pytest.approx(25.0)

    def test_the_type_is_not_twinned_and_keeps_its_own_name(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        types = _data_types(model_root)

        assert sorted(types) == [
            ("application_types", "Temperature"),
            ("base_types", "Word"),
            ("implementation_types", "RawTemp"),
        ]
        assert _stated(model_root, types[("application_types", "Temperature")])["range"] == {"min": -40.0, "max": 125.0}
        assert _stated(model_root, types[("application_types", "Temperature")])["unit"] == "deg C"

    def test_no_conversion_is_reported_as_dropped(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert "Physical properties not imported" not in report.render()

    def test_a_packages_selection_keeps_the_conversion(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert _application(model_root)["Temperature"]["conversion"] == {"factor": 0.1, "offset": -40.0}


class TestTheTwoLinearShapesDecisionSixteenRefuses:
    """The importer recomputes no physics.

    A denominator other than ``1`` and a method of several scales import without the conversion, keep unit and range,
    and are reported with their shape.
    """

    FIXTURE = "linear_conversion_not_read.arxml"

    def test_neither_type_carries_a_conversion(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        types = _application(model_root)

        assert _stated(model_root, types["Ratio"]).get("conversion") is None
        assert _stated(model_root, types["Segmented"]).get("conversion") is None

    def test_both_keep_their_unit_and_their_range(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        types = _application(model_root)

        assert _stated(model_root, types["Ratio"])["unit"] == "%"
        assert _stated(model_root, types["Ratio"])["range"] == {"min": 0.0, "max": 100.0}
        assert _stated(model_root, types["Segmented"])["unit"] == "mm"
        assert _stated(model_root, types["Segmented"])["range"] == {"min": 0.0, "max": 1000.0}

    def test_the_report_says_which_of_the_two_shapes_each_was(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE, dry_run=True)

        rendered = report.render()

        assert "Physical properties not imported" in rendered
        assert "denominator" in rendered
        assert "COMPU-SCALE" in rendered

    def test_the_report_lines_name_no_storage_path(self, tmp_path: Path) -> None:
        """An unread-physics line names the export's AUTOSAR paths and never where the project lives."""
        report, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        # Non-vacuity: the loop below passes on an empty list.
        assert report.unread_physics_lines
        for line in report.unread_physics_lines:
            assert str(model_root) not in line
            assert str(model_root.parent) not in line

    def test_a_packages_selection_still_drops_both_conversions(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        types = _application(model_root)

        assert _stated(model_root, types["Ratio"]).get("conversion") is None
        assert _stated(model_root, types["Segmented"]).get("conversion") is None


class TestANonLinearCompuMethodIsNamedWithItsCategory:
    """The category is in the line, because a reader has to know which one."""

    FIXTURE = "non_linear_compu_method.arxml"

    def test_the_type_is_written_without_its_conversion(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        flow = _application(model_root)["Flow"]

        assert flow["kind"] == "application_type"
        assert _stated(model_root, flow).get("conversion") is None
        assert _stated(model_root, flow)["unit"] == "l/min"
        assert _stated(model_root, flow)["range"] == {"min": 0.0, "max": 60.0}

    def test_the_report_names_the_category(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE, dry_run=True)

        rendered = report.render()

        assert "Physical properties not imported" in rendered
        assert "RAT_FUNC" in rendered

    def test_a_packages_selection_keeps_the_line_for_a_type_it_kept(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert "Flow" in _application(model_root)
        assert any("RAT_FUNC" in line for line in report.unread_physics_lines)


class TestAnImplementationTypedDataPrototypeIsKept:
    """A data prototype may be typed either way, and both are written as the export declared them."""

    FIXTURE = "implementation_typed_data_prototype.arxml"

    def test_each_element_keeps_the_type_the_export_named(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        # Each element states the side of its type in the field it sets.
        elements = {
            element["name"]: {
                field: element[field] for field in ("application_type", "implementation_type") if field in element
            }
            for element in _interface(model_root, "IReadings")["data_elements"]
        }

        assert elements == {"count": {"implementation_type": "Counter"}, "pressure": {"application_type": "Pressure"}}

    def test_the_two_targets_are_of_the_two_kinds(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _implementation(model_root)["Counter"]["kind"] == "value_type"
        assert _application(model_root)["Pressure"]["kind"] == "application_type"

    def test_a_packages_selection_pulls_both_into_the_closure(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert "Counter" in _implementation(model_root)
        assert "Pressure" in _application(model_root)

    def test_a_dry_run_reports_the_same_two(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert "IMPORTED 4 data types" in report.render()


class TestAnImplSuffixedNameTheExportDeclaresIsAnOrdinaryType:
    """No twin name is minted: an export's own ``Angle_impl`` is one more implementation type beside the pair."""

    FIXTURE = "twin_name_already_declared.arxml"

    _WRITTEN = [
        ("application_types", "Angle"),
        ("base_types", "Word"),
        ("implementation_types", "Angle"),
        ("implementation_types", "Angle_impl"),
    ]

    def test_the_declared_name_keeps_its_declaration(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _application(model_root)["Angle"]["kind"] == "application_type"
        assert _implementation(model_root)["Angle_impl"]["kind"] == "value_type"
        assert _interface(model_root, "IAngle")["data_elements"][1]["implementation_type"] == "Angle_impl"

    def test_the_pair_keeps_one_name_in_two_layers_and_nothing_is_renamed(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE)

        types = _data_types(model_root)

        assert sorted(types) == self._WRITTEN
        assert types[("implementation_types", "Angle")]["kind"] == "value_type"
        assert report.rename_lines == []

    def test_the_application_type_still_keeps_the_bare_name(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _interface(model_root, "IAngle")["data_elements"][0]["application_type"] == "Angle"

    def test_a_packages_selection_keeps_the_declaration_and_the_pair(self, tmp_path: Path) -> None:
        """The closure holds the application type, the declared ``Angle_impl`` and the pair's implementation member."""
        _, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert sorted(_data_types(model_root)) == self._WRITTEN


class TestNoLineOfTheseReportsNamesWhereTheProjectIsStored:
    """No line of the whole rendered report of these shapes names where the project is stored.

    Does not reach the first line, which names the source export and is excluded (the CLI's ``render(full_path=…)``
    limit).
    """

    @pytest.mark.parametrize("fixture", sorted(_FIXTURES))
    def test_no_line_but_the_source_names_the_project(self, tmp_path: Path, fixture: str) -> None:
        report, model_root = _import(tmp_path, fixture)

        rendered = report.render(verbose=True).splitlines()

        assert rendered[0].startswith("Import report: ")
        offenders = [
            line
            for line in rendered[1:]
            if model_root.as_posix() in line or model_root.parent.as_posix() in line or str(tmp_path) in line
        ]
        assert offenders == [], f"{fixture}: these report lines name where the project is stored: {offenders}"


class TestWhatTheReaderDoesNotModelReachesTheSkipLog:
    """Every ``SwDataDefProps`` property the reader does not model is reported with its own tag."""

    def test_an_unmodelled_property_is_reported_with_its_tag(self, tmp_path: Path) -> None:
        source = tmp_path / "display_format.arxml"
        source.write_text(_DISPLAY_FORMAT_EXPORT, encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)

        rendered = run_import(str(source), str(project / "swcraft.yaml"), dry_run=True).render()

        assert "DISPLAY-FORMAT" in rendered
        assert "SW-CALIBRATION-ACCESS" in rendered

    def test_the_type_itself_is_still_imported(self, tmp_path: Path) -> None:
        source = tmp_path / "display_format.arxml"
        source.write_text(_DISPLAY_FORMAT_EXPORT, encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)

        run_import(str(source), str(project / "swcraft.yaml"))

        assert _application(project / "model")["Gauge"]["kind"] == "application_type"


#: One application primitive with two ``SwDataDefProps`` properties the model does not hold, beside one it does;
#: inline because the shape is about what is reported, not what is imported.
_DISPLAY_FORMAT_EXPORT = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Types</SHORT-NAME>
          <ELEMENTS>
            <UNIT>
              <SHORT-NAME>GaugeUnit</SHORT-NAME>
              <DISPLAY-NAME>bar</DISPLAY-NAME>
            </UNIT>
            <APPLICATION-PRIMITIVE-DATA-TYPE>
              <SHORT-NAME>Gauge</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <DISPLAY-FORMAT>%.2f</DISPLAY-FORMAT>
                    <SW-CALIBRATION-ACCESS>READ-ONLY</SW-CALIBRATION-ACCESS>
                    <UNIT-REF DEST="UNIT">/Acme/Types/GaugeUnit</UNIT-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </APPLICATION-PRIMITIVE-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Interfaces</SHORT-NAME>
          <ELEMENTS>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>IGauge</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>gauge</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/Types/Gauge</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Components</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>GaugeSwc</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pGauge</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Interfaces/IGauge\
</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""
