"""Component exchange: the access forms and the asynchronous calls survive it.

``swcraft generate arxml --component`` writes implicit reads and writes, asynchronous calls with their
poll, result and timeout, and ``call_returned`` events, refuses what ``check-model`` refuses about them,
and writes two capability markers into the behaviour's ``SWCRAFT`` group (``SD GID="IMPLICIT-ACCESS"``,
``SD GID="ASYNCHRONOUS-CALLS"``). ``swcraft import component`` reads them back and decides, per
component and per marker, which regime governs an ``--update``:

* **marked** -- the file is the truth for the family, absence included;
* **stated** -- no marker, but the file states the family somewhere: what it states is the truth for the
  whole component;
* **carried** -- no marker and none stated (every export written without the markers): the family is left
  out of the comparison and ``--update`` keeps the project's forms on each pair, call and runnable the
  file still holds.

The subject is ``fixtures/access_forms/model``'s ``BrakeController``, exported by the real exporter and,
where a case needs an older, third-party or damaged file, changed before the export or edited as text after.

Does not reach: a file an earlier SWCraft release wrote (the *unmarked* file is the current export with
the markers and forms removed, not every earlier byte); an operation event at the product level (the
fixture has no provide client/server port; the registry case holds the traversal's order); real tool
exports; the MCP surface's rendering of the :class:`ComponentImportReport`.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from external_stubs_support import change, put
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.capability_markers import (
    ASYNCHRONOUS_CALLS,
    COMPONENT_EXPORT_MARKERS,
    DATA_EVENTS,
    IMPLICIT_ACCESS,
)
from swcraft_tools.component_exchange import (
    ClosureValidationError,
    ComponentImportConflictError,
    InvalidComponentArxmlError,
    LossyComponentImportError,
    capabilities,
    export_component,
    import_component,
)
from swcraft_tools.component_exchange.capabilities import CARRIED, MARKED, NONE, STATED
from swcraft_tools.component_exchange.resulting_behaviour import _behaviour_findings
from swcraft_tools.data_description import read_data_description
from swcraft_tools.differ import diff_model_states
from swcraft_tools.model_state import ModelState
from swcraft_tools.reader import read_interfaces, read_mapping_sets, read_swc_model
from swcraft_tools.schema import CallReturnedEvent, DataEvent, Runnable, SWComponent, TimingEvent
from swcraft_tools.validator import check_async_calls, check_cross_layer_consistency

FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
CONTROLLER = "BrakeController"
CONTROLLER_FILE = "swc/chassis/BrakeController.yaml"
COMPUTE = "ComputeBrakeTorque"
ON_WRITTEN = "OnBlockWritten"
DIAGNOSE = "Diagnose"

MARKER_LINE = {
    marker.sd_gid: re.compile(rf'\n *<SD GID="{marker.sd_gid}">[^<]*</SD>')
    for marker in (IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS)
}


# Helpers


def _model(destination: Path) -> Path:
    shutil.copytree(FIXTURE, destination)
    return destination


def _export(model_root: Path, arxml: Path, version: str = "4.2.2", name: str = CONTROLLER) -> Path:
    export_component(str(model_root), name, str(arxml), version=version)
    return arxml


def _component(model_root: Path) -> SWComponent:
    (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == CONTROLLER]
    return swc


def _runnables(model_root: Path) -> dict[str, Runnable]:
    return {runnable.name: runnable for runnable in _component(model_root).runnables}


def _forms(swc: SWComponent) -> dict[str, Any]:
    """Every runnable's forms, synchrony and ``call_returned`` events, by runnable."""
    return {
        runnable.name: {
            "reads": [(r.port, r.element, r.by) for r in runnable.reads],
            "writes": [(w.port, w.element, w.by) for w in runnable.writes],
            "calls": [(c.port, c.operation, c.asynchronous, c.poll, c.result, c.timeout) for c in runnable.calls],
            "events": [type(e).__name__ for e in runnable.events],
        }
        for runnable in swc.runnables
    }


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _edit(edit: Any) -> Any:
    """A model-copy edit applying *edit* to the controller's document."""
    return lambda copy: change(copy, CONTROLLER_FILE, edit)


def _without_forms(document: dict[str, Any]) -> None:
    """The controller as an export written without the access forms: none of the forms, no event."""
    for runnable in document["runnables"]:
        for entry in (*runnable.get("reads", []), *runnable.get("writes", [])):
            if entry.get("by") == "implicit":
                del entry["by"]
        for call in runnable.get("calls", []):
            for key in ("asynchronous", "poll", "result", "timeout"):
                call.pop(key, None)
        runnable["events"] = [e for e in runnable.get("events", []) if e["type"] != "call_returned"]


def _set_period(period: float) -> Any:
    def edit(document: dict[str, Any]) -> None:
        (diagnose,) = [r for r in document["runnables"] if r["name"] == DIAGNOSE]
        diagnose["events"] = [{"type": "timing", "period": period}]

    return edit


def _unmark(arxml: Path, *markers: Any) -> Path:
    """Remove *markers* (both by default), as an export written without them lacks them."""
    text = arxml.read_text(encoding="utf-8")
    for marker in markers or (IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS):
        pattern = MARKER_LINE[marker.sd_gid]
        assert len(pattern.findall(text)) == 1
        text = pattern.sub("", text)
    arxml.write_text(text, encoding="utf-8", newline="\n")
    return arxml


def _replace_text(arxml: Path, old: str, new: str) -> Path:
    text = arxml.read_text(encoding="utf-8")
    assert text.count(old) == 1, (old, text.count(old))
    arxml.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return arxml


def _copy_export(tmp_path: Path, name: str, *edits: Any, marked: bool = True, version: str = "4.2.2") -> Path:
    """An export of a fresh fixture copy at *version*, each of *edits* applied to it first."""
    copy = _model(tmp_path / f"{name}-model")
    for edit in edits:
        edit(copy)
    arxml = _export(copy, tmp_path / f"{name}.arxml", version=version)
    return arxml if marked else _unmark(arxml)


def _with_an_unanswered_mapping_set(arxml: Path) -> Path:
    """*arxml* with a ``DATA-TYPE-MAPPING-REF`` onto a set neither the file nor the target declares."""
    return _replace_text(
        arxml,
        "<HANDLE-TERMINATION-AND-RESTART>",
        '<DATA-TYPE-MAPPING-REFS><DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Maps'
        "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS><HANDLE-TERMINATION-AND-RESTART>",
    )


@pytest.fixture
def exchanged(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The fixture, its marked export of the controller, and a project holding the controller imported from it.

    Returns ``(source model root, marked export, target model root)``.
    """
    source = _model(tmp_path / "source")
    arxml = _export(source, tmp_path / "marked.arxml")
    target = init_project(tmp_path / "target") / "model"
    assert import_component(str(arxml), str(target)).swc_action == "created"
    return source, arxml, target


# The closure check


class TestTheClosureCheckJudgesTheForms:
    def test_an_implicit_read_of_an_element_with_an_update_flag_is_the_closure_check_s_refusal(
        self, tmp_path: Path
    ) -> None:
        source = _model(tmp_path / "source")

        def flagged(document: dict[str, Any]) -> None:
            (port,) = [p for p in document["ports"] if p["name"] == "rWheelSpeed"]
            port["com_specs"] = [{"data_element": "speed", "enable_update": True}]

        change(source, CONTROLLER_FILE, flagged)
        with pytest.raises(ClosureValidationError) as refusal:
            _export(source, tmp_path / "component.arxml")
        assert "swc.access.implicit-read-enable-update" in refusal.value.rules
        assert not (tmp_path / "component.arxml").exists()

    def test_a_call_returned_event_naming_no_asynchronous_call_is_the_closure_check_s_refusal(
        self, tmp_path: Path
    ) -> None:
        source = _model(tmp_path / "source")

        def synchronous(document: dict[str, Any]) -> None:
            (compute,) = [r for r in document["runnables"] if r["name"] == COMPUTE]
            (call,) = [c for c in compute["calls"] if c["operation"] == "WriteBlock"]
            del call["asynchronous"]

        change(source, CONTROLLER_FILE, synchronous)
        with pytest.raises(ClosureValidationError) as refusal:
            _export(source, tmp_path / "component.arxml")
        assert refusal.value.rules == ("swc.call-returned-event.unknown-call",)
        assert not (tmp_path / "component.arxml").exists()

    @pytest.mark.parametrize(("version", "refused"), [("4.2.2", True), ("4.3.0", False)])
    def test_an_asynchronous_call_without_a_result_is_judged_at_the_export_s_version(
        self, tmp_path: Path, version: str, refused: bool
    ) -> None:
        """``[constr_2006]``: exactly one result point at 4.2.2, at most one at 4.3.0."""
        source = _model(tmp_path / "source")

        def no_event(document: dict[str, Any]) -> None:
            document["runnables"] = [r for r in document["runnables"] if r["name"] != ON_WRITTEN]

        change(source, CONTROLLER_FILE, no_event)
        arxml = tmp_path / "component.arxml"
        if refused:
            with pytest.raises(ClosureValidationError) as refusal:
                _export(source, arxml, version)
            assert refusal.value.rules == ("swc.call.async-without-result",)
            assert not arxml.exists()
        else:
            assert "<ASYNCHRONOUS-SERVER-CALL-POINT>" in _export(source, arxml, version).read_text(encoding="utf-8")


# The markers


class TestTheExportCarriesTheMarkers:
    def test_every_component_export_carries_both_last_in_order(self, tmp_path: Path) -> None:
        arxml = _export(_model(tmp_path / "source"), tmp_path / "component.arxml")
        text = arxml.read_text(encoding="utf-8")
        for marker in (IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS):
            assert f'<SD GID="{marker.sd_gid}">{marker.text}</SD>' in text
        (group,) = [
            node
            for node in etree.parse(str(arxml)).iter()
            if etree.QName(node).localname == "SDG" and node.get("GID") == "SWCRAFT"
        ]
        gids = [child.get("GID") for child in group]
        assert gids[-3:] == [DATA_EVENTS.sd_gid, IMPLICIT_ACCESS.sd_gid, ASYNCHRONOUS_CALLS.sd_gid]
        assert COMPONENT_EXPORT_MARKERS[-2:] == (IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS)

    def test_a_component_stating_none_carries_them_too(self, tmp_path: Path) -> None:
        """A statement about the exporter: the server declares no form and no asynchronous call."""
        text = _export(_model(tmp_path / "source"), tmp_path / "server.arxml", name="NvStorage").read_text(
            encoding="utf-8"
        )
        assert "<ASYNCHRONOUS-SERVER-CALL-POINT>" not in text
        assert "<DATA-READ-ACCESSS>" not in text
        for marker in (IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS):
            assert f'<SD GID="{marker.sd_gid}">' in text

    def test_the_reader_admits_the_group_and_reads_both(self, tmp_path: Path) -> None:
        model = read_arxml(str(_export(_model(tmp_path / "source"), tmp_path / "component.arxml")))
        for marker in (IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS):
            assert model.swcs[0].swcraft_markers[marker.sd_gid] == marker.text
        assert not [entry for entry in model.skip_log if "ADMIN-DATA" in entry.reason]

    def test_the_markers_are_capability_tokens_never_versions(self) -> None:
        assert (IMPLICIT_ACCESS.sd_gid, ASYNCHRONOUS_CALLS.sd_gid) == ("IMPLICIT-ACCESS", "ASYNCHRONOUS-CALLS")
        for marker in (IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS):
            assert not re.search(r"\d", marker.text), "a capability, never a version"

    @pytest.mark.parametrize("marker", [IMPLICIT_ACCESS, ASYNCHRONOUS_CALLS], ids=lambda m: m.sd_gid)
    def test_a_value_the_writer_does_not_mint_is_refused_with_nothing_written(self, exchanged, marker: Any) -> None:
        _source, arxml, target = exchanged
        _replace_text(arxml, f">{marker.text}<", ">something else<")
        before = _tree(target)
        for update in (False, True):
            with pytest.raises(InvalidComponentArxmlError) as refusal:
                import_component(str(arxml), str(target), update=update)
            assert f"{marker.sd_gid} marker with the value 'something else'" in str(refusal.value)
        assert _tree(target) == before


# The round trip


class TestTheFormsSurviveTheRoundTrip:
    def test_the_component_exports_and_reimports_unchanged(self, exchanged) -> None:
        source, arxml, target = exchanged
        assert _component(target).runnables == _component(source).runnables, "every form, synchrony and event"
        before = _tree(target)
        for update in (False, True):
            report = import_component(str(arxml), str(target), update=update)
            assert report.swc_action == "unchanged"
            assert (report.implicit_access_regime, report.asynchronous_call_regime) == (NONE, NONE)
        assert _tree(target) == before

    def test_the_imported_project_checks_clean(self, exchanged) -> None:
        _source, _arxml, target = exchanged
        result = check_cross_layer_consistency(str(target))
        assert result.consistent, result.issues

    def test_an_update_from_a_marked_export_keeps_every_form(self, exchanged, tmp_path: Path) -> None:
        """``--update`` from a marked export keeps every access form and drops none."""
        source, _arxml, target = exchanged
        newer = _copy_export(tmp_path, "newer", _edit(_set_period(2.0)))

        report = import_component(str(newer), str(target), update=True)

        assert report.swc_action == "updated"
        assert (report.implicit_access_regime, report.asynchronous_call_regime) == (MARKED, MARKED)
        assert _forms(_component(target)) == _forms(_component(source))
        assert _runnables(target)[DIAGNOSE].events == [TimingEvent(type="timing", period=2.0)]
        assert _runnables(target)[COMPUTE] == _runnables(source)[COMPUTE]
        assert _runnables(target)[ON_WRITTEN] == _runnables(source)[ON_WRITTEN]


# The three regimes


class TestAnUnmarkedFileStatingNoneCarriesTheProjectsForms:
    """An unmarked export stating none keeps the project's forms.

    It never turns an implicit access explicit or an asynchronous call synchronous, and never erases a ``call_returned``
    event.
    """

    def test_an_otherwise_identical_unmarked_file_is_unchanged(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        older = _copy_export(tmp_path, "older", _edit(_without_forms), marked=False)
        before = _tree(target)
        assert import_component(str(older), str(target)).swc_action == "unchanged"
        assert import_component(str(older), str(target), update=True).swc_action == "unchanged"
        assert _tree(target) == before

    def test_an_update_keeps_the_forms_the_synchrony_and_the_events(self, exchanged, tmp_path: Path) -> None:
        source, _arxml, target = exchanged
        older = _copy_export(tmp_path, "older", _edit(_without_forms), _edit(_set_period(2.0)), marked=False)
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(older), str(target))
        assert "the implicit form of reads and writes is not compared either" in str(conflict.value)
        assert "asynchronous calls with their poll, result and timeout" in str(conflict.value)

        report = import_component(str(older), str(target), update=True)

        assert report.swc_action == "updated"
        assert (report.implicit_access_regime, report.asynchronous_call_regime) == (CARRIED, CARRIED)
        assert _forms(_component(target)) == _forms(_component(source))
        compute = _runnables(target)[COMPUTE]
        assert [(c.operation, c.asynchronous, c.poll, c.timeout) for c in compute.calls] == [
            ("WriteBlock", True, False, None),
            ("ReadBlock", True, True, 0.5),
        ], "poll and timeout carried with the synchrony"
        assert report.preserved_implicit_accesses == {COMPUTE: {"reads": 1, "writes": 1}, DIAGNOSE: {"reads": 1}}
        assert report.preserved_asynchronous_calls == {COMPUTE: 2}
        assert report.preserved_call_returned_events == {ON_WRITTEN: 1}
        rendered = report.render()
        assert "IMPLICIT ACCESS regime: carried -- the export carries no SWCraft implicit access marker" in rendered
        assert (
            "ASYNCHRONOUS CALLS regime: carried -- the export carries no SWCraft asynchronous calls marker" in rendered
        )
        assert "IMPLICIT ACCESS preserved from the target project (not carried by the source ARXML):" in rendered
        assert f"  - {COMPUTE}: reads=1, writes=1" in rendered
        assert f"  - {COMPUTE}: asynchronous_calls=2" in rendered
        assert f"  - {ON_WRITTEN}: call_returned_events=1" in rendered
        assert check_cross_layer_consistency(str(target)).consistent

    def test_a_stated_result_is_carried_with_the_poll(self, tmp_path: Path) -> None:
        source = _model(tmp_path / "source")

        def named(document: dict[str, Any]) -> None:
            (compute,) = [r for r in document["runnables"] if r["name"] == COMPUTE]
            (call,) = [c for c in compute["calls"] if c["operation"] == "ReadBlock"]
            call["result"] = "CalibrationResult"

        change(source, CONTROLLER_FILE, named)
        target = init_project(tmp_path / "target") / "model"
        import_component(str(_export(source, tmp_path / "marked.arxml")), str(target))
        older = _copy_export(tmp_path, "older", _edit(_without_forms), _edit(_set_period(2.0)), marked=False)

        import_component(str(older), str(target), update=True)

        (call,) = [c for c in _runnables(target)[COMPUTE].calls if c.operation == "ReadBlock"]
        assert (call.asynchronous, call.poll, call.result, call.timeout) == (True, True, "CalibrationResult", 0.5)

    def test_a_by_value_read_the_file_states_is_not_turned_implicit(self, exchanged, tmp_path: Path) -> None:
        """The carry reaches a pair the file lists with no stated form; a stated ``by: value`` is the file's."""
        _source, _arxml, target = exchanged

        def by_value(document: dict[str, Any]) -> None:
            (diagnose,) = [r for r in document["runnables"] if r["name"] == DIAGNOSE]
            (read,) = [r for r in diagnose["reads"] if r["port"] == "rWheelSpeed"]
            read["by"] = "value"

        older = _copy_export(tmp_path, "older", _edit(_without_forms), _edit(by_value), marked=False)
        with pytest.raises(ComponentImportConflictError):
            import_component(str(older), str(target))

        report = import_component(str(older), str(target), update=True)

        assert [(r.port, r.by) for r in _runnables(target)[DIAGNOSE].reads] == [
            ("rWheelSpeed", "value"),
            ("rPedal", "value"),
        ]
        assert report.preserved_implicit_accesses == {COMPUTE: {"reads": 1, "writes": 1}}

    def test_a_carried_event_whose_call_is_gone_is_kept_and_check_model_reports_it(
        self, exchanged, tmp_path: Path
    ) -> None:
        _source, _arxml, target = exchanged

        def no_write_block(document: dict[str, Any]) -> None:
            _without_forms(document)
            (compute,) = [r for r in document["runnables"] if r["name"] == COMPUTE]
            compute["calls"] = [c for c in compute["calls"] if c["operation"] != "WriteBlock"]

        older = _copy_export(tmp_path, "older", _edit(no_write_block), marked=False)

        report = import_component(str(older), str(target), update=True)

        assert report.preserved_call_returned_events == {ON_WRITTEN: 1}
        assert [type(e) for e in _runnables(target)[ON_WRITTEN].events] == [CallReturnedEvent]
        assert [c.operation for c in _runnables(target)[COMPUTE].calls] == ["ReadBlock"]
        result = check_async_calls(str(target))
        assert [record.rule for record in result.records] == ["swc.call-returned-event.unknown-call"]

    def test_a_runnable_the_file_removed_takes_its_event_with_it(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged

        def drop(document: dict[str, Any]) -> None:
            _without_forms(document)
            document["runnables"] = [r for r in document["runnables"] if r["name"] != ON_WRITTEN]

        older = _copy_export(tmp_path, "older", _edit(drop), marked=False)
        report = import_component(str(older), str(target), update=True)

        assert ON_WRITTEN not in _runnables(target)
        assert report.preserved_call_returned_events == {}


class TestAnUnmarkedFileStatingThemIsTheTruth:
    """A third-party file's implicit access, asynchronous call or returns event is the truth.

    The file's statement holds for the whole component's.
    """

    def test_what_it_states_replaces_the_project_s(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged

        def third_party(document: dict[str, Any]) -> None:
            (diagnose,) = [r for r in document["runnables"] if r["name"] == DIAGNOSE]
            (read,) = [r for r in diagnose["reads"] if r["port"] == "rWheelSpeed"]
            del read["by"]
            (compute,) = [r for r in document["runnables"] if r["name"] == COMPUTE]
            (call,) = [c for c in compute["calls"] if c["operation"] == "ReadBlock"]
            for key in ("asynchronous", "poll", "timeout"):
                del call[key]

        stated = _copy_export(tmp_path, "third", _edit(third_party), marked=False)
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(stated), str(target))
        assert "not compared either" not in str(conflict.value)

        report = import_component(str(stated), str(target), update=True)

        assert (report.implicit_access_regime, report.asynchronous_call_regime) == (STATED, STATED)
        assert (report.preserved_implicit_accesses, report.preserved_asynchronous_calls) == ({}, {})
        assert [(r.port, r.by) for r in _runnables(target)[DIAGNOSE].reads] == [
            ("rWheelSpeed", None),
            ("rPedal", "value"),
        ]
        (read_block,) = [c for c in _runnables(target)[COMPUTE].calls if c.operation == "ReadBlock"]
        assert (read_block.asynchronous, read_block.poll, read_block.timeout) == (False, False, None)
        assert "IMPLICIT ACCESS regime: stated -- " in report.render()

    def test_an_identical_unmarked_file_stating_them_is_unchanged(self, exchanged) -> None:
        _source, arxml, target = exchanged
        assert import_component(str(_unmark(arxml)), str(target)).swc_action == "unchanged"


class TestAMarkedFileReplacesThem:
    def test_a_marked_file_stating_none_removes_every_form_and_event(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        newer = _copy_export(tmp_path, "newer", _edit(_without_forms))
        with pytest.raises(ComponentImportConflictError):
            import_component(str(newer), str(target))

        report = import_component(str(newer), str(target), update=True)

        assert (report.implicit_access_regime, report.asynchronous_call_regime) == (MARKED, MARKED)
        runnables = _runnables(target)
        assert {r.by for runnable in runnables.values() for r in (*runnable.reads, *runnable.writes)} <= {None, "value"}
        assert not [c for runnable in runnables.values() for c in runnable.calls if c.asynchronous]
        assert not [e for runnable in runnables.values() for e in runnable.events if isinstance(e, CallReturnedEvent)]
        assert "ASYNCHRONOUS CALLS regime: marked -- " in report.render()

    def test_a_marked_file_with_one_marker_carries_the_other_family(self, exchanged, tmp_path: Path) -> None:
        """Each family reads its own marker: the asynchronous calls carried, the forms replaced."""
        source, _arxml, target = exchanged

        def no_calls(document: dict[str, Any]) -> None:
            _without_forms(document)

        partial = _unmark(_copy_export(tmp_path, "partial", _edit(no_calls)), ASYNCHRONOUS_CALLS)
        report = import_component(str(partial), str(target), update=True)

        assert (report.implicit_access_regime, report.asynchronous_call_regime) == (MARKED, CARRIED)
        forms = _forms(_component(target))
        assert forms[COMPUTE]["calls"] == _forms(_component(source))[COMPUTE]["calls"]
        assert forms[COMPUTE]["reads"] == [("rWheelSpeed", "speed", None), ("rPedal", "position", None)]


# One traversal of the events

_TIMING = {"type": "timing", "period": 0.1}
_OPERATION = {"type": "operation", "port": "Srv", "operation": "Reset"}
_DATA = {"type": "data", "port": "In", "element": "value"}
_RETURNED = {"type": "call_returned", "port": "Cli", "operation": "Get"}


class TestOneTraversalKeepsEveryOrder:
    @pytest.mark.parametrize(
        "order",
        [[_TIMING, _RETURNED, _DATA, _OPERATION], [_OPERATION, _DATA, _TIMING, _RETURNED], [_RETURNED, _TIMING]],
        ids=["returned-second", "returned-last", "returned-first"],
    )
    def test_carried_events_keep_their_places_beside_a_carried_call_returned_one(self, order: list[Any]) -> None:
        existing = SWComponent.model_validate({"name": "S", "runnables": [{"name": "R", "events": order}]})
        incoming = SWComponent.model_validate(
            {"name": "S", "runnables": [{"name": "R", "events": [{"type": "timing", "period": 0.2}]}]}
        )

        swc, operations, data, returned = capabilities._carry_target_events(
            existing, incoming, operation_events=True, data_events=True, call_returned_events=True
        )

        expected = [{**e, "period": 0.2} if e["type"] == "timing" else e for e in order]
        assert [e.model_dump(exclude_defaults=True) for e in swc.runnables[0].events] == expected
        assert returned == {"R": 1}

    def test_the_product_path_keeps_a_data_event_and_a_call_returned_event_in_order(self, tmp_path: Path) -> None:
        source = _model(tmp_path / "source")

        def events(document: dict[str, Any]) -> None:
            (on_written,) = [r for r in document["runnables"] if r["name"] == ON_WRITTEN]
            on_written["events"] = [
                {"type": "timing", "period": 0.5},
                {"type": "call_returned", "port": "rNvStorage", "operation": "WriteBlock"},
                {"type": "data", "port": "rWheelSpeed", "element": "speed"},
            ]

        change(source, CONTROLLER_FILE, events)
        target = init_project(tmp_path / "target") / "model"
        import_component(str(_export(source, tmp_path / "marked.arxml")), str(target))

        def older_events(document: dict[str, Any]) -> None:
            events(document)
            _without_forms(document)
            (on_written,) = [r for r in document["runnables"] if r["name"] == ON_WRITTEN]
            on_written["events"] = [{"type": "timing", "period": 0.25}]

        older = _copy_export(tmp_path, "older", _edit(older_events), marked=False)
        older = _replace_text(older, f'<SD GID="{DATA_EVENTS.sd_gid}">{DATA_EVENTS.text}</SD>', "")

        report = import_component(str(older), str(target), update=True)

        assert (report.data_event_regime, report.asynchronous_call_regime) == (CARRIED, CARRIED)
        kept = _runnables(target)[ON_WRITTEN].events
        assert [type(e) for e in kept] == [TimingEvent, CallReturnedEvent, DataEvent]
        assert kept[0].period == 0.25


# A withheld behaviour


class TestAWithheldBehaviourKeepsTheForms:
    def test_an_update_keeps_the_project_s_runnables_with_their_forms(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        expected = _component(target)

        def describe(document: dict[str, Any]) -> None:
            document["description"] = "A new description."
            _without_forms(document)

        newer = _with_an_unanswered_mapping_set(_copy_export(tmp_path, "newer", _edit(describe)))

        report = import_component(str(newer), str(target), update=True)

        assert report.behaviour_kept
        assert (report.implicit_access_regime, report.asynchronous_call_regime) == (NONE, NONE)
        kept = _component(target)
        assert kept.description == "A new description."
        assert kept.runnables == expected.runnables

    def test_an_overlay_flagging_an_implicitly_read_element_is_a_conflict_line(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged

        def flagged(document: dict[str, Any]) -> None:
            _without_forms(document)
            (port,) = [p for p in document["ports"] if p["name"] == "rWheelSpeed"]
            port["com_specs"] = [{"data_element": "speed", "enable_update": True}]

        newer = _with_an_unanswered_mapping_set(_copy_export(tmp_path, "newer", _edit(flagged)))
        before = _tree(target)

        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(newer), str(target), update=True)

        assert (
            f"SWC '{CONTROLLER}' keeps the project's own behaviour (the file's is withheld), and the update would "
            f"break it: SWC '{CONTROLLER}' runnable '{COMPUTE}' reads data element 'speed' through port "
            "'rWheelSpeed' implicitly"
        ) in str(conflict.value)
        assert _tree(target) == before

    def test_the_kept_behaviour_is_judged_by_the_new_records(self, tmp_path: Path) -> None:
        source = _model(tmp_path / "source")
        swc = _component(source)
        description = read_data_description(str(source))
        sets = read_mapping_sets(str(source))
        interfaces = read_interfaces(str(source))
        assert _behaviour_findings(swc, interfaces, description, sets) == []

        without_call = swc.model_copy(
            update={
                "runnables": [
                    r.model_copy(update={"calls": [c for c in r.calls if c.operation != "WriteBlock"]})
                    for r in swc.runnables
                ]
            }
        )
        findings = _behaviour_findings(without_call, interfaces, description, sets)
        assert any("has a call_returned event naming operation 'WriteBlock'" in f for f in findings), findings


class TestTheImportJudgesAtTheProjectsVersion:
    """``import component`` judges the asynchronous call rules at the project's ``arxml_version``, not 4.2.2.

    An ``--update`` with a withheld behaviour is accepted at either version: the overlay keeps every runnable
    whole, so a ``swc.call.async-without-result`` finding is the project's own, ``check-model``'s.
    """

    @staticmethod
    def _without_a_result(swc: SWComponent) -> SWComponent:
        return swc.model_copy(update={"runnables": [r for r in swc.runnables if r.name != ON_WRITTEN]})

    @pytest.mark.parametrize(("version", "fires"), [(None, True), ("4.2.2", True), ("4.3.0", False)])
    def test_the_findings_take_the_version(self, tmp_path: Path, version: str | None, fires: bool) -> None:
        source = _model(tmp_path / "source")
        swc = self._without_a_result(_component(source))
        description = read_data_description(str(source))
        findings = _behaviour_findings(
            swc,
            read_interfaces(str(source)),
            description,
            read_mapping_sets(str(source)),
            None if version is None else ArxmlVersion.from_string(version),
        )

        assert any("nothing fetches its result" in finding for finding in findings) is fires

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_cli_hands_the_manifest_s_version_and_the_update_is_accepted(
        self, exchanged, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
    ) -> None:
        from swcraft_tools import cli
        from swcraft_tools.cli import import_commands

        _source, _arxml, target = exchanged
        manifest = target.parent / "swcraft.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f'arxml_version: "{version}"\n', encoding="utf-8", newline="\n"
        )
        # The project's own component: an asynchronous call with no result point.
        (written,) = (target / "swc").rglob(f"{CONTROLLER}.yaml")
        change(
            target,
            written.relative_to(target).as_posix(),
            lambda d: d.update(runnables=[r for r in d["runnables"] if r["name"] != ON_WRITTEN]),
        )

        def describe(document: dict[str, Any]) -> None:
            document["description"] = "A new description."
            _without_forms(document)

        newer = _with_an_unanswered_mapping_set(_copy_export(tmp_path, "newer", _edit(describe)))
        handed: list[Any] = []
        real = import_commands.import_component

        def recording(**arguments: Any) -> Any:
            handed.append(arguments["arxml_version"])
            return real(**arguments)

        monkeypatch.setattr(import_commands, "import_component", recording)
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "component", "--source", str(newer), "--project", str(manifest), "--update"],
        )

        try:
            cli.main()
            exited = 0
        except SystemExit as exit_:
            exited = exit_.code or 0

        assert handed == [version]
        assert exited == 0
        assert _component(target).description == "A new description."


def _drop_on_written(document: dict[str, Any]) -> None:
    """The controller without ``OnBlockWritten``: ``WriteBlock``'s asynchronous call loses its one result point."""
    document["runnables"] = [r for r in document["runnables"] if r["name"] != ON_WRITTEN]


def _drop_on_written_without_forms(document: dict[str, Any]) -> None:
    _without_forms(document)
    _drop_on_written(document)


WRITE_BLOCK_WITHOUT_A_RESULT = (
    f"runnable '{COMPUTE}' calls operation 'WriteBlock' of port 'rNvStorage' asynchronously and nothing "
    "fetches its result: rule swc.call.async-without-result, [constr_2006] of AUTOSAR R4.2.2"
)


class TestEveryImportNamesAnAsynchronousCallLeftWithoutAResult:
    """Every import names each asynchronous call it leaves with no result point, judged at the project's version.

    At 4.2.2 the line is printed and the component written; 4.3.0 allows such a call (``[constr_2006]`` of
    R4.3.0), so there is no line. ``check-model`` at the project's version agrees either way.
    """

    @staticmethod
    def _judged(target: Path, version: ArxmlVersion) -> list[str]:
        return [r.rule for r in check_async_calls(str(target), version).records]

    @staticmethod
    def _assert_the_line(report: Any, version: ArxmlVersion, target: Path) -> None:
        rendered = report.render()
        if version == ArxmlVersion.V4_2_2:
            (line,) = report.async_without_result_lines
            assert line.startswith(WRITE_BLOCK_WITHOUT_A_RESULT), line
            assert "add `poll: true` to the call or a `call_returned` event naming it" in line
            assert "set `arxml_version: 4.3.0` in swcraft.yaml" in line
            assert f"  - {line}" in rendered
            assert "ASYNCHRONOUS CALLS WITHOUT A RESULT (1;" in rendered
            assert TestEveryImportNamesAnAsynchronousCallLeftWithoutAResult._judged(target, version) == [
                "swc.call.async-without-result"
            ]
        else:
            assert report.async_without_result_lines == []
            assert "ASYNCHRONOUS CALLS WITHOUT A RESULT" not in rendered
            assert TestEveryImportNamesAnAsynchronousCallLeftWithoutAResult._judged(target, version) == []
        assert not any(str(target) in line for line in report.async_without_result_lines)

    @pytest.mark.parametrize("version", [ArxmlVersion.V4_2_2, ArxmlVersion.V4_3_0])
    def test_a_new_import_of_a_4_3_0_file(self, tmp_path: Path, version: ArxmlVersion) -> None:
        arxml = _copy_export(tmp_path, "third", _edit(_drop_on_written), version="4.3.0")
        target = init_project(tmp_path / "target") / "model"

        report = import_component(str(arxml), str(target), arxml_version=version)

        assert report.swc_action == "created"
        self._assert_the_line(report, version, target)

    @pytest.mark.parametrize("version", [ArxmlVersion.V4_2_2, ArxmlVersion.V4_3_0])
    def test_a_dry_run_names_it_too_and_writes_nothing(self, tmp_path: Path, version: ArxmlVersion) -> None:
        arxml = _copy_export(tmp_path, "third", _edit(_drop_on_written), version="4.3.0")
        target = init_project(tmp_path / "target") / "model"
        before = _tree(target)

        report = import_component(str(arxml), str(target), dry_run=True, arxml_version=version)

        assert len(report.async_without_result_lines) == (1 if version == ArxmlVersion.V4_2_2 else 0)
        assert _tree(target) == before

    @pytest.mark.parametrize("version", [ArxmlVersion.V4_2_2, ArxmlVersion.V4_3_0])
    def test_a_marked_update(self, exchanged, tmp_path: Path, version: ArxmlVersion) -> None:
        _source, _arxml, target = exchanged
        newer = _copy_export(tmp_path, "newer", _edit(_drop_on_written), version="4.3.0")

        report = import_component(str(newer), str(target), update=True, arxml_version=version)

        assert (report.swc_action, report.asynchronous_call_regime) == ("updated", MARKED)
        self._assert_the_line(report, version, target)

    @pytest.mark.parametrize("version", [ArxmlVersion.V4_2_2, ArxmlVersion.V4_3_0])
    def test_a_stated_update(self, exchanged, tmp_path: Path, version: ArxmlVersion) -> None:
        _source, _arxml, target = exchanged
        stated = _copy_export(tmp_path, "third", _edit(_drop_on_written), marked=False, version="4.3.0")

        report = import_component(str(stated), str(target), update=True, arxml_version=version)

        assert (report.swc_action, report.asynchronous_call_regime) == ("updated", STATED)
        self._assert_the_line(report, version, target)

    @pytest.mark.parametrize("version", [ArxmlVersion.V4_2_2, ArxmlVersion.V4_3_0])
    def test_a_carried_update_whose_file_removed_the_event_s_runnable(
        self, exchanged, tmp_path: Path, version: ArxmlVersion
    ) -> None:
        """The line naming the carried event's removed runnable is the carry's consequence, named.

        The carry puts ``asynchronous`` back on ``WriteBlock`` and its ``call_returned`` event goes with
        ``OnBlockWritten``, which the file removed.
        """
        _source, _arxml, target = exchanged
        older = _copy_export(tmp_path, "older", _edit(_drop_on_written_without_forms), marked=False)

        report = import_component(str(older), str(target), update=True, arxml_version=version)

        assert (report.swc_action, report.asynchronous_call_regime) == ("updated", CARRIED)
        (write_block,) = [c for c in _runnables(target)[COMPUTE].calls if c.operation == "WriteBlock"]
        assert write_block.asynchronous
        self._assert_the_line(report, version, target)

    def test_a_finding_the_project_already_holds_is_not_the_import_s(self, exchanged, tmp_path: Path) -> None:
        """An update names only what it introduces: the project's own call without a result is ``check-model``'s."""
        _source, _arxml, target = exchanged
        (written,) = (target / "swc").rglob(f"{CONTROLLER}.yaml")
        change(target, written.relative_to(target).as_posix(), _drop_on_written)
        newer = _copy_export(tmp_path, "newer", _edit(_drop_on_written), _edit(_set_period(2.0)), version="4.3.0")

        report = import_component(str(newer), str(target), update=True, arxml_version=ArxmlVersion.V4_2_2)

        assert report.swc_action == "updated"
        assert report.async_without_result_lines == []

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_cli_prints_it_at_the_manifest_s_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], version: str
    ) -> None:
        from swcraft_tools import cli

        arxml = _copy_export(tmp_path, "third", _edit(_drop_on_written), version="4.3.0")
        target = init_project(tmp_path / "target") / "model"
        manifest = target.parent / "swcraft.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f'arxml_version: "{version}"\n', encoding="utf-8", newline="\n"
        )
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "import", "component", "--source", str(arxml), "--project", str(manifest)]
        )

        try:
            cli.main()
            exited = 0
        except SystemExit as exit_:
            exited = exit_.code or 0

        printed = capsys.readouterr().out
        assert exited == 0
        assert CONTROLLER in {swc.name for swc in read_swc_model(str(target))}
        assert (WRITE_BLOCK_WITHOUT_A_RESULT in printed) is (version == "4.2.2")


def _diagnose_calls_write_block(document: dict[str, Any]) -> None:
    """``Diagnose`` calls ``rNvStorage.WriteBlock`` too: synchronously, as a carried regime's file states every call."""
    (diagnose,) = [r for r in document["runnables"] if r["name"] == DIAGNOSE]
    diagnose.setdefault("calls", []).append({"port": "rNvStorage", "operation": "WriteBlock"})


def _compute_drops_write_block(document: dict[str, Any]) -> None:
    """``ComputeBrakeTorque`` no longer calls ``rNvStorage.WriteBlock``; ``OnBlockWritten`` stays."""
    (compute,) = [r for r in document["runnables"] if r["name"] == COMPUTE]
    compute["calls"] = [c for c in compute["calls"] if c["operation"] != "WriteBlock"]


def _a_second_asynchronous_caller(arxml: Path) -> Path:
    """*arxml* with ``ComputeBrakeTorque``'s asynchronous ``WriteBlock`` call point copied into ``Diagnose``.

    Valid at either edition (``[constr_2006]`` counts result points per call point); the exporter refuses
    the model it stands for, so the file is edited after the export.
    """
    import copy

    ns = {"a": "http://autosar.org/schema/r4.0"}
    tree = etree.parse(str(arxml))

    def runnable(name: str) -> Any:
        (found,) = tree.xpath(f"//a:RUNNABLE-ENTITY[a:SHORT-NAME='{name}']", namespaces=ns)
        return found

    (call,) = runnable(COMPUTE).xpath(
        "a:SERVER-CALL-POINTS/a:ASYNCHRONOUS-SERVER-CALL-POINT[a:SHORT-NAME='Call_rNvStorage_WriteBlock']",
        namespaces=ns,
    )
    (points,) = runnable(DIAGNOSE).xpath("a:SERVER-CALL-POINTS", namespaces=ns)
    points.append(copy.deepcopy(call))
    tree.write(str(arxml), encoding="UTF-8", xml_declaration=True)
    return arxml


class TestEveryImportNamesTheOtherAsynchronousCallFindingsItIntroduces:
    """Every other asynchronous call finding the import introduces is an ``ASYNCHRONOUS CALL FINDINGS`` line.

    Only the carried regime can introduce one (a carried ``asynchronous`` beside a call the file adds, a
    carried event whose call the file dropped). None is version-aware; the update runs at 4.3.0 so no
    without-result line joins them.
    """

    @staticmethod
    def _assert_the_line(report: Any, target: Path, rule: str) -> None:
        (line,) = report.async_call_finding_lines
        assert line.startswith(f"rule {rule}: SWC '{CONTROLLER}' "), line
        rendered = report.render()
        assert f"  - {line}" in rendered
        assert "ASYNCHRONOUS CALL FINDINGS (1; `check-model` refuses each, at either AUTOSAR version" in rendered
        assert str(target) not in line
        assert report.async_without_result_lines == []

    @pytest.mark.parametrize("dry_run", [False, True], ids=["written", "dry-run"])
    def test_a_carried_asynchronous_call_beside_a_call_the_file_adds(
        self, exchanged, tmp_path: Path, dry_run: bool
    ) -> None:
        _source, _arxml, target = exchanged
        older = _copy_export(tmp_path, "older", _edit(_without_forms), _edit(_diagnose_calls_write_block), marked=False)

        report = import_component(
            str(older), str(target), update=True, dry_run=dry_run, arxml_version=ArxmlVersion.V4_3_0
        )

        assert (report.swc_action, report.asynchronous_call_regime) == ("updated", CARRIED)
        self._assert_the_line(report, target, "swc.call.mixed-synchrony")
        if not dry_run:
            assert [r.rule for r in check_async_calls(str(target), ArxmlVersion.V4_3_0).records] == [
                "swc.call.mixed-synchrony"
            ]

    def test_a_carried_event_whose_call_the_file_dropped(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        older = _copy_export(tmp_path, "older", _edit(_without_forms), _edit(_compute_drops_write_block), marked=False)

        report = import_component(str(older), str(target), update=True, arxml_version=ArxmlVersion.V4_3_0)

        assert (report.swc_action, report.asynchronous_call_regime) == ("updated", CARRIED)
        self._assert_the_line(report, target, "swc.call-returned-event.unknown-call")
        assert [r.rule for r in check_async_calls(str(target), ArxmlVersion.V4_3_0).records] == [
            "swc.call-returned-event.unknown-call"
        ]

    def test_an_ambiguous_call_is_named_and_one_the_target_holds_is_not(self) -> None:
        """The helper over the components themselves: no import path writes an ambiguous call today."""
        from swcraft_tools.component_exchange.resulting_behaviour import _async_call_finding_lines

        clean = _component(FIXTURE)
        runnables = [
            r.model_copy(update={"calls": [*r.calls, clean.runnables[0].calls[0]]}) if r.name == DIAGNOSE else r
            for r in clean.runnables
        ]
        ambiguous = clean.model_copy(update={"runnables": runnables})

        _without, introduced = _async_call_finding_lines(ambiguous, clean, ArxmlVersion.V4_3_0)
        (line,) = introduced
        assert line.startswith(
            f"rule swc.call-returned-event.ambiguous-call: SWC '{CONTROLLER}' runnable '{ON_WRITTEN}' has a "
            "call_returned event naming operation 'WriteBlock' of port 'rNvStorage'"
        ), line
        assert _async_call_finding_lines(ambiguous, ambiguous, ArxmlVersion.V4_3_0) == ([], [])

    def test_a_file_whose_returns_event_names_a_call_of_two_runnables_is_refused(self, tmp_path: Path) -> None:
        arxml = _a_second_asynchronous_caller(_copy_export(tmp_path, "two-callers", version="4.3.0"))
        target = init_project(tmp_path / "target") / "model"
        before = _tree(target)

        with pytest.raises(LossyComponentImportError) as refused:
            import_component(str(arxml), str(target), arxml_version=ArxmlVersion.V4_3_0)

        assert (
            f"runnables '{COMPUTE}' and '{DIAGNOSE}' call rNvStorage.WriteBlock asynchronously; a call_returned "
            "event names the pair, not the caller" in str(refused.value)
        )
        assert _tree(target) == before

    def test_a_file_whose_collapsed_call_points_each_have_a_result_point_is_refused(self, tmp_path: Path) -> None:
        """The importer's ``several-results`` skip is an abort here, as every result point line is.

        The file is valid, and the one call the model keeps holds one result point.
        """
        arxml = _a_polled_twin_of_the_write_call(_copy_export(tmp_path, "twin"))
        target = init_project(tmp_path / "target") / "model"
        before = _tree(target)

        with pytest.raises(LossyComponentImportError) as refused:
            import_component(str(arxml), str(target))

        assert "result point 'SecondWriteResult' references call point 'SecondWrite'" in str(refused.value)
        assert "(rule:swc.call.several-results)" in str(refused.value)
        assert _tree(target) == before


def _a_polled_twin_of_the_write_call(arxml: Path) -> Path:
    """*arxml* with a twin of ``ComputeBrakeTorque``'s write call point and a result point of the twin on it."""
    import copy

    ns = {"a": "http://autosar.org/schema/r4.0"}
    tree = etree.parse(str(arxml))
    (compute,) = tree.xpath(f"//a:RUNNABLE-ENTITY[a:SHORT-NAME='{COMPUTE}']", namespaces=ns)
    (call,) = compute.xpath(
        "a:SERVER-CALL-POINTS/a:ASYNCHRONOUS-SERVER-CALL-POINT[a:SHORT-NAME='Call_rNvStorage_WriteBlock']",
        namespaces=ns,
    )
    twin = copy.deepcopy(call)
    twin.find("a:SHORT-NAME", ns).text = "SecondWrite"
    call.getparent().append(twin)
    (point,) = compute.xpath("a:ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS/*", namespaces=ns)
    second = copy.deepcopy(point)
    second.find("a:SHORT-NAME", ns).text = "SecondWriteResult"
    reference = second.find("a:ASYNCHRONOUS-SERVER-CALL-POINT-REF", ns)
    reference.text = reference.text.rsplit("/", 1)[0] + "/SecondWrite"
    point.getparent().append(second)
    tree.write(str(arxml), encoding="UTF-8", xml_declaration=True)
    return arxml


# The differ


def _runnable_change(old: dict[str, Any], new: dict[str, Any], field: str) -> Any:
    """The *field* collection change of the one changed runnable, as ``swcraft diff`` computes it."""

    def state(runnable: dict[str, Any]) -> ModelState:
        return ModelState(
            layers={
                "swc": [
                    SWComponent(name=CONTROLLER, runnables=[Runnable.model_validate({"name": COMPUTE, **runnable})])
                ]
            }
        )

    (swc,) = diff_model_states(state(old), state(new))["swc"].changed
    (runnables,) = [entry for entry in swc.collection_changes if entry.field == "runnables"]
    (runnable,) = runnables.changed
    (change_,) = [entry for entry in runnable.collection_changes if entry.field == field]
    return change_


def _field_change(old: dict[str, Any], new: dict[str, Any], field: str) -> list[tuple[str, Any, Any]]:
    """The one changed member of *field* and its changed fields."""
    collection = _runnable_change({field: [old]}, {field: [new]}, field)
    assert (collection.added, collection.removed) == ([], [])
    (member,) = collection.changed
    return member.changed_fields


_READ = {"port": "rWheelSpeed", "element": "speed"}
_WRITE = {"port": "pBrakeTorque", "element": "torque"}
_CALL = {"port": "rCalibration", "operation": "ReadBlock"}
_ASYNC = {**_CALL, "asynchronous": True}


class TestTheDifferReportsTheNewFields:
    """The differ reports the access-form fields with no code of its own for them.

    ``by``, ``asynchronous``, ``poll``, ``result`` and ``timeout`` are fields of an identified member, and the event is
    a whole value.
    """

    @pytest.mark.parametrize(
        ("field", "old", "new", "expected"),
        [
            ("reads", _READ, {**_READ, "by": "implicit"}, [("by", None, "implicit")]),
            ("reads", {**_READ, "by": "implicit"}, _READ, [("by", "implicit", None)]),
            ("reads", {**_READ, "by": "value"}, {**_READ, "by": "implicit"}, [("by", "value", "implicit")]),
            ("writes", _WRITE, {**_WRITE, "by": "implicit"}, [("by", None, "implicit")]),
            ("writes", {**_WRITE, "by": "implicit"}, _WRITE, [("by", "implicit", None)]),
            ("calls", _CALL, _ASYNC, [("asynchronous", False, True)]),
            ("calls", _ASYNC, _CALL, [("asynchronous", True, False)]),
            ("calls", _ASYNC, {**_ASYNC, "poll": True}, [("poll", False, True)]),
            ("calls", {**_ASYNC, "poll": True}, _ASYNC, [("poll", True, False)]),
            ("calls", {**_ASYNC, "poll": True}, {**_ASYNC, "poll": True, "result": "R"}, [("result", None, "R")]),
            ("calls", _ASYNC, {**_ASYNC, "timeout": 0.5}, [("timeout", None, 0.5)]),
            ("calls", {**_ASYNC, "timeout": 0.5}, _ASYNC, [("timeout", 0.5, None)]),
            ("calls", {**_ASYNC, "timeout": 0.5}, {**_ASYNC, "timeout": 1.0}, [("timeout", 0.5, 1.0)]),
        ],
        ids=[
            "form-added",
            "form-removed",
            "form-changed",
            "write-form-added",
            "write-form-removed",
            "synchrony-added",
            "synchrony-removed",
            "poll-added",
            "poll-removed",
            "result-added",
            "timeout-added",
            "timeout-removed",
            "timeout-changed",
        ],
    )
    def test_a_member_field_is_a_changed_field_of_its_member(
        self, field: str, old: dict[str, Any], new: dict[str, Any], expected: list[tuple[str, Any, Any]]
    ) -> None:
        assert _field_change(old, new, field) == expected

    _EVENT = {"type": "call_returned", "port": "rNvStorage", "operation": "WriteBlock", "result": None, "name": None}
    _OTHER = {**_EVENT, "operation": "ReadBlock"}

    def test_an_added_event(self) -> None:
        events = _runnable_change({"events": []}, {"events": [self._EVENT]}, "events")
        assert (events.added, events.removed, events.changed) == ([self._EVENT], [], [])

    def test_a_removed_event(self) -> None:
        events = _runnable_change({"events": [self._EVENT]}, {"events": []}, "events")
        assert (events.added, events.removed, events.changed) == ([], [self._EVENT], [])

    def test_a_changed_event_is_a_removal_plus_an_addition(self) -> None:
        events = _runnable_change({"events": [self._EVENT]}, {"events": [self._OTHER]}, "events")
        assert (events.added, events.removed, events.changed) == ([self._OTHER], [self._EVENT], [])


# The implicit reads a carried update writes


def _wheel_speed_flags_enable_update(document: dict[str, Any]) -> None:
    """``rWheelSpeed``'s com-spec states ``enable_update: true`` for ``speed``, which both runnables read."""
    for port in document["ports"]:
        if port["name"] == "rWheelSpeed":
            port["com_specs"] = [{"data_element": "speed", "enable_update": True}]


def _a_queued_wheel_speed_interface(copy: Path) -> None:
    put(
        copy,
        "interfaces/WheelSpeedQIf.yaml",
        {
            "name": "WheelSpeedQIf",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "uint16", "sw_impl_policy": "queued"}],
        },
    )


def _wheel_speed_through_the_queued_interface(document: dict[str, Any]) -> None:
    for port in document["ports"]:
        if port["name"] == "rWheelSpeed":
            port["interface"] = "WheelSpeedQIf"
            port["com_specs"] = [{"data_element": "speed", "queue_length": 4}]


class TestACarriedUpdateNamesTheImplicitReadFindingsItIntroduces:
    """A read the carried regime turns implicit and ``check-model`` refuses is an ``IMPLICIT ACCESS FINDINGS`` line.

    The carry asks neither the file's com-spec nor its interface; neither rule is version-aware."""

    @staticmethod
    def _assert_the_lines(report: Any, target: Path, rule: str) -> None:
        assert (report.swc_action, report.implicit_access_regime) == ("updated", CARRIED)
        assert [line.split(": ", 1)[0] for line in report.implicit_access_finding_lines] == [f"rule {rule}"] * 2
        assert [
            f"runnable '{name}'" in line
            for name, line in zip((COMPUTE, DIAGNOSE), report.implicit_access_finding_lines, strict=True)
        ] == [True, True]
        rendered = report.render()
        assert "IMPLICIT ACCESS FINDINGS (2; `check-model` refuses each, at either AUTOSAR version" in rendered
        assert all(f"  - {line}" in rendered for line in report.implicit_access_finding_lines)
        assert all(str(target) not in line for line in report.implicit_access_finding_lines)

    @pytest.mark.parametrize("dry_run", [False, True], ids=["written", "dry-run"])
    def test_a_com_spec_the_file_flags_enable_update(self, exchanged, tmp_path: Path, dry_run: bool) -> None:
        _source, _arxml, target = exchanged
        older = _copy_export(
            tmp_path, "older", _edit(_without_forms), _edit(_wheel_speed_flags_enable_update), marked=False
        )
        before = _tree(target)

        report = import_component(str(older), str(target), update=True, dry_run=dry_run)

        self._assert_the_lines(report, target, "swc.access.implicit-read-enable-update")
        if dry_run:
            assert _tree(target) == before
        else:
            assert [r.rule for r in check_cross_layer_consistency(str(target)).records] == [
                "swc.access.implicit-read-enable-update"
            ] * 2

    def test_an_element_the_file_makes_queued(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        older = _copy_export(
            tmp_path,
            "older",
            _a_queued_wheel_speed_interface,
            _edit(_without_forms),
            _edit(_wheel_speed_through_the_queued_interface),
            marked=False,
        )

        report = import_component(str(older), str(target), update=True)

        self._assert_the_lines(report, target, "swc.access.queued-implicit-read")
        assert [r.rule for r in check_cross_layer_consistency(str(target)).records] == [
            "swc.access.queued-implicit-read"
        ] * 2

    def test_a_finding_the_project_already_holds_is_not_the_import_s(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        (written,) = (target / "swc").rglob(f"{CONTROLLER}.yaml")
        change(target, written.relative_to(target).as_posix(), _wheel_speed_flags_enable_update)
        older = _copy_export(
            tmp_path,
            "older",
            _edit(_without_forms),
            _edit(_wheel_speed_flags_enable_update),
            _edit(_set_period(2.0)),
            marked=False,
        )

        report = import_component(str(older), str(target), update=True)

        assert (report.swc_action, report.implicit_access_regime) == ("updated", CARRIED)
        assert report.implicit_access_finding_lines == []
        assert "IMPLICIT ACCESS FINDINGS" not in report.render()

    def test_a_finding_the_file_states_itself_is_the_reader_s_line_not_this_one(self) -> None:
        """The helper over the components: a read the file's own component holds is named by the reader."""
        from swcraft_tools.component_exchange.resulting_behaviour import _implicit_read_finding_lines

        clean = _component(FIXTURE)
        flagged = clean.model_copy(
            update={
                "ports": [
                    type(p).model_validate(
                        {**p.model_dump(), "com_specs": [{"data_element": "speed", "enable_update": True}]}
                    )
                    if p.name == "rWheelSpeed"
                    else p
                    for p in clean.ports
                ]
            }
        )
        interfaces = read_interfaces(FIXTURE)

        assert len(_implicit_read_finding_lines(flagged, clean, clean, {}, interfaces)) == 2
        assert _implicit_read_finding_lines(flagged, None, flagged, {}, interfaces) == []
