"""The rendered import report over the external stubs.

``test_external_stubs_import.py`` asserts the report's records; this module asserts what
``ImportReport.render()`` prints and what the records are narrowed to:

* the *External interface stubs* and *External type stubs* categories, each printed only
  when it counts something, one line per stub;
* the ``STUBBED`` summary line;
* the *Unresolved references* list, shrunk by exactly the stubbed references;
* one *DEST disagreement* line per path left without a stub;
* the narrowing of the referrers under ``--packages`` and the reserved device-name skip;
* ``--dry-run``, which prints all of it and writes nothing;
* no line naming where the project is stored.

What it does **not** cover: the real tool exports (``dev.py test-internal``), and the
CLI's wrapping, which prints :meth:`ImportReport.render` unchanged
(``test_arxml_import_cli.py``).
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.report import ImportReport
from swcraft_tools.arxml_import.unresolved import (
    DestDisagreement,
    ExternalStub,
    UnresolvedReferrer,
    disagreements_narrowed,
    owned_by,
    stubs_narrowed,
)
from swcraft_tools.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "arxml_external_stubs"
ROLES = FIXTURES / "stub_roles.arxml"
DISAGREEMENT = FIXTURES / "dest_disagreement.arxml"


def _import(tmp_path: Path, source: Path, name: str = "proj", **options) -> ImportReport:
    """Import *source* into a project scaffolded through the real ``swcraft init``."""
    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return run_import(str(source), str(target / "swcraft.yaml"), **options)


def _summary(rendered: str) -> list[str]:
    """The summary block: every line between the first blank line and the next."""
    lines = rendered.splitlines()
    start = lines.index("") + 1
    return lines[start : lines.index("", start)]


def _section(rendered: str, heading_prefix: str) -> list[str]:
    """The block whose heading starts with *heading_prefix*, heading first, up to the next blank line."""
    lines = rendered.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading_prefix))
    end = lines.index("", start) if "" in lines[start:] else len(lines)
    return lines[start:end]


@pytest.fixture
def roles(tmp_path: Path) -> ImportReport:
    return _import(tmp_path, ROLES)


@pytest.fixture
def disagreeing(tmp_path: Path) -> ImportReport:
    return _import(tmp_path, DISAGREEMENT)


class TestTheTwoCategories:
    def test_the_interface_stubs_one_line_each(self, roles: ImportReport) -> None:
        section = _section(roles.render(), "External interface stubs")

        assert section[0].startswith("External interface stubs (5; ")
        assert section[1:] == [
            "  - /Partner/PortInterfaces/IRemote (DEST CLIENT-SERVER-INTERFACE) -- imported as external stub "
            "'IRemote_ext0'",
            "  - /Remote/ModeInterfaces/IRemoteMode (DEST MODE-SWITCH-INTERFACE) -- imported as external stub "
            "'IRemoteMode_ext0'",
            "  - /Remote/PortInterfaces/IBoundaryOnly (DEST SENDER-RECEIVER-INTERFACE) -- imported as external stub "
            "'IBoundaryOnly_ext0'",
            "  - /Remote/PortInterfaces/IRemote (DEST CLIENT-SERVER-INTERFACE) -- imported as external stub "
            "'IRemote_ext1'",
            "  - /Remote/PortInterfaces/IRemoteSr (DEST SENDER-RECEIVER-INTERFACE) -- imported as external stub "
            "'IRemoteSr_ext0'",
        ]

    def test_the_type_stubs_one_line_each(self, roles: ImportReport) -> None:
        section = _section(roles.render(), "External type stubs")

        assert section[0].startswith("External type stubs (4; ")
        assert section[1:] == [
            "  - /Remote/Types/Clash (DEST IMPLEMENTATION-DATA-TYPE) -- imported as external stub 'Clash_ext0'",
            "  - /Remote/Types/RemoteArg (DEST IMPLEMENTATION-DATA-TYPE) -- imported as external stub 'RemoteArg_ext0'",
            "  - /Remote/Types/RemoteBase (DEST IMPLEMENTATION-DATA-TYPE) -- imported as external stub "
            "'RemoteBase_ext0'",
            "  - /Remote/Types/RemoteValue (DEST IMPLEMENTATION-DATA-TYPE) -- imported as external stub "
            "'RemoteValue_ext0'",
        ]

    def test_a_category_that_counts_nothing_is_not_printed(self, roles: ImportReport) -> None:
        interfaces_only = dataclasses.replace(roles, external_type_stubs=[]).render()
        types_only = dataclasses.replace(roles, external_interface_stubs=[]).render()

        assert "External interface stubs" in interfaces_only
        assert "External type stubs" not in interfaces_only
        assert "External type stubs" in types_only
        assert "External interface stubs" not in types_only

    def test_a_report_with_no_stub_prints_neither_category_nor_the_summary_line(self, roles: ImportReport) -> None:
        rendered = dataclasses.replace(
            roles, external_interface_stubs=[], external_type_stubs=[], dest_disagreements=[]
        ).render()

        for absent in ("External interface stubs", "External type stubs", "STUBBED", "DEST disagreements"):
            assert absent not in rendered

    def test_the_component_stub_category_is_a_separate_one(self, roles: ImportReport) -> None:
        """An interface or type stub is not a component stub, and neither category borrows the other's heading."""
        assert "External component stubs" not in roles.render()


class TestTheSummaryLine:
    def test_it_counts_both_layers(self, roles: ImportReport) -> None:
        assert "STUBBED 5 interface(s), 4 type(s)" in _summary(roles.render())

    def test_it_is_printed_when_only_one_layer_counts(self, roles: ImportReport) -> None:
        rendered = dataclasses.replace(roles, external_type_stubs=[]).render()

        assert "STUBBED 5 interface(s), 0 type(s)" in _summary(rendered)

    def test_it_stands_before_the_unresolved_count(self, roles: ImportReport) -> None:
        summary = _summary(roles.render())

        assert summary.index("STUBBED 5 interface(s), 4 type(s)") < summary.index(
            "UNRESOLVED 1 reference(s) to 1 target(s)"
        )


class TestTheUnresolvedListShrinks:
    def test_by_exactly_the_stubbed_references(self, roles: ImportReport) -> None:
        rendered = roles.render()
        section = _section(rendered, "Unresolved references")

        assert "UNRESOLVED 1 reference(s) to 1 target(s)" in _summary(rendered)
        assert section[0].startswith("Unresolved references (1 reference(s) to 1 target(s);")
        assert section[1].startswith("  - /Remote/AppTypes/RemotePhys (DEST APPLICATION-PRIMITIVE-DATA-TYPE)")
        stubbed = [stub.target for stub in (*roles.external_interface_stubs, *roles.external_type_stubs)]
        assert not any(target in line for target in stubbed for line in section)


class TestTheDisagreementLines:
    def test_one_line_per_path_naming_each_dest(self, disagreeing: ImportReport) -> None:
        section = _section(disagreeing.render(), "DEST disagreements")

        assert section[0].startswith("DEST disagreements (4; ")
        assert section[1:] == [
            "  - /Remote/If/IAcross referenced with DEST CLIENT-SERVER-INTERFACE, SENDER-RECEIVER-INTERFACE "
            "-- not imported as an external stub",
            "  - /Remote/If/IFar referenced with DEST CLIENT-SERVER-INTERFACE, SENDER-RECEIVER-INTERFACE "
            "-- not imported as an external stub",
            "  - /Remote/If/IWithin referenced with DEST CLIENT-SERVER-INTERFACE, MODE-SWITCH-INTERFACE "
            "-- not imported as an external stub",
            "  - /Remote/Types/TWithin referenced with DEST IMPLEMENTATION-DATA-TYPE, APPLICATION-PRIMITIVE-DATA-TYPE "
            "-- not imported as an external stub",
        ]

    def test_the_disagreeing_references_stay_unresolved_and_come_after(self, disagreeing: ImportReport) -> None:
        lines = disagreeing.render().splitlines()
        heading = next(index for index, line in enumerate(lines) if line.startswith("DEST disagreements"))
        unresolved = next(index for index, line in enumerate(lines) if line.startswith("Unresolved references"))

        assert heading < unresolved
        assert "UNRESOLVED 9 reference(s) to 4 target(s)" in _summary(disagreeing.render())

    def test_the_agreeing_path_beside_them_is_a_stub_line(self, disagreeing: ImportReport) -> None:
        assert _section(disagreeing.render(), "External interface stubs")[1:] == [
            "  - /Remote/If/IAgreed (DEST CLIENT-SERVER-INTERFACE) -- imported as external stub 'IAgreed_ext0'"
        ]
        assert "STUBBED 1 interface(s), 0 type(s)" in _summary(disagreeing.render())


class TestASelectionNarrows:
    """``--packages``: every stub and disagreement line is about a file the import writes."""

    def test_a_stub_no_kept_element_references_is_not_listed(self, tmp_path: Path) -> None:
        rendered = _import(tmp_path, ROLES, packages=["Components"]).render()

        assert "IBoundaryOnly" not in rendered
        assert "STUBBED 4 interface(s), 4 type(s)" in _summary(rendered)

    def test_a_kept_stubs_referrers_are_the_kept_elements_only(self, tmp_path: Path) -> None:
        """``IRemote_ext1``'s record keeps the component port, not ``Top``, which the selection does not write."""
        whole = {stub.name: stub for stub in _import(tmp_path, ROLES, name="whole").external_interface_stubs}
        narrowed = {
            stub.name: stub
            for stub in _import(tmp_path, ROLES, name="narrowed", packages=["Components"]).external_interface_stubs
        }

        assert [referrer.path for referrer in whole["IRemote_ext1"].referrers] == [
            "/Acme/Components/Controller/rCs",
            "/Acme/Compositions/Top/rOuterCs",
        ]
        assert [referrer.path for referrer in narrowed["IRemote_ext1"].referrers] == ["/Acme/Components/Controller/rCs"]

    def test_a_disagreement_no_kept_element_makes_is_not_listed(self, tmp_path: Path) -> None:
        report = _import(tmp_path, DISAGREEMENT, packages=["Components"])

        assert [entry.target for entry in report.dest_disagreements] == [
            "/Remote/If/IAcross",
            "/Remote/If/IWithin",
            "/Remote/Types/TWithin",
        ]
        assert "IFar" not in report.render()

    def test_a_kept_disagreement_is_named_whole(self, tmp_path: Path) -> None:
        """A kept disagreement's line keeps every ``DEST`` of the whole export; dropped referrers leave the record."""
        report = _import(tmp_path, DISAGREEMENT, packages=["Components"])

        across = {entry.target: entry for entry in report.dest_disagreements}["/Remote/If/IAcross"]
        assert across.dests == ("CLIENT-SERVER-INTERFACE", "SENDER-RECEIVER-INTERFACE")
        assert [referrer.path for referrer in across.referrers] == [
            "/Acme/Components/Left/rAcross",
            "/Acme/Components/Right/rAcross",
        ]


def _referrer(path: str, dest: str = "CLIENT-SERVER-INTERFACE") -> UnresolvedReferrer:
    return UnresolvedReferrer(
        path=path,
        tag="R-PORT-PROTOTYPE",
        attribute="REQUIRED-INTERFACE-TREF",
        field="swc.port.interface",
        dest=dest,
    )


class TestTheNarrowingFunctions:
    """The two functions both narrowings call.

    The selection's (kept paths) and the reserved device-name skip's (dropped paths, through the complement).
    """

    STUB = ExternalStub(
        layer="interfaces",
        name="IRemote",
        target="/Remote/IRemote",
        dest="CLIENT-SERVER-INTERFACE",
        referrers=(_referrer("/Acme/A/rOne"), _referrer("/Acme/B/rTwo")),
    )
    DISAGREEMENT = DestDisagreement(
        target="/Remote/IMixed",
        referrers=(_referrer("/Acme/A/rOne"), _referrer("/Acme/B/rTwo", "SENDER-RECEIVER-INTERFACE")),
        dests=("CLIENT-SERVER-INTERFACE", "SENDER-RECEIVER-INTERFACE"),
    )

    def test_a_stub_keeps_the_referrers_whose_owner_is_kept(self) -> None:
        (narrowed,) = stubs_narrowed([self.STUB], owned_by(frozenset({"/Acme/A"})))

        assert [referrer.path for referrer in narrowed.referrers] == ["/Acme/A/rOne"]
        assert (narrowed.name, narrowed.target, narrowed.dest) == (self.STUB.name, self.STUB.target, self.STUB.dest)

    def test_a_stub_loses_the_referrers_whose_owner_is_dropped(self) -> None:
        dropped = owned_by(frozenset({"/Acme/A"}))
        (narrowed,) = stubs_narrowed([self.STUB], lambda path: not dropped(path))

        assert [referrer.path for referrer in narrowed.referrers] == ["/Acme/B/rTwo"]

    def test_a_disagreement_is_kept_whole_while_any_referrer_is(self) -> None:
        (kept,) = disagreements_narrowed([self.DISAGREEMENT], owned_by(frozenset({"/Acme/A"})))

        assert [referrer.path for referrer in kept.referrers] == ["/Acme/A/rOne"]
        assert kept.target == self.DISAGREEMENT.target

    def test_a_disagreement_with_no_kept_referrer_is_gone(self) -> None:
        assert disagreements_narrowed([self.DISAGREEMENT], owned_by(frozenset({"/Acme/C"}))) == []


class TestADryRun:
    def test_it_prints_every_stub_line_and_writes_nothing(self, tmp_path: Path) -> None:
        written = _import(tmp_path, ROLES, name="written").render()
        preview = _import(tmp_path, ROLES, name="preview", dry_run=True).render()

        assert "(DRY RUN -- no files were written)" in preview
        for heading in ("External interface stubs", "External type stubs", "Unresolved references"):
            assert _section(preview, heading) == _section(written, heading)
        assert "STUBBED 5 interface(s), 4 type(s)" in _summary(preview)
        assert list((tmp_path / "preview" / "model" / "interfaces").glob("*.yaml")) == []

    def test_it_prints_the_disagreement_lines(self, tmp_path: Path) -> None:
        written = _import(tmp_path, DISAGREEMENT, name="written").render()
        preview = _import(tmp_path, DISAGREEMENT, name="preview", dry_run=True).render()

        assert _section(preview, "DEST disagreements") == _section(written, "DEST disagreements")


class TestNoLineNamesAStoragePath:
    @pytest.mark.parametrize("source", [ROLES, DISAGREEMENT], ids=["roles", "disagreement"])
    def test_the_rendered_report(self, tmp_path: Path, source: Path) -> None:
        rendered = _import(tmp_path, source).render(verbose=True)

        assert tmp_path.as_posix() not in rendered
        assert str(tmp_path) not in rendered
        assert str(tmp_path.resolve()) not in rendered
