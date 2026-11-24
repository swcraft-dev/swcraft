"""The declared composition hierarchy in the ARXML.

What the flat goldens of ``test_arxml_composition_writer.py`` do not reach: boundary ports with content,
composition-typed prototypes, ``DELEGATION-SW-CONNECTOR``, external stubs, and the resolution of an
endpoint's type to the member of that type.

The oracle is ``fixtures/compositions/model``, a committed throwaway root used in place (no committed
``.arxml`` is generated from it; its README says what every element is for). **The expected shapes are
not derived from this writer**: the two licensed AUTOSAR XSDs (identical in every composition group), real
tool exports and a hand-built probe valid at 4.2.2 and 4.3.0 agree on them.

**What the XSD cannot answer:** it does not resolve references, so a delegation wired to the wrong
instance or port, an endpoint at the wrong prototype and an external prototype whose reference differs
from the source's all validate. Every assertion about which element a reference names is therefore
structural here; the closed round trip on real tool exports is the second oracle for the last two.

The XSD half is ``TestTheCompositionHierarchyConforms`` in ``test_arxml_xsd_conformance.py``:
``dev.py test-xsd`` selects that file by path and turns a missing schema into a failure, where an XSD
assertion here would only skip.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from conftest import (
    COMPATIBLE_INTERFACES_MODEL_ROOT,
    COMPOSITIONS_MODEL_ROOT,
    INTERFACE_COMPATIBILITY_MODEL_ROOT,
    REPO_ROOT,
    model_element_path,
)
from swcraft_tools.cli import main
from swcraft_tools.reader import read_compositions
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml

AR_NS = "{http://autosar.org/schema/r4.0}"


def _copy_compositions_model(tmp_path: Path) -> Path:
    """A writable copy of ``fixtures/compositions/model`` under *tmp_path*, for a case that edits an element."""
    model_root = tmp_path / "model"
    shutil.copytree(COMPOSITIONS_MODEL_ROOT, model_root)
    return model_root


def _short_name(element: ET.Element) -> str | None:
    child = element.find(f"{AR_NS}SHORT-NAME")
    return child.text if child is not None else None


def _composition(root: ET.Element, name: str) -> ET.Element:
    """The ``COMPOSITION-SW-COMPONENT-TYPE`` of that ``SHORT-NAME``."""
    for element in root.iter(f"{AR_NS}COMPOSITION-SW-COMPONENT-TYPE"):
        if _short_name(element) == name:
            return element
    raise AssertionError(f"no COMPOSITION-SW-COMPONENT-TYPE named {name!r}")


def _named_child(parent: ET.Element, tag: str, name: str) -> ET.Element:
    for element in parent.iter(f"{AR_NS}{tag}"):
        if _short_name(element) == name:
            return element
    raise AssertionError(f"no {tag} named {name!r}")


def _reference(parent: ET.Element, tag: str) -> tuple[str, str]:
    """``(DEST, path)`` of the one *tag* below *parent*."""
    matches = list(parent.iter(f"{AR_NS}{tag}"))
    assert len(matches) == 1, f"expected exactly one {tag}, found {len(matches)}"
    return matches[0].get("DEST", ""), matches[0].text or ""


def _local(tag: str) -> str:
    return tag[len(AR_NS) :] if tag.startswith(AR_NS) else tag


@pytest.fixture(scope="module")
def fixture_document(tmp_path_factory) -> ET.Element:
    """``fixtures/compositions/model`` generated once, at the default version."""
    output = tmp_path_factory.mktemp("compositions") / "model.arxml"
    generate_arxml(str(COMPOSITIONS_MODEL_ROOT), str(output))
    return ET.fromstring(output.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compatible_document(tmp_path_factory) -> ET.Element:
    """``fixtures/compatible_interfaces/model`` generated once, at the default version."""
    output = tmp_path_factory.mktemp("compatible") / "model.arxml"
    generate_arxml(str(COMPATIBLE_INTERFACES_MODEL_ROOT), str(output))
    return ET.fromstring(output.read_text(encoding="utf-8"))


class TestTheFixtureIsTheModelItClaimsToBe:
    """The oracle's own premises: a shape assertion below is only about the writer if the model carries the shape."""

    def test_the_model_passes_check_model(self) -> None:
        result = check_cross_layer_consistency(str(COMPOSITIONS_MODEL_ROOT))
        assert result.consistent, result.issues

    def test_it_declares_one_root_and_two_levels_below_it(self) -> None:
        compositions = {c.name: c for c in read_compositions(str(COMPOSITIONS_MODEL_ROOT))}
        roots = [c.name for c in compositions.values() if c.root]
        assert roots == ["Root"]
        assert [m.composition for m in compositions["Root"].members] == ["Sub", "Sink"]

    def test_it_carries_three_delegations_across_two_compositions(self) -> None:
        compositions = {c.name: c for c in read_compositions(str(COMPOSITIONS_MODEL_ROOT))}
        counted = {name: len(c.delegations) for name, c in compositions.items()}
        assert counted == {"Root": 0, "Sub": 2, "Sink": 1}

    def test_it_carries_a_member_whose_name_differs_from_its_type(self) -> None:
        sub = {c.name: c for c in read_compositions(str(COMPOSITIONS_MODEL_ROOT))}["Sub"]
        alpha = next(member for member in sub.members if member.name == "Alpha")
        assert alpha.type == "Beta"

    def test_its_boundary_ports_are_not_declared_provide_first(self) -> None:
        """The premise of ``test_boundary_ports_follow_declaration_order``."""
        sub = {c.name: c for c in read_compositions(str(COMPOSITIONS_MODEL_ROOT))}["Sub"]
        assert [port.direction for port in sub.ports] == ["require", "provide"]


class TestTheCompositionElement:
    """One element per declared composition, with the schema's child order."""

    def test_one_element_per_declared_composition_in_read_order(self, fixture_document: ET.Element) -> None:
        emitted = [_short_name(element) for element in fixture_document.iter(f"{AR_NS}COMPOSITION-SW-COMPONENT-TYPE")]
        assert emitted == ["Root", "Sink", "Sub"]

    @pytest.mark.parametrize("name", ["Root", "Sink", "Sub"])
    def test_the_child_order_is_the_schemas(self, fixture_document: ET.Element, name: str) -> None:
        """``SHORT-NAME, PORTS, COMPONENTS, CONNECTORS`` — the schema's sequence.

        A ``LONG-NAME`` and a ``DESC`` stand between ``SHORT-NAME`` and ``PORTS``, the ``Identifiable`` position.
        """
        order = ("SHORT-NAME", "LONG-NAME", "DESC", "PORTS", "COMPONENTS", "CONNECTORS")
        children = [_local(child.tag) for child in _composition(fixture_document, name)]
        assert children == [tag for tag in order if tag in children]
        assert children[0] == "SHORT-NAME"
        assert "PORTS" in children

    def test_boundary_ports_follow_declaration_order(self, fixture_document: ET.Element) -> None:
        """Declaration order, **not** the provide-first order a component's ``PORTS`` uses.

        AUTOSAR fixes no order for a composition's ports; provide-first is a kept legacy order of components
        (``component_exchange/divergence.py`` sorts to match it).
        """
        ports = _composition(fixture_document, "Sub").find(f"{AR_NS}PORTS")
        assert [(_local(port.tag), _short_name(port)) for port in ports] == [
            ("R-PORT-PROTOTYPE", "rTrigger"),
            ("P-PORT-PROTOTYPE", "pSignalOut"),
        ]

    def test_a_boundary_port_carries_its_com_specs_and_interface_tref(self, fixture_document: ET.Element) -> None:
        """The same port emitter a component uses: the schema gives a composition port the component port's content."""
        port = _named_child(_composition(fixture_document, "Sub"), "P-PORT-PROTOTYPE", "pSignalOut")
        assert [_local(child.tag) for child in port] == [
            "SHORT-NAME",
            "PROVIDED-COM-SPECS",
            "PROVIDED-INTERFACE-TREF",
        ]
        assert _reference(port, "PROVIDED-INTERFACE-TREF") == (
            "SENDER-RECEIVER-INTERFACE",
            "/PortInterfaces/ISignal",
        )


class TestTheComponentPrototypes:
    """``SW-COMPONENT-PROTOTYPE`` — one per member, with the member's own name."""

    def test_a_member_named_after_its_type_points_at_the_component(self, fixture_document: ET.Element) -> None:
        prototype = _named_child(_composition(fixture_document, "Sub"), "SW-COMPONENT-PROTOTYPE", "Gamma")
        assert _reference(prototype, "TYPE-TREF") == ("APPLICATION-SW-COMPONENT-TYPE", "/ComponentTypes/Gamma")

    def test_a_composition_typed_member_carries_the_composition_dest(self, fixture_document: ET.Element) -> None:
        """The model's two type fields are AUTOSAR's ``DEST``, and this is where they land."""
        prototype = _named_child(_composition(fixture_document, "Root"), "SW-COMPONENT-PROTOTYPE", "Sub")
        assert _reference(prototype, "TYPE-TREF") == ("COMPOSITION-SW-COMPONENT-TYPE", "/ComponentTypes/Sub")

    def test_the_prototype_of_a_differing_name_member_keeps_both_names(self, fixture_document: ET.Element) -> None:
        """The wrong-prototype failure, at the prototype: ``Alpha`` of type ``Beta``.

        The ``SHORT-NAME`` is the member's and the ``TYPE-TREF`` is the type's, neither derived from the other.
        """
        prototype = _named_child(_composition(fixture_document, "Sub"), "SW-COMPONENT-PROTOTYPE", "Alpha")
        assert _reference(prototype, "TYPE-TREF") == ("APPLICATION-SW-COMPONENT-TYPE", "/ComponentTypes/Beta")


class TestTheExternalStub:
    """The external prototype: the two verbatim strings, and no component type.

    A ``TYPE-TREF`` outside the document is XSD-valid, so these are structural assertions; the byte
    comparison against the source is the closed round-trip suite's.
    """

    def test_the_prototype_carries_the_sources_reference_and_dest_verbatim(self, fixture_document: ET.Element) -> None:
        prototype = _named_child(_composition(fixture_document, "Sub"), "SW-COMPONENT-PROTOTYPE", "Ext")
        assert _reference(prototype, "TYPE-TREF") == (
            "APPLICATION-SW-COMPONENT-TYPE",
            "/Other/Project/SupplierPlatforms/Bsw/ExternalStub",
        )

    def test_no_component_type_is_emitted_for_it(self, fixture_document: ET.Element) -> None:
        """The stub says the model has **no** type declaration; inventing one would deny it."""
        emitted = [_short_name(element) for element in fixture_document.iter(f"{AR_NS}APPLICATION-SW-COMPONENT-TYPE")]
        assert emitted == ["Beta", "Consumer", "Gamma"]
        assert "ExternalStub" not in emitted

    def test_no_implementation_sibling_is_emitted_for_it_either(self, fixture_document: ET.Element) -> None:
        emitted = [_short_name(element) for element in fixture_document.iter(f"{AR_NS}SWC-IMPLEMENTATION")]
        assert emitted == ["Beta_Implementation", "Consumer_Implementation", "Gamma_Implementation"]

    def test_the_stubs_own_name_never_reaches_a_reference(self, fixture_document: ET.Element) -> None:
        """``external_ref`` is the reference, never ``/ComponentTypes/{name}``."""
        text = ET.tostring(fixture_document, encoding="unicode")
        assert "/ComponentTypes/ExternalStub" not in text


class TestTheDeclaredFieldDecidesWhichLayerAReferenceResolvesIn:
    """The resolvers dispatch on the member's declared field (``type`` or ``composition``), never on a lookup order.

    Graded as a unit, in memory: the colliding model is refused on disk by ``_check_component_type_names``
    and ``check_name_uniqueness``, and the resolver must not depend on the gate in front of it. The
    end-to-end shape is not reachable, which is the point.
    """

    @staticmethod
    def _colliding_maps() -> tuple[dict, dict]:
        """One name, two layers: an external stub and a composition both called ``Shared``."""
        from swcraft_tools.schema import Composition, CompositionPort, SWComponent

        stub = SWComponent(
            name="Shared",
            external=True,
            external_ref="/Foreign/Pkg/ComponentTypes/Shared",
            external_dest="APPLICATION-SW-COMPONENT-TYPE",
        )
        composition = Composition(
            name="Shared",
            title="A composition of the stub's name",
            ports=[CompositionPort(name="rBoundary", direction="require", interface="ISignal")],
        )
        return {stub.name: stub}, {composition.name: composition}

    def test_a_composition_typed_member_resolves_in_the_compositions_layer(self) -> None:
        from swcraft_tools.writer.arxml_generation import _component_type_path

        swc_map, composition_index = self._colliding_maps()
        assert _component_type_path("composition", "Shared", swc_map, composition_index, owner="Root") == (
            "/ComponentTypes/Shared",
            "COMPOSITION-SW-COMPONENT-TYPE",
        )

    def test_a_component_typed_member_resolves_in_the_swc_layer(self) -> None:
        from swcraft_tools.writer.arxml_generation import _component_type_path

        swc_map, composition_index = self._colliding_maps()
        assert _component_type_path("type", "Shared", swc_map, composition_index, owner="Root") == (
            "/Foreign/Pkg/ComponentTypes/Shared",
            "APPLICATION-SW-COMPONENT-TYPE",
        )

    def test_the_named_layer_not_carrying_the_name_is_a_refusal(self) -> None:
        """Never a fall-through to the other layer: the model said which one."""
        from swcraft_tools.writer.arxml_generation import _component_type_path

        swc_map, composition_index = self._colliding_maps()
        with pytest.raises(ValueError, match="places composition 'Absent', which is not declared in"):
            _component_type_path("composition", "Absent", swc_map, {}, owner="Root")
        with pytest.raises(ValueError, match="places component 'Absent', which is not declared in"):
            _component_type_path("type", "Absent", {}, composition_index, owner="Root")

    def test_declared_ports_reads_the_layer_the_field_names(self) -> None:
        """A delegation's inner port is looked up on the member's declared layer."""
        from swcraft_tools.writer.arxml_generation import _declared_ports

        swc_map, composition_index = self._colliding_maps()
        assert list(_declared_ports("composition", "Shared", swc_map, composition_index)) == ["rBoundary"]
        assert _declared_ports("type", "Shared", swc_map, composition_index) == {}


class TestAnExternalStubIsNotHeldToNamesNothingEmits:
    """A stub emits no ``_Implementation``, so none is reserved for it.

    ``arxml_import.registry.stub_name`` keeps a stub's leaf whole up to the limit; charging it the two
    generated forms would make such an import produce a project ``generate arxml`` refuses. Not reached: the
    stub's own declared name, which is still checked.
    """

    #: 120 legal characters: with ``_Implementation`` appended it is 135, over the
    #: 128-character AUTOSAR limit, and with ``_InternalBehavior`` 138.
    LONG = "S" + "t" * 119

    def _renamed_stub(self, tmp_path: Path) -> Path:
        model_root = _copy_compositions_model(tmp_path)
        for relative in (
            "swc/external/ExternalStub.yaml",
            "compositions/Sub.yaml",
            "requirements/REQ_COMPOSITIONS_001.yaml",
        ):
            path = model_root / relative
            path.write_text(
                path.read_text(encoding="utf-8").replace("ExternalStub", self.LONG),
                encoding="utf-8",
                newline="\n",
            )
        (model_root / "swc" / "external" / "ExternalStub.yaml").rename(
            model_root / "swc" / "external" / f"{self.LONG}.yaml"
        )
        return model_root

    def test_a_120_character_stub_name_generates(self, tmp_path: Path) -> None:
        output = tmp_path / "model.arxml"
        generate_arxml(str(self._renamed_stub(tmp_path)), str(output))
        text = output.read_text(encoding="utf-8")
        assert f"/Other/Project/SupplierPlatforms/Bsw/{self.LONG}" in text
        assert f"/ComponentTypes/{self.LONG}" not in text

    def test_check_model_agrees(self, tmp_path: Path) -> None:
        """The reporting twin: neither guard charges a stub a name nothing emits."""
        result = check_cross_layer_consistency(str(self._renamed_stub(tmp_path)))
        assert result.consistent, result.issues

    def test_a_component_named_after_a_stubs_implementation_is_a_free_name(self, tmp_path: Path) -> None:
        """No ``ExternalStub_Implementation`` is emitted, so the name collides with nothing."""
        model_root = _copy_compositions_model(tmp_path)
        source = model_root / "swc" / "core" / "Gamma.yaml"
        (model_root / "swc" / "core" / "ExternalStub_Implementation.yaml").write_text(
            source.read_text(encoding="utf-8").replace("name: Gamma", "name: ExternalStub_Implementation"),
            encoding="utf-8",
            newline="\n",
        )
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))
        assert "/ComponentTypes/ExternalStub_Implementation" in output.read_text(encoding="utf-8")


class TestTheDelegations:
    """Each delegation wired to the right instance and port, element by element, against the measured shape."""

    def test_the_child_sequence_is_the_measured_one(self, fixture_document: ET.Element) -> None:
        """``SHORT-NAME, INNER-PORT-IREF, OUTER-PORT-REF`` — the schema's sequence."""
        for name in ("Sub_pSignalOut_Alpha", "Sub_rTrigger_Gamma", "Sink_rSignalIn_Consumer"):
            node = _named_child(fixture_document, "DELEGATION-SW-CONNECTOR", name)
            # A declared description is a DESC after the SHORT-NAME.
            children = [_local(child.tag) for child in node]
            assert [tag for tag in children if tag != "DESC"] == [
                "SHORT-NAME",
                "INNER-PORT-IREF",
                "OUTER-PORT-REF",
            ], name
            assert children[1:2] in (["DESC"], ["INNER-PORT-IREF"]), name

    def test_the_provide_side_delegation_is_wired_element_by_element(self, fixture_document: ET.Element) -> None:
        """The P variant, through the member whose name differs from its type."""
        node = _named_child(fixture_document, "DELEGATION-SW-CONNECTOR", "Sub_pSignalOut_Alpha")
        inner = node.find(f"{AR_NS}INNER-PORT-IREF")
        assert [_local(child.tag) for child in inner] == ["P-PORT-IN-COMPOSITION-INSTANCE-REF"]
        instance = inner[0]
        assert [_local(child.tag) for child in instance] == ["CONTEXT-COMPONENT-REF", "TARGET-P-PORT-REF"]
        assert _reference(instance, "CONTEXT-COMPONENT-REF") == (
            "SW-COMPONENT-PROTOTYPE",
            "/ComponentTypes/Sub/Alpha",
        )
        assert _reference(instance, "TARGET-P-PORT-REF") == ("P-PORT-PROTOTYPE", "/ComponentTypes/Beta/pOut")
        assert _reference(node, "OUTER-PORT-REF") == ("P-PORT-PROTOTYPE", "/ComponentTypes/Sub/pSignalOut")

    def test_the_require_side_delegation_is_wired_element_by_element(self, fixture_document: ET.Element) -> None:
        node = _named_child(fixture_document, "DELEGATION-SW-CONNECTOR", "Sub_rTrigger_Gamma")
        inner = node.find(f"{AR_NS}INNER-PORT-IREF")
        assert [_local(child.tag) for child in inner] == ["R-PORT-IN-COMPOSITION-INSTANCE-REF"]
        instance = inner[0]
        assert _reference(instance, "CONTEXT-COMPONENT-REF") == (
            "SW-COMPONENT-PROTOTYPE",
            "/ComponentTypes/Sub/Gamma",
        )
        assert _reference(instance, "TARGET-R-PORT-REF") == ("R-PORT-PROTOTYPE", "/ComponentTypes/Gamma/rTrig")
        assert _reference(node, "OUTER-PORT-REF") == ("R-PORT-PROTOTYPE", "/ComponentTypes/Sub/rTrigger")

    def test_the_third_delegation_is_wired_at_the_second_level(self, fixture_document: ET.Element) -> None:
        node = _named_child(fixture_document, "DELEGATION-SW-CONNECTOR", "Sink_rSignalIn_Consumer")
        instance = node.find(f"{AR_NS}INNER-PORT-IREF")[0]
        assert _reference(instance, "CONTEXT-COMPONENT-REF") == (
            "SW-COMPONENT-PROTOTYPE",
            "/ComponentTypes/Sink/Consumer",
        )
        assert _reference(instance, "TARGET-R-PORT-REF") == ("R-PORT-PROTOTYPE", "/ComponentTypes/Consumer/rIn")
        assert _reference(node, "OUTER-PORT-REF") == ("R-PORT-PROTOTYPE", "/ComponentTypes/Sink/rSignalIn")

    def test_the_outer_dest_always_matches_the_inner_side(self, fixture_document: ET.Element) -> None:
        """True of every delegation the writer emits, and asserted rather than assumed."""
        for node in fixture_document.iter(f"{AR_NS}DELEGATION-SW-CONNECTOR"):
            instance = node.find(f"{AR_NS}INNER-PORT-IREF")[0]
            letter = _local(instance.tag)[0]
            assert node.find(f"{AR_NS}OUTER-PORT-REF").get("DEST") == f"{letter}-PORT-PROTOTYPE"

    def test_each_delegation_is_emitted_under_its_own_composition(self, fixture_document: ET.Element) -> None:
        placed = {
            _short_name(composition): sorted(
                _short_name(node) for node in composition.iter(f"{AR_NS}DELEGATION-SW-CONNECTOR")
            )
            for composition in fixture_document.iter(f"{AR_NS}COMPOSITION-SW-COMPONENT-TYPE")
        }
        assert placed == {
            "Root": [],
            "Sink": ["Sink_rSignalIn_Consumer"],
            "Sub": ["Sub_pSignalOut_Alpha", "Sub_rTrigger_Gamma"],
        }


class TestTheAssemblyConnectorsAcrossTwoLevels:
    """The right prototype, under the right composition."""

    def test_a_connector_naming_no_composition_is_drawn_inside_the_root(self, fixture_document: ET.Element) -> None:
        """A connector's ``None`` composition means the root, resolved where the model is in hand."""
        assert _short_name(_named_child(_composition(fixture_document, "Root"), "ASSEMBLY-SW-CONNECTOR", "root_link"))

    def test_a_connector_naming_its_composition_is_drawn_inside_that_one(self, fixture_document: ET.Element) -> None:
        placed = {
            _short_name(composition): sorted(
                _short_name(node) for node in composition.iter(f"{AR_NS}ASSEMBLY-SW-CONNECTOR")
            )
            for composition in fixture_document.iter(f"{AR_NS}COMPOSITION-SW-COMPONENT-TYPE")
        }
        assert placed == {"Root": ["root_link"], "Sink": [], "Sub": ["sub_link"]}

    def test_two_composition_typed_endpoints_resolve_to_composition_prototypes(
        self, fixture_document: ET.Element
    ) -> None:
        """A top-level shape AUTOSAR allows: the root's assembly connectors join two composition prototypes."""
        node = _named_child(_composition(fixture_document, "Root"), "ASSEMBLY-SW-CONNECTOR", "root_link")
        provider = node.find(f"{AR_NS}PROVIDER-IREF")
        requester = node.find(f"{AR_NS}REQUESTER-IREF")
        assert _reference(provider, "CONTEXT-COMPONENT-REF") == (
            "SW-COMPONENT-PROTOTYPE",
            "/ComponentTypes/Root/Sub",
        )
        assert _reference(provider, "TARGET-P-PORT-REF") == ("P-PORT-PROTOTYPE", "/ComponentTypes/Sub/pSignalOut")
        assert _reference(requester, "CONTEXT-COMPONENT-REF") == (
            "SW-COMPONENT-PROTOTYPE",
            "/ComponentTypes/Root/Sink",
        )
        assert _reference(requester, "TARGET-R-PORT-REF") == ("R-PORT-PROTOTYPE", "/ComponentTypes/Sink/rSignalIn")

    def test_an_endpoint_naming_a_type_resolves_to_the_member_of_that_type(self, fixture_document: ET.Element) -> None:
        """The wrong-prototype failure, at the connector: an endpoint naming the type resolves to its member.

        ``.../Sub/Beta``, spelled from the endpoint's own value, would resolve to nothing and pass the XSD.
        """
        node = _named_child(_composition(fixture_document, "Sub"), "ASSEMBLY-SW-CONNECTOR", "sub_link")
        provider = node.find(f"{AR_NS}PROVIDER-IREF")
        assert _reference(provider, "CONTEXT-COMPONENT-REF") == (
            "SW-COMPONENT-PROTOTYPE",
            "/ComponentTypes/Sub/Alpha",
        )
        assert _reference(provider, "TARGET-P-PORT-REF") == ("P-PORT-PROTOTYPE", "/ComponentTypes/Beta/pInner")

    def test_assembly_connectors_precede_delegations_in_one_connectors_element(
        self, fixture_document: ET.Element
    ) -> None:
        """Assembly connectors precede delegations: a chosen order, as AUTOSAR fixes none between the two kinds."""
        connectors = _composition(fixture_document, "Sub").find(f"{AR_NS}CONNECTORS")
        assert [_local(child.tag) for child in connectors] == [
            "ASSEMBLY-SW-CONNECTOR",
            "DELEGATION-SW-CONNECTOR",
            "DELEGATION-SW-CONNECTOR",
        ]


class TestAFreshlyInitedProjectEmitsItsRoot:
    """``swcraft init`` scaffolds a root with an empty ``PORTS`` and no ``COMPONENTS``, and the writer emits it."""

    def test_the_root_is_emitted_with_no_members(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = tmp_path / "fresh"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(project)])
        main()

        output = tmp_path / "fresh.arxml"
        generate_arxml(str(project / "model"), str(output))
        root = ET.fromstring(output.read_text(encoding="utf-8"))

        composition = _composition(root, "Root")
        # The template's title and description are written.
        assert [_local(child.tag) for child in composition] == ["SHORT-NAME", "LONG-NAME", "DESC", "PORTS"]
        assert list(composition.find(f"{AR_NS}PORTS")) == []

    def test_the_scaffolded_root_is_the_one_the_template_ships(self, tmp_path: Path) -> None:
        """The `init` scaffold and the committed template copy are one file."""
        template = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "templates"
        shipped = template / "vehicle-architecture" / "model" / "compositions" / "Root.yaml"
        assert shipped.is_file()


class TestARefusedHierarchyLeavesNoFile:
    """A refused hierarchy leaves no output file.

    Several refusals are raised inside an emitter, mid-emission; that the document is built in memory and
    ``save()`` runs last is asserted rather than argued, because it depends on emission order. Each refusal
    names the ``check-model`` rule that reports the same condition.
    """

    def _refuses(self, tmp_path: Path, model_root: Path, message: str) -> None:
        output = tmp_path / "never-written.arxml"
        with pytest.raises(ValueError, match=message):
            generate_arxml(str(model_root), str(output))
        assert not output.exists()

    def test_a_member_of_an_undeclared_type(self, tmp_path: Path) -> None:
        """``rule:composition.member-unknown-type``'s writer half.

        Retargeted at ``Ext``, which no connector touches: a connected member makes the connector gate raise first.
        """
        model_root = _copy_compositions_model(tmp_path)
        path = model_root / "compositions" / "Sub.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("type: ExternalStub", "type: Absent"),
            encoding="utf-8",
            newline="\n",
        )
        self._refuses(tmp_path, model_root, "places component 'Absent', which is not declared in")

    def test_a_delegation_naming_a_port_the_members_type_does_not_declare(self, tmp_path: Path) -> None:
        """``rule:composition.delegation-unknown-port``'s writer half."""
        model_root = _copy_compositions_model(tmp_path)
        path = model_root / "compositions" / "Sink.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("member_port: rIn", "member_port: rAbsent"),
            encoding="utf-8",
            newline="\n",
        )
        self._refuses(tmp_path, model_root, "names port 'rAbsent' on member 'Consumer'")

    def test_a_connector_naming_a_composition_that_is_not_declared(self, tmp_path: Path) -> None:
        """``rule:connector.unknown-composition``'s writer half.

        With connectors, ``_check_connectors`` raises the validator's wording first (one condition, one
        message); the emitter keeps its own raise for the connector-less path.
        """
        model_root = _copy_compositions_model(tmp_path)
        path = model_root / "connectors" / "sub_link.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("composition: Sub", "composition: Absent"),
            encoding="utf-8",
            newline="\n",
        )
        self._refuses(tmp_path, model_root, "names composition 'Absent', which does not exist")

    def test_a_delegation_naming_a_member_the_composition_does_not_declare(self, tmp_path: Path) -> None:
        """``rule:composition.delegation-unknown-member``'s writer half.

        ``generate_arxml`` never calls ``check_composition_consistency``, so this refusal and the one below are
        the sole write-path barriers against a delegation wired to the wrong instance or port.
        """
        model_root = _copy_compositions_model(tmp_path)
        path = model_root / "compositions" / "Sub.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("member: Alpha", "member: Absent"),
            encoding="utf-8",
            newline="\n",
        )
        self._refuses(tmp_path, model_root, "names member 'Absent', which that composition does not declare")

    def test_a_delegation_whose_two_ports_face_opposite_ways(self, tmp_path: Path) -> None:
        """``rule:composition.delegation-port-direction-mismatch``'s writer half.

        Without this guard a direction mismatch emits ``TARGET-R-PORT-REF DEST="R-PORT-PROTOTYPE"`` at a
        P-PORT and passes the 4.2.2 schema. Both ports are on ``ITrigger`` (a pure direction mismatch), so the
        interface guard cannot fire and a narrowed direction guard would fail here.
        """
        model_root = _copy_compositions_model(tmp_path)
        path = model_root / "compositions" / "Sub.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "    member: Gamma\n    member_port: rTrig", "    member: Alpha\n    member_port: pInner"
            ),
            encoding="utf-8",
            newline="\n",
        )
        self._refuses(tmp_path, model_root, "must declare the same direction")

    def test_a_delegation_whose_two_ports_name_different_interfaces(self, tmp_path: Path) -> None:
        """``rule:composition.delegation-interface-mismatch``'s writer half.

        ``check_composition_consistency`` does not cover it on the write path, and the XSD cannot express
        it: an ``ISignal`` boundary port delegated onto an ``ITrigger`` member port validates. Both ports
        provide, so the direction guard cannot fire first and ``[constr_1070]`` 2(a) applies; the message
        is ``check_composition_consistency``'s sentence, to the byte.
        """
        model_root = _copy_compositions_model(tmp_path)
        path = model_root / "compositions" / "Sub.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("    member_port: pOut", "    member_port: pInner"),
            encoding="utf-8",
            newline="\n",
        )
        self._refuses(tmp_path, model_root, "no data element of 'ITrigger' has a counterpart in 'ISignal'")

    def test_a_delegation_whose_two_interfaces_differ_only_in_a_members_type(self, tmp_path: Path) -> None:
        """The **type** half of the writer's delegation gate.

        With the data type map empty, two interfaces whose one member differs only in its resolved type are
        accepted; ``ITriggerWide`` makes this case fail the moment the map stops arriving.
        """
        model_root = _copy_compositions_model(tmp_path)
        (model_element_path(model_root, "uint16_base", kind="base_type")).write_text(
            "name: uint16_base\nkind: base_type\nsize: 16\nencoding: unsigned\nnative_declaration: uint16\n",
            encoding="utf-8",
            newline="\n",
        )
        (model_element_path(model_root, "uint16", kind="value_type")).write_text(
            "name: uint16\nkind: value_type\nbase: uint16_base\n",
            encoding="utf-8",
            newline="\n",
        )
        (model_root / "interfaces" / "ITriggerWide.yaml").write_text(
            "name: ITriggerWide\nkind: sender_receiver\ndata_elements:\n"
            "  - name: trigger\n    implementation_type: uint16\n",
            encoding="utf-8",
            newline="\n",
        )
        path = model_root / "swc" / "core" / "Gamma.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "  - name: rTrig\n    direction: require\n    interface: ITrigger",
                "  - name: rTrig\n    direction: require\n    interface: ITriggerWide",
            ),
            encoding="utf-8",
            newline="\n",
        )
        self._refuses(tmp_path, model_root, "type 'uint16' vs 'uint8' — base type size 16 vs 8")

    def test_a_delegation_that_mismatches_both_ways_is_refused_by_the_direction_guard(self, tmp_path: Path) -> None:
        """The guard **order**: a mismatch on both axes is refused on the direction, the guard that runs first.

        A case of its own, so neither single-axis case above becomes the other's oracle.
        """
        model_root = _copy_compositions_model(tmp_path)
        path = model_root / "compositions" / "Sub.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "    member: Alpha\n    member_port: pOut", "    member: Gamma\n    member_port: rTrig"
            ),
            encoding="utf-8",
            newline="\n",
        )
        self._refuses(tmp_path, model_root, "must declare the same direction")

    def test_a_connector_that_names_no_composition_when_no_root_is_declared(self, tmp_path: Path) -> None:
        """``rule:composition.no-root``'s writer half, in the one case the writer needs it.

        The writer resolves the root only for a connector that leaves ``composition`` unset; ``check-model``
        holds the rule unconditionally, deliberately.
        """
        model_root = _copy_compositions_model(tmp_path)
        path = model_root / "compositions" / "Root.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("root: true", "root: false"),
            encoding="utf-8",
            newline="\n",
        )
        self._refuses(tmp_path, model_root, "declares 'root: true'")


class TestADelegationTheStandardJoinsIsEmitted:
    """The writer's delegation twin judges by the compatibility predicate, as the gate does.

    A delegation between two *compatible*, differently named interfaces is emitted.
    """

    def test_a_require_delegation_onto_a_subset_interface_is_emitted(self, tmp_path: Path) -> None:
        """``[constr_1070]``/``[constr_1080]`` 1(a) on the write path: a require delegation onto a subset interface.

        ``Gamma.rInner`` is left alone, so the only thing this case changes is what the delegation joins.
        """
        model_root = _copy_compositions_model(tmp_path)
        (model_root / "interfaces" / "ITriggerBasic.yaml").write_text(
            "name: ITriggerBasic\nkind: sender_receiver\ndata_elements:\n"
            "  - name: trigger\n    implementation_type: uint8\n",
            encoding="utf-8",
            newline="\n",
        )
        path = model_root / "swc" / "core" / "Gamma.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "  - name: rTrig\n    direction: require\n    interface: ITrigger",
                "  - name: rTrig\n    direction: require\n    interface: ITriggerBasic",
            ),
            encoding="utf-8",
            newline="\n",
        )

        # The premise: `check-model` already accepts it. The writer refusing what the gate accepts
        # would be a disagreement, so the two are asserted together.
        assert check_cross_layer_consistency(str(model_root)).consistent

        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))
        document = ET.fromstring(output.read_text(encoding="utf-8"))

        delegation = _named_child(_composition(document, "Sub"), "DELEGATION-SW-CONNECTOR", "Sub_rTrigger_Gamma")
        assert _reference(delegation, "OUTER-PORT-REF") == ("R-PORT-PROTOTYPE", "/ComponentTypes/Sub/rTrigger")
        assert _reference(delegation, "TARGET-R-PORT-REF") == ("R-PORT-PROTOTYPE", "/ComponentTypes/Gamma/rTrig")


class TestTheCompatibleInterfaceRootIsEmitted:
    """``fixtures/compatible_interfaces/model``: four shapes the standard joins, across differently named interfaces.

    The structural half; the conformance half is ``TestTheCompatibleInterfaceRootConforms`` in
    ``test_arxml_xsd_conformance.py``.
    """

    def test_the_root_is_accepted_by_check_model(self) -> None:
        """The premise: every assertion below is only about the writer over a model a user could author."""
        result = check_cross_layer_consistency(str(COMPATIBLE_INTERFACES_MODEL_ROOT))

        assert result.consistent, result.issues

    def test_the_two_compatible_interface_roots_differ_only_in_the_hierarchy(self) -> None:
        """This root and the validator's are one tree plus a hierarchy, byte for byte.

        They stay apart because the validator's root is a perturbation base a hierarchy would move, and a
        reworded shared file would hide the files that really differ. Not reached: whether either root is
        right. The requirement id is the one token compared as equal, as each tree declares its own.
        """
        left = {
            path.relative_to(INTERFACE_COMPATIBILITY_MODEL_ROOT).as_posix()
            for path in INTERFACE_COMPATIBILITY_MODEL_ROOT.rglob("*.yaml")
        }
        right = {
            path.relative_to(COMPATIBLE_INTERFACES_MODEL_ROOT).as_posix()
            for path in COMPATIBLE_INTERFACES_MODEL_ROOT.rglob("*.yaml")
        }

        assert sorted(left ^ right) == [
            "compositions/Edge.yaml",
            "requirements/REQ_COMPATIBILITY_001.yaml",
            "requirements/REQ_COMPATIBLE_001.yaml",
            "swc/core/Relay.yaml",
        ]

        def spelled_as_this_root(relative: str) -> bytes:
            text = (INTERFACE_COMPATIBILITY_MODEL_ROOT / relative).read_bytes()
            return text.replace(b"REQ_COMPATIBILITY_001", b"REQ_COMPATIBLE_001")

        differing = sorted(
            relative
            for relative in left & right
            if spelled_as_this_root(relative) != (COMPATIBLE_INTERFACES_MODEL_ROOT / relative).read_bytes()
        )

        assert differing == ["compositions/Root.yaml"], (
            "a file the two roots share differs; edit a shared file in both trees or in neither"
        )

    def test_every_pair_really_names_two_interfaces(self) -> None:
        """Every pair names two interfaces, so the cases below do not pass through the same-name short-circuit."""
        compositions = {c.name: c for c in read_compositions(str(COMPATIBLE_INTERFACES_MODEL_ROOT))}
        edge = compositions["Edge"]
        outer = {port.name: port.interface for port in edge.ports}

        assert outer == {"pStatusEdge": "IStatusBasic", "rCommandEdge": "ICommandFull"}
        assert [(d.port, d.member_port) for d in edge.delegations] == [
            ("pStatusEdge", "pStatusOut"),
            ("rCommandEdge", "rCommandIn"),
        ]

    def test_both_assembly_connectors_name_both_ports(self, compatible_document: ET.Element) -> None:
        """The two connectors the gate accepts are emitted, each naming its own two ports."""
        root = _composition(compatible_document, "Root")
        emitted = {}
        for name, provider, requester in (
            ("status_link", "/ComponentTypes/Sensor/pStatus", "/ComponentTypes/Display/rStatus"),
            ("command_link", "/ComponentTypes/Actuator/pCommand", "/ComponentTypes/Controller/rCommand"),
        ):
            connector = _named_child(root, "ASSEMBLY-SW-CONNECTOR", name)
            emitted[name] = (
                _reference(connector.find(f"{AR_NS}PROVIDER-IREF"), "TARGET-P-PORT-REF"),
                _reference(connector.find(f"{AR_NS}REQUESTER-IREF"), "TARGET-R-PORT-REF"),
            )
            assert emitted[name] == (
                ("P-PORT-PROTOTYPE", provider),
                ("R-PORT-PROTOTYPE", requester),
            )

    def test_both_delegations_are_emitted_in_the_variant_their_direction_names(
        self, compatible_document: ET.Element
    ) -> None:
        """One delegation per clause, and the ``INNER-PORT-IREF`` variant each direction takes."""
        edge = _composition(compatible_document, "Edge")

        provide = _named_child(edge, "DELEGATION-SW-CONNECTOR", "Edge_pStatusEdge_Relay")
        assert _reference(provide, "OUTER-PORT-REF") == ("P-PORT-PROTOTYPE", "/ComponentTypes/Edge/pStatusEdge")
        assert _reference(provide, "TARGET-P-PORT-REF") == ("P-PORT-PROTOTYPE", "/ComponentTypes/Relay/pStatusOut")
        assert [_local(child.tag) for child in provide.find(f"{AR_NS}INNER-PORT-IREF")] == [
            "P-PORT-IN-COMPOSITION-INSTANCE-REF"
        ]

        require = _named_child(edge, "DELEGATION-SW-CONNECTOR", "Edge_rCommandEdge_Relay")
        assert _reference(require, "OUTER-PORT-REF") == ("R-PORT-PROTOTYPE", "/ComponentTypes/Edge/rCommandEdge")
        assert _reference(require, "TARGET-R-PORT-REF") == ("R-PORT-PROTOTYPE", "/ComponentTypes/Relay/rCommandIn")
        assert [_local(child.tag) for child in require.find(f"{AR_NS}INNER-PORT-IREF")] == [
            "R-PORT-IN-COMPOSITION-INSTANCE-REF"
        ]
