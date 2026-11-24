"""The importer: an ``OPERATION-INVOKED-EVENT`` is bound, or skipped with a reason.

``swcraft import model`` binds an operation-invoked event by exact path (the ``CONTEXT-P-PORT-REF`` to
one of the component's own provide ports, the ``TARGET-PROVIDED-OPERATION-REF`` to an operation of that
port's client/server interface), reads the runnable's ``CAN-BE-INVOKED-CONCURRENTLY`` into
``concurrent``, and reports a source ``SERVER-COM-SPEC`` queue length the export cannot carry.

The inputs are synthetic:

* ``fixtures/operation_events/operation_events.arxml``, written by hand with invented names: every
  shape the mapper binds and every shape it skips, the external stub seam (a stub minted and written,
  one sharing a declared leaf, one left unminted by two ``DEST``s) and a queue length of 2;
* the writer's own output for ``operation_event_support``'s two models, the mixed-event copy and the
  every-shape copy, generated in the test and imported back.

What this module does **not** cover:

* ``import component`` (``test_operation_event_exchange.py``), which maps through the same
  ``map_swcs`` but passes no stub policy, so a stub-port event there is a non-deferred skip;
* real tool exports (the closed acceptance oracle);
* the import merge's signature over the new reader values (``test_arxml_import_merges.py``).

**Mixed event kinds are not judged.** The RTE specification describes mixing as unsupported in
explanatory text, with no tagged requirement. The mixed shape is imported as written, and nothing in
this module labels it RTE-conformant.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from operation_event_support import every_event_shape_model, mixed_event_model
from swcraft_tools.arxml_import import map_data_types, map_interfaces, map_swcs, run_import
from swcraft_tools.arxml_import.report import QUEUE_LENGTH_HEADING, ImportReport
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.cli import main
from swcraft_tools.project import require_project
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.schema import ExternalInterface, InitEvent, OperationEvent, TimingEvent
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import generate_arxml

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "operation_events" / "operation_events.arxml"
EVENT_TAG = "OPERATION-INVOKED-EVENT"
BEHAVIOR = "/Acme/Components/ServerSwc/Behavior"


@pytest.fixture
def import_into_fresh_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Import a source into a fresh project scaffolded by the real ``swcraft init`` CLI path.

    Returns a function of *source* and ``run_import``'s keyword options that answers the new project's
    model root and the report.
    """

    def run(source: Path, **options) -> tuple[Path, ImportReport]:
        project = tmp_path / "proj"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(project)])
        main()
        report = run_import(str(source), str(project / "swcraft.yaml"), **options)
        return Path(require_project(str(project / "swcraft.yaml")).resolved_model_root), report

    return run


def _swcs(model_root: Path) -> dict:
    return {swc.name: swc for swc in read_swc_model(str(model_root))}


def _runnables(model_root: Path, swc: str) -> dict:
    return {runnable.name: runnable for runnable in _swcs(model_root)[swc].runnables}


def _reason(report: ImportReport, event: str) -> str:
    (reason,) = [entry.reason for entry in report.skip_entries if entry.path == f"{BEHAVIOR}/{event}"]
    return reason


def _event_skips(report: ImportReport) -> list[str]:
    return [entry.path for entry in report.skip_entries if entry.tag == EVENT_TAG]


@pytest.fixture
def imported(import_into_fresh_project) -> tuple[Path, ImportReport]:
    return import_into_fresh_project(FIXTURE)


class TestAnEventIsBound:
    """The event names the component's own port and that port's operation."""

    def test_two_operations_start_one_runnable_in_source_order(self, imported) -> None:
        model_root, _ = imported

        # Each keeps the fixture's own name, which is not the derived `OIT_{port}_{operation}`.
        assert _runnables(model_root, "ServerSwc")["ServeRecords"].events == [
            OperationEvent(type="operation", port="pRecords", operation="ReadRecord", name="E_ReadRecord"),
            OperationEvent(type="operation", port="pRecords", operation="ReadOther", name="E_ReadOther"),
        ]

    def test_an_operation_event_beside_a_timing_event_is_imported_with_it(self, imported) -> None:
        """The mixed-event shape: both events kept, no skip, not judged."""
        model_root, report = imported

        assert _runnables(model_root, "ServerSwc")["ServeAndTick"].events == [
            TimingEvent(type="timing", period=0.01, name="E_Tick"),
            OperationEvent(type="operation", port="pRecords", operation="Reset", name="E_Reset"),
        ]
        assert not [path for path in _event_skips(report) if path.endswith(("/E_Tick", "/E_Reset"))]

    def test_no_bound_event_carries_a_skip_entry(self, imported) -> None:
        _, report = imported

        bound = {f"{BEHAVIOR}/{name}" for name in ("E_ReadRecord", "E_ReadOther", "E_Reset", "E_Tick")}
        # A bound event is imported under the fixture's own name, not reported.
        assert not bound & {entry.path for entry in report.skip_entries if entry.effect == "abort"}

    def test_every_source_event_is_imported_or_skipped(self, imported) -> None:
        """Fifteen operation-invoked events in the fixture: five imported and ten skipped, each with a reason.

        The two events through a port whose stub is minted name an operation the stub lists, and are imported."""
        model_root, report = imported

        imported_events = [
            event
            for runnable in _runnables(model_root, "ServerSwc").values()
            for event in runnable.events
            if isinstance(event, OperationEvent)
        ]
        skips = _event_skips(report)
        assert (len(imported_events), len(skips)) == (5, 10)
        assert all(entry.reason for entry in report.skip_entries if entry.tag == EVENT_TAG)

    def test_the_runnable_whose_other_events_are_skipped_keeps_the_two_through_minted_stubs(self, imported) -> None:
        """Every other event of ``ServeRejected`` is skipped; the two through minted stubs are kept."""
        model_root, _ = imported

        assert _runnables(model_root, "ServerSwc")["ServeRejected"].events == [
            OperationEvent(type="operation", port="pRemote", operation="Serve", name="E_Remote"),
            OperationEvent(type="operation", port="pClash", operation="ReadRecord", name="E_Clash"),
        ]

    def test_no_event_is_imported_as_an_init_event(self, imported) -> None:
        model_root, _ = imported

        assert not [
            event
            for runnable in _runnables(model_root, "ServerSwc").values()
            for event in runnable.events
            if isinstance(event, InitEvent)
        ]


class TestConcurrencyIsRead:
    """``CAN-BE-INVOKED-CONCURRENTLY`` reaches ``concurrent`` and the file."""

    def test_true_false_and_absent(self, imported) -> None:
        model_root, _ = imported

        runnables = _runnables(model_root, "ServerSwc")
        assert (
            runnables["ServeRecords"].concurrent,
            runnables["ServeAndTick"].concurrent,
            runnables["ServeRejected"].concurrent,
        ) == (True, False, False)

    def test_the_written_file_keeps_concurrent_true_and_omits_the_default(self, imported) -> None:
        model_root, _ = imported

        document = yaml.safe_load((model_root / "swc" / "Components" / "ServerSwc.yaml").read_text(encoding="utf-8"))
        assert {runnable["name"]: runnable.get("concurrent") for runnable in document["runnables"]} == {
            "ServeRecords": True,
            "ServeAndTick": None,
            "ServeRejected": None,
        }

    @pytest.mark.parametrize(
        ("spelled", "expected"),
        [
            pytest.param("yes", "yes", id="unreadable-is-a-skip-entry"),
            pytest.param(" 1 ", None, id="an-xsd-boolean-spelling-is-read"),
        ],
    )
    def test_an_unreadable_value_is_reported_not_silently_false(
        self, tmp_path: Path, spelled: str, expected: str | None
    ) -> None:
        """A present ``CAN-BE-INVOKED-CONCURRENTLY`` that is not an xsd:boolean is a skip entry.

        The value is written as ``false``, so the loss is a skip entry naming the runnable and the value, never a silent
        default."""
        old = "<CAN-BE-INVOKED-CONCURRENTLY>true</CAN-BE-INVOKED-CONCURRENTLY>"
        text = FIXTURE.read_text(encoding="utf-8")
        assert text.count(old) == 1
        source = tmp_path / "edited.arxml"
        source.write_text(
            text.replace(old, f"<CAN-BE-INVOKED-CONCURRENTLY>{spelled}</CAN-BE-INVOKED-CONCURRENTLY>"),
            encoding="utf-8",
            newline="\n",
        )
        model = read_arxml(str(source))
        forms: list = []
        data_types, registry, _ = map_data_types(model, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)

        swcs, skip_log = map_swcs(model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})

        (server,) = [imported for imported in swcs if imported.swc.name == "ServerSwc"]
        (runnable,) = [r for r in server.swc.runnables if r.name == "ServeRecords"]
        entries = [entry for entry in skip_log if entry.tag == "CAN-BE-INVOKED-CONCURRENTLY"]
        if expected is None:
            assert runnable.concurrent is True
            assert entries == []
        else:
            assert runnable.concurrent is False
            (entry,) = entries
            assert entry.path.endswith("/ServeRecords")
            assert f"unreadable value {expected!r}" in entry.reason
            assert "runnable 'ServeRecords' is written with concurrent: false" in entry.reason


class TestAnUnbindableEventIsSkippedWithAReason:
    """Each shape the mapper cannot bind, with the reason its line carries."""

    @pytest.mark.parametrize(
        ("event", "reason"),
        [
            pytest.param(
                "E_Signal",
                "operation event through port 'pSignals' whose interface 'ISignals' is sender/receiver "
                "(an operation event needs a client/server interface)",
                id="sender-receiver-port",
            ),
            pytest.param(
                "E_Require",
                "operation event through require port 'rRecords' (an operation event needs a provide port)",
                id="require-port",
            ),
            pytest.param(
                "E_Missing",
                "unresolved reference /Acme/Interfaces/IRecords/Missing "
                "(not an operation of port 'pRecords's interface 'IRecords')",
                id="undeclared-operation",
            ),
            pytest.param(
                "E_Foreign",
                "operation event names the port /Acme/Clients/ClientSwc/pRecords, which is not a port of its "
                "own SWC 'ServerSwc' (/Acme/Components/ServerSwc)",
                id="port-of-another-component",
            ),
            pytest.param(
                "E_Elsewhere",
                "unresolved reference /Acme/Interfaces/IOther/Ping "
                "(not an operation of port 'pRecords's interface 'IRecords')",
                id="operation-of-another-interface",
            ),
            pytest.param(
                "E_NoIref",
                "operation event has no CONTEXT-P-PORT-REF in an OPERATION-IREF",
                id="no-operation-iref",
            ),
            pytest.param(
                "E_NoTarget",
                "operation event has no TARGET-PROVIDED-OPERATION-REF in an OPERATION-IREF",
                id="no-target-operation-ref",
            ),
            pytest.param(
                "E_PrPort",
                "chain-skipped: port 'prRecords' was skipped (PR (combined provide+require) ports have no SWCraft "
                "equivalent (Port.direction is provide|require only))",
                id="port-the-port-mapper-skipped",
            ),
            pytest.param(
                "E_Bare",
                "unresolved reference ReadRecord (not an AUTOSAR path)",
                id="target-not-a-path",
            ),
        ],
    )
    def test_the_reason(self, imported, event: str, reason: str) -> None:
        _, report = imported

        assert _reason(report, event) == reason


class TestTheStubSeam:
    """The external stub seam, as the access entries use it.

    The stub minted for the port lists the operation the event names and the event is bound; a target
    no stub is minted for keeps the line ``chain_skip`` records, which names a stub only if one is minted
    **and written** (``name_written_stubs``). ``test_external_stubs_import.py`` holds an event naming no
    listed operation."""

    def test_a_stub_minted_and_written_lists_the_operation_and_the_event_is_bound(self, imported) -> None:
        model_root, report = imported

        stubs = {i.name: i for i in read_interfaces(str(model_root)) if isinstance(i, ExternalInterface)}
        assert (stubs["IRemote_ext0"].external_ref, stubs["IRemote_ext0"].operations) == (
            "/Remote/Interfaces/IRemote",
            ["Serve"],
        )
        assert not [entry for entry in report.skip_entries if entry.path == f"{BEHAVIOR}/E_Remote"]

    def test_a_stub_that_collides_with_a_declared_name_lists_under_its_ext_name(self, imported) -> None:
        model_root, report = imported

        stubs = {i.name: i for i in read_interfaces(str(model_root)) if isinstance(i, ExternalInterface)}
        assert stubs["IRecords_ext0"].external_ref == "/Remote/Interfaces/IRecords"
        assert stubs["IRecords_ext0"].operations == ["ReadRecord"]
        assert not [entry for entry in report.skip_entries if entry.path == f"{BEHAVIOR}/E_Clash"]

    def test_conflicting_dests_mint_no_stub_and_keep_the_path_wording(self, imported) -> None:
        model_root, report = imported

        assert "/Remote/Interfaces/IConflict" in {record.target for record in report.dest_disagreements}
        assert "IConflict" not in {i.name for i in read_interfaces(str(model_root))}
        assert _reason(report, "E_Conflict") == (
            "operation event through port 'pConflict' whose interface reference /Remote/Interfaces/IConflict "
            "is not declared by this export"
        )

    def test_a_selection_that_drops_the_stub_keeps_the_path_wording(self, import_into_fresh_project) -> None:
        """``--packages Clients`` writes ``ClientSwc`` alone, so its event lines name no stub.

        The two events through a minted stub are ``ServerSwc``'s and leave with it; the one through the
        target no stub is minted for keeps the path wording."""
        model_root, report = import_into_fresh_project(FIXTURE, packages=["Clients"])

        assert set(_swcs(model_root)) == {"ClientSwc"}
        assert not [i for i in read_interfaces(str(model_root)) if isinstance(i, ExternalInterface)]
        assert not [entry for entry in report.skip_entries if entry.path.endswith(("/E_Remote", "/E_Clash"))]
        assert "external interface stub" not in _reason(report, "E_Conflict")


class TestAQueueLengthOtherThanOneIsReported:
    """A queue length other than one is not preserved; it is reported at import."""

    def test_one_line_for_the_com_spec_of_two(self, imported) -> None:
        _, report = imported

        assert report.queue_length_lines == [
            "ServerSwc/pRecords: SERVER-COM-SPEC of operation 'ReadOther' declares QUEUE-LENGTH 2"
        ]

    def test_the_report_renders_it_under_its_own_heading_and_not_as_a_skip(self, imported) -> None:
        _, report = imported

        rendered = report.render()
        assert (
            f"{QUEUE_LENGTH_HEADING} (1; every SERVER-COM-SPEC is exported with QUEUE-LENGTH 1):\n"
            "  - ServerSwc/pRecords: SERVER-COM-SPEC of operation 'ReadOther' declares QUEUE-LENGTH 2"
        ) in rendered
        assert not [entry for entry in report.skip_entries if entry.tag == "SERVER-COM-SPEC"]

    def test_a_component_the_selection_drops_reports_none(self, import_into_fresh_project) -> None:
        _, report = import_into_fresh_project(FIXTURE, packages=["Clients"])

        assert report.queue_length_lines == []

    @pytest.mark.parametrize(
        ("edit", "expected"),
        [
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", "<QUEUE-LENGTH>abc</QUEUE-LENGTH>"),
                ["ServerSwc/pRecords: SERVER-COM-SPEC of operation 'ReadOther' declares QUEUE-LENGTH abc"],
                id="unreadable-is-reported-as-spelled",
            ),
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", "<QUEUE-LENGTH>01</QUEUE-LENGTH>"),
                [],
                id="another-spelling-of-one-is-no-line",
            ),
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", "<QUEUE-LENGTH>0x1</QUEUE-LENGTH>"),
                [],
                id="a-hex-one-is-no-line",
            ),
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", "<QUEUE-LENGTH>0b1</QUEUE-LENGTH>"),
                [],
                id="a-binary-one-is-no-line",
            ),
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", "<QUEUE-LENGTH>001</QUEUE-LENGTH>"),
                [],
                id="an-octal-one-is-no-line",
            ),
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", "<QUEUE-LENGTH>+01</QUEUE-LENGTH>"),
                ["ServerSwc/pRecords: SERVER-COM-SPEC of operation 'ReadOther' declares QUEUE-LENGTH +01"],
                id="a-signed-octal-is-no-integer-of-the-pattern",
            ),
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", "<QUEUE-LENGTH>0x2</QUEUE-LENGTH>"),
                ["ServerSwc/pRecords: SERVER-COM-SPEC of operation 'ReadOther' declares QUEUE-LENGTH 0x2"],
                id="a-hex-two-is-a-line-as-spelled",
            ),
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", "<QUEUE-LENGTH>\u00a01\u00a0</QUEUE-LENGTH>"),
                ["ServerSwc/pRecords: SERVER-COM-SPEC of operation 'ReadOther' declares QUEUE-LENGTH \u00a01\u00a0"],
                id="a-no-break-space-padded-one-is-a-line-as-spelled",
            ),
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", "<QUEUE-LENGTH> \t\n1\n</QUEUE-LENGTH>"),
                [],
                id="an-xml-whitespace-padded-one-is-no-line",
            ),
            pytest.param(
                ("<QUEUE-LENGTH>2</QUEUE-LENGTH>", ""),
                [],
                id="absent-is-no-line",
            ),
            pytest.param(
                (
                    '<REQUIRED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/Acme/Interfaces/IRecords'
                    "</REQUIRED-INTERFACE-TREF>",
                    '<REQUIRED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/Acme/Interfaces/IRecords'
                    "</REQUIRED-INTERFACE-TREF><PROVIDED-COM-SPECS><SERVER-COM-SPEC>"
                    '<OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/Acme/Interfaces/IRecords/Reset</OPERATION-REF>'
                    "<QUEUE-LENGTH>5</QUEUE-LENGTH></SERVER-COM-SPEC></PROVIDED-COM-SPECS>",
                ),
                ["ServerSwc/pRecords: SERVER-COM-SPEC of operation 'ReadOther' declares QUEUE-LENGTH 2"],
                id="a-require-ports-spec-is-no-line",
            ),
        ],
    )
    def test_the_mapper_decides_on_the_value(self, tmp_path: Path, edit: tuple[str, str], expected: list[str]) -> None:
        """The rule at the mapper, on spellings the fixture does not carry.

        A value that does not read as an integer is a line as spelled, ``01``, ``0x1`` and ``0b1`` are one (the reader's
        ``parse_int_literal``, compared with the writer's ``SERVER_QUEUE_LENGTH``) while ``0x2`` is a line as spelled,
        an absent ``QUEUE-LENGTH`` states nothing to lose, and only a provide port's com-spec is read."""
        old, new = edit
        text = FIXTURE.read_text(encoding="utf-8")
        assert text.count(old) == 1, old
        source = tmp_path / "edited.arxml"
        source.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
        model = read_arxml(str(source))
        forms: list = []
        data_types, registry, _ = map_data_types(model, elements=forms)
        interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)

        swcs, _ = map_swcs(model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})

        (server,) = [imported for imported in swcs if imported.swc.name == "ServerSwc"]
        assert server.queue_length_losses == expected


class TestTheWritersOutputImportsBack:
    """The round trip over ``operation_event_support``'s two models: generated, imported, compared by runnable."""

    @pytest.mark.parametrize("build", [mixed_event_model, every_event_shape_model], ids=["mixed", "every-shape"])
    def test_every_runnable_keeps_its_events_and_concurrency(
        self, tmp_path: Path, import_into_fresh_project, build
    ) -> None:
        source = build(tmp_path / "source")
        generate_arxml(str(source), str(tmp_path / "model.arxml"))

        model_root, report = import_into_fresh_project(tmp_path / "model.arxml")

        expected = {
            runnable.name: (runnable.events, runnable.concurrent) for runnable in _swcs(source)["ServerSwc"].runnables
        }
        actual = {
            runnable.name: (runnable.events, runnable.concurrent)
            for runnable in _swcs(model_root)["ServerSwc"].runnables
        }
        assert actual == expected
        assert not _event_skips(report)
        assert report.queue_length_lines == []

    @pytest.mark.parametrize("build", [mixed_event_model, every_event_shape_model], ids=["mixed", "every-shape"])
    def test_check_model_reports_nothing_about_an_operation_event(
        self, tmp_path: Path, import_into_fresh_project, build
    ) -> None:
        """The imported project carries no operation-event finding.

        The mixed runnable included, there is no finding at all over the component the events are on."""
        source = build(tmp_path / "source")
        generate_arxml(str(source), str(tmp_path / "model.arxml"))

        model_root, _ = import_into_fresh_project(tmp_path / "model.arxml")

        records = check_cross_layer_consistency(model_root=str(model_root)).records
        assert not [record for record in records if record.rule.startswith("swc.operation-event.")]
        assert not [record for record in records if "ServerSwc" in record.message]


class TestTheFixtureDirectory:
    def test_the_arxml_fixtures_are_exactly_the_one_export(self) -> None:
        """The set of ``*.arxml`` fixtures is pinned.

        ``.gitignore`` admits this directory's direct ``*.arxml`` children by a negation of its own; a file the negation
        skipped fails here rather than going missing from a fresh clone."""
        assert sorted(path.name for path in FIXTURE.parent.glob("*.arxml")) == ["operation_events.arxml"]
