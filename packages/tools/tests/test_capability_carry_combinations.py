"""The component import's carry and regime combinations no family's own suite reaches.

The capability-marker registry (``component_exchange/capabilities.py``) folds the families' carries
into one dispatch; each family's suite drives its own family, and this module holds:

* one ``--update`` with the five families of :data:`REGIME_FIELDS` **carried at once**, so their
  carries run on one component, in their order;
* a **withheld behaviour** (``behaviour_kept``) under an unmarked file, over a target that states
  every family and over one that states none of the three component-scope ones: no carry runs,
  every regime reads ``none`` and the project's runnables are kept;
* the variables carry over a runnable the target does not declare, over a target runnable that
  accesses no variable, and over a target with neither a variable nor an access.

What this module does **not** reach: a file SWCraft wrote before a family's contract (the
*unmarked* file is this release's export with the markers removed); the implicit-access and
asynchronous-call carries, whose markers this module does not remove; the MCP surface, which renders
the same :class:`ComponentImportReport`; the real tool exports (the closed suites).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from data_event_support import COMPONENT, COMPONENT_FILE, FILTER, MONITOR, WHEEL_PORT, stub_free_data_event_model
from external_stubs_support import change, put
from inter_runnable_variable_support import COMPONENT as VARIABLE_COMPONENT
from inter_runnable_variable_support import COMPONENT_FILE as VARIABLE_COMPONENT_FILE
from inter_runnable_variable_support import LOGGER, WRITER, variable_model
from inter_runnable_variable_support import MONITOR as VARIABLE_MONITOR
from project_support import init_project
from swcraft_tools.capability_markers import (
    ACCESS_POINTS,
    DATA_EVENTS,
    EXCLUSIVE_AREAS,
    INTER_RUNNABLE_VARIABLES,
    OPERATION_EVENTS,
)
from swcraft_tools.component_exchange import export_component, import_component
from swcraft_tools.component_exchange.capabilities import CARRIED, NONE
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import SWComponent
from swcraft_tools.validator import check_cross_layer_consistency

#: Every regime family's marker, the five a file written before them all lacks.
REGIME_MARKERS = (ACCESS_POINTS, OPERATION_EVENTS, INTER_RUNNABLE_VARIABLES, EXCLUSIVE_AREAS, DATA_EVENTS)

#: The client/server interface, the provide port and the operation the monitor serves.
SERVICE_INTERFACE = "SpeedServiceIf"
SERVICE_PORT = "P_SpeedService"
RESET = "Reset"

#: The variable and the exclusive area the every-family component adds.
LAST_SPEED = "LastSpeed"
SPEED_STATE = "SpeedState"

#: Every report field a regime family sets, by family.
REGIME_FIELDS = (
    "access_list_regime",
    "operation_event_regime",
    "variable_regime",
    "exclusive_area_regime",
    "data_event_regime",
)


# Helpers


def _component(model_root: Path, name: str = COMPONENT) -> SWComponent:
    (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == name]
    return swc


def _every_family(copy: Path) -> None:
    """The stub-free data-event component given every other family too."""
    put(
        copy,
        f"interfaces/{SERVICE_INTERFACE}.yaml",
        {"name": SERVICE_INTERFACE, "kind": "client_server", "operations": [{"name": RESET}]},
    )

    def component(document: dict[str, Any]) -> None:
        document["ports"].append({"name": SERVICE_PORT, "direction": "provide", "interface": SERVICE_INTERFACE})
        document["variables"] = [{"name": LAST_SPEED, "access": "explicit", "implementation_type": "uint16", "init": 0}]
        document["exclusive_areas"] = [{"name": SPEED_STATE}]
        for runnable in document["runnables"]:
            if runnable["name"] == MONITOR:
                runnable["events"] = [
                    {"type": "timing", "period": 0.1},
                    {"type": "operation", "port": SERVICE_PORT, "operation": RESET},
                    {"type": "data", "port": WHEEL_PORT, "element": "FrontRight"},
                ]
                runnable["concurrent"] = True
                runnable["writes_variables"] = [{"variable": LAST_SPEED}]
                runnable["can_enter"] = [{"area": SPEED_STATE}]
            else:
                runnable["reads_variables"] = [{"variable": LAST_SPEED}]
                runnable["runs_inside"] = [{"area": SPEED_STATE}]

    change(copy, COMPONENT_FILE, component)


def _without_the_three(document: dict[str, Any]) -> None:
    """No variable, no exclusive area and no data-received event, with every reference to them."""
    document.pop("variables", None)
    document.pop("exclusive_areas", None)
    for runnable in document["runnables"]:
        for field in ("reads_variables", "writes_variables", "can_enter", "runs_inside"):
            runnable.pop(field, None)
        runnable["events"] = [e for e in runnable["events"] if e["type"] != "data"] or [
            {"type": "timing", "period": 0.05}
        ]


def _silent_about_every_family(document: dict[str, Any]) -> None:
    """What a file written before every family's contract states: none of the five."""
    _without_the_three(document)
    for runnable in document["runnables"]:
        for field in ("reads", "writes", "calls"):
            runnable.pop(field, None)
        runnable["events"] = [e for e in runnable["events"] if e["type"] != "operation"]
        runnable.pop("concurrent", None)


def _set_monitor_period(document: dict[str, Any]) -> None:
    (monitor,) = [r for r in document["runnables"] if r["name"] == MONITOR]
    for event in monitor["events"]:
        if event["type"] == "timing":
            event["period"] = 0.2


def _unmark_every_family(arxml: Path) -> Path:
    """Remove every regime family's marker the file carries; the data-types marker stays."""
    text = arxml.read_text(encoding="utf-8")
    for marker in REGIME_MARKERS:
        text = re.sub(rf'\n *<SD GID="{marker.sd_gid}">[^<]*</SD>', "", text)
    arxml.write_text(text, encoding="utf-8", newline="\n")
    for marker in REGIME_MARKERS:
        assert f'<SD GID="{marker.sd_gid}">' not in text
    return arxml


def _export(model_root: Path, arxml: Path, component: str = COMPONENT) -> Path:
    export_component(str(model_root), component, str(arxml))
    return arxml


def _target(tmp_path: Path, *edits: Any) -> Path:
    """A project holding the every-family component (each of *edits* applied first), imported from its export."""
    source = stub_free_data_event_model(tmp_path / "source")
    _every_family(source)
    for edit in edits:
        change(source, COMPONENT_FILE, edit)
    target = init_project(tmp_path / "target") / "model"
    assert import_component(str(_export(source, tmp_path / "marked.arxml")), str(target)).swc_action == "created"
    return target


def _older(tmp_path: Path, *edits: Any) -> Path:
    """An unmarked export of the every-family component with each of *edits* applied."""
    copy = stub_free_data_event_model(tmp_path / "older-model")
    _every_family(copy)
    for edit in edits:
        change(copy, COMPONENT_FILE, edit)
    return _unmark_every_family(_export(copy, tmp_path / "older.arxml"))


def _with_an_unanswered_mapping_set(arxml: Path) -> Path:
    """*arxml* with a ``DATA-TYPE-MAPPING-REF`` onto a set neither the file nor the target declares."""
    text = arxml.read_text(encoding="utf-8")
    anchor = "<HANDLE-TERMINATION-AND-RESTART>"
    assert text.count(anchor) == 1
    arxml.write_text(
        text.replace(
            anchor,
            '<DATA-TYPE-MAPPING-REFS><DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Maps'
            "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS>" + anchor,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return arxml


# Every family carried at once


class TestEveryFamilyCarriedAtOnce:
    def test_an_update_from_a_file_silent_about_every_family_keeps_them_all(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        expected = _component(target)

        report = import_component(
            str(_older(tmp_path, _silent_about_every_family, _set_monitor_period)), str(target), update=True
        )

        assert report.swc_action == "updated"
        assert not report.behaviour_kept
        assert {field: getattr(report, field) for field in REGIME_FIELDS} == dict.fromkeys(REGIME_FIELDS, CARRIED)
        assert report.preserved_access_lists == {FILTER: {"reads": 1}}
        assert report.preserved_operation_events == {MONITOR: {"operation_events": 1, "concurrent": True}}
        assert report.preserved_data_events == {FILTER: 1, MONITOR: 1}
        assert report.preserved_variables == [LAST_SPEED]
        assert report.preserved_variable_accesses == {
            FILTER: {"reads_variables": 1},
            MONITOR: {"writes_variables": 1},
        }
        assert report.preserved_exclusive_areas == [SPEED_STATE]
        assert report.preserved_exclusive_area_references == {
            FILTER: {"runs_inside": 1},
            MONITOR: {"can_enter": 1},
        }
        kept = _component(target)
        monitor = next(r for r in kept.runnables if r.name == MONITOR)
        assert [e.type for e in monitor.events] == ["timing", "operation", "data"], "one traversal keeps the order"
        assert monitor.events[0].period == 0.2
        assert kept.model_copy(update={"runnables": expected.runnables}) == expected
        assert [r.model_copy(update={"events": []}) for r in kept.runnables] == [
            r.model_copy(update={"events": []}) for r in expected.runnables
        ]
        assert check_cross_layer_consistency(str(target)).consistent


# A withheld behaviour under an unmarked file


class TestAWithheldBehaviourUnderAnUnmarkedFileCarriesNothing:
    @pytest.mark.parametrize(
        "target_edits",
        [(), (_without_the_three,)],
        ids=["the-target-states-every-family", "the-target-states-none-of-the-three"],
    )
    def test_a_kept_behaviour_under_an_unmarked_file_reports_no_regime(
        self, tmp_path: Path, target_edits: tuple[Any, ...]
    ) -> None:
        target = _target(tmp_path, *target_edits)
        expected = _component(target)

        def describe(document: dict[str, Any]) -> None:
            document["description"] = "A new description."

        newer = _with_an_unanswered_mapping_set(
            _older(tmp_path, *target_edits, _silent_about_every_family, _set_monitor_period, describe)
        )

        report = import_component(str(newer), str(target), update=True)

        assert report.behaviour_kept
        assert report.swc_action == "updated"
        assert {field: getattr(report, field) for field in REGIME_FIELDS} == dict.fromkeys(REGIME_FIELDS, NONE)
        assert report.preserved_access_lists == {}
        assert report.preserved_operation_events == {}
        assert report.preserved_data_events == {}
        assert report.preserved_variables == []
        assert report.preserved_variable_accesses == {}
        assert report.preserved_exclusive_areas == []
        assert report.preserved_exclusive_area_references == {}
        kept = _component(target)
        assert kept.description == "A new description."
        assert kept.runnables == expected.runnables
        assert kept.variables == expected.variables
        assert kept.exclusive_areas == expected.exclusive_areas


# The variables carry's two remaining branches


def _without_variables(document: dict[str, Any]) -> None:
    document.pop("variables", None)
    for runnable in document["runnables"]:
        runnable.pop("reads_variables", None)
        runnable.pop("writes_variables", None)


def _variable_older(tmp_path: Path, source: Path, *edits: Any) -> Path:
    """An export of a copy of *source*, each of *edits* applied, without the variables marker."""
    copy = tmp_path / "older-model"
    shutil.copytree(source, copy)
    for edit in edits:
        change(copy, VARIABLE_COMPONENT_FILE, edit)
    arxml = _export(copy, tmp_path / "older.arxml", VARIABLE_COMPONENT)
    text = arxml.read_text(encoding="utf-8")
    pattern = rf'\n *<SD GID="{INTER_RUNNABLE_VARIABLES.sd_gid}">[^<]*</SD>'
    assert len(re.findall(pattern, text)) == 1
    arxml.write_text(re.sub(pattern, "", text), encoding="utf-8", newline="\n")
    return arxml


def _variable_target(tmp_path: Path, *edits: Any) -> tuple[Path, Path]:
    """``(source, target)``: the variable fixture (each of *edits* applied) and a project imported from it."""
    source = variable_model(tmp_path / "source")
    for edit in edits:
        change(source, VARIABLE_COMPONENT_FILE, edit)
    target = init_project(tmp_path / "target") / "model"
    arxml = _export(source, tmp_path / "marked.arxml", VARIABLE_COMPONENT)
    assert import_component(str(arxml), str(target)).swc_action == "created"
    return source, target


def _add_runnable(name: str) -> Any:
    def edit(document: dict[str, Any]) -> None:
        document["runnables"].append({"name": name, "events": [{"type": "timing", "period": 0.5}]})

    return edit


def _set_writer_period(document: dict[str, Any]) -> None:
    (writer,) = [r for r in document["runnables"] if r["name"] == WRITER]
    writer["events"] = [{"type": "timing", "period": 0.02}]


class TestTheVariablesCarrySkipsWhatItCannotCarry:
    def test_a_runnable_the_target_does_not_declare_and_one_accessing_no_variable_carry_nothing(
        self, tmp_path: Path
    ) -> None:
        source, target = _variable_target(tmp_path, _add_runnable("IdleSpeed"))
        expected = _component(target, VARIABLE_COMPONENT)

        report = import_component(
            str(
                _variable_older(tmp_path, source, _without_variables, _set_writer_period, _add_runnable("ReportSpeed"))
            ),
            str(target),
            update=True,
        )

        assert report.swc_action == "updated"
        assert report.variable_regime == CARRIED
        assert report.preserved_variables == [v.name for v in expected.variables]
        assert report.preserved_variable_accesses == {
            WRITER: {"writes_variables": 1},
            LOGGER: {"reads_variables": 1},
            VARIABLE_MONITOR: {"reads_variables": 2, "writes_variables": 1},
        }
        kept = {r.name: r for r in _component(target, VARIABLE_COMPONENT).runnables}
        for name in ("IdleSpeed", "ReportSpeed"):
            assert not kept[name].reads_variables
            assert not kept[name].writes_variables
        assert check_cross_layer_consistency(str(target)).consistent

    def test_a_target_with_no_variable_and_no_access_carries_nothing(self, tmp_path: Path) -> None:
        source, target = _variable_target(tmp_path, _without_variables)

        report = import_component(str(_variable_older(tmp_path, source, _set_writer_period)), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.variable_regime == CARRIED
        assert report.preserved_variables == []
        assert report.preserved_variable_accesses == {}
        assert report.preserved_variable_mappings == []
        assert "VARIABLES preserved" not in report.render()
        assert not _component(target, VARIABLE_COMPONENT).variables
