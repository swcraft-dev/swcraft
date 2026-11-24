"""The importer's mapping sets and the behaviours' references to them, each on a synthetic fixture.

Each shape is a minimal export under ``fixtures/arxml_import_shapes/``, run through ``swcraft import model``; the claims
read back the YAML written, the report rendered and ``check-model`` on the project left behind. The merge verdict and
the unresolved-reference rule are checked on this kind too.

What this module does **not** reach:

* the ``--packages`` walk over the ``DATA-TYPE-MAP`` edge: which member of a conflicting group a selection is narrowed
  to is ``test_arxml_import_cli.py``'s;
* real tool exports: every figure here is synthetic;
* the writer, except where a class claims that import, ``check-model`` and ``generate arxml`` agree;
* ``import component``, which applies neither the merge verdict nor the unresolved-reference rule
  (``test_component_import.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from arxml_import_shapes_support import shape_path
from conftest import model_element_path, model_element_storage_name
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.report import ImportReport
from swcraft_tools.arxml_import.unresolved import CAUSE_ABSENT, CAUSE_CONFLICT
from swcraft_tools.arxml_reader import ABORT, REPORT, SkipCategory
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml

#: The fixtures this module claims its shapes on.
_FIXTURES = (
    "mapping_set_named_by_every_behaviour.arxml",
    "two_behaviours_on_two_sets.arxml",
    "map_onto_a_structure_implementation.arxml",
    "map_of_a_collapsed_application_type.arxml",
    "identical_mapping_sets.arxml",
    "differing_mapping_sets.arxml",
    "unresolved_mapping_references.arxml",
    "mapping_set_named_like_a_data_type.arxml",
    "application_primitive_of_an_uncarried_category.arxml",
    "application_categories_against_the_written_kind.arxml",
    "map_rows_naming_a_merged_application_type.arxml",
    "half_data_type_maps.arxml",
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


def _mapping_sets(model_root: Path) -> dict[str, dict[str, Any]]:
    """Every written ``mapping_sets/`` element by its declared ``name``."""
    return {
        document["name"]: document
        for document in (
            yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in sorted(
                model_root.glob(model_element_storage_name("*", layer="mapping_sets")), key=lambda path: path.as_posix()
            )
        )
    }


def _component(model_root: Path, group: str, name: str) -> dict[str, Any]:
    return yaml.safe_load((model_root / "swc" / group / f"{name}.yaml").read_text(encoding="utf-8"))


def _pairs(mapping_set: dict[str, Any]) -> list[tuple[str, str]]:
    return [(entry["application"], entry["implementation"]) for entry in mapping_set["maps"]]


class TestOneSetEveryBehaviourNames:
    """One ``DATA-TYPE-MAPPING-SET`` referenced by every behaviour becomes one element every component names."""

    FIXTURE = "mapping_set_named_by_every_behaviour.arxml"

    def test_the_set_is_one_element_with_its_one_pair(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _pairs(_mapping_sets(model_root)["PlatformMaps"]) == [("Cadence", "RawCount")]

    def test_every_component_names_the_set(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _component(model_root, "Components", "CadenceSource")["data_type_mappings"] == ["PlatformMaps"]
        assert _component(model_root, "Components", "CadenceSink")["data_type_mappings"] == ["PlatformMaps"]

    def test_the_imported_project_passes_check_model(self, tmp_path: Path) -> None:
        """A physical type on a port, and a set the component names that pairs it with its store."""
        _, model_root = _import(tmp_path, self.FIXTURE)

        result = check_cross_layer_consistency(model_root=str(model_root))

        assert result.consistent, result.issues

    def test_a_selection_over_the_component_package_keeps_the_set(self, tmp_path: Path) -> None:
        """The closure follows the map from the application type to its store.

        An application type names no other type, so without that edge the store and the map fall outside every
        selection.
        """
        report, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert report.imported_mapping_sets == 1
        assert _pairs(_mapping_sets(model_root)["PlatformMaps"]) == [("Cadence", "RawCount")]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_the_set_is_counted_in_the_report_and_in_the_total(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE)

        assert report.imported_mapping_sets == 1
        assert "IMPORTED 1 mapping sets" in report.render()

    def test_a_dry_run_writes_no_set_and_reports_the_same_count(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert report.imported_mapping_sets == 1
        assert list(model_root.glob(model_element_storage_name("*", layer="mapping_sets"))) == []

    def test_no_set_is_reported_as_having_no_swcraft_representation(self, tmp_path: Path) -> None:
        """``DATA-TYPE-MAPPING-SET`` is an element of the model, so listing it would say the import lost it."""
        report, _ = _import(tmp_path, self.FIXTURE)

        assert [entry for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAPPING-SET"] == []


class TestTwoBehavioursOnTwoSets:
    """``data_type_mappings`` is an ordered list, and it is per component."""

    FIXTURE = "two_behaviours_on_two_sets.arxml"

    def test_both_sets_are_written(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE)

        assert report.imported_mapping_sets == 2
        assert sorted(_mapping_sets(model_root)) == ["PressureMaps", "TorqueMaps"]

    def test_the_component_naming_both_keeps_the_exports_own_order(self, tmp_path: Path) -> None:
        """The component's ``data_type_mappings`` keeps the export's order, never a sorted one."""
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _component(model_root, "Components", "Both")["data_type_mappings"] == [
            "TorqueMaps",
            "PressureMaps",
        ]

    def test_the_component_naming_one_names_only_that_one(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _component(model_root, "Sinks", "TorqueOnly")["data_type_mappings"] == ["TorqueMaps"]

    def test_the_imported_project_passes_check_model(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        result = check_cross_layer_consistency(model_root=str(model_root))

        assert result.consistent, result.issues

    def test_a_selection_drops_the_set_nothing_names_and_narrows_the_one_it_keeps(self, tmp_path: Path) -> None:
        """A selection drops the set no kept component names and narrows the named one to the rows it can carry.

        A set is a container: one entry it cannot carry is no reason to lose the others, as in ``map_mapping_sets``.
        """
        report, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Sinks"])

        assert sorted(_mapping_sets(model_root)) == ["TorqueMaps"]
        assert _pairs(_mapping_sets(model_root)["TorqueMaps"]) == [("Torque", "RawB")]
        assert [line for line in report.closure_lines if "mapping set" in line or "data type map" in line] == [
            "1 mapping set(s) excluded (outside the selection, and no component it kept names them): PressureMaps",
            "1 data type map(s) excluded (one side fell outside the selection)",
        ]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues


class TestAMapOntoAnImplementationCompositeIsReportedNotWritten:
    """A map onto a ``STRUCTURE`` the import cannot carry is reported, and the set is written with what it can carry.

    The fixture's ``STRUCTURE`` declares no ``SUB-ELEMENTS``, a shape the model does not hold; a supported structure's
    map is written (``test_impl_composites_import.py``).
    """

    FIXTURE = "map_onto_a_structure_implementation.arxml"

    def test_the_set_carries_the_one_map_it_can_write(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _pairs(_mapping_sets(model_root)["Maps"]) == [("Distance", "RawScalar")]

    def test_the_dropped_map_is_reported_and_the_skipped_list_names_the_shape(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE)

        reasons = [entry.reason for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"]
        structure = [entry.reason for entry in report.skip_entries if entry.path == "/Acme/Types/RawPair"]

        assert reasons == [
            "implementation side /Acme/Types/RawPair was not imported as a data type "
            "(see the skipped list for its own reason)"
        ]
        assert structure == ["STRUCTURE declares no SUB-ELEMENTS, and this release does not import it"]

    def test_the_record_itself_is_still_imported(self, tmp_path: Path) -> None:
        """The application record is still written with its application-typed members."""
        _, model_root = _import(tmp_path, self.FIXTURE)

        record = yaml.safe_load((model_element_path(model_root, "Span", kind="record")).read_text(encoding="utf-8"))

        assert record["kind"] == "record"
        assert [field["type"] for field in record["fields"]] == ["Distance", "Distance"]

    def test_a_dry_run_reports_the_drop_and_writes_nothing(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert [entry.tag for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == ["DATA-TYPE-MAP"]
        assert list(model_root.glob(model_element_storage_name("*", layer="mapping_sets"))) == []


class TestAMapWhoseApplicationSideCollapsed:
    """A map whose bare same-name pair collapsed is dropped beside the reduction line, never refused.

    The set keeps the maps the collapse does not reach.
    """

    FIXTURE = "map_of_a_collapsed_application_type.arxml"

    def test_the_collapsed_pairs_map_is_dropped_and_the_other_is_written(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _pairs(_mapping_sets(model_root)["Maps"]) == [("Angle", "RawAngle")]

    def test_the_drop_names_the_element_the_two_declarations_became(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE)

        assert [(entry.reason, entry.effect) for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == [
            (
                "the map row of the same-name pair 'Level' is folded into its collapse: both sides are the one "
                "implementation type 'Level'",
                REPORT,
            )
        ]
        assert [entry.category for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == [
            SkipCategory.NOT_SUPPORTED
        ]

    def test_the_reduction_line_beside_it_says_which_declarations_merged(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE)

        assert report.reduction_lines == [
            "/Acme/AppTypes/Level + /Acme/ImplTypes/Level -> Level (app/impl twin collapse)"
        ]

    def test_the_imported_project_passes_check_model(self, tmp_path: Path) -> None:
        """The collapsed pair needs no mapping and the kept pair has one, so nothing is left unmapped."""
        _, model_root = _import(tmp_path, self.FIXTURE)

        result = check_cross_layer_consistency(model_root=str(model_root))

        assert result.consistent, result.issues


def _import_variant(tmp_path: Path, fixture: str, old: str, new: str) -> ImportReport:
    """Run ``import model`` on *fixture* with one spelling replaced; return the report.

    The variant is written under ``tmp_path`` so the corpus keeps one file per row of ``SHAPES``.
    """
    text = shape_path(fixture).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    variant = tmp_path / "variant.arxml"
    variant.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    project = _init_project(tmp_path)
    return run_import(str(variant), str(project / "swcraft.yaml"))


#: The collapsed pair's own row in ``map_of_a_collapsed_application_type.arxml``.
_LEVEL_ROW = (
    '<APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/AppTypes/Level'
    "</APPLICATION-DATA-TYPE-REF>\n"
    '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/ImplTypes/Level'
    "</IMPLEMENTATION-DATA-TYPE-REF>\n"
    "                </DATA-TYPE-MAP>\n"
)
#: The same-name pair's row with an extra ``App/Level -> App/Level`` row before it.
_LEVEL_ROW_WITH_A_SELF_MAP_BEFORE_IT = (
    '<APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/AppTypes/Level'
    "</APPLICATION-DATA-TYPE-REF>\n"
    '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/AppTypes/Level'
    "</IMPLEMENTATION-DATA-TYPE-REF>\n"
    "                </DATA-TYPE-MAP>\n"
    "                <DATA-TYPE-MAP>\n"
    "                  " + _LEVEL_ROW
)
#: The end of the fixture's one set, and the same end followed by a SECOND set, declared after it,
#: whose one row is ``App/Level -> App/Level`` (in the same set it would be the duplicate-row line).
_MAPS_END = "</DATA-TYPE-MAPPING-SET>\n          </ELEMENTS>"
_MAPS_END_AND_A_SELF_MAP_SET_AFTER_IT = (
    "</DATA-TYPE-MAPPING-SET>\n"
    "            <DATA-TYPE-MAPPING-SET>\n"
    "              <SHORT-NAME>Extra</SHORT-NAME>\n"
    "              <DATA-TYPE-MAPS>\n"
    "                <DATA-TYPE-MAP>\n"
    '                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/AppTypes/Level'
    "</APPLICATION-DATA-TYPE-REF>\n"
    '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/AppTypes/Level'
    "</IMPLEMENTATION-DATA-TYPE-REF>\n"
    "                </DATA-TYPE-MAP>\n"
    "              </DATA-TYPE-MAPS>\n"
    "            </DATA-TYPE-MAPPING-SET>\n"
    "          </ELEMENTS>"
)
_FOLDED_LEVEL = (
    "the map row of the same-name pair 'Level' is folded into its collapse: both sides are the one "
    "implementation type 'Level'"
)
_SELF_MAP_LEVEL = "both sides resolve to the data type 'Level' (the same-name pair was collapsed)"


class TestOnlyTheRowThatCausedTheCollapseIsFolded:
    """The folded row is decided by the pair of paths a ``TwinReduction`` records, not the application path alone.

    A file may hold a self-map ``App/X -> App/X`` beside the pair's ``App/X -> Impl/X``: only the second folds.
    """

    FIXTURE = "map_of_a_collapsed_application_type.arxml"

    def test_the_pairs_row_folds_and_the_extra_self_map_aborts(self, tmp_path: Path) -> None:
        report = _import_variant(tmp_path, self.FIXTURE, _LEVEL_ROW, _LEVEL_ROW_WITH_A_SELF_MAP_BEFORE_IT)

        assert [(entry.reason, entry.effect) for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == [
            (_SELF_MAP_LEVEL, ABORT),
            (_FOLDED_LEVEL, REPORT),
        ]

    def test_a_self_map_through_an_implementation_types_own_path_still_aborts(self, tmp_path: Path) -> None:
        """A self-map through an implementation type's own path still aborts, and its reason claims no collapse."""
        report = _import_variant(
            tmp_path,
            self.FIXTURE,
            '<APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/AppTypes/Angle',
            '<APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/ImplTypes/RawAngle',
        )

        assert [(entry.reason, entry.effect) for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == [
            (_FOLDED_LEVEL, REPORT),
            ("both sides resolve to the data type 'RawAngle'", ABORT),
        ]

    def test_an_extra_self_map_after_the_pairs_row_collapses_nothing_and_claims_no_collapse(
        self, tmp_path: Path
    ) -> None:
        """The twin candidate is the last same-name row: an extra self-map after the pair's row collapses nothing.

        Both members are kept, the pair's row is written, and the extra row aborts without claiming a collapse.
        """
        report = _import_variant(tmp_path, self.FIXTURE, _MAPS_END, _MAPS_END_AND_A_SELF_MAP_SET_AFTER_IT)

        assert [(entry.reason, entry.effect) for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == [
            ("both sides resolve to the data type 'Level'", ABORT),
        ]
        assert not [line for line in report.reduction_lines if "Level" in line], report.reduction_lines


class TestADuplicateRowIsAskedOfTheElementsNotTheNames:
    """The duplicate-row checks compare ``(layer, name)``, not bare names.

    A kept pair's ``Impl/Level -> Impl/Level`` resolves to the same two names but is the self-map abort, not a repeat.
    """

    FIXTURE = "map_of_a_collapsed_application_type.arxml"

    def test_a_self_map_after_a_kept_pairs_row_aborts(self, tmp_path: Path) -> None:
        text = shape_path(self.FIXTURE).read_text(encoding="utf-8")
        bare_level = (
            "<SHORT-NAME>Level</SHORT-NAME>\n              <CATEGORY>VALUE</CATEGORY>\n            </APPLICATION"
        )
        assert text.count(bare_level) == 1
        level_with_a_unit = (
            "<SHORT-NAME>Level</SHORT-NAME>\n"
            "              <CATEGORY>VALUE</CATEGORY>\n"
            "              <SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
            '<UNIT-REF DEST="UNIT">/Acme/AppTypes/Degree</UNIT-REF>'
            "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>\n"
            "            </APPLICATION"
        )
        self_map_after_it = (
            _LEVEL_ROW + "                <DATA-TYPE-MAP>\n"
            '                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/ImplTypes/Level'
            "</APPLICATION-DATA-TYPE-REF>\n"
            '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/ImplTypes/Level'
            "</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "                </DATA-TYPE-MAP>\n"
        )
        assert text.count(_LEVEL_ROW) == 1
        variant = tmp_path / "variant.arxml"
        variant.write_text(
            text.replace(bare_level, level_with_a_unit).replace(_LEVEL_ROW, self_map_after_it),
            encoding="utf-8",
            newline="\n",
        )
        project = _init_project(tmp_path)

        report = run_import(str(variant), str(project / "swcraft.yaml"))

        assert not report.reduction_lines, report.reduction_lines
        assert [(entry.reason, entry.effect) for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == [
            ("both sides resolve to the data type 'Level'", ABORT),
        ]
        assert _pairs(_mapping_sets(project / "model")["Maps"]) == [("Level", "Level"), ("Angle", "RawAngle")]


class TestTheMergeVerdictReachesTheMappingSet:
    """The same-name merge verdict reaches the mapping set: one element when maps resolve alike, none when not."""

    def test_two_identical_sets_across_packages_merge(self, tmp_path: Path) -> None:
        """Two identical sets across packages merge, their maps compared resolved.

        Comparing the spelled paths would refuse a pair whose declarations have just merged.
        """
        report, model_root = _import(tmp_path, "identical_mapping_sets.arxml")

        assert [str(group.key) for group in report.conflicting_groups] == []
        assert "mapping_sets/DATA-TYPE-MAPPING-SET/Maps" in [str(group.key) for group in report.merged_groups]
        assert _pairs(_mapping_sets(model_root)["Maps"]) == [("Flow", "RawFlow")]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_two_differing_sets_conflict_and_neither_is_written(self, tmp_path: Path) -> None:
        """A conflicting group imports neither set, and the component keeps the path the export spelled."""
        report, model_root = _import(tmp_path, "differing_mapping_sets.arxml")

        group = next(
            group for group in report.conflicting_groups if str(group.key) == "mapping_sets/DATA-TYPE-MAPPING-SET/Maps"
        )
        assert group.difference == "DATA-TYPE-MAPS"
        assert list(model_root.glob(model_element_storage_name("*", layer="mapping_sets"))) == []
        assert _component(model_root, "App", "FlowSwc")["data_type_mappings"] == ["/Acme/Left/Maps"]
        assert [(record.target, record.cause) for record in report.unresolved_references] == [
            ("/Acme/Left/Maps", CAUSE_CONFLICT)
        ]

    def test_a_selection_keeping_one_declaration_re_admits_it(self, tmp_path: Path) -> None:
        """A selection naming one declaring package re-admits that member, and the reference resolves to a name."""
        report, model_root = _import(tmp_path, "differing_mapping_sets.arxml", packages=["/Acme/App", "/Acme/Left"])

        assert report.conflicting_groups == []
        assert [line for line in report.closure_lines if "re-admitted" in line] == [
            "1 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            "mapping_sets/DATA-TYPE-MAPPING-SET/Maps at /Acme/Left/Maps"
        ]
        assert _pairs(_mapping_sets(model_root)["Maps"]) == [("Flow", "RawNarrow")]
        assert _component(model_root, "App", "FlowSwc")["data_type_mappings"] == ["Maps"]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues


class TestTheUnresolvedReferenceRuleReachesTheThreeNewRoles:
    """The unresolved-reference rule reaches a map's two sides and ``data_type_mappings``.

    Each keeps the path the export spelled, so the element survives and ``check-model`` names what is missing.
    """

    FIXTURE = "unresolved_mapping_references.arxml"

    def test_the_three_roles_are_recorded_with_their_referrers(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE)

        assert [
            (record.target, record.cause, [referrer.field for referrer in record.referrers])
            for record in report.unresolved_references
        ] == [
            ("/Elsewhere/Types/Humidity", CAUSE_ABSENT, ["mapping_sets.map.application"]),
            ("/Elsewhere/Types/RawHumidity", CAUSE_ABSENT, ["mapping_sets.map.implementation"]),
            ("/Elsewhere/Mappings/PlatformMaps", CAUSE_ABSENT, ["swc.data_type_mappings"]),
        ]

    def test_the_map_is_kept_with_the_path_the_export_spelled(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _pairs(_mapping_sets(model_root)["Maps"]) == [
            ("Temperature", "RawTemp"),
            ("/Elsewhere/Types/Humidity", "/Elsewhere/Types/RawHumidity"),
        ]

    def test_the_component_keeps_both_references(self, tmp_path: Path) -> None:
        _, model_root = _import(tmp_path, self.FIXTURE)

        assert _component(model_root, "Components", "TempSwc")["data_type_mappings"] == [
            "Maps",
            "/Elsewhere/Mappings/PlatformMaps",
        ]

    def test_check_model_reports_exactly_the_three_missing_targets(self, tmp_path: Path) -> None:
        """``check-model`` reports exactly the three missing targets, under the rule each role already has."""
        _, model_root = _import(tmp_path, self.FIXTURE)

        result = check_cross_layer_consistency(model_root=str(model_root))

        assert sorted(record.rule for record in result.records) == [
            "mapping-set.unknown-application",
            "mapping-set.unknown-implementation",
            "swc.unknown-mapping-set",
        ]


class TestTwoRowsNamingOneMergedApplicationTypeKeepTheResolvableOne:
    """Of two rows naming one merged application type, the one that resolves is kept.

    The row is decided over the element each row resolves to, never the spelled path; keyed on the path, the
    unresolvable row would be written and ``check-model`` report a ``mapping-set.unknown-implementation``.
    """

    FIXTURE = "map_rows_naming_a_merged_application_type.arxml"
    DROP = "a second row of this set maps /Acme/TypesCopy/Temperature onto a type this import carries"

    @staticmethod
    def _assert_the_three_commands_agree(model_root: Path, tmp_path: Path) -> None:
        assert _pairs(_mapping_sets(model_root)["Maps"]) == [("Temperature", "RawTemp")]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues
        generate_arxml(str(model_root), str(tmp_path / "model.arxml"))

    def test_the_whole_file_keeps_the_resolvable_row(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE)

        assert [entry.reason for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == [self.DROP]
        assert report.unresolved_references == []
        self._assert_the_three_commands_agree(model_root, tmp_path)

    def test_a_selection_over_the_component_package_keeps_it_too(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert [entry.reason for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == [self.DROP]
        self._assert_the_three_commands_agree(model_root, tmp_path)

    def test_a_dry_run_reports_the_same_drop_and_writes_nothing(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert [entry.reason for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"] == [self.DROP]
        assert report.unresolved_references == []
        assert list(model_root.glob(model_element_storage_name("*", layer="mapping_sets"))) == []


class TestARowRepeatingTheMergedPairIsADescriptiveLine:
    """Two rows that map the same pair after the merge: one is written, the other is a descriptive line.

    The line sits at the row's key ``({set}/DATA-TYPE-MAPS, DATA-TYPE-MAP)``.
    """

    def test_the_repeat_is_one_descriptive_line(self, tmp_path: Path) -> None:
        text = shape_path("map_rows_naming_a_merged_application_type.arxml").read_text(encoding="utf-8")
        text = text.replace("/Elsewhere/Types/RawTemp", "/Acme/Types/RawTemp")
        source = tmp_path / "repeated_merged_pair.arxml"
        source.write_text(text, encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert _pairs(_mapping_sets(project / "model")["Maps"]) == [("Temperature", "RawTemp")]
        rows = [entry for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"]
        assert [(entry.path, entry.effect) for entry in rows] == [("/Acme/Mappings/Maps/DATA-TYPE-MAPS", "report")]
        assert "'Temperature' -> 'RawTemp'" in rows[0].reason


class TestAHalfMapIsASkipEntryNotASilentDrop:
    """A ``DATA-TYPE-MAP`` with one side absent is a skip entry under ``DATA-TYPE-MAP``, never a silent drop.

    The set is still written with its whole maps; ``import component`` refuses the same entry
    (``test_component_import.py``).
    """

    FIXTURE = "half_data_type_maps.arxml"
    SKIPS = [
        (
            "/Acme/Mappings/Maps/DATA-TYPE-MAPS",
            "a DATA-TYPE-MAP [1] with no IMPLEMENTATION-DATA-TYPE-REF pairs nothing -- the map is dropped",
        ),
        (
            "/Acme/Mappings/Maps/DATA-TYPE-MAPS",
            "a DATA-TYPE-MAP [2] with no APPLICATION-DATA-TYPE-REF pairs nothing -- the map is dropped",
        ),
    ]

    @staticmethod
    def _skips(report: ImportReport) -> list[tuple[str, str]]:
        return [(entry.path, entry.reason) for entry in report.skip_entries if entry.tag == "DATA-TYPE-MAP"]

    def test_the_whole_file_reports_both_and_keeps_the_whole_map(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE)

        assert self._skips(report) == self.SKIPS
        assert _pairs(_mapping_sets(model_root)["Maps"]) == [("Temperature", "RawTemp")]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues
        generate_arxml(str(model_root), str(tmp_path / "model.arxml"))

    def test_a_selection_over_the_component_package_reports_both(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert self._skips(report) == self.SKIPS
        assert _pairs(_mapping_sets(model_root)["Maps"]) == [("Temperature", "RawTemp")]

    def test_a_dry_run_reports_both_and_writes_nothing(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert self._skips(report) == self.SKIPS
        assert list(model_root.glob(model_element_storage_name("*", layer="mapping_sets"))) == []


class TestASetNamedLikeADataTypeKeepsItsNameAndBuilds:
    """A set named like a data type of another package keeps its name, and the project checks and builds.

    The set lies in its own ``DataTypes/MappingSets`` package, so no rename line is needed.
    """

    FIXTURE = "mapping_set_named_like_a_data_type.arxml"
    NAME = "Cadence"

    def _assert_the_three_commands_agree(self, model_root: Path, tmp_path: Path) -> None:
        assert set(_mapping_sets(model_root)) == {self.NAME}
        assert _pairs(_mapping_sets(model_root)[self.NAME]) == [("Cadence", "RawCount")]
        for name in ("CadenceSource", "CadenceSink"):
            assert _component(model_root, "Components", name)["data_type_mappings"] == [self.NAME]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))
        text = output.read_text(encoding="utf-8")
        assert text.count(f"<SHORT-NAME>{self.NAME}</SHORT-NAME>") == 2, "the set and the type, one each"

    def test_the_whole_file_imports_keeps_the_sets_name_and_builds(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE)

        assert report.rename_lines == []
        assert report.imported_mapping_sets == 1
        self._assert_the_three_commands_agree(model_root, tmp_path)

    def test_a_selection_over_the_component_package_keeps_the_name_and_builds(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert report.rename_lines == []
        self._assert_the_three_commands_agree(model_root, tmp_path)

    def test_a_dry_run_reports_no_rename_and_writes_nothing(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert report.rename_lines == []
        assert list(model_root.glob(model_element_storage_name("*", layer="mapping_sets"))) == []
        assert list(model_root.glob("swc/**/*.yaml")) == []


class TestAnUncarriedCategoryIsANoteNotASkip:
    """An application primitive of ``CATEGORY`` ``BOOLEAN`` is written as ``application_type`` with a physics note.

    It is never a skip entry: on ``import component`` that would be the lossy-import refusal, making every component
    export carrying a ``BOOLEAN`` primitive unimportable (``test_component_import.py``).
    """

    FIXTURE = "application_primitive_of_an_uncarried_category.arxml"
    NOTE = (
        "/Acme/Types/Cadence: CATEGORY 'BOOLEAN' has no SWCraft kind of its own -- the type is imported "
        "and would be re-exported as CATEGORY 'VALUE'"
    )

    def test_the_whole_file_writes_the_type_and_notes_the_category(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE)

        assert report.unread_physics_lines == [self.NOTE]
        assert [entry for entry in report.skip_entries if entry.tag == "CATEGORY"] == []
        written = yaml.safe_load(
            (model_element_path(model_root, "Cadence", kind="application_type")).read_text(encoding="utf-8")
        )
        assert written["kind"] == "application_type"
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_a_selection_carries_the_note_with_the_type(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert report.unread_physics_lines == [self.NOTE]

    def test_a_dry_run_carries_the_note_and_writes_nothing(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert report.unread_physics_lines == [self.NOTE]
        assert list((model_root / "data_types").rglob("*.yaml")) == []


class TestACategoryIsComparedWithTheOneItsKindReExports:
    """The category note compares the source's ``CATEGORY`` with the ``VALUE`` the written kind re-exports.

    The writer emits no ``TEXTTABLE`` on an enum's type (``_emit_enum_type``), so a ``TEXTTABLE`` enum is noted while a
    ``VALUE`` enum is not.
    """

    FIXTURE = "application_categories_against_the_written_kind.arxml"
    NOTES = [
        "/Acme/Types/Cadence: CATEGORY 'TEXTTABLE' is imported as an application_type -- the type would "
        "be re-exported as CATEGORY 'VALUE'",
        "/Acme/Types/Mode: CATEGORY 'TEXTTABLE' is imported as an enum -- the type would be re-exported as "
        "CATEGORY 'VALUE'",
    ]
    KINDS = {
        "Gear": "enum",
        "Cadence": "application_type",
        "Mode": "enum",
        "Torque": "application_type",
        "Level": "application_type",
    }

    def test_the_whole_file_notes_both_changes_and_nothing_else(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE)

        assert report.unread_physics_lines == self.NOTES
        assert [entry for entry in report.skip_entries if entry.tag == "CATEGORY"] == []
        for name, kind in self.KINDS.items():
            written = yaml.safe_load(model_element_path(model_root, name, kind=kind).read_text(encoding="utf-8"))
            assert written["kind"] == kind, name
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_a_selection_carries_the_notes_with_the_types(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, self.FIXTURE, packages=["/Acme/Components"])

        assert report.unread_physics_lines == self.NOTES

    def test_a_dry_run_carries_the_notes_and_writes_nothing(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, self.FIXTURE, dry_run=True)

        assert report.unread_physics_lines == self.NOTES
        assert list((model_root / "data_types").rglob("*.yaml")) == []

    def test_a_boolean_enumeration_imports_as_an_enum_and_is_noted_as_value(self, tmp_path: Path) -> None:
        """A ``BOOLEAN`` enumeration over a ``TEXTTABLE`` method imports as an enum, noted as ``VALUE``.

        The standard admits the spelling at R22-11. The note takes the *no SWCraft kind of its own* wording, the limit
        ``uncarried_category_note`` states.
        """
        text = shape_path(self.FIXTURE).read_text(encoding="utf-8")
        gear = "<SHORT-NAME>Gear</SHORT-NAME>\n              <CATEGORY>VALUE</CATEGORY>"
        assert text.count(gear) == 1
        source = tmp_path / "boolean_enumeration.arxml"
        source.write_text(
            text.replace(gear, gear.replace("VALUE", "BOOLEAN")),
            encoding="utf-8",
            newline="\n",
        )
        project = _init_project(tmp_path, "proj")

        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert (
            "/Acme/Types/Gear: CATEGORY 'BOOLEAN' has no SWCraft kind of its own -- the type is imported and "
            "would be re-exported as CATEGORY 'VALUE'"
        ) in report.unread_physics_lines
        assert len(report.unread_physics_lines) == len(self.NOTES) + 1
        written = yaml.safe_load(
            (model_element_path(project / "model", "Gear", kind="enum")).read_text(encoding="utf-8")
        )
        assert written["kind"] == "enum"


class TestNoLineOfTheseReportsNamesWhereTheProjectIsStored:
    """No line of the whole rendered report of these shapes names where the project is stored.

    Does not reach the first line, which names the source export, or any line no fixture of this corpus produces.
    """

    @pytest.mark.parametrize("fixture", _FIXTURES)
    def test_no_rendered_line_but_the_header_carries_the_project_path(self, tmp_path: Path, fixture: str) -> None:
        report, model_root = _import(tmp_path, fixture)

        rendered = report.render(verbose=True).splitlines()

        offenders = [line for line in rendered[1:] if str(model_root) in line or str(tmp_path) in line]
        assert offenders == []
