"""The reader and the importer of implicit access and asynchronous calls.

The synthetic export is the writer's own output of ``fixtures/access_forms/model``, changed case by case
by an ElementTree edit of the file or a change to the model copy before it is written, so every case starts
from a document the exporter wrote and states the one difference it is about. Each case runs the whole of
``import model`` (``run_import``) into a fresh project; the reader is asked directly only where the case
is its alone.

Does not reach: real tool exports; ``import component``'s regimes and markers; a call through a port whose
interface the file does not declare, which keeps its synchrony and timeout but whose result point is a line
(:func:`runnable_accesses._result_point_call`).
"""

from __future__ import annotations

import copy
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from external_stubs_support import change
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.report import ImportReport
from swcraft_tools.arxml_reader import SkipCategory, StandardRule, read_arxml
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml

FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
NS = "http://autosar.org/schema/r4.0"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", NS)
ET.register_namespace("xsi", XSI)

VERSIONS = ("4.2.2", "4.3.0")
CONTROLLER = "BrakeController"
CONTROLLER_FILE = "swc/chassis/BrakeController.yaml"
COMPUTE = "ComputeBrakeTorque"
ON_WRITTEN = "OnBlockWritten"
DIAGNOSE = "Diagnose"
COMPONENT_PATH = f"/ComponentTypes/{CONTROLLER}"
BEHAVIOUR = f"{COMPONENT_PATH}/{CONTROLLER}_InternalBehavior"
RESULT_POINT = "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT"
RESULT_POINTS = "ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS"
RETURNS_EVENT = "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT"
ASYNC_POINT = "ASYNCHRONOUS-SERVER-CALL-POINT"
SYNC_POINT = "SYNCHRONOUS-SERVER-CALL-POINT"
#: The names the writer derives for the fixture's new elements.
POLL_RESULT = "Result_rCalibration_ReadBlock"
EVENT_RESULT = "Result_rNvStorage_WriteBlock"
RETURNS = "ASRT_rNvStorage_WriteBlock"
WRITE_CALL = "Call_rNvStorage_WriteBlock"
READ_CALL = "Call_rCalibration_ReadBlock"
VERSION_CALL = "Call_rCalibration_GetVersion"


def _committed() -> dict[str, Any]:
    return yaml.safe_load((FIXTURE / CONTROLLER_FILE).read_text(encoding="utf-8"))


#: What the fixture's controller holds, as committed.
COMMITTED_RUNNABLES: list[dict[str, Any]] = _committed()["runnables"]


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _named(root: ET.Element, tag: str, name: str) -> ET.Element:
    """The one *tag* element of *root* whose ``SHORT-NAME`` is *name*."""
    found = [el for el in root.iter(_q(tag)) if el.findtext(_q("SHORT-NAME")) == name]
    assert len(found) == 1, f"expected one {tag} named {name!r}, found {len(found)}"
    return found[0]


def _controller(root: ET.Element) -> ET.Element:
    return _named(root, "APPLICATION-SW-COMPONENT-TYPE", CONTROLLER)


def _runnable_el(root: ET.Element, name: str) -> ET.Element:
    return _named(_controller(root), "RUNNABLE-ENTITY", name)


def _rename(element: ET.Element, name: str) -> None:
    short = element.find(_q("SHORT-NAME"))
    assert short is not None
    short.text = name


def _export(
    tmp_path: Path,
    edit: Callable[[ET.Element], None] | None = None,
    *,
    model_edit: Callable[[Path], None] | None = None,
    version: str = "4.2.2",
) -> Path:
    """The writer's output of the fixture, its model changed by *model_edit* and its file by *edit*."""
    model_root = tmp_path / "model"
    shutil.copytree(FIXTURE, model_root)
    if model_edit is not None:
        model_edit(model_root)
    source = tmp_path / "source.arxml"
    generate_arxml(str(model_root), str(source), version=version)
    if edit is not None:
        tree = ET.parse(source)
        edit(tree.getroot())
        tree.write(source, encoding="utf-8", xml_declaration=True)
    return source


def _import(tmp_path: Path, source: Path) -> tuple[ImportReport, Path]:
    project = init_project(tmp_path / "project")
    report = run_import(str(source), str(project / "swcraft.yaml"))
    return report, project / "model"


def _document(model_root: Path, name: str = CONTROLLER) -> dict[str, Any]:
    (path,) = list((model_root / "swc").rglob(f"{name}.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _runnables(model_root: Path, name: str = CONTROLLER) -> dict[str, dict[str, Any]]:
    return {runnable["name"]: runnable for runnable in _document(model_root, name)["runnables"]}


def _entries(report: ImportReport) -> list[tuple[str, str, str]]:
    """Every entry but the component implementation's, which no case is about."""
    return [(entry.path, entry.tag, entry.reason) for entry in report.skip_entries if entry.tag != "SWC-IMPLEMENTATION"]


def _at(report: ImportReport, path: str) -> list[tuple[str, str]]:
    return [(tag, reason) for at, tag, reason in _entries(report) if at == path]


def _collapses(report: ImportReport) -> list[str]:
    return list(report.access_collapse_lines)


def _call(runnable: dict[str, Any], port: str, operation: str) -> dict[str, Any]:
    (call,) = [call for call in runnable.get("calls", []) if (call["port"], call["operation"]) == (port, operation)]
    return call


def _drop_returns_event(root: ET.Element) -> None:
    """The returns event and its result point out of the file: the asynchronous call keeps no result."""
    events = _controller(root).find(f".//{_q('EVENTS')}")
    assert events is not None
    events.remove(_named(events, RETURNS_EVENT, RETURNS))
    runnable = _runnable_el(root, ON_WRITTEN)
    wrapper = runnable.find(_q(RESULT_POINTS))
    assert wrapper is not None
    runnable.remove(wrapper)


# The writer's own output comes back as it was written


@pytest.mark.parametrize("version", VERSIONS)
class TestTheWritersOwnOutputComesBack:
    def test_every_form_comes_back_with_no_line(self, tmp_path: Path, version: str) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, version=version))

        assert _entries(report) == []
        assert _collapses(report) == []
        assert _document(model_root)["runnables"] == COMMITTED_RUNNABLES
        (sample,) = _document(model_root, "WheelSensor")["runnables"]
        assert sample["writes"] == [{"port": "pWheelSpeed", "element": "speed", "by": "implicit"}]

    def test_the_round_trip_writes_every_form_back(self, tmp_path: Path, version: str) -> None:
        """An import followed by ``generate arxml`` writes the source's accesses, result points and events again."""
        source = _export(tmp_path, version=version)
        _report, model_root = _import(tmp_path, source)
        assert check_cross_layer_consistency(str(model_root), version).consistent
        again = tmp_path / "again.arxml"
        generate_arxml(str(model_root), str(again), version=version)

        def facts(path: Path) -> list[tuple[Any, ...]]:
            swcs = {swc.name: swc for swc in read_arxml(str(path)).swcs}
            return [
                *(
                    (swc, runnable.name, "access", a.kind, a.tag, a.name, a.port_ref, a.target_ref, a.timeout_text)
                    for swc in sorted(swcs)
                    for runnable in swcs[swc].runnables
                    for a in runnable.accesses
                ),
                *(
                    (swc, runnable.name, "result", p.name, p.call_point_ref, p.call_point_dest)
                    for swc in sorted(swcs)
                    for runnable in swcs[swc].runnables
                    for p in runnable.result_points
                ),
                *(
                    (swc, "event", e.kind, e.name, e.runnable_ref, e.event_source_ref, e.event_source_dest)
                    for swc in sorted(swcs)
                    for e in swcs[swc].events
                ),
            ]

        source_facts = facts(source)
        assert any(fact[2] == "result" for fact in source_facts)
        assert any(fact[2] == RETURNS_EVENT for fact in source_facts)
        assert facts(again) == source_facts


# The reader


class TestTheReader:
    def test_the_six_containers_and_the_timeout_are_read(self, tmp_path: Path) -> None:
        (swc,) = [swc for swc in read_arxml(str(_export(tmp_path))).swcs if swc.name == CONTROLLER]
        compute = next(runnable for runnable in swc.runnables if runnable.name == COMPUTE)

        assert [(a.kind, a.tag, a.name, a.timeout_text) for a in compute.accesses] == [
            ("DATA-READ-ACCESSS", "VARIABLE-ACCESS", "R_rWheelSpeed_speed", None),
            ("DATA-RECEIVE-POINT-BY-ARGUMENTS", "VARIABLE-ACCESS", "R_rPedal_position", None),
            ("DATA-WRITE-ACCESSS", "VARIABLE-ACCESS", "W_pBrakeTorque_torque", None),
            ("SERVER-CALL-POINTS", ASYNC_POINT, WRITE_CALL, None),
            ("SERVER-CALL-POINTS", ASYNC_POINT, READ_CALL, "0.5"),
        ]
        diagnose = next(runnable for runnable in swc.runnables if runnable.name == DIAGNOSE)
        assert [(a.tag, a.timeout_text) for a in diagnose.accesses if a.kind == "SERVER-CALL-POINTS"] == [
            (SYNC_POINT, "0")
        ]

    def test_the_result_points_and_the_returns_event_are_read_with_their_references(self, tmp_path: Path) -> None:
        (swc,) = [swc for swc in read_arxml(str(_export(tmp_path))).swcs if swc.name == CONTROLLER]
        points = {
            runnable.name: [
                (p.path, p.name, p.tag, p.call_point_ref, p.call_point_dest) for p in runnable.result_points
            ]
            for runnable in swc.runnables
        }

        assert points == {
            COMPUTE: [
                (
                    f"{BEHAVIOUR}/{COMPUTE}/{POLL_RESULT}",
                    POLL_RESULT,
                    RESULT_POINT,
                    f"{BEHAVIOUR}/{COMPUTE}/{READ_CALL}",
                    ASYNC_POINT,
                )
            ],
            ON_WRITTEN: [
                (
                    f"{BEHAVIOUR}/{ON_WRITTEN}/{EVENT_RESULT}",
                    EVENT_RESULT,
                    RESULT_POINT,
                    f"{BEHAVIOUR}/{COMPUTE}/{WRITE_CALL}",
                    ASYNC_POINT,
                )
            ],
            DIAGNOSE: [],
        }
        (event,) = [event for event in swc.events if event.kind == RETURNS_EVENT]
        assert (event.name, event.runnable_ref, event.event_source_ref, event.event_source_dest) == (
            RETURNS,
            f"{BEHAVIOUR}/{ON_WRITTEN}",
            f"{BEHAVIOUR}/{ON_WRITTEN}/{EVENT_RESULT}",
            RESULT_POINT,
        )

    def test_an_unrecognized_child_of_the_result_points_is_its_line(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            wrapper = _runnable_el(root, COMPUTE).find(_q(RESULT_POINTS))
            assert wrapper is not None
            ET.SubElement(ET.SubElement(wrapper, _q("WAIT-POINT")), _q("SHORT-NAME")).text = "Waiting"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _at(report, f"{BEHAVIOUR}/{COMPUTE}/Waiting") == [
            (
                "WAIT-POINT",
                f"unrecognized result point kind inside {RESULT_POINTS} (only {RESULT_POINT} is read)",
            )
        ]
        assert _runnables(model_root)[COMPUTE] == COMMITTED_RUNNABLES[0]

    def test_a_variation_point_and_a_description_on_a_result_point_are_one_line_each(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            point = _named(_runnable_el(root, COMPUTE), RESULT_POINT, POLL_RESULT)
            ET.SubElement(ET.SubElement(point, _q("DESC")), _q("L-2"), {"L": "FOR-ALL"}).text = "Polled."
            ET.SubElement(ET.SubElement(point, _q("VARIATION-POINT")), _q("SHORT-LABEL")).text = "vp"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert [tag for tag, _reason in _at(report, f"{BEHAVIOUR}/{COMPUTE}/{POLL_RESULT}")] == [
            "VARIATION-POINT",
            "DESC",
        ]
        assert _call(_runnables(model_root)[COMPUTE], "rCalibration", "ReadBlock")["poll"] is True


# The mapping: forms, collapse, the two refused combinations


def _move_access(runnable: str, name: str, source: str, target: str) -> Callable[[ET.Element], None]:
    """Move the access *name* of *runnable* from its *source* container into a new *target* one."""

    def edit(root: ET.Element) -> None:
        element = _runnable_el(root, runnable)
        container = element.find(_q(source))
        assert container is not None
        access = _named(container, "VARIABLE-ACCESS", name)
        container.remove(access)
        if not list(container):
            element.remove(container)
        wrapper = element.find(_q(target))
        if wrapper is None:
            wrapper = ET.SubElement(element, _q(target))
        wrapper.append(access)

    return edit


class TestTheMapping:
    def test_a_pair_in_two_forms_is_collapsed_into_the_first_with_a_line(self, tmp_path: Path) -> None:
        """Schema order puts the implicit read first: it is kept, and the explicit one is the collapse line."""

        def edit(root: ET.Element) -> None:
            runnable = _runnable_el(root, COMPUTE)
            implicit = _named(runnable.find(_q("DATA-READ-ACCESSS")), "VARIABLE-ACCESS", "R_rWheelSpeed_speed")
            twin = copy.deepcopy(implicit)
            _rename(twin, "ExplicitSpeed")
            arguments = runnable.find(_q("DATA-RECEIVE-POINT-BY-ARGUMENTS"))
            assert arguments is not None
            arguments.append(twin)

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _collapses(report) == [
            f"{CONTROLLER}/{COMPUTE}: reads access rWheelSpeed.speed declared twice -- 'ExplicitSpeed' collapsed "
            "into 'R_rWheelSpeed_speed'"
        ]
        assert _runnables(model_root)[COMPUTE]["reads"] == COMMITTED_RUNNABLES[0]["reads"]

    def test_an_implicit_read_of_a_queued_element_is_imported_with_its_line(self, tmp_path: Path) -> None:
        """``[constr_2020]``: imported as the file states it, the line says check-model refuses it."""

        def model_edit(model_root: Path) -> None:
            def queued(document: dict[str, Any]) -> None:
                document["data_elements"][0]["sw_impl_policy"] = "queued"

            def receiver(document: dict[str, Any]) -> None:
                # A queued receiver states its queue length, and Diagnose's by-value read becomes by argument,
                # so the writer writes the model before the edit moves ComputeBrakeTorque's read.
                (port,) = [port for port in document["ports"] if port["name"] == "rPedal"]
                port["com_specs"] = [{"data_element": "position", "queue_length": 1}]
                (diagnose,) = [r for r in document["runnables"] if r["name"] == DIAGNOSE]
                del diagnose["reads"][1]["by"]

            change(model_root, "interfaces/PedalIf.yaml", queued)
            change(model_root, CONTROLLER_FILE, receiver)

        edit = _move_access(COMPUTE, "R_rPedal_position", "DATA-RECEIVE-POINT-BY-ARGUMENTS", "DATA-READ-ACCESSS")
        source = _export(tmp_path, edit, model_edit=model_edit)
        report, model_root = _import(tmp_path, source)

        (entry,) = [e for e in report.skip_entries if e.path == f"{BEHAVIOUR}/{COMPUTE}/R_rPedal_position"]
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.NO_QUEUED_IMPLICIT_READ)
        assert "rule:swc.access.queued-implicit-read" in entry.reason
        reads = _runnables(model_root)[COMPUTE]["reads"]
        assert {"port": "rPedal", "element": "position", "by": "implicit"} in reads
        rules = {issue.rule for issue in check_cross_layer_consistency(str(model_root)).records}
        assert "swc.access.queued-implicit-read" in rules

    def test_an_implicit_read_of_an_enable_update_element_is_imported_with_its_line(self, tmp_path: Path) -> None:
        """``[constr_1103]``: the port's com-spec states ENABLE-UPDATE true for the element read implicitly."""

        def model_edit(model_root: Path) -> None:
            def enable(document: dict[str, Any]) -> None:
                (port,) = [port for port in document["ports"] if port["name"] == "rPedal"]
                port["com_specs"] = [{"data_element": "position", "enable_update": True}]

            change(model_root, CONTROLLER_FILE, enable)

        edit = _move_access(COMPUTE, "R_rPedal_position", "DATA-RECEIVE-POINT-BY-ARGUMENTS", "DATA-READ-ACCESSS")
        report, model_root = _import(tmp_path, _export(tmp_path, edit, model_edit=model_edit))

        (entry,) = [e for e in report.skip_entries if e.path == f"{BEHAVIOUR}/{COMPUTE}/R_rPedal_position"]
        assert (entry.category, entry.rule) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.NO_ENABLE_UPDATE_FOR_AN_IMPLICIT_READ,
        )
        assert {"port": "rPedal", "element": "position", "by": "implicit"} in _runnables(model_root)[COMPUTE]["reads"]
        rules = {issue.rule for issue in check_cross_layer_consistency(str(model_root)).records}
        assert "swc.access.implicit-read-enable-update" in rules

    def test_stated_result_point_and_event_names_are_kept(self, tmp_path: Path) -> None:
        def edit(root: ET.Element) -> None:
            _rename(_named(_runnable_el(root, COMPUTE), RESULT_POINT, POLL_RESULT), "CalibrationResult")
            on_written = _runnable_el(root, ON_WRITTEN)
            _rename(_named(on_written, RESULT_POINT, EVENT_RESULT), "StoreDone")
            event = _named(_controller(root), RETURNS_EVENT, RETURNS)
            _rename(event, "ASRT_StoreDone")
            source = event.find(_q("EVENT-SOURCE-REF"))
            assert source is not None
            source.text = f"{BEHAVIOUR}/{ON_WRITTEN}/StoreDone"

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        assert _entries(report) == []
        runnables = _runnables(model_root)
        assert _call(runnables[COMPUTE], "rCalibration", "ReadBlock")["result"] == "CalibrationResult"
        assert runnables[ON_WRITTEN]["events"] == [
            {
                "type": "call_returned",
                "port": "rNvStorage",
                "operation": "WriteBlock",
                "result": "StoreDone",
                "name": "ASRT_StoreDone",
            }
        ]

    def test_a_result_point_name_shared_with_an_access_point_is_not_stated_and_says_why(self, tmp_path: Path) -> None:
        """The result points share the runnable's access point scope, so the settling pass declines the name."""

        def edit(root: ET.Element) -> None:
            _rename(_named(_runnable_el(root, COMPUTE), RESULT_POINT, POLL_RESULT), "r_rwheelspeed_speed")

        report, model_root = _import(tmp_path, _export(tmp_path, edit))

        (entry,) = [e for e in report.skip_entries if e.path == f"{BEHAVIOUR}/{COMPUTE}/r_rwheelspeed_speed"]
        assert entry.tag == "SHORT-NAME"
        assert entry.reason.startswith(f"SHORT-NAME of a result point ({RESULT_POINT}) not carried")
        assert "would collide with the access point SWCraft writes as 'R_rWheelSpeed_speed'" in entry.reason
        assert "result" not in _call(_runnables(model_root)[COMPUTE], "rCalibration", "ReadBlock")


# Shapes the standard allows: a polled result point, no returns event, no TIMEOUT


def _poll_without_returns_event_shapes(root: ET.Element) -> None:
    """Every result point a poll named otherwise, no returns event, no TIMEOUT on an asynchronous point."""
    _drop_returns_event(root)
    _rename(_named(_runnable_el(root, COMPUTE), RESULT_POINT, POLL_RESULT), "AsyncResult_ReadBlock")
    point = _named(_runnable_el(root, COMPUTE), ASYNC_POINT, READ_CALL)
    timeout = point.find(_q("TIMEOUT"))
    assert timeout is not None
    point.remove(timeout)


_WRITE_WITHOUT_RESULT = (
    f"SWC '{CONTROLLER}' runnable '{COMPUTE}' calls operation 'WriteBlock' of port 'rNvStorage' asynchronously, "
    "and nothing fetches its result; at AUTOSAR 4.2.2 an asynchronous call point is referenced by exactly one "
    "result point ([constr_2006]), where 4.3.0 allows none. State `poll: true` on the call, add a "
    "`call_returned` event naming it to the runnable that takes the result, or generate at 4.3.0. -- imported "
    "as the file states it; the model is one check-model refuses (rule:swc.call.async-without-result)"
)


@pytest.mark.parametrize("version", VERSIONS)
def test_a_poll_without_a_returns_event_is_a_poll_with_its_stated_result_and_no_timeout(
    tmp_path: Path, version: str
) -> None:
    report, model_root = _import(tmp_path, _export(tmp_path, _poll_without_returns_event_shapes, version=version))

    # A 4.2.2 file states the call with no result point, its own [constr_2006]: one section-1 line;
    # a 4.3.0 file allows it.
    expected = [_WRITE_WITHOUT_RESULT] if version == "4.2.2" else []
    assert [(entry.category, entry.rule) for entry in report.skip_entries if entry.tag != "SWC-IMPLEMENTATION"] == [
        (SkipCategory.EXPORT_DEFECT, StandardRule.ONE_RESULT_POINT_PER_CALL)
    ] * len(expected)
    assert [reason for _path, _tag, reason in _entries(report)] == expected
    compute = _runnables(model_root)[COMPUTE]
    assert compute["calls"] == [
        {"port": "rNvStorage", "operation": "WriteBlock", "asynchronous": True},
        {
            "port": "rCalibration",
            "operation": "ReadBlock",
            "asynchronous": True,
            "poll": True,
            "result": "AsyncResult_ReadBlock",
        },
    ]
    assert _runnables(model_root)[ON_WRITTEN]["events"] == []


class TestAnAsynchronousCallWithNoResult:
    """Findings are not fixed by the importer: a call with no result is imported as stated."""

    def test_it_is_refused_by_check_model_at_4_2_2_and_accepted_at_4_3_0(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _drop_returns_event, version="4.3.0"))

        assert _entries(report) == []
        assert _call(_runnables(model_root)[COMPUTE], "rNvStorage", "WriteBlock") == {
            "port": "rNvStorage",
            "operation": "WriteBlock",
            "asynchronous": True,
        }
        at_4_2_2 = check_cross_layer_consistency(str(model_root), "4.2.2")
        assert [issue.rule for issue in at_4_2_2.records] == ["swc.call.async-without-result"]
        assert check_cross_layer_consistency(str(model_root), "4.3.0").consistent


# The timeout


def _set_timeout(point_name: str, text: str | None, tag: str = ASYNC_POINT) -> Callable[[ET.Element], None]:
    def edit(root: ET.Element) -> None:
        point = _named(_controller(root), tag, point_name)
        existing = point.find(_q("TIMEOUT"))
        if existing is not None:
            point.remove(existing)
        if text is not None:
            ET.SubElement(point, _q("TIMEOUT")).text = text

    return edit


class TestTheTimeout:
    @pytest.mark.parametrize(("text", "carried"), [("0.5", 0.5), ("0", 0.0), (" 2E-3 ", 0.002), (None, None)])
    def test_an_asynchronous_point_s_timeout_is_carried_exactly_and_its_absence_as_absence(
        self, tmp_path: Path, text: str | None, carried: float | None
    ) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _set_timeout(WRITE_CALL, text)))

        assert _entries(report) == []
        assert _call(_runnables(model_root)[COMPUTE], "rNvStorage", "WriteBlock").get("timeout") == carried

    @pytest.mark.parametrize("text", ["soon", "-1", "INF"])
    def test_an_unreadable_or_negative_timeout_is_one_line_and_the_call_is_kept(
        self, tmp_path: Path, text: str
    ) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _set_timeout(WRITE_CALL, text)))

        assert _at(report, f"{BEHAVIOUR}/{COMPUTE}/{WRITE_CALL}") == [
            (
                "TIMEOUT",
                f"TIMEOUT {text} of an {ASYNC_POINT} not carried: it reads as no non-negative number of seconds, "
                "and the call is imported with no timeout",
            )
        ]
        assert "timeout" not in _call(_runnables(model_root)[COMPUTE], "rNvStorage", "WriteBlock")

    @pytest.mark.parametrize(("text", "line"), [("0.1", True), ("0", False), ("0.0", False), (None, False)])
    def test_a_synchronous_point_s_timeout_other_than_0_is_one_line(
        self, tmp_path: Path, text: str | None, line: bool
    ) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _set_timeout(VERSION_CALL, text, SYNC_POINT)))

        expected = (
            [
                (
                    "TIMEOUT",
                    f"TIMEOUT {text} of a {SYNC_POINT} not carried: SWCraft writes a synchronous call point's "
                    "TIMEOUT as 0",
                )
            ]
            if line
            else []
        )
        assert _at(report, f"{BEHAVIOUR}/{DIAGNOSE}/{VERSION_CALL}") == expected
        assert _call(_runnables(model_root)[DIAGNOSE], "rCalibration", "GetVersion") == {
            "port": "rCalibration",
            "operation": "GetVersion",
        }


# Binding by exact path through the kept call


def test_a_result_point_naming_a_discarded_duplicate_binds_to_the_kept_call_with_a_line(tmp_path: Path) -> None:
    """The collapse keeps the first call point of a pair; a result point naming the second binds to the first."""

    def edit(root: ET.Element) -> None:
        runnable = _runnable_el(root, COMPUTE)
        calls = runnable.find(_q("SERVER-CALL-POINTS"))
        assert calls is not None
        twin = copy.deepcopy(_named(calls, ASYNC_POINT, READ_CALL))
        _rename(twin, "SecondReadBlock")
        calls.append(twin)
        reference = _named(runnable, RESULT_POINT, POLL_RESULT).find(_q("ASYNCHRONOUS-SERVER-CALL-POINT-REF"))
        assert reference is not None
        reference.text = f"{BEHAVIOUR}/{COMPUTE}/SecondReadBlock"

    report, model_root = _import(tmp_path, _export(tmp_path, edit))

    assert _collapses(report) == [
        f"{CONTROLLER}/{COMPUTE}: calls access rCalibration.ReadBlock declared twice -- 'SecondReadBlock' collapsed "
        f"into '{READ_CALL}'",
        f"{CONTROLLER}/{COMPUTE}: result point '{POLL_RESULT}' references call point 'SecondReadBlock', collapsed "
        f"into '{READ_CALL}' -- bound to it",
    ]
    assert _runnables(model_root)[COMPUTE] == COMMITTED_RUNNABLES[0]


# What the model cannot represent: each skip class, with its reason


def _event_el(root: ET.Element) -> ET.Element:
    return _named(_controller(root), RETURNS_EVENT, RETURNS)


def _no_start(root: ET.Element) -> None:
    event = _event_el(root)
    event.remove(event.find(_q("START-ON-EVENT-REF")))


def _disabled_mode(root: ET.Element) -> None:
    event = _event_el(root)
    irefs = ET.Element(_q("DISABLED-MODE-IREFS"))
    iref = ET.SubElement(irefs, _q("DISABLED-MODE-IREF"))
    ET.SubElement(iref, _q("CONTEXT-PORT-REF"), {"DEST": "R-PORT-PROTOTYPE"}).text = f"{COMPONENT_PATH}/rMode"
    event.insert(1, irefs)


def _dangling_source(root: ET.Element) -> None:
    source = _event_el(root).find(_q("EVENT-SOURCE-REF"))
    source.text = f"{BEHAVIOUR}/{ON_WRITTEN}/Nowhere"


def _no_source(root: ET.Element) -> None:
    event = _event_el(root)
    event.remove(event.find(_q("EVENT-SOURCE-REF")))


def _starts_another_runnable(root: ET.Element) -> None:
    """The event starts ``Diagnose``; its result point stays on ``OnBlockWritten``."""
    reference = _event_el(root).find(_q("START-ON-EVENT-REF"))
    reference.text = f"{BEHAVIOUR}/{DIAGNOSE}"


def _event_result_reference(root: ET.Element) -> ET.Element:
    point = _named(_runnable_el(root, ON_WRITTEN), RESULT_POINT, EVENT_RESULT)
    reference = point.find(_q("ASYNCHRONOUS-SERVER-CALL-POINT-REF"))
    assert reference is not None
    return reference


def _result_names_a_synchronous_point(root: ET.Element) -> None:
    _event_result_reference(root).text = f"{BEHAVIOUR}/{DIAGNOSE}/{VERSION_CALL}"


def _result_names_another_behaviour(root: ET.Element) -> None:
    _event_result_reference(root).text = "/ComponentTypes/NvStorage/NvStorage_InternalBehavior/X/Call"


def _result_has_no_reference(root: ET.Element) -> None:
    point = _named(_runnable_el(root, ON_WRITTEN), RESULT_POINT, EVENT_RESULT)
    point.remove(point.find(_q("ASYNCHRONOUS-SERVER-CALL-POINT-REF")))


EVENT_PATH = f"{BEHAVIOUR}/{RETURNS}"
EVENT_RESULT_PATH = f"{BEHAVIOUR}/{ON_WRITTEN}/{EVENT_RESULT}"


@pytest.mark.parametrize(
    ("edit", "event_reason", "result_line"),
    [
        (
            _no_start,
            "returns event has no START-ON-EVENT-REF: the event a WaitPoint waits for ([TPS_SWCT_01347]) -- "
            "SWCraft models no wait point, so a blocking fetch of the result is not carried",
            "chained",
        ),
        (
            _disabled_mode,
            "returns event states 1 disabled mode(s): a call_returned event holds none, and the event without "
            "them would start its runnable in modes the file disables",
            "chained",
        ),
        # Named by no event's source, the result point is a poll candidate -- of another runnable's call.
        (_no_source, "returns event has no EVENT-SOURCE-REF", "poll of another"),
        (
            _dangling_source,
            f"returns event references {BEHAVIOUR}/{ON_WRITTEN}/Nowhere, which is no result point of a runnable "
            f"of its own behaviour {BEHAVIOUR}",
            "poll of another",
        ),
        (
            _starts_another_runnable,
            f"returns event starts runnable '{DIAGNOSE}' and its result point belongs to runnable '{ON_WRITTEN}': "
            "SWCraft models the returns event that starts the runnable owning its result point",
            "chained",
        ),
        (
            _result_names_a_synchronous_point,
            "chain-skipped: its result point",
            f"result point references {BEHAVIOUR}/{DIAGNOSE}/{VERSION_CALL}, a SYNCHRONOUS-SERVER-CALL-POINT: "
            "only an asynchronous call has a result point",
        ),
        (
            _result_names_another_behaviour,
            "chain-skipped: its result point",
            "result point references /ComponentTypes/NvStorage/NvStorage_InternalBehavior/X/Call, which is no call "
            f"point of a runnable of its own behaviour {BEHAVIOUR}",
        ),
        (
            _result_has_no_reference,
            "chain-skipped: its result point",
            "result point has no ASYNCHRONOUS-SERVER-CALL-POINT-REF",
        ),
    ],
    ids=[
        "wait-point-event",
        "disabled-mode",
        "no-source",
        "dangling-source",
        "another-runnable",
        "synchronous-call",
        "another-behaviour",
        "no-reference",
    ],
)
def test_an_event_or_result_point_the_model_cannot_represent_is_skipped_with_its_reason(
    tmp_path: Path, edit: Callable[[ET.Element], None], event_reason: str, result_line: str
) -> None:
    report, model_root = _import(tmp_path, _export(tmp_path, edit))

    (event_line,) = _at(report, EVENT_PATH)
    assert event_line[0] == RETURNS_EVENT
    assert event_line[1].startswith(event_reason)
    (point_tag, point_reason) = _at(report, EVENT_RESULT_PATH)[0]
    assert point_tag == RESULT_POINT
    if result_line == "chained":
        assert point_reason == f"chain-skipped: its returns event '{RETURNS}' was not carried ({event_line[1]})"
    elif result_line == "poll of another":
        assert point_reason.startswith(f"result point of runnable '{ON_WRITTEN}' references a call point of runnable")
    else:
        assert point_reason == result_line
    runnables = _runnables(model_root)
    assert all(event.get("type") != "call_returned" for r in runnables.values() for event in r.get("events", []))
    # The call stays asynchronous, with no result: check-model's finding at 4.2.2, never fixed here.
    assert _call(runnables[COMPUTE], "rNvStorage", "WriteBlock") == {
        "port": "rNvStorage",
        "operation": "WriteBlock",
        "asynchronous": True,
    }


def test_a_poll_of_another_runnable_s_call_is_skipped_with_its_reason(tmp_path: Path) -> None:
    """A result point no returns event names, on a runnable that does not own the call point."""

    def edit(root: ET.Element) -> None:
        compute = _runnable_el(root, COMPUTE)
        wrapper = compute.find(_q(RESULT_POINTS))
        compute.remove(wrapper)
        diagnose = _runnable_el(root, DIAGNOSE)
        diagnose.insert(2, wrapper)

    report, model_root = _import(tmp_path, _export(tmp_path, edit))

    assert _at(report, f"{BEHAVIOUR}/{DIAGNOSE}/{POLL_RESULT}") == [
        (
            RESULT_POINT,
            f"result point of runnable '{DIAGNOSE}' references a call point of runnable '{COMPUTE}' and no returns "
            "event names it: a poll of another runnable's call is not expressible -- SWCraft models a poll on the "
            "calling runnable only",
        )
    ]
    call = _call(_runnables(model_root)[COMPUTE], "rCalibration", "ReadBlock")
    assert "poll" not in call


def test_a_second_result_point_of_one_call_is_skipped(tmp_path: Path) -> None:
    def edit(root: ET.Element) -> None:
        wrapper = _runnable_el(root, COMPUTE).find(_q(RESULT_POINTS))
        twin = copy.deepcopy(_named(wrapper, RESULT_POINT, POLL_RESULT))
        _rename(twin, "SecondResult")
        wrapper.append(twin)

    report, model_root = _import(tmp_path, _export(tmp_path, edit))

    assert _at(report, f"{BEHAVIOUR}/{COMPUTE}/SecondResult") == [
        (
            RESULT_POINT,
            f"a second result point of the call rCalibration.ReadBlock on runnable '{COMPUTE}' not carried: a call "
            "holds one poll",
        )
    ]
    assert _call(_runnables(model_root)[COMPUTE], "rCalibration", "ReadBlock")["poll"] is True


# Two asynchronous callers of one pair: a call_returned event names the pair, not the caller

DIAGNOSE_CALL_PATH = f"{BEHAVIOUR}/{DIAGNOSE}/{WRITE_CALL}"
DIAGNOSE_RESULT_PATH = f"{BEHAVIOUR}/{DIAGNOSE}/{EVENT_RESULT}"
SECOND_RETURNS = "ASRT_Diagnose_WriteBlock"
AMBIGUOUS = (
    f"runnables '{COMPUTE}' and '{DIAGNOSE}' call rNvStorage.WriteBlock asynchronously; a call_returned event "
    "names the pair, not the caller, so the event is not carried (rule:swc.call-returned-event.ambiguous-call)"
)


def _a_second_caller(root: ET.Element) -> None:
    """``Diagnose`` calls ``rNvStorage.WriteBlock`` asynchronously too: the writer's call point, copied.

    Valid at both editions: ``[constr_2006]`` counts result points per call point, and neither edition
    limits an operation's asynchronous call points to one runnable.
    """
    call = copy.deepcopy(_named(_runnable_el(root, COMPUTE), ASYNC_POINT, WRITE_CALL))
    points = _runnable_el(root, DIAGNOSE).find(_q("SERVER-CALL-POINTS"))
    assert points is not None
    points.append(call)


def _a_second_caller_with_a_result_point(root: ET.Element) -> None:
    """:func:`_a_second_caller`, and ``Diagnose`` owning a result point of its own call: valid at 4.2.2."""
    _a_second_caller(root)
    point = copy.deepcopy(_named(_runnable_el(root, ON_WRITTEN), RESULT_POINT, EVENT_RESULT))
    reference = point.find(_q("ASYNCHRONOUS-SERVER-CALL-POINT-REF"))
    assert reference is not None
    reference.text = DIAGNOSE_CALL_PATH
    wrapper = ET.Element(_q(RESULT_POINTS))
    wrapper.append(point)
    _runnable_el(root, DIAGNOSE).insert(2, wrapper)


def _two_returns_events(root: ET.Element) -> None:
    """Each caller's result taken by a returns event: ``Diagnose``'s starts ``Diagnose`` itself."""
    _a_second_caller_with_a_result_point(root)
    event = copy.deepcopy(_event_el(root))
    _rename(event, SECOND_RETURNS)
    start = event.find(_q("START-ON-EVENT-REF"))
    source = event.find(_q("EVENT-SOURCE-REF"))
    assert start is not None and source is not None
    start.text = f"{BEHAVIOUR}/{DIAGNOSE}"
    source.text = DIAGNOSE_RESULT_PATH
    events = _controller(root).find(f".//{_q('EVENTS')}")
    assert events is not None
    events.append(event)


def _chained_to(event: str, reason: str) -> tuple[str, str]:
    return (RESULT_POINT, f"chain-skipped: its returns event '{event}' was not carried ({reason})")


def _rules(model_root: Path, version: str) -> list[str]:
    return [issue.rule for issue in check_cross_layer_consistency(str(model_root), version).records]


class TestAReturnsEventOfACallTwoRunnablesMake:
    """A returns event of a pair two runnables call asynchronously is skipped with its reason, never bound.

    A ``call_returned`` event names ``(port, operation)``, not the caller
    (``swc.call-returned-event.ambiguous-call``). Its result point is never turned into a poll either: a
    returns event starts a runnable when the call completes (``[TPS_SWCT_01346]``), a poll states no event.
    """

    def test_two_callers_and_one_returns_event(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _a_second_caller, version="4.3.0"))

        assert _at(report, EVENT_PATH) == [(RETURNS_EVENT, AMBIGUOUS)]
        assert _at(report, EVENT_RESULT_PATH) == [_chained_to(RETURNS, AMBIGUOUS)]
        runnables = _runnables(model_root)
        assert runnables[ON_WRITTEN]["events"] == []
        expected = {"port": "rNvStorage", "operation": "WriteBlock", "asynchronous": True}
        assert _call(runnables[COMPUTE], "rNvStorage", "WriteBlock") == expected
        assert _call(runnables[DIAGNOSE], "rNvStorage", "WriteBlock") == expected
        entry = next(e for e in report.skip_entries if e.path == EVENT_PATH)
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)
        assert _rules(model_root, "4.3.0") == []
        assert "swc.call-returned-event.ambiguous-call" not in _rules(model_root, "4.2.2")

    def test_one_caller_s_poll_and_the_other_s_returns_event(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _a_second_caller_with_a_result_point))

        assert _at(report, EVENT_PATH) == [(RETURNS_EVENT, AMBIGUOUS)]
        assert _at(report, EVENT_RESULT_PATH) == [_chained_to(RETURNS, AMBIGUOUS)]
        assert _at(report, DIAGNOSE_RESULT_PATH) == []
        runnables = _runnables(model_root)
        assert _call(runnables[DIAGNOSE], "rNvStorage", "WriteBlock")["poll"] is True
        assert "poll" not in _call(runnables[COMPUTE], "rNvStorage", "WriteBlock")
        assert runnables[ON_WRITTEN]["events"] == []
        # The caller whose event was skipped keeps its call with no result: check-model's 4.2.2 finding.
        assert _rules(model_root, "4.2.2") == ["swc.call.async-without-result"]
        assert _rules(model_root, "4.3.0") == []

    def test_two_returns_events(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _two_returns_events))

        second = f"{BEHAVIOUR}/{SECOND_RETURNS}"
        assert _at(report, EVENT_PATH) == [(RETURNS_EVENT, AMBIGUOUS)]
        assert _at(report, second) == [(RETURNS_EVENT, AMBIGUOUS)]
        assert _at(report, EVENT_RESULT_PATH) == [_chained_to(RETURNS, AMBIGUOUS)]
        # The event that starts the caller itself is no poll: the result point follows its event.
        assert _at(report, DIAGNOSE_RESULT_PATH) == [_chained_to(SECOND_RETURNS, AMBIGUOUS)]
        runnables = _runnables(model_root)
        assert "poll" not in _call(runnables[DIAGNOSE], "rNvStorage", "WriteBlock")
        assert all(event["type"] != "call_returned" for r in runnables.values() for event in r.get("events", []))
        assert _rules(model_root, "4.2.2") == ["swc.call.async-without-result"] * 2
        assert _rules(model_root, "4.3.0") == []


# A skipped runnable chain-skips its result points and the event naming one


def _unreadable_interval(runnable: str) -> Callable[[ET.Element], None]:
    def edit(root: ET.Element) -> None:
        interval = _runnable_el(root, runnable).find(_q("MINIMUM-START-INTERVAL"))
        assert interval is not None
        interval.text = "later"

    return edit


def test_a_skipped_callback_chain_skips_its_result_point_and_its_event(tmp_path: Path) -> None:
    report, model_root = _import(tmp_path, _export(tmp_path, _unreadable_interval(ON_WRITTEN)))

    (runnable_tag, runnable_reason) = _at(report, f"{BEHAVIOUR}/{ON_WRITTEN}")[0]
    assert runnable_tag == "RUNNABLE-ENTITY"
    assert _at(report, EVENT_RESULT_PATH) == [
        (RESULT_POINT, f"chain-skipped: runnable '{ON_WRITTEN}' was skipped ({runnable_reason})")
    ]
    assert _at(report, EVENT_PATH) == [
        (
            RETURNS_EVENT,
            f"chain-skipped: the runnable it starts, {BEHAVIOUR}/{ON_WRITTEN}, was skipped ({runnable_reason})",
        )
    ]
    entries = {(e.path, e.tag): e for e in report.skip_entries}
    root = entries[(f"{BEHAVIOUR}/{ON_WRITTEN}", "RUNNABLE-ENTITY")]
    for key in ((EVENT_RESULT_PATH, RESULT_POINT), (EVENT_PATH, RETURNS_EVENT)):
        assert (entries[key].category, entries[key].rule, entries[key].effect) == (
            root.category,
            root.rule,
            root.effect,
        )
    assert ON_WRITTEN not in _runnables(model_root)


def test_a_skipped_caller_chain_skips_its_poll_and_the_event_of_its_call(tmp_path: Path) -> None:
    report, model_root = _import(tmp_path, _export(tmp_path, _unreadable_interval(COMPUTE)))

    (_tag, runnable_reason) = _at(report, f"{BEHAVIOUR}/{COMPUTE}")[0]
    call_line = f"chain-skipped: runnable '{COMPUTE}' was skipped ({runnable_reason})"
    assert _at(report, f"{BEHAVIOUR}/{COMPUTE}/{POLL_RESULT}") == [(RESULT_POINT, call_line)]
    # The callback's result point names a call point the import did not carry, and its event follows it.
    point_reason = f"chain-skipped: its call point {BEHAVIOUR}/{COMPUTE}/{WRITE_CALL} was not carried ({call_line})"
    assert _at(report, EVENT_RESULT_PATH) == [(RESULT_POINT, point_reason)]
    assert _at(report, EVENT_PATH) == [
        (RETURNS_EVENT, f"chain-skipped: its result point '{EVENT_RESULT}' was not carried ({point_reason})")
    ]
    assert _runnables(model_root)[ON_WRITTEN]["events"] == []


# Two declarations of one component


def _duplicated(change_twin: Callable[[ET.Element], None] | None = None) -> Callable[[ET.Element], None]:
    """The controller declared a second time in a package of its own, *change_twin* applied to the copy.

    The copies differ in their inner paths only, which the signature resolves away.
    """

    def edit(root: ET.Element) -> None:
        component = _controller(root)
        twin = copy.deepcopy(component)
        for tag in (
            "START-ON-EVENT-REF",
            "EVENT-SOURCE-REF",
            "ASYNCHRONOUS-SERVER-CALL-POINT-REF",
            "PORT-PROTOTYPE-REF",
            "CONTEXT-R-PORT-REF",
        ):
            for reference in twin.iter(_q(tag)):
                reference.text = reference.text.replace("/ComponentTypes/", "/ComponentTypes/Copy/")
        if change_twin is not None:
            change_twin(twin)
        component_types = _named(root, "AR-PACKAGE", "ComponentTypes")
        packages = component_types.find(_q("AR-PACKAGES"))
        if packages is None:
            packages = ET.SubElement(component_types, _q("AR-PACKAGES"))
        package = ET.SubElement(packages, _q("AR-PACKAGE"))
        ET.SubElement(package, _q("SHORT-NAME")).text = "Copy"
        ET.SubElement(ET.SubElement(package, _q("ELEMENTS")), twin.tag).extend(list(twin))

    return edit


def _twin_runnable(twin: ET.Element, name: str) -> ET.Element:
    return _named(twin, "RUNNABLE-ENTITY", name)


def _form(twin: ET.Element) -> None:
    """The copy reads the wheel speed by argument instead of implicitly."""
    runnable = _twin_runnable(twin, COMPUTE)
    implicit = runnable.find(_q("DATA-READ-ACCESSS"))
    implicit.tag = _q("DATA-RECEIVE-POINT-BY-ARGUMENTS-X")
    runnable.remove(implicit)
    arguments = runnable.find(_q("DATA-RECEIVE-POINT-BY-ARGUMENTS"))
    arguments.insert(0, list(implicit)[0])


def _synchrony(twin: ET.Element) -> None:
    _named(twin, ASYNC_POINT, WRITE_CALL).tag = _q(SYNC_POINT)


def _poll(twin: ET.Element) -> None:
    runnable = _twin_runnable(twin, COMPUTE)
    runnable.remove(runnable.find(_q(RESULT_POINTS)))


def _timeout(twin: ET.Element) -> None:
    _named(twin, ASYNC_POINT, READ_CALL).find(_q("TIMEOUT")).text = "0.25"


def _event(twin: ET.Element) -> None:
    _rename(_named(twin, RETURNS_EVENT, RETURNS), "ASRT_Other")


class TestTwoDeclarationsOfOneComponent:
    def test_two_equal_declarations_merge(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _duplicated()))

        assert report.conflicting_groups == []
        assert _document(model_root)["runnables"] == COMMITTED_RUNNABLES

    @pytest.mark.parametrize("change_twin", [_form, _synchrony, _poll, _timeout, _event])
    def test_a_differing_form_synchrony_poll_timeout_or_event_is_a_conflict(
        self, tmp_path: Path, change_twin: Callable[[ET.Element], None]
    ) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _duplicated(change_twin)))

        assert len(report.conflicting_groups) == 1
        assert not list((model_root / "swc").rglob(f"{CONTROLLER}.yaml"))


# A call through a port whose interface the file does not declare (the stub seam)


def test_a_call_on_a_stub_keeps_its_synchrony_and_timeout_and_its_result_point_is_a_line(tmp_path: Path) -> None:
    """The stated limit: the result points are bound before the stub decision admits the call."""

    def edit(root: ET.Element) -> None:
        # The interface leaves the file, and every reference to it moves to a package the file does not
        # declare: the import mints a stub there (the data-received event's stub case, the same seam).
        interface = _named(root, "CLIENT-SERVER-INTERFACE", "CalibrationIf")
        for parent in root.iter():
            if interface in list(parent):
                parent.remove(interface)
                break
        for element in root.iter():
            if element.text and element.text.startswith("/PortInterfaces/CalibrationIf"):
                element.text = element.text.replace("/PortInterfaces/", "/Foreign/PortInterfaces/")

    report, model_root = _import(tmp_path, _export(tmp_path, edit))

    assert _at(report, f"{BEHAVIOUR}/{COMPUTE}/{POLL_RESULT}") == [
        (
            RESULT_POINT,
            f"result point of the call point {BEHAVIOUR}/{COMPUTE}/{READ_CALL} not carried: the call is through a "
            "port whose interface this export does not declare, decided after the result points are bound",
        )
    ]
    assert _call(_runnables(model_root)[COMPUTE], "rCalibration", "ReadBlock") == {
        "port": "rCalibration",
        "operation": "ReadBlock",
        "asynchronous": True,
        "timeout": 0.5,
    }


# A held implicit read and an earlier returns event that fails


def _pedal_if_elsewhere(root: ET.Element) -> None:
    """``PedalIf`` out of the file, every reference to it under a package the file does not declare: a stub."""
    interface = _named(root, "SENDER-RECEIVER-INTERFACE", "PedalIf")
    for parent in root.iter():
        if interface in list(parent):
            parent.remove(interface)
            break
    for element in root.iter():
        if element.text and element.text.startswith("/PortInterfaces/PedalIf"):
            element.text = element.text.replace("/PortInterfaces/", "/Foreign/PortInterfaces/")


def test_an_implicit_read_held_for_a_stub_is_judged_against_its_carried_com_spec(tmp_path: Path) -> None:
    """``[constr_1103]`` for a read the stub decision admits, as for one the file's interface declares."""

    def model_edit(model_root: Path) -> None:
        def enable(document: dict[str, Any]) -> None:
            (port,) = [port for port in document["ports"] if port["name"] == "rPedal"]
            port["com_specs"] = [{"data_element": "position", "enable_update": True}]

        change(model_root, CONTROLLER_FILE, enable)

    move = _move_access(COMPUTE, "R_rPedal_position", "DATA-RECEIVE-POINT-BY-ARGUMENTS", "DATA-READ-ACCESSS")

    def edit(root: ET.Element) -> None:
        move(root)
        _pedal_if_elsewhere(root)

    report, model_root = _import(tmp_path, _export(tmp_path, edit, model_edit=model_edit))

    (entry,) = [e for e in report.skip_entries if e.path == f"{BEHAVIOUR}/{COMPUTE}/R_rPedal_position"]
    assert (entry.category, entry.rule) == (
        SkipCategory.EXPORT_DEFECT,
        StandardRule.NO_ENABLE_UPDATE_FOR_AN_IMPLICIT_READ,
    )
    assert {"port": "rPedal", "element": "position", "by": "implicit"} in _runnables(model_root)[COMPUTE]["reads"]
    rules = {issue.rule for issue in check_cross_layer_consistency(str(model_root)).records}
    assert "swc.access.implicit-read-enable-update" in rules


def test_a_held_implicit_read_that_collapses_into_an_earlier_admitted_read_takes_no_line() -> None:
    """``[constr_1103]`` is named only for a held read ``_insert_held`` carries.

    A second, implicit read of one ``(port, element)`` collapses into the first and is not carried, so its
    line would name a form the project does not hold.
    """
    from types import SimpleNamespace

    from swcraft_tools.arxml_import.swc_mapping import _report_held_implicit_enable_update
    from swcraft_tools.arxml_reader import SkipLog
    from swcraft_tools.schema import Port, PortComSpec

    port = Port(
        name="rPedal",
        direction="require",
        interface="PedalIf",
        com_specs=[PortComSpec(data_element="position", enable_update=True)],
    )

    def held(name: str, implicit: bool) -> Any:
        return SimpleNamespace(
            runnable_name=COMPUTE,
            collection="reads",
            port="rPedal",
            implicit_read=implicit,
            path=f"{BEHAVIOUR}/{COMPUTE}/{name}",
            tag="VARIABLE-ACCESS",
        )

    explicit_first = SkipLog()
    _report_held_implicit_enable_update(
        [(held("R_first", False), "position"), (held("R_second", True), "position")], [port], explicit_first
    )
    assert explicit_first.entries == []

    implicit_first = SkipLog()
    _report_held_implicit_enable_update(
        [(held("R_first", True), "position"), (held("R_second", True), "position")], [port], implicit_first
    )
    assert [entry.path for entry in implicit_first.entries] == [f"{BEHAVIOUR}/{COMPUTE}/R_first"]


def _an_earlier_event_that_fails(root: ET.Element) -> None:
    """A second returns event of the same result point, before the one the writer wrote, stating a disabled mode."""
    events = _controller(root).find(f".//{_q('EVENTS')}")
    assert events is not None
    original = _event_el(root)
    early = copy.deepcopy(original)
    _rename(early, "ASRT_Early")
    irefs = ET.Element(_q("DISABLED-MODE-IREFS"))
    iref = ET.SubElement(irefs, _q("DISABLED-MODE-IREF"))
    ET.SubElement(iref, _q("CONTEXT-PORT-REF"), {"DEST": "R-PORT-PROTOTYPE"}).text = f"{COMPONENT_PATH}/rMode"
    early.insert(1, irefs)
    events.insert(list(events).index(original), early)


def test_a_returns_event_skipped_for_its_own_reason_leaves_the_result_point_to_a_later_one(tmp_path: Path) -> None:
    """The later event of the same result point is carried, and the result point is chained to nothing."""
    report, model_root = _import(tmp_path, _export(tmp_path, _an_earlier_event_that_fails))

    (early,) = _at(report, f"{BEHAVIOUR}/ASRT_Early")
    assert early[1].startswith("returns event states 1 disabled mode(s)")
    assert _at(report, EVENT_PATH) == []
    assert _at(report, EVENT_RESULT_PATH) == []
    events = _runnables(model_root)[ON_WRITTEN]["events"]
    assert events == [{"type": "call_returned", "port": "rNvStorage", "operation": "WriteBlock"}]


def test_a_result_point_whose_every_returns_event_fails_is_chained_to_the_first(tmp_path: Path) -> None:
    """Both events skipped: the result point's one line, chained to the first event's, recorded at the last."""

    def edit(root: ET.Element) -> None:
        _an_earlier_event_that_fails(root)
        _no_start(root)

    report, _model_root = _import(tmp_path, _export(tmp_path, edit))

    (early,) = _at(report, f"{BEHAVIOUR}/ASRT_Early")
    (later,) = _at(report, EVENT_PATH)
    assert later[1].startswith("returns event has no START-ON-EVENT-REF")
    assert _at(report, EVENT_RESULT_PATH) == [
        (RESULT_POINT, f"chain-skipped: its returns event 'ASRT_Early' was not carried ({early[1]})")
    ]


def test_a_second_event_of_a_result_point_naming_no_call_is_chained_to_the_point(tmp_path: Path) -> None:
    """The result point's own line is recorded once; each event naming it is chained to that line."""

    def edit(root: ET.Element) -> None:
        _result_has_no_reference(root)
        events = _controller(root).find(f".//{_q('EVENTS')}")
        assert events is not None
        twin = copy.deepcopy(_event_el(root))
        _rename(twin, "ASRT_Twin")
        events.append(twin)

    report, model_root = _import(tmp_path, _export(tmp_path, edit))

    point_line = "result point has no ASYNCHRONOUS-SERVER-CALL-POINT-REF"
    assert _at(report, EVENT_RESULT_PATH) == [(RESULT_POINT, point_line)]
    chained = f"chain-skipped: its result point '{EVENT_RESULT}' was not carried ({point_line})"
    assert _at(report, EVENT_PATH) == [(RETURNS_EVENT, chained)]
    assert _at(report, f"{BEHAVIOUR}/ASRT_Twin") == [(RETURNS_EVENT, chained)]
    assert all(event.get("type") != "call_returned" for event in _runnables(model_root)[ON_WRITTEN]["events"])


# A second result point reached through a collapsed duplicate

SECOND_WRITE = "SecondWrite"
SECOND_WRITE_RESULT = "SecondWriteResult"
SECOND_WRITE_RETURNS = "ASRT_SecondWrite"


def _a_twin_of_the_write_call(root: ET.Element) -> None:
    """``ComputeBrakeTorque`` states a second asynchronous call point of ``rNvStorage.WriteBlock``: collapsed."""
    calls = _runnable_el(root, COMPUTE).find(_q("SERVER-CALL-POINTS"))
    assert calls is not None
    twin = copy.deepcopy(_named(calls, ASYNC_POINT, WRITE_CALL))
    _rename(twin, SECOND_WRITE)
    calls.append(twin)


def _a_result_point_of_the_twin(root: ET.Element, runnable: str) -> None:
    """*runnable* owns a result point referencing the twin call point: each call point of the file has one."""
    point = copy.deepcopy(_named(_runnable_el(root, ON_WRITTEN), RESULT_POINT, EVENT_RESULT))
    _rename(point, SECOND_WRITE_RESULT)
    reference = point.find(_q("ASYNCHRONOUS-SERVER-CALL-POINT-REF"))
    assert reference is not None
    reference.text = f"{BEHAVIOUR}/{COMPUTE}/{SECOND_WRITE}"
    owner = _runnable_el(root, runnable)
    wrapper = owner.find(_q(RESULT_POINTS))
    if wrapper is None:
        wrapper = ET.Element(_q(RESULT_POINTS))
        owner.insert(2, wrapper)
    wrapper.append(point)


def _a_polled_twin(root: ET.Element) -> None:
    """The twin's result point on the caller, named by no returns event (a poll)."""
    _a_twin_of_the_write_call(root)
    _a_result_point_of_the_twin(root, COMPUTE)


def _an_event_of_the_twin(root: ET.Element) -> None:
    """The twin's result point on ``Diagnose``, and a returns event of it starting ``Diagnose``."""
    _a_twin_of_the_write_call(root)
    _a_result_point_of_the_twin(root, DIAGNOSE)
    event = copy.deepcopy(_event_el(root))
    _rename(event, SECOND_WRITE_RETURNS)
    start = event.find(_q("START-ON-EVENT-REF"))
    source = event.find(_q("EVENT-SOURCE-REF"))
    assert start is not None and source is not None
    start.text = f"{BEHAVIOUR}/{DIAGNOSE}"
    source.text = f"{BEHAVIOUR}/{DIAGNOSE}/{SECOND_WRITE_RESULT}"
    events = _controller(root).find(f".//{_q('EVENTS')}")
    assert events is not None
    events.append(event)


def _several_results(subject: str) -> str:
    return (
        f"{subject} references call point '{SECOND_WRITE}' of rNvStorage.WriteBlock on runnable '{COMPUTE}', which "
        f"SWCraft collapsed with call point '{WRITE_CALL}' into one call, whose result the returns event "
        f"'{RETURNS}' (result point '{EVENT_RESULT}') already takes; the call holds one result point, so this one "
        "is not carried (rule:swc.call.several-results)"
    )


class TestASecondResultPointReachedThroughACollapsedDuplicate:
    """Two call points of one pair in one runnable, each with a result point, collapse into one call.

    The file is valid at both editions (``[constr_2006]`` counts per call point). The returns event's result
    point is the call's; the other is a ``rule:swc.call.several-results`` line."""

    _COLLAPSE = (
        f"{CONTROLLER}/{COMPUTE}: calls access rNvStorage.WriteBlock declared twice -- '{SECOND_WRITE}' collapsed "
        f"into '{WRITE_CALL}'"
    )

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_poll_beside_a_returns_event(self, tmp_path: Path, version: str) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _a_polled_twin, version=version))

        point_path = f"{BEHAVIOUR}/{COMPUTE}/{SECOND_WRITE_RESULT}"
        assert _at(report, point_path) == [(RESULT_POINT, _several_results(f"result point '{SECOND_WRITE_RESULT}'"))]
        entry = next(e for e in report.skip_entries if e.path == point_path)
        assert (entry.category, entry.rule) == (SkipCategory.NOT_SUPPORTED, None)
        assert [line for _p, _t, line in _entries(report) if "several-results" in line or "constr_2006" in line] == [
            _several_results(f"result point '{SECOND_WRITE_RESULT}'")
        ]
        assert _collapses(report) == [self._COLLAPSE]
        runnables = _runnables(model_root)
        assert _call(runnables[COMPUTE], "rNvStorage", "WriteBlock") == {
            "port": "rNvStorage",
            "operation": "WriteBlock",
            "asynchronous": True,
        }
        assert runnables[ON_WRITTEN]["events"] == COMMITTED_RUNNABLES[1]["events"]
        assert _rules(model_root, version) == []
        generate_arxml(str(model_root), str(tmp_path / "out.arxml"), version=version)

    def test_two_returns_events(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _an_event_of_the_twin))

        reason = _several_results(f"its result point '{SECOND_WRITE_RESULT}'")
        assert _at(report, f"{BEHAVIOUR}/{SECOND_WRITE_RETURNS}") == [(RETURNS_EVENT, reason)]
        assert _at(report, f"{BEHAVIOUR}/{DIAGNOSE}/{SECOND_WRITE_RESULT}") == [
            _chained_to(SECOND_WRITE_RETURNS, reason)
        ]
        assert _collapses(report) == [self._COLLAPSE]
        runnables = _runnables(model_root)
        assert runnables[ON_WRITTEN]["events"] == COMMITTED_RUNNABLES[1]["events"]
        assert all(event["type"] != "call_returned" for event in runnables[DIAGNOSE]["events"])
        assert _rules(model_root, "4.2.2") == []
        generate_arxml(str(model_root), str(tmp_path / "out.arxml"), version="4.2.2")


# Import model names every asynchronous call finding the file states

STATED_AS = " -- imported as the file states it; the model is one check-model refuses (rule:{rule})"


def _finding_entries(report: ImportReport) -> list[tuple[SkipCategory, StandardRule | None, str]]:
    """The finding pass's entries: keyed at the controller's own path."""
    return [(e.category, e.rule, e.reason) for e in report.skip_entries if e.path == COMPONENT_PATH]


def _a_synchronous_write_in_diagnose(root: ET.Element) -> None:
    """``Diagnose`` calls ``rNvStorage.WriteBlock`` synchronously, ``ComputeBrakeTorque`` asynchronously."""
    calls = _runnable_el(root, DIAGNOSE).find(_q("SERVER-CALL-POINTS"))
    assert calls is not None
    sync = copy.deepcopy(_named(calls, SYNC_POINT, VERSION_CALL))
    _rename(sync, "SyncWrite")
    operation = next(child for child in sync if child.tag.endswith("OPERATION-IREF"))
    source = next(
        child for child in _named(_runnable_el(root, COMPUTE), ASYNC_POINT, WRITE_CALL) if child.tag == operation.tag
    )
    sync.remove(operation)
    sync.insert(1, copy.deepcopy(source))
    calls.append(sync)


def _a_second_read_with_another_timeout(root: ET.Element) -> None:
    """``Diagnose`` calls ``rCalibration.ReadBlock`` asynchronously too, with a ``TIMEOUT`` of 0.25 against 0.5."""
    call = copy.deepcopy(_named(_runnable_el(root, COMPUTE), ASYNC_POINT, READ_CALL))
    timeout = call.find(_q("TIMEOUT"))
    assert timeout is not None
    timeout.text = "0.25"
    calls = _runnable_el(root, DIAGNOSE).find(_q("SERVER-CALL-POINTS"))
    assert calls is not None
    calls.append(call)


def _an_init_event_on_the_poller(root: ET.Element) -> None:
    """An ``INIT-EVENT`` starts ``ComputeBrakeTorque``, which owns the poll's result point."""
    events = _controller(root).find(f".//{_q('EVENTS')}")
    assert events is not None
    event = ET.SubElement(events, _q("INIT-EVENT"))
    ET.SubElement(event, _q("SHORT-NAME")).text = "IN_ComputeBrakeTorque"
    start = ET.SubElement(event, _q("START-ON-EVENT-REF"), {"DEST": "RUNNABLE-ENTITY"})
    start.text = f"{BEHAVIOUR}/{COMPUTE}"


def _a_poll_of_the_evented_call(root: ET.Element) -> None:
    """``ComputeBrakeTorque`` owns a second result point of the one write call point: two, the file's own."""
    point = copy.deepcopy(_named(_runnable_el(root, ON_WRITTEN), RESULT_POINT, EVENT_RESULT))
    _rename(point, "PolledWrite")
    wrapper = _runnable_el(root, COMPUTE).find(_q(RESULT_POINTS))
    assert wrapper is not None
    wrapper.append(point)


class TestImportModelNamesTheAsynchronousCallFindingsTheFileStates:
    """Each is imported as stated -- nothing refused, nothing dropped -- and is one section-1 line.

    The line cites its rule with ``check-model``'s own message, as the implicit reads' combinations are.
    """

    def test_mixed_synchrony(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _a_synchronous_write_in_diagnose))

        assert "asynchronous" not in _call(_runnables(model_root)[DIAGNOSE], "rNvStorage", "WriteBlock")
        (record,) = check_cross_layer_consistency(str(model_root), "4.2.2").records
        assert record.rule == "swc.call.mixed-synchrony"
        assert _finding_entries(report) == [
            (
                SkipCategory.EXPORT_DEFECT,
                StandardRule.CALL_SYNCHRONY_EXCLUSIVE,
                record.message + STATED_AS.format(rule=record.rule),
            )
        ]

    def test_a_timeout_mismatch(self, tmp_path: Path) -> None:
        # A 4.3.0 file: Diagnose's call with no result point is no defect of it.
        report, model_root = _import(tmp_path, _export(tmp_path, _a_second_read_with_another_timeout, version="4.3.0"))

        assert _call(_runnables(model_root)[DIAGNOSE], "rCalibration", "ReadBlock")["timeout"] == 0.25
        rules = {r.rule: r for r in check_cross_layer_consistency(str(model_root), "4.2.2").records}
        assert set(rules) == {"swc.call.timeout-mismatch", "swc.call.async-without-result"}
        record = rules["swc.call.timeout-mismatch"]
        assert _finding_entries(report) == [
            (
                SkipCategory.EXPORT_DEFECT,
                StandardRule.CALL_TIMEOUTS_IDENTICAL,
                record.message + STATED_AS.format(rule=record.rule),
            )
        ]

    def test_a_result_point_of_an_init_started_runnable(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _an_init_event_on_the_poller))

        assert "init" in [event["type"] for event in _runnables(model_root)[COMPUTE]["events"]]
        (record,) = check_cross_layer_consistency(str(model_root), "4.2.2").records
        assert record.rule == "swc.call.init-runnable-result"
        assert _finding_entries(report) == [
            (
                SkipCategory.EXPORT_DEFECT,
                StandardRule.NO_RESULT_POINT_IN_AN_INIT_RUNNABLE,
                record.message + STATED_AS.format(rule=record.rule),
            )
        ]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_two_result_points_of_one_call_point(self, tmp_path: Path, version: str) -> None:
        """Not SWCraft's collapse: the file's one call point has two result points, a poll and an event's."""
        report, model_root = _import(tmp_path, _export(tmp_path, _a_poll_of_the_evented_call, version=version))

        runnables = _runnables(model_root)
        assert _call(runnables[COMPUTE], "rNvStorage", "WriteBlock")["poll"] is True
        assert runnables[ON_WRITTEN]["events"] == COMMITTED_RUNNABLES[1]["events"]
        (record,) = check_cross_layer_consistency(str(model_root), "4.2.2").records
        assert record.rule == "swc.call.several-results"
        assert _finding_entries(report) == [
            (
                SkipCategory.EXPORT_DEFECT,
                StandardRule.ONE_RESULT_POINT_PER_CALL,
                record.message + STATED_AS.format(rule=record.rule),
            )
        ]
        assert f"(R{version} [constr_2006])" in report.render()

    def test_a_call_a_4_2_2_file_leaves_without_a_result(self, tmp_path: Path) -> None:
        report, model_root = _import(tmp_path, _export(tmp_path, _drop_returns_event))

        assert _finding_entries(report) == [
            (SkipCategory.EXPORT_DEFECT, StandardRule.ONE_RESULT_POINT_PER_CALL, _WRITE_WITHOUT_RESULT)
        ]
        assert "(R4.2.2 [constr_2006])" in report.render()
        assert _rules(model_root, "4.2.2") == ["swc.call.async-without-result"]

    def test_a_call_whose_result_point_the_import_did_not_carry_takes_the_point_s_line_alone(
        self, tmp_path: Path
    ) -> None:
        """The two-callers shape at 4.2.2: the event is skipped and its result point chained.

        check-model's ``async-without-result`` for the caller is the consequence of that line, not a second one.
        """
        report, model_root = _import(tmp_path, _export(tmp_path, _a_second_caller_with_a_result_point))

        assert _rules(model_root, "4.2.2") == ["swc.call.async-without-result"]
        assert _at(report, EVENT_RESULT_PATH) == [_chained_to(RETURNS, AMBIGUOUS)]
        assert _finding_entries(report) == []

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_clean_file_takes_no_line(self, tmp_path: Path, version: str) -> None:
        report, _model_root = _import(tmp_path, _export(tmp_path, version=version))

        assert _finding_entries(report) == []


# A second held call on one stub port, and a file of no known release


def _calibration_if_elsewhere_and_an_async_version_call(root: ET.Element) -> None:
    """``CalibrationIf`` a stub, and ``ComputeBrakeTorque`` calls ``GetVersion`` asynchronously with no result point.

    ``Diagnose``'s synchronous ``GetVersion`` leaves the file, so mixed synchrony is not this case.
    """
    calls = _runnable_el(root, DIAGNOSE).find(_q("SERVER-CALL-POINTS"))
    assert calls is not None
    calls.remove(_named(calls, SYNC_POINT, VERSION_CALL))
    call = copy.deepcopy(_named(_runnable_el(root, COMPUTE), ASYNC_POINT, READ_CALL))
    _rename(call, "Call_rCalibration_GetVersion_Async")
    for element in call.iter():
        if element.text and element.text.endswith("/ReadBlock"):
            element.text = element.text[: -len("ReadBlock")] + "GetVersion"
    wrapper = _runnable_el(root, COMPUTE).find(_q("SERVER-CALL-POINTS"))
    assert wrapper is not None
    wrapper.append(call)
    interface = _named(root, "CLIENT-SERVER-INTERFACE", "CalibrationIf")
    for parent in root.iter():
        if interface in list(parent):
            parent.remove(interface)
            break
    for element in root.iter():
        if element.text and element.text.startswith("/PortInterfaces/CalibrationIf"):
            element.text = element.text.replace("/PortInterfaces/", "/Foreign/PortInterfaces/")


def _without_a_result(report: ImportReport) -> list[str]:
    return [reason for _, _, reason in _finding_entries(report) if "rule:swc.call.async-without-result" in reason]


def test_a_second_held_call_on_one_stub_port_keeps_its_line_when_the_first_has_a_result_point(
    tmp_path: Path,
) -> None:
    """A held call is keyed by its source operation: ``ReadBlock``'s result point does not hide ``GetVersion``."""
    report, model_root = _import(tmp_path, _export(tmp_path, _calibration_if_elsewhere_and_an_async_version_call))

    lines = _without_a_result(report)
    assert len(lines) == 1
    assert "calls operation 'GetVersion' of port 'rCalibration'" in lines[0]
    assert "(R4.2.2 [constr_2006])" in report.render()
    assert sorted(_rules(model_root, "4.2.2")) == ["swc.call.async-without-result"] * 2


NO_RELEASE_TAIL = (
    " -- imported as the file states it; the file declares no AUTOSAR release SWCraft writes, so this project's "
    "arxml_version 4.2.2 rules apply, and check-model refuses the model (rule:swc.call.async-without-result); set "
    "poll: true on the call, give it a call_returned event, or set arxml_version: 4.3.0 in swcraft.yaml"
)


def _no_known_release(schema: str | None) -> Callable[[ET.Element], None]:
    """The returns event dropped, and the file's ``xsi:schemaLocation`` naming *schema* or gone."""

    def edit(root: ET.Element) -> None:
        _drop_returns_event(root)
        key = f"{{{XSI}}}schemaLocation"
        if schema is None:
            del root.attrib[key]
        else:
            root.set(key, f"{NS} {schema}")

    return edit


@pytest.mark.parametrize("schema", ["AUTOSAR_00048.xsd", None], ids=["unknown-schema", "no-schema-location"])
@pytest.mark.parametrize("project_version", VERSIONS)
def test_a_file_of_no_known_release_names_a_call_without_a_result_at_a_4_2_2_project(
    tmp_path: Path, schema: str | None, project_version: str
) -> None:
    """No release of its own to cite: one ``NOT_SUPPORTED`` line at a 4.2.2 project, none at a 4.3.0 one."""
    source = _export(tmp_path, _no_known_release(schema))
    project = init_project(tmp_path / "project")
    if project_version == "4.3.0":
        manifest = project / "swcraft.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + 'arxml_version: "4.3.0"\n', encoding="utf-8", newline="\n"
        )
    report = run_import(str(source), str(project / "swcraft.yaml"))

    assert report.declared_release is None
    if project_version == "4.2.2":
        (entry,) = _finding_entries(report)
        assert entry[0] == SkipCategory.NOT_SUPPORTED
        assert entry[1] is None
        assert entry[2].endswith(NO_RELEASE_TAIL)
        assert "calls operation 'WriteBlock' of port 'rNvStorage' asynchronously" in entry[2]
        assert _rules(project / "model", "4.2.2") == ["swc.call.async-without-result"]
    else:
        assert _finding_entries(report) == []
        assert _rules(project / "model", "4.3.0") == []
