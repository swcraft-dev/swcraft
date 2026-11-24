"""Contract ``7.6.0``: ports on an external **component** stub, at ``generate arxml`` and the component export.

A connector or delegation end on a stub's port is emitted as the stub's ``external_ref`` plus the
port name, with the ``DEST`` the port's direction gives; no stub element or stub-port interface is
emitted. The fixture is the compositions tree wired by
``external_stubs_support.add_component_stub_ports``. The classes pin: the emission, read off the
generated document; that the hint reaches no byte; that direction still judges, nothing written;
the writer twin ``check_external_stubs`` agreeing with ``check-model``; a declared port without an
interface refused in its own words; and the component export refusing a stub with ports.

What it does **not** reach: the XSD (``test_arxml_xsd_conformance.py``'s stub-port class, run by
``test-xsd``); whether the stub's foreign type really owns the port the end names (nothing in
SWCraft resolves an ``external_ref``; the integrator's tool is that check); the importer's minting
(``test_stub_ports_import.py``); and any model shape outside the ones written below -- this is
example-based evidence, not a proof. A model without stub ports emits the bytes it did before:
``test_stub_ports_byte_identity.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import external_stubs_support as stubs
import pytest
import yaml
from conftest import _write_manifest
from lxml import etree
from swcraft_tools.cli import main
from swcraft_tools.component_exchange import ComponentExchangeError, export_all_components, export_component
from swcraft_tools.component_exchange.closure import compute_closure
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.arxml_xml import ar_qname, short_name_of

STUB = "swc/external/ExternalStub.yaml"
OTHER_STUB = "swc/external/OtherStub.yaml"
BETA = "swc/core/Beta.yaml"
SUB = "compositions/Sub.yaml"

STUB_REF = "/Other/Project/SupplierPlatforms/Bsw/ExternalStub"
OTHER_STUB_REF = stubs.SECOND_COMPONENT_STUB[1]
SUB_PATH = "/ComponentTypes/Sub"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    root = stubs.component_stub_ports_model_root(tmp_path / "model")
    assert stubs.rules(root) == [], "the wired fixture must be one check-model accepts"
    return root


def _generate_bytes(model_root: Path, output: Path) -> bytes:
    generate_arxml(str(model_root), str(output))
    return output.read_bytes()


def _generate(model_root: Path, tmp_path: Path) -> etree._Element:
    output = tmp_path / "out" / "model.arxml"
    generate_arxml(str(model_root), str(output))
    return etree.parse(str(output)).getroot()


def _refused(model_root: Path, tmp_path: Path, *fragments: str) -> str:
    """Generate into a directory that does not exist; assert the refusal and that nothing was written."""
    output_dir = tmp_path / "fresh-output"
    assert not output_dir.exists()
    with pytest.raises(ValueError) as caught:
        generate_arxml(str(model_root), str(output_dir / "model.arxml"))
    message = str(caught.value)
    for fragment in fragments:
        assert fragment in message, message
    assert not output_dir.exists(), "a refused generation wrote something"
    return message


def _ref(parent: etree._Element, tag: str) -> tuple[str | None, str | None]:
    (reference,) = parent.findall(ar_qname(tag))
    return reference.get("DEST"), reference.text


def _named(root: etree._Element, tag: str, name: str) -> etree._Element:
    (node,) = [element for element in root.iter(ar_qname(tag)) if short_name_of(element) == name]
    return node


def _end(connector: etree._Element, wrapper: str) -> dict[str, tuple[str | None, str | None]]:
    """The two references of one ``PROVIDER-IREF`` / ``REQUESTER-IREF``, by tag."""
    (iref,) = connector.findall(ar_qname(wrapper))
    return {etree.QName(child).localname: (child.get("DEST"), child.text) for child in iref}


def _inner(delegation: etree._Element) -> tuple[str, dict[str, tuple[str | None, str | None]]]:
    """The one instance-ref variant under ``INNER-PORT-IREF``, and its two references by tag."""
    (inner_iref,) = delegation.findall(ar_qname("INNER-PORT-IREF"))
    (instance,) = list(inner_iref)
    return (
        etree.QName(instance).localname,
        {etree.QName(child).localname: (child.get("DEST"), child.text) for child in instance},
    )


def _add_provide_delegation(model_root: Path) -> None:
    """A provide boundary port ``pExtOut`` on ``Sub``, delegated onto the stub's ``pExt``."""

    def sub(document: dict[str, Any]) -> None:
        document["ports"].append({"name": "pExtOut", "direction": "provide", "interface": "ITrigger"})
        document["delegations"].append(
            {"name": "Sub_pExtOut_Ext", "port": "pExtOut", "member": "Ext", "member_port": "pExt"}
        )

    stubs.change(model_root, SUB, sub)


# ---------------------------------------------------------------------------
# The emission
# ---------------------------------------------------------------------------


class TestAConnectionToAStubsPortIsEmitted:
    """Every end on a stub's port is the stub's ``external_ref`` plus the port name, ``DEST`` from the direction."""

    def test_an_assembly_connector_from_a_stubs_port(self, model_root: Path, tmp_path: Path) -> None:
        connector = _named(_generate(model_root, tmp_path), "ASSEMBLY-SW-CONNECTOR", "ext_link")

        assert _end(connector, "PROVIDER-IREF") == {
            "CONTEXT-COMPONENT-REF": ("SW-COMPONENT-PROTOTYPE", f"{SUB_PATH}/Ext"),
            "TARGET-P-PORT-REF": ("P-PORT-PROTOTYPE", f"{STUB_REF}/pExt"),
        }
        assert _end(connector, "REQUESTER-IREF") == {
            "CONTEXT-COMPONENT-REF": ("SW-COMPONENT-PROTOTYPE", f"{SUB_PATH}/Alpha"),
            "TARGET-R-PORT-REF": ("R-PORT-PROTOTYPE", "/ComponentTypes/Beta/rFromExt"),
        }

    def test_an_assembly_connector_with_a_stub_on_both_ends(self, model_root: Path, tmp_path: Path) -> None:
        connector = _named(_generate(model_root, tmp_path), "ASSEMBLY-SW-CONNECTOR", "stub_link")

        assert _end(connector, "PROVIDER-IREF") == {
            "CONTEXT-COMPONENT-REF": ("SW-COMPONENT-PROTOTYPE", f"{SUB_PATH}/Ext"),
            "TARGET-P-PORT-REF": ("P-PORT-PROTOTYPE", f"{STUB_REF}/pExt"),
        }
        assert _end(connector, "REQUESTER-IREF") == {
            "CONTEXT-COMPONENT-REF": ("SW-COMPONENT-PROTOTYPE", f"{SUB_PATH}/Ext2"),
            "TARGET-R-PORT-REF": ("R-PORT-PROTOTYPE", f"{OTHER_STUB_REF}/rOther"),
        }

    def test_a_require_delegation_onto_a_stubs_port(self, model_root: Path, tmp_path: Path) -> None:
        delegation = _named(_generate(model_root, tmp_path), "DELEGATION-SW-CONNECTOR", "Sub_rExtIn_Ext")

        assert _inner(delegation) == (
            "R-PORT-IN-COMPOSITION-INSTANCE-REF",
            {
                "CONTEXT-COMPONENT-REF": ("SW-COMPONENT-PROTOTYPE", f"{SUB_PATH}/Ext"),
                "TARGET-R-PORT-REF": ("R-PORT-PROTOTYPE", f"{STUB_REF}/rExt"),
            },
        )
        assert _ref(delegation, "OUTER-PORT-REF") == ("R-PORT-PROTOTYPE", f"{SUB_PATH}/rExtIn")

    def test_a_provide_delegation_onto_a_stubs_port(self, model_root: Path, tmp_path: Path) -> None:
        _add_provide_delegation(model_root)
        assert stubs.rules(model_root) == []

        delegation = _named(_generate(model_root, tmp_path), "DELEGATION-SW-CONNECTOR", "Sub_pExtOut_Ext")

        assert _inner(delegation) == (
            "P-PORT-IN-COMPOSITION-INSTANCE-REF",
            {
                "CONTEXT-COMPONENT-REF": ("SW-COMPONENT-PROTOTYPE", f"{SUB_PATH}/Ext"),
                "TARGET-P-PORT-REF": ("P-PORT-PROTOTYPE", f"{STUB_REF}/pExt"),
            },
        )
        assert _ref(delegation, "OUTER-PORT-REF") == ("P-PORT-PROTOTYPE", f"{SUB_PATH}/pExtOut")

    def test_the_stub_is_emitted_as_its_prototypes_reference_alone(self, model_root: Path, tmp_path: Path) -> None:
        """No component type, no port prototype and no interface reference for a stub or its ports."""
        root = _generate(model_root, tmp_path)

        names = {short_name_of(element) for element in root.iter() if element.find(ar_qname("SHORT-NAME")) is not None}
        assert not names & {"ExternalStub", "OtherStub", "pExt", "rExt", "rOther"}, names
        assert _ref(_named(root, "SW-COMPONENT-PROTOTYPE", "Ext"), "TYPE-TREF") == (
            "APPLICATION-SW-COMPONENT-TYPE",
            STUB_REF,
        )
        assert _ref(_named(root, "SW-COMPONENT-PROTOTYPE", "Ext2"), "TYPE-TREF") == (
            "APPLICATION-SW-COMPONENT-TYPE",
            OTHER_STUB_REF,
        )


class TestTheHintReachesNoByte:
    """The emitted document is independent of ``observed_interface`` (TOR: the reference values do not depend on it)."""

    @pytest.mark.parametrize(
        "hints",
        [
            pytest.param({"pExt": None}, id="hint-removed"),
            pytest.param({"pExt": "ISignal"}, id="hint-another-declared-interface"),
            pytest.param({"pExt": "INoLongerDeclared"}, id="hint-naming-nothing"),
            pytest.param({"rExt": "ITrigger"}, id="hint-added"),
        ],
    )
    def test_the_bytes_are_those_of_the_fixture(self, model_root: Path, tmp_path: Path, hints: dict) -> None:
        before = _generate_bytes(model_root, tmp_path / "before" / "model.arxml")
        for port, hint in hints.items():
            stubs.set_port(model_root, STUB, port, observed_interface=hint)
        assert stubs.rules(model_root) == []

        assert _generate_bytes(model_root, tmp_path / "after" / "model.arxml") == before

    def test_the_premise_the_hint_was_really_changed(self, model_root: Path) -> None:
        """Non-vacuity: the fixture's stub file carries the hint the cases above vary."""
        document = yaml.safe_load((model_root / STUB).read_text(encoding="utf-8"))
        assert {port["name"]: port.get("observed_interface") for port in document["ports"]} == {
            "pExt": "ITrigger",
            "rExt": None,
        }


# ---------------------------------------------------------------------------
# What is still refused
# ---------------------------------------------------------------------------


class TestTheDirectionStillJudges:
    """A stub's port abstains on the interface only; its direction is a fact and is still judged."""

    def test_a_connector_from_a_stubs_require_port(self, model_root: Path, tmp_path: Path) -> None:
        stubs.change(model_root, "connectors/ext_link.yaml", lambda d: d["from"].update(port="rExt"))
        assert stubs.rules(model_root) != []

        _refused(model_root, tmp_path, "ext_link", "rExt")

    def test_a_connector_to_a_stubs_provide_port(self, model_root: Path, tmp_path: Path) -> None:
        stubs.set_port(model_root, OTHER_STUB, "rOther", direction="provide")
        assert stubs.rules(model_root) != []

        _refused(model_root, tmp_path, "stub_link", "rOther")

    def test_a_delegation_onto_a_stubs_port_of_the_other_direction(self, model_root: Path, tmp_path: Path) -> None:
        stubs.change(model_root, SUB, lambda d: d["delegations"][-1].update(member_port="pExt"))
        assert "composition.delegation-port-direction-mismatch" in stubs.rules(model_root)

        _refused(model_root, tmp_path, "Sub_rExtIn_Ext", "must declare the same direction")

    def test_a_connector_to_a_port_the_stub_does_not_declare(self, model_root: Path, tmp_path: Path) -> None:
        stubs.change(model_root, "connectors/stub_link.yaml", lambda d: d["to"].update(port="rNotDeclared"))
        assert stubs.rules(model_root) != []

        _refused(model_root, tmp_path, "rNotDeclared")


class TestAStubPortNameIsAReferencePathSegment:
    """The end on a stub's port is ``external_ref`` + ``/`` + its name, so a non-Identifier name is refused.

    ``x/y`` would emit ``…/ExternalStub/x/y`` -- a schema-valid reference to a port of another
    element than the context prototype's type ([TPS_GST_00162]) -- and ``1x`` / ``a b`` a
    schema-invalid one.
    """

    @pytest.mark.parametrize("bad", ["x/y", "1x", "a b"])
    def test_check_model_reports_it_and_generation_writes_nothing(
        self, model_root: Path, tmp_path: Path, bad: str
    ) -> None:
        stubs.set_port(model_root, STUB, "pExt", name=bad)
        for connector in ("ext_link", "stub_link"):
            stubs.change(model_root, f"connectors/{connector}.yaml", lambda d: d["from"].update(port=bad))

        assert stubs.rules(model_root) == ["identifier.declared"]
        _refused(model_root, tmp_path, f"SWC 'ExternalStub' port name (a reference path segment): '{bad}'")


class TestTheWriterTwinOfTheStubRule:
    """``check_external_stubs``, restated with ``swc.external-with-ports``: ports yes, anything more no."""

    def test_a_declared_interface_on_a_stubs_port(self, model_root: Path, tmp_path: Path) -> None:
        stubs.set_port(model_root, STUB, "pExt", interface="ITrigger")

        _refused(model_root, tmp_path, "external stub 'ExternalStub'", "port 'pExt' declares an interface")

    def test_measures_on_a_stubs_port(self, model_root: Path, tmp_path: Path) -> None:
        stubs.set_port(model_root, STUB, "rExt", measures={"trigger": "an argument nothing reads"})

        _refused(model_root, tmp_path, "external stub 'ExternalStub'", "port 'rExt' declares measures")

    def test_com_specs_on_a_stubs_port(self, model_root: Path, tmp_path: Path) -> None:
        """Contract ``7.11.0`` widened the rule by ``com_specs``, and the writer twin with it."""
        stubs.set_port(model_root, STUB, "rExt", com_specs=[{"data_element": "trigger", "enable_update": True}])

        _refused(model_root, tmp_path, "external stub 'ExternalStub'", "port 'rExt' declares com_specs")

    def test_a_runnable_is_still_refused_and_names_no_port(self, model_root: Path, tmp_path: Path) -> None:
        stubs.change(model_root, STUB, lambda d: d.update(runnables=[{"name": "Run"}]))

        message = _refused(model_root, tmp_path, "external stub 'ExternalStub' declares runnables")
        assert "declares ports" not in message
        assert "port 'pExt'" not in message

    @pytest.mark.parametrize(
        ("port", "changes", "refused"),
        [
            pytest.param("pExt", {}, False, id="hinted"),
            pytest.param("rExt", {}, False, id="bare"),
            pytest.param("pExt", {"observed_interface": "INoLongerDeclared"}, False, id="hint-naming-nothing"),
            pytest.param("pExt", {"interface": "ITrigger"}, True, id="interface"),
            pytest.param("rExt", {"interface": "IAbsent"}, True, id="undeclared-interface"),
            pytest.param("rExt", {"measures": {"trigger": "m"}}, True, id="measures"),
            pytest.param("pExt", {"interface": "ITrigger", "measures": {"trigger": "m"}}, True, id="both"),
            pytest.param("rExt", {"com_specs": [{"data_element": "trigger"}]}, True, id="com-specs"),
        ],
    )
    def test_the_writer_and_check_model_agree(
        self, model_root: Path, tmp_path: Path, port: str, changes: dict, refused: bool
    ) -> None:
        """A guard stricter or looser than the gate is a disagreement that must never happen."""
        stubs.set_port(model_root, STUB, port, **changes)

        reported = "swc.external-with-ports" in stubs.rules(model_root)
        output_dir = tmp_path / "fresh-output"
        try:
            generate_arxml(str(model_root), str(output_dir / "model.arxml"))
            writer_refused = False
        except ValueError as error:
            # Any refusal counts: a case expected to pass must not pass on a
            # ValueError raised for some other reason.
            writer_refused = True
            assert "external stub 'ExternalStub'" in str(error), error
            assert not output_dir.exists()
        assert (reported, writer_refused) == (refused, refused)


class TestADeclaredPortWithoutAnInterface:
    """``interface`` is optional at load, so the writer's refusal of a declared port without one needs its own words."""

    def test_generation_refuses_with_a_sentence_of_its_own(self, model_root: Path, tmp_path: Path) -> None:
        stubs.change(
            model_root,
            BETA,
            lambda d: d["ports"].append({"name": "pBare", "direction": "provide"}),
        )
        assert stubs.rules(model_root) == ["port.undeclared-interface"]

        message = _refused(model_root, tmp_path, "SWC 'Beta' port 'pBare' declares no interface")
        assert "None" not in message


# ---------------------------------------------------------------------------
# The component export
# ---------------------------------------------------------------------------


class TestTheComponentExportOfAStubWithPorts:
    """A stub with ports is refused by name before anything is written, and a closure tolerates ``None``."""

    def test_the_export_is_refused_with_its_sentence(self, model_root: Path, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ComponentExchangeError) as caught:
            export_component(str(model_root), "ExternalStub", str(out_dir / "ExternalStub.arxml"))

        assert "SWC 'ExternalStub' is an external stub (external: true)" in str(caught.value)
        assert list(out_dir.iterdir()) == []

    def test_a_declared_port_without_an_interface_is_refused_by_the_writer(
        self, model_root: Path, tmp_path: Path
    ) -> None:
        """The closure carries nothing for the port; ``_port_interface`` refuses the export in its own words."""
        stubs.change(model_root, BETA, lambda d: d["ports"].append({"name": "pBare", "direction": "provide"}))
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with pytest.raises(ValueError, match="SWC 'Beta' port 'pBare' declares no interface"):
            export_component(str(model_root), "Beta", str(out_dir / "Beta.arxml"))

        assert list(out_dir.iterdir()) == []

    def test_the_batch_skips_both_stubs(self, model_root: Path, tmp_path: Path) -> None:
        reports = export_all_components(str(model_root), str(tmp_path / "batch"))

        assert {report.swc for report in reports}.isdisjoint({"ExternalStub", "OtherStub"})
        assert reports, "the declared components are still exported"

    def test_a_closure_over_a_stubs_ports_does_not_raise(self, model_root: Path) -> None:
        closure = compute_closure(str(model_root), "ExternalStub")

        assert closure.interfaces == []

    def test_a_closure_over_a_declared_port_without_an_interface_does_not_raise(self, model_root: Path) -> None:
        """A declared port with no interface is ``port.undeclared-interface``'s; the closure names nothing for it."""
        stubs.change(model_root, BETA, lambda d: d["ports"].append({"name": "pBare", "direction": "provide"}))

        closure = compute_closure(str(model_root), "Beta")

        assert sorted(interface.name for interface in closure.interfaces) == ["ISignal", "ITrigger"]

    def test_generate_arxml_component_gives_the_sentence_not_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = tmp_path / "project"
        stubs.component_stub_ports_model_root(project / "model")
        manifest = _write_manifest(
            project,
            {
                "name": "stub-ports",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
            },
        )
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "generate", "arxml", "--project", str(manifest), "--component", "ExternalStub"],
        )

        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == 1
        err = capsys.readouterr().err
        assert "SWC 'ExternalStub' is an external stub (external: true)" in err, err
        assert "TypeError" not in err
        assert not (project / "dist" / "components").exists()
