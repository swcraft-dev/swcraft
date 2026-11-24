"""The reader and the importer of exclusive areas.

The synthetic export is the writer's own output of ``exclusive_area_support``'s
fixture (one component, three areas, two runnables running inside one area and a
third that can enter another), changed case by case by an ElementTree edit, so every
case states one difference from a document the exporter wrote. Each case runs the whole
of ``import model`` (``run_import``) into a fresh project; the reader is asked directly
only where the case is its alone.

What this does **not** reach: the real tool exports (``test-internal``) and
``import component``'s regimes.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from exclusive_area_support import (
    CALIBRATE,
    COMPONENT,
    FILTER,
    MONITOR,
    OFFSET_TABLE,
    SPARE_AREA,
    SPEED_STATE,
    area_model,
    every_area_shape_model,
)
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.report import ImportReport
from swcraft_tools.arxml_reader import SkipCategory, read_arxml
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml

NS = "http://autosar.org/schema/r4.0"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", NS)
ET.register_namespace("xsi", XSI)

#: The behaviour the fixture's component is written with, and its areas' paths.
BEHAVIOUR_PATH = f"/ComponentTypes/{COMPONENT}/{COMPONENT}_InternalBehavior"
#: The two reference wrappers and their one item each, as the writer spells them.
CAN_ENTER = "CAN-ENTER-EXCLUSIVE-AREA-REFS"
RUNS_INSIDE = "RUNS-INSIDE-EXCLUSIVE-AREA-REFS"
CAN_ENTER_REF = "CAN-ENTER-EXCLUSIVE-AREA-REF"
RUNS_INSIDE_REF = "RUNS-INSIDE-EXCLUSIVE-AREA-REF"
#: The fixture's area descriptions, as committed.
SPEED_STATE_DESCRIPTION = "Guards the filtered speed shared by the 10 ms filter and the 100 ms monitor."


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _named(root: ET.Element, tag: str, name: str) -> ET.Element:
    """The one *tag* element of *root* whose ``SHORT-NAME`` is *name*."""
    found = [el for el in root.iter(_q(tag)) if el.findtext(_q("SHORT-NAME")) == name]
    assert len(found) == 1, f"expected one {tag} named {name!r}, found {len(found)}"
    return found[0]


def _parent(root: ET.Element, child: ET.Element) -> ET.Element:
    return next(el for el in root.iter() if child in list(el))


def _behaviour(root: ET.Element) -> ET.Element:
    return _named(root, "SWC-INTERNAL-BEHAVIOR", f"{COMPONENT}_InternalBehavior")


def _areas(root: ET.Element) -> ET.Element:
    wrapper = _behaviour(root).find(_q("EXCLUSIVE-AREAS"))
    assert wrapper is not None
    return wrapper


def _reference(root: ET.Element, runnable: str, wrapper: str) -> ET.Element:
    """The first reference of *runnable*'s *wrapper*."""
    element = _named(root, "RUNNABLE-ENTITY", runnable).find(f"{_q(wrapper)}/*")
    assert element is not None
    return element


def _export(
    tmp_path: Path,
    edit: Callable[[ET.Element], None] | None = None,
    *,
    model: Callable[[Path], Path] = area_model,
    version: str = "4.2.2",
) -> Path:
    """The writer's output of *model*, changed by *edit* when one is given."""
    model_root = model(tmp_path / "model")
    source = tmp_path / "source.arxml"
    generate_arxml(str(model_root), str(source), version=version)
    if edit is not None:
        tree = ET.parse(source)
        edit(tree.getroot())
        tree.write(source, encoding="utf-8", xml_declaration=True)
    return source


def _import(tmp_path: Path, source: Path, packages: list[str] | None = None) -> tuple[ImportReport, Path]:
    project = init_project(tmp_path / "project")
    report = run_import(str(source), str(project / "swcraft.yaml"), packages=packages)
    return report, project / "model"


def _component(model_root: Path, name: str = COMPONENT) -> dict[str, Any]:
    (path,) = [path for path in (model_root / "swc").rglob(f"{name}.yaml")]
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _runnable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (runnable,) = [runnable for runnable in document["runnables"] if runnable["name"] == name]
    return runnable


def _entries(report: ImportReport, tag: str | None = None) -> list[tuple[str, str, str]]:
    return [(entry.path, entry.tag, entry.reason) for entry in report.skip_entries if tag in (None, entry.tag)]


def _area_entries(report: ImportReport) -> list[tuple[str, str, str]]:
    """Every entry the areas and their references took (the component's implementation aside)."""
    return [entry for entry in _entries(report) if entry[1] != "SWC-IMPLEMENTATION"]


def _unread(container: str, parent_tag: str) -> str:
    """The closed tables' sentence for an unread child (``arxml_reader._unread_reason``)."""
    return (
        f"{container} of a {parent_tag} is not read by SWCraft -- no SWCraft representation, not imported or preserved"
    )


#: What the fixture's component holds of the construct, as committed.
COMMITTED_AREAS = [
    {"name": SPEED_STATE, "description": SPEED_STATE_DESCRIPTION},
    {"name": OFFSET_TABLE},
    {"name": SPARE_AREA},
]


# The writer's own output comes back as it was written


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
class TestTheWritersOwnOutputComesBack:
    def test_every_area_comes_back_with_its_description_in_declared_order(self, tmp_path: Path, version: str) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, version=version))

        assert _area_entries(report) == []
        assert _component(model_root)["exclusive_areas"] == COMMITTED_AREAS

    def test_each_role_comes_back_in_its_list(self, tmp_path: Path, version: str) -> None:
        _report, model_root = _import(tmp_path, _export(tmp_path, version=version))
        document = _component(model_root)

        assert _runnable(document, FILTER)["runs_inside"] == [{"area": SPEED_STATE}]
        assert _runnable(document, MONITOR)["runs_inside"] == [{"area": SPEED_STATE}]
        assert _runnable(document, CALIBRATE)["can_enter"] == [{"area": OFFSET_TABLE}]
        assert "can_enter" not in _runnable(document, FILTER)
        assert "runs_inside" not in _runnable(document, CALIBRATE)

    def test_the_report_counts_the_component_s_areas_and_references(self, tmp_path: Path, version: str) -> None:
        report, _model_root = _import(tmp_path, _export(tmp_path, version=version))

        assert report.imported_exclusive_areas == {COMPONENT: (3, 3)}
        rendered = report.render().splitlines()
        assert "IMPORTED 3 exclusive areas and 3 exclusive area references" in rendered
        assert f"  {COMPONENT}: 3 exclusive areas, 3 references" in rendered

    def test_the_imported_project_checks_clean_and_regenerates_the_areas_by_value(
        self, tmp_path: Path, version: str
    ) -> None:
        """The round trip: the import followed by ``generate arxml`` writes every area and reference back."""
        source = _export(tmp_path, version=version)
        _report, model_root = _import(tmp_path, source)
        assert check_cross_layer_consistency(str(model_root)).consistent
        again = tmp_path / "again.arxml"
        generate_arxml(str(model_root), str(again), version=version)

        def facts(path: Path) -> tuple[Any, ...]:
            swc = read_arxml(str(path)).swcs[0]
            return (
                [(area.name, area.description) for area in swc.exclusive_areas],
                [
                    (runnable.name, ref.role, ref.dest, ref.ref.rsplit("/", 1)[1])
                    for runnable in swc.runnables
                    for ref in runnable.area_refs
                ],
            )

        assert facts(again) == facts(source)


def test_the_every_shape_model_comes_back_in_declared_order(tmp_path: Path) -> None:
    """Two entries in one list against the alphabet, and one area in both roles on one runnable (silence)."""
    report, model_root = _import(tmp_path, _export(tmp_path, model=every_area_shape_model))
    document = _component(model_root)

    assert _area_entries(report) == []
    assert document["exclusive_areas"] == [
        {"name": SPEED_STATE, "description": "Guards the filtered speed."},
        {"name": OFFSET_TABLE},
    ]
    monitor = _runnable(document, "MonitorSpeed")
    assert monitor["can_enter"] == [{"area": OFFSET_TABLE}, {"area": SPEED_STATE}]
    assert monitor["runs_inside"] == [{"area": SPEED_STATE}]
    # The neighbours beside the references are mapped as they always were.
    assert monitor["reads"] == [{"port": "rSpeed", "element": "value"}]
    assert report.imported_exclusive_areas == {COMPONENT: (2, 4)}


# The reader


class TestTheReader:
    def test_the_areas_and_both_wrappers_are_read_and_report_nothing(self, tmp_path: Path) -> None:
        model = read_arxml(str(_export(tmp_path)))
        (swc,) = model.swcs

        assert [(area.path, area.name, area.description) for area in swc.exclusive_areas] == [
            (f"{BEHAVIOUR_PATH}/{SPEED_STATE}", SPEED_STATE, SPEED_STATE_DESCRIPTION),
            (f"{BEHAVIOUR_PATH}/{OFFSET_TABLE}", OFFSET_TABLE, ""),
            (f"{BEHAVIOUR_PATH}/{SPARE_AREA}", SPARE_AREA, ""),
        ]
        calibrate = next(r for r in swc.runnables if r.name == CALIBRATE)
        assert [(ref.path, ref.role, ref.tag, ref.ref, ref.dest) for ref in calibrate.area_refs] == [
            (
                f"{BEHAVIOUR_PATH}/{CALIBRATE}",
                CAN_ENTER,
                CAN_ENTER_REF,
                f"{BEHAVIOUR_PATH}/{OFFSET_TABLE}",
                "EXCLUSIVE-AREA",
            )
        ]
        assert [entry for entry in model.skip_log if entry.tag != "SWC-IMPLEMENTATION"] == []

    def test_empty_wrappers_write_and_report_nothing(self, tmp_path: Path) -> None:
        """``S-EMPTY-IN``: an empty wrapper holds nothing to read or to lose."""

        def edit(root: ET.Element) -> None:
            areas = _areas(root)
            for area in list(areas):
                areas.remove(area)
            for runnable in root.iter(_q("RUNNABLE-ENTITY")):
                for wrapper in (CAN_ENTER, RUNS_INSIDE):
                    for el in runnable.findall(_q(wrapper)):
                        for child in list(el):
                            el.remove(child)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _area_entries(report) == []
        document = _component(model_root)
        assert "exclusive_areas" not in document
        assert all("can_enter" not in r and "runs_inside" not in r for r in document["runnables"])
        assert report.imported_exclusive_areas == {}
        assert not any("exclusive area" in line for line in report.render().splitlines())

    def test_a_child_of_the_area_wrapper_that_is_no_area_is_the_selecting_container_s_line(
        self, tmp_path: Path
    ) -> None:
        def edit(root: ET.Element) -> None:
            stranger = ET.SubElement(_areas(root), _q("EXCLUSIVE-AREA-NESTING-ORDER"))
            ET.SubElement(stranger, _q("SHORT-NAME")).text = "Stranger"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _area_entries(report) == [
            (
                f"{BEHAVIOUR_PATH}/Stranger",
                "EXCLUSIVE-AREA-NESTING-ORDER",
                "unrecognized child inside EXCLUSIVE-AREAS (only EXCLUSIVE-AREA is read)",
            )
        ]
        assert _component(model_root)["exclusive_areas"] == COMMITTED_AREAS

    @pytest.mark.parametrize(
        ("runnable", "wrapper", "item"), [(CALIBRATE, CAN_ENTER, CAN_ENTER_REF), (FILTER, RUNS_INSIDE, RUNS_INSIDE_REF)]
    )
    def test_a_child_of_a_reference_wrapper_that_is_no_reference_is_its_own_line(
        self, tmp_path: Path, runnable: str, wrapper: str, item: str
    ) -> None:
        def edit(root: ET.Element) -> None:
            container = _named(root, "RUNNABLE-ENTITY", runnable).find(_q(wrapper))
            stranger = ET.SubElement(
                container, _q("EXCLUSIVE-AREA-NESTING-ORDER-REF"), DEST="EXCLUSIVE-AREA-NESTING-ORDER"
            )
            stranger.text = f"{BEHAVIOUR_PATH}/Order"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _area_entries(report) == [
            (
                f"{BEHAVIOUR_PATH}/{runnable}",
                "EXCLUSIVE-AREA-NESTING-ORDER-REF",
                f"unrecognized child inside {wrapper} (only {item} is read)",
            )
        ]
        # The reference the wrapper does hold is kept.
        role = "can_enter" if wrapper == CAN_ENTER else "runs_inside"
        assert len(_runnable(_component(model_root), runnable)[role]) == 1

    def test_a_category_of_an_area_is_the_closed_table_s_unread_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            area = _named(root, "EXCLUSIVE-AREA", OFFSET_TABLE)
            ET.SubElement(area, _q("CATEGORY")).text = "VENDOR"

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _area_entries(report) == [
            (f"{BEHAVIOUR_PATH}/{OFFSET_TABLE}", "CATEGORY", _unread("CATEGORY", "EXCLUSIVE-AREA"))
        ]

    def test_a_long_name_of_an_area_is_the_index_s_one_line(self, tmp_path: Path) -> None:
        """The documentation group of a named element is the index's (family F13), never doubled here."""

        def edit(root: ET.Element) -> None:
            area = _named(root, "EXCLUSIVE-AREA", OFFSET_TABLE)
            long_name = ET.Element(_q("LONG-NAME"))
            ET.SubElement(long_name, _q("L-4"), L="EN").text = "Offset table"
            area.insert(1, long_name)

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert [(path, tag) for path, tag, _reason in _area_entries(report)] == [
            (f"{BEHAVIOUR_PATH}/{OFFSET_TABLE}", "LONG-NAME")
        ]

    def test_an_admin_data_of_an_area_is_the_index_s_one_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            admin = ET.SubElement(_named(root, "EXCLUSIVE-AREA", OFFSET_TABLE), _q("ADMIN-DATA"))
            ET.SubElement(admin, _q("LANGUAGE")).text = "EN"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _area_entries(report) == [
            (
                f"{BEHAVIOUR_PATH}/{OFFSET_TABLE}",
                "ADMIN-DATA",
                f"ADMIN-DATA/SDG vendor metadata on EXCLUSIVE-AREA '{OFFSET_TABLE}' has no SWCraft representation "
                "-- not imported or preserved",
            )
        ]
        assert _component(model_root)["exclusive_areas"] == COMMITTED_AREAS

    def test_an_area_with_no_short_name_element_is_the_blank_name_skip(self, tmp_path: Path) -> None:
        """No ``SHORT-NAME`` at all is read as a blank one: the area is skipped, nothing else moves."""

        def edit(root: ET.Element) -> None:
            area = _named(root, "EXCLUSIVE-AREA", SPARE_AREA)
            area.remove(area.find(_q("SHORT-NAME")))

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _area_entries(report) == [(f"{BEHAVIOUR_PATH}/", "EXCLUSIVE-AREA", "missing or blank SHORT-NAME")]
        assert _component(model_root)["exclusive_areas"] == COMMITTED_AREAS[:2]

    def test_an_area_whose_path_an_earlier_sibling_of_another_kind_claimed_is_not_read(self, tmp_path: Path) -> None:
        """``CONSTANT-MEMORYS`` precedes ``EXCLUSIVE-AREAS``: its item claims the path.

        The area is the index's dropped declaration, and a reference to the path names no area -- the index's
        line names the cause.
        """

        def edit(root: ET.Element) -> None:
            behaviour = _behaviour(root)
            memories = ET.Element(_q("CONSTANT-MEMORYS"))
            parameter = ET.SubElement(memories, _q("PARAMETER-DATA-PROTOTYPE"))
            ET.SubElement(parameter, _q("SHORT-NAME")).text = OFFSET_TABLE
            behaviour.insert(list(behaviour).index(_areas(root)), memories)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        offsets = f"{BEHAVIOUR_PATH}/{OFFSET_TABLE}"
        assert _area_entries(report) == [
            (
                offsets,
                "EXCLUSIVE-AREA",
                "duplicate full AUTOSAR path (already indexed as 'PARAMETER-DATA-PROTOTYPE') -- malformed export, "
                "this declaration is dropped",
            ),
            (offsets, "PARAMETER-DATA-PROTOTYPE", _unread("CONSTANT-MEMORYS", "SWC-INTERNAL-BEHAVIOR")),
            (
                f"{BEHAVIOUR_PATH}/{CALIBRATE}",
                CAN_ENTER_REF,
                f"{CAN_ENTER_REF} {offsets} names no exclusive area its behaviour declares",
            ),
        ]
        document = _component(model_root)
        assert document["exclusive_areas"] == [COMMITTED_AREAS[0], COMMITTED_AREAS[2]]
        assert "can_enter" not in _runnable(document, CALIBRATE)

    @pytest.mark.parametrize(
        ("xml", "item", "path_suffix"),
        [
            (
                "<EXCLUSIVE-AREA-POLICYS><SWC-EXCLUSIVE-AREA-POLICY><API-PRINCIPLE>COMMON</API-PRINCIPLE>"
                f'<EXCLUSIVE-AREA-REF DEST="EXCLUSIVE-AREA">{BEHAVIOUR_PATH}/{SPEED_STATE}</EXCLUSIVE-AREA-REF>'
                "</SWC-EXCLUSIVE-AREA-POLICY></EXCLUSIVE-AREA-POLICYS>",
                "SWC-EXCLUSIVE-AREA-POLICY",
                "",
            ),
            (
                "<EXCLUSIVE-AREA-NESTING-ORDERS><EXCLUSIVE-AREA-NESTING-ORDER><SHORT-NAME>Order</SHORT-NAME>"
                f'<EXCLUSIVE-AREA-REFS><EXCLUSIVE-AREA-REF DEST="EXCLUSIVE-AREA">{BEHAVIOUR_PATH}/{SPEED_STATE}'
                "</EXCLUSIVE-AREA-REF></EXCLUSIVE-AREA-REFS></EXCLUSIVE-AREA-NESTING-ORDER>"
                "</EXCLUSIVE-AREA-NESTING-ORDERS>",
                "EXCLUSIVE-AREA-NESTING-ORDER",
                "/Order",
            ),
        ],
        ids=["policy", "nesting order"],
    )
    def test_a_policy_and_a_nesting_order_stay_unread(
        self, tmp_path: Path, xml: str, item: str, path_suffix: str
    ) -> None:
        def edit(root: ET.Element) -> None:
            for element in ET.fromstring(f'<W xmlns="{NS}">{xml}</W>'):
                _behaviour(root).append(element)

        report, model_root = _import(tmp_path, _export(tmp_path, edit, version="4.3.0"))

        container = xml[1 : xml.index(">")]
        assert _area_entries(report) == [
            (f"{BEHAVIOUR_PATH}{path_suffix}", item, _unread(container, "SWC-INTERNAL-BEHAVIOR"))
        ]
        assert _component(model_root)["exclusive_areas"] == COMMITTED_AREAS

    def test_a_nesting_order_reference_of_a_runnable_stays_unread(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            runnable = _named(root, "RUNNABLE-ENTITY", FILTER)
            container = ET.SubElement(runnable, _q("EXCLUSIVE-AREA-NESTING-ORDER-REFS"))
            reference = ET.SubElement(
                container, _q("EXCLUSIVE-AREA-NESTING-ORDER-REF"), DEST="EXCLUSIVE-AREA-NESTING-ORDER"
            )
            reference.text = f"{BEHAVIOUR_PATH}/Order"

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _area_entries(report) == [
            (
                f"{BEHAVIOUR_PATH}/{FILTER}",
                "EXCLUSIVE-AREA-NESTING-ORDER-REF",
                _unread("EXCLUSIVE-AREA-NESTING-ORDER-REFS", "RUNNABLE-ENTITY"),
            )
        ]


# The loss line: an area's variation point, one line per element


def _variation_point(owner: ET.Element) -> None:
    """A ``VARIATION-POINT`` with a system condition, last in *owner* (the schema's place in an area)."""
    point = ET.SubElement(owner, _q("VARIATION-POINT"))
    ET.SubElement(point, _q("SHORT-LABEL")).text = "Variant"
    ET.SubElement(point, _q("SW-SYSCOND"), {"BINDING-TIME": "PRE-COMPILE-TIME"}).text = "0"


class TestAVariationPointOnAnAreaIsOneLine:
    def test_the_area_is_kept_and_its_condition_is_one_line(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path, _export(tmp_path, lambda root: _variation_point(_named(root, "EXCLUSIVE-AREA", SPEED_STATE)))
        )

        assert _component(model_root)["exclusive_areas"] == COMMITTED_AREAS
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "VARIATION-POINT"]
        assert (entry.path, entry.reason) == (
            f"{BEHAVIOUR_PATH}/{SPEED_STATE}",
            _unread("VARIATION-POINT", "EXCLUSIVE-AREA"),
        )
        assert (entry.category, entry.effect) == (SkipCategory.NOT_SUPPORTED, "abort")

    def test_an_empty_point_is_no_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            ET.SubElement(_named(root, "EXCLUSIVE-AREA", SPEED_STATE), _q("VARIATION-POINT"))

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _entries(report, "VARIATION-POINT") == []


# The references: each skip class


class TestTheReferences:
    def test_a_reference_whose_dest_is_no_exclusive_area_is_a_skip(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _reference(root, CALIBRATE, CAN_ENTER).set("DEST", "EXCLUSIVE-AREA-NESTING-ORDER")

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        # The line sits at the runnable's path (a reference has none of its own),
        # and a line at that path never decides the runnable's fate.
        assert "can_enter" not in _runnable(_component(model_root), CALIBRATE)
        (entry,) = [entry for entry in report.skip_entries if entry.tag == CAN_ENTER_REF]
        assert entry.path == f"{BEHAVIOUR_PATH}/{CALIBRATE}"
        assert not [entry for entry in report.skip_entries if entry.tag == "RUNNABLE-ENTITY"]
        assert entry.reason == (
            f"{CAN_ENTER_REF} {BEHAVIOUR_PATH}/{OFFSET_TABLE} states DEST 'EXCLUSIVE-AREA-NESTING-ORDER', and only "
            "EXCLUSIVE-AREA names an exclusive area"
        )
        assert (entry.category, entry.effect) == (SkipCategory.NOT_SUPPORTED, "abort")

    def test_a_reference_into_another_behaviour_is_a_skip(self, tmp_path: Path) -> None:
        other = f"/ComponentTypes/Other/Other_InternalBehavior/{SPEED_STATE}"

        def edit(root: ET.Element) -> None:
            _reference(root, FILTER, RUNS_INSIDE).text = other

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "runs_inside" not in _runnable(_component(model_root), FILTER)
        assert _entries(report, RUNS_INSIDE_REF) == [
            (
                f"{BEHAVIOUR_PATH}/{FILTER}",
                RUNS_INSIDE_REF,
                f"{RUNS_INSIDE_REF} {other} names an exclusive area outside its own behaviour -- an exclusive area "
                "is local to its component",
            )
        ]

    def test_a_reference_naming_no_area_of_its_behaviour_is_a_skip(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _reference(root, FILTER, RUNS_INSIDE).text = f"{BEHAVIOUR_PATH}/NoSuchArea"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "runs_inside" not in _runnable(_component(model_root), FILTER)
        assert _entries(report, RUNS_INSIDE_REF) == [
            (
                f"{BEHAVIOUR_PATH}/{FILTER}",
                RUNS_INSIDE_REF,
                f"{RUNS_INSIDE_REF} {BEHAVIOUR_PATH}/NoSuchArea names no exclusive area its behaviour declares",
            )
        ]

    @pytest.mark.parametrize("dest", [True, False], ids=["dest stated", "no dest"])
    def test_an_empty_reference_is_a_skip(self, tmp_path: Path, dest: bool) -> None:
        """Judged before its ``DEST``: an empty reference names nothing, whatever ``DEST`` it states."""

        def edit(root: ET.Element) -> None:
            reference = _reference(root, FILTER, RUNS_INSIDE)
            reference.text = None
            if not dest:
                del reference.attrib["DEST"]

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert [reason for _path, _tag, reason in _entries(report, RUNS_INSIDE_REF)] == [
            f"{RUNS_INSIDE_REF} names no exclusive area"
        ]

    def test_a_reference_stating_no_dest_is_a_skip(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            del _reference(root, FILTER, RUNS_INSIDE).attrib["DEST"]

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "runs_inside" not in _runnable(_component(model_root), FILTER)
        assert [reason for _path, _tag, reason in _entries(report, RUNS_INSIDE_REF)] == [
            f"{RUNS_INSIDE_REF} {BEHAVIOUR_PATH}/{SPEED_STATE} states no DEST, and only EXCLUSIVE-AREA names an "
            "exclusive area"
        ]

    def test_a_reference_to_a_skipped_area_is_chain_skipped(self, tmp_path: Path) -> None:
        """An area with a blank ``SHORT-NAME`` is the file's defect; both its users are chained to it."""

        def edit(root: ET.Element) -> None:
            _named(root, "EXCLUSIVE-AREA", SPEED_STATE).find(_q("SHORT-NAME")).text = " "
            for runnable in (FILTER, MONITOR):
                _reference(root, runnable, RUNS_INSIDE).text = f"{BEHAVIOUR_PATH}/ "

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        document = _component(model_root)
        assert [area["name"] for area in document["exclusive_areas"]] == [OFFSET_TABLE, SPARE_AREA]
        assert all("runs_inside" not in _runnable(document, name) for name in (FILTER, MONITOR))
        (root_entry,) = [entry for entry in report.skip_entries if entry.tag == "EXCLUSIVE-AREA"]
        assert root_entry.path == f"{BEHAVIOUR_PATH}/ "
        assert root_entry.category == SkipCategory.EXPORT_DEFECT
        chained = [entry for entry in report.skip_entries if entry.tag == RUNS_INSIDE_REF]
        assert [entry.path for entry in chained] == [f"{BEHAVIOUR_PATH}/{FILTER}", f"{BEHAVIOUR_PATH}/{MONITOR}"]
        assert all(entry.reason.startswith("chain-skipped: exclusive area ' ' was skipped") for entry in chained)
        assert all(entry.category == SkipCategory.EXPORT_DEFECT for entry in chained)

    def test_a_skipped_runnable_chain_skips_its_references(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _named(root, "RUNNABLE-ENTITY", CALIBRATE).find(_q("MINIMUM-START-INTERVAL")).text = "-1"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert CALIBRATE not in [runnable["name"] for runnable in _component(model_root)["runnables"]]
        assert [
            (path, tag) for path, tag, reason in _entries(report) if reason.startswith("chain-skipped: runnable")
        ] == [(f"{BEHAVIOUR_PATH}/{CALIBRATE}", CAN_ENTER_REF)]

    def test_two_references_to_one_area_in_one_wrapper_collapse_with_a_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            reference = _reference(root, FILTER, RUNS_INSIDE)
            _parent(root, reference).append(copy.deepcopy(reference))

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _runnable(_component(model_root), FILTER)["runs_inside"] == [{"area": SPEED_STATE}]
        assert report.access_collapse_lines == [
            f"{COMPONENT}/{FILTER}: runs_inside entry {SPEED_STATE} declared twice -- the repeated "
            f"{RUNS_INSIDE_REF} collapsed into the first"
        ]
        assert _area_entries(report) == []
        assert report.imported_exclusive_areas == {COMPONENT: (3, 3)}

    def test_one_area_in_both_roles_on_one_runnable_imports_as_it_is(self, tmp_path: Path) -> None:
        """A finding is not fixed by the importer: the source's two statements are both kept."""

        def edit(root: ET.Element) -> None:
            runnable = _named(root, "RUNNABLE-ENTITY", FILTER)
            container = ET.Element(_q(CAN_ENTER))
            ET.SubElement(container, _q(CAN_ENTER_REF), DEST="EXCLUSIVE-AREA").text = f"{BEHAVIOUR_PATH}/{SPEED_STATE}"
            runnable.insert(1, container)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        filter_speed = _runnable(_component(model_root), FILTER)
        assert filter_speed["can_enter"] == [{"area": SPEED_STATE}]
        assert filter_speed["runs_inside"] == [{"area": SPEED_STATE}]
        assert _area_entries(report) == []


# Source collisions


class TestSourceCollisions:
    def test_an_area_named_like_a_runnable_drops_the_runnable_with_the_index_s_line(self, tmp_path: Path) -> None:
        """EXCLUSIVE-AREAS precedes RUNNABLES: the area claims the path first, as it did before areas were read."""

        def edit(root: ET.Element) -> None:
            _named(root, "EXCLUSIVE-AREA", SPARE_AREA).find(_q("SHORT-NAME")).text = FILTER

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        document = _component(model_root)
        assert FILTER not in [runnable["name"] for runnable in document["runnables"]]
        assert FILTER in [area["name"] for area in document["exclusive_areas"]]
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "RUNNABLE-ENTITY"]
        assert entry.path == f"{BEHAVIOUR_PATH}/{FILTER}"
        assert entry.reason.startswith("duplicate full AUTOSAR path")

    def test_two_areas_of_one_name_bind_every_reference_to_the_survivor(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            twin = copy.deepcopy(_named(root, "EXCLUSIVE-AREA", SPEED_STATE))
            twin.find(f"{_q('DESC')}/{_q('L-2')}").text = "The second declaration."
            _areas(root).append(twin)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        document = _component(model_root)
        assert document["exclusive_areas"] == COMMITTED_AREAS
        assert _runnable(document, FILTER)["runs_inside"] == [{"area": SPEED_STATE}]
        assert _runnable(document, MONITOR)["runs_inside"] == [{"area": SPEED_STATE}]
        (entry,) = _area_entries(report)
        assert entry[:2] == (f"{BEHAVIOUR_PATH}/{SPEED_STATE}", "EXCLUSIVE-AREA")
        assert entry[2].startswith("duplicate full AUTOSAR path")


# The identifier guard


@pytest.mark.parametrize(
    ("name", "violation"),
    [("Bad Area", "AUTOSAR Identifier matching"), ("A" * 129, "must be at most 128 characters")],
    ids=["pattern", "length"],
)
def test_an_area_name_that_is_no_identifier_aborts_with_nothing_written(
    tmp_path: Path, name: str, violation: str
) -> None:
    """``reconcile.identifier_violations`` refuses a declared area name as it does a variable's."""

    def edit(root: ET.Element) -> None:
        _named(root, "EXCLUSIVE-AREA", OFFSET_TABLE).find(_q("SHORT-NAME")).text = name
        _reference(root, CALIBRATE, CAN_ENTER).text = f"{BEHAVIOUR_PATH}/{name}"

    source = _export(tmp_path, edit)
    with pytest.raises(ValueError, match="Import aborted with nothing written") as aborted:
        _import(tmp_path, source)
    assert f"SWC '{COMPONENT}' exclusive area '{name}': " in str(aborted.value)
    assert violation in str(aborted.value)
    assert not list((tmp_path / "project" / "model" / "swc").rglob("*.yaml"))


# Two declarations of one component


def _duplicated(change: Callable[[ET.Element], None] | None = None) -> Callable[[ET.Element], None]:
    """The component declared a second time in a package of its own, *change* applied to the copy.

    The copies differ in their inner paths only, which the signature resolves away.
    """

    def edit(root: ET.Element) -> None:
        component = _named(root, "APPLICATION-SW-COMPONENT-TYPE", COMPONENT)
        twin = copy.deepcopy(component)
        for tag in ("START-ON-EVENT-REF", CAN_ENTER_REF, RUNS_INSIDE_REF):
            for reference in twin.iter(_q(tag)):
                reference.text = reference.text.replace("/ComponentTypes/", "/ComponentTypes/Copy/")
        if change is not None:
            change(twin)
        component_types = _named(root, "AR-PACKAGE", "ComponentTypes")
        packages = component_types.find(_q("AR-PACKAGES"))
        if packages is None:
            packages = ET.SubElement(component_types, _q("AR-PACKAGES"))
        package = ET.SubElement(packages, _q("AR-PACKAGE"))
        ET.SubElement(package, _q("SHORT-NAME")).text = "Copy"
        ET.SubElement(ET.SubElement(package, _q("ELEMENTS")), twin.tag).extend(list(twin))

    return edit


class TestTwoDeclarationsOfOneComponent:
    def test_two_equal_declarations_merge(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _duplicated()))

        assert report.conflicting_groups == []
        document = _component(model_root)
        assert document["exclusive_areas"] == COMMITTED_AREAS
        assert _runnable(document, CALIBRATE)["can_enter"] == [{"area": OFFSET_TABLE}]

    @pytest.mark.parametrize("difference", ["name", "order"])
    def test_a_differing_area_is_a_conflict(self, tmp_path: Path, difference: str) -> None:
        """The signature takes the areas by name and in declared order: a rename and a reorder both differ."""

        def change(twin: ET.Element) -> None:
            if difference == "name":
                _named(twin, "EXCLUSIVE-AREA", SPARE_AREA).find(_q("SHORT-NAME")).text = "OtherArea"
                return
            wrapper = _areas(twin)
            areas = list(wrapper)
            for area in areas:
                wrapper.remove(area)
            wrapper.extend(reversed(areas))

        report, model_root = _import(tmp_path, _export(tmp_path, _duplicated(change)))

        assert len(report.conflicting_groups) == 1
        assert not list((model_root / "swc").rglob(f"{COMPONENT}.yaml"))

    @pytest.mark.parametrize("difference", ["target", "role", "order"])
    def test_a_differing_reference_is_a_conflict(self, tmp_path: Path, difference: str) -> None:
        def change(twin: ET.Element) -> None:
            if difference == "target":
                reference = _reference(twin, CALIBRATE, CAN_ENTER)
                reference.text = reference.text.replace(OFFSET_TABLE, SPARE_AREA)
            elif difference == "role":
                wrapper = _named(twin, "RUNNABLE-ENTITY", CALIBRATE).find(_q(CAN_ENTER))
                wrapper.tag = _q(RUNS_INSIDE)
                for reference in wrapper:
                    reference.tag = _q(RUNS_INSIDE_REF)
            else:
                # The same two areas, in the other order.
                wrapper = _named(twin, "RUNNABLE-ENTITY", FILTER).find(_q(RUNS_INSIDE))
                references = list(wrapper)
                for reference in references:
                    wrapper.remove(reference)
                wrapper.extend(reversed(references))

        def change_primary_too(root: ET.Element) -> None:
            if difference == "order":
                # The primary states two areas; the copy states them reversed.
                wrapper = _named(root, "RUNNABLE-ENTITY", FILTER).find(_q(RUNS_INSIDE))
                second = copy.deepcopy(wrapper[0])
                second.text = second.text.replace(SPEED_STATE, OFFSET_TABLE)
                wrapper.append(second)
            _duplicated(change)(root)

        report, model_root = _import(tmp_path, _export(tmp_path, change_primary_too))

        assert len(report.conflicting_groups) == 1
        assert not list((model_root / "swc").rglob(f"{COMPONENT}.yaml"))

    def test_a_differing_area_description_merges_and_is_reported(self, tmp_path: Path) -> None:
        """The nested-description walk: the merged component keeps the primary's text, and says so."""

        def change(twin: ET.Element) -> None:
            _named(twin, "EXCLUSIVE-AREA", SPEED_STATE).find(f"{_q('DESC')}/{_q('L-2')}").text = "Another text."

        report, model_root = _import(tmp_path, _export(tmp_path, _duplicated(change)))

        assert report.conflicting_groups == []
        texts = {
            f"{BEHAVIOUR_PATH}/{SPEED_STATE}": SPEED_STATE_DESCRIPTION,
            f"/ComponentTypes/Copy/{COMPONENT}/{COMPONENT}_InternalBehavior/{SPEED_STATE}": "Another text.",
        }
        (entry,) = [entry for entry in report.skip_entries if entry.tag == "DESC"]
        assert entry.path in texts
        assert entry.reason.startswith(f"DESC of {COMPONENT}/exclusive_areas/{SPEED_STATE} not carried")
        # The merged component keeps the primary's text: the one the line does not name.
        (kept,) = [text for path, text in texts.items() if path != entry.path]
        areas = _component(model_root)["exclusive_areas"]
        assert areas[0] == {"name": SPEED_STATE, "description": kept}
        assert areas[1:] == COMMITTED_AREAS[1:]


# --packages: a component the selection excludes contributes nothing


def test_a_component_the_selection_excludes_counts_nothing(tmp_path: Path) -> None:
    """A second component in a package of its own is selected; the area component is not."""

    def edit(root: ET.Element) -> None:
        package = ET.SubElement(root.find(_q("AR-PACKAGES")), _q("AR-PACKAGE"))
        ET.SubElement(package, _q("SHORT-NAME")).text = "Others"
        other = ET.SubElement(ET.SubElement(package, _q("ELEMENTS")), _q("APPLICATION-SW-COMPONENT-TYPE"))
        ET.SubElement(other, _q("SHORT-NAME")).text = "Other"

    report, model_root = _import(tmp_path, _export(tmp_path, edit), packages=["Others"])

    assert [path.stem for path in (model_root / "swc").rglob("*.yaml")] == ["Other"]
    assert report.imported_exclusive_areas == {}
    assert not any("exclusive area" in line for line in report.render().splitlines())
