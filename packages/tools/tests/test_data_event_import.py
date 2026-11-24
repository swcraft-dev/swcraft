"""The reader and the importer of data-received events.

The synthetic export is the writer's own output of the shared fixture (``data_event_support``),
changed case by case by an ElementTree edit of the generated file or a change to the model copy, so
every case starts from a document the exporter wrote and states the one difference it is about. Each
case runs the whole of ``import model`` (``run_import``) into a fresh project; the reader or the
mapper is asked directly only where the case is its alone.

**Not reached:** the real tool exports (the closed acceptance), and ``import component``'s regimes
and its report.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from data_event_support import (
    COMPONENT,
    COMPONENT_FILE,
    FILTER,
    FRONT_LEFT,
    FRONT_RIGHT,
    MODE_PORT,
    MONITOR,
    REAR_LEFT,
    REAR_PORT,
    SLEEP_MODE,
    TRACK_REAR,
    WHEEL_PORT,
    data_event,
    data_event_model,
)
from external_stubs_support import change, put
from project_support import init_project
from swcraft_tools.arxml_import import map_data_types, map_interfaces, map_swcs, run_import
from swcraft_tools.arxml_import.report import ImportReport
from swcraft_tools.arxml_import.swc_mapping import UnansweredRecord
from swcraft_tools.arxml_import.unresolved import UnresolvedReferences
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml

NS = "http://autosar.org/schema/r4.0"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", NS)
ET.register_namespace("xsi", XSI)

EVENT_TAG = "DATA-RECEIVED-EVENT"
COMPONENT_PATH = f"/ComponentTypes/{COMPONENT}"
BEHAVIOUR_PATH = f"{COMPONENT_PATH}/{COMPONENT}_InternalBehavior"
#: The three events, by the names the writer derives for them.
FRONT_LEFT_EVENT = f"DRT_{WHEEL_PORT}_{FRONT_LEFT}"
FRONT_RIGHT_EVENT = f"DRT_{WHEEL_PORT}_{FRONT_RIGHT}"
REAR_LEFT_EVENT = f"DRT_{REAR_PORT}_{REAR_LEFT}"
#: The stub the import mints for the rear port's interface.
REAR_STUB = "RearSpeedIf_ext0"

#: What the fixture's component holds of the construct, as committed.
COMMITTED_EVENTS: dict[str, list[dict[str, Any]]] = {
    FILTER: [data_event(WHEEL_PORT, FRONT_LEFT)],
    MONITOR: [{"type": "timing", "period": 0.1}, data_event(WHEEL_PORT, FRONT_RIGHT, disabled_modes=[SLEEP_MODE])],
    TRACK_REAR: [data_event(REAR_PORT, REAR_LEFT)],
}


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _named(root: ET.Element, tag: str, name: str) -> ET.Element:
    """The one *tag* element of *root* whose ``SHORT-NAME`` is *name*."""
    found = [el for el in root.iter(_q(tag)) if el.findtext(_q("SHORT-NAME")) == name]
    assert len(found) == 1, f"expected one {tag} named {name!r}, found {len(found)}"
    return found[0]


def _event(root: ET.Element, name: str = FRONT_LEFT_EVENT) -> ET.Element:
    return _named(root, EVENT_TAG, name)


def _iref(root: ET.Element, name: str = FRONT_LEFT_EVENT) -> ET.Element:
    iref = _event(root, name).find(_q("DATA-IREF"))
    assert iref is not None
    return iref


def _set_iref(root: ET.Element, *, port: str | None = None, target: str | None = None, dest: str | None = None) -> None:
    """Rewrite the front-left event's ``DATA-IREF``: the port path, the target path, the port's ``DEST``."""
    iref = _iref(root)
    port_ref = iref.find(_q("CONTEXT-R-PORT-REF"))
    target_ref = iref.find(_q("TARGET-DATA-ELEMENT-REF"))
    assert port_ref is not None and target_ref is not None
    if port is not None:
        port_ref.text = port
    if dest is not None:
        port_ref.set("DEST", dest)
    if target is not None:
        target_ref.text = target


def _remove(root: ET.Element, parent: ET.Element, tag: str) -> None:
    child = parent.find(_q(tag))
    assert child is not None
    parent.remove(child)


def _export(
    tmp_path: Path,
    edit: Callable[[ET.Element], None] | None = None,
    *,
    model_edit: Callable[[Path], None] | None = None,
    version: str = "4.2.2",
) -> Path:
    """The writer's output of the fixture, its model changed by *model_edit* and its file by *edit*."""
    model_root = data_event_model(tmp_path / "model")
    if model_edit is not None:
        model_edit(model_root)
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


def _document(model_root: Path, layer: str, name: str) -> dict[str, Any]:
    (path,) = list((model_root / layer).rglob(f"{name}.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _component(model_root: Path) -> dict[str, Any]:
    return _document(model_root, "swc", COMPONENT)


def _events(model_root: Path) -> dict[str, list[dict[str, Any]]]:
    return {runnable["name"]: runnable.get("events", []) for runnable in _component(model_root)["runnables"]}


def _entries(report: ImportReport) -> list[tuple[str, str, str]]:
    """Every entry but the component implementation's, which no case is about."""
    return [(entry.path, entry.tag, entry.reason) for entry in report.skip_entries if entry.tag != "SWC-IMPLEMENTATION"]


def _event_entries(report: ImportReport, name: str = FRONT_LEFT_EVENT) -> list[tuple[str, str]]:
    return [(tag, reason) for path, tag, reason in _entries(report) if path == f"{BEHAVIOUR_PATH}/{name}"]


def _unread(child: str, parent_tag: str) -> str:
    """The closed tables' sentence for an unread child (``arxml_reader._unread_reason``)."""
    return f"{child} of a {parent_tag} is not read by SWCraft -- no SWCraft representation, not imported or preserved"


def _add_runnable_event(runnable: str, event: dict[str, Any]) -> Callable[[Path], None]:
    def edit(model_root: Path) -> None:
        def mutate(document: dict[str, Any]) -> None:
            (target,) = [r for r in document["runnables"] if r["name"] == runnable]
            target["events"].append(event)

        change(model_root, COMPONENT_FILE, mutate)

    return edit


def _add_port(name: str, direction: str, interface: str) -> Callable[[Path], None]:
    def edit(model_root: Path) -> None:
        change(
            model_root,
            COMPONENT_FILE,
            lambda document: document["ports"].append({"name": name, "direction": direction, "interface": interface}),
        )

    return edit


# The writer's own output comes back as it was written


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
class TestTheWritersOwnOutputComesBack:
    def test_every_event_comes_back_in_declared_order_beside_the_timing_event(
        self, tmp_path: Path, version: str
    ) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, version=version))

        assert _entries(report) == []
        assert _events(model_root) == COMMITTED_EVENTS

    def test_the_stub_lists_the_element_the_event_names(self, tmp_path: Path, version: str) -> None:
        _report, model_root = _import(tmp_path, _export(tmp_path, version=version))

        stub = _document(model_root, "interfaces", REAR_STUB)
        assert (stub["external_ref"], stub["data_elements"]) == ("/Foreign/PortInterfaces/RearSpeedIf", [REAR_LEFT])

    def test_the_report_counts_the_component_s_events_and_no_kind_is_unrecognized(
        self, tmp_path: Path, version: str
    ) -> None:
        report, _model_root = _import(tmp_path, _export(tmp_path, version=version))

        assert report.imported_data_events == {COMPONENT: 3}
        rendered = report.render().splitlines()
        assert "IMPORTED 3 data-received events" in rendered
        assert f"  {COMPONENT}: 3 data-received events" in rendered
        assert not any("unrecognized event kind" in line for line in rendered)

    def test_the_imported_project_checks_clean_and_regenerates_the_events_by_value(
        self, tmp_path: Path, version: str
    ) -> None:
        """The round trip: the import followed by ``generate arxml`` writes every event back."""
        source = _export(tmp_path, version=version)
        _report, model_root = _import(tmp_path, source)
        assert check_cross_layer_consistency(str(model_root)).consistent
        again = tmp_path / "again.arxml"
        generate_arxml(str(model_root), str(again), version=version)

        def facts(path: Path) -> list[tuple[Any, ...]]:
            (swc,) = read_arxml(str(path)).swcs
            return [
                (
                    event.kind,
                    event.name,
                    event.runnable_ref,
                    event.context_r_port_ref,
                    event.context_r_port_dest,
                    event.target_data_element_ref,
                    [(mode.context_port_ref, mode.mode_group_ref, mode.mode_ref) for mode in event.disabled_modes],
                )
                for event in swc.events
            ]

        assert [fact for fact in facts(source) if fact[0] == EVENT_TAG]
        assert facts(again) == facts(source)


# The reader


class TestTheReader:
    def test_both_halves_and_the_port_s_dest_are_read_at_the_event_s_own_node(self, tmp_path: Path) -> None:
        (swc,) = read_arxml(str(_export(tmp_path))).swcs

        assert [
            (event.name, event.context_r_port_ref, event.context_r_port_dest, event.target_data_element_ref)
            for event in swc.events
            if event.kind == EVENT_TAG
        ] == [
            (
                FRONT_LEFT_EVENT,
                f"{COMPONENT_PATH}/{WHEEL_PORT}",
                "R-PORT-PROTOTYPE",
                f"/PortInterfaces/WheelSpeedIf/{FRONT_LEFT}",
            ),
            (
                FRONT_RIGHT_EVENT,
                f"{COMPONENT_PATH}/{WHEEL_PORT}",
                "R-PORT-PROTOTYPE",
                f"/PortInterfaces/WheelSpeedIf/{FRONT_RIGHT}",
            ),
            (
                REAR_LEFT_EVENT,
                f"{COMPONENT_PATH}/{REAR_PORT}",
                "R-PORT-PROTOTYPE",
                f"/Foreign/PortInterfaces/RearSpeedIf/{REAR_LEFT}",
            ),
        ]

    def test_the_disabled_mode_is_read_on_the_data_event(self, tmp_path: Path) -> None:
        (swc,) = read_arxml(str(_export(tmp_path))).swcs

        (event,) = [event for event in swc.events if event.name == FRONT_RIGHT_EVENT]
        assert [(m.context_port_ref, m.mode_group_ref, m.mode_ref) for m in event.disabled_modes] == [
            (f"{COMPONENT_PATH}/{MODE_PORT}", SLEEP_MODE["mode_group_ref"], SLEEP_MODE["mode_ref"])
        ]

    def test_a_data_iref_below_another_child_is_not_read(self, tmp_path: Path) -> None:
        """Only the event's **own** ``DATA-IREF`` child: one under an off-schema child is that child's line."""

        def edit(root: ET.Element) -> None:
            event = _event(root)
            iref = event.find(_q("DATA-IREF"))
            assert iref is not None
            event.remove(iref)
            ET.SubElement(event, _q("X-WRAPPER")).append(iref)

        source = _export(tmp_path, edit)
        (swc,) = read_arxml(str(source)).swcs
        (event,) = [event for event in swc.events if event.name == FRONT_LEFT_EVENT]
        assert (event.context_r_port_ref, event.context_r_port_dest, event.target_data_element_ref) == (
            None,
            None,
            None,
        )
        report, model_root = _import(tmp_path, source)
        assert _event_entries(report) == [
            ("DATA-IREF", _unread("X-WRAPPER", EVENT_TAG)),
            (EVENT_TAG, "data-received event has no CONTEXT-R-PORT-REF in a DATA-IREF"),
        ]
        assert _events(model_root)[FILTER] == []

    def test_a_category_and_an_activation_reason_are_the_closed_table_s_unread_lines(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            event = _event(root)
            ET.SubElement(event, _q("CATEGORY")).text = "SPECIAL"
            reason = ET.SubElement(event, _q("ACTIVATION-REASON-REPRESENTATION-REF"))
            reason.set("DEST", "EXECUTABLE-ENTITY-ACTIVATION-REASON")
            reason.text = "/Reasons/R1"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert sorted(_event_entries(report)) == sorted(
            [
                ("CATEGORY", _unread("CATEGORY", EVENT_TAG)),
                ("ACTIVATION-REASON-REPRESENTATION-REF", _unread("ACTIVATION-REASON-REPRESENTATION-REF", EVENT_TAG)),
            ]
        )
        assert _events(model_root)[FILTER] == COMMITTED_EVENTS[FILTER]

    def test_an_off_schema_child_and_an_attribute_of_the_data_iref_are_its_closed_table_s_lines(
        self, tmp_path: Path
    ) -> None:
        def edit(root: ET.Element) -> None:
            iref = _iref(root)
            iref.set("S", "checksum")
            ET.SubElement(iref, _q("CONTEXT-SW-COMPONENT-PROTOTYPE-REF")).text = "/Somewhere/Prototype"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        owner = "R-VARIABLE-IN-ATOMIC-SWC-INSTANCE-REF"
        assert sorted(
            (path, tag, reason) for path, tag, reason in _entries(report) if path.startswith(BEHAVIOUR_PATH)
        ) == sorted(
            [
                (
                    f"{BEHAVIOUR_PATH}/{FRONT_LEFT_EVENT}/DATA-IREF",
                    "CONTEXT-SW-COMPONENT-PROTOTYPE-REF",
                    _unread("CONTEXT-SW-COMPONENT-PROTOTYPE-REF", owner),
                ),
                (
                    f"{BEHAVIOUR_PATH}/{FRONT_LEFT_EVENT}/DATA-IREF",
                    "S",
                    f"attribute S of a {owner} is not read by SWCraft -- not imported or preserved",
                ),
            ]
        )
        assert _events(model_root)[FILTER] == COMMITTED_EVENTS[FILTER]

    def test_an_unread_attribute_of_a_disabled_mode_is_reported_on_a_data_event(self, tmp_path: Path) -> None:
        """The disabled-mode IREF's closed table applies to a data-received event as to a timing event."""

        def edit(root: ET.Element) -> None:
            mode = _event(root, FRONT_RIGHT_EVENT).find(f"{_q('DISABLED-MODE-IREFS')}/{_q('DISABLED-MODE-IREF')}")
            assert mode is not None
            mode.set("T", "2026-11-05")

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _entries(report) == [
            (
                f"{BEHAVIOUR_PATH}/{FRONT_RIGHT_EVENT}/DISABLED-MODE-IREFS",
                "T",
                "attribute T of a R-MODE-IN-ATOMIC-SWC-INSTANCE-REF is not read by SWCraft -- "
                "not imported or preserved",
            )
        ]
        assert _events(model_root)[MONITOR] == COMMITTED_EVENTS[MONITOR]

    def test_a_description_keeps_the_uncarried_desc_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            event = _event(root)
            desc = ET.Element(_q("DESC"))
            ET.SubElement(desc, _q("L-2"), {"L": "FOR-ALL"}).text = "Starts the filter."
            event.insert(1, desc)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _event_entries(report) == [
            ("DESC", "DESC not carried: the event's name is derived and the model holds no text for it")
        ]
        assert _events(model_root)[FILTER] == COMMITTED_EVENTS[FILTER]


class TestAVariationPointOnAnEventIsOneLine:
    def test_the_event_is_kept_and_its_condition_is_one_line(self, tmp_path: Path) -> None:
        """The event is kept, and its variation point is one line at the event, whatever the point holds."""

        def edit(root: ET.Element) -> None:
            point = ET.SubElement(_event(root), _q("VARIATION-POINT"))
            ET.SubElement(point, _q("SHORT-LABEL")).text = "Variant"
            ET.SubElement(point, _q("SW-SYSCOND")).text = "FEATURE == 1"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _event_entries(report) == [("VARIATION-POINT", _unread("VARIATION-POINT", EVENT_TAG))]
        assert _events(model_root)[FILTER] == COMMITTED_EVENTS[FILTER]

    def test_an_empty_point_is_no_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            ET.SubElement(_event(root), _q("VARIATION-POINT"))

        report, _model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _entries(report) == []


# The mapping


class TestTheMapping:
    def test_two_events_on_one_runnable_come_back_in_source_order(self, tmp_path: Path) -> None:
        source = _export(tmp_path, model_edit=_add_runnable_event(FILTER, data_event(WHEEL_PORT, FRONT_RIGHT)))

        report, model_root = _import(tmp_path, source)

        assert _entries(report) == []
        assert _events(model_root)[FILTER] == [data_event(WHEEL_PORT, FRONT_LEFT), data_event(WHEEL_PORT, FRONT_RIGHT)]
        assert report.imported_data_events == {COMPONENT: 4}

    def test_a_stated_name_is_kept_and_a_derived_one_is_not_stated(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _event(root).find(_q("SHORT-NAME")).text = "E_FrontLeftArrived"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _entries(report) == []
        events = _events(model_root)
        assert events[FILTER] == [data_event(WHEEL_PORT, FRONT_LEFT, name="E_FrontLeftArrived")]
        assert events[TRACK_REAR] == COMMITTED_EVENTS[TRACK_REAR]

    def test_a_disabled_mode_whose_port_the_import_skips_is_one_chained_line(self, tmp_path: Path) -> None:
        """The mode port as a PR port: the event is kept without the mode, which is one chained line."""

        def edit(root: ET.Element) -> None:
            port = _named(root, "R-PORT-PROTOTYPE", MODE_PORT)
            port.tag = _q("PR-PORT-PROTOTYPE")

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        (line,) = [
            (tag, reason) for tag, reason in _event_entries(report, FRONT_RIGHT_EVENT) if tag == "DISABLED-MODE-IREFS"
        ]
        assert line[1].startswith(
            f"disabled mode not carried: its context port {COMPONENT_PATH}/{MODE_PORT} was skipped"
        )
        assert _events(model_root)[MONITOR] == [{"type": "timing", "period": 0.1}, data_event(WHEEL_PORT, FRONT_RIGHT)]

    def test_a_component_the_selection_excludes_counts_nothing(self, tmp_path: Path) -> None:
        """A second component in a package of its own is selected; the event component is not."""

        def edit(root: ET.Element) -> None:
            package = ET.SubElement(root.find(_q("AR-PACKAGES")), _q("AR-PACKAGE"))
            ET.SubElement(package, _q("SHORT-NAME")).text = "Others"
            other = ET.SubElement(ET.SubElement(package, _q("ELEMENTS")), _q("APPLICATION-SW-COMPONENT-TYPE"))
            ET.SubElement(other, _q("SHORT-NAME")).text = "Other"

        report, model_root = _import(tmp_path, _export(tmp_path, edit), packages=["Others"])

        assert [path.stem for path in (model_root / "swc").rglob("*.yaml")] == ["Other"]
        assert report.imported_data_events == {}
        assert not any("data-received" in line for line in report.render().splitlines())


# What the importer skips, each with a reason


def _wheel_port_as_pr(root: ET.Element) -> None:
    _named(root, "R-PORT-PROTOTYPE", WHEEL_PORT).tag = _q("PR-PORT-PROTOTYPE")
    _set_iref(root, dest="PR-PORT-PROTOTYPE")


def _no_data_iref(root: ET.Element) -> None:
    _remove(root, _event(root), "DATA-IREF")


def _no_port_ref(root: ET.Element) -> None:
    _remove(root, _iref(root), "CONTEXT-R-PORT-REF")


def _no_target_ref(root: ET.Element) -> None:
    _remove(root, _iref(root), "TARGET-DATA-ELEMENT-REF")


SKIP_CASES = [
    pytest.param(
        _wheel_port_as_pr,
        None,
        "chain-skipped: port 'R_WheelSpeed' was skipped (PR (combined provide+require) ports have no SWCraft "
        "equivalent (Port.direction is provide|require only))",
        id="pr-port",
    ),
    pytest.param(
        lambda root: _set_iref(root, port="/ComponentTypes/Other/R_WheelSpeed"),
        None,
        f"data-received event names the port /ComponentTypes/Other/R_WheelSpeed, which is not a port of its own "
        f"SWC '{COMPONENT}' ({COMPONENT_PATH})",
        id="port-of-another-component",
    ),
    pytest.param(
        lambda root: _set_iref(root, target="/PortInterfaces/WheelSpeedIf/Missing"),
        None,
        "unresolved reference /PortInterfaces/WheelSpeedIf/Missing "
        f"(not a data element of port '{WHEEL_PORT}'s interface 'WheelSpeedIf')",
        id="element-not-declared",
    ),
    pytest.param(
        lambda root: _set_iref(root, target=f"/PortInterfaces/OtherIf/{FRONT_LEFT}"),
        None,
        f"unresolved reference /PortInterfaces/OtherIf/{FRONT_LEFT} "
        f"(not a data element of port '{WHEEL_PORT}'s interface 'WheelSpeedIf')",
        id="element-of-another-interface",
    ),
    pytest.param(
        lambda root: _set_iref(root, target=FRONT_LEFT),
        None,
        f"unresolved reference {FRONT_LEFT} (not an AUTOSAR path)",
        id="target-not-a-path",
    ),
    pytest.param(_no_data_iref, None, "data-received event has no CONTEXT-R-PORT-REF in a DATA-IREF", id="no-iref"),
    pytest.param(
        _no_port_ref, None, "data-received event has no CONTEXT-R-PORT-REF in a DATA-IREF", id="no-context-port-ref"
    ),
    pytest.param(
        _no_target_ref,
        None,
        "data-received event has no TARGET-DATA-ELEMENT-REF in a DATA-IREF",
        id="no-target-data-element-ref",
    ),
    pytest.param(
        lambda root: _set_iref(root, dest="ABSTRACT-REQUIRED-PORT-PROTOTYPE"),
        None,
        "data-received event names its context port with DEST ABSTRACT-REQUIRED-PORT-PROTOTYPE, which no concrete "
        "port prototype is (a data-received event needs an R-PORT-PROTOTYPE)",
        id="abstract-required-port-dest",
    ),
    pytest.param(
        lambda root: _set_iref(root, port=f"{COMPONENT_PATH}/P_Out"),
        _add_port("P_Out", "provide", "WheelSpeedIf"),
        "data-received event through provide port 'P_Out' (a data-received event needs a require port)",
        id="provide-port",
    ),
    pytest.param(
        lambda root: _set_iref(root, port=f"{COMPONENT_PATH}/R_Calibrate", target="/PortInterfaces/CalibrateIf/Reset"),
        lambda model_root: (
            put(
                model_root,
                "interfaces/CalibrateIf.yaml",
                {"name": "CalibrateIf", "kind": "client_server", "operations": [{"name": "Reset"}]},
            ),
            _add_port("R_Calibrate", "require", "CalibrateIf")(model_root),
        ),
        "data-received event through port 'R_Calibrate' whose interface 'CalibrateIf' is client/server "
        "(a data-received event needs a sender/receiver interface)",
        id="client-server-interface",
    ),
]


@pytest.mark.parametrize(("edit", "model_edit", "reason"), SKIP_CASES)
def test_an_event_the_import_cannot_represent_is_skipped_with_its_reason(
    tmp_path: Path,
    edit: Callable[[ET.Element], None],
    model_edit: Callable[[Path], None] | None,
    reason: str,
) -> None:
    report, model_root = _import(tmp_path, _export(tmp_path, edit, model_edit=model_edit))

    assert (EVENT_TAG, reason) in _event_entries(report)
    assert _events(model_root)[FILTER] == []
    # The neighbour through another port is imported as it always was.
    assert _events(model_root)[TRACK_REAR] == COMMITTED_EVENTS[TRACK_REAR]


def test_a_skipped_runnable_s_event_is_not_imported_and_keeps_today_s_line(tmp_path: Path) -> None:
    """A runnable the import skipped takes its events with it: every kind's line, unchanged."""

    def edit(root: ET.Element) -> None:
        _named(root, "RUNNABLE-ENTITY", FILTER).find(_q("MINIMUM-START-INTERVAL")).text = "-1"

    report, model_root = _import(tmp_path, _export(tmp_path, edit))

    assert FILTER not in _events(model_root)
    assert _event_entries(report) == [
        (
            EVENT_TAG,
            f"unresolved reference '{BEHAVIOUR_PATH}/{FILTER}' (runnable not found on SWC '{COMPONENT}')",
        )
    ]


class TestTheStubSeam:
    """The external stub seam, as the access entries and the operation event use it."""

    def test_an_event_through_a_mode_switch_stub_port_names_the_stub_written(self, tmp_path: Path) -> None:
        """The stub is minted and written; it lists no data element, so the line names it by its ``_ext`` name."""

        def edit(root: ET.Element) -> None:
            _set_iref(root, port=f"{COMPONENT_PATH}/{MODE_PORT}", target="/Foreign/ModeInterfaces/EcuModeIf/EcuMode")

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _event_entries(report) == [
            (
                EVENT_TAG,
                f"data-received event through port '{MODE_PORT}' whose interface is the external interface stub "
                "'EcuModeIf_ext0' (/Foreign/ModeInterfaces/EcuModeIf): the stub does not list the member "
                "/Foreign/ModeInterfaces/EcuModeIf/EcuMode names (a stub lists the operations or data elements the "
                "source names directly under its path, of the kind its DEST admits), so there is no member to "
                "resolve it against",
            )
        ]
        assert _events(model_root)[FILTER] == []
        assert "data_elements" not in _document(model_root, "interfaces", "EcuModeIf_ext0")

    def test_conflicting_dests_mint_no_stub_and_keep_the_path_wording(self, tmp_path: Path) -> None:
        """The mode port names the rear interface's path with another ``DEST``: no stub, the path's line."""

        def edit(root: ET.Element) -> None:
            tref = _named(root, "R-PORT-PROTOTYPE", MODE_PORT).find(_q("REQUIRED-INTERFACE-TREF"))
            assert tref is not None
            tref.text = "/Foreign/PortInterfaces/RearSpeedIf"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert "/Foreign/PortInterfaces/RearSpeedIf" in {record.target for record in report.dest_disagreements}
        assert (
            EVENT_TAG,
            f"data-received event through port '{REAR_PORT}' whose interface reference "
            "/Foreign/PortInterfaces/RearSpeedIf is not declared by this export",
        ) in _event_entries(report, REAR_LEFT_EVENT)
        assert _events(model_root)[TRACK_REAR] == []


def test_a_late_resolving_policy_records_a_missing_element_with_the_event_kind(tmp_path: Path) -> None:
    """``import component``'s policy: an element the file's interface lacks is a late-pass verdict, not a line.

    The record carries the event's tag, so the late pass phrases a data-received event apart from an
    operation event.
    """
    source = _export(tmp_path, lambda root: _set_iref(root, target="/PortInterfaces/WheelSpeedIf/Missing"))
    model = read_arxml(str(source))
    forms: list = []
    data_types, registry, _ = map_data_types(model, elements=forms)
    interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)

    (imported,), skip_log = map_swcs(
        model,
        interfaces,
        iface_skip_log=iface_skips,
        unresolved=UnresolvedReferences.for_component_import(model),
        port_inits={},
        variable_types={},
    )

    assert imported.behaviour_member_misses == [
        UnansweredRecord(
            runnable_name=FILTER,
            collection="events",
            port=WHEEL_PORT,
            member_name="Missing",
            is_operation=False,
            interface_name="WheelSpeedIf",
            tag=EVENT_TAG,
        )
    ]
    assert not [entry for entry in skip_log if entry.path == f"{BEHAVIOUR_PATH}/{FRONT_LEFT_EVENT}"]


def test_a_repeated_event_is_kept_as_the_source_states_it(tmp_path: Path) -> None:
    """One event repeated on one runnable is silence for ``check-model`` and imported as it is."""
    source = _export(tmp_path, model_edit=_add_runnable_event(FILTER, data_event(WHEEL_PORT, FRONT_LEFT)))

    report, model_root = _import(tmp_path, source)

    assert _entries(report) == []
    assert _events(model_root)[FILTER] == [data_event(WHEEL_PORT, FRONT_LEFT), data_event(WHEEL_PORT, FRONT_LEFT)]
