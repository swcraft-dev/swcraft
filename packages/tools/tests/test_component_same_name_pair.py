"""``export component`` -> ``import component`` of a kept same-name application/implementation pair.

An application type and an implementation type are elements of two layers, so one name may be
declared once in each and a mapping set may pair them. An import that collapsed ``(layer, name)`` onto
``name`` would write one file, refuse a "duplicate" or match the wrong side of a target. A **bare**
pair the project keeps because a record outside the closure names it re-imports ``unchanged`` through
the target's tolerant scan (``target_project.target_bare_pairs``); into an empty project it collapses
as ``import model`` would, its map row folded into a *Not carried from the file* line, and an extra
``App/X -> App/X`` row beside it is a refused self-map. Into a target holding only the implementation
``ProbeShared``, the application type is created and the implementation type reused.

What this does not reach: a pair whose two sides diverge from a target's declarations (the conflict
policy's own suites), and a pair under a record field or an array element, which resolve in
``application_types`` alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path, write_model_element
from mode_bit_support import read_denied
from project_support import files_of, init_project
from swcraft_tools.arxml_import import map_data_types
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.component_exchange import export_component, import_component
from swcraft_tools.component_exchange.import_ import (
    ComponentImportConflictError,
    LossyComponentImportError,
    plan_component_import,
)
from swcraft_tools.component_exchange.target_project import target_bare_pairs
from swcraft_tools.data_description import read_data_description
from swcraft_tools.schema import ApplicationDataType, ValueDataType
from swcraft_tools.validator import check_cross_layer_consistency

SHARED = "ProbeShared"

_BASE = {
    "name": "ProbeShared_base",
    "kind": "base_type",
    "size": 16,
    "encoding": "NONE",
    "native_declaration": "uint16",
}
_IMPLEMENTATION = {"name": SHARED, "kind": "value_type", "base": "ProbeShared_base"}
_APPLICATION = {"name": SHARED, "kind": "application_type", "unit": "km/h"}
_MAPS = {"name": "ProbeMaps", "maps": [{"application": SHARED, "implementation": SHARED}]}


def _write(model_root: Path, payload: dict[str, Any], *, layer: str | None = None) -> Path:
    """One element file, routed by kind (or *layer*), LF-terminated."""
    return write_model_element(model_root, payload, layer=layer)


def _source(tmp_path: Path) -> Path:
    """The exporting project: the pair, its base type, the set pairing them, one interface, one SWC."""
    root = tmp_path / "source_model"
    _write(root, _BASE)
    _write(root, _IMPLEMENTATION)
    _write(root, _APPLICATION)
    _write(root, _MAPS, layer="mapping_sets")
    _write(
        root,
        {
            "name": "IProbe",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "phys", "application_type": SHARED},
                {"name": "impl", "implementation_type": SHARED},
            ],
        },
        layer="interfaces",
    )
    swc_path = root / "swc" / "body" / "ProbeSwc.yaml"
    swc_path.parent.mkdir(parents=True, exist_ok=True)
    swc_path.write_text(
        yaml.safe_dump(
            {
                "name": "ProbeSwc",
                "type": "application",
                "ports": [{"name": "pProbe", "direction": "provide", "interface": "IProbe"}],
                "runnables": [{"name": "ProbeRunnable", "events": [{"type": "timing", "period": 0.01}]}],
                "data_type_mappings": ["ProbeMaps"],
                "handle_termination_and_restart": "no_support",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return root


def _export(tmp_path: Path) -> Path:
    arxml = tmp_path / "ProbeSwc.arxml"
    export_component(str(_source(tmp_path)), "ProbeSwc", str(arxml))
    return arxml


def _type_files(model_root: Path) -> list[str]:
    """Every file of the three data description layers, model-root-relative and POSIX-spelled."""
    return sorted(
        path.relative_to(model_root).as_posix()
        for directory in ("data_types/application", "data_types/implementation", "data_types/base")
        for path in (model_root / directory).glob("*.yaml")
    )


class TestTheExportCarriesBothSidesOfThePair:
    def test_the_export_holds_one_type_per_side_and_the_map_between_them(self, tmp_path: Path) -> None:
        """The closure keeps ``(layer, name)``: both types reach the file, each in its own package."""
        text = _export(tmp_path).read_text(encoding="utf-8")

        assert f"/DataTypes/Application/{SHARED}" in text
        assert f"/DataTypes/Implementation/{SHARED}" in text
        assert "<SHORT-NAME>ProbeMaps</SHORT-NAME>" in text


class TestARoundTripIntoAFreshProjectKeepsThePair:
    def test_both_files_are_written_under_the_declared_name_each_in_its_own_layer(self, tmp_path: Path) -> None:
        arxml = _export(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        assert _type_files(model_root) == [
            f"data_types/application/{SHARED}.yaml",
            "data_types/base/ProbeShared_base.yaml",
            f"data_types/implementation/{SHARED}.yaml",
        ]
        assert sorted(report.created_data_types) == sorted([SHARED, SHARED, "ProbeShared_base"])
        assert report.reused_data_types == []
        view = read_data_description(str(model_root))
        assert isinstance(view.application(SHARED), ApplicationDataType)
        assert isinstance(view.implementation(SHARED), ValueDataType)
        assert view.application(SHARED).unit == "km/h"
        assert view.implementation(SHARED).base == "ProbeShared_base"

    def test_the_map_between_the_two_sides_is_intact(self, tmp_path: Path) -> None:
        arxml = _export(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        assert report.created_mapping_sets == ["ProbeMaps"]
        written = yaml.safe_load(
            model_element_path(model_root, "ProbeMaps", layer="mapping_sets").read_text(encoding="utf-8")
        )
        assert written == _MAPS

    def test_each_data_element_keeps_its_side_and_nothing_is_renamed_or_reported(self, tmp_path: Path) -> None:
        arxml = _export(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        interface = yaml.safe_load(
            model_element_path(model_root, "IProbe", layer="interfaces").read_text(encoding="utf-8")
        )
        assert interface["data_elements"] == [
            {"name": "phys", "application_type": SHARED},
            {"name": "impl", "implementation_type": SHARED},
        ]
        # The one line every SWCraft export carries (its SWC-IMPLEMENTATION) is out of scope;
        # no line may concern the pair.
        assert not [line for line in report.skip_lines if SHARED in line], report.skip_lines
        assert not any("~" in Path(written).name for written in report.written_paths), report.written_paths
        result = check_cross_layer_consistency(str(model_root))
        assert [
            record.rule for record in result.records if record.rule.startswith(("data-type.", "mapping-set."))
        ] == []


class TestATargetHoldingOnlyTheImplementationSide:
    def test_the_application_type_is_created_and_the_implementation_type_reused(self, tmp_path: Path) -> None:
        arxml = _export(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        _write(model_root, _BASE)
        implementation_file = _write(model_root, _IMPLEMENTATION)
        implementation_before = implementation_file.read_bytes()

        report = import_component(str(arxml), str(model_root))

        assert report.created_data_types == [SHARED]
        assert sorted(report.reused_data_types) == sorted([SHARED, "ProbeShared_base"])
        assert implementation_file.read_bytes() == implementation_before
        assert _type_files(model_root) == [
            f"data_types/application/{SHARED}.yaml",
            "data_types/base/ProbeShared_base.yaml",
            f"data_types/implementation/{SHARED}.yaml",
        ]
        created = yaml.safe_load(
            model_element_path(model_root, SHARED, layer="application_types").read_text(encoding="utf-8")
        )
        assert created["kind"] == "application_type"
        assert created["unit"] == "km/h"
        assert str(model_element_path(model_root, SHARED, layer="application_types")) in report.written_paths
        assert str(implementation_file) not in report.written_paths


# A BARE pair the project keeps

BARE = "ProbeBare"

_BARE_BASE = {
    "name": "ProbeBare_base",
    "kind": "base_type",
    "size": 8,
    "encoding": "NONE",
    "native_declaration": "uint8",
}
_BARE_IMPLEMENTATION = {"name": BARE, "kind": "value_type", "base": "ProbeBare_base"}
_BARE_APPLICATION = {"name": BARE, "kind": "application_type"}
_BARE_MAPS = {"name": "ProbeBareMaps", "maps": [{"application": BARE, "implementation": BARE}]}
#: The record whose field names the bare application member: it is what makes the
#: project keep the pair, and no component of the export reaches it.
_BARE_RECORD = {"name": "ProbeBareRecord", "kind": "record", "fields": [{"name": "value", "type": BARE}]}


def _bare_project(tmp_path: Path) -> Path:
    """A project keeping the bare pair ``ProbeBare`` (a record field names it) and one component using it.

    The record lies outside the component's closure, so its export names the application member nowhere.
    """
    model_root = init_project(tmp_path / "proj") / "model"
    _write(model_root, _BARE_BASE)
    _write(model_root, _BARE_IMPLEMENTATION)
    _write(model_root, _BARE_APPLICATION)
    _write(model_root, _BARE_RECORD)
    _write(model_root, _BARE_MAPS, layer="mapping_sets")
    _write(
        model_root,
        {"name": "IBare", "kind": "sender_receiver", "data_elements": [{"name": "phys", "application_type": BARE}]},
        layer="interfaces",
    )
    swc_path = model_root / "swc" / "body" / "BareSwc.yaml"
    swc_path.parent.mkdir(parents=True, exist_ok=True)
    swc_path.write_text(
        yaml.safe_dump(
            {
                "name": "BareSwc",
                "type": "application",
                "ports": [{"name": "pBare", "direction": "provide", "interface": "IBare"}],
                "runnables": [{"name": "BareRunnable", "events": [{"type": "timing", "period": 0.01}]}],
                "data_type_mappings": ["ProbeBareMaps"],
                "handle_termination_and_restart": "no_support",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return model_root


def _bare_export(tmp_path: Path) -> tuple[Path, Path]:
    """``(model root, export)``: the project keeping the bare pair and its export of ``BareSwc``."""
    model_root = _bare_project(tmp_path / "bare")
    arxml = tmp_path / "BareSwc.arxml"
    export_component(str(model_root), "BareSwc", str(arxml))
    return model_root, arxml


class TestABarePairTheProjectKeepsReimportsUnchanged:
    def test_the_export_carries_the_pair_and_nothing_naming_the_application_member(self, tmp_path: Path) -> None:
        """The precondition: the file alone would collapse the pair."""
        _, arxml = _bare_export(tmp_path)
        text = arxml.read_text(encoding="utf-8")

        assert f"/DataTypes/Application/{BARE}" in text
        assert f"/DataTypes/Implementation/{BARE}" in text
        assert "ProbeBareRecord" not in text
        # ...and read alone, without the target's kept set, the file collapses the pair: so the
        # re-import below is unchanged through the kept set and through nothing else of the file.
        _types, registry, _skips = map_data_types(read_arxml(str(arxml)), elements=[], kept_pairs=frozenset())
        assert [(record.app_path, record.impl_path) for record in registry.reduction_records] == [
            (f"/DataTypes/Application/{BARE}", f"/DataTypes/Implementation/{BARE}")
        ]

    def test_the_reimport_into_the_same_project_is_unchanged_and_refuses_nothing(self, tmp_path: Path) -> None:
        model_root, arxml = _bare_export(tmp_path)
        files_before = files_of(model_root)

        plan = plan_component_import(str(arxml), str(model_root))

        assert plan.report.swc_action == "unchanged"
        assert plan.targets == ()
        assert sorted(plan.report.reused_data_types) == sorted([BARE, BARE, "ProbeBare_base"])
        assert plan.report.created_data_types == []
        assert plan.report.reused_mapping_sets == ["ProbeBareMaps"]
        assert plan.report.extended_mapping_sets == {}
        assert not [line for line in plan.report.not_carried_lines if BARE in line], plan.report.not_carried_lines
        assert files_of(model_root) == files_before

    def test_a_self_map_after_the_kept_pairs_row_is_refused_and_never_read_as_its_repeat(self, tmp_path: Path) -> None:
        """``Impl/X -> Impl/X`` after the kept ``App/X -> Impl/X`` is a self-map, not a repeat.

        Guards a duplicate-row check that compared bare names, not ``(layer, name)``, and accepted it.
        """
        model_root, arxml = _bare_export(tmp_path)
        own_row = "</DATA-TYPE-MAP>\n"
        text = arxml.read_text(encoding="utf-8")
        assert text.count(own_row) == 1
        implementation = f"/DataTypes/Implementation/{BARE}"
        self_map = (
            "</DATA-TYPE-MAP>\n"
            "                <DATA-TYPE-MAP>\n"
            '                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
            f"{implementation}</APPLICATION-DATA-TYPE-REF>\n"
            '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
            f"{implementation}</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "                </DATA-TYPE-MAP>\n"
        )
        arxml.write_text(text.replace(own_row, self_map), encoding="utf-8", newline="\n")
        files_before = files_of(model_root)

        assert target_bare_pairs(model_root) == frozenset({BARE})
        with pytest.raises(LossyComponentImportError) as refused:
            plan_component_import(str(arxml), str(model_root))

        assert [line for line in refused.value.skip_lines if "DATA-TYPE-MAP" in line] == [
            f"DATA-TYPE-MAP /DataTypes/MappingSets/ProbeBareMaps/DATA-TYPE-MAPS: both sides resolve to the data type "
            f"'{BARE}'"
        ]
        assert files_of(model_root) == files_before


class TestTheTargetScanKeepsOnlyABarePairBothLayersDeclare:
    def test_a_project_declaring_both_members_keeps_the_name(self, tmp_path: Path) -> None:
        assert target_bare_pairs(_bare_project(tmp_path)) == frozenset({BARE})

    def test_an_enum_application_member_contributes_no_name(self, tmp_path: Path) -> None:
        model_root = init_project(tmp_path / "proj") / "model"
        _write(model_root, _BARE_BASE)
        _write(model_root, _BARE_IMPLEMENTATION)
        _write(model_root, {"name": BARE, "kind": "enum", "literals": [{"name": "OFF", "value": 0}]})

        assert target_bare_pairs(model_root) == frozenset()

    def test_an_absent_application_member_contributes_no_name(self, tmp_path: Path) -> None:
        model_root = init_project(tmp_path / "proj") / "model"
        _write(model_root, _BARE_BASE)
        _write(model_root, _BARE_IMPLEMENTATION)

        assert target_bare_pairs(model_root) == frozenset()

    def test_an_unparseable_file_and_a_missing_directory_answer_no_name_and_never_raise(self, tmp_path: Path) -> None:
        model_root = _bare_project(tmp_path)
        (model_root / "data_types" / "application" / "Broken.yaml").write_text(
            "name: [unclosed\n", encoding="utf-8", newline="\n"
        )
        (model_root / "data_types" / "application" / "NotUtf8.yaml").write_bytes(b"name: \xff\xfe\n")

        assert target_bare_pairs(model_root) == frozenset({BARE})
        assert target_bare_pairs(tmp_path / "nowhere") == frozenset()

    @pytest.mark.parametrize(
        "text",
        ["", "- a\n", "just a scalar\n", "name: 5\nkind: application_type\n", "name: [a]\nkind: application_type\n"],
        ids=["empty", "top-level-list", "top-level-scalar", "integer-name", "list-name"],
    )
    def test_a_document_that_is_not_a_mapping_or_names_no_string_answers_no_name(
        self, tmp_path: Path, text: str
    ) -> None:
        """Parsed, but not an element: the scan skips it rather than raise before step 3a."""
        model_root = _bare_project(tmp_path)
        for layer in ("application", "implementation"):
            (model_root / "data_types" / layer / "Odd.yaml").write_text(text, encoding="utf-8", newline="\n")

        assert target_bare_pairs(model_root) == frozenset({BARE})

    def test_an_unlistable_layer_directory_answers_no_name_and_never_raises(self, tmp_path: Path) -> None:
        model_root = _bare_project(tmp_path)

        with read_denied(model_root / "data_types" / "application"):
            assert target_bare_pairs(model_root) == frozenset()

    def test_an_unreadable_file_contributes_no_name_and_the_others_still_count(self, tmp_path: Path) -> None:
        model_root = _bare_project(tmp_path)
        _write(model_root, {"name": "ProbeOther", "kind": "value_type", "base": "ProbeBare_base"})
        _write(model_root, {"name": "ProbeOther", "kind": "application_type"})
        assert target_bare_pairs(model_root) == frozenset({BARE, "ProbeOther"})

        with read_denied(model_element_path(model_root, BARE, layer="application_types")):
            assert target_bare_pairs(model_root) == frozenset({"ProbeOther"})


#: The map row the collapse folds, as ``import component`` prints it under *Not carried from the file*.
_FOLDED_BARE_ROW = (
    "DATA-TYPE-MAP /DataTypes/MappingSets/ProbeBareMaps/DATA-TYPE-MAPS: the map row of the same-name pair "
    f"'{BARE}' is folded into its collapse: both sides are the one implementation type '{BARE}'"
)


class TestABarePairIntoAnEmptyProjectCollapsesAndIsCreated:
    def test_the_import_is_created_with_the_one_implementation_type_and_the_folded_row(self, tmp_path: Path) -> None:
        _, arxml = _bare_export(tmp_path)
        model_root = init_project(tmp_path / "empty" / "proj") / "model"

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        assert _type_files(model_root) == [
            "data_types/base/ProbeBare_base.yaml",
            f"data_types/implementation/{BARE}.yaml",
        ]
        assert sorted(report.created_data_types) == sorted([BARE, "ProbeBare_base"])
        assert report.created_mapping_sets == ["ProbeBareMaps"]
        written = yaml.safe_load(
            model_element_path(model_root, "ProbeBareMaps", layer="mapping_sets").read_text(encoding="utf-8")
        )
        assert written == {"name": "ProbeBareMaps", "maps": []}
        assert _FOLDED_BARE_ROW in report.not_carried_lines
        assert [line for line in report.not_carried_lines if BARE in line] == [
            f"APPLICATION-PRIMITIVE-DATA-TYPE /DataTypes/Application/{BARE}: collapsed into its implementation twin, "
            f"written as '{BARE}' (the same-name pair): SWCraft writes the one implementation type",
            _FOLDED_BARE_ROW,
        ]

    def test_an_extra_self_map_row_beside_the_pairs_own_is_still_refused(self, tmp_path: Path) -> None:
        """Only the row that caused the collapse folds: ``App/X -> App/X`` is the file's own self-map."""
        _, arxml = _bare_export(tmp_path)
        application = f'<APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/{BARE}'
        own_row = "<DATA-TYPE-MAP>\n"
        text = arxml.read_text(encoding="utf-8")
        assert text.count(own_row) == 1
        extra_row = (
            "<DATA-TYPE-MAP>\n"
            f"                  {application}</APPLICATION-DATA-TYPE-REF>\n"
            '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
            f"/DataTypes/Application/{BARE}</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "                </DATA-TYPE-MAP>\n"
            "                "
        )
        arxml.write_text(text.replace(own_row, extra_row + own_row), encoding="utf-8", newline="\n")
        model_root = init_project(tmp_path / "empty" / "proj") / "model"

        with pytest.raises(LossyComponentImportError) as refused:
            plan_component_import(str(arxml), str(model_root))

        assert [line for line in refused.value.skip_lines if "DATA-TYPE-MAP" in line] == [
            "DATA-TYPE-MAP /DataTypes/MappingSets/ProbeBareMaps/DATA-TYPE-MAPS: both sides resolve to the data type "
            f"'{BARE}' (the same-name pair was collapsed)"
        ]


class TestATargetHoldingTheNameOtherwise:
    """A target that does not hold the bare pair as the export does.

    The scan reads the kind, never the physics: both members with a unit is a kept pair that conflicts;
    one member only keeps no name, so the pair collapses as ``import model`` would.
    """

    @pytest.mark.parametrize("update", [False, True], ids=["plain", "update"])
    def test_an_application_member_with_a_unit_is_a_divergent_shared_type(self, tmp_path: Path, update: bool) -> None:
        _, arxml = _bare_export(tmp_path)
        model_root = init_project(tmp_path / "target" / "proj") / "model"
        _write(model_root, _BARE_BASE)
        _write(model_root, _BARE_IMPLEMENTATION)
        _write(model_root, {"name": BARE, "kind": "application_type", "unit": "km/h"})
        files_before = files_of(model_root)

        assert target_bare_pairs(model_root) == frozenset({BARE})
        with pytest.raises(ComponentImportConflictError) as refused:
            import_component(str(arxml), str(model_root), update=update)

        assert f"shared data type '{BARE}' diverges" in str(refused.value)
        assert files_of(model_root) == files_before

    def test_only_the_implementation_member_collapses_the_pair_onto_it_and_reuses_it(self, tmp_path: Path) -> None:
        _, arxml = _bare_export(tmp_path)
        model_root = init_project(tmp_path / "target" / "proj") / "model"
        _write(model_root, _BARE_BASE)
        implementation_file = _write(model_root, _BARE_IMPLEMENTATION)
        implementation_before = implementation_file.read_bytes()

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        assert report.created_data_types == []
        assert sorted(report.reused_data_types) == sorted([BARE, "ProbeBare_base"])
        assert implementation_file.read_bytes() == implementation_before
        assert _type_files(model_root) == [
            "data_types/base/ProbeBare_base.yaml",
            f"data_types/implementation/{BARE}.yaml",
        ]
        assert _FOLDED_BARE_ROW in report.not_carried_lines
        self._assert_consistent(model_root)

    def test_only_the_application_member_collapses_the_pair_and_leaves_that_member_as_it_was(
        self, tmp_path: Path
    ) -> None:
        _, arxml = _bare_export(tmp_path)
        model_root = init_project(tmp_path / "target" / "proj") / "model"
        application_file = _write(model_root, _BARE_APPLICATION)
        application_before = application_file.read_bytes()

        report = import_component(str(arxml), str(model_root))

        assert report.swc_action == "created"
        assert sorted(report.created_data_types) == sorted([BARE, "ProbeBare_base"])
        assert report.reused_data_types == []
        assert application_file.read_bytes() == application_before
        assert str(application_file) not in report.written_paths
        assert _type_files(model_root) == [
            f"data_types/application/{BARE}.yaml",
            "data_types/base/ProbeBare_base.yaml",
            f"data_types/implementation/{BARE}.yaml",
        ]
        assert _FOLDED_BARE_ROW in report.not_carried_lines
        self._assert_consistent(model_root)

    @staticmethod
    def _assert_consistent(model_root: Path) -> None:
        result = check_cross_layer_consistency(str(model_root))
        assert [
            record.rule for record in result.records if record.rule.startswith(("data-type.", "mapping-set."))
        ] == []
