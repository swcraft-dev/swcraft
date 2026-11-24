"""Component exchange of the exclusive areas: they survive the round trip.

``swcraft generate arxml --component`` writes a component's exclusive areas and its
runnables' ``can_enter`` and ``runs_inside``, refuses a reference naming no area, and
writes the capability marker ``SD GID="EXCLUSIVE-AREAS"`` into the behaviour's
``SWCRAFT`` group. ``swcraft import component`` reads them back through ``map_swcs`` and
decides, per component, which of three regimes governs an ``--update``:

* **marked**: the file is the truth for the areas and every runnable's two lists,
  absence included; they are compared, and ``--update`` replaces them;
* **stated**: no marker, but the file states an area or a reference (a third-party
  export): it is the truth for all of them, so a runnable it gives no reference keeps none;
* **carried**: no marker and nothing stated (an older SWCraft export): both sides leave
  the areas out of the comparison and ``--update`` keeps the project's own, each
  runnable's lists by runnable name.

A marker value the writer does not mint is refused. The subject is
``exclusive_area_support``'s portless component with three areas, exported by the real
exporter and edited as text where a test needs another shape.

What this module does **not** reach: a file an older SWCraft wrote itself (the unmarked
file is this export with the marker removed, not its every byte); the real tool exports
(``test-internal``); the MCP surface's rendering of the same :class:`ComponentImportReport`.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from exclusive_area_support import (
    CALIBRATE,
    COMPONENT,
    COMPONENT_FILE,
    FILTER,
    MONITOR,
    OFFSET_TABLE,
    SPARE_AREA,
    SPEED_STATE,
    area_model,
)
from external_stubs_support import change
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.capability_markers import EXCLUSIVE_AREAS, INTER_RUNNABLE_VARIABLES
from swcraft_tools.component_exchange import (
    ClosureValidationError,
    ComponentImportConflictError,
    InvalidComponentArxmlError,
    LossyComponentImportError,
    ReservedNameError,
    export_component,
    import_component,
)
from swcraft_tools.component_exchange.capabilities import CARRIED, MARKED, NONE, STATED
from swcraft_tools.component_exchange.name_policies import illegal_identifiers
from swcraft_tools.component_exchange.resulting_behaviour import _behaviour_findings
from swcraft_tools.data_description import read_data_description
from swcraft_tools.differ import diff_model_states, diff_snapshots, format_diff
from swcraft_tools.model_state import ModelState
from swcraft_tools.reader import read_mapping_sets, read_swc_model
from swcraft_tools.schema import CanEnterArea, ExclusiveArea, Runnable, RunsInsideArea, SWComponent
from swcraft_tools.snapshot import load_snapshot
from swcraft_tools.validator import check_cross_layer_consistency, exclusive_area_records
from swcraft_tools.writer.arxml_generation import (
    generate_arxml,
)

MARKER_LINE = re.compile(r'\n *<SD GID="EXCLUSIVE-AREAS">[^<]*</SD>')


# Helpers


def _export(model_root: Path, arxml: Path) -> Path:
    export_component(str(model_root), COMPONENT, str(arxml))
    return arxml


def _component(model_root: Path) -> SWComponent:
    (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == COMPONENT]
    return swc


def _areas(swc: SWComponent) -> list[ExclusiveArea]:
    return list(swc.exclusive_areas)


def _references(swc: SWComponent) -> dict[str, tuple[list[str], list[str]]]:
    return {r.name: ([e.area for e in r.can_enter], [e.area for e in r.runs_inside]) for r in swc.runnables}


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _without_areas(model_root: Path) -> None:
    """Drop every area and every area reference from the component, in place."""

    def strip(document: dict[str, Any]) -> None:
        document.pop("exclusive_areas", None)
        for runnable in document["runnables"]:
            runnable.pop("can_enter", None)
            runnable.pop("runs_inside", None)

    change(model_root, COMPONENT_FILE, strip)


def _set_period(model_root: Path, runnable: str, period: float) -> None:
    def edit(document: dict[str, Any]) -> None:
        (entry,) = [r for r in document["runnables"] if r["name"] == runnable]
        entry["events"] = [{"type": "timing", "period": period}]

    change(model_root, COMPONENT_FILE, edit)


def _unmark(arxml: Path) -> Path:
    """Remove the exclusive areas marker, which older exports lack."""
    text = arxml.read_text(encoding="utf-8")
    assert len(MARKER_LINE.findall(text)) == 1
    arxml.write_text(MARKER_LINE.sub("", text), encoding="utf-8", newline="\n")
    assert f'<SD GID="{EXCLUSIVE_AREAS.sd_gid}">' not in arxml.read_text(encoding="utf-8")
    return arxml


def _replace_text(arxml: Path, old: str, new: str) -> Path:
    text = arxml.read_text(encoding="utf-8")
    assert text.count(old) == 1, (old, text.count(old))
    arxml.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return arxml


def _copy_export(tmp_path: Path, source: Path, name: str, *edits: Any, marked: bool = True) -> Path:
    """An export of a copy of *source*, each of *edits* applied to the copy first, unmarked unless *marked*."""
    copy = tmp_path / f"{name}-model"
    shutil.copytree(source, copy)
    for edit in edits:
        edit(copy)
    arxml = _export(copy, tmp_path / f"{name}.arxml")
    return arxml if marked else _unmark(arxml)


@pytest.fixture
def exchanged(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The fixture model, its marked export, and a project holding the component imported from it.

    Returns ``(source model root, marked export, target model root)``.
    """
    source = area_model(tmp_path / "source")
    arxml = _export(source, tmp_path / "marked.arxml")
    target = init_project(tmp_path / "target") / "model"
    assert import_component(str(arxml), str(target)).swc_action == "created"
    return source, arxml, target


# The closure


class TestTheClosureCheckJudgesTheReferences:
    def test_the_export_writes_every_area_and_reference(self, tmp_path: Path) -> None:
        text = _export(area_model(tmp_path / "source"), tmp_path / "c.arxml").read_text(encoding="utf-8")
        for name in (SPEED_STATE, OFFSET_TABLE, SPARE_AREA):
            assert f"<SHORT-NAME>{name}</SHORT-NAME>" in text
        assert text.count('<RUNS-INSIDE-EXCLUSIVE-AREA-REF DEST="EXCLUSIVE-AREA">') == 2
        assert text.count('<CAN-ENTER-EXCLUSIVE-AREA-REF DEST="EXCLUSIVE-AREA">') == 1

    def test_a_reference_to_no_area_is_the_closure_check_s_refusal(self, tmp_path: Path) -> None:
        source = area_model(tmp_path / "source")

        def dangling(document: dict[str, Any]) -> None:
            (calibrate,) = [r for r in document["runnables"] if r["name"] == CALIBRATE]
            calibrate["runs_inside"] = [{"area": "Missing"}]

        change(source, COMPONENT_FILE, dangling)
        with pytest.raises(ClosureValidationError) as refusal:
            _export(source, tmp_path / "component.arxml")
        assert any("declares no exclusive area 'Missing'" in issue for issue in refusal.value.issues)
        assert not (tmp_path / "component.arxml").exists()


# The marker


class TestTheExportCarriesTheMarker:
    def test_every_component_export_carries_it_fifth_and_the_whole_model_does_not(self, tmp_path: Path) -> None:
        source = area_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "component.arxml")
        component = arxml.read_text(encoding="utf-8")
        assert f'<SD GID="{EXCLUSIVE_AREAS.sd_gid}">{EXCLUSIVE_AREAS.text}</SD>' in component
        assert component.index(f'<SD GID="{INTER_RUNNABLE_VARIABLES.sd_gid}">') < component.index(
            f'<SD GID="{EXCLUSIVE_AREAS.sd_gid}">'
        ), "after the inter-runnable variables marker"
        (group,) = [
            node
            for node in etree.parse(str(arxml)).iter()
            if etree.QName(node).localname == "SDG" and node.get("GID") == "SWCRAFT"
        ]
        # The later capability markers follow it.
        assert [child.get("GID") for child in group][-4:] == [
            EXCLUSIVE_AREAS.sd_gid,
            "DATA-EVENTS",
            "IMPLICIT-ACCESS",
            "ASYNCHRONOUS-CALLS",
        ], "the fifth of the component export's markers"
        whole = tmp_path / "whole.arxml"
        generate_arxml(str(source), str(whole))
        assert f'<SD GID="{EXCLUSIVE_AREAS.sd_gid}">' not in whole.read_text(encoding="utf-8")

    def test_a_component_with_no_area_carries_it_too(self, tmp_path: Path) -> None:
        """A statement about the exporter: a marked file with no area says *none*."""
        source = area_model(tmp_path / "source")
        _without_areas(source)
        text = _export(source, tmp_path / "component.arxml").read_text(encoding="utf-8")
        assert "<EXCLUSIVE-AREAS>" not in text
        assert f'<SD GID="{EXCLUSIVE_AREAS.sd_gid}">' in text

    def test_the_reader_admits_the_group_and_reads_the_marker(self, tmp_path: Path) -> None:
        model = read_arxml(str(_export(area_model(tmp_path / "source"), tmp_path / "component.arxml")))
        assert model.swcs[0].swcraft_markers[EXCLUSIVE_AREAS.sd_gid] == EXCLUSIVE_AREAS.text
        assert not [entry for entry in model.skip_log if "ADMIN-DATA" in entry.reason]

    def test_the_marker_is_a_capability_token_never_a_version(self) -> None:
        """One copy in the leaf ``capability_markers``, which the writer mints from and the reader admits."""
        assert EXCLUSIVE_AREAS.sd_gid == "EXCLUSIVE-AREAS"
        assert not re.search(r"\d", EXCLUSIVE_AREAS.text), "a capability, never a version"

    def test_a_value_the_writer_does_not_mint_is_refused_with_nothing_written(self, exchanged) -> None:
        _source, arxml, target = exchanged
        _replace_text(arxml, f">{EXCLUSIVE_AREAS.text}<", ">something else<")
        before = _tree(target)
        with pytest.raises(InvalidComponentArxmlError) as refusal:
            import_component(str(arxml), str(target), update=True)
        assert "EXCLUSIVE-AREAS marker with the value 'something else'" in str(refusal.value)
        assert _tree(target) == before


# The round trip


class TestTheAreasSurviveTheRoundTrip:
    def test_the_component_exports_and_reimports_unchanged(self, exchanged) -> None:
        source, arxml, target = exchanged
        assert _areas(_component(target)) == _areas(_component(source))
        assert _references(_component(target)) == _references(_component(source))
        before = _tree(target)
        for update in (False, True):
            report = import_component(str(arxml), str(target), update=update)
            assert report.swc_action == "unchanged"
            assert report.exclusive_area_regime == NONE
            assert "WRITTEN" not in report.render()
        assert _tree(target) == before

    def test_the_created_component_s_report_counts_what_it_wrote(self, tmp_path: Path) -> None:
        arxml = _export(area_model(tmp_path / "source"), tmp_path / "c.arxml")
        report = import_component(str(arxml), str(init_project(tmp_path / "target") / "model"))
        assert report.written_exclusive_areas == (3, 3)
        assert "WRITTEN 3 exclusive areas and 3 exclusive area references" in report.render()

    def test_the_imported_project_checks_clean(self, exchanged) -> None:
        _source, _arxml, target = exchanged
        result = check_cross_layer_consistency(str(target))
        assert result.consistent, result.issues

    def test_the_import_s_own_export_is_unchanged_too(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        again = _export(target, tmp_path / "again.arxml")
        assert import_component(str(again), str(target)).swc_action == "unchanged"

    def test_a_foreign_export_carrying_areas_imports_them(self, tmp_path: Path) -> None:
        """A foreign export carrying areas imports them.

        The behaviour's whole ``ADMIN-DATA`` is removed, so nothing vouches for SWCraft.
        """
        arxml = _export(area_model(tmp_path / "source"), tmp_path / "c.arxml")
        tree = etree.parse(str(arxml))
        (behaviour,) = [node for node in tree.iter() if etree.QName(node).localname == "SWC-INTERNAL-BEHAVIOR"]
        (admin,) = [node for node in behaviour if etree.QName(node).localname == "ADMIN-DATA"]
        behaviour.remove(admin)
        foreign = tmp_path / "foreign.arxml"
        tree.write(str(foreign), xml_declaration=True, encoding="UTF-8")
        target = init_project(tmp_path / "target") / "model"

        report = import_component(str(foreign), str(target))

        assert report.swc_action == "created"
        assert [area.name for area in _component(target).exclusive_areas] == [SPEED_STATE, OFFSET_TABLE, SPARE_AREA]
        assert _references(_component(target))[CALIBRATE] == ([OFFSET_TABLE], [])
        assert check_cross_layer_consistency(str(target)).consistent


# The three regimes


class TestAnUnmarkedFileStatingNoneCarriesTheProjectsAreas:
    def test_an_otherwise_identical_unmarked_file_is_unchanged(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        older = _copy_export(tmp_path, source, "older", _without_areas, marked=False)
        before = _tree(target)
        assert import_component(str(older), str(target)).swc_action == "unchanged"
        assert import_component(str(older), str(target), update=True).swc_action == "unchanged"
        assert _tree(target) == before

    def test_an_update_keeps_the_areas_and_the_references(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        older = _copy_export(
            tmp_path, source, "older", _without_areas, lambda copy: _set_period(copy, FILTER, 0.02), marked=False
        )
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(older), str(target))
        assert "exclusive areas and the runnables' can_enter and runs_inside are not compared either" in str(
            conflict.value
        )
        expected_areas = _areas(_component(target))
        expected_references = _references(_component(target))

        report = import_component(str(older), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.exclusive_area_regime == CARRIED
        kept = _component(target)
        assert _areas(kept) == expected_areas, "the target's, in its order, descriptions included"
        assert _references(kept) == expected_references
        assert next(r for r in kept.runnables if r.name == FILTER).events[0].period == 0.02
        assert report.preserved_exclusive_areas == [SPEED_STATE, OFFSET_TABLE, SPARE_AREA]
        assert report.preserved_exclusive_area_references == {
            FILTER: {"runs_inside": 1},
            MONITOR: {"runs_inside": 1},
            CALIBRATE: {"can_enter": 1},
        }
        assert report.written_exclusive_areas == (3, 3)
        rendered = report.render()
        assert "EXCLUSIVE AREAS regime: carried -- the export carries no SWCraft exclusive areas marker" in rendered
        assert (
            "EXCLUSIVE AREAS preserved from the target project (not carried by the source ARXML): "
            "SpeedState, OffsetTable, SpareArea" in rendered
        )
        assert f"  - {CALIBRATE}: can_enter=1" in rendered
        assert exclusive_area_records([kept]) == [], "the carried result names no removed area"
        assert check_cross_layer_consistency(str(target)).consistent

    def test_a_runnable_the_file_removed_takes_its_references_with_it(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged

        def drop_calibrate(copy: Path) -> None:
            change(
                copy,
                COMPONENT_FILE,
                lambda document: document.update(
                    runnables=[r for r in document["runnables"] if r["name"] != CALIBRATE]
                ),
            )

        older = _copy_export(tmp_path, source, "older", _without_areas, drop_calibrate, marked=False)
        report = import_component(str(older), str(target), update=True)

        kept = _component(target)
        assert CALIBRATE not in _references(kept)
        assert CALIBRATE not in report.preserved_exclusive_area_references
        assert [area.name for area in kept.exclusive_areas] == [SPEED_STATE, OFFSET_TABLE, SPARE_AREA]
        assert exclusive_area_records([kept]) == []

    def test_a_target_without_areas_carries_nothing_and_reads_as_before(self, tmp_path: Path) -> None:
        """The carried regime over a component that declares no area: nothing preserved, no note."""
        source = area_model(tmp_path / "source")
        _without_areas(source)
        target = init_project(tmp_path / "target") / "model"
        assert import_component(str(_export(source, tmp_path / "marked.arxml")), str(target)).swc_action == "created"
        older = _copy_export(tmp_path, source, "older", lambda copy: _set_period(copy, FILTER, 0.02), marked=False)
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(older), str(target))
        assert "exclusive areas" not in str(conflict.value)

        report = import_component(str(older), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.exclusive_area_regime == CARRIED
        assert (report.preserved_exclusive_areas, report.preserved_exclusive_area_references) == ([], {})
        assert report.written_exclusive_areas == (0, 0)
        kept = _component(target)
        assert kept.exclusive_areas == []
        assert all(not entered and not inside for entered, inside in _references(kept).values())
        rendered = report.render()
        assert "EXCLUSIVE AREAS preserved" not in rendered
        assert "EXCLUSIVE AREAS regime: carried -- " in rendered


class TestAnUnmarkedFileStatingAreasIsTheTruth:
    def test_a_changed_description_is_compared_and_updated(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged

        def describe(copy: Path) -> None:
            def edit(document: dict[str, Any]) -> None:
                (entry,) = [a for a in document["exclusive_areas"] if a["name"] == OFFSET_TABLE]
                entry["description"] = "Guards the calibration offsets."

            change(copy, COMPONENT_FILE, edit)

        third_party = _copy_export(tmp_path, source, "third", describe, marked=False)
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(third_party), str(target))
        assert "not compared either" not in str(conflict.value)

        report = import_component(str(third_party), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.exclusive_area_regime == STATED
        assert report.preserved_exclusive_areas == []
        (offsets,) = [a for a in _component(target).exclusive_areas if a.name == OFFSET_TABLE]
        assert offsets.description == "Guards the calibration offsets."
        assert "EXCLUSIVE AREAS regime: stated -- " in report.render()

    def test_an_empty_area_description_is_compared_as_written(self, exchanged, tmp_path: Path) -> None:
        """Unlike the component's, a port's or a runnable's, an empty area description keeps no target text."""
        source, _arxml, target = exchanged

        def undescribe(copy: Path) -> None:
            def edit(document: dict[str, Any]) -> None:
                (entry,) = [a for a in document["exclusive_areas"] if a["name"] == SPEED_STATE]
                entry.pop("description")

            change(copy, COMPONENT_FILE, edit)

        third_party = _copy_export(tmp_path, source, "third", undescribe, marked=False)
        with pytest.raises(ComponentImportConflictError):
            import_component(str(third_party), str(target))

        report = import_component(str(third_party), str(target), update=True)

        assert (report.swc_action, report.exclusive_area_regime) == ("updated", STATED)
        (speed_state,) = [a for a in _component(target).exclusive_areas if a.name == SPEED_STATE]
        assert speed_state.description == ""

    def test_a_runnable_the_file_gives_no_reference_keeps_none(self, exchanged, tmp_path: Path) -> None:
        """Per component: a file that states areas states every reference, so nothing is carried per runnable."""
        source, _arxml, target = exchanged

        def unguard_monitor(copy: Path) -> None:
            def edit(document: dict[str, Any]) -> None:
                (monitor,) = [r for r in document["runnables"] if r["name"] == MONITOR]
                monitor.pop("runs_inside")

            change(copy, COMPONENT_FILE, edit)

        third_party = _copy_export(tmp_path, source, "third", unguard_monitor, marked=False)
        report = import_component(str(third_party), str(target), update=True)

        assert report.exclusive_area_regime == STATED
        assert report.preserved_exclusive_area_references == {}
        assert _references(_component(target)) == {
            FILTER: ([], [SPEED_STATE]),
            MONITOR: ([], []),
            CALIBRATE: ([OFFSET_TABLE], []),
        }

    def test_an_area_the_file_omits_is_removed(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged

        def drop_spare(copy: Path) -> None:
            change(
                copy,
                COMPONENT_FILE,
                lambda document: document.update(
                    exclusive_areas=[a for a in document["exclusive_areas"] if a["name"] != SPARE_AREA]
                ),
            )

        third_party = _copy_export(tmp_path, source, "third", drop_spare, marked=False)
        import_component(str(third_party), str(target), update=True)
        assert [area.name for area in _component(target).exclusive_areas] == [SPEED_STATE, OFFSET_TABLE]


class TestAMarkedFileReplacesTheAreas:
    def test_a_removed_area_and_reference_are_removed(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged

        def drop(document: dict[str, Any]) -> None:
            document["exclusive_areas"] = [a for a in document["exclusive_areas"] if a["name"] != OFFSET_TABLE]
            (calibrate,) = [r for r in document["runnables"] if r["name"] == CALIBRATE]
            calibrate.pop("can_enter")

        newer = _copy_export(tmp_path, source, "newer", lambda copy: change(copy, COMPONENT_FILE, drop))
        with pytest.raises(ComponentImportConflictError):
            import_component(str(newer), str(target))

        report = import_component(str(newer), str(target), update=True)

        assert report.exclusive_area_regime == MARKED
        kept = _component(target)
        assert [area.name for area in kept.exclusive_areas] == [SPEED_STATE, SPARE_AREA]
        assert _references(kept)[CALIBRATE] == ([], [])
        assert report.preserved_exclusive_areas == []
        assert "EXCLUSIVE AREAS regime: marked -- " in report.render()
        assert check_cross_layer_consistency(str(target)).consistent

    def test_a_marked_file_stating_none_removes_them_all(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        newer = _copy_export(tmp_path, source, "newer", _without_areas)
        with pytest.raises(ComponentImportConflictError):
            import_component(str(newer), str(target))
        report = import_component(str(newer), str(target), update=True)
        assert report.exclusive_area_regime == MARKED
        assert report.written_exclusive_areas == (0, 0)
        kept = _component(target)
        assert kept.exclusive_areas == []
        assert all(not entered and not inside for entered, inside in _references(kept).values())


# A withheld behaviour


def _with_an_unanswered_mapping_set(arxml: Path) -> Path:
    """*arxml* with a ``DATA-TYPE-MAPPING-REF`` onto a set neither the file nor the target declares."""
    return _replace_text(
        arxml,
        "<EXCLUSIVE-AREAS>",
        '<DATA-TYPE-MAPPING-REFS><DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Maps'
        "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS><EXCLUSIVE-AREAS>",
    )


class TestAWithheldBehaviourKeepsTheAreasWithTheRunnables:
    def test_an_update_keeps_the_project_s_areas_and_runnables(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        expected = _component(target)

        def describe(document: dict[str, Any]) -> None:
            document["description"] = "A new description."
            document["exclusive_areas"] = [{"name": "OtherArea"}]
            for runnable in document["runnables"]:
                runnable.pop("can_enter", None)
                runnable.pop("runs_inside", None)

        newer = _with_an_unanswered_mapping_set(
            _copy_export(tmp_path, source, "newer", lambda copy: change(copy, COMPONENT_FILE, describe))
        )

        report = import_component(str(newer), str(target), update=True)

        assert report.behaviour_kept
        assert report.swc_action == "updated"
        assert report.exclusive_area_regime == NONE
        kept = _component(target)
        assert kept.description == "A new description."
        assert kept.exclusive_areas == expected.exclusive_areas
        assert kept.runnables == expected.runnables

    def test_a_withheld_create_writes_neither_runnables_nor_areas(self, tmp_path: Path) -> None:
        arxml = _with_an_unanswered_mapping_set(_export(area_model(tmp_path / "source"), tmp_path / "c.arxml"))
        target = init_project(tmp_path / "target") / "model"

        report = import_component(str(arxml), str(target))

        assert report.swc_action == "created"
        assert report.missing_references
        written = _component(target)
        assert written.runnables == []
        assert written.exclusive_areas == []
        assert report.written_exclusive_areas == (0, 0)

    def test_the_kept_behaviour_is_judged_by_the_area_records_too(self, tmp_path: Path) -> None:
        """An overlay whose kept reference names an area that is gone is a break."""
        source = area_model(tmp_path / "source")
        swc = _component(source)
        description = read_data_description(str(source))
        sets = read_mapping_sets(str(source))
        assert _behaviour_findings(swc, [], description, sets) == []

        dangling = swc.model_copy(
            update={"exclusive_areas": [a for a in swc.exclusive_areas if a.name != OFFSET_TABLE]}
        )
        (finding,) = _behaviour_findings(dangling, [], description, sets)
        assert f"SWC '{COMPONENT}' declares no exclusive area '{OFFSET_TABLE}'" in finding


# The identifier guard, and a skip the import cannot carry


class TestTheIdentifierGuardJudgesTheAreas:
    @pytest.mark.parametrize(
        ("name", "why"),
        [("Bad Area", "AUTOSAR Identifier matching"), ("A" * 129, "at most 128 characters")],
        ids=["pattern", "length"],
    )
    def test_an_area_name_that_is_no_identifier_aborts_with_nothing_written(
        self, tmp_path: Path, name: str, why: str
    ) -> None:
        arxml = _replace_text(
            _export(area_model(tmp_path / "source"), tmp_path / "bad.arxml"),
            f"<SHORT-NAME>{SPARE_AREA}</SHORT-NAME>",
            f"<SHORT-NAME>{name}</SHORT-NAME>",
        )
        target = init_project(tmp_path / "target") / "model"
        before = _tree(target)
        with pytest.raises(ReservedNameError) as aborted:
            import_component(str(arxml), str(target))
        assert f"SWC '{COMPONENT}' exclusive area name '{name}' is not a legal AUTOSAR Identifier: " in str(
            aborted.value
        )
        assert why in str(aborted.value)
        assert _tree(target) == before

    def test_a_reference_entry_is_asked_nothing(self) -> None:
        """An entry writes no SHORT-NAME of its own; a dangling one is ``unknown-area``'s, not this guard's."""
        swc = SWComponent(
            name=COMPONENT,
            type="application",
            exclusive_areas=[ExclusiveArea(name="Guard")],
            runnables=[Runnable(name="Run", runs_inside=[RunsInsideArea(area="Guard")])],
        )
        assert illegal_identifiers(swc, [], []) == []


class TestANonDeferredSkipAbortsTheImport:
    def test_a_reference_of_another_dest_aborts_with_its_reason_and_nothing_written(self, tmp_path: Path) -> None:
        arxml = _export(area_model(tmp_path / "source"), tmp_path / "component.arxml")
        _replace_text(
            arxml,
            '<CAN-ENTER-EXCLUSIVE-AREA-REF DEST="EXCLUSIVE-AREA">',
            '<CAN-ENTER-EXCLUSIVE-AREA-REF DEST="RUNNABLE-ENTITY">',
        )
        target = init_project(tmp_path / "target") / "model"
        before = _tree(target)

        with pytest.raises(LossyComponentImportError) as refusal:
            import_component(str(arxml), str(target))

        assert "CAN-ENTER-EXCLUSIVE-AREA-REF" in str(refusal.value)
        assert _tree(target) == before


class TestTheAreasAreComparedInDeclaredOrder:
    """Areas and reference lists compare in declared order: a reordered file differs, ``--update`` writes its order."""

    @staticmethod
    def _reverse_areas(copy: Path) -> None:
        change(
            copy, COMPONENT_FILE, lambda document: document.update(exclusive_areas=document["exclusive_areas"][::-1])
        )

    @pytest.mark.parametrize(
        ("marked", "regime"),
        [(True, MARKED), (False, STATED)],
        ids=["marked", "stated"],
    )
    def test_reordered_areas_differ_and_update_to_the_file_s_order(
        self, exchanged, tmp_path: Path, marked: bool, regime: str
    ) -> None:
        source, _arxml, target = exchanged
        reordered = _copy_export(tmp_path, source, "reordered", self._reverse_areas, marked=marked)
        with pytest.raises(ComponentImportConflictError):
            import_component(str(reordered), str(target))

        report = import_component(str(reordered), str(target), update=True)

        assert (report.swc_action, report.exclusive_area_regime) == ("updated", regime)
        assert [area.name for area in _component(target).exclusive_areas] == [SPARE_AREA, OFFSET_TABLE, SPEED_STATE]

    @pytest.mark.parametrize("marked", [True, False], ids=["marked", "stated"])
    def test_a_reordered_reference_list_differs_and_updates_to_the_file_s_order(
        self, tmp_path: Path, marked: bool
    ) -> None:
        def guard(order: list[str]) -> Any:
            def edit(copy: Path) -> None:
                def runs_inside(document: dict[str, Any]) -> None:
                    (entry,) = [r for r in document["runnables"] if r["name"] == FILTER]
                    entry["runs_inside"] = [{"area": area} for area in order]

                change(copy, COMPONENT_FILE, runs_inside)

            return edit

        source = area_model(tmp_path / "source")
        guard([SPEED_STATE, OFFSET_TABLE])(source)
        target = init_project(tmp_path / "target") / "model"
        assert import_component(str(_export(source, tmp_path / "marked.arxml")), str(target)).swc_action == "created"
        reordered = _copy_export(tmp_path, source, "reordered", guard([OFFSET_TABLE, SPEED_STATE]), marked=marked)
        with pytest.raises(ComponentImportConflictError):
            import_component(str(reordered), str(target))

        report = import_component(str(reordered), str(target), update=True)

        assert report.swc_action == "updated"
        assert _references(_component(target))[FILTER] == ([], [OFFSET_TABLE, SPEED_STATE])


# The differ


_GUARD = ExclusiveArea(name="Guard")
_TABLE = ExclusiveArea(name="Table", description="Guards the table.")


def _state(
    areas: list[ExclusiveArea], can_enter: list[CanEnterArea], runs_inside: list[RunsInsideArea] | None = None
) -> ModelState:
    runnable = Runnable(name=FILTER, can_enter=can_enter, runs_inside=runs_inside or [])
    return ModelState(layers={"swc": [SWComponent(name=COMPONENT, exclusive_areas=areas, runnables=[runnable])]})


def _collection(old: ModelState, new: ModelState, field: str):
    (swc,) = diff_model_states(old, new)["swc"].changed
    if field == "exclusive_areas":
        (entry,) = [entry for entry in swc.collection_changes if entry.field == field]
        return entry
    (runnables,) = [entry for entry in swc.collection_changes if entry.field == "runnables"]
    (runnable,) = runnables.changed
    (entry,) = [entry for entry in runnable.collection_changes if entry.field == field]
    return entry


class TestTheDifferReportsAreasByIdentity:
    """``swcraft diff`` reads the contract's identity through ``diff_model_states``, with no code of its own.

    The identity is ``name`` on an area and ``area`` on an entry.
    """

    def test_an_added_and_a_removed_area(self) -> None:
        entry = _collection(_state([_GUARD], []), _state([_TABLE], []), "exclusive_areas")
        assert [item["name"] for item in entry.added] == ["Table"]
        assert [item["name"] for item in entry.removed] == ["Guard"]
        assert entry.changed == []

    def test_a_changed_area(self) -> None:
        entry = _collection(
            _state([_TABLE], []),
            _state([_TABLE.model_copy(update={"description": "Guards the offsets."})], []),
            "exclusive_areas",
        )
        (changed,) = entry.changed
        assert (changed.key, changed.changed_fields) == (
            "Table",
            [("description", "Guards the table.", "Guards the offsets.")],
        )

    @pytest.mark.parametrize(("field", "kind"), [("can_enter", CanEnterArea), ("runs_inside", RunsInsideArea)])
    def test_an_added_and_a_removed_entry(self, field: str, kind: type) -> None:
        """An entry holds its identity alone, so a retarget is a removal and an addition."""
        areas = [_GUARD, _TABLE]

        def state(area: str) -> ModelState:
            entries = [kind(area=area)]
            return _state(areas, entries, None) if field == "can_enter" else _state(areas, [], entries)

        entry = _collection(state("Guard"), state("Table"), field)
        assert ([item["area"] for item in entry.added], [item["area"] for item in entry.removed]) == (
            ["Table"],
            ["Guard"],
        )
        assert entry.changed == []

    def test_two_models_on_disk_through_the_command_s_loader(self, tmp_path: Path) -> None:
        """What ``swcraft diff`` runs -- ``load_snapshot``, ``diff_snapshots``, ``format_diff`` -- over two copies."""
        old = area_model(tmp_path / "old")
        new = area_model(tmp_path / "new")

        def edit(document: dict[str, Any]) -> None:
            document["exclusive_areas"] = [a for a in document["exclusive_areas"] if a["name"] != SPARE_AREA]
            document["exclusive_areas"].append({"name": "CalibrationState"})
            (calibrate,) = [r for r in document["runnables"] if r["name"] == CALIBRATE]
            calibrate["runs_inside"] = [{"area": "CalibrationState"}]

        change(new, COMPONENT_FILE, edit)

        diff = diff_snapshots(load_snapshot(str(old)), load_snapshot(str(new)))
        (swc,) = diff.swcs.changed
        (areas,) = [entry for entry in swc.collection_changes if entry.field == "exclusive_areas"]
        assert ([item["name"] for item in areas.added], [item["name"] for item in areas.removed]) == (
            ["CalibrationState"],
            [SPARE_AREA],
        )
        rendered = format_diff(diff)
        for name in ("CalibrationState", SPARE_AREA, "runs_inside"):
            assert name in rendered
