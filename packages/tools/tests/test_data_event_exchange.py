"""Component exchange: the data-received events survive the round trip.

``swcraft generate arxml --component`` writes a runnable's data-received events through the writer,
refuses one naming no port or element through the closure check, and writes the capability marker
``SD GID="DATA-EVENTS"`` into the behaviour's ``SWCRAFT`` group. ``swcraft import component`` reads
them back through ``map_swcs`` and decides **per component**, by that marker and by what the file
states, which regime governs an ``--update``:

* **marked** -- the file is the truth for every runnable's data-received events, absence included:
  they are compared, and ``--update`` replaces them;
* **stated** -- no marker, and the file states a data-received event on any runnable (a third-party
  export): it is the truth for every runnable's;
* **carried** -- no marker and none stated (an export written before contract 12.3.0): both sides
  leave them out of the comparison and ``--update`` keeps the project's own, by runnable name.

Operation events keep their per-runnable rule, and the two carries are one traversal of the target's
events, so a timing, an operation and a data-received event keep their relative order. A marker value
the writer does not mint is refused.

The subject is the shared fixture (``data_event_support``) made stub-free (a component export refuses
an external stub), exported by the real exporter and edited as text where a test needs an older, a
third-party or a damaged file.

**Not reached:** a file an earlier SWCraft wrote itself (the *unmarked* file is this export with the
marker removed); a disabled mode on a data-received event in a component export (no export can carry
the mode-switch stub; the late pass's verdict is tested from an edited file); the real tool exports;
the MCP surface's rendering of the same :class:`ComponentImportReport`.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import write_model_element
from data_event_support import (
    COMPONENT,
    COMPONENT_FILE,
    FILTER,
    FRONT_LEFT,
    FRONT_RIGHT,
    MONITOR,
    WHEEL_INTERFACE,
    WHEEL_PORT,
    data_event,
    stub_free_data_event_model,
)
from external_stubs_support import change, put
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.capability_markers import DATA_EVENTS, EXCLUSIVE_AREAS
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
from swcraft_tools.component_exchange.dependencies import MissingReference
from swcraft_tools.component_exchange.import_ import plan_component_import
from swcraft_tools.component_exchange.name_policies import illegal_identifiers
from swcraft_tools.component_exchange.resulting_behaviour import _behaviour_findings
from swcraft_tools.data_description import read_data_description
from swcraft_tools.differ import diff_model_states
from swcraft_tools.model_state import ModelState
from swcraft_tools.reader import read_interfaces, read_mapping_sets, read_swc_model
from swcraft_tools.schema import DataEvent, OperationEvent, Port, Runnable, SWComponent, TimingEvent
from swcraft_tools.validator import check_cross_layer_consistency, data_event_records
from swcraft_tools.writer.arxml_generation import (
    generate_arxml,
)

DATA_MARKER_LINE = re.compile(r'\n *<SD GID="DATA-EVENTS">[^<]*</SD>')
OPERATION_MARKER_LINE = re.compile(r'\n *<SD GID="OPERATION-EVENTS">[^<]*</SD>')

#: The client/server interface and the provide port the order cases add, and the operation.
SERVICE_INTERFACE = "SpeedServiceIf"
SERVICE_PORT = "P_SpeedService"
RESET = "Reset"


# Helpers


def _model(destination: Path) -> Path:
    return stub_free_data_event_model(destination)


def _export(model_root: Path, arxml: Path) -> Path:
    export_component(str(model_root), COMPONENT, str(arxml))
    return arxml


def _component(model_root: Path) -> SWComponent:
    (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == COMPONENT]
    return swc


def _runnable(model_root: Path, name: str) -> Runnable:
    return next(r for r in _component(model_root).runnables if r.name == name)


def _data_events(swc: SWComponent) -> dict[str, list[tuple[str, str]]]:
    return {r.name: [(e.port, e.element) for e in r.events if isinstance(e, DataEvent)] for r in swc.runnables}


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _edit(edit: Any) -> Any:
    """A model-copy edit applying *edit* to the component's document."""
    return lambda copy: change(copy, COMPONENT_FILE, edit)


def _without_data_events(document: dict[str, Any]) -> None:
    for runnable in document["runnables"]:
        runnable["events"] = [e for e in runnable["events"] if e["type"] != "data"]


def _set_period(period: float) -> Any:
    def edit(document: dict[str, Any]) -> None:
        (monitor,) = [r for r in document["runnables"] if r["name"] == MONITOR]
        for event in monitor["events"]:
            if event["type"] == "timing":
                event["period"] = period

    return edit


def _strip(arxml: Path, pattern: re.Pattern[str], gid: str) -> Path:
    text = arxml.read_text(encoding="utf-8")
    assert len(pattern.findall(text)) == 1
    arxml.write_text(pattern.sub("", text), encoding="utf-8", newline="\n")
    assert f'<SD GID="{gid}">' not in arxml.read_text(encoding="utf-8")
    return arxml


def _unmark(arxml: Path) -> Path:
    """Remove the data-received events marker, which every export before contract 12.3.0 lacked."""
    return _strip(arxml, DATA_MARKER_LINE, DATA_EVENTS.sd_gid)


def _replace_text(arxml: Path, old: str, new: str, count: int = 1) -> Path:
    text = arxml.read_text(encoding="utf-8")
    assert text.count(old) == count, (old, text.count(old))
    arxml.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return arxml


def _copy_export(tmp_path: Path, name: str, *edits: Any, marked: bool = True, source: Path | None = None) -> Path:
    """An export of a fresh fixture copy (or of *source*'s edits), each of *edits* applied first."""
    copy = _model(tmp_path / f"{name}-model") if source is None else source
    for edit in edits:
        edit(copy)
    arxml = _export(copy, tmp_path / f"{name}.arxml")
    return arxml if marked else _unmark(arxml)


@pytest.fixture
def exchanged(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The stub-free fixture, its marked export, and a project holding the component imported from it.

    Returns ``(source model root, marked export, target model root)``.
    """
    source = _model(tmp_path / "source")
    arxml = _export(source, tmp_path / "marked.arxml")
    target = init_project(tmp_path / "target") / "model"
    assert import_component(str(arxml), str(target)).swc_action == "created"
    return source, arxml, target


# The closure


class TestTheClosureCheckJudgesTheEvents:
    def test_the_export_writes_every_event(self, tmp_path: Path) -> None:
        text = _export(_model(tmp_path / "source"), tmp_path / "c.arxml").read_text(encoding="utf-8")
        assert text.count("<DATA-RECEIVED-EVENT>") == 2
        for element in (FRONT_LEFT, FRONT_RIGHT):
            assert f"<SHORT-NAME>DRT_{WHEEL_PORT}_{element}</SHORT-NAME>" in text

    def test_an_event_naming_no_element_is_the_closure_check_s_refusal(self, tmp_path: Path) -> None:
        source = _model(tmp_path / "source")

        def dangling(document: dict[str, Any]) -> None:
            (filter_speed,) = [r for r in document["runnables"] if r["name"] == FILTER]
            filter_speed["events"] = [data_event(WHEEL_PORT, "Missing")]

        change(source, COMPONENT_FILE, dangling)
        with pytest.raises(ClosureValidationError) as refusal:
            _export(source, tmp_path / "component.arxml")
        assert any(
            f"naming element 'Missing' of port '{WHEEL_PORT}', which interface '{WHEEL_INTERFACE}' does not declare"
            in issue
            for issue in refusal.value.issues
        )
        assert not (tmp_path / "component.arxml").exists()

    def test_an_event_naming_no_port_is_the_closure_check_s_refusal(self, tmp_path: Path) -> None:
        source = _model(tmp_path / "source")

        def dangling(document: dict[str, Any]) -> None:
            (filter_speed,) = [r for r in document["runnables"] if r["name"] == FILTER]
            filter_speed["events"] = [data_event("R_Nowhere", FRONT_LEFT)]

        change(source, COMPONENT_FILE, dangling)
        with pytest.raises(ClosureValidationError) as refusal:
            _export(source, tmp_path / "component.arxml")
        assert any("naming port 'R_Nowhere', which the component does not declare" in i for i in refusal.value.issues)
        assert not (tmp_path / "component.arxml").exists()


# The marker


class TestTheExportCarriesTheMarker:
    def test_every_component_export_carries_it_last_and_the_whole_model_does_not(self, tmp_path: Path) -> None:
        source = _model(tmp_path / "source")
        arxml = _export(source, tmp_path / "component.arxml")
        component = arxml.read_text(encoding="utf-8")
        assert f'<SD GID="{DATA_EVENTS.sd_gid}">{DATA_EVENTS.text}</SD>' in component
        assert component.index(f'<SD GID="{EXCLUSIVE_AREAS.sd_gid}">') < component.index(
            f'<SD GID="{DATA_EVENTS.sd_gid}">'
        ), "after the exclusive areas marker"
        (group,) = [
            node
            for node in etree.parse(str(arxml)).iter()
            if etree.QName(node).localname == "SDG" and node.get("GID") == "SWCRAFT"
        ]
        # The last of the markers before contract 12.7.0, whose two follow it.
        assert [child.get("GID") for child in group][-3:] == [
            DATA_EVENTS.sd_gid,
            "IMPLICIT-ACCESS",
            "ASYNCHRONOUS-CALLS",
        ], "the sixth of the component export's markers"
        whole = tmp_path / "whole.arxml"
        generate_arxml(str(source), str(whole))
        assert f'<SD GID="{DATA_EVENTS.sd_gid}">' not in whole.read_text(encoding="utf-8")

    def test_a_component_with_no_data_event_carries_it_too(self, tmp_path: Path) -> None:
        """A statement about the exporter: a marked file with no data-received event says *none*."""
        source = _model(tmp_path / "source")
        change(source, COMPONENT_FILE, _without_data_events)
        text = _export(source, tmp_path / "component.arxml").read_text(encoding="utf-8")
        assert "<DATA-RECEIVED-EVENT>" not in text
        assert f'<SD GID="{DATA_EVENTS.sd_gid}">' in text

    def test_the_reader_admits_the_group_and_reads_the_marker(self, tmp_path: Path) -> None:
        model = read_arxml(str(_export(_model(tmp_path / "source"), tmp_path / "component.arxml")))
        assert model.swcs[0].swcraft_markers[DATA_EVENTS.sd_gid] == DATA_EVENTS.text
        assert not [entry for entry in model.skip_log if "ADMIN-DATA" in entry.reason]

    def test_the_marker_is_a_capability_token_never_a_version(self) -> None:
        """One copy in the leaf ``capability_markers``, which the writer mints from and the reader admits."""
        assert DATA_EVENTS.sd_gid == "DATA-EVENTS"
        assert not re.search(r"\d", DATA_EVENTS.text), "a capability, never a version"

    def test_a_value_the_writer_does_not_mint_is_refused_with_nothing_written(self, exchanged) -> None:
        _source, arxml, target = exchanged
        _replace_text(arxml, f">{DATA_EVENTS.text}<", ">something else<")
        before = _tree(target)
        for update in (False, True):
            with pytest.raises(InvalidComponentArxmlError) as refusal:
                import_component(str(arxml), str(target), update=update)
            assert "DATA-EVENTS marker with the value 'something else'" in str(refusal.value)
        assert _tree(target) == before


# The round trip


class TestTheEventsSurviveTheRoundTrip:
    def test_the_component_exports_and_reimports_unchanged(self, exchanged) -> None:
        source, arxml, target = exchanged
        assert _data_events(_component(target)) == _data_events(_component(source))
        assert _runnable(target, MONITOR).events == _runnable(source, MONITOR).events, "declared order kept"
        before = _tree(target)
        for update in (False, True):
            report = import_component(str(arxml), str(target), update=update)
            assert report.swc_action == "unchanged"
            assert report.data_event_regime == NONE
            assert "DATA-RECEIVED EVENTS regime" not in report.render()
        assert _tree(target) == before

    def test_the_imported_project_checks_clean(self, exchanged) -> None:
        _source, _arxml, target = exchanged
        result = check_cross_layer_consistency(str(target))
        assert result.consistent, result.issues

    def test_the_import_s_own_export_is_unchanged_too(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        again = _export(target, tmp_path / "again.arxml")
        assert import_component(str(again), str(target)).swc_action == "unchanged"


# The three regimes


class TestAnUnmarkedFileStatingNoneCarriesTheProjectsEvents:
    """An export written before contract 12.3.0 never erases the project's data-received events."""

    def test_an_otherwise_identical_unmarked_file_is_unchanged(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        older = _copy_export(tmp_path, "older", _edit(_without_data_events), marked=False)
        before = _tree(target)
        assert import_component(str(older), str(target)).swc_action == "unchanged"
        assert import_component(str(older), str(target), update=True).swc_action == "unchanged"
        assert _tree(target) == before

    def test_an_update_keeps_the_events_in_their_places(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        older = _copy_export(tmp_path, "older", _edit(_without_data_events), _edit(_set_period(0.2)), marked=False)
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(older), str(target))
        assert "data-received events are not compared either" in str(conflict.value)
        expected = _data_events(_component(target))

        report = import_component(str(older), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.data_event_regime == CARRIED
        kept = _component(target)
        assert _data_events(kept) == expected
        monitor = next(r for r in kept.runnables if r.name == MONITOR)
        assert [type(e) for e in monitor.events] == [TimingEvent, DataEvent], "the carried event keeps its place"
        assert monitor.events[0].period == 0.2
        assert report.preserved_data_events == {FILTER: 1, MONITOR: 1}
        rendered = report.render()
        assert (
            "DATA-RECEIVED EVENTS regime: carried -- the export carries no SWCraft data-received events marker"
            in rendered
        )
        assert "DATA-RECEIVED EVENTS preserved from the target project (not carried by the source ARXML):" in rendered
        assert f"  - {FILTER}: data_events=1" in rendered
        assert data_event_records([kept], read_interfaces(str(target))) == []
        assert check_cross_layer_consistency(str(target)).consistent

    def test_a_runnable_the_file_removed_takes_its_events_with_it(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged

        def drop_filter(document: dict[str, Any]) -> None:
            _without_data_events(document)
            document["runnables"] = [r for r in document["runnables"] if r["name"] != FILTER]

        older = _copy_export(tmp_path, "older", _edit(drop_filter), marked=False)
        report = import_component(str(older), str(target), update=True)

        kept = _component(target)
        assert FILTER not in _data_events(kept)
        assert report.preserved_data_events == {MONITOR: 1}
        assert _data_events(kept)[MONITOR] == [(WHEEL_PORT, FRONT_RIGHT)]

    def test_a_target_without_data_events_carries_nothing_and_reads_as_before(self, tmp_path: Path) -> None:
        source = _model(tmp_path / "source")
        change(source, COMPONENT_FILE, _without_data_events)
        target = init_project(tmp_path / "target") / "model"
        assert import_component(str(_export(source, tmp_path / "marked.arxml")), str(target)).swc_action == "created"
        older = _copy_export(tmp_path, "older", _edit(_without_data_events), _edit(_set_period(0.2)), marked=False)
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(older), str(target))
        assert "data-received" not in str(conflict.value)

        report = import_component(str(older), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.data_event_regime == CARRIED
        assert report.preserved_data_events == {}
        rendered = report.render()
        assert "DATA-RECEIVED EVENTS preserved" not in rendered
        assert "DATA-RECEIVED EVENTS regime: carried -- " in rendered


class TestAnUnmarkedFileStatingOneIsTheTruthForEveryRunnable:
    """One stated data-received event makes the file the truth for every runnable of the component."""

    def test_a_runnable_it_states_none_for_loses_its_event(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged

        def monitor_without(document: dict[str, Any]) -> None:
            (monitor,) = [r for r in document["runnables"] if r["name"] == MONITOR]
            monitor["events"] = [e for e in monitor["events"] if e["type"] != "data"]

        third_party = _copy_export(tmp_path, "third", _edit(monitor_without), marked=False)
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(third_party), str(target))
        assert "data-received events are not compared either" not in str(conflict.value)

        report = import_component(str(third_party), str(target), update=True)

        assert report.data_event_regime == STATED
        assert report.preserved_data_events == {}
        assert _data_events(_component(target)) == {FILTER: [(WHEEL_PORT, FRONT_LEFT)], MONITOR: []}
        assert "DATA-RECEIVED EVENTS regime: stated -- " in report.render()

    def test_an_identical_unmarked_file_stating_them_is_unchanged(self, exchanged, tmp_path: Path) -> None:
        source, arxml, target = exchanged
        third_party = _unmark(arxml)
        assert import_component(str(third_party), str(target)).swc_action == "unchanged"
        assert source.is_dir()

    def test_a_changed_event_is_compared_and_replaced(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged

        def front_right(document: dict[str, Any]) -> None:
            (filter_speed,) = [r for r in document["runnables"] if r["name"] == FILTER]
            filter_speed["events"] = [data_event(WHEEL_PORT, FRONT_RIGHT)]

        third_party = _copy_export(tmp_path, "third", _edit(front_right), marked=False)
        with pytest.raises(ComponentImportConflictError):
            import_component(str(third_party), str(target))
        import_component(str(third_party), str(target), update=True)
        assert _data_events(_component(target))[FILTER] == [(WHEEL_PORT, FRONT_RIGHT)]


class TestAMarkedFileReplacesTheEvents:
    def test_a_removed_event_is_removed(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged

        def drop(document: dict[str, Any]) -> None:
            (filter_speed,) = [r for r in document["runnables"] if r["name"] == FILTER]
            filter_speed["events"] = []

        newer = _copy_export(tmp_path, "newer", _edit(drop))
        with pytest.raises(ComponentImportConflictError):
            import_component(str(newer), str(target))

        report = import_component(str(newer), str(target), update=True)

        assert report.data_event_regime == MARKED
        assert report.preserved_data_events == {}
        assert _data_events(_component(target)) == {FILTER: [], MONITOR: [(WHEEL_PORT, FRONT_RIGHT)]}
        assert "DATA-RECEIVED EVENTS regime: marked -- " in report.render()

    def test_a_marked_file_stating_none_removes_them_all(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        newer = _copy_export(tmp_path, "newer", _edit(_without_data_events))
        with pytest.raises(ComponentImportConflictError):
            import_component(str(newer), str(target))
        report = import_component(str(newer), str(target), update=True)
        assert report.data_event_regime == MARKED
        assert _data_events(_component(target)) == {FILTER: [], MONITOR: []}

    def test_a_marked_file_adds_an_event_the_target_lacks(self, tmp_path: Path) -> None:
        source = _model(tmp_path / "source")
        change(source, COMPONENT_FILE, _without_data_events)
        target = init_project(tmp_path / "target") / "model"
        import_component(str(_export(source, tmp_path / "plain.arxml")), str(target))

        newer = _copy_export(tmp_path, "newer")
        report = import_component(str(newer), str(target), update=True)

        assert report.data_event_regime == MARKED
        assert _data_events(_component(target)) == {
            FILTER: [(WHEEL_PORT, FRONT_LEFT)],
            MONITOR: [(WHEEL_PORT, FRONT_RIGHT)],
        }


# One carry: each relative order of a timing, an operation and a data-received event

_KINDS = ("timing", "operation", "data")


def _serving(order: tuple[str, ...]) -> Any:
    """A model-copy edit: the service interface and port, and the monitor's three events in *order*."""

    def edit(copy: Path) -> None:
        put(
            copy,
            f"interfaces/{SERVICE_INTERFACE}.yaml",
            {"name": SERVICE_INTERFACE, "kind": "client_server", "operations": [{"name": RESET}]},
        )
        events = {
            "timing": {"type": "timing", "period": 0.1},
            "operation": {"type": "operation", "port": SERVICE_PORT, "operation": RESET},
            "data": data_event(WHEEL_PORT, FRONT_RIGHT),
        }

        def component(document: dict[str, Any]) -> None:
            document["ports"].append({"name": SERVICE_PORT, "direction": "provide", "interface": SERVICE_INTERFACE})
            (monitor,) = [r for r in document["runnables"] if r["name"] == MONITOR]
            monitor["events"] = [events[kind] for kind in order]

        change(copy, COMPONENT_FILE, component)

    return edit


def _silent_about_both(document: dict[str, Any]) -> None:
    """What an export written before contract 7.7.0 states: no operation and no data-received event."""
    for runnable in document["runnables"]:
        runnable["events"] = [e for e in runnable["events"] if e["type"] not in ("data", "operation")]


class TestOneCarryKeepsEveryOrder:
    @pytest.mark.parametrize("order", list(itertools.permutations(_KINDS)), ids="-".join)
    def test_a_period_changing_update_from_a_file_unmarked_for_both_keeps_the_order(
        self, tmp_path: Path, order: tuple[str, ...]
    ) -> None:
        source = _model(tmp_path / "source")
        _serving(order)(source)
        target = init_project(tmp_path / "target") / "model"
        assert import_component(str(_export(source, tmp_path / "marked.arxml")), str(target)).swc_action == "created"
        expected = _runnable(target, MONITOR).events
        assert [e.type for e in expected] == list(order)

        older = _copy_export(
            tmp_path, "older", _serving(order), _edit(_silent_about_both), _edit(_set_period(0.2)), marked=False
        )
        _strip(older, OPERATION_MARKER_LINE, "OPERATION-EVENTS")

        report = import_component(str(older), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.operation_event_regime == CARRIED
        assert report.data_event_regime == CARRIED
        events = _runnable(target, MONITOR).events
        assert [e.type for e in events] == list(order)
        for got, want in zip(events, expected, strict=True):
            if isinstance(want, TimingEvent):
                assert got == want.model_copy(update={"period": 0.2})
            else:
                assert got == want
        assert report.preserved_operation_events == {MONITOR: {"operation_events": 1}}
        assert report.preserved_data_events == {FILTER: 1, MONITOR: 1}
        assert check_cross_layer_consistency(str(target)).consistent

    def test_an_operation_marked_file_keeps_the_data_events_and_replaces_the_operation_event(
        self, tmp_path: Path
    ) -> None:
        """Marked for operation events, silent for data-received events: only the data kind is carried."""
        order = ("data", "operation", "timing")
        source = _model(tmp_path / "source")
        _serving(order)(source)
        target = init_project(tmp_path / "target") / "model"
        import_component(str(_export(source, tmp_path / "marked.arxml")), str(target))

        older = _copy_export(
            tmp_path, "older", _serving(order), _edit(_silent_about_both), _edit(_set_period(0.2)), marked=False
        )
        report = import_component(str(older), str(target), update=True)

        assert report.data_event_regime == CARRIED
        assert report.preserved_operation_events == {}
        events = _runnable(target, MONITOR).events
        assert [e.type for e in events] == ["data", "timing"]
        assert not any(isinstance(e, OperationEvent) for e in events)


# A withheld behaviour, and the overlay's predicates


def _with_an_unanswered_mapping_set(arxml: Path) -> Path:
    """*arxml* with a ``DATA-TYPE-MAPPING-REF`` onto a set neither the file nor the target declares."""
    return _replace_text(
        arxml,
        "<HANDLE-TERMINATION-AND-RESTART>",
        '<DATA-TYPE-MAPPING-REFS><DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Maps'
        "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS><HANDLE-TERMINATION-AND-RESTART>",
    )


class TestAWithheldBehaviourKeepsTheEvents:
    def test_an_update_keeps_the_project_s_runnables_with_their_events(self, exchanged, tmp_path: Path) -> None:
        _source, _arxml, target = exchanged
        expected = _component(target)

        def describe(document: dict[str, Any]) -> None:
            document["description"] = "A new description."
            _without_data_events(document)

        newer = _with_an_unanswered_mapping_set(_copy_export(tmp_path, "newer", _edit(describe)))

        report = import_component(str(newer), str(target), update=True)

        assert report.behaviour_kept
        assert report.swc_action == "updated"
        assert report.data_event_regime == NONE
        kept = _component(target)
        assert kept.description == "A new description."
        assert kept.runnables == expected.runnables
        assert _data_events(kept) == _data_events(expected)

    def test_an_overlay_removing_the_port_a_kept_event_names_is_a_conflict_line(
        self, exchanged, tmp_path: Path
    ) -> None:
        _source, _arxml, target = exchanged

        def portless(document: dict[str, Any]) -> None:
            document["ports"] = []
            for runnable in document["runnables"]:
                runnable["events"] = [e for e in runnable["events"] if e["type"] == "timing"]
                runnable.pop("reads", None)

        newer = _with_an_unanswered_mapping_set(_copy_export(tmp_path, "newer", _edit(portless)))
        before = _tree(target)

        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(newer), str(target), update=True)

        assert (
            f"SWC '{COMPONENT}' keeps the project's own behaviour (the file's is withheld), and the update would "
            f"break it: SWC '{COMPONENT}' runnable '{MONITOR}' has a data-received event naming port '{WHEEL_PORT}'"
        ) in str(conflict.value)
        assert _tree(target) == before

    def test_the_kept_behaviour_is_judged_by_the_data_event_records(self, tmp_path: Path) -> None:
        """An overlay whose kept event names an element its interface no longer declares is a break."""
        source = _model(tmp_path / "source")
        swc = _component(source)
        description = read_data_description(str(source))
        sets = read_mapping_sets(str(source))
        interfaces = read_interfaces(str(source))
        assert _behaviour_findings(swc, interfaces, description, sets) == []

        narrowed = [
            interface.model_copy(
                update={"data_elements": [e for e in interface.data_elements if e.name != FRONT_RIGHT]}
            )
            if interface.name == WHEEL_INTERFACE
            else interface
            for interface in interfaces
        ]
        findings = _behaviour_findings(swc, narrowed, description, sets)
        assert (
            f"SWC '{COMPONENT}' runnable '{MONITOR}' has a data-received event naming element '{FRONT_RIGHT}' of "
            f"port '{WHEEL_PORT}', which interface '{WHEEL_INTERFACE}' does not declare."
        ) in findings


# The identifier guard, and a skip the import cannot carry


class TestTheIdentifierGuardJudgesTheEventAsAPair:
    def test_an_over_budget_event_name_is_refused_as_a_pair(self) -> None:
        port, element = "R" + "p" * 69, "E" + "e" * 69
        swc = SWComponent(
            name=COMPONENT,
            type="application",
            ports=[Port(name=port, direction="require", interface=WHEEL_INTERFACE)],
            runnables=[Runnable(name="Run", events=[DataEvent(type="data", port=port, element=element)])],
        )
        (violation,) = illegal_identifiers(swc, [], [])
        assert violation.startswith(
            f"SWC '{COMPONENT}' runnable 'Run' data-received event '{port}'.'{element}' generates the SHORT-NAME "
            f"'DRT_{port}_{element}' (145 characters; '{port}' and '{element}' get "
        ), violation
        assert "so shorten" in violation

    def test_an_illegal_declared_port_name_is_not_charged_to_the_drt_form(self) -> None:
        swc = SWComponent(
            name=COMPONENT,
            type="application",
            ports=[Port(name="Bad Port", direction="require", interface=WHEEL_INTERFACE)],
            runnables=[Runnable(name="Run", events=[DataEvent(type="data", port="Bad Port", element=FRONT_LEFT)])],
        )
        violations = illegal_identifiers(swc, [], [])
        assert len(violations) == 1, violations
        assert "port name 'Bad Port' is not a legal AUTOSAR Identifier" in violations[0]

    def test_a_runnable_named_like_a_carried_event_is_refused_with_nothing_written(
        self, exchanged, tmp_path: Path
    ) -> None:
        """The carried event's ``DRT_`` name meets a runnable the unmarked file adds under that name."""
        _source, _arxml, target = exchanged
        taken = f"DRT_{WHEEL_PORT}_{FRONT_LEFT}"

        def add(document: dict[str, Any]) -> None:
            _without_data_events(document)
            document["runnables"].append({"name": taken, "events": [{"type": "timing", "period": 0.5}]})

        older = _copy_export(tmp_path, "older", _edit(add), marked=False)
        before = _tree(target)
        with pytest.raises(ReservedNameError):
            import_component(str(older), str(target), update=True)
        assert _tree(target) == before


class TestANonDeferredSkipAbortsTheImport:
    def test_an_event_through_an_abstract_port_reference_aborts_with_its_reason(self, tmp_path: Path) -> None:
        arxml = _export(_model(tmp_path / "source"), tmp_path / "component.arxml")
        text = arxml.read_text(encoding="utf-8")
        edited, count = re.subn(
            r'<CONTEXT-R-PORT-REF DEST="R-PORT-PROTOTYPE">',
            '<CONTEXT-R-PORT-REF DEST="ABSTRACT-REQUIRED-PORT-PROTOTYPE">',
            text,
            count=1,
        )
        assert count == 1
        arxml.write_text(edited, encoding="utf-8", newline="\n")
        target = init_project(tmp_path / "target") / "model"
        before = _tree(target)

        with pytest.raises(LossyComponentImportError) as refusal:
            import_component(str(arxml), str(target))

        assert "DATA-RECEIVED-EVENT" in str(refusal.value)
        assert "ABSTRACT-REQUIRED-PORT-PROTOTYPE" in str(refusal.value)
        assert _tree(target) == before


# The late dependency pass

_U8_BASE = {"name": "u8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"}
_U8 = {"name": "u8", "kind": "value_type", "base": "u8_base"}


def _sig(*elements: str) -> dict[str, Any]:
    return {
        "name": "ISig",
        "kind": "sender_receiver",
        "data_elements": [{"name": name, "implementation_type": "u8"} for name in elements],
    }


def _late_export(tmp_path: Path) -> str:
    """``Comp``'s export with ``ISig`` removed: runnable ``R`` started by element ``a`` through ``rSig``."""
    model_root = init_project(tmp_path / "late-source") / "model"
    for element in (_U8_BASE, _U8, _sig("a")):
        write_model_element(model_root, element)
    component = {
        "name": "Comp",
        "type": "application",
        "ports": [{"name": "rSig", "direction": "require", "interface": "ISig"}],
        "runnables": [{"name": "R", "events": [data_event("rSig", "a")]}],
        "handle_termination_and_restart": "no_support",
    }
    put(model_root, "swc/body/Comp.yaml", component)
    arxml = tmp_path / "Comp.arxml"
    export_component(str(model_root), "Comp", str(arxml))
    text = arxml.read_text(encoding="utf-8")
    text, count = re.subn(
        r"\s*<SENDER-RECEIVER-INTERFACE>\s*<SHORT-NAME>ISig</SHORT-NAME>.*?</SENDER-RECEIVER-INTERFACE>",
        "",
        text,
        count=1,
        flags=re.DOTALL,
    )
    assert count == 1
    text = re.sub(r"\s*<(REQUIRED|PROVIDED)-COM-SPECS>.*?</\1-COM-SPECS>", "", text, flags=re.DOTALL)
    return text


def _late_target(tmp_path: Path, *elements: str) -> Path:
    model_root = init_project(tmp_path / "late-target") / "model"
    for element in (_U8_BASE, _U8, _sig(*elements)):
        write_model_element(model_root, element)
    return model_root


def _save(tmp_path: Path, text: str) -> str:
    path = tmp_path / "late.arxml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


class TestTheLatePassPhrasesADataReceivedEvent:
    def test_a_data_element_the_targets_interface_lacks_is_a_data_received_event_verdict(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _late_export(tmp_path))
        target = _late_target(tmp_path, "other")

        planned = plan_component_import(source, str(target))

        assert list(planned.missing_references) == [
            MissingReference(
                referrer_layer="swc",
                referrer_name="Comp",
                referrer="runnable 'R' data-received event through port 'rSig'",
                missing_member="a",
                reference_kind="data_element",
                missing_layer="interfaces",
                missing_name="ISig",
            )
        ]

    def test_an_answered_element_is_no_verdict(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _late_export(tmp_path))
        planned = plan_component_import(source, str(_late_target(tmp_path, "a")))
        assert not planned.missing_references

    def test_a_disabled_mode_on_a_data_received_event_gets_its_verdict(self, tmp_path: Path) -> None:
        text = _late_export(tmp_path).replace(
            "</PORTS>",
            "<R-PORT-PROTOTYPE><SHORT-NAME>rMode</SHORT-NAME>"
            '<REQUIRED-INTERFACE-TREF DEST="MODE-SWITCH-INTERFACE">/Elsewhere/IMode</REQUIRED-INTERFACE-TREF>'
            "</R-PORT-PROTOTYPE></PORTS>",
            1,
        )
        name = "<SHORT-NAME>DRT_rSig_a</SHORT-NAME>"
        assert text.count(name) == 1
        text = text.replace(
            name,
            name + "<DISABLED-MODE-IREFS><DISABLED-MODE-IREF>"
            '<CONTEXT-PORT-REF DEST="R-PORT-PROTOTYPE">/ComponentTypes/Comp/rMode</CONTEXT-PORT-REF>'
            '<CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF DEST="MODE-DECLARATION-GROUP-PROTOTYPE">/Elsewhere/IMode/mg'
            "</CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF>"
            '<TARGET-MODE-DECLARATION-REF DEST="MODE-DECLARATION">/Elsewhere/Modes/OFF</TARGET-MODE-DECLARATION-REF>'
            "</DISABLED-MODE-IREF></DISABLED-MODE-IREFS>",
        )

        planned = plan_component_import(_save(tmp_path, text), str(_late_target(tmp_path, "a")))

        (verdict,) = planned.missing_references
        assert verdict.referrer == "runnable 'R' data-received event disabled mode through port 'rMode'"
        assert (verdict.reference_kind, verdict.missing_member, verdict.missing_is_stub) == ("mode_group", "mg", True)

    def test_a_late_admitted_event_s_uncarried_disabled_mode_refuses_the_import(self, tmp_path: Path) -> None:
        # The event is built only when the late pass admits it, so its disabled-mode line belongs in the
        # late pass's log: the mapper's own log was spent before the target was read.
        name = "<SHORT-NAME>DRT_rSig_a</SHORT-NAME>"
        text = _late_export(tmp_path)
        assert text.count(name) == 1
        text = text.replace(
            name,
            name + "<DISABLED-MODE-IREFS><DISABLED-MODE-IREF>"
            '<CONTEXT-PORT-REF DEST="R-PORT-PROTOTYPE">/ComponentTypes/Comp/rNoSuch</CONTEXT-PORT-REF>'
            '<CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF DEST="MODE-DECLARATION-GROUP-PROTOTYPE">/Elsewhere/IMode/mg'
            "</CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF>"
            '<TARGET-MODE-DECLARATION-REF DEST="MODE-DECLARATION">/Elsewhere/Modes/OFF</TARGET-MODE-DECLARATION-REF>'
            "</DISABLED-MODE-IREF></DISABLED-MODE-IREFS>",
        )
        source = _save(tmp_path, text)
        target = _late_target(tmp_path, "a")
        before = _tree(target)

        with pytest.raises(LossyComponentImportError) as refusal:
            import_component(source, str(target))

        assert "DISABLED-MODE-IREFS" in str(refusal.value)
        assert "names no port of this component" in str(refusal.value)
        assert _tree(target) == before


# The differ


def _events_change(old: list[Any], new: list[Any]) -> Any:
    """The ``events`` collection change of the one changed runnable, as ``swcraft diff`` computes it."""

    def state(events: list[Any]) -> ModelState:
        return ModelState(
            layers={"swc": [SWComponent(name=COMPONENT, runnables=[Runnable(name=FILTER, events=events)])]}
        )

    (swc,) = diff_model_states(state(old), state(new))["swc"].changed
    (runnables,) = [entry for entry in swc.collection_changes if entry.field == "runnables"]
    (runnable,) = runnables.changed
    (events,) = [entry for entry in runnable.collection_changes if entry.field == "events"]
    return events


#: As the differ dumps them: every field, ``disabled_modes`` and the stated ``name`` included.
LEFT = {"type": "data", "port": WHEEL_PORT, "element": FRONT_LEFT, "disabled_modes": [], "name": None}
RIGHT = {"type": "data", "port": WHEEL_PORT, "element": FRONT_RIGHT, "disabled_modes": [], "name": None}
TICK = TimingEvent(type="timing", period=0.01)


class TestTheDifferReportsADataReceivedEventAsAValue:
    """The differ reports a data-received event as a value, never as a ``changed`` item.

    ``Runnable.events`` is a multiset keyed by value: a data-received event is added, removed, or replaced as one
    removal plus one addition.
    """

    def test_an_added_event(self) -> None:
        events = _events_change([TICK], [TICK, DataEvent.model_validate(LEFT)])
        assert (events.added, events.removed, events.changed) == ([LEFT], [], [])

    def test_a_removed_event(self) -> None:
        events = _events_change([TICK, DataEvent.model_validate(LEFT)], [TICK])
        assert (events.added, events.removed, events.changed) == ([], [LEFT], [])

    def test_a_replaced_event_is_a_removal_plus_an_addition(self) -> None:
        events = _events_change([DataEvent.model_validate(LEFT)], [DataEvent.model_validate(RIGHT)])
        assert (events.added, events.removed, events.changed) == ([RIGHT], [LEFT], [])
