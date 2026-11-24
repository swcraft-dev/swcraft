"""Component exchange: an operation event and ``concurrent`` survive the round trip.

``swcraft generate arxml --component`` writes a runnable's ``OPERATION-INVOKED-EVENT``, its
``CAN-BE-INVOKED-CONCURRENTLY`` and the capability marker ``SD GID="OPERATION-EVENTS"`` in the behaviour's
``SWCRAFT`` group. ``swcraft import component`` reads the event back through ``map_swcs`` and decides by that marker
which regime governs an ``--update``:

* **marked**: the file is the truth for every runnable's operation events and ``concurrent``; they are compared, and
  ``--update`` replaces them, deleting and resetting what the file lacks;
* **carried**: no marker (an export that could carry no operation event and wrote ``false``). For a runnable the file
  is silent about, both are left out of the comparison and ``--update`` keeps the project's own, by runnable name; a
  runnable declaring an operation event or ``concurrent: true`` is compared and replaced. Timing and init events
  are compared under both regimes.

There is no flag like ``--replace-access-lists``, and a marker value the writer does not mint is refused. The
subjects are ``operation_event_support``'s two models, exported by the real exporter and edited as text where a test
needs an older or damaged file. Mixed event kinds are not judged and kept as written.

What this module does **not** reach: a file an older SWCraft wrote byte for byte (the *unmarked* file is this
release's export with the marker removed); real tool exports and the MCP surface's rendering of
:class:`ComponentImportReport`; whether an RTE generator accepts the mixed runnable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from operation_event_support import (
    BOUNDARY_OPERATION,
    BOUNDARY_PORT,
    INTERFACE,
    OPERATION,
    PORT,
    RUNNABLE,
    SERVER,
    SERVER_FILE,
    change,
    every_event_shape_model,
    mixed_event_model,
)
from swcraft_tools.arxml_import.registry import stub_name
from swcraft_tools.arxml_import.report import QUEUE_LENGTH_HEADING
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.capability_markers import OPERATION_EVENTS
from swcraft_tools.cli import main
from swcraft_tools.component_exchange import (
    ComponentImportConflictError,
    InvalidComponentArxmlError,
    LossyComponentImportError,
    ReservedNameError,
    export_component,
    import_component,
)
from swcraft_tools.component_exchange.capabilities import CARRIED, MARKED, NONE
from swcraft_tools.component_exchange.import_ import plan_component_import
from swcraft_tools.differ import diff_model_states
from swcraft_tools.model_state import ModelState
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import InitEvent, OperationEvent, Runnable, SWComponent, TimingEvent
from swcraft_tools.writer.arxml_generation import (
    generate_arxml,
)

MARKER_LINE = re.compile(r'\n *<SD GID="OPERATION-EVENTS">[^<]*</SD>')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_project(tmp_path: Path, name: str = "proj") -> Path:
    """A fresh project scaffolded by the real ``swcraft init`` CLI path; returns its model root."""
    target = tmp_path / name
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.argv", ["swcraft", "init", str(target)])
        main()
    return target / "model"


def _export(model_root: Path, arxml: Path) -> Path:
    export_component(str(model_root), SERVER, str(arxml))
    return arxml


def _without_operation_events(model_root: Path) -> None:
    """Drop every operation event and every ``concurrent`` key from the component, in place."""

    def strip(document: dict[str, Any]) -> None:
        for runnable in document["runnables"]:
            runnable["events"] = [event for event in runnable["events"] if event["type"] != "operation"]
            runnable.pop("concurrent", None)

    change(model_root, SERVER_FILE, strip)


def _unmark(arxml: Path) -> Path:
    """Remove the operation-events marker, which an older export lacks."""
    text = arxml.read_text(encoding="utf-8")
    assert len(MARKER_LINE.findall(text)) == 1
    arxml.write_text(MARKER_LINE.sub("", text), encoding="utf-8", newline="\n")
    assert OPERATION_EVENTS.sd_gid not in arxml.read_text(encoding="utf-8")
    return arxml


def _replace_text(arxml: Path, old: str, new: str, count: int | None = 1) -> Path:
    text = arxml.read_text(encoding="utf-8")
    if count is not None:
        assert text.count(old) == count, (old, text.count(old))
    arxml.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return arxml


def _set_period(model_root: Path, runnable: str, period: float) -> None:
    def edit(document: dict[str, Any]) -> None:
        (entry,) = [r for r in document["runnables"] if r["name"] == runnable]
        for event in entry["events"]:
            if event["type"] == "timing":
                event["period"] = period

    change(model_root, SERVER_FILE, edit)


def _runnables(model_root: Path) -> dict[str, Runnable]:
    (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == SERVER]
    return {runnable.name: runnable for runnable in swc.runnables}


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _operation_events(runnable: Runnable) -> list[tuple[str, str]]:
    return [(event.port, event.operation) for event in runnable.events if isinstance(event, OperationEvent)]


@pytest.fixture
def shapes(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The every-shape model, its marked export, and a project holding the component imported from it.

    Returns ``(source model root, marked export, target model root)``.
    """
    source = every_event_shape_model(tmp_path / "source")
    arxml = _export(source, tmp_path / "marked.arxml")
    target = _init_project(tmp_path)
    assert import_component(str(arxml), str(target)).swc_action == "created"
    return source, arxml, target


def _older_export(tmp_path: Path, source: Path, *, edit=None, name: str = "older") -> Path:
    """An unmarked export of *source* with its operation events and ``concurrent`` removed.

    *edit*, when given, changes the copy further before the export.
    """
    copy = tmp_path / f"{name}-model"
    copy.mkdir()
    for path in sorted(source.rglob("*"), key=lambda path: path.as_posix()):
        if path.is_file():
            destination = copy / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
    _without_operation_events(copy)
    if edit is not None:
        edit(copy)
    return _unmark(_export(copy, tmp_path / f"{name}.arxml"))


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------


class TestTheExportCarriesTheOperationEventsMarker:
    def test_every_component_export_carries_it_and_the_whole_model_does_not(self, tmp_path: Path) -> None:
        source = mixed_event_model(tmp_path / "source")
        component = _export(source, tmp_path / "component.arxml").read_text(encoding="utf-8")
        assert f'<SD GID="{OPERATION_EVENTS.sd_gid}">{OPERATION_EVENTS.text}</SD>' in component
        whole = tmp_path / "whole.arxml"
        generate_arxml(str(source), str(whole))
        assert OPERATION_EVENTS.sd_gid not in whole.read_text(encoding="utf-8")

    def test_a_component_with_no_operation_event_carries_it_too(self, tmp_path: Path) -> None:
        """A marked file with no operation event says *none*, which is what lets ``--update`` delete them.

        A statement about the exporter, never about the component."""
        source = mixed_event_model(tmp_path / "source")
        _without_operation_events(source)
        text = _export(source, tmp_path / "component.arxml").read_text(encoding="utf-8")
        assert "OPERATION-INVOKED-EVENT" not in text
        assert f'<SD GID="{OPERATION_EVENTS.sd_gid}">' in text

    def test_the_reader_admits_the_group_and_reads_the_marker(self, tmp_path: Path) -> None:
        source = mixed_event_model(tmp_path / "source")
        model = read_arxml(str(_export(source, tmp_path / "component.arxml")))
        assert model.swcs[0].swcraft_markers[OPERATION_EVENTS.sd_gid] == OPERATION_EVENTS.text
        assert not [entry for entry in model.skip_log if "ADMIN-DATA" in entry.reason]

    def test_the_marker_is_a_capability_token_never_a_version(self) -> None:
        """One copy in the leaf ``capability_markers``, which the writer mints from and the reader admits."""
        assert OPERATION_EVENTS.sd_gid == "OPERATION-EVENTS"
        assert not re.search(r"\d", OPERATION_EVENTS.text), "a capability, never a version"

    def test_a_value_the_writer_does_not_mint_is_refused_with_nothing_written(self, shapes) -> None:
        _source, arxml, target = shapes
        _replace_text(arxml, f">{OPERATION_EVENTS.text}<", ">something else<")
        before = _tree(target)
        with pytest.raises(InvalidComponentArxmlError) as refusal:
            import_component(str(arxml), str(target), update=True)
        assert "OPERATION-EVENTS marker with the value 'something else'" in str(refusal.value)
        assert _tree(target) == before


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


class TestAnOperationEventSurvivesTheRoundTrip:
    def test_the_every_shape_component_exports_and_reimports_unchanged(self, shapes) -> None:
        source, arxml, target = shapes
        assert _runnables(target) == _runnables(source)
        assert _runnables(target)["Serve"].concurrent is True
        before = _tree(target)
        report = import_component(str(arxml), str(target))
        assert report.swc_action == "unchanged"
        assert report.operation_event_regime == NONE
        assert _tree(target) == before

    def test_the_mixed_event_fixture_keeps_both_events(self, tmp_path: Path) -> None:
        source = mixed_event_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "mixed.arxml")
        target = _init_project(tmp_path)
        assert import_component(str(arxml), str(target)).swc_action == "created"
        (runnable,) = _runnables(target).values()
        assert runnable.name == RUNNABLE
        assert [type(event) for event in runnable.events] == [TimingEvent, OperationEvent]
        assert _operation_events(runnable) == [(PORT, OPERATION)]
        assert import_component(str(arxml), str(target)).swc_action == "unchanged"

    def test_the_import_goes_through_the_cli(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The import through the CLI no longer fails with ``Error: 'operation'``."""
        source = every_event_shape_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "marked.arxml")
        _init_project(tmp_path)
        capsys.readouterr()
        manifest = tmp_path / "proj" / "swcraft.yaml"
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "import", "component", "--source", str(arxml), "--project", str(manifest)]
        )
        main()
        output = capsys.readouterr()
        assert f"SWC {SERVER}: created" in output.out
        assert "Error" not in output.err
        assert _runnables(tmp_path / "proj" / "model") == _runnables(source)


# ---------------------------------------------------------------------------
# The two regimes
# ---------------------------------------------------------------------------


class TestAnUnmarkedFileCarriesTheProjectsOperationEvents:
    def test_an_otherwise_identical_unmarked_file_is_unchanged(self, shapes, tmp_path: Path) -> None:
        source, _arxml, target = shapes
        older = _older_export(tmp_path, source)
        before = _tree(target)
        report = import_component(str(older), str(target))
        assert report.swc_action == "unchanged"
        assert _tree(target) == before

    def test_a_timing_change_is_still_compared_and_the_operation_events_are_carried(
        self, shapes, tmp_path: Path
    ) -> None:
        source, _arxml, target = shapes
        older = _older_export(tmp_path, source, edit=lambda copy: _set_period(copy, RUNNABLE, 0.02))
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(older), str(target))
        assert "operation events and a runnable's concurrency are not compared either" in str(conflict.value)

        report = import_component(str(older), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.operation_event_regime == CARRIED
        runnables = _runnables(target)
        assert runnables[RUNNABLE].events == [
            TimingEvent(type="timing", period=0.02),
            OperationEvent(type="operation", port=PORT, operation=OPERATION),
        ], "the carried event keeps its place beside the compared timing event"
        assert runnables == {
            name: runnable if name != RUNNABLE else runnables[RUNNABLE] for name, runnable in _runnables(source).items()
        }
        assert runnables["Serve"].concurrent is True
        assert report.preserved_operation_events == {
            RUNNABLE: {"operation_events": 1},
            "Serve": {"operation_events": 2, "concurrent": True},
            "CollideA": {"operation_events": 1},
            "CollideB": {"operation_events": 1},
            "Boundary": {"operation_events": 1},
        }
        rendered = report.render()
        assert "OPERATION EVENTS regime: carried -- the export carries no SWCraft operation-events marker" in rendered
        assert "  - Serve: operation_events=2, concurrent=true" in rendered

    def test_an_init_event_is_still_compared(self, shapes, tmp_path: Path) -> None:
        source, _arxml, target = shapes

        def timing_instead_of_init(copy: Path) -> None:
            def edit(document: dict[str, Any]) -> None:
                (entry,) = [r for r in document["runnables"] if r["name"] == "Initialize"]
                entry["events"] = [{"type": "timing", "period": 0.1}]

            change(copy, SERVER_FILE, edit)

        older = _older_export(tmp_path, source, edit=timing_instead_of_init)
        with pytest.raises(ComponentImportConflictError):
            import_component(str(older), str(target))
        import_component(str(older), str(target), update=True)
        assert _runnables(target)["Initialize"].events == [TimingEvent(type="timing", period=0.1)]

    @pytest.mark.parametrize("spelling", [f"OIT_{PORT}_{OPERATION}", f"OIT_{PORT}_{OPERATION}".lower()])
    def test_a_runnable_named_like_a_carried_event_is_refused_with_nothing_written(
        self, spelling: str, shapes, tmp_path: Path
    ) -> None:
        """The unmarked file states no operation event, so the incoming component collides with nothing.

        The carry puts the target's ``OIT_pRecords_ReadRecord`` back beside the added runnable of that name, and
        ``rule:name.runnable-event-collision`` is asked again."""
        source, _arxml, target = shapes

        def add_runnable(copy: Path) -> None:
            change(copy, SERVER_FILE, lambda document: document["runnables"].append({"name": spelling}))

        older = _older_export(tmp_path, source, edit=add_runnable)
        before = _tree(target)

        with pytest.raises(ReservedNameError) as caught:
            import_component(str(older), str(target), update=True)

        assert f"runnable '{spelling}'" in str(caught.value)
        assert f"OPERATION-INVOKED-EVENT 'OIT_{PORT}_{OPERATION}'" in str(caught.value)
        assert _tree(target) == before

    def test_an_unmarked_runnable_that_speaks_is_the_truth_for_itself(self, shapes, tmp_path: Path) -> None:
        """An unmarked runnable carrying an operation event or ``true`` is not its silence.

        An older writer could write either, so its own declarations replace the target's, and only the silent runnables
        are carried."""
        _source, _arxml, target = shapes
        copy = every_event_shape_model(tmp_path / "speaking")

        def rebind(document: dict[str, Any]) -> None:
            (serve,) = [r for r in document["runnables"] if r["name"] == "Serve"]
            serve["events"] = [{"type": "operation", "port": PORT, "operation": OPERATION}]
            (tick,) = [r for r in document["runnables"] if r["name"] == RUNNABLE]
            tick["events"] = [{"type": "timing", "period": 0.02}]  # a compared change, so the update has work
            (boundary,) = [r for r in document["runnables"] if r["name"] == "Boundary"]
            boundary["events"] = []

        change(copy, SERVER_FILE, rebind)
        unmarked = _unmark(_export(copy, tmp_path / "speaking.arxml"))

        report = import_component(str(unmarked), str(target), update=True)

        assert report.swc_action == "updated"
        runnables = _runnables(target)
        assert runnables[RUNNABLE].events[0] == TimingEvent(type="timing", period=0.02)
        assert _operation_events(runnables["Serve"]) == [(PORT, OPERATION)], "the speaking runnable's own"
        assert runnables["Serve"].concurrent is True
        assert _operation_events(runnables[RUNNABLE]) == [(PORT, OPERATION)], "silent: carried"
        assert _operation_events(runnables["Boundary"]) == [(BOUNDARY_PORT, BOUNDARY_OPERATION)], "silent: carried"
        assert "Serve" not in report.preserved_operation_events
        assert set(report.preserved_operation_events) == {RUNNABLE, "Boundary"}

    def test_a_speaking_runnable_is_compared_so_its_only_difference_is_not_unchanged(self, tmp_path: Path) -> None:
        """A speaking runnable is compared.

        An unmarked file whose only difference is its own operation events and ``concurrent`` is a conflict, not
        ``unchanged``, and ``--update`` takes the file's."""
        source = every_event_shape_model(tmp_path / "source")
        silent = _older_export(tmp_path, source)
        target = _init_project(tmp_path)
        assert import_component(str(silent), str(target)).swc_action == "created"
        assert not any(_operation_events(runnable) for runnable in _runnables(target).values())
        speaking = _unmark(_export(source, tmp_path / "speaking.arxml"))

        before = _tree(target)
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(speaking), str(target))
        assert "except on a runnable that declares an operation event" in str(conflict.value)
        assert _tree(target) == before

        report = import_component(str(speaking), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.operation_event_regime == CARRIED
        assert report.preserved_operation_events == {}
        assert _runnables(target) == _runnables(source)
        assert _runnables(target)["Serve"].concurrent is True
        assert import_component(str(speaking), str(target)).swc_action == "unchanged"

    def test_a_runnable_that_speaks_only_through_concurrent_is_compared(self, tmp_path: Path) -> None:
        source = mixed_event_model(tmp_path / "source")
        _without_operation_events(source)
        target = _init_project(tmp_path)
        assert import_component(str(_export(source, tmp_path / "plain.arxml")), str(target)).swc_action == "created"

        def concurrent(document: dict[str, Any]) -> None:
            (entry,) = document["runnables"]
            entry["concurrent"] = True

        change(source, SERVER_FILE, concurrent)
        unmarked = _unmark(_export(source, tmp_path / "concurrent.arxml"))
        with pytest.raises(ComponentImportConflictError):
            import_component(str(unmarked), str(target))
        report = import_component(str(unmarked), str(target), update=True)
        assert report.swc_action == "updated"
        assert report.preserved_operation_events == {}
        (runnable,) = _runnables(target).values()
        assert runnable.concurrent is True

    def test_a_conflict_about_neither_side_speaking_carries_no_clause(self, tmp_path: Path) -> None:
        source = mixed_event_model(tmp_path / "source")
        _without_operation_events(source)
        target = _init_project(tmp_path)
        assert import_component(str(_export(source, tmp_path / "plain.arxml")), str(target)).swc_action == "created"
        _set_period(source, RUNNABLE, 0.02)
        unmarked = _unmark(_export(source, tmp_path / "period.arxml"))
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(unmarked), str(target))
        assert "operation events" not in str(conflict.value)

    @pytest.mark.parametrize(
        ("edit_events", "expected"),
        [
            pytest.param(
                lambda events: [*events, {"type": "init"}],
                [
                    TimingEvent(type="timing", period=0.01),
                    OperationEvent(type="operation", port=PORT, operation=OPERATION),
                    InitEvent(type="init"),
                ],
                id="the export adds an event: it follows",
            ),
            pytest.param(
                lambda events: [],
                [OperationEvent(type="operation", port=PORT, operation=OPERATION)],
                id="the export drops the timing event: the carried event stays alone",
            ),
        ],
    )
    def test_the_carried_events_keep_their_places_when_the_lists_differ_in_length(
        self, shapes, tmp_path: Path, edit_events, expected
    ) -> None:
        source, _arxml, target = shapes
        assert [type(event) for event in _runnables(target)[RUNNABLE].events] == [TimingEvent, OperationEvent]

        def edit(copy: Path) -> None:
            def change_events(document: dict[str, Any]) -> None:
                (entry,) = [r for r in document["runnables"] if r["name"] == RUNNABLE]
                entry["events"] = edit_events(entry["events"])

            change(copy, SERVER_FILE, change_events)

        older = _older_export(tmp_path, source, edit=edit)
        report = import_component(str(older), str(target), update=True)
        assert report.swc_action == "updated"
        assert _runnables(target)[RUNNABLE].events == expected
        assert report.preserved_operation_events[RUNNABLE] == {"operation_events": 1}

    def test_a_removed_runnable_is_removed_with_its_events(self, shapes, tmp_path: Path) -> None:
        source, _arxml, target = shapes

        def drop_serve(copy: Path) -> None:
            change(
                copy,
                SERVER_FILE,
                lambda document: document.update(runnables=[r for r in document["runnables"] if r["name"] != "Serve"]),
            )

        older = _older_export(tmp_path, source, edit=drop_serve)
        report = import_component(str(older), str(target), update=True)
        assert "Serve" not in _runnables(target)
        assert "Serve" not in report.preserved_operation_events
        assert "Serve" in report.swc_diff_preview


class TestAMarkedFileIsTheTruth:
    def test_a_marked_file_without_operation_events_deletes_and_resets_them(self, shapes, tmp_path: Path) -> None:
        source, _arxml, target = shapes
        copy = every_event_shape_model(tmp_path / "copy")
        _without_operation_events(copy)
        marked = _export(copy, tmp_path / "marked-none.arxml")
        assert f'<SD GID="{OPERATION_EVENTS.sd_gid}">' in marked.read_text(encoding="utf-8")

        before = _tree(target)
        with pytest.raises(ComponentImportConflictError) as conflict:
            import_component(str(marked), str(target))
        assert "not compared either" not in str(conflict.value)
        assert _tree(target) == before

        report = import_component(str(marked), str(target), update=True)

        assert report.operation_event_regime == MARKED
        assert report.preserved_operation_events == {}
        runnables = _runnables(target)
        assert not any(_operation_events(runnable) for runnable in runnables.values())
        assert not any(runnable.concurrent for runnable in runnables.values())
        assert runnables[RUNNABLE].events == [TimingEvent(type="timing", period=0.01)]
        assert "operation" in report.swc_diff_preview
        assert "OPERATION EVENTS regime: marked" in report.render()

    def test_a_marked_file_adds_an_operation_event_the_target_lacks(self, tmp_path: Path) -> None:
        source = every_event_shape_model(tmp_path / "source")
        older = _older_export(tmp_path, source)
        target = _init_project(tmp_path)
        assert import_component(str(older), str(target)).swc_action == "created"
        assert not any(_operation_events(runnable) for runnable in _runnables(target).values())

        marked = _export(source, tmp_path / "marked.arxml")
        with pytest.raises(ComponentImportConflictError):
            import_component(str(marked), str(target))
        import_component(str(marked), str(target), update=True)
        assert _runnables(target) == _runnables(source)


# ---------------------------------------------------------------------------
# Refusals and report lines
# ---------------------------------------------------------------------------


class TestWhatAnImportCannotCarry:
    def test_an_event_through_a_port_on_an_undeclared_interface_is_a_verdict_naming_it(self, tmp_path: Path) -> None:
        """No declared interface answers the port, so the event is a missing reference named by the port's stub.

        The server com-spec's line (its member is not one segment under the stub's path) is descriptive:
        the component is created with its port on the stub and its behaviour withheld, and the line is
        printed under *Not carried*."""
        source = mixed_event_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "stub.arxml")
        _replace_text(
            arxml,
            f'<PROVIDED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/PortInterfaces/{INTERFACE}<',
            '<PROVIDED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/Foreign/IRemote<',
        )
        target = _init_project(tmp_path)
        planned = plan_component_import(str(arxml), str(target))
        report = import_component(str(arxml), str(target))
        (verdict,) = planned.missing_references
        assert (verdict.reference_kind, verdict.missing_member, verdict.missing_name) == (
            "operation",
            OPERATION,
            stub_name("IRemote", 0, set())[0],
        )
        assert f"through port '{PORT}'" in verdict.referrer
        assert report.missing_references == [verdict.line()]
        assert any(
            line.startswith(f"SERVER-COM-SPEC /ComponentTypes/ServerSwc/{PORT}/PROVIDED-COM-SPECS")
            and "names no member the stub lists" in line
            for line in report.not_carried_lines
        )
        written = read_swc_model(str(target))
        assert [swc.runnables for swc in written] == [[]]

    def test_an_unreadable_concurrency_aborts_with_nothing_written(self, tmp_path: Path) -> None:
        """A concurrency value that is not an xsd:boolean stops a component import on the loss.

        No SWCraft export writes such a value; one that does is not carried, and the import stops rather than writing
        ``false``."""
        source = mixed_event_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "unreadable.arxml")
        _replace_text(
            arxml,
            "<CAN-BE-INVOKED-CONCURRENTLY>false</CAN-BE-INVOKED-CONCURRENTLY>",
            "<CAN-BE-INVOKED-CONCURRENTLY>maybe</CAN-BE-INVOKED-CONCURRENTLY>",
        )
        target = _init_project(tmp_path)
        before = _tree(target)
        with pytest.raises(LossyComponentImportError) as refusal:
            import_component(str(arxml), str(target))
        (line,) = [line for line in refusal.value.skip_lines if "CAN-BE-INVOKED-CONCURRENTLY" in line]
        assert "unreadable value 'maybe'" in line
        assert _tree(target) == before

    def test_an_over_budget_operation_event_name_is_refused_as_a_pair(self, tmp_path: Path) -> None:
        source = every_event_shape_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "long.arxml")
        _replace_text(arxml, BOUNDARY_PORT, f"{BOUNDARY_PORT}X", count=None)
        target = _init_project(tmp_path)
        before = _tree(target)
        with pytest.raises(ReservedNameError) as refusal:
            import_component(str(arxml), str(target))
        (line,) = [line for line in refusal.value.violations if "operation event" in line]
        assert f"operation event '{BOUNDARY_PORT}X'.'{BOUNDARY_OPERATION}'" in line
        assert "(129 characters;" in line
        assert "get 123 characters between them" in line
        assert f"so shorten '{BOUNDARY_OPERATION}' to at most 61 characters" in line
        assert _tree(target) == before

    def test_an_illegal_declared_port_name_is_not_charged_to_the_oit_form(self, tmp_path: Path) -> None:
        """One bad declared name is one root cause.

        A 130-character port is refused as a port name, and the ``OIT_`` form it would also make illegal is not a second
        line."""
        source = mixed_event_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "illegal.arxml")
        long_port = PORT + "P" * (130 - len(PORT))
        _replace_text(arxml, f">{PORT}<", f">{long_port}<", count=None)
        _replace_text(arxml, f"/{PORT}<", f"/{long_port}<", count=None)
        _replace_text(arxml, f"/{PORT}/", f"/{long_port}/", count=None)
        target = _init_project(tmp_path)
        before = _tree(target)
        with pytest.raises(ReservedNameError) as refusal:
            import_component(str(arxml), str(target))
        (port_line,) = [line for line in refusal.value.violations if "port name" in line]
        assert f"'{long_port}' is not a legal AUTOSAR Identifier" in port_line
        assert not [line for line in refusal.value.violations if "operation event" in line]
        assert _tree(target) == before

    def test_the_boundary_name_and_the_colliding_pairs_import(self, shapes) -> None:
        """The guard is not stricter than the writer.

        A 128-character ``OIT_`` name and two pairs the retroactive counter suffixes are what the every-shape export
        carries."""
        source, _arxml, target = shapes
        assert _runnables(target)["Boundary"] == _runnables(source)["Boundary"]

    def test_a_queue_length_other_than_one_is_a_report_line(self, tmp_path: Path) -> None:
        source = mixed_event_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "queue.arxml")
        _replace_text(arxml, "<QUEUE-LENGTH>1</QUEUE-LENGTH>", "<QUEUE-LENGTH>2</QUEUE-LENGTH>")
        target = _init_project(tmp_path)
        report = import_component(str(arxml), str(target))
        assert report.swc_action == "created"
        (line,) = report.queue_length_lines
        assert "QUEUE-LENGTH 2" in line
        assert f"{QUEUE_LENGTH_HEADING} (1; " in report.render()
        assert not report.skip_lines or all("QUEUE" not in skip for skip in report.skip_lines)

    @pytest.mark.parametrize("length", ["+1", "-1"])
    def test_a_signed_queue_length_is_no_length_and_a_report_line(self, tmp_path: Path, length: str) -> None:
        """A signed queue length is named as written rather than read as 1 and dropped without a line.

        ``SERVER-COM-SPEC/QUEUE-LENGTH`` is a ``POSITIVE-INTEGER`` (XSD R4.3.0:86393; R4.2.2:75075), which admits no
        sign: ``+1`` is no spelling of the writer's 1."""
        source = mixed_event_model(tmp_path / "source")
        arxml = _export(source, tmp_path / "queue.arxml")
        _replace_text(arxml, "<QUEUE-LENGTH>1</QUEUE-LENGTH>", f"<QUEUE-LENGTH>{length}</QUEUE-LENGTH>")
        target = _init_project(tmp_path)
        report = import_component(str(arxml), str(target))
        (line,) = report.queue_length_lines
        assert f"QUEUE-LENGTH {length}" in line

    def test_a_queue_length_of_one_is_no_line(self, shapes) -> None:
        _source, arxml, target = shapes
        report = import_component(str(arxml), str(target))
        assert report.queue_length_lines == []
        assert QUEUE_LENGTH_HEADING not in report.render()


# ---------------------------------------------------------------------------
# The differ
# ---------------------------------------------------------------------------


def _state(runnable: Runnable) -> ModelState:
    return ModelState(layers={"swc": [SWComponent(name=SERVER, runnables=[runnable])]})


def _runnable_change(old: Runnable, new: Runnable):
    """The one changed runnable of the one changed component, as ``swcraft diff`` computes it."""
    (swc,) = diff_model_states(_state(old), _state(new))["swc"].changed
    (runnables,) = [entry for entry in swc.collection_changes if entry.field == "runnables"]
    (runnable,) = runnables.changed
    return runnable


def _events_change(old: list[Any], new: list[Any]):
    runnable = _runnable_change(Runnable(name=RUNNABLE, events=old), Runnable(name=RUNNABLE, events=new))
    (events,) = [entry for entry in runnable.collection_changes if entry.field == "events"]
    return events


#: As the differ dumps them: every field, the event's `name` at `None` included.
READ = {"type": "operation", "port": PORT, "operation": OPERATION, "name": None}
OTHER = {"type": "operation", "port": PORT, "operation": "ReadOther", "name": None}
TICK = {"type": "timing", "period": 0.01}


class TestTheDifferReportsAnOperationEventAsAValue:
    """``Runnable.events`` is a multiset keyed by value.

    An operation event is added, removed, or replaced as one removal plus one addition, never a ``changed`` item.
    Compared through ``diff_model_states``; the rendering is ``test_differ.py``'s."""

    def test_an_added_operation_event(self) -> None:
        events = _events_change([TICK], [TICK, READ])
        assert (events.added, events.removed, events.changed) == ([READ], [], [])

    def test_a_removed_operation_event(self) -> None:
        events = _events_change([TICK, READ], [TICK])
        assert (events.added, events.removed, events.changed) == ([], [READ], [])

    def test_a_replaced_operation_event_is_a_removal_plus_an_addition(self) -> None:
        events = _events_change([READ], [OTHER])
        assert (events.added, events.removed, events.changed) == ([OTHER], [READ], [])

    def test_concurrent_is_a_changed_field(self) -> None:
        runnable = _runnable_change(
            Runnable(name=RUNNABLE, events=[InitEvent(type="init")]),
            Runnable(name=RUNNABLE, events=[InitEvent(type="init")], concurrent=True),
        )
        assert [(name, old, new) for name, old, new in runnable.changed_fields] == [("concurrent", False, True)]
