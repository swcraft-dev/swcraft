"""The importer's half of the stub kinds: a target the export declares nowhere becomes a stub.

For the five roles that may name a stub (a component's or a composition's port
interface, a data element's or an argument's type, a reference type's base) such a
target is imported as an ``external_interface`` or ``external_type`` stub, one per
path, carrying the path and its ``DEST`` verbatim, and every committed occurrence
names it; elsewhere the path stays in the field.

The subjects run on synthetic exports under ``fixtures/arxml_external_stubs/``:
``stub_roles.arxml`` (every stubbed role, the name collisions, the shapes that must not
mint), ``dest_disagreement.arxml`` (a path whose occurrences disagree on ``DEST``) and
``stub_members.arxml``. The ``CAUSE_SELECTION`` and ``CAUSE_CONFLICT`` cases reuse the
shape corpus, whose references are in an admitted role and must still not mint.

What this module does **not** cover:

* the rendered report (``test_external_stubs_report.py``'s); here the report's
  records are asserted, and a rename line;
* the real tool exports held privately (``test-internal``);
* ``import component``, which passes no policy
  (``test_arxml_import_unresolved.py::TestImportComponentTakesItsOwnPolicy``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from arxml_import_shapes_support import shape_path
from external_stubs_support import change, listed_stub_model_root
from lxml import etree
from report_family_support import uncarried_family_counts
from swcraft_tools.arxml_import import connectors as connectors_module
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.reconcile import reserved_data_type_names, reserved_interface_names
from swcraft_tools.arxml_import.registry import stub_name, stub_names
from swcraft_tools.arxml_import.report import ImportReport, closure_data_type_names, interface_to_dict
from swcraft_tools.arxml_import.unresolved import CAUSE_ABSENT, CAUSE_CONFLICT, CAUSE_SELECTION
from swcraft_tools.arxml_reader import SkipCategory
from swcraft_tools.cli import main
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.project import require_project
from swcraft_tools.reader import read_compositions, read_data_types, read_interfaces, read_swc_model
from swcraft_tools.schema import (
    CSInterface,
    DataElement,
    DataReadRef,
    DataWriteRef,
    EnumDataType,
    EnumLiteral,
    ExternalDataType,
    ExternalInterface,
    OperationCallRef,
    OperationEvent,
    PortComSpec,
    ReferenceDataType,
    SRInterface,
    TimingEvent,
    ValueDataType,
)
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import data_type_to_dict

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "arxml_external_stubs"
ROLES = FIXTURES / "stub_roles.arxml"
DISAGREEMENT = FIXTURES / "dest_disagreement.arxml"
MEMBERS = FIXTURES / "stub_members.arxml"


def _init_project(tmp_path: Path, name: str = "proj") -> Path:
    """Scaffold a fresh project through the real ``swcraft init`` CLI path."""
    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _import(tmp_path: Path, source: Path, name: str = "proj", **options) -> tuple[str, ImportReport]:
    """Import *source* into a fresh project; return its model root and the report."""
    project = _init_project(tmp_path, name)
    report = run_import(str(source), str(project / "swcraft.yaml"), **options)
    return str(require_project(str(project / "swcraft.yaml")).resolved_model_root), report


def _by_name(elements):
    return {element.name: element for element in elements}


def _stub_refs(elements) -> dict[str, tuple[str, str]]:
    """Stub name -> ``(external_ref, external_dest)`` over one layer's elements."""
    return {
        element.name: (element.external_ref, element.external_dest)
        for element in elements
        if isinstance(element, (ExternalInterface, ExternalDataType))
    }


@pytest.fixture
def imported(tmp_path: Path) -> tuple[str, ImportReport]:
    return _import(tmp_path, ROLES)


class TestTheInterfaceStubRoles:
    """A port on an absent client-server, sender-receiver and mode-switch interface."""

    def test_every_absent_port_interface_is_one_stub_carrying_its_pair(self, imported) -> None:
        model_root, _ = imported

        assert _stub_refs(read_interfaces(model_root)) == {
            "IRemote_ext0": ("/Partner/PortInterfaces/IRemote", "CLIENT-SERVER-INTERFACE"),
            "IRemote_ext1": ("/Remote/PortInterfaces/IRemote", "CLIENT-SERVER-INTERFACE"),
            "IRemoteSr_ext0": ("/Remote/PortInterfaces/IRemoteSr", "SENDER-RECEIVER-INTERFACE"),
            "IRemoteMode_ext0": ("/Remote/ModeInterfaces/IRemoteMode", "MODE-SWITCH-INTERFACE"),
            "IBoundaryOnly_ext0": ("/Remote/PortInterfaces/IBoundaryOnly", "SENDER-RECEIVER-INTERFACE"),
        }

    def test_a_component_port_names_its_stub(self, imported) -> None:
        model_root, _ = imported

        (controller,) = read_swc_model(model_root)
        assert [(port.name, port.interface) for port in controller.ports] == [
            ("rCs", "IRemote_ext1"),
            ("rSr", "IRemoteSr_ext0"),
            ("rMode", "IRemoteMode_ext0"),
            ("rPartner", "IRemote_ext0"),
            ("pStatus", "IStatus"),
            ("pService", "IService"),
        ]

    def test_a_composition_boundary_port_names_its_stub(self, imported) -> None:
        model_root, _ = imported

        top = _by_name(read_compositions(model_root))["Top"]
        assert [(port.name, port.interface) for port in top.ports] == [
            ("rOuterCs", "IRemote_ext1"),
            ("pOuterOnly", "IBoundaryOnly_ext0"),
        ]

    def test_two_referrers_of_one_path_give_one_stub(self, imported) -> None:
        model_root, report = imported

        stubs = {stub.target: stub for stub in report.external_interface_stubs}
        shared = stubs["/Remote/PortInterfaces/IRemote"]
        assert shared.name == "IRemote_ext1"
        assert [(referrer.path, referrer.field) for referrer in shared.referrers] == [
            ("/Acme/Components/Controller/rCs", "swc.port.interface"),
            ("/Acme/Compositions/Top/rOuterCs", "compositions.composition_port.interface"),
        ]
        refs = [ref for ref, _ in _stub_refs(read_interfaces(model_root)).values()]
        assert refs.count("/Remote/PortInterfaces/IRemote") == 1


class TestTheTypeStubRoles:
    """An argument, a data element and a reference type's base on an absent implementation type."""

    def test_every_absent_implementation_type_in_an_admitted_role_is_one_stub(self, imported) -> None:
        model_root, _ = imported

        assert _stub_refs(read_data_types(model_root)) == {
            "RemoteValue_ext0": ("/Remote/Types/RemoteValue", "IMPLEMENTATION-DATA-TYPE"),
            "RemoteArg_ext0": ("/Remote/Types/RemoteArg", "IMPLEMENTATION-DATA-TYPE"),
            "RemoteBase_ext0": ("/Remote/Types/RemoteBase", "IMPLEMENTATION-DATA-TYPE"),
            "Clash_ext0": ("/Remote/Types/Clash", "IMPLEMENTATION-DATA-TYPE"),
        }

    def test_a_data_element_names_its_stub(self, imported) -> None:
        model_root, _ = imported

        status = _by_name(read_interfaces(model_root))["IStatus"]
        assert isinstance(status, SRInterface)
        assert [(element.name, element.implementation_type) for element in status.data_elements] == [
            ("value", "RemoteValue_ext0"),
            ("level", "Level"),
            ("aliased", "LocalAlias"),
            ("clashing", "Clash"),
        ]

    def test_arguments_of_two_operations_on_one_path_name_one_stub(self, imported) -> None:
        model_root, report = imported

        service = _by_name(read_interfaces(model_root))["IService"]
        assert isinstance(service, CSInterface)
        assert [
            (op.name, [(arg.name, arg.implementation_type) for arg in op.arguments]) for op in service.operations
        ] == [
            ("Run", [("request", "RemoteArg_ext0"), ("clash", "Clash_ext0")]),
            ("Stop", [("request", "RemoteArg_ext0")]),
            ("Measure", [("phys", "/Remote/AppTypes/RemotePhys")]),
        ]
        stub = {stub.name: stub for stub in report.external_type_stubs}["RemoteArg_ext0"]
        assert [referrer.occurrence for referrer in stub.referrers] == [
            "/Acme/Interfaces/IService/Run/request",
            "/Acme/Interfaces/IService/Stop/request",
        ]

    def test_a_reference_types_base_names_its_stub(self, imported) -> None:
        model_root, _ = imported

        alias = _by_name(read_data_types(model_root))["LocalAlias"]
        assert isinstance(alias, ReferenceDataType)
        assert alias.base == "RemoteBase_ext0"


class TestNamesAreMintedAfterEveryDeclaredNameIsReserved:
    """Every stub is ``<leaf>_ext<X>``: a declared element keeps its name, namesakes are numbered by sorted path."""

    def test_the_declared_element_keeps_its_name_and_its_referrer(self, imported) -> None:
        model_root, report = imported

        types = _by_name(read_data_types(model_root))
        assert isinstance(types["Clash"], ValueDataType)
        assert isinstance(types["Clash_ext0"], ExternalDataType)
        # The declared type's own referrer still names the declared type.
        status = _by_name(read_interfaces(model_root))["IStatus"]
        assert {element.name: element.implementation_type for element in status.data_elements}["clashing"] == "Clash"
        assert "Clash -> Clash_ext0 (imported stub, namesake 0 of 1 -- the stub of /Remote/Types/Clash)" in (
            report.rename_lines
        )

    def test_two_absent_paths_sharing_a_last_segment_are_two_stubs(self, imported) -> None:
        """One short name in two packages (AUTOSAR admits it): two stubs, ranked by sorted path."""
        model_root, report = imported

        stubs = _stub_refs(read_interfaces(model_root))
        assert stubs["IRemote_ext0"][0] == "/Partner/PortInterfaces/IRemote"
        assert stubs["IRemote_ext1"][0] == "/Remote/PortInterfaces/IRemote"
        assert [line for line in report.rename_lines if line.startswith("IRemote ->")] == [
            "IRemote -> IRemote_ext0 (imported stub, namesake 0 of 2 -- the stub of /Partner/PortInterfaces/IRemote)",
            "IRemote -> IRemote_ext1 (imported stub, namesake 1 of 2 -- the stub of /Remote/PortInterfaces/IRemote)",
        ]

    def test_every_stub_has_one_rename_line(self, imported) -> None:
        """One ``<leaf> -> <leaf>_ext<X>`` line per stub, compared with every rename line of the report.

        This fixture renames nothing else, so a missing or doubled line fails here."""
        _, report = imported

        stubs = [*report.external_interface_stubs, *report.external_type_stubs]
        assert sorted((f"{stub.target.rsplit('/', 1)[-1]} -> {stub.name}", stub.target) for stub in stubs) == sorted(
            (line.split(" (", 1)[0], line.rsplit(" -- the stub of ", 1)[1][:-1]) for line in report.rename_lines
        )
        assert len(stubs) == 9


class TestWhatDoesNotMint:
    def test_an_occurrence_of_an_abandoned_declaration_mints_nothing(self, imported) -> None:
        model_root, report = imported

        refs = {ref for ref, _ in _stub_refs(read_data_types(model_root)).values()}
        assert "/Remote/Types/OnlyAbandoned" not in refs
        assert "/Remote/Types/OnlyAbandoned" not in {record.target for record in report.unresolved_references}
        assert "IAbandoned" not in _by_name(read_interfaces(model_root))

    def test_a_dest_the_role_does_not_admit_keeps_the_unresolved_reference(self, imported) -> None:
        model_root, report = imported

        assert [(record.target, record.dest, record.cause) for record in report.unresolved_references] == [
            ("/Remote/AppTypes/RemotePhys", "APPLICATION-PRIMITIVE-DATA-TYPE", CAUSE_ABSENT)
        ]
        refs = {ref for ref, _ in _stub_refs(read_data_types(model_root)).values()}
        assert "/Remote/AppTypes/RemotePhys" not in refs

    def test_a_declared_but_unimported_target_keeps_its_skip(self, imported) -> None:
        model_root, report = imported

        assert "unresolved reference /Acme/Interfaces/IDeclaredMode (interface was skipped)" in {
            entry.reason for entry in report.skip_entries if entry.path.endswith("/rDeclaredMode")
        }
        assert "IDeclaredMode" not in _by_name(read_interfaces(model_root))

    def test_a_selection_cause_does_not_mint(self, tmp_path: Path) -> None:
        """``CAUSE_SELECTION``, in a role that admits a stub -- a data element's type."""
        model_root, report = _import(
            tmp_path, shape_path("packages_selection_conflict.arxml"), packages=["App", "TreeB"]
        )

        assert [(record.target, record.cause) for record in report.unresolved_references] == [
            ("/Acme/TreeA/Types/Ratio", CAUSE_SELECTION)
        ]
        assert (report.external_interface_stubs, report.external_type_stubs) == ([], [])
        assert _stub_refs(read_data_types(model_root)) == {}

    def test_a_conflict_cause_does_not_mint(self, tmp_path: Path) -> None:
        model_root, report = _import(tmp_path, shape_path("packages_selection_conflict.arxml"), packages=["Acme"])

        assert [record.cause for record in report.unresolved_references] == [CAUSE_CONFLICT]
        assert (report.external_interface_stubs, report.external_type_stubs) == ([], [])
        assert _stub_refs(read_data_types(model_root)) == {}


class TestTheChainedSkipsNameTheStub:
    """A port COM-SPEC's ``INIT-VALUE`` and an ``init`` through a stub keep their skip.

    The variable access and the call point name a member the stub lists, and are kept
    (``TestAStubListsTheMembersItsConstructsName`` has every construct).
    """

    @staticmethod
    def _reason(report: ImportReport, path_end: str) -> str:
        (reason,) = [entry.reason for entry in report.skip_entries if entry.path.endswith(path_end)]
        return reason

    def test_a_variable_access_naming_a_listed_member_is_kept(self, imported) -> None:
        model_root, report = imported

        assert _reasons(report, "/Step/readSignal") == []
        (controller,) = read_swc_model(model_root)
        (step,) = controller.runnables
        assert [(read.port, read.element) for read in step.reads] == [("rSr", "signal")]

    def test_a_reworded_entry_keeps_its_section(self, imported) -> None:
        """Re-worded through ``mint_stubs`` and ``name_written_stubs``: only the text changes, not the section."""
        _, report = imported

        (entry,) = [
            entry
            for entry in report.skip_entries
            if entry.path.endswith("/rSr/REQUIRED-COM-SPECS") and entry.tag == "INIT-VALUE"
        ]
        assert "the external interface stub 'IRemoteSr_ext0'" in entry.reason
        assert (entry.category, entry.rule) == (SkipCategory.OUTSIDE_FILE, None)

    def test_a_call_point_naming_a_listed_member_is_kept(self, imported) -> None:
        model_root, report = imported

        assert _reasons(report, "/Step/callRun") == []
        (controller,) = read_swc_model(model_root)
        (step,) = controller.runnables
        assert [(call.port, call.operation) for call in step.calls] == [("rCs", "Run")]

    def test_a_port_com_spec(self, imported) -> None:
        _, report = imported

        (reason,) = [
            entry.reason
            for entry in report.skip_entries
            if entry.path.endswith("/rSr/REQUIRED-COM-SPECS") and entry.tag == "INIT-VALUE"
        ]
        assert "a member of the external interface stub 'IRemoteSr_ext0' (/Remote/PortInterfaces/IRemoteSr)" in reason

    def test_an_init_on_a_stub_typed_element(self, imported) -> None:
        _, report = imported

        assert "declared type is the external type stub 'RemoteValue_ext0' (/Remote/Types/RemoteValue)" in (
            self._reason(report, "/IStatus/value")
        )


class TestTheWrittenProject:
    def test_the_unresolved_list_holds_only_what_was_not_stubbed(self, imported) -> None:
        _, report = imported

        assert [record.target for record in report.unresolved_references] == ["/Remote/AppTypes/RemotePhys"]
        assert sorted(stub.name for stub in report.external_interface_stubs) == [
            "IBoundaryOnly_ext0",
            "IRemoteMode_ext0",
            "IRemoteSr_ext0",
            "IRemote_ext0",
            "IRemote_ext1",
        ]
        assert sorted(stub.name for stub in report.external_type_stubs) == [
            "Clash_ext0",
            "RemoteArg_ext0",
            "RemoteBase_ext0",
            "RemoteValue_ext0",
        ]

    def test_the_files_reload_and_check_model_reports_only_the_residue(self, imported) -> None:
        model_root, _ = imported

        result = check_cross_layer_consistency(model_root=model_root)

        assert [issue.target for issue in result.records] == ["/Remote/AppTypes/RemotePhys"]

    def test_a_dry_run_shows_the_stubs_and_writes_nothing(self, tmp_path: Path) -> None:
        _, written = _import(tmp_path, ROLES, name="written")
        model_root, preview = _import(tmp_path, ROLES, name="preview", dry_run=True)

        assert preview.external_interface_stubs == written.external_interface_stubs
        assert preview.external_type_stubs == written.external_type_stubs
        assert (preview.imported_interfaces, preview.imported_data_types) == (
            written.imported_interfaces,
            written.imported_data_types,
        )
        assert list((Path(model_root) / "interfaces").glob("*.yaml")) == []
        assert list((Path(model_root) / "data_types").rglob("*.yaml")) == []


class TestASelection:
    """``--packages``: a stub survives exactly as long as a kept element references it."""

    def test_a_referenced_stub_is_kept_and_an_orphaned_one_dropped(self, tmp_path: Path) -> None:
        model_root, report = _import(tmp_path, ROLES, packages=["Components"])

        interfaces = _stub_refs(read_interfaces(model_root))
        assert "IBoundaryOnly_ext0" not in interfaces
        assert {"IRemote_ext0", "IRemote_ext1", "IRemoteSr_ext0", "IRemoteMode_ext0"} <= set(interfaces)
        assert "IBoundaryOnly_ext0" not in {stub.name for stub in report.external_interface_stubs}
        assert not any("IBoundaryOnly_ext0" in line for line in report.closure_lines)

    def test_a_type_stub_ends_the_closure_walk(self, tmp_path: Path) -> None:
        model_root, report = _import(tmp_path, ROLES, packages=["Components"])

        types = _by_name(read_data_types(model_root))
        assert isinstance(types["LocalAlias"], ReferenceDataType)
        assert isinstance(types["RemoteBase_ext0"], ExternalDataType)
        assert {stub.name for stub in report.external_type_stubs} == {
            "RemoteValue_ext0",
            "RemoteArg_ext0",
            "RemoteBase_ext0",
            "Clash_ext0",
        }


class TestDisagreeingDestGetsNoStub:
    """A path whose committed occurrences disagree on ``DEST`` gets no stub."""

    @pytest.fixture
    def disagreeing(self, tmp_path: Path) -> tuple[str, ImportReport]:
        return _import(tmp_path, DISAGREEMENT)

    def test_across_owners(self, disagreeing) -> None:
        model_root, report = disagreeing

        ports = {(swc.name, port.name): port.interface for swc in read_swc_model(model_root) for port in swc.ports}
        assert ports[("Left", "rAcross")] == ports[("Right", "rAcross")] == "/Remote/If/IAcross"
        disagreement = {entry.target: entry for entry in report.dest_disagreements}["/Remote/If/IAcross"]
        assert disagreement.dests == ("CLIENT-SERVER-INTERFACE", "SENDER-RECEIVER-INTERFACE")

    def test_within_one_owner(self, disagreeing) -> None:
        model_root, report = disagreeing

        right = _by_name(read_swc_model(model_root))["Right"]
        assert {port.name: port.interface for port in right.ports}["rWithinMode"] == "/Remote/If/IWithin"
        assert {entry.target: entry.dests for entry in report.dest_disagreements}["/Remote/If/IWithin"] == (
            "CLIENT-SERVER-INTERFACE",
            "MODE-SWITCH-INTERFACE",
        )

    def test_within_one_owner_on_two_arguments_of_one_name(self, disagreeing) -> None:
        """Two operations' ``value`` arguments: only the occurrence's own path tells the two ``DEST``s apart.

        The path includes the operation.
        """
        model_root, report = disagreeing

        mixed = _by_name(read_interfaces(model_root))["IMixedArgs"]
        assert isinstance(mixed, CSInterface)
        assert [arg.implementation_type for op in mixed.operations for arg in op.arguments] == [
            "/Remote/Types/TWithin",
            "/Remote/Types/TWithin",
        ]
        assert {entry.target: entry.dests for entry in report.dest_disagreements}["/Remote/Types/TWithin"] == (
            "IMPLEMENTATION-DATA-TYPE",
            "APPLICATION-PRIMITIVE-DATA-TYPE",
        )

    def test_an_agreeing_path_beside_them_is_stubbed(self, disagreeing) -> None:
        model_root, report = disagreeing

        assert _stub_refs(read_interfaces(model_root)) == {
            "IAgreed_ext0": ("/Remote/If/IAgreed", "CLIENT-SERVER-INTERFACE")
        }
        assert sorted(record.target for record in report.unresolved_references) == [
            "/Remote/If/IAcross",
            "/Remote/If/IFar",
            "/Remote/If/IWithin",
            "/Remote/Types/TWithin",
        ]


class TestTheSharedPieces:
    def test_the_fixture_directory_is_exactly_the_three_exports(self) -> None:
        """Pinned as a set, so a skipped ``.gitignore`` negation fails here rather than vanishing from a fresh clone."""
        assert {path.name for path in FIXTURES.glob("*.arxml")} == {ROLES.name, DISAGREEMENT.name, MEMBERS.name}

    def test_the_component_stub_path_names_through_the_moved_function(self) -> None:
        assert connectors_module.stub_names is stub_names
        assert stub_name("Speed", 0, set()) == ("Speed_ext0", 0)
        assert stub_name("Speed", 0, {"Speed_ext0"}) == ("Speed_ext1", 1)

    @pytest.mark.parametrize(
        ("ref", "dest", "description"),
        [
            ("/P/X", "PORT-INTERFACE", "d"),
            ("/P/X", "PORT-INTERFACE", ""),
            ("", "PORT-INTERFACE", ""),
            ("/P/X", "", "d"),
            ("", "", ""),
        ],
    )
    def test_the_two_serializers_write_what_the_derived_one_writes(self, ref: str, dest: str, description: str) -> None:
        """The hand-written serializers equal the derived one key for key, a blank half included.

        The derived serializer omits a ``""`` default; a hand-written arm writing it would give one element two file
        shapes.
        """
        interface = ExternalInterface(
            name="IX", kind="external_interface", external_ref=ref, external_dest=dest, description=description
        )
        data_type = ExternalDataType(
            name="TX", kind="external_type", external_ref=ref, external_dest=dest, description=description
        )

        assert list(interface_to_dict(interface).items()) == list(element_to_dict(interface).items())
        assert list(data_type_to_dict(data_type).items()) == list(element_to_dict(data_type).items())

    def test_the_closure_walk_passes_an_interface_stub(self) -> None:
        stub = ExternalInterface(
            name="IX", kind="external_interface", external_ref="/P/IX", external_dest="PORT-INTERFACE"
        )

        assert closure_data_type_names([stub], []) == set()

    def test_phase_b_reserves_no_stub_name(self) -> None:
        """A stub emits no ``SHORT-NAME``, so a name beside it is no collision: phase B is no stricter than the writer.

        Nor is it stricter than ``check-model``.
        """
        enum = EnumDataType(name="Mode", kind="enum", literals=[EnumLiteral(name="OFF", value=0)])
        compu = ExternalDataType(
            name="Mode_CompuMethod", kind="external_type", external_ref="/P/M", external_dest="IMPLEMENTATION-DATA-TYPE"
        )
        owner = SRInterface(
            name="I", kind="sender_receiver", data_elements=[DataElement(name="e", application_type="Mode")]
        )
        constant = ExternalInterface(
            name="I_e_Init", kind="external_interface", external_ref="/P/I_e_Init", external_dest="PORT-INTERFACE"
        )

        assert reserved_data_type_names({"Mode": enum, "Mode_CompuMethod": compu}) == {}
        assert reserved_interface_names([owner, constant]) == {}


# Variants of the roles export, one shape each

#: Insertion anchors in ``stub_roles.arxml``: each new element goes in front of one.
_BEFORE_STATUS_PORT = "                <P-PORT-PROTOTYPE>\n                  <SHORT-NAME>pStatus</SHORT-NAME>"
_BEFORE_CONTROLLER = "            <APPLICATION-SW-COMPONENT-TYPE>\n              <SHORT-NAME>Controller</SHORT-NAME>"
_BEFORE_LOCAL_ALIAS = "            <IMPLEMENTATION-DATA-TYPE>\n              <SHORT-NAME>LocalAlias</SHORT-NAME>"


def _variant(tmp_path: Path, *edits: tuple[str, str], name: str = "variant.arxml") -> Path:
    """``stub_roles.arxml`` with each ``(old, new)`` replaced everywhere; every *old* must occur."""
    text = ROLES.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _insert(anchor: str, xml: str) -> tuple[str, str]:
    return anchor, xml + "\n" + anchor


def _reasons(report: ImportReport, path_end: str) -> list[str]:
    return [entry.reason for entry in report.skip_entries if entry.path.endswith(path_end)]


def _residue(model_root: str) -> list[str]:
    return [issue.target for issue in check_cross_layer_consistency(model_root=model_root).records]


class TestAReservedDeviceName:
    """A Windows device name is never a stub's, and a stub no written element names is not written."""

    def test_a_stub_is_never_named_for_a_device(self, tmp_path: Path) -> None:
        """``<leaf>_ext<X>`` is no device name, so a stub is never skipped for one."""
        source = _variant(
            tmp_path,
            ("/Remote/ModeInterfaces/IRemoteMode", "/Remote/ModeInterfaces/AUX"),
            ("/Remote/Types/RemoteArg", "/Remote/Types/con"),
        )
        model_root, report = _import(tmp_path, source)

        assert _stub_refs(read_interfaces(model_root))["AUX_ext0"] == (
            "/Remote/ModeInterfaces/AUX",
            "MODE-SWITCH-INTERFACE",
        )
        assert _stub_refs(read_data_types(model_root))["con_ext0"] == ("/Remote/Types/con", "IMPLEMENTATION-DATA-TYPE")
        (controller,) = read_swc_model(model_root)
        assert {port.name: port.interface for port in controller.ports}["rMode"] == "AUX_ext0"
        assert [line for line in report.rename_lines if line.startswith(("AUX ->", "con ->"))] == [
            "AUX -> AUX_ext0 (imported stub, namesake 0 of 1 -- the stub of /Remote/ModeInterfaces/AUX)",
            "con -> con_ext0 (imported stub, namesake 0 of 1 -- the stub of /Remote/Types/con)",
        ]
        assert not any("reserved-device-name" in entry.reason for entry in report.skip_entries)
        assert _residue(model_root) == ["/Remote/AppTypes/RemotePhys"]

    def test_a_component_skipped_for_a_device_name_takes_its_only_stubs_with_it(self, tmp_path: Path) -> None:
        """``Controller`` renamed ``CON`` is skipped, and the stubs only its ports named go with it.

        The read and the call name listed members, so they go with the component with no
        line of their own; the port's ``INIT-VALUE`` line is the chained one."""
        source = _variant(
            tmp_path,
            ("<SHORT-NAME>Controller</SHORT-NAME>", "<SHORT-NAME>CON</SHORT-NAME>"),
            ("/Acme/Components/Controller", "/Acme/Components/CON"),
        )
        model_root, report = _import(tmp_path, source)

        written = set(_stub_refs(read_interfaces(model_root)))
        assert written == {"IRemote_ext1", "IBoundaryOnly_ext0"}
        # A stated limit: the lists are decided when the stubs are minted, before the
        # reserved-name pass skips the component, so `Run`, named by its call alone,
        # stays listed, as after a `--packages` drop (the test below).
        stubs = {i.name: i for i in read_interfaces(model_root) if isinstance(i, ExternalInterface)}
        assert (stubs["IRemote_ext1"].operations, stubs["IBoundaryOnly_ext0"].operations) == (["Run"], [])
        assert {stub.name for stub in report.external_interface_stubs} == written
        assert all(stub.referrers for stub in report.external_interface_stubs)
        assert _reasons(report, "/Step/readSignal") == _reasons(report, "/Step/callRun") == []
        (init_line,) = _reasons(report, "/rSr/REQUIRED-COM-SPECS")
        assert "IRemoteSr" in init_line
        assert "external interface stub" not in init_line

    def test_a_chained_skip_under_a_selection_names_no_dropped_stub(self, tmp_path: Path) -> None:
        """``Top`` selected alone: ``Controller`` and the stubs only its ports name are not written.

        Its read and call leave with it; the port's ``INIT-VALUE`` line is the chained one."""
        source = _variant(
            tmp_path,
            (
                "              <COMPONENTS>\n"
                "                <SW-COMPONENT-PROTOTYPE>\n"
                "                  <SHORT-NAME>ctl</SHORT-NAME>\n"
                '                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">'
                "/Acme/Components/Controller</TYPE-TREF>\n"
                "                </SW-COMPONENT-PROTOTYPE>\n"
                "              </COMPONENTS>\n",
                "",
            ),
        )
        model_root, report = _import(tmp_path, source, packages=["Compositions"])

        assert set(_stub_refs(read_interfaces(model_root))) == {"IRemote_ext1", "IBoundaryOnly_ext0"}
        # The stated limit: the lists are decided before the selection, so `Run` stays listed.
        stubs = {i.name: i for i in read_interfaces(model_root) if isinstance(i, ExternalInterface)}
        assert (stubs["IRemote_ext1"].operations, stubs["IBoundaryOnly_ext0"].operations) == (["Run"], [])
        assert _reasons(report, "/Step/readSignal") == _reasons(report, "/Step/callRun") == []
        (init_line,) = _reasons(report, "/rSr/REQUIRED-COM-SPECS")
        assert "external interface stub" not in init_line


class TestAnInitThroughAReferenceChain:
    """An ``init`` on an element whose type reaches a stub through a reference type is dropped."""

    @pytest.mark.parametrize("constant", ["CZero", "CHex"])
    def test_the_init_is_dropped_with_a_line_and_the_project_is_clean(self, tmp_path: Path, constant: str) -> None:
        """A dropped init takes its radix line with it: no line about an init in no written file.

        ``CHex`` is spelled ``0x10``, so the value is shaped while the chain's base is still a path."""
        source = _variant(
            tmp_path,
            _insert(
                "            <CONSTANT-SPECIFICATION>\n              <SHORT-NAME>CZero</SHORT-NAME>",
                "            <CONSTANT-SPECIFICATION>\n"
                "              <SHORT-NAME>CHex</SHORT-NAME>\n"
                "              <VALUE-SPEC>\n"
                "                <NUMERICAL-VALUE-SPECIFICATION>\n"
                "                  <VALUE>0x10</VALUE>\n"
                "                </NUMERICAL-VALUE-SPECIFICATION>\n"
                "              </VALUE-SPEC>\n"
                "            </CONSTANT-SPECIFICATION>",
            ),
            (
                '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Types/LocalAlias</TYPE-TREF>',
                '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Types/LocalAlias</TYPE-TREF>\n'
                "                  <INIT-VALUE>\n"
                "                    <CONSTANT-REFERENCE>\n"
                '                      <CONSTANT-REF DEST="CONSTANT-SPECIFICATION">'
                f"/Acme/Constants/{constant}</CONSTANT-REF>\n"
                "                    </CONSTANT-REFERENCE>\n"
                "                  </INIT-VALUE>",
            ),
        )
        model_root, report = _import(tmp_path, source)

        assert report.init_radix_lines == []
        status = _by_name(read_interfaces(model_root))["IStatus"]
        assert isinstance(status, SRInterface)
        assert {element.name: element.init for element in status.data_elements}["aliased"] is None
        (reason,) = _reasons(report, "/IStatus/aliased")
        assert reason == (
            "init: declared type 'LocalAlias' resolves through the reference chain 'LocalAlias' -> 'RemoteBase_ext0' "
            "to the external type stub 'RemoteBase_ext0' (/Remote/Types/RemoteBase), whose content the export does "
            "not declare, so no init value can be shaped against it"
        )
        assert _residue(model_root) == ["/Remote/AppTypes/RemotePhys"]


_STUB_CLAUSE = "; the element's declared type is the external type stub 'RemoteValue_ext0' (/Remote/Types/RemoteValue)"
_BEFORE_CZERO = "            <CONSTANT-SPECIFICATION>\n              <SHORT-NAME>CZero</SHORT-NAME>"


class TestAnInitLineOnAStubTypedElementNamesTheStub:
    """Whatever reason drops the init of a stub-typed element, the line names the stub.

    The stub reason is re-worded in place; any other reason keeps its sentence and gains
    one clause (a ``str.replace`` of a phrase it does not carry left the stub unnamed)."""

    def test_the_stub_reason_is_unchanged(self, imported) -> None:
        """The stub reason's pinned sentence, byte for byte."""
        _, report = imported

        (reason,) = _reasons(report, "/IStatus/value")
        assert reason == (
            "init: declared type is the external type stub 'RemoteValue_ext0' (/Remote/Types/RemoteValue), whose "
            "content the export does not declare, so no init value can be shaped against it"
        )

    def test_a_port_s_own_value_names_the_stub(self, tmp_path: Path) -> None:
        source = _variant(
            tmp_path,
            _insert(
                _BEFORE_CZERO,
                "            <CONSTANT-SPECIFICATION>\n"
                "              <SHORT-NAME>COther</SHORT-NAME>\n"
                "              <VALUE-SPEC>\n"
                "                <NUMERICAL-VALUE-SPECIFICATION>\n"
                "                  <VALUE>1</VALUE>\n"
                "                </NUMERICAL-VALUE-SPECIFICATION>\n"
                "              </VALUE-SPEC>\n"
                "            </CONSTANT-SPECIFICATION>",
            ),
            (
                "                  <SHORT-NAME>pStatus</SHORT-NAME>\n",
                "                  <SHORT-NAME>pStatus</SHORT-NAME>\n"
                "                  <PROVIDED-COM-SPECS>\n"
                "                    <NONQUEUED-SENDER-COM-SPEC>\n"
                '                      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">'
                "/Acme/Interfaces/IStatus/value</DATA-ELEMENT-REF>\n"
                "                      <INIT-VALUE>\n"
                "                        <CONSTANT-REFERENCE>\n"
                '                          <CONSTANT-REF DEST="CONSTANT-SPECIFICATION">'
                "/Acme/Constants/COther</CONSTANT-REF>\n"
                "                        </CONSTANT-REFERENCE>\n"
                "                      </INIT-VALUE>\n"
                "                    </NONQUEUED-SENDER-COM-SPEC>\n"
                "                  </PROVIDED-COM-SPECS>\n",
            ),
        )
        model_root, report = _import(tmp_path, source)

        status = _by_name(read_interfaces(model_root))["IStatus"]
        assert isinstance(status, SRInterface)
        assert {element.name: element.init for element in status.data_elements}["value"] is None
        # The element's own init is decided by the element alone; the port's own
        # value is a line of its own.
        (reason,) = [
            entry.reason
            for entry in report.skip_entries
            if entry.path.endswith("/IStatus/value") and entry.tag == "INIT-VALUE"
        ]
        assert reason == (
            "init: declared type is the external type stub 'RemoteValue_ext0' (/Remote/Types/RemoteValue), whose "
            "content the export does not declare, so no init value can be shaped against it"
        )
        # The port's value cannot be shaped against the stub either: one behavioural
        # F4 line at the port's key (so ``import component`` aborts on it), naming the
        # stub, and no ``com_specs`` entry.
        (port_line,) = [entry for entry in report.skip_entries if entry.path.endswith("/pStatus/PROVIDED-COM-SPECS")]
        assert (port_line.tag, port_line.effect) == ("INIT-VALUE", "report")
        assert port_line.reason == (
            "INIT-VALUE of a com-spec not carried: data element 'value': init: declared type is the external "
            "type stub 'RemoteValue_ext0' (/Remote/Types/RemoteValue), whose content the export does not declare, so "
            "no init value can be shaped against it"
        )
        (controller,) = [swc for swc in read_swc_model(model_root) if swc.name == "Controller"]
        assert [port.com_specs for port in controller.ports if port.name == "pStatus"] == [[]]

    def test_an_unreadable_constant_names_the_stub(self, tmp_path: Path) -> None:
        source = _variant(
            tmp_path,
            (
                "              <SHORT-NAME>CZero</SHORT-NAME>\n"
                "              <VALUE-SPEC>\n"
                "                <NUMERICAL-VALUE-SPECIFICATION>\n"
                "                  <VALUE>0</VALUE>\n"
                "                </NUMERICAL-VALUE-SPECIFICATION>\n",
                "              <SHORT-NAME>CZero</SHORT-NAME>\n"
                "              <VALUE-SPEC>\n"
                "                <NUMERICAL-VALUE-SPECIFICATION>\n"
                "                </NUMERICAL-VALUE-SPECIFICATION>\n",
            ),
        )
        model_root, report = _import(tmp_path, source)

        status = _by_name(read_interfaces(model_root))["IStatus"]
        assert isinstance(status, SRInterface)
        assert {element.name: element.init for element in status.data_elements}["value"] is None
        (reason,) = _reasons(report, "/IStatus/value")
        assert reason == (
            "INIT-VALUE references '/Acme/Constants/CZero': NUMERICAL-VALUE-SPECIFICATION carries no VALUE"
            + _STUB_CLAUSE
        )


class TestADeclaredButUnimportedNameIsNotAStubs:
    """A stub reserves only the names the project carries: a declared-but-unimported one is free.

    The refused declaration is absent from the model and the output, and on a round trip
    the plain name is the fixed point. Every stub is ``<leaf>_ext<X>`` numbered over the
    stubs alone, so the refused name takes no part. What this does not reach: a reader of
    the source who knew the refused declaration may read the stub as it.
    """

    def test_an_interface_the_reader_refused_leaves_its_name_to_the_stub(self, tmp_path: Path) -> None:
        """``IDeclaredMode`` is a mode-switch interface the reader refuses and does not index."""
        source = _variant(
            tmp_path,
            _insert(
                _BEFORE_STATUS_PORT,
                "                <R-PORT-PROTOTYPE>\n"
                "                  <SHORT-NAME>rFarMode</SHORT-NAME>\n"
                '                  <REQUIRED-INTERFACE-TREF DEST="MODE-SWITCH-INTERFACE">'
                "/Remote/ModeInterfaces/IDeclaredMode</REQUIRED-INTERFACE-TREF>\n"
                "                </R-PORT-PROTOTYPE>",
            ),
        )
        model_root, report = _import(tmp_path, source)

        stubs = _stub_refs(read_interfaces(model_root))
        assert stubs["IDeclaredMode_ext0"] == ("/Remote/ModeInterfaces/IDeclaredMode", "MODE-SWITCH-INTERFACE")
        assert "IDeclaredMode_ext1" not in stubs
        assert [line for line in report.rename_lines if line.startswith("IDeclaredMode ->")] == [
            "IDeclaredMode -> IDeclaredMode_ext0 (imported stub, namesake 0 of 1 -- the stub of "
            "/Remote/ModeInterfaces/IDeclaredMode)"
        ]

    def test_a_type_the_mapper_skipped_leaves_its_name_to_the_stub(self, tmp_path: Path) -> None:
        """``Pointer`` is an implementation type of a category the mapper skips."""
        source = _variant(
            tmp_path,
            _insert(
                _BEFORE_LOCAL_ALIAS,
                "            <IMPLEMENTATION-DATA-TYPE>\n"
                "              <SHORT-NAME>Pointer</SHORT-NAME>\n"
                "              <CATEGORY>DATA_REFERENCE</CATEGORY>\n"
                "            </IMPLEMENTATION-DATA-TYPE>",
            ),
            ("/Remote/Types/RemoteValue", "/Remote/Types/Pointer"),
        )
        model_root, report = _import(tmp_path, source)

        stubs = _stub_refs(read_data_types(model_root))
        assert stubs["Pointer_ext0"] == ("/Remote/Types/Pointer", "IMPLEMENTATION-DATA-TYPE")
        assert "Pointer_ext1" not in stubs
        assert [line for line in report.rename_lines if line.startswith("Pointer ->")] == [
            "Pointer -> Pointer_ext0 (imported stub, namesake 0 of 1 -- the stub of /Remote/Types/Pointer)"
        ]


_BEFORE_CONSTANTS = "        <AR-PACKAGE>\n          <SHORT-NAME>Constants</SHORT-NAME>"


def _twin_package(elements: str) -> tuple[str, str]:
    """An ``/Acme/Twins`` package holding *elements*, inserted before ``Constants``."""
    return _insert(
        _BEFORE_CONSTANTS,
        "        <AR-PACKAGE>\n"
        "          <SHORT-NAME>Twins</SHORT-NAME>\n"
        "          <ELEMENTS>\n" + elements + "          </ELEMENTS>\n"
        "        </AR-PACKAGE>",
    )


class TestAStubStillYieldsToACarriedName:
    """The names the import writes or claims stay reserved: an imported element's and a conflicting group's.

    A conflicting group's references are written unresolved to the claimed name, so a
    stub of that name would bind them; a carried name that is ``<leaf>_ext0`` moves the
    stub's X past it (:class:`TestTheStubNamingRule`)."""

    def test_an_imported_interface(self, tmp_path: Path) -> None:
        source = _variant(tmp_path, ("/Remote/PortInterfaces/IBoundaryOnly", "/Remote/PortInterfaces/IStatus"))
        model_root, report = _import(tmp_path, source)

        interfaces = read_interfaces(model_root)
        assert isinstance(_by_name(interfaces)["IStatus"], SRInterface)
        assert _stub_refs(interfaces)["IStatus_ext0"][0] == "/Remote/PortInterfaces/IStatus"
        assert any(
            line.startswith("IStatus -> IStatus_ext0 (imported stub, namesake 0 of 1") for line in report.rename_lines
        )

    def test_a_conflicting_interface_group(self, tmp_path: Path) -> None:
        """``IRemoteSr`` declared twice with different definitions: a conflict, whose name stays claimed."""
        declaration = (
            "            <SENDER-RECEIVER-INTERFACE>\n"
            "              <SHORT-NAME>IRemoteSr</SHORT-NAME>\n"
            "              <IS-SERVICE>false</IS-SERVICE>\n"
            "              <DATA-ELEMENTS>\n"
            "                <VARIABLE-DATA-PROTOTYPE>\n"
            "                  <SHORT-NAME>{element}</SHORT-NAME>\n"
            '                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Types/Level</TYPE-TREF>\n'
            "                </VARIABLE-DATA-PROTOTYPE>\n"
            "              </DATA-ELEMENTS>\n"
            "            </SENDER-RECEIVER-INTERFACE>\n"
        )
        source = _variant(
            tmp_path,
            _insert(
                "            <MODE-SWITCH-INTERFACE>\n              <SHORT-NAME>IDeclaredMode</SHORT-NAME>",
                declaration.format(element="one").rstrip("\n"),
            ),
            _twin_package(declaration.format(element="two")),
        )
        model_root, report = _import(tmp_path, source)

        interfaces = read_interfaces(model_root)
        assert [(g.key.layer, g.key.name) for g in report.conflicting_groups] == [("interfaces", "IRemoteSr")]
        assert "IRemoteSr" not in _by_name(interfaces)
        assert _stub_refs(interfaces)["IRemoteSr_ext0"][0] == "/Remote/PortInterfaces/IRemoteSr"
        assert any(
            line.startswith("IRemoteSr -> IRemoteSr_ext0 (imported stub, namesake 0 of 1")
            for line in report.rename_lines
        )

    def test_a_conflicting_type_group(self, tmp_path: Path) -> None:
        """``Twin`` declared twice with different definitions: a conflict, whose name stays claimed."""
        source = _variant(
            tmp_path,
            _insert(
                _BEFORE_LOCAL_ALIAS,
                "            <IMPLEMENTATION-DATA-TYPE>\n"
                "              <SHORT-NAME>Twin</SHORT-NAME>\n"
                "              <CATEGORY>TYPE_REFERENCE</CATEGORY>\n"
                "              <SW-DATA-DEF-PROPS>\n"
                "                <SW-DATA-DEF-PROPS-VARIANTS>\n"
                "                  <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                '                    <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
                "/Acme/Types/Level</IMPLEMENTATION-DATA-TYPE-REF>\n"
                "                  </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                "                </SW-DATA-DEF-PROPS-VARIANTS>\n"
                "              </SW-DATA-DEF-PROPS>\n"
                "            </IMPLEMENTATION-DATA-TYPE>",
            ),
            _twin_package(
                "            <IMPLEMENTATION-DATA-TYPE>\n"
                "              <SHORT-NAME>Twin</SHORT-NAME>\n"
                "              <CATEGORY>VALUE</CATEGORY>\n"
                "              <SW-DATA-DEF-PROPS>\n"
                "                <SW-DATA-DEF-PROPS-VARIANTS>\n"
                "                  <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                '                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Types/Octet</BASE-TYPE-REF>\n'
                "                  </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                "                </SW-DATA-DEF-PROPS-VARIANTS>\n"
                "              </SW-DATA-DEF-PROPS>\n"
                "            </IMPLEMENTATION-DATA-TYPE>\n"
            ),
            ("/Remote/Types/RemoteValue", "/Remote/Types/Twin"),
        )
        model_root, report = _import(tmp_path, source)

        types = read_data_types(model_root)
        assert [(g.key.layer, g.key.name) for g in report.conflicting_groups] == [("implementation_types", "Twin")]
        assert "Twin" not in _by_name(types)
        assert _stub_refs(types)["Twin_ext0"] == ("/Remote/Types/Twin", "IMPLEMENTATION-DATA-TYPE")
        assert any(line.startswith("Twin -> Twin_ext0 (imported stub, namesake 0 of 1") for line in report.rename_lines)


class TestTheComponentStubPathIsTheSameRule:
    """A component type the export declares and SWCraft does not import leaves its name to a stub.

    ``connectors.map_compositions`` claims only imported and conflicting component names."""

    def test_a_declared_but_unimported_component_type_leaves_its_name_to_the_stub(self, tmp_path: Path) -> None:
        """``Sensor`` is a service component type, which the mapper skips whole."""
        source = _variant(
            tmp_path,
            _insert(
                _BEFORE_CONTROLLER,
                "            <SERVICE-SW-COMPONENT-TYPE>\n"
                "              <SHORT-NAME>Sensor</SHORT-NAME>\n"
                "            </SERVICE-SW-COMPONENT-TYPE>",
            ),
            _insert(
                "              </COMPONENTS>\n            </COMPOSITION-SW-COMPONENT-TYPE>",
                "                <SW-COMPONENT-PROTOTYPE>\n"
                "                  <SHORT-NAME>far</SHORT-NAME>\n"
                '                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">'
                "/Remote/Components/Sensor</TYPE-TREF>\n"
                "                </SW-COMPONENT-PROTOTYPE>",
            ),
        )
        model_root, report = _import(tmp_path, source)

        swcs = {swc.name: swc for swc in read_swc_model(model_root)}
        assert swcs["Sensor_ext0"].external
        assert swcs["Sensor_ext0"].external_ref == "/Remote/Components/Sensor"
        assert "Sensor" not in swcs
        assert [line for line in report.rename_lines if line.startswith("Sensor ->")] == [
            "Sensor -> Sensor_ext0 (imported stub, namesake 0 of 1 -- the stub of /Remote/Components/Sensor)"
        ]


class TestTheShapesThatDoNotMint:
    """The importer's claims, one synthetic shape each."""

    def test_a_pr_port_on_an_absent_interface_stays_skipped(self, tmp_path: Path) -> None:
        source = _variant(
            tmp_path,
            _insert(
                _BEFORE_STATUS_PORT,
                "                <PR-PORT-PROTOTYPE>\n"
                "                  <SHORT-NAME>prBoth</SHORT-NAME>\n"
                '                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">'
                "/Remote/PortInterfaces/IPrOnly</PROVIDED-INTERFACE-TREF>\n"
                '                  <REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">'
                "/Remote/PortInterfaces/IPrOnly</REQUIRED-INTERFACE-TREF>\n"
                "                </PR-PORT-PROTOTYPE>",
            ),
        )
        model_root, report = _import(tmp_path, source)

        assert "IPrOnly" not in _stub_refs(read_interfaces(model_root))
        assert _reasons(report, "/Controller/prBoth")
        assert "/Remote/PortInterfaces/IPrOnly" not in {record.target for record in report.unresolved_references}

    def test_a_component_type_skipped_whole_mints_nothing_for_its_ports(self, tmp_path: Path) -> None:
        source = _variant(
            tmp_path,
            _insert(
                _BEFORE_CONTROLLER,
                "            <SERVICE-SW-COMPONENT-TYPE>\n"
                "              <SHORT-NAME>Svc</SHORT-NAME>\n"
                "              <PORTS>\n"
                "                <R-PORT-PROTOTYPE>\n"
                "                  <SHORT-NAME>rSvc</SHORT-NAME>\n"
                '                  <REQUIRED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">'
                "/Remote/PortInterfaces/IServiceOnly</REQUIRED-INTERFACE-TREF>\n"
                "                </R-PORT-PROTOTYPE>\n"
                "              </PORTS>\n"
                "            </SERVICE-SW-COMPONENT-TYPE>",
            ),
        )
        model_root, report = _import(tmp_path, source)

        assert "/Remote/PortInterfaces/IServiceOnly" not in {record.target for record in report.unresolved_references}
        assert _stub_refs(read_interfaces(model_root)).keys() == {
            "IRemote_ext0",
            "IRemote_ext1",
            "IRemoteSr_ext0",
            "IRemoteMode_ext0",
            "IBoundaryOnly_ext0",
        }

    def test_a_path_in_an_admitted_and_a_never_admitted_role_is_stubbed_for_the_first_alone(
        self, tmp_path: Path
    ) -> None:
        """A path is stubbed for the admitted role alone.

        A mapping set's implementation side never names a stub, so that occurrence keeps its path while the arguments
        naming the same path name the stub.
        """
        source = _variant(
            tmp_path,
            _insert(
                _BEFORE_LOCAL_ALIAS,
                "            <APPLICATION-PRIMITIVE-DATA-TYPE>\n"
                "              <SHORT-NAME>Phys</SHORT-NAME>\n"
                "              <CATEGORY>VALUE</CATEGORY>\n"
                "            </APPLICATION-PRIMITIVE-DATA-TYPE>\n"
                "            <DATA-TYPE-MAPPING-SET>\n"
                "              <SHORT-NAME>Maps</SHORT-NAME>\n"
                "              <DATA-TYPE-MAPS>\n"
                "                <DATA-TYPE-MAP>\n"
                '                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
                "/Acme/Types/Phys</APPLICATION-DATA-TYPE-REF>\n"
                '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
                "/Remote/Types/RemoteArg</IMPLEMENTATION-DATA-TYPE-REF>\n"
                "                </DATA-TYPE-MAP>\n"
                "              </DATA-TYPE-MAPS>\n"
                "            </DATA-TYPE-MAPPING-SET>",
            ),
        )
        model_root, report = _import(tmp_path, source)

        assert _stub_refs(read_data_types(model_root))["RemoteArg_ext0"] == (
            "/Remote/Types/RemoteArg",
            "IMPLEMENTATION-DATA-TYPE",
        )
        service = _by_name(read_interfaces(model_root))["IService"]
        assert isinstance(service, CSInterface)
        assert service.operations[1].arguments[0].implementation_type == "RemoteArg_ext0"
        (record,) = [record for record in report.unresolved_references if record.target == "/Remote/Types/RemoteArg"]
        assert {referrer.tag for referrer in record.referrers} == {"DATA-TYPE-MAPPING-SET"}

    def test_a_differing_dest_on_an_abandoned_occurrence_does_not_disagree(self, tmp_path: Path) -> None:
        """Only committed occurrences count: the refused ``IAbandoned``'s differing ``DEST`` is no disagreement."""
        source = _variant(
            tmp_path,
            (
                '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Remote/Types/OnlyAbandoned</TYPE-TREF>',
                '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Remote/Types/RemoteArg</TYPE-TREF>',
            ),
        )
        model_root, report = _import(tmp_path, source)

        assert _stub_refs(read_data_types(model_root))["RemoteArg_ext0"] == (
            "/Remote/Types/RemoteArg",
            "IMPLEMENTATION-DATA-TYPE",
        )
        assert report.dest_disagreements == []

    def test_a_path_inside_a_generated_package_keeps_the_unresolved_reference(self, tmp_path: Path) -> None:
        """A path in ``/PortInterfaces`` or ``/DataTypes`` mints no stub and keeps its path record.

        The writer declares the project's own elements there, so a stub of
        ``/PortInterfaces/IStatus`` would join the declared ``IStatus`` in the output.
        """
        inside = {"/PortInterfaces/IStatus", "/DataTypes/Level"}
        source = _variant(
            tmp_path,
            ("/Remote/PortInterfaces/IRemoteSr", "/PortInterfaces/IStatus"),
            ("/Remote/Types/RemoteArg", "/DataTypes/Level"),
        )
        model_root, report = _import(tmp_path, source)

        interfaces, data_types = read_interfaces(model_root), read_data_types(model_root)
        refs = {ref for ref, _ in (*_stub_refs(interfaces).values(), *_stub_refs(data_types).values())}
        assert refs.isdisjoint(inside)
        assert {"IStatus", "Level"} <= {*_by_name(interfaces), *_by_name(data_types)}
        assert not {"IStatus_ext", "Level_ext"} & {*_by_name(interfaces), *_by_name(data_types)}
        assert {stub.target for stub in (*report.external_interface_stubs, *report.external_type_stubs)}.isdisjoint(
            inside
        )
        assert {
            (record.target, record.cause) for record in report.unresolved_references if record.target in inside
        } == {(target, CAUSE_ABSENT) for target in inside}


class TestAPortValueCarriedByEquivalenceThroughAReferenceChain:
    """A port value equal to its element's is carried by equivalence (no ``com_specs`` entry).

    When the element's ``init`` is dropped through a reference chain ending at a type stub,
    the com-spec gets no ``INIT-VALUE``, so the port's source value is named at its key
    (``orchestrator.name_equivalent_port_inits_through_a_stub``)."""

    def _source(self, tmp_path: Path, port_value_xml: str) -> Path:
        return _variant(
            tmp_path,
            (
                '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Types/LocalAlias</TYPE-TREF>',
                '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Types/LocalAlias</TYPE-TREF>\n'
                "                  <INIT-VALUE>\n"
                "                    <CONSTANT-REFERENCE>\n"
                '                      <CONSTANT-REF DEST="CONSTANT-SPECIFICATION">'
                "/Acme/Constants/CZero</CONSTANT-REF>\n"
                "                    </CONSTANT-REFERENCE>\n"
                "                  </INIT-VALUE>",
            ),
            _insert(
                _BEFORE_CZERO,
                "            <CONSTANT-SPECIFICATION>\n"
                "              <SHORT-NAME>CInventedZero</SHORT-NAME>\n"
                "              <VALUE-SPEC>\n"
                "                <NUMERICAL-VALUE-SPECIFICATION>\n"
                "                  <VALUE>0</VALUE>\n"
                "                </NUMERICAL-VALUE-SPECIFICATION>\n"
                "              </VALUE-SPEC>\n"
                "            </CONSTANT-SPECIFICATION>",
            ),
            (
                "                  <SHORT-NAME>pStatus</SHORT-NAME>\n",
                "                  <SHORT-NAME>pStatus</SHORT-NAME>\n"
                "                  <PROVIDED-COM-SPECS>\n"
                "                    <NONQUEUED-SENDER-COM-SPEC>\n"
                '                      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">'
                "/Acme/Interfaces/IStatus/aliased</DATA-ELEMENT-REF>\n"
                f"                      <INIT-VALUE>{port_value_xml}</INIT-VALUE>\n"
                "                    </NONQUEUED-SENDER-COM-SPEC>\n"
                "                  </PROVIDED-COM-SPECS>\n",
            ),
        )

    @pytest.mark.parametrize(
        "port_value_xml",
        [
            '<CONSTANT-REFERENCE><CONSTANT-REF DEST="CONSTANT-SPECIFICATION">/Acme/Constants/CInventedZero'
            "</CONSTANT-REF></CONSTANT-REFERENCE>",
            "<NUMERICAL-VALUE-SPECIFICATION><VALUE>0</VALUE></NUMERICAL-VALUE-SPECIFICATION>",
        ],
        ids=["another-constant", "in-place"],
    )
    def test_the_port_s_value_is_named_when_the_element_s_init_goes(self, tmp_path: Path, port_value_xml: str) -> None:
        model_root, report = _import(tmp_path, self._source(tmp_path, port_value_xml))

        status = _by_name(read_interfaces(model_root))["IStatus"]
        assert isinstance(status, SRInterface)
        assert {element.name: element.init for element in status.data_elements}["aliased"] is None
        (controller,) = [swc for swc in read_swc_model(model_root) if swc.name == "Controller"]
        assert [port.com_specs for port in controller.ports if port.name == "pStatus"] == [[]]
        (line,) = [entry for entry in report.skip_entries if entry.path.endswith("/pStatus/PROVIDED-COM-SPECS")]
        assert (line.tag, line.effect) == ("INIT-VALUE", "report")
        assert line.reason == (
            "INIT-VALUE of a com-spec not carried: data element 'aliased': init: declared type 'LocalAlias' "
            "resolves through the reference chain 'LocalAlias' -> 'RemoteBase_ext0' to the external type stub "
            "'RemoteBase_ext0' (/Remote/Types/RemoteBase), whose content the export does not declare, so no init "
            "value can be shaped against it"
        )

    def test_a_port_naming_the_element_s_own_constant_gets_no_line_of_its_own(self, tmp_path: Path) -> None:
        """What the pass does not reach, pinned: a reference to the element's OWN constant is no port value."""
        source = self._source(
            tmp_path,
            '<CONSTANT-REFERENCE><CONSTANT-REF DEST="CONSTANT-SPECIFICATION">/Acme/Constants/CZero'
            "</CONSTANT-REF></CONSTANT-REFERENCE>",
        )
        _model_root, report = _import(tmp_path, source)

        assert [entry for entry in report.skip_entries if entry.path.endswith("/pStatus/PROVIDED-COM-SPECS")] == []


class TestAPortValueLabelLineThroughAReferenceChain:
    """A port value's ``SHORT-LABEL`` line exists only where the output holds an ``INIT-VALUE`` at that com-spec.

    Once the element's type reaches a type stub, the carried value
    (``orchestrator.without_port_inits_through_a_stub``) and the element's ``init``
    (``without_inits_through_a_stub``) both go, so a label line would cover nothing.
    Where no stub takes the value the label line stays (``test_port_init_import.py``'s
    ``TestOverrides``)."""

    _source = TestAPortValueCarriedByEquivalenceThroughAReferenceChain._source

    @pytest.mark.parametrize("value", ["0", "5"], ids=["carried-by-equivalence", "carried"])
    def test_no_label_line_once_the_value_goes_through_the_stub(self, tmp_path: Path, value: str) -> None:
        port_value_xml = (
            f"<NUMERICAL-VALUE-SPECIFICATION><SHORT-LABEL>InventedLabel</SHORT-LABEL><VALUE>{value}</VALUE>"
            "</NUMERICAL-VALUE-SPECIFICATION>"
        )
        model_root, report = _import(tmp_path, self._source(tmp_path, port_value_xml))

        (controller,) = [swc for swc in read_swc_model(model_root) if swc.name == "Controller"]
        assert [port.com_specs for port in controller.ports if port.name == "pStatus"] == [[]]
        lines = [entry for entry in report.skip_entries if entry.path.endswith("/pStatus/PROVIDED-COM-SPECS")]
        assert [line.tag for line in lines] == ["INIT-VALUE"]

    @pytest.mark.parametrize("value", ["0", "5"], ids=["equal-to-the-element-s", "its-own"])
    def test_no_label_line_on_a_composition_s_port_once_the_element_s_init_goes(
        self, tmp_path: Path, value: str
    ) -> None:
        """A composition's port has no label line when the stub takes the element's ``init``.

        It carries no com-spec value, so its label line is spent only while the element keeps its ``init``.
        """
        port_value_xml = (
            f"<NUMERICAL-VALUE-SPECIFICATION><SHORT-LABEL>InventedLabel</SHORT-LABEL><VALUE>{value}</VALUE>"
            "</NUMERICAL-VALUE-SPECIFICATION>"
        )
        source = self._source(
            tmp_path, "<NUMERICAL-VALUE-SPECIFICATION><VALUE>0</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        text = source.read_text(encoding="utf-8")
        anchor = "                <P-PORT-PROTOTYPE>\n                  <SHORT-NAME>pOuterOnly</SHORT-NAME>"
        assert anchor in text
        text = text.replace(
            anchor,
            "                <P-PORT-PROTOTYPE>\n"
            "                  <SHORT-NAME>pInventedOuter</SHORT-NAME>\n"
            "                  <PROVIDED-COM-SPECS>\n"
            "                    <NONQUEUED-SENDER-COM-SPEC>\n"
            '                      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">'
            "/Acme/Interfaces/IStatus/aliased</DATA-ELEMENT-REF>\n"
            f"                      <INIT-VALUE>{port_value_xml}</INIT-VALUE>\n"
            "                    </NONQUEUED-SENDER-COM-SPEC>\n"
            "                  </PROVIDED-COM-SPECS>\n"
            '                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Interfaces/IStatus'
            "</PROVIDED-INTERFACE-TREF>\n"
            "                </P-PORT-PROTOTYPE>\n" + anchor,
        )
        source.write_text(text, encoding="utf-8", newline="\n")
        _model_root, report = _import(tmp_path, source)

        lines = [entry for entry in report.skip_entries if entry.path.endswith("/pInventedOuter/PROVIDED-COM-SPECS")]
        assert [line.tag for line in lines] == ["INIT-VALUE"]


# An interface stub lists the members the import's constructs name under it, and every
# record that names a listed member is kept.


@pytest.fixture
def members(tmp_path: Path) -> tuple[str, ImportReport]:
    return _import(tmp_path, MEMBERS)


def _server(model_root: str):
    (server,) = read_swc_model(model_root)
    return server


def _lines(report: ImportReport, path_end: str, tag: str | None = None) -> list[str]:
    return [
        entry.reason
        for entry in report.skip_entries
        if entry.path.endswith(path_end) and (tag is None or entry.tag == tag)
    ]


class TestAStubListsTheMembersItsConstructsName:
    """``stub_members.arxml``: each construct kind names a member, and each reason one is not listed."""

    def test_each_stub_lists_its_members_by_kind_and_a_mode_switch_stub_nothing(self, members) -> None:
        model_root, _ = members

        stubs = {i.name: i for i in read_interfaces(model_root) if isinstance(i, ExternalInterface)}
        assert {name: (stub.operations, stub.data_elements) for name, stub in stubs.items()} == {
            "ICalc_ext0": (["Add", "Sub"], []),
            "ISig_ext0": ([], ["level", "speed", "torque"]),
            "IMode_ext0": ([], []),
        }

    def test_a_member_named_by_a_com_spec_alone_is_listed(self, members) -> None:
        """Members named only by com-specs are listed.

        ``Sub``, ``speed`` and ``torque`` are named by a component's server and receiver com-spec and a composition's
        sender com-spec.
        """
        model_root, report = members

        stubs = {i.name: i for i in read_interfaces(model_root) if isinstance(i, ExternalInterface)}
        assert "Sub" in stubs["ICalc_ext0"].operations
        assert {"speed", "torque"} <= set(stubs["ISig_ext0"].data_elements)
        assert not [line for line in _lines(report, "/Top/pOut/PROVIDED-COM-SPECS") if "stub" in line]

    def test_an_operation_event_is_kept_after_the_timing_event_before_it(self, members) -> None:
        model_root, report = members

        serve = {runnable.name: runnable for runnable in _server(model_root).runnables}["Serve"]
        assert serve.events == [
            TimingEvent(type="timing", period=0.01, name="E_Tick"),
            OperationEvent(type="operation", port="pCs", operation="Add", name="E_Add"),
        ]
        assert _lines(report, "/Behavior/E_Add") == []

    def test_every_access_is_kept_in_source_order(self, members) -> None:
        """The stub call precedes the declared one in the source, and so in the model."""
        model_root, report = members

        step = {runnable.name: runnable for runnable in _server(model_root).runnables}["Step"]
        assert step.reads == [DataReadRef(port="rSr", element="level")]
        assert step.writes == [DataWriteRef(port="pSr", element="level", name="sendLevel")]
        assert step.calls == [
            OperationCallRef(port="rCs", operation="Add"),
            OperationCallRef(port="rLocal", operation="Ping"),
        ]
        for kept in ("/Step/R_rSr_level", "/Step/sendLevel", "/Step/Call_rCs_Add"):
            assert _lines(report, kept) == []

    def test_a_differing_com_spec_attribute_is_a_com_specs_entry_and_nothing_else_is(self, members) -> None:
        model_root, _ = members

        ports = {port.name: port.com_specs for port in _server(model_root).ports}
        assert ports["rSr"] == [PortComSpec(data_element="level", enable_update=True)]
        assert ports["pCs"] == ports["pSr"] == ports["rCs"] == []

    def test_a_carried_com_spec_has_no_family_f5_line(self, members) -> None:
        _, report = members

        for block in ("/pCs/PROVIDED-COM-SPECS", "/pSr/PROVIDED-COM-SPECS", "/rSr/REQUIRED-COM-SPECS"):
            assert not [line for line in _lines(report, block) if "port on an external interface stub" in line]

    def test_a_mode_switch_stub_s_com_spec_keeps_family_f5(self, members) -> None:
        _, report = members

        assert _lines(report, "/rMode/REQUIRED-COM-SPECS") == [
            "MODE-SWITCH-RECEIVER-COM-SPEC of a port on an external interface stub not carried: SWCraft writes a "
            "stub port's com-specs from the members the stub lists -- a server or a client com-spec, or a "
            "non-queued sender or receiver com-spec in the port's own block -- and this com-spec is none of them"
        ]

    def test_f4_s_stub_arm_names_what_the_derivation_cannot_write(self, members) -> None:
        """A value no model value spells on a component's port; any non-silent value on a composition's."""
        _, report = members

        assert _lines(report, "/rSr/REQUIRED-COM-SPECS", "HANDLE-TIMEOUT-TYPE") == [
            "HANDLE-TIMEOUT-TYPE value 'INVENTED' of a com-spec not carried: no model value spells it"
        ]
        assert _lines(report, "/Top/pOut/PROVIDED-COM-SPECS") == [
            "HANDLE-OUT-OF-RANGE of a com-spec not carried: a composition's port carries no com-spec values, "
            "and SWCraft writes the silent one"
        ]

    def test_the_init_value_of_a_carried_com_spec_keeps_its_line_reworded(self, members) -> None:
        _, report = members

        (entry,) = [
            entry
            for entry in report.skip_entries
            if entry.path.endswith("/rSr/REQUIRED-COM-SPECS") and entry.tag == "INIT-VALUE"
        ]
        assert entry.reason == (
            "carries an INIT-VALUE that references constant '/Acme/Constants/CZero' but its DATA-ELEMENT-REF "
            "names '/Remote/Ifc/ISig/level', a member of the external interface stub 'ISig_ext0' (/Remote/Ifc/ISig), "
            "which holds the names of its members and never their types, so there is no data element type the "
            "value could be carried against"
        )
        assert (entry.category, entry.effect) == (SkipCategory.OUTSIDE_FILE, "report")
        # No family of the report counts it.
        assert uncarried_family_counts([entry]) == {}

    @pytest.mark.parametrize(
        ("path_end", "clause"),
        [
            ("/Behavior/E_Deep", "the stub does not list the member /Remote/Ifc/ICalc/Nested/Add names"),
            ("/Step/readWrongKind", "the stub does not list the member /Remote/Ifc/ICalc/Add names"),
        ],
        ids=["two-segments-under-the-stub", "a-data-element-of-a-client-server-stub"],
    )
    def test_a_record_naming_no_listed_member_keeps_its_line(self, members, path_end: str, clause: str) -> None:
        model_root, report = members

        (reason,) = _lines(report, path_end)
        assert "whose interface is the external interface stub 'ICalc_ext0' (/Remote/Ifc/ICalc)" in reason
        assert clause in reason
        step = {runnable.name: runnable for runnable in _server(model_root).runnables}["Step"]
        assert all(read.port != "rCs" for read in step.reads)

    def test_a_listed_member_through_a_port_of_the_wrong_direction_takes_the_direction_line(self, members) -> None:
        model_root, report = members

        assert _lines(report, "/Behavior/E_Backwards") == [
            "operation event through require port 'rCs' (an operation event needs a provide port)"
        ]
        assert all(
            event.name != "E_Backwards" for runnable in _server(model_root).runnables for event in runnable.events
        )

    def test_check_model_accepts_the_project(self, members) -> None:
        model_root, _ = members

        assert check_cross_layer_consistency(model_root=model_root).records == ()

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_build_names_every_kept_member_under_its_stub(self, members, tmp_path: Path, version: str) -> None:
        model_root, _ = members
        output = tmp_path / f"model-{version}.arxml"

        generate_arxml(model_root, str(output), version=version)

        text = output.read_text(encoding="utf-8")
        for reference in (
            '<TARGET-PROVIDED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/Remote/Ifc/ICalc/Add<',
            '<TARGET-REQUIRED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/Remote/Ifc/ICalc/Add<',
            '<TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">/Remote/Ifc/ISig/level<',
            '<OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/Remote/Ifc/ICalc/Sub<',
            '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Remote/Ifc/ISig/speed<',
        ):
            assert reference in text, reference


class TestWhatTheStubDecisionDeclinesKeepsItsLine:
    """A target ``mint_stubs`` declines lists nothing, and what names it keeps today's path line."""

    def test_a_dest_disagreement_keeps_the_path_wording(self, tmp_path: Path) -> None:
        source = tmp_path / "disagreeing.arxml"
        text = MEMBERS.read_text(encoding="utf-8")
        old = '<REQUIRED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/Remote/Ifc/ICalc</REQUIRED-INTERFACE-TREF>'
        assert old in text
        source.write_text(text.replace(old, old.replace("CLIENT-SERVER", "SENDER-RECEIVER")), encoding="utf-8")

        model_root, report = _import(tmp_path, source)

        assert "ICalc_ext0" not in {i.name for i in read_interfaces(model_root)}
        assert _lines(report, "/Behavior/E_Add") == [
            "operation event through port 'pCs' whose interface reference /Remote/Ifc/ICalc is not declared by "
            "this export"
        ]
        assert [line.split(":")[0] for line in _lines(report, "/pCs/PROVIDED-COM-SPECS")] == [
            "SERVER-COM-SPEC of a port on an external interface stub not carried",
            "SERVER-COM-SPEC of a port on an external interface stub not carried",
        ]


def _stated_names(model_root: str) -> dict[str, tuple]:
    """Every name field of every component: the behaviour's, and each runnable's events' and access entries'."""
    return {
        swc.name: (
            swc.behavior_name,
            [
                (
                    runnable.name,
                    [event.name for event in runnable.events],
                    [entry.name for entry in (*runnable.reads, *runnable.writes, *runnable.calls)],
                )
                for runnable in swc.runnables
            ],
        )
        for swc in read_swc_model(model_root)
    }


class TestSWCraftsOwnOutputRoundTrips:
    """The writer's output of a project whose stubs list members and whose elements state names, re-imported."""

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_no_line_and_the_same_bytes(self, tmp_path: Path, version: str) -> None:
        model = listed_stub_model_root(tmp_path / "model")
        # The import lists a stub's members sorted, so a list the project holds
        # sorted is the fixed point.
        change(model, "interfaces/IForeignSR.yaml", lambda d: d.update(data_elements=["angle", "speed"]))
        change(model, "swc/example/ExampleSWC.yaml", lambda d: d.update(behavior_name="InventedBehavior"))
        change(model, "swc/example/ExampleSWC.yaml", lambda d: d["runnables"][1]["events"][0].update(name="OnGet"))
        change(
            model, "swc/example/ExampleConsumerSWC.yaml", lambda d: d["runnables"][0]["reads"][0].update(name="Peek")
        )
        # Every name field stated: a timing, an init, an operation event; a read, write and call.
        change(model, "swc/example/ExampleSWC.yaml", lambda d: d["runnables"][0]["events"][0].update(name="Tick"))
        change(model, "swc/example/ExampleSWC.yaml", lambda d: d["runnables"][0]["writes"][0].update(name="Put"))
        change(
            model,
            "swc/example/ExampleConsumerSWC.yaml",
            lambda d: d["runnables"][0]["events"].append({"type": "init", "name": "Boot"}),
        )
        change(model, "swc/example/ExampleConsumerSWC.yaml", lambda d: d["runnables"][0]["calls"][0].update(name="Ask"))
        stated = _stated_names(str(model))
        assert check_cross_layer_consistency(model_root=str(model)).records == ()
        first = tmp_path / "first.arxml"
        generate_arxml(str(model), str(first), version=version)

        model_root, report = _import(tmp_path, first)
        second = tmp_path / "second.arxml"
        generate_arxml(model_root, str(second), version=version)

        # The only lines: the two implementations SWCraft writes itself, as for every SWCraft-written file.
        assert [(entry.path, entry.reason) for entry in report.skip_entries] == [
            (f"/ComponentTypes/{name}_Implementation", "SWC-IMPLEMENTATION has no SWCraft representation")
            for name in ("ExampleConsumerSWC", "ExampleSWC")
        ]
        assert _stated_names(model_root) == stated
        stubs = {i.name: i for i in read_interfaces(model_root) if isinstance(i, ExternalInterface)}
        assert (stubs["IForeignCS_ext0"].operations, stubs["IForeignSR_ext0"].data_elements) == (
            ["Get", "Set"],
            ["angle", "speed"],
        )
        assert second.read_bytes() == first.read_bytes()


# What the stub decision cannot list is a counted line, never a raise

_READ_THROUGH_RSR = (
    '<PORT-PROTOTYPE-REF DEST="R-PORT-PROTOTYPE">/Acme/Components/Server/rSr</PORT-PROTOTYPE-REF>\n'
    '                              <TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">'
)
_WRITE_THROUGH_PSR = (
    '<PORT-PROTOTYPE-REF DEST="P-PORT-PROTOTYPE">/Acme/Components/Server/pSr</PORT-PROTOTYPE-REF>\n'
    '                              <TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">'
)
_DEEP_EVENT_TARGET = "/Remote/Ifc/ICalc/Nested/Add</TARGET-PROVIDED-OPERATION-REF>"
_BEFORE_WRONG_KIND_READ = (
    "                        <VARIABLE-ACCESS>\n                          <SHORT-NAME>readWrongKind"
)
_BEFORE_RSR = "                <R-PORT-PROTOTYPE>\n                  <SHORT-NAME>rSr</SHORT-NAME>"
_BEFORE_DEEP_EVENT = "                    <OPERATION-INVOKED-EVENT>\n                      <SHORT-NAME>E_Deep"


def _members_variant(tmp_path: Path, *edits: tuple[str, str]) -> Path:
    """``stub_members.arxml`` with each ``(old, new)`` replaced once; every *old* must occur exactly once."""
    text = MEMBERS.read_text(encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path = tmp_path / "members_variant.arxml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _entries(report: ImportReport, path_end: str) -> list:
    return [entry for entry in report.skip_entries if entry.path.endswith(path_end)]


class TestAMemberSegmentNoStubCanListIsALine:
    """A member segment that is blank, no identifier, or alike another but for case is listed by no stub.

    The record naming it keeps its chained line, counted in the report's third section
    (outside the file): never dropped in silence, never a raise."""

    @pytest.mark.parametrize(
        ("edit", "path_end"),
        [
            ((f"{_READ_THROUGH_RSR}/Remote/Ifc/ISig/level<", f"{_READ_THROUGH_RSR}/Remote/Ifc/ISig/<"), "/R_rSr_level"),
            (
                (f"{_READ_THROUGH_RSR}/Remote/Ifc/ISig/level<", f"{_READ_THROUGH_RSR}/Remote/Ifc/ISig/ <"),
                "/R_rSr_level",
            ),
            ((_DEEP_EVENT_TARGET, _DEEP_EVENT_TARGET.replace("Nested/Add", "")), "/Behavior/E_Deep"),
            ((_DEEP_EVENT_TARGET, _DEEP_EVENT_TARGET.replace("Nested/Add", " ")), "/Behavior/E_Deep"),
            (
                (f"{_READ_THROUGH_RSR}/Remote/Ifc/ISig/level<", f"{_READ_THROUGH_RSR}/Remote/Ifc/ISig/bad-name<"),
                "/R_rSr_level",
            ),
        ],
        ids=[
            "a-read-ending-in-a-slash",
            "a-read-ending-in-a-blank-segment",
            "an-event-ending-in-a-slash",
            "an-event-ending-in-a-blank-segment",
            "a-read-naming-no-identifier",
        ],
    )
    def test_the_record_keeps_its_line_and_the_import_completes(
        self, tmp_path: Path, edit: tuple[str, str], path_end: str
    ) -> None:
        model_root, report = _import(tmp_path, _members_variant(tmp_path, edit))

        (entry,) = _entries(report, path_end)
        assert "the stub does not list the member" in entry.reason, entry.reason
        assert (entry.category, entry.effect) == (SkipCategory.OUTSIDE_FILE, "abort")
        stubs = {i.name: i for i in read_interfaces(model_root) if isinstance(i, ExternalInterface)}
        assert (stubs["ICalc_ext0"].operations, stubs["ISig_ext0"].data_elements) == (
            ["Add", "Sub"],
            ["level", "speed", "torque"],
        )
        assert check_cross_layer_consistency(model_root=model_root).records == ()

    def test_two_names_alike_but_for_case_are_listed_by_neither(self, tmp_path: Path) -> None:
        """``Level`` beside ``level``: every record naming either keeps its line, and the stub lists neither."""
        edit = (f"{_WRITE_THROUGH_PSR}/Remote/Ifc/ISig/level<", f"{_WRITE_THROUGH_PSR}/Remote/Ifc/ISig/Level<")
        model_root, report = _import(tmp_path, _members_variant(tmp_path, edit))

        stubs = {i.name: i for i in read_interfaces(model_root) if isinstance(i, ExternalInterface)}
        assert stubs["ISig_ext0"].data_elements == ["speed", "torque"]
        for path_end in ("/R_rSr_level", "/sendLevel"):
            (entry,) = _entries(report, path_end)
            assert "the stub does not list the member" in entry.reason
            assert entry.category is SkipCategory.OUTSIDE_FILE
        step = {runnable.name: runnable for runnable in _server(model_root).runnables}["Step"]
        assert (step.reads, step.writes) == ([], [])
        assert check_cross_layer_consistency(model_root=model_root).records == ()


class TestARecordTheStubDecisionNeverSees:
    """A target declared in a conflicting group is no stub candidate; its record takes the chained line at once.

    Nothing is staged for it.
    """

    def test_an_access_and_an_operation_event_through_a_conflicting_interface(self, tmp_path: Path) -> None:
        twin = (
            "        <AR-PACKAGE>\n"
            "          <SHORT-NAME>Other</SHORT-NAME>\n"
            "          <ELEMENTS>\n"
            "            <CLIENT-SERVER-INTERFACE>\n"
            "              <SHORT-NAME>ILocal</SHORT-NAME>\n"
            "              <IS-SERVICE>false</IS-SERVICE>\n"
            "              <OPERATIONS>\n"
            "                <CLIENT-SERVER-OPERATION>\n"
            "                  <SHORT-NAME>Pong</SHORT-NAME>\n"
            "                </CLIENT-SERVER-OPERATION>\n"
            "              </OPERATIONS>\n"
            "            </CLIENT-SERVER-INTERFACE>\n"
            "          </ELEMENTS>\n"
            "        </AR-PACKAGE>\n"
        )
        before_components = "        <AR-PACKAGE>\n          <SHORT-NAME>Components</SHORT-NAME>"
        provide_local = (
            "                <P-PORT-PROTOTYPE>\n"
            "                  <SHORT-NAME>pLocal</SHORT-NAME>\n"
            '                  <PROVIDED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/Acme/Interfaces/ILocal'
            "</PROVIDED-INTERFACE-TREF>\n"
            "                </P-PORT-PROTOTYPE>"
        )
        ping_event = (
            "                    <OPERATION-INVOKED-EVENT>\n"
            "                      <SHORT-NAME>E_Ping</SHORT-NAME>\n"
            '                      <START-ON-EVENT-REF DEST="RUNNABLE-ENTITY">/Acme/Components/Server/Behavior/Serve'
            "</START-ON-EVENT-REF>\n"
            "                      <OPERATION-IREF>\n"
            '                        <CONTEXT-P-PORT-REF DEST="P-PORT-PROTOTYPE">/Acme/Components/Server/pLocal'
            "</CONTEXT-P-PORT-REF>\n"
            '                        <TARGET-PROVIDED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">'
            "/Acme/Interfaces/ILocal/Ping</TARGET-PROVIDED-OPERATION-REF>\n"
            "                      </OPERATION-IREF>\n"
            "                    </OPERATION-INVOKED-EVENT>"
        )
        source = _members_variant(
            tmp_path,
            (_BEFORE_RSR, provide_local + "\n" + _BEFORE_RSR),
            (_BEFORE_DEEP_EVENT, ping_event + "\n" + _BEFORE_DEEP_EVENT),
            (before_components, twin + before_components),
        )
        model_root, report = _import(tmp_path, source)

        (call,) = _entries(report, "/Step/Call_rLocal_Ping")
        assert call.reason == (
            "calls access through port 'rLocal' whose interface reference /Acme/Interfaces/ILocal is not declared "
            "by this export"
        )
        (event,) = _entries(report, "/Behavior/E_Ping")
        assert event.reason == (
            "operation event through port 'pLocal' whose interface reference /Acme/Interfaces/ILocal is not "
            "declared by this export"
        )
        assert {call.category, event.category} == {SkipCategory.NOT_SUPPORTED}
        assert "ILocal" not in {i.name for i in read_interfaces(model_root)}


class TestAComSpecTheDerivationDoesNotWrite:
    """Family F5's hold-time line says the derivation writes no such com-spec, never that the member is unlisted.

    "No such com-spec" is one of that shape or block, or one per member; another construct may list the member.
    """

    def test_a_queued_receiver_com_spec_for_a_member_a_read_lists(self, tmp_path: Path) -> None:
        old = (
            "                    <NONQUEUED-RECEIVER-COM-SPEC>\n"
            '                      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Remote/Ifc/ISig/speed'
        )
        new = (
            "                    <QUEUED-RECEIVER-COM-SPEC>\n"
            '                      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Remote/Ifc/ISig/level'
        )
        source = _members_variant(
            tmp_path,
            (old, new),
            (
                "<HANDLE-TIMEOUT-TYPE>INVENTED</HANDLE-TIMEOUT-TYPE>\n"
                "                    </NONQUEUED-RECEIVER-COM-SPEC>",
                "<QUEUE-LENGTH>2</QUEUE-LENGTH>\n                    </QUEUED-RECEIVER-COM-SPEC>",
            ),
        )
        model_root, report = _import(tmp_path, source)

        assert "level" in {i.name: i for i in read_interfaces(model_root)}["ISig_ext0"].data_elements
        assert _lines(report, "/rSr/REQUIRED-COM-SPECS", "QUEUED-RECEIVER-COM-SPEC") == [
            "QUEUED-RECEIVER-COM-SPEC of a port on an external interface stub not carried: SWCraft writes a stub "
            "port's com-specs from the members the stub lists -- a server or a client com-spec, or a non-queued "
            "sender or receiver com-spec in the port's own block -- and this com-spec is none of them"
        ]

    def test_a_com_spec_in_the_other_block(self, tmp_path: Path) -> None:
        receiver_on_a_provide_port = (
            "                  <REQUIRED-COM-SPECS>\n"
            "                    <NONQUEUED-RECEIVER-COM-SPEC>\n"
            '                      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Remote/Ifc/ISig/level'
            "</DATA-ELEMENT-REF>\n"
            "                    </NONQUEUED-RECEIVER-COM-SPEC>\n"
            "                  </REQUIRED-COM-SPECS>\n"
        )
        anchor = (
            "                  <PROVIDED-COM-SPECS>\n"
            "                    <NONQUEUED-SENDER-COM-SPEC>\n"
            '                      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Remote/Ifc/ISig/level'
            "</DATA-ELEMENT-REF>\n"
            "                      <HANDLE-OUT-OF-RANGE>NONE"
        )
        _model_root, report = _import(
            tmp_path, _members_variant(tmp_path, (anchor, receiver_on_a_provide_port + anchor))
        )

        (line,) = _lines(report, "/pSr/REQUIRED-COM-SPECS")
        assert line.startswith("NONQUEUED-RECEIVER-COM-SPEC of a port on an external interface stub not carried")
        assert line.endswith("and this com-spec is none of them"), line
        assert not _lines(report, "/pSr/PROVIDED-COM-SPECS")


class TestASecondComSpecForOneMember:
    def test_the_first_is_carried_and_the_second_keeps_its_own_clause(self, tmp_path: Path) -> None:
        server_add = (
            "                    <SERVER-COM-SPEC>\n"
            '                      <OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/Remote/Ifc/ICalc/Add'
            "</OPERATION-REF>\n"
            "                      <QUEUE-LENGTH>1</QUEUE-LENGTH>\n"
            "                    </SERVER-COM-SPEC>\n"
        )
        model_root, report = _import(tmp_path, _members_variant(tmp_path, (server_add, server_add + server_add)))

        assert _lines(report, "/pCs/PROVIDED-COM-SPECS") == [
            "SERVER-COM-SPEC of a port on an external interface stub not carried: another com-spec of the port "
            "names the same member, and SWCraft writes one com-spec per listed member"
        ]
        assert {i.name: i for i in read_interfaces(model_root)}["ICalc_ext0"].operations == ["Add", "Sub"]


class TestARepeatedHeldAccessIsCollapsed:
    def test_one_read_kept_one_collapse_line_and_check_model_clean(self, tmp_path: Path) -> None:
        again = (
            "                        <VARIABLE-ACCESS>\n"
            "                          <SHORT-NAME>readAgain</SHORT-NAME>\n"
            "                          <ACCESSED-VARIABLE>\n"
            "                            <AUTOSAR-VARIABLE-IREF>\n"
            f"                              {_READ_THROUGH_RSR}/Remote/Ifc/ISig/level</TARGET-DATA-PROTOTYPE-REF>\n"
            "                            </AUTOSAR-VARIABLE-IREF>\n"
            "                          </ACCESSED-VARIABLE>\n"
            "                        </VARIABLE-ACCESS>\n"
        )
        model_root, report = _import(
            tmp_path, _members_variant(tmp_path, (_BEFORE_WRONG_KIND_READ, again + _BEFORE_WRONG_KIND_READ))
        )

        step = {runnable.name: runnable for runnable in _server(model_root).runnables}["Step"]
        assert step.reads == [DataReadRef(port="rSr", element="level")]
        assert report.access_collapse_lines == [
            "Server/Step: reads access rSr.level declared twice -- 'readAgain' collapsed into 'R_rSr_level'"
        ]
        assert _lines(report, "/readAgain") == []
        assert check_cross_layer_consistency(model_root=model_root).records == ()


# What a com-spec states is carried or is a line, never written back silently

_RCS_TREF = '                  <REQUIRED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/Remote/Ifc/ICalc'
_RLOCAL_TREF = '                  <REQUIRED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">/Acme/Interfaces/ILocal'
_TOP_COMPONENTS = "              </PORTS>\n              <COMPONENTS>"


def _client_com_specs(*operation_refs: str) -> str:
    """A ``REQUIRED-COM-SPECS`` block holding one ``CLIENT-COM-SPEC`` per operation reference, in order."""
    specs = "".join(
        "                    <CLIENT-COM-SPEC>\n"
        f'                      <OPERATION-REF DEST="CLIENT-SERVER-OPERATION">{ref}</OPERATION-REF>\n'
        "                    </CLIENT-COM-SPEC>\n"
        for ref in operation_refs
    )
    return f"                  <REQUIRED-COM-SPECS>\n{specs}                  </REQUIRED-COM-SPECS>\n"


def _client_operation_refs(output: Path, port: str) -> list[str]:
    """The ``OPERATION-REF`` of every ``CLIENT-COM-SPEC`` the build wrote on the require port named *port*."""
    ns = {"ar": "http://autosar.org/schema/r4.0"}
    tree = etree.parse(str(output))
    return [
        ref.text
        for prototype in tree.iterfind(".//ar:R-PORT-PROTOTYPE", ns)
        if prototype.findtext("ar:SHORT-NAME", namespaces=ns) == port
        for ref in prototype.iterfind("ar:REQUIRED-COM-SPECS/ar:CLIENT-COM-SPEC/ar:OPERATION-REF", ns)
    ]


def _composition_server_port(name: str, interface_ref: str, operation_ref: str, queue_length: str) -> str:
    """A composition ``P-PORT-PROTOTYPE`` with one ``SERVER-COM-SPEC`` stating *queue_length*."""
    return (
        "                <P-PORT-PROTOTYPE>\n"
        f"                  <SHORT-NAME>{name}</SHORT-NAME>\n"
        "                  <PROVIDED-COM-SPECS>\n"
        "                    <SERVER-COM-SPEC>\n"
        f'                      <OPERATION-REF DEST="CLIENT-SERVER-OPERATION">{operation_ref}</OPERATION-REF>\n'
        f"                      <QUEUE-LENGTH>{queue_length}</QUEUE-LENGTH>\n"
        "                    </SERVER-COM-SPEC>\n"
        "                  </PROVIDED-COM-SPECS>\n"
        f'                  <PROVIDED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">{interface_ref}'
        "</PROVIDED-INTERFACE-TREF>\n"
        "                </P-PORT-PROTOTYPE>\n"
    )


class TestAClientComSpecOnAStubPortNamesItsOperation:
    """A ``CLIENT-COM-SPEC`` on a client-server stub's require port lists its operation and is carried.

    A ``ClientComSpec`` references an operation in the role ``ServerComSpec.operation`` has.
    A second one for an operation, and one whose operation no stub can list, are family
    F5's lines rather than dropped unreported."""

    def test_each_operation_is_listed_and_carried_and_the_repeat_is_a_line(self, tmp_path: Path) -> None:
        block = _client_com_specs("/Remote/Ifc/ICalc/Add", "/Remote/Ifc/ICalc/Mul", "/Remote/Ifc/ICalc/Add")
        model_root, report = _import(tmp_path, _members_variant(tmp_path, (_RCS_TREF, block + _RCS_TREF)))

        assert _lines(report, "/rCs/REQUIRED-COM-SPECS") == [
            "CLIENT-COM-SPEC of a port on an external interface stub not carried: another com-spec of the port "
            "names the same member, and SWCraft writes one com-spec per listed member",
        ]
        assert {i.name: i for i in read_interfaces(model_root)}["ICalc_ext0"].operations == ["Add", "Mul", "Sub"]
        assert check_cross_layer_consistency(model_root=model_root).records == ()

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_a_client_com_spec_alone_lists_its_operation_and_the_build_writes_it_back(
        self, tmp_path: Path, version: str
    ) -> None:
        """``Mul``, named by the client com-spec alone, is listed, leaves no line and is in the derived com-specs.

        The derived com-specs are the require port's.
        """
        block = _client_com_specs("/Remote/Ifc/ICalc/Mul")
        model_root, report = _import(tmp_path, _members_variant(tmp_path, (_RCS_TREF, block + _RCS_TREF)))

        assert {i.name: i for i in read_interfaces(model_root)}["ICalc_ext0"].operations == ["Add", "Mul", "Sub"]
        assert _entries(report, "/rCs/REQUIRED-COM-SPECS") == []
        assert check_cross_layer_consistency(model_root=model_root).records == ()
        output = tmp_path / f"model-{version}.arxml"
        generate_arxml(model_root, str(output), version=version)
        assert "/Remote/Ifc/ICalc/Mul" in _client_operation_refs(output, "rCs")

    @pytest.mark.parametrize(
        "operation_refs",
        [
            ("/Remote/Ifc/ICalc/",),
            ("/Remote/Ifc/ICalc/ ",),
            ("/Remote/Ifc/ICalc/bad-name",),
            ("/Remote/Ifc/ICalc/Nested/Mul",),
            ("/Remote/Ifc/ISig/Mul",),
            ("/Remote/Ifc/ICalc/Mul", "/Remote/Ifc/ICalc/mul"),
        ],
        ids=["blank", "whitespace", "no-identifier", "deeper", "under-another-stub", "alike-but-for-case"],
    )
    def test_one_whose_operation_no_stub_can_list_keeps_its_line(
        self, tmp_path: Path, operation_refs: tuple[str, ...]
    ) -> None:
        block = _client_com_specs(*operation_refs)
        model_root, report = _import(tmp_path, _members_variant(tmp_path, (_RCS_TREF, block + _RCS_TREF)))

        interfaces = {i.name: i for i in read_interfaces(model_root)}
        assert interfaces["ICalc_ext0"].operations == ["Add", "Sub"]
        assert interfaces["ISig_ext0"].data_elements == ["level", "speed", "torque"]
        assert _lines(report, "/rCs/REQUIRED-COM-SPECS") == [
            "CLIENT-COM-SPEC of a port on an external interface stub not carried: it names no member the stub "
            "lists, and SWCraft writes a com-spec for a listed member only"
        ] * len(operation_refs)

    def test_one_on_a_sender_receiver_stub_s_port_lists_nothing(self, tmp_path: Path) -> None:
        """The wrong kind: a sender-receiver stub lists data elements, so an operation it names is listed nowhere."""
        rsr_tref = '                  <REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Remote/Ifc/ISig'
        block = _client_com_specs("/Remote/Ifc/ISig/Mul")
        receivers = "                  </REQUIRED-COM-SPECS>\n" + rsr_tref
        assert block.endswith("                  </REQUIRED-COM-SPECS>\n")
        client = block.removeprefix("                  <REQUIRED-COM-SPECS>\n").removesuffix(
            "                  </REQUIRED-COM-SPECS>\n"
        )
        model_root, report = _import(tmp_path, _members_variant(tmp_path, (receivers, client + receivers)))

        assert {i.name: i for i in read_interfaces(model_root)}["ISig_ext0"].data_elements == [
            "level",
            "speed",
            "torque",
        ]
        assert any(
            entry.tag == "CLIENT-COM-SPEC" and "not carried" in entry.reason
            for entry in _entries(report, "/rSr/REQUIRED-COM-SPECS")
        )

    def test_a_declared_interface_s_client_com_spec_is_judged_by_its_operation_too(self, tmp_path: Path) -> None:
        """The declared arm's twin: a repeat and a reference to no operation of the port's interface are lines."""
        block = _client_com_specs(
            "/Acme/Interfaces/ILocal/Ping", "/Acme/Interfaces/ILocal/Pong", "/Acme/Interfaces/ILocal/Ping"
        )
        _model_root, report = _import(tmp_path, _members_variant(tmp_path, (_RLOCAL_TREF, block + _RLOCAL_TREF)))

        assert sorted(_lines(report, "/rLocal/REQUIRED-COM-SPECS")) == [
            "CLIENT-COM-SPEC not carried: it is a second com-spec for one operation -- SWCraft writes one com-spec "
            "per data element or operation of the port's interface",
            "CLIENT-COM-SPEC not carried: it names no operation of the port's interface -- SWCraft writes one "
            "com-spec per data element or operation of the port's interface",
        ]


class TestACompositionServerComSpecStatesItsQueueLength:
    """A composition's server com-spec ``QUEUE-LENGTH`` other than 1 is family F4's line.

    A composition's port carries no com-spec values and the writer writes ``QUEUE-LENGTH``
    1, stub-typed or declared alike; a length that reads as 1 is no line."""

    @pytest.mark.parametrize(
        ("interface_ref", "operation_ref"),
        [("/Remote/Ifc/ICalc", "/Remote/Ifc/ICalc/Add"), ("/Acme/Interfaces/ILocal", "/Acme/Interfaces/ILocal/Ping")],
        ids=["a-stub-port", "a-declared-port"],
    )
    def test_a_length_other_than_one_is_a_line(self, tmp_path: Path, interface_ref: str, operation_ref: str) -> None:
        ports = _composition_server_port("pFour", interface_ref, operation_ref, "4") + _composition_server_port(
            "pOne", interface_ref, operation_ref, "0x1"
        )
        _model_root, report = _import(tmp_path, _members_variant(tmp_path, (_TOP_COMPONENTS, ports + _TOP_COMPONENTS)))

        (entry,) = _entries(report, "/Top/pFour/PROVIDED-COM-SPECS")
        assert entry.tag == "QUEUE-LENGTH"
        assert entry.reason == (
            "QUEUE-LENGTH 4 of a SERVER-COM-SPEC not carried: a composition's port carries no com-spec values, "
            "and SWCraft writes QUEUE-LENGTH 1"
        )
        assert (entry.category, entry.effect) == (SkipCategory.NOT_SUPPORTED, "report")
        assert _entries(report, "/Top/pOne/PROVIDED-COM-SPECS") == []


class TestAConstructThroughAPortOfTheWrongDirectionListsNothing:
    """A held record through a wrong-direction port is dropped with a line and stages no member.

    The stub lists nothing on the strength of a construct it drops.
    """

    def test_an_operation_event_through_a_require_port(self, tmp_path: Path) -> None:
        backwards = (
            '<CONTEXT-P-PORT-REF DEST="P-PORT-PROTOTYPE">/Acme/Components/Server/rCs</CONTEXT-P-PORT-REF>\n'
            '                        <TARGET-PROVIDED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">'
            "/Remote/Ifc/ICalc/Add<"
        )
        model_root, report = _import(
            tmp_path, _members_variant(tmp_path, (backwards, backwards.replace("ICalc/Add<", "ICalc/Mul<")))
        )

        assert {i.name: i for i in read_interfaces(model_root)}["ICalc_ext0"].operations == ["Add", "Sub"]
        (line,) = _lines(report, "/Behavior/E_Backwards")
        assert "the stub does not list the member /Remote/Ifc/ICalc/Mul names" in line

    def test_a_write_through_a_require_port(self, tmp_path: Path) -> None:
        backwards = (
            "                      <DATA-SEND-POINTS>\n"
            "                        <VARIABLE-ACCESS>\n"
            "                          <SHORT-NAME>sendBackwards</SHORT-NAME>\n"
            "                          <ACCESSED-VARIABLE>\n"
            "                            <AUTOSAR-VARIABLE-IREF>\n"
            f"                              {_READ_THROUGH_RSR}/Remote/Ifc/ISig/extra</TARGET-DATA-PROTOTYPE-REF>\n"
            "                            </AUTOSAR-VARIABLE-IREF>\n"
            "                          </ACCESSED-VARIABLE>\n"
            "                        </VARIABLE-ACCESS>\n"
        )
        model_root, report = _import(
            tmp_path,
            _members_variant(tmp_path, ("                      <DATA-SEND-POINTS>\n", backwards)),
        )

        assert "extra" not in {i.name: i for i in read_interfaces(model_root)}["ISig_ext0"].data_elements
        (line,) = _lines(report, "/Step/sendBackwards")
        assert "the stub does not list the member /Remote/Ifc/ISig/extra names" in line
        step = {runnable.name: runnable for runnable in _server(model_root).runnables}["Step"]
        assert step.writes == [DataWriteRef(port="pSr", element="level", name="sendLevel")]


# Every stub is `<leaf>_ext<X>`

#: The three interface paths of ``stub_roles.arxml`` the namesake variants re-point.
_SR, _MODE, _BOUNDARY = (
    "/Remote/PortInterfaces/IRemoteSr",
    "/Remote/ModeInterfaces/IRemoteMode",
    "/Remote/PortInterfaces/IBoundaryOnly",
)

#: Three far prototypes in ``Top``, typed by three component paths sharing the leaf ``Far``.
_FAR_ANCHOR = "              </COMPONENTS>\n            </COMPOSITION-SW-COMPONENT-TYPE>"


def _far_prototypes(*paths: str) -> tuple[str, str]:
    return _insert(
        _FAR_ANCHOR,
        "\n".join(
            "                <SW-COMPONENT-PROTOTYPE>\n"
            f"                  <SHORT-NAME>far{index}</SHORT-NAME>\n"
            f'                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">{path}</TYPE-TREF>\n'
            "                </SW-COMPONENT-PROTOTYPE>"
            for index, path in enumerate(paths)
        ),
    )


class TestTheStubNamingRule:
    """``<leaf>_ext<X>``, X from 0 by sorted source path over the namesakes of one layer.

    The name depends on no reading order. A legal leaf is fitted by its stem; a leaf
    over the limit stays uncapped so the identifier check refuses it.
    """

    def test_no_namesake_is_ext0(self) -> None:
        assert {path: named.name for path, named in stub_names({"/P/IFoo": "IFoo"}, set()).items()} == {
            "/P/IFoo": "IFoo_ext0"
        }

    @pytest.mark.parametrize(
        "order", [("/C/X/IFoo", "/A/X/IFoo", "/B/X/IFoo"), ("/B/X/IFoo", "/C/X/IFoo", "/A/X/IFoo")]
    )
    def test_three_namesakes_are_numbered_by_sorted_path_whatever_the_order(self, order) -> None:
        named = stub_names({path: "IFoo" for path in order}, set())

        assert {path: each.name for path, each in named.items()} == {
            "/A/X/IFoo": "IFoo_ext0",
            "/B/X/IFoo": "IFoo_ext1",
            "/C/X/IFoo": "IFoo_ext2",
        }
        assert [named[path].reason for path in sorted(named)] == [
            f"imported stub, namesake {index} of 3" for index in range(3)
        ]

    @pytest.mark.parametrize(
        ("length", "rank", "expected"),
        [
            (122, 0, "L" * 122 + "_ext0"),
            (123, 0, "L" * 123 + "_ext0"),
            (124, 0, "L" * 123 + "_ext0"),
            (122, 10, "L" * 122 + "_ext10"),
            (123, 10, "L" * 122 + "_ext10"),
        ],
        ids=["122-fits-in-127", "123-fits-in-128", "124-loses-one", "ext10-122-fits", "ext10-123-loses-one"],
    )
    def test_the_suffix_width_is_the_boundary(self, length: int, rank: int, expected: str) -> None:
        name, x = stub_name("L" * length, rank, set())

        assert (name, x) == (expected, rank)
        assert len(name) <= MAX_IDENTIFIER_LENGTH

    def test_a_leaf_over_the_limit_stays_uncapped(self) -> None:
        assert stub_name("L" * 129, 0, set()) == ("L" * 129 + "_ext0", 0)

    def test_two_leaves_sharing_a_shortened_stem_take_a_free_rank(self) -> None:
        """Two leaves sharing a shortened stem take a free rank.

        Two 125-character leaves alike in their first 123 shorten to one stem: the second takes ``_ext1``, the one case
        where X is a free rank and not the namesake index.
        """
        first, second = "S" * 123 + "AA", "S" * 123 + "BB"
        named = stub_names({"/P/one": second, "/P/two": first}, set())

        assert (named["/P/two"].name, named["/P/one"].name) == ("S" * 123 + "_ext0", "S" * 123 + "_ext1")
        assert (named["/P/two"].index, named["/P/one"].index) == (0, 0)

    def test_a_claimed_name_moves_x_past_it(self) -> None:
        named = stub_names({"/A/IFoo": "IFoo", "/B/IFoo": "IFoo"}, {"IFoo_ext0"})

        assert (named["/A/IFoo"].name, named["/B/IFoo"].name) == ("IFoo_ext1", "IFoo_ext2")

    def test_leaves_alike_but_for_case_are_two_names(self) -> None:
        named = stub_names({"/A/IFoo": "IFoo", "/B/Ifoo": "Ifoo"}, set())

        assert (named["/A/IFoo"].name, named["/B/Ifoo"].name) == ("IFoo_ext0", "Ifoo_ext0")

    @pytest.mark.parametrize(
        ("sr", "mode", "boundary"),
        [("/C/If/IShared", "/A/If/IShared", "/B/If/IShared"), ("/A/If/IShared", "/C/If/IShared", "/B/If/IShared")],
        ids=["mode-first", "sr-first"],
    )
    def test_interface_namesakes_by_path_not_by_file_order(self, tmp_path: Path, sr, mode, boundary) -> None:
        source = _variant(tmp_path, (_SR, sr), (_MODE, mode), (_BOUNDARY, boundary))

        model_root, report = _import(tmp_path, source)

        stubs = {ref: name for name, (ref, _) in _stub_refs(read_interfaces(model_root)).items()}
        assert {ref: name for ref, name in stubs.items() if ref.endswith("/IShared")} == {
            "/A/If/IShared": "IShared_ext0",
            "/B/If/IShared": "IShared_ext1",
            "/C/If/IShared": "IShared_ext2",
        }
        assert [line for line in report.rename_lines if line.startswith("IShared ->")] == [
            f"IShared -> IShared_ext{index} (imported stub, namesake {index} of 3 -- the stub of /{pkg}/If/IShared)"
            for index, pkg in enumerate("ABC")
        ]

    def test_type_namesakes_by_path(self, tmp_path: Path) -> None:
        source = _variant(
            tmp_path,
            ("/Remote/Types/RemoteValue", "/C/T/Shared"),
            ("/Remote/Types/RemoteArg", "/A/T/Shared"),
            ("/Remote/Types/RemoteBase", "/B/T/Shared"),
        )

        model_root, _ = _import(tmp_path, source)

        stubs = {ref: name for name, (ref, _) in _stub_refs(read_data_types(model_root)).items()}
        assert {ref: name for ref, name in stubs.items() if ref.endswith("/Shared")} == {
            "/A/T/Shared": "Shared_ext0",
            "/B/T/Shared": "Shared_ext1",
            "/C/T/Shared": "Shared_ext2",
        }

    def test_every_component_stub_has_one_rename_line(self, tmp_path: Path) -> None:
        """One rename line per component stub, naming the leaf, the stub and its path."""
        source = _variant(tmp_path, _far_prototypes("/C/Comp/Far", "/A/Comp/Far", "/B/Comp/Far"))

        model_root, report = _import(tmp_path, source)

        stubs = {swc.name: swc.external_ref for swc in read_swc_model(model_root) if swc.external}
        lines = [line for line in report.rename_lines if line.startswith("Far ->")]
        assert sorted(lines) == sorted(
            f"Far -> {name} (imported stub, namesake {int(name[-1])} of 3 -- the stub of {ref})"
            for name, ref in stubs.items()
        )
        assert len(stubs) == 3

    def test_component_namesakes_by_path_not_by_composition_order(self, tmp_path: Path) -> None:
        source = _variant(tmp_path, _far_prototypes("/C/Comp/Far", "/A/Comp/Far", "/B/Comp/Far"))

        model_root, _ = _import(tmp_path, source)

        assert {swc.external_ref: swc.name for swc in read_swc_model(model_root) if swc.external} == {
            "/A/Comp/Far": "Far_ext0",
            "/B/Comp/Far": "Far_ext1",
            "/C/Comp/Far": "Far_ext2",
        }
        members = {m.name: m.type for m in _by_name(read_compositions(model_root))["Top"].members}
        assert (members["far0"], members["far1"], members["far2"]) == ("Far_ext2", "Far_ext0", "Far_ext1")

    def test_a_refused_record_takes_no_rank(self, tmp_path: Path) -> None:
        """A refused record takes no rank: ``/B/If/IShared`` is ``_ext0``.

        ``/A/If/IShared`` sorts first but disagrees on ``DEST``, so it mints no stub.
        """
        source = _variant(
            tmp_path,
            (_MODE, "/A/If/IShared"),
            (_BOUNDARY, "/B/If/IShared"),
            _insert(
                _BEFORE_STATUS_PORT,
                "                <R-PORT-PROTOTYPE>\n"
                "                  <SHORT-NAME>rDisagree</SHORT-NAME>\n"
                '                  <REQUIRED-INTERFACE-TREF DEST="CLIENT-SERVER-INTERFACE">'
                "/A/If/IShared</REQUIRED-INTERFACE-TREF>\n"
                "                </R-PORT-PROTOTYPE>",
            ),
        )

        model_root, report = _import(tmp_path, source)

        assert "/A/If/IShared" in {entry.target for entry in report.dest_disagreements}
        stubs = {ref: name for name, (ref, _) in _stub_refs(read_interfaces(model_root)).items()}
        assert {ref: name for ref, name in stubs.items() if ref.endswith("/IShared")} == {
            "/B/If/IShared": "IShared_ext0"
        }
        assert "IShared -> IShared_ext0 (imported stub, namesake 0 of 1 -- the stub of /B/If/IShared)" in (
            report.rename_lines
        )

    def test_names_alike_but_for_case_get_the_file_suffix_rule(self, tmp_path: Path) -> None:
        source = _variant(tmp_path, (_SR, "/A/If/IFoo"), (_BOUNDARY, "/B/If/Ifoo"))

        model_root, _ = _import(tmp_path, source)

        names = set(_stub_refs(read_interfaces(model_root)))
        assert {"IFoo_ext0", "Ifoo_ext0"} <= names
        stems = sorted(path.stem for path in (Path(model_root) / "interfaces").glob("*.yaml"))
        assert sorted(stem for stem in stems if stem.casefold().startswith("ifoo_ext0")) in (
            ["IFoo_ext0", "Ifoo_ext0~1"],
            ["IFoo_ext0~1", "Ifoo_ext0"],
        )

    def test_a_leaf_of_124_is_fitted_and_its_path_kept_whole(self, tmp_path: Path) -> None:
        path = "/Remote/Long/" + "L" * 124
        source = _variant(tmp_path, (_BOUNDARY, path))

        model_root, _ = _import(tmp_path, source)

        stubs = _stub_refs(read_interfaces(model_root))
        assert stubs["L" * 123 + "_ext0"] == (path, "SENDER-RECEIVER-INTERFACE")

    def test_a_leaf_over_the_limit_mints_no_stub_as_before(self, tmp_path: Path) -> None:
        """An over-length leaf mints no stub: its path stays in the field.

        The segment fails ``validator.external_pair_problems`` before any name is given; the uncapped ``stub_name``
        branch is the second guard.
        """
        path = "/Remote/Long/" + "L" * 129
        source = _variant(tmp_path, (_BOUNDARY, path))

        model_root, report = _import(tmp_path, source)

        assert (path, CAUSE_ABSENT) in {(record.target, record.cause) for record in report.unresolved_references}
        assert path not in {ref for ref, _ in _stub_refs(read_interfaces(model_root)).values()}

    def test_a_hand_written_stub_without_the_suffix_loads_and_validates(self, tmp_path: Path) -> None:
        """A stub nothing imports is named by its author: the suffix is the importer's convention."""
        model = listed_stub_model_root(tmp_path / "model")

        stubs = [i.name for i in read_interfaces(str(model)) if isinstance(i, ExternalInterface)]
        assert stubs and not any("_ext" in name for name in stubs)
        assert check_cross_layer_consistency(model_root=str(model)).records == ()

    def test_a_reimport_of_swcrafts_own_output_mints_the_same_names(self, tmp_path: Path) -> None:
        """The fixed point, for a whole-file import (no drop)."""
        model_root, _ = _import(tmp_path, MEMBERS, name="first")
        output = tmp_path / "first.arxml"
        generate_arxml(model_root, str(output))

        again, _ = _import(tmp_path, output, name="again")

        def stubs(root: str) -> dict[str, str]:
            return {ref: name for name, (ref, _) in _stub_refs(read_interfaces(root)).items()}

        assert stubs(again) == stubs(model_root)
        assert all("_ext" in name for name in stubs(again).values())

    def test_a_selection_leaves_a_gap_a_reimport_closes(self, tmp_path: Path) -> None:
        """The stated limit: the rank is taken before ``--packages`` drops a namesake.

        The kept stub is written ``_ext1`` beside no ``_ext0``, and a re-import of that
        output numbers it ``_ext0``."""
        source = _variant(
            tmp_path,
            (
                "              <COMPONENTS>\n"
                "                <SW-COMPONENT-PROTOTYPE>\n"
                "                  <SHORT-NAME>ctl</SHORT-NAME>\n"
                '                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">'
                "/Acme/Components/Controller</TYPE-TREF>\n"
                "                </SW-COMPONENT-PROTOTYPE>\n"
                "              </COMPONENTS>\n",
                "",
            ),
        )
        model_root, _ = _import(tmp_path, source, name="selected", packages=["Compositions"])

        stubs = _stub_refs(read_interfaces(model_root))
        assert stubs["IRemote_ext1"][0] == "/Remote/PortInterfaces/IRemote"
        assert "IRemote_ext0" not in stubs
        output = tmp_path / "selected.arxml"
        generate_arxml(model_root, str(output))

        again, _ = _import(tmp_path, output, name="again")

        assert _stub_refs(read_interfaces(again))["IRemote_ext0"][0] == "/Remote/PortInterfaces/IRemote"

    def test_a_later_skip_of_the_sole_referrer_leaves_the_same_gap(self, tmp_path: Path) -> None:
        """A skip of the sole referrer after minting leaves the same gap: ``/Partner``'s ``IRemote_ext0`` goes with it.

        The referrer is ``Controller``, renamed ``CON``.
        """
        source = _variant(
            tmp_path,
            ("<SHORT-NAME>Controller</SHORT-NAME>", "<SHORT-NAME>CON</SHORT-NAME>"),
            ("/Acme/Components/Controller", "/Acme/Components/CON"),
        )

        model_root, _ = _import(tmp_path, source)

        stubs = _stub_refs(read_interfaces(model_root))
        assert stubs["IRemote_ext1"][0] == "/Remote/PortInterfaces/IRemote"
        assert "IRemote_ext0" not in stubs
