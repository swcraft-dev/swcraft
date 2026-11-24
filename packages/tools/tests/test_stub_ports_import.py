"""Contract ``7.6.0``: the importer mints an external component stub's ports, and keeps their connectors.

A prototype whose type the export does not map becomes a component stub. The importer mints the
stub's ports from the connector ends that land on it -- the name is the end's last path segment,
the direction the end's own ``DEST`` checked against the element that holds it -- keeps the
connectors, and writes a one-hop ``observed_interface`` hint computed last, over the final names.
The classes pin:

* **the hint** -- one neighbour, equal neighbours, ambiguous neighbours (no hint, candidates
  reported), both ends on stubs, a delegation's inner end, a hint through an interface stub and
  one through a reserved-name rename;
* **the direction** -- contradicting occurrences mint nothing and are reported, and a ``DEST``
  that is absent, unsupported or contradicts its tag or wrapper mints nothing from that end;
* **ownership** -- an end whose target port path is not owned by the context prototype's
  ``TYPE-TREF`` path (``[TPS_GST_00162]``) mints no port and drops its connector, reported under
  its own reason; the same last segment under another package is refused the same way;
* **conflicting declarations** -- two declarations that differ only in an end's ``DEST`` or a
  delegation's wrapper conflict;
* **``--packages``** -- a retained stub keeps the ports a kept connector reaches, and loses the
  one whose last connection fell;
* **the report** -- the stub-port lines, and no *declares no port* category;
* **the combined identifier test** -- one non-Identifier stub-port segment through import
  (reported, its connector dropped), editing, ``check-model`` and generation (each refusing it).

Every export here is synthetic, written by the builder below with invented names. What this does
**not** reach: real tool exports, the diagrams, and any shape the builder does not write -- it is
example-based evidence, not a proof over every export.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.connectors import UNOWNED_TARGET_PORT
from swcraft_tools.arxml_import.merge import CONFLICTING, MERGED
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_import.report import ImportReport
from swcraft_tools.arxml_reader import SkipCategory, StandardRule, read_arxml
from swcraft_tools.cli import main
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.project import require_project
from swcraft_tools.reader import read_compositions, read_connectors, read_interfaces, read_swc_model
from swcraft_tools.schema import ExternalInterface
from swcraft_tools.validator import check_cross_layer_consistency, check_identifier_conformance
from swcraft_tools.writer import generate_arxml

EXT = "/Vendor/Ext/ExtSwc"
EXT2 = "/Vendor/Ext/OtherSwc"
ROOT = "/Acme/Compositions/Root"
NS = "{http://autosar.org/schema/r4.0}"

# ---------------------------------------------------------------------------
# The builder: a small export with three declared components and two absent types
# ---------------------------------------------------------------------------

_TYPES = """
        <AR-PACKAGE>
          <SHORT-NAME>Types</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>uint8</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Sig_impl</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Types/uint8</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>"""


def _sr_interface(name: str, element: str = "val") -> str:
    return f"""
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>{name}</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>{element}</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Types/Sig_impl</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>"""


def _port(name: str, direction: str, interface: str) -> str:
    if direction == "provide":
        return f"""
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>{name}</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">{interface}</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>"""
    return f"""
                <R-PORT-PROTOTYPE>
                  <SHORT-NAME>{name}</SHORT-NAME>
                  <REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">{interface}</REQUIRED-INTERFACE-TREF>
                </R-PORT-PROTOTYPE>"""


def _swc(name: str, *ports: str) -> str:
    """A declared component whose behaviour states NO-SUPPORT (a component without a termination value is refused)."""
    return f"""
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>{name}</SHORT-NAME>
              <PORTS>{"".join(ports)}
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>{name}_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>"""


#: The three declared components every export here carries. ``SwcC``'s port names
#: an interface the export declares nowhere (an interface stub), and ``SwcD``'s
#: one the reserved-name pass renames (``ISig_val_Init`` is the constant ``ISig``'s
#: element ``val`` derives).
_COMPONENTS = (
    _swc(
        "SwcA",
        _port("pSig", "provide", "/Acme/Interfaces/ISig"),
        _port("rSig", "require", "/Acme/Interfaces/ISig"),
        _port("pOther", "provide", "/Acme/Interfaces/IOther"),
    )
    + _swc(
        "SwcB",
        _port("pSig", "provide", "/Acme/Interfaces/ISig"),
        _port("rSig", "require", "/Acme/Interfaces/ISig"),
        _port("pOther", "provide", "/Acme/Interfaces/IOther"),
    )
    + _swc(
        "SwcC",
        _port("pGhost", "provide", "/Nowhere/Interfaces/IGhost"),
        _port("pInit", "provide", "/Acme/Interfaces/ISig_val_Init"),
    )
)


def end(context: str, target: str, dest: str | None, *, tag: str) -> str:
    """One ``CONTEXT-COMPONENT-REF`` + target pair; ``dest=None`` leaves the attribute out."""
    attribute = "" if dest is None else f' DEST="{dest}"'
    return f"""
                    <CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">{ROOT}/{context}</CONTEXT-COMPONENT-REF>
                    <{tag}{attribute}>{target}</{tag}>"""


def assembly(
    name: str,
    provider: tuple[str, str],
    requester: tuple[str, str],
    *,
    provider_dest: str | None = "P-PORT-PROTOTYPE",
    requester_dest: str | None = "R-PORT-PROTOTYPE",
) -> str:
    """An ``ASSEMBLY-SW-CONNECTOR`` inside ``Root``; each end is ``(prototype, target port path)``."""
    return f"""
                <ASSEMBLY-SW-CONNECTOR>
                  <SHORT-NAME>{name}</SHORT-NAME>
                  <PROVIDER-IREF>{end(*provider, provider_dest, tag="TARGET-P-PORT-REF")}
                  </PROVIDER-IREF>
                  <REQUESTER-IREF>{end(*requester, requester_dest, tag="TARGET-R-PORT-REF")}
                  </REQUESTER-IREF>
                </ASSEMBLY-SW-CONNECTOR>"""


def delegation(name: str, wrapper: str, inner: tuple[str, str], outer: str, *, dest: str | None = None) -> str:
    """A ``DELEGATION-SW-CONNECTOR`` inside ``Root``; ``wrapper`` is ``P`` or ``R``."""
    tag = f"TARGET-{wrapper}-PORT-REF"
    dest = dest if dest is not None else f"{wrapper}-PORT-PROTOTYPE"
    outer_dest = f"{wrapper}-PORT-PROTOTYPE"
    return f"""
                <DELEGATION-SW-CONNECTOR>
                  <SHORT-NAME>{name}</SHORT-NAME>
                  <INNER-PORT-IREF>
                    <{wrapper}-PORT-IN-COMPOSITION-INSTANCE-REF>{end(*inner, dest, tag=tag)}
                    </{wrapper}-PORT-IN-COMPOSITION-INSTANCE-REF>
                  </INNER-PORT-IREF>
                  <OUTER-PORT-REF DEST="{outer_dest}">{ROOT}/{outer}</OUTER-PORT-REF>
                </DELEGATION-SW-CONNECTOR>"""


def prototype(name: str, type_ref: str, dest: str = "APPLICATION-SW-COMPONENT-TYPE") -> str:
    return f"""
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>{name}</SHORT-NAME>
                  <TYPE-TREF DEST="{dest}">{type_ref}</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>"""


#: ``Root``'s members in every export: the three declared components and the two absent types.
PROTOTYPES = (
    prototype("A", "/Acme/Components/SwcA")
    + prototype("B", "/Acme/Components/SwcB")
    + prototype("C", "/Acme/Components/SwcC")
    + prototype("Ext", EXT)
    + prototype("Ext2", EXT2)
)


def composition(name: str, connectors: str, *, ports: str = "", prototypes: str = PROTOTYPES) -> str:
    return f"""
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>{name}</SHORT-NAME>
              <PORTS>{ports}
              </PORTS>
              <COMPONENTS>{prototypes}
              </COMPONENTS>
              <CONNECTORS>{connectors}
              </CONNECTORS>
            </COMPOSITION-SW-COMPONENT-TYPE>"""


_AUTOSAR_ROOT = (
    '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">'
)


def export_text(connectors: str, *, ports: str = "", extra_packages: str = "") -> str:
    interfaces = _sr_interface("ISig") + _sr_interface("IOther") + _sr_interface("ISig_val_Init", "x")
    return f"""<?xml version="1.0" encoding="utf-8"?>
{_AUTOSAR_ROOT}
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>{_TYPES}
        <AR-PACKAGE>
          <SHORT-NAME>Interfaces</SHORT-NAME>
          <ELEMENTS>{interfaces}
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Components</SHORT-NAME>
          <ELEMENTS>{_COMPONENTS}
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Compositions</SHORT-NAME>
          <ELEMENTS>{composition("Root", connectors, ports=ports)}
          </ELEMENTS>
        </AR-PACKAGE>{extra_packages}
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


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


class Imported:
    """One import of one synthetic export: the report and the written model root."""

    def __init__(self, tmp_path: Path, text: str, name: str = "proj", **options: Any) -> None:
        source = tmp_path / f"{name}.arxml"
        source.write_text(text, encoding="utf-8", newline="\n")
        self.source = source
        self.project = _init_project(tmp_path, name)
        self.report: ImportReport = run_import(str(source), str(self.project / "swcraft.yaml"), **options)
        self.model_root = str(require_project(str(self.project / "swcraft.yaml")).resolved_model_root)

    def swcs(self) -> dict[str, Any]:
        return {swc.name: swc for swc in read_swc_model(self.model_root)}

    def stub_ports(self, stub: str = "ExtSwc_ext0") -> dict[str, Any]:
        return {port.name: port for port in self.swcs()[stub].ports}

    def connector_names(self) -> set[str]:
        return {connector.name for connector in read_connectors(self.model_root)}

    def delegation_names(self) -> set[str]:
        return {d.name for comp in read_compositions(self.model_root) for d in comp.delegations}

    def reason(self, name: str) -> str:
        (reason,) = [entry.reason for entry in self.report.skip_entries if entry.path.endswith(f"/{name}")]
        return reason

    def findings(self) -> list[Any]:
        return list(check_cross_layer_consistency(model_root=self.model_root).records)


def _pout_to_ext(name: str = "A_to_Ext", source: str = "A", port: str = "pSig", target: str = "rIn") -> str:
    return assembly(name, (source, f"/Acme/Components/Swc{source}/{port}"), ("Ext", f"{EXT}/{target}"))


# ---------------------------------------------------------------------------
# The hint
# ---------------------------------------------------------------------------


class TestTheSingleHopHint:
    def test_one_neighbour_names_the_hint_and_the_connector_is_kept(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext()))

        port = imported.stub_ports()["rIn"]
        assert (port.direction, port.interface, port.observed_interface) == ("require", None, "ISig")
        assert "A_to_Ext" in imported.connector_names()
        assert imported.findings() == []

    def test_equal_neighbours_name_the_hint(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext() + _pout_to_ext("B_to_Ext", source="B")))

        assert imported.stub_ports()["rIn"].observed_interface == "ISig"
        assert {"A_to_Ext", "B_to_Ext"} <= imported.connector_names()
        assert imported.report.stub_port_ambiguity_lines == []

    def test_ambiguous_neighbours_leave_no_hint_and_report_the_candidates(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext() + _pout_to_ext("B_to_Ext", source="B", port="pOther")))

        port = imported.stub_ports()["rIn"]
        assert (port.interface, port.observed_interface) == (None, None)
        assert {"A_to_Ext", "B_to_Ext"} <= imported.connector_names(), "both connectors are kept"
        assert imported.report.stub_port_ambiguity_lines == ["ExtSwc_ext0.rIn -- candidates: IOther, ISig"]
        assert "Stub ports with no observed interface (1;" in imported.report.render()

    def test_both_ends_on_stubs_mint_both_ports_with_no_hint(self, tmp_path: Path) -> None:
        imported = Imported(
            tmp_path, export_text(assembly("Ext_to_Ext2", ("Ext", f"{EXT}/pOut"), ("Ext2", f"{EXT2}/rIn")))
        )

        assert "Ext_to_Ext2" in imported.connector_names()
        provide = imported.stub_ports("ExtSwc_ext0")["pOut"]
        require = imported.stub_ports("OtherSwc_ext0")["rIn"]
        assert (provide.direction, provide.observed_interface) == ("provide", None)
        assert (require.direction, require.observed_interface) == ("require", None)
        assert imported.findings() == []

    def test_a_delegations_inner_end_on_a_stub_mints_the_port_and_the_boundary_port_is_the_hint(
        self, tmp_path: Path
    ) -> None:
        imported = Imported(
            tmp_path,
            export_text(
                delegation("DEL_Ext", "R", ("Ext", f"{EXT}/rOuter"), "rOuter"),
                ports=_port("rOuter", "require", "/Acme/Interfaces/IOther"),
            ),
        )

        assert "DEL_Ext" in imported.delegation_names()
        port = imported.stub_ports()["rOuter"]
        assert (port.direction, port.observed_interface) == ("require", "IOther")
        assert imported.findings() == []

    def test_a_hint_through_an_external_interface_stub_names_the_stub(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext("C_to_Ext", source="C", port="pGhost", target="rG")))

        stubs = {i.name for i in read_interfaces(imported.model_root) if isinstance(i, ExternalInterface)}
        assert "IGhost_ext0" in stubs
        assert imported.stub_ports()["rG"].observed_interface == "IGhost_ext0"

    def test_a_hint_through_a_reserved_name_rename_names_the_renamed_interface(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext("C_to_Ext", source="C", port="pInit", target="rI")))

        renamed = {port.name: port.interface for port in imported.swcs()["SwcC"].ports}["pInit"]
        assert renamed != "ISig_val_Init", "the reserved-name pass renames the interface"
        assert imported.stub_ports()["rI"].observed_interface == renamed

    def test_a_port_is_written_only_on_the_stub_and_no_declared_port_carries_a_hint(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext()))

        declared = [port for swc in imported.swcs().values() if not swc.external for port in swc.ports]
        assert declared and all(port.observed_interface is None for port in declared)


# ---------------------------------------------------------------------------
# The direction
# ---------------------------------------------------------------------------


class TestTheDirection:
    def test_occurrences_disagreeing_on_the_direction_mint_no_port_and_are_reported(self, tmp_path: Path) -> None:
        text = export_text(
            _pout_to_ext("A_to_Ext", target="xPort")
            + assembly("Ext_to_B", ("Ext", f"{EXT}/xPort"), ("B", "/Acme/Components/SwcB/rSig"))
        )
        imported = Imported(tmp_path, text)

        assert "xPort" not in imported.stub_ports()
        assert not {"A_to_Ext", "Ext_to_B"} & imported.connector_names()
        for name in ("A_to_Ext", "Ext_to_B"):
            assert "contradicting directions" in imported.reason(name)
        assert imported.report.stub_port_contradiction_lines == [
            f"{EXT}/xPort -- provide at 1 end(s), require at 1 end(s)"
        ]
        assert "Stub ports not imported (1;" in imported.report.render()

    def test_a_connector_whose_other_end_does_not_resolve_states_no_direction(self, tmp_path: Path) -> None:
        """The census counts an end only when every other reference of its connector resolves.

        ``Ext_to_B``'s ``B`` end names a port ``SwcB`` lacks, so it may not strike the ``require``
        port the sound ``A_to_Ext`` needs.
        """
        text = export_text(
            _pout_to_ext() + assembly("Ext_to_B", ("Ext", f"{EXT}/rIn"), ("B", "/Acme/Components/SwcB/rNope"))
        )
        imported = Imported(tmp_path, text)

        assert imported.stub_ports()["rIn"].direction == "require"
        assert "A_to_Ext" in imported.connector_names()
        assert "Ext_to_B" not in imported.connector_names()
        assert imported.report.stub_port_contradiction_lines == []

    def test_a_delegation_to_a_boundary_port_not_kept_states_no_direction(self, tmp_path: Path) -> None:
        """A delegation whose outer reference names no boundary port of the composition states nothing."""
        text = export_text(_pout_to_ext() + delegation("DEL_Ext", "P", ("Ext", f"{EXT}/rIn"), "pMissing"))
        imported = Imported(tmp_path, text)

        assert imported.stub_ports()["rIn"].direction == "require"
        assert "A_to_Ext" in imported.connector_names()
        assert "DEL_Ext" not in imported.delegation_names()
        assert imported.report.stub_port_contradiction_lines == []

    def test_an_absent_dest_mints_nothing(self, tmp_path: Path) -> None:
        text = export_text(
            assembly("A_to_Ext", ("A", "/Acme/Components/SwcA/pSig"), ("Ext", f"{EXT}/rIn"), requester_dest=None)
        )
        imported = Imported(tmp_path, text)

        assert imported.stub_ports() == {}
        assert "no DEST attribute" in imported.reason("A_to_Ext")

    def test_an_unsupported_dest_mints_nothing(self, tmp_path: Path) -> None:
        text = export_text(
            assembly(
                "A_to_Ext",
                ("A", "/Acme/Components/SwcA/pSig"),
                ("Ext", f"{EXT}/rIn"),
                requester_dest="PR-PORT-PROTOTYPE",
            )
        )
        imported = Imported(tmp_path, text)

        assert imported.stub_ports() == {}
        assert "not a P- or R-PORT-PROTOTYPE" in imported.reason("A_to_Ext")

    def test_a_dest_contradicting_its_tag_mints_nothing(self, tmp_path: Path) -> None:
        text = export_text(
            assembly(
                "A_to_Ext",
                ("A", "/Acme/Components/SwcA/pSig"),
                ("Ext", f"{EXT}/rIn"),
                requester_dest="P-PORT-PROTOTYPE",
            )
        )
        imported = Imported(tmp_path, text)

        assert imported.stub_ports() == {}
        assert "states a require port" in imported.reason("A_to_Ext")

    def test_a_dest_contradicting_its_wrapper_mints_nothing(self, tmp_path: Path) -> None:
        text = export_text(
            delegation("DEL_Ext", "R", ("Ext", f"{EXT}/rOuter"), "rOuter", dest="P-PORT-PROTOTYPE"),
            ports=_port("rOuter", "require", "/Acme/Interfaces/IOther"),
        )
        imported = Imported(tmp_path, text)

        assert imported.stub_ports() == {}
        assert "DEL_Ext" not in imported.delegation_names()
        assert "states a require port" in imported.reason("DEL_Ext")

    def test_the_writer_re_emits_the_end_with_the_sources_reference_and_dest(self, tmp_path: Path) -> None:
        imported = Imported(
            tmp_path,
            export_text(
                _pout_to_ext() + delegation("DEL_Ext", "R", ("Ext", f"{EXT}/rOuter"), "rOuter"),
                ports=_port("rOuter", "require", "/Acme/Interfaces/IOther"),
            ),
        )

        out = tmp_path / "out"
        generate_arxml(imported.model_root, str(out / "model.arxml"))
        root = ET.parse(out / "model.arxml").getroot()
        targets = {
            (element.tag.removeprefix(NS), element.get("DEST"), element.text)
            for element in root.iter()
            if element.tag.removeprefix(NS) in ("TARGET-P-PORT-REF", "TARGET-R-PORT-REF")
            and (element.text or "").startswith(EXT)
        }
        assert targets == {
            ("TARGET-R-PORT-REF", "R-PORT-PROTOTYPE", f"{EXT}/rIn"),
            ("TARGET-R-PORT-REF", "R-PORT-PROTOTYPE", f"{EXT}/rOuter"),
        }


# ---------------------------------------------------------------------------
# An end the context's type does not own
# ---------------------------------------------------------------------------


class TestAnEndTheContextTypeDoesNotOwn:
    """An end on a stub whose target port path the context's type does not own is unresolved."""

    @pytest.mark.parametrize(
        ("connector", "name"),
        [
            pytest.param(
                assembly("Ext_to_B", ("Ext", "/Vendor/Other/ExtSwc/pOut"), ("B", "/Acme/Components/SwcB/rSig")),
                "Ext_to_B",
                id="assembly-provider-same-last-segment-another-package",
            ),
            pytest.param(
                assembly("A_to_Ext", ("A", "/Acme/Components/SwcA/pSig"), ("Ext", "/Vendor/Ext/ThirdSwc/rIn")),
                "A_to_Ext",
                id="assembly-requester-another-type",
            ),
        ],
    )
    def test_an_assembly_end_is_refused_and_its_connector_dropped(
        self, tmp_path: Path, connector: str, name: str
    ) -> None:
        imported = Imported(tmp_path, export_text(connector))

        assert name not in imported.connector_names()
        assert imported.stub_ports() == {}, "no port is minted from an unowned end"
        assert UNOWNED_TARGET_PORT in imported.reason(name)
        assert "constr_2626" not in imported.reason(name), "the report prints the citation, the reason none"
        assert imported.report.unowned_stub_port_paths == [f"{ROOT}/{name}"]

    def test_an_unowned_end_is_reported_before_the_other_ends_own_reason(self, tmp_path: Path) -> None:
        """The provider end fails on its own (a port ``SwcA`` lacks); the requester's ownership check still decides."""
        imported = Imported(
            tmp_path,
            export_text(
                assembly("A_to_Ext", ("A", "/Acme/Components/SwcA/pNope"), ("Ext", "/Vendor/Ext/ThirdSwc/rIn"))
            ),
        )

        assert imported.reason("A_to_Ext").startswith(
            f"requester end: unresolved reference /Vendor/Ext/ThirdSwc/rIn ({UNOWNED_TARGET_PORT}"
        )
        assert imported.report.unowned_stub_port_paths == [f"{ROOT}/A_to_Ext"]

    def test_a_target_path_naming_no_port_mints_nothing(self, tmp_path: Path) -> None:
        imported = Imported(
            tmp_path, export_text(assembly("A_to_Ext", ("A", "/Acme/Components/SwcA/pSig"), ("Ext", f"{EXT}/")))
        )

        assert imported.stub_ports() == {}
        assert "A_to_Ext" not in imported.connector_names()
        assert "(the target port path names no port)" in imported.reason("A_to_Ext")
        assert imported.report.unowned_stub_port_paths == []

    def test_a_delegations_inner_end_is_refused_the_same_way(self, tmp_path: Path) -> None:
        imported = Imported(
            tmp_path,
            export_text(
                delegation("DEL_Ext", "R", ("Ext", "/Elsewhere/Ext/ExtSwc/rOuter"), "rOuter"),
                ports=_port("rOuter", "require", "/Acme/Interfaces/IOther"),
            ),
        )

        assert "DEL_Ext" not in imported.delegation_names()
        assert imported.stub_ports() == {}
        assert UNOWNED_TARGET_PORT in imported.reason("DEL_Ext")
        assert imported.report.unowned_stub_port_paths == [f"{ROOT}/DEL_Ext"]
        (entry,) = [entry for entry in imported.report.skip_entries if entry.path == f"{ROOT}/DEL_Ext"]
        assert (entry.category, entry.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.INSTANCE_REF_PATH)

    def test_a_port_is_only_ever_minted_from_a_consistent_end(self, tmp_path: Path) -> None:
        text = export_text(
            _pout_to_ext()
            + assembly("B_to_Ext", ("B", "/Acme/Components/SwcB/pOther"), ("Ext", "/Vendor/Other/ExtSwc/rIn"))
        )
        imported = Imported(tmp_path, text)

        port = imported.stub_ports()["rIn"]
        assert port.observed_interface == "ISig", "the refused end's neighbour is no candidate"
        assert "A_to_Ext" in imported.connector_names()
        assert "B_to_Ext" not in imported.connector_names()
        assert imported.report.stub_port_ambiguity_lines == []

    def test_the_report_names_the_category_and_the_standard(self, tmp_path: Path) -> None:
        imported = Imported(
            tmp_path,
            export_text(assembly("A_to_Ext", ("A", "/Acme/Components/SwcA/pSig"), ("Ext", "/Vendor/Ext/ThirdSwc/rIn"))),
        )

        rendered = imported.report.render()
        # The rule is cited in the release the file declares (AUTOSAR_4-2-2.xsd), as the
        # skipped list's own line under "Defects of the export" is.
        assert (
            "1 connector(s)/delegation(s) reaching an external stub were skipped because the target port path "
            "is not owned by the context component's type (R4.2.2 [TPS_GST_00162]) -- a defect of the export; "
            'see "Defects of the export" below.'
        ) in rendered
        assert "constr_2626" not in rendered
        lines = rendered.split("\n")
        section = lines.index(next(line for line in lines if line.startswith("Defects of the export (")))
        assert lines[section + 1].startswith("  - 1 × ASSEMBLY-SW-CONNECTOR (R4.2.2 [TPS_GST_00162]): ")


# ---------------------------------------------------------------------------
# Conflicting declarations
# ---------------------------------------------------------------------------


def _twin_package(package: str, connectors: str, ports: str = "") -> str:
    """A second declaration of the composition ``Sub`` in another package, placing ``Ext`` and ``A``."""
    members = prototype("A", "/Acme/Components/SwcA") + prototype("Ext", EXT)
    body = composition("Sub", connectors.replace(f"{ROOT}/", f"/Acme/{package}/Sub/"), ports=ports, prototypes=members)
    return f"""
        <AR-PACKAGE>
          <SHORT-NAME>{package}</SHORT-NAME>
          <ELEMENTS>{body.replace(f"{ROOT}/", f"/Acme/{package}/Sub/")}
          </ELEMENTS>
        </AR-PACKAGE>"""


class TestTheMergeVerdictOnStubPorts:
    """Two declarations of one composition differing only in what mints a stub port conflict."""

    def _verdict(self, tmp_path: Path, first: str, second: str, ports: str = "") -> str:
        text = export_text(
            "", extra_packages=_twin_package("PkgX", first, ports) + _twin_package("PkgY", second, ports)
        )
        source = tmp_path / "twins.arxml"
        source.write_text(text, encoding="utf-8", newline="\n")
        decisions = decide_merges(read_arxml(str(source)))
        (group,) = [group for group in (*decisions.merged, *decisions.conflicting) if group.key.name == "Sub"]
        return group.verdict

    def test_identical_declarations_merge(self, tmp_path: Path) -> None:
        connector = _pout_to_ext()
        assert self._verdict(tmp_path, connector, connector) == MERGED

    def test_a_connector_end_differing_only_in_its_dest_conflicts(self, tmp_path: Path) -> None:
        first = _pout_to_ext()
        second = assembly(
            "A_to_Ext", ("A", "/Acme/Components/SwcA/pSig"), ("Ext", f"{EXT}/rIn"), requester_dest="P-PORT-PROTOTYPE"
        )
        assert self._verdict(tmp_path, first, second) == CONFLICTING

    def test_a_delegation_differing_only_in_its_wrapper_conflicts(self, tmp_path: Path) -> None:
        ports = _port("rOuter", "require", "/Acme/Interfaces/IOther")
        first = delegation("DEL_Ext", "R", ("Ext", f"{EXT}/rOuter"), "rOuter", dest="R-PORT-PROTOTYPE")
        second = delegation("DEL_Ext", "P", ("Ext", f"{EXT}/rOuter"), "rOuter", dest="R-PORT-PROTOTYPE")
        # The outer reference's DEST follows the wrapper in the builder; spell it the same in both.
        second = second.replace('OUTER-PORT-REF DEST="P-PORT-PROTOTYPE"', 'OUTER-PORT-REF DEST="R-PORT-PROTOTYPE"')
        assert self._verdict(tmp_path, first, second, ports) == CONFLICTING

    def test_a_conflicting_connector_group_mints_no_port_and_no_contradiction(self, tmp_path: Path) -> None:
        """Two connectors named ``Link`` state opposite directions for ``rIn``; the verdict imports neither.

        So nothing is minted and no contradiction is reported for a direction no imported connector states.
        """
        members = prototype("B", "/Acme/Components/SwcB") + prototype("Ext", EXT)
        sub_connector = assembly("Link", ("Ext", f"{EXT}/rIn"), ("B", "/Acme/Components/SwcB/rSig"))
        body = composition("Sub", sub_connector, prototypes=members).replace(f"{ROOT}/", "/Acme/SubPkg/Sub/")
        sub_package = f"""
        <AR-PACKAGE>
          <SHORT-NAME>SubPkg</SHORT-NAME>
          <ELEMENTS>{body}
          </ELEMENTS>
        </AR-PACKAGE>"""
        text = export_text(_pout_to_ext("Link"), extra_packages=sub_package).replace(
            PROTOTYPES, PROTOTYPES + prototype("S", "/Acme/SubPkg/Sub", "COMPOSITION-SW-COMPONENT-TYPE"), 1
        )
        source = tmp_path / "links.arxml"
        source.write_text(text, encoding="utf-8", newline="\n")
        (group,) = [group for group in decide_merges(read_arxml(str(source))).groups if group.key.name == "Link"]
        assert group.verdict == CONFLICTING

        imported = Imported(tmp_path, text)

        assert "Link" not in imported.connector_names()
        assert "rIn" not in imported.stub_ports()
        assert imported.report.stub_port_contradiction_lines == []


# ---------------------------------------------------------------------------
# --packages
# ---------------------------------------------------------------------------


def _packages_export(*, contradicting_in_b: bool = False) -> str:
    """``Root`` (package ``Top``) places ``SubA`` and ``SubB``; the stub ``Ext`` is placed in both.

    In ``SubA``: ``A.pSig -> Ext.rX``. In ``SubB``: ``B.pOther -> Ext.rX`` and
    ``Ext.pY -> B.rSig``. The whole file makes ``rX`` ambiguous and ``pY`` hinted;
    a selection of ``CompositionsA`` alone drops ``SubB`` -- ``rX`` loses one
    neighbour and keeps the other, and ``pY`` loses its last connection.
    """

    def sub(package: str, name: str, member: str, connectors: str, extra_members: str = "") -> str:
        members = prototype(member, f"/Acme/Components/Swc{member}") + prototype("Ext", EXT) + extra_members
        body = composition(name, connectors, prototypes=members).replace(f"{ROOT}/", f"/Acme/{package}/{name}/")
        return f"""
        <AR-PACKAGE>
          <SHORT-NAME>{package}</SHORT-NAME>
          <ELEMENTS>{body}
          </ELEMENTS>
        </AR-PACKAGE>"""

    sub_a = sub("CompositionsA", "SubA", "A", _pout_to_ext("A_to_Ext", target="rX"))
    sub_b = sub(
        "CompositionsB",
        "SubB",
        "B",
        _pout_to_ext("B_to_Ext", source="B", port="pOther", target="rX")
        + assembly("Ext_to_B", ("Ext", f"{EXT}/pY"), ("B", "/Acme/Components/SwcB/rSig"))
        + (
            assembly("B_to_Ext2", ("B", "/Acme/Components/SwcB/pSig"), ("Ext2", f"{EXT2}/xP"))
            + assembly("Ext2_to_B", ("Ext2", f"{EXT2}/xP"), ("B", "/Acme/Components/SwcB/rSig"))
            if contradicting_in_b
            else ""
        ),
        prototype("Ext2", EXT2) if contradicting_in_b else "",
    )
    root_members = prototype("SubA", "/Acme/CompositionsA/SubA", "COMPOSITION-SW-COMPONENT-TYPE") + prototype(
        "SubB", "/Acme/CompositionsB/SubB", "COMPOSITION-SW-COMPONENT-TYPE"
    )
    text = export_text("", extra_packages=sub_a + sub_b)
    return text.replace(PROTOTYPES, root_members)


class TestPackages:
    def test_the_whole_file_keeps_both_ports(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, _packages_export())

        ports = imported.stub_ports()
        assert set(ports) == {"rX", "pY"}
        assert ports["rX"].observed_interface is None
        assert ports["pY"].observed_interface == "ISig"
        assert imported.report.stub_port_ambiguity_lines == ["ExtSwc_ext0.rX -- candidates: IOther, ISig"]

    def test_a_selection_keeps_the_reached_port_and_drops_the_one_whose_last_connection_fell(
        self, tmp_path: Path
    ) -> None:
        imported = Imported(tmp_path, _packages_export(), packages=["CompositionsA"])

        ports = imported.stub_ports()
        assert set(ports) == {"rX"}, "pY lost its last connection with SubB"
        assert ports["rX"].observed_interface == "ISig", "the neighbour that fell is no longer a candidate"
        assert imported.report.stub_port_ambiguity_lines == []
        assert imported.report.external_stub_port_lines == [
            f"ExtSwc_ext0 ({EXT}): 1 port(s) -- 0 provide, 1 require; 1 with an observed interface"
        ]
        assert imported.findings() == []

    def test_an_unowned_count_inside_a_dropped_composition_is_narrowed_away(self, tmp_path: Path) -> None:
        text = _packages_export().replace(f"{EXT}/pY", "/Vendor/Other/ExtSwc/pY")
        whole = Imported(tmp_path, text, name="whole")
        selected = Imported(tmp_path, text, name="selected", packages=["CompositionsA"])

        assert whole.report.unowned_stub_port_paths == ["/Acme/CompositionsB/SubB/Ext_to_B"]
        assert selected.report.unowned_stub_port_paths == []

    def test_a_contradiction_on_a_stub_the_selection_drops_is_narrowed_away(self, tmp_path: Path) -> None:
        """``Ext2`` sits in ``SubB`` alone and states ``xP`` both ways; a selection without ``SubB`` drops it."""
        text = _packages_export(contradicting_in_b=True)
        whole = Imported(tmp_path, text, name="whole")
        selected = Imported(tmp_path, text, name="selected", packages=["CompositionsA"])

        assert whole.report.stub_port_contradiction_lines == [f"{EXT2}/xP -- provide at 1 end(s), require at 1 end(s)"]
        assert "OtherSwc_ext0" not in selected.swcs()
        assert selected.report.stub_port_contradiction_lines == []


# ---------------------------------------------------------------------------
# The finish: after the reserved device-name skip
# ---------------------------------------------------------------------------


def _device_named_interface_export(connectors: str) -> str:
    """``SwcE.pDev`` is typed by an interface named ``CON``, a name Windows reserves for a device."""
    extra = f"""
        <AR-PACKAGE>
          <SHORT-NAME>Extra</SHORT-NAME>
          <ELEMENTS>{_sr_interface("CON")}{_swc("SwcE", _port("pDev", "provide", "/Acme/Extra/CON"))}
          </ELEMENTS>
        </AR-PACKAGE>"""
    text = export_text(connectors, extra_packages=extra)
    return text.replace(PROTOTYPES, PROTOTYPES + prototype("E", "/Acme/Extra/SwcE"), 1)


class TestTheFinishReadsTheFinalElements:
    def test_an_interface_this_import_does_not_write_gives_no_hint(self, tmp_path: Path) -> None:
        text = _device_named_interface_export(
            assembly("E_to_Ext", ("E", "/Acme/Extra/SwcE/pDev"), ("Ext", f"{EXT}/rD"))
        )
        imported = Imported(tmp_path, text)

        assert "CON" not in {interface.name for interface in read_interfaces(imported.model_root)}
        assert {port.name: port.interface for port in imported.swcs()["SwcE"].ports}["pDev"] == "CON"
        port = imported.stub_ports()["rD"]
        assert (port.direction, port.observed_interface) == ("require", None)

    def test_a_port_whose_last_connector_the_device_name_skip_drops_is_pruned(self, tmp_path: Path) -> None:
        """The connector ``AUX`` is skipped for its reserved name; ``rC`` loses its only connection."""
        imported = Imported(tmp_path, export_text(_pout_to_ext() + _pout_to_ext("AUX", target="rC")))

        assert "AUX" not in imported.connector_names()
        assert set(imported.stub_ports()) == {"rIn"}
        assert imported.report.external_stub_port_lines == [
            f"ExtSwc_ext0 ({EXT}): 1 port(s) -- 0 provide, 1 require; 1 with an observed interface"
        ]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class TestTheReport:
    def test_the_stub_port_lines_and_no_declares_no_port_category(self, tmp_path: Path) -> None:
        imported = Imported(
            tmp_path, export_text(_pout_to_ext() + _pout_to_ext("A_to_Ext_2", port="pOther", target="rIn2"))
        )

        rendered = imported.report.render()
        assert imported.report.external_stub_port_lines == [
            f"ExtSwc_ext0 ({EXT}): 2 port(s) -- 0 provide, 2 require; 2 with an observed interface"
        ]
        assert "External component stub ports (1 stub(s);" in rendered
        assert "declares no port" not in rendered
        assert imported.report.unowned_stub_port_paths == []
        assert not any("declares no port" in entry.reason for entry in imported.report.skip_entries)

    def test_the_stub_line_says_what_the_stub_declares(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext()))

        (line,) = [line for line in imported.report.external_stub_lines if "ExtSwc_ext0" in line]
        assert line.endswith(
            "imported as external stub 'ExtSwc_ext0', declaring only the ports its connectors reach, "
            "by name and direction"
        )
        assert "with no ports" not in line


# ---------------------------------------------------------------------------
# The combined identifier test
# ---------------------------------------------------------------------------


#: A last segment no AUTOSAR reference path may carry: a hyphen is outside an
#: ``Identifier``, so a source stating it is itself not schema-valid.
FOREIGN = "r-Level"


class TestAForeignStubPortName:
    """One non-Identifier stub-port name through import, editing, ``check-model`` and generation.

    The writer emits a stub port's name as the last segment of every end on it, and a reference
    path segment is an ``Identifier``, so the name is held to the rule everywhere: the importer
    reports the end unresolved and drops its connector (never repairing the segment), and a
    hand-authored or protocol-made port of that name is refused by every gate.
    """

    def test_the_importer_reports_the_end_and_drops_its_connector(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext(target=FOREIGN) + _pout_to_ext("B_to_Ext", source="B")))

        assert "A_to_Ext" not in imported.connector_names()
        assert "B_to_Ext" in imported.connector_names()
        assert set(imported.stub_ports()) == {"rIn"}, "nothing is minted from the foreign segment"
        reason = imported.reason("A_to_Ext")
        assert f"unresolved reference {EXT}/{FOREIGN} (the target port path's last segment '{FOREIGN}'" in reason
        assert "no port is minted from it" in reason
        assert imported.report.unowned_stub_port_paths == [], "not the unowned-stub-port refusal"
        assert imported.findings() == []
        assert check_identifier_conformance(imported.model_root).records == ()

    def test_editing_check_model_and_generation_refuse_it(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext()))
        project = str(imported.project)
        model = Path(imported.model_root)
        before = {p.relative_to(model).as_posix(): p.read_bytes() for p in model.rglob("*") if p.is_file()}

        # editing
        token = handle_request({"operation": "discover"}, project=project)["baseline"]
        address = {"layer": "swc", "element": "ExtSwc_ext0", "path": [{"field": "ports"}, {"member": FOREIGN}]}
        response = handle_request(
            {
                "operation": "change_set",
                "baseline": token,
                "intent": "a foreign stub port",
                "operations": [
                    {"operation": "create", "address": address, "value": {"name": FOREIGN, "direction": "provide"}}
                ],
            },
            project=project,
            review=lambda proposal: ApprovalDecision(
                change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
            ),
        )
        assert response["error"] == "invalid-identifier", response
        assert "last segment of an AUTOSAR reference path" in response["message"]
        assert {p.relative_to(model).as_posix(): p.read_bytes() for p in model.rglob("*") if p.is_file()} == before

        # hand-authored
        (stub_file,) = model.rglob("ExtSwc_ext0.yaml")
        document = yaml.safe_load(stub_file.read_text(encoding="utf-8"))
        document["ports"].append({"name": FOREIGN, "direction": "provide"})
        stub_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        records = check_identifier_conformance(imported.model_root).records
        assert [record.rule for record in records if FOREIGN in record.message] == ["identifier.declared"]
        out = tmp_path / "fresh-out"
        with pytest.raises(ValueError, match="reference path segment"):
            generate_arxml(imported.model_root, str(out / "model.arxml"))
        assert not out.exists()

    def test_the_same_name_on_a_declared_component_is_still_refused(self, tmp_path: Path) -> None:
        imported = Imported(tmp_path, export_text(_pout_to_ext()))
        path = Path(imported.model_root) / "swc"
        (swc_file,) = [p for p in path.rglob("SwcA.yaml")]
        document = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
        document["ports"][0]["name"] = FOREIGN
        swc_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        records = check_identifier_conformance(imported.model_root).records
        assert [record.rule for record in records if FOREIGN in record.message] == ["identifier.declared"]
