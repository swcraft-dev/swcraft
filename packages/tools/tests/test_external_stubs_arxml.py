"""The two stub kinds at ``generate arxml``.

The writer emits no element for a stub, and every reference naming one carries the
stub's ``external_ref`` as its text and its ``external_dest`` as its ``DEST``, verbatim:

* a port on an interface stub (SWC or composition boundary, provide or require, any
  interface kind or the abstract ``PORT-INTERFACE``) carries a ``…-INTERFACE-TREF`` to
  the source and no ``…-COM-SPECS`` wrapper while the stub lists no member;
* a listed member is referenced as ``{external_ref}/{name}`` by an operation event, a
  call point, a read and a write, and a client-server or sender-receiver stub port's
  com-specs are derived from the list as a declared interface's are (a mode-switch or
  abstract stub's port writes none);
* a ``TYPE-TREF`` or ``IMPLEMENTATION-DATA-TYPE-REF`` to a type stub carries its pair;
* a connector and a delegation across a stub port are emitted when the compatibility
  predicate lets them, and refused when a concrete ``DEST`` names another kind.

The writer refuses, with nothing written, what ``check-model`` reports: a malformed stub
(even one nothing names), an access through a stub port naming a member the stub does not
list, and an ``init`` on an element typed by a stub directly or through a reference chain.
Every refusal is asserted from a fresh output directory. Stubs emit no ``SHORT-NAME``,
while model identity holds.

What it does **not** reach: the XSD (``test_arxml_xsd_conformance.py``, run by
``test-xsd``), the component export, the importer, and any model shape outside the
ones written below; this is example-based evidence. A stub-free model's bytes are
``test_external_stubs_byte_identity.py``'s.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import model_element_storage_name, write_model_element
from external_stubs_support import (
    BOUNDARY_PORTS,
    CONSUMER,
    INTERFACE_STUBS,
    LISTED_DATA_ELEMENTS,
    LISTED_OPERATIONS,
    PROVIDER,
    ROOT,
    SR_INTERFACE,
    STUB_PORTS,
    TYPE_STUB,
    baseline_model_root,
    change,
    external_stubs_model_root,
    interface_stub,
    listed_stub_model_root,
    put,
    set_port,
    type_stub,
)
from lxml import etree
from swcraft_tools.validator import check_cross_layer_consistency, data_description_package_path
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.arxml_xml import ar_qname, short_name_of

_STUB_REFS = {name: (ref, dest) for name, ref, dest in (*INTERFACE_STUBS, TYPE_STUB)}


# Helpers


def _rules(model_root: Path) -> list[str]:
    return [record.rule for record in check_cross_layer_consistency(str(model_root)).records]


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


def _package(root: etree._Element, name: str) -> etree._Element:
    (package,) = [p for p in root.iter(ar_qname("AR-PACKAGE")) if short_name_of(p) == name]
    return package


def _port(root: etree._Element, owner: str, port: str) -> etree._Element:
    owners = [
        element
        for tag in ("APPLICATION-SW-COMPONENT-TYPE", "COMPOSITION-SW-COMPONENT-TYPE")
        for element in root.iter(ar_qname(tag))
        if short_name_of(element) == owner
    ]
    (component,) = owners
    (node,) = [
        element
        for tag in ("P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE")
        for element in component.iter(ar_qname(tag))
        if short_name_of(element) == port
    ]
    return node


def _typed(root: etree._Element, prototype_tag: str, name: str) -> tuple[str | None, str | None]:
    (node,) = [e for e in root.iter(ar_qname(prototype_tag)) if short_name_of(e) == name]
    reference = node.find(ar_qname("TYPE-TREF"))
    return reference.get("DEST"), reference.text


@pytest.fixture()
def stub_model(tmp_path: Path) -> Path:
    root = external_stubs_model_root(tmp_path / "project")
    assert _rules(root) == [], "the stub model must be one check-model accepts"
    return root


@pytest.fixture()
def generated(stub_model: Path, tmp_path: Path) -> etree._Element:
    return _generate(stub_model, tmp_path)


@pytest.fixture()
def baseline(tmp_path: Path) -> Path:
    root = baseline_model_root(tmp_path / "baseline")
    assert _rules(root) == []
    return root


# Ports


class TestAPortOnAStub:
    @pytest.mark.parametrize(
        ("owner", "port", "direction", "interface"), STUB_PORTS, ids=[f"{o}.{p}" for o, p, _d, _i in STUB_PORTS]
    )
    def test_the_interface_reference_is_the_stubs_pair_verbatim(
        self, generated: etree._Element, owner: str, port: str, direction: str, interface: str
    ) -> None:
        node = _port(generated, owner, port)
        tag = "PROVIDED-INTERFACE-TREF" if direction == "provide" else "REQUIRED-INTERFACE-TREF"
        reference = node.find(ar_qname(tag))

        assert node.tag == ar_qname("P-PORT-PROTOTYPE" if direction == "provide" else "R-PORT-PROTOTYPE")
        assert (reference.get("DEST"), reference.text) == tuple(reversed(_STUB_REFS[interface]))

    @pytest.mark.parametrize(
        ("owner", "port", "direction", "interface"), STUB_PORTS, ids=[f"{o}.{p}" for o, p, _d, _i in STUB_PORTS]
    )
    def test_no_com_specs_wrapper_is_emitted(
        self, generated: etree._Element, owner: str, port: str, direction: str, interface: str
    ) -> None:
        node = _port(generated, owner, port)

        assert node.find(ar_qname("PROVIDED-COM-SPECS")) is None
        assert node.find(ar_qname("REQUIRED-COM-SPECS")) is None

    @pytest.mark.parametrize(
        ("port", "direction", "interface"), BOUNDARY_PORTS, ids=[p for p, _d, _i in BOUNDARY_PORTS]
    )
    def test_a_composition_boundary_port_carries_the_pair_too(
        self, generated: etree._Element, port: str, direction: str, interface: str
    ) -> None:
        node = _port(generated, "Root", port)
        tag = "PROVIDED-INTERFACE-TREF" if direction == "provide" else "REQUIRED-INTERFACE-TREF"
        reference = node.find(ar_qname(tag))

        assert (reference.get("DEST"), reference.text) == tuple(reversed(_STUB_REFS[interface]))
        assert not [child for child in node if child.tag.endswith("-COM-SPECS")]

    def test_a_declared_port_keeps_its_com_specs_and_package_reference(self, generated: etree._Element) -> None:
        node = _port(generated, "ExampleSWC", "pOut")
        reference = node.find(ar_qname("PROVIDED-INTERFACE-TREF"))

        assert node.find(ar_qname("PROVIDED-COM-SPECS")) is not None
        assert (reference.get("DEST"), reference.text) == ("SENDER-RECEIVER-INTERFACE", "/PortInterfaces/IExample")

    def test_no_stub_port_has_a_port_api_option(self, generated: etree._Element) -> None:
        """The writer writes no port API option, for a stub port either."""
        assert list(generated.iter(ar_qname("PORT-API-OPTIONS"))) == []
        for owner, port, _direction, _interface in STUB_PORTS:
            assert _port(generated, owner, port) is not None


# A stub that lists its members

_CS_REF = _STUB_REFS["IForeignCS"][0]
_SR_REF = _STUB_REFS["IForeignSR"][0]

#: The silent sender-receiver attributes, in the XSD's order, as a declared interface's com-spec writes them.
_SENDER_SHAPE = [("HANDLE-OUT-OF-RANGE", "NONE"), ("USES-END-TO-END-PROTECTION", "false")]
_RECEIVER_SHAPE = [
    *_SENDER_SHAPE,
    ("ALIVE-TIMEOUT", "0"),
    ("ENABLE-UPDATE", "false"),
    ("HANDLE-NEVER-RECEIVED", "false"),
    ("HANDLE-TIMEOUT-TYPE", "NONE"),
]


@pytest.fixture()
def listed_model(tmp_path: Path) -> Path:
    root = listed_stub_model_root(tmp_path / "listed")
    assert _rules(root) == [], "the listed stub model must be one check-model accepts"
    return root


def _reference(node: etree._Element, tag: str) -> tuple[str | None, str | None]:
    (reference,) = node.iter(ar_qname(tag))
    return reference.get("DEST"), reference.text


def _com_specs(port: etree._Element) -> list[etree._Element]:
    wrappers = [child for child in port if child.tag.endswith("-COM-SPECS")]
    assert len(wrappers) <= 1
    return list(wrappers[0]) if wrappers else []


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname


def _shape(spec: etree._Element) -> list[tuple[str, str | None]]:
    """Every child of *spec* but its member reference, as ``(tag, text)``."""
    return [(_local(child), child.text) for child in spec if not _local(child).endswith("-REF")]


class TestAListedMemberIsReferencedUnderTheStubsPath:
    """A listed member is ``{external_ref}/{name}``, verbatim, with the ``DEST`` a declared one gets."""

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_operation_event(self, listed_model: Path, tmp_path: Path, version: str) -> None:
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(listed_model), str(output), version=version)
        (event,) = etree.parse(str(output)).getroot().iter(ar_qname("OPERATION-INVOKED-EVENT"))

        assert _reference(event, "TARGET-PROVIDED-OPERATION-REF") == ("CLIENT-SERVER-OPERATION", f"{_CS_REF}/Get")
        assert _reference(event, "CONTEXT-P-PORT-REF") == ("P-PORT-PROTOTYPE", "/ComponentTypes/ExampleSWC/pCs")

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_call_point(self, listed_model: Path, tmp_path: Path, version: str) -> None:
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(listed_model), str(output), version=version)
        (call,) = etree.parse(str(output)).getroot().iter(ar_qname("SYNCHRONOUS-SERVER-CALL-POINT"))

        assert short_name_of(call) == "Call_rCs_Set"
        assert _reference(call, "TARGET-REQUIRED-OPERATION-REF") == ("CLIENT-SERVER-OPERATION", f"{_CS_REF}/Set")

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_read_and_the_write(self, listed_model: Path, tmp_path: Path, version: str) -> None:
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(listed_model), str(output), version=version)
        accesses = {
            short_name_of(access): _reference(access, "TARGET-DATA-PROTOTYPE-REF")
            for access in etree.parse(str(output)).getroot().iter(ar_qname("VARIABLE-ACCESS"))
        }

        assert accesses == {
            "W_pSr_speed": ("VARIABLE-DATA-PROTOTYPE", f"{_SR_REF}/speed"),
            "R_rSr_angle": ("VARIABLE-DATA-PROTOTYPE", f"{_SR_REF}/angle"),
        }

    def test_a_stated_access_name_through_a_stub(self, listed_model: Path, tmp_path: Path) -> None:
        change(listed_model, CONSUMER, lambda d: d["runnables"][0]["calls"][0].update(name="CallSet"))
        assert _rules(listed_model) == []

        (call,) = _generate(listed_model, tmp_path).iter(ar_qname("SYNCHRONOUS-SERVER-CALL-POINT"))
        assert short_name_of(call) == "CallSet"
        assert _reference(call, "TARGET-REQUIRED-OPERATION-REF")[1] == f"{_CS_REF}/Set"

    def test_no_stub_member_path_is_written_under_a_package_of_this_document(
        self, listed_model: Path, tmp_path: Path
    ) -> None:
        """No reference anywhere names a member under the stub's own name in ``/PortInterfaces``."""
        root = _generate(listed_model, tmp_path)
        texts = [element.text or "" for element in root.iter() if _local(element).endswith("-REF")]

        assert not [text for text in texts if text.startswith("/PortInterfaces/IForeign")]
        assert len([text for text in texts if text.startswith((f"{_CS_REF}/", f"{_SR_REF}/"))]) == 16


class TestAStubPortsComSpecsAreDerivedFromTheList:
    """One com-spec per listed member, as a declared interface's, on a component or composition port."""

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_a_provide_port_on_the_client_server_stub(self, listed_model: Path, tmp_path: Path, version: str) -> None:
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(listed_model), str(output), version=version)
        specs = _com_specs(_port(etree.parse(str(output)).getroot(), "ExampleSWC", "pCs"))

        assert [_local(spec) for spec in specs] == ["SERVER-COM-SPEC", "SERVER-COM-SPEC"]
        assert [_reference(spec, "OPERATION-REF") for spec in specs] == [
            ("CLIENT-SERVER-OPERATION", f"{_CS_REF}/{name}") for name in LISTED_OPERATIONS
        ]
        assert [_shape(spec) for spec in specs] == [[("QUEUE-LENGTH", "1")]] * 2

    @pytest.mark.parametrize(("owner", "port"), [("ExampleConsumerSWC", "rCs"), ("Root", "rExtCs")])
    def test_a_require_port_on_the_client_server_stub(
        self, listed_model: Path, tmp_path: Path, owner: str, port: str
    ) -> None:
        specs = _com_specs(_port(_generate(listed_model, tmp_path), owner, port))

        assert [_local(spec) for spec in specs] == ["CLIENT-COM-SPEC", "CLIENT-COM-SPEC"]
        assert [_reference(spec, "OPERATION-REF")[1] for spec in specs] == [
            f"{_CS_REF}/{name}" for name in LISTED_OPERATIONS
        ]
        assert [_shape(spec) for spec in specs] == [[], []], "a client com-spec holds its OPERATION-REF alone"

    @pytest.mark.parametrize(("owner", "port"), [("ExampleSWC", "pSr"), ("Root", "pExtSr")])
    def test_a_provide_port_on_the_sender_receiver_stub(
        self, listed_model: Path, tmp_path: Path, owner: str, port: str
    ) -> None:
        specs = _com_specs(_port(_generate(listed_model, tmp_path), owner, port))

        assert [_local(spec) for spec in specs] == ["NONQUEUED-SENDER-COM-SPEC"] * 2
        assert [_reference(spec, "DATA-ELEMENT-REF") for spec in specs] == [
            ("VARIABLE-DATA-PROTOTYPE", f"{_SR_REF}/{name}") for name in LISTED_DATA_ELEMENTS
        ]
        assert [_shape(spec) for spec in specs] == [_SENDER_SHAPE] * 2, "no INIT-VALUE: a stub member has no type"

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_a_require_port_on_the_sender_receiver_stub(self, listed_model: Path, tmp_path: Path, version: str) -> None:
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(listed_model), str(output), version=version)
        specs = _com_specs(_port(etree.parse(str(output)).getroot(), "ExampleConsumerSWC", "rSr"))

        assert [_local(spec) for spec in specs] == ["NONQUEUED-RECEIVER-COM-SPEC"] * 2
        assert [_reference(spec, "DATA-ELEMENT-REF")[1] for spec in specs] == [
            f"{_SR_REF}/{name}" for name in LISTED_DATA_ELEMENTS
        ]
        assert [_shape(spec) for spec in specs] == [_RECEIVER_SHAPE] * 2

    def test_the_shape_is_the_declared_interfaces(self, listed_model: Path, tmp_path: Path) -> None:
        """The derivation is the declared path's: the stub's com-specs equal ``IExample``'s but for the reference."""
        root = _generate(listed_model, tmp_path)
        for (owner, declared), stub in ((("ExampleSWC", "pOut"), "pSr"), (("ExampleConsumerSWC", "rIn"), "rSr")):
            (reference,) = [
                spec
                for spec in _com_specs(_port(root, owner, declared))
                if spec.findtext(ar_qname("DATA-ELEMENT-REF")).endswith("/IExample/value")
            ]
            assert reference.findtext(ar_qname("INIT-VALUE")) is None, "the comparison needs a declared one with none"
            for spec in _com_specs(_port(root, owner, stub)):
                assert (_local(spec), _shape(spec)) == (_local(reference), _shape(reference))

    def test_a_com_specs_entry_states_its_attributes(self, listed_model: Path, tmp_path: Path) -> None:
        set_port(
            listed_model,
            CONSUMER,
            "rSr",
            com_specs=[{"data_element": "angle", "enable_update": True, "alive_timeout": 0.5}],
        )
        assert _rules(listed_model) == []

        speed, angle = _com_specs(_port(_generate(listed_model, tmp_path), "ExampleConsumerSWC", "rSr"))
        assert _shape(speed) == _RECEIVER_SHAPE
        assert dict(_shape(angle)) == {**dict(_RECEIVER_SHAPE), "ALIVE-TIMEOUT": "0.5", "ENABLE-UPDATE": "true"}
        assert [tag for tag, _text in _shape(angle)] == [tag for tag, _text in _RECEIVER_SHAPE]

    @pytest.mark.parametrize(("owner", "port"), [("ExampleSWC", "pMode"), ("ExampleConsumerSWC", "rMode")])
    def test_a_mode_switch_stub_port_writes_nothing(
        self, listed_model: Path, tmp_path: Path, owner: str, port: str
    ) -> None:
        assert _com_specs(_port(_generate(listed_model, tmp_path), owner, port)) == []

    def test_an_abstract_stub_port_writes_nothing(self, listed_model: Path, tmp_path: Path) -> None:
        assert _com_specs(_port(_generate(listed_model, tmp_path), "ExampleConsumerSWC", "rAny")) == []

    def test_an_empty_list_writes_no_wrapper_beside_a_listing_stub(self, listed_model: Path, tmp_path: Path) -> None:
        """The client-server arm removes its empty wrapper, as the sender-receiver arm always did."""
        change(listed_model, "interfaces/IForeignCS.yaml", lambda d: d.update(operations=[]))
        change(listed_model, PROVIDER, lambda d: d["runnables"].pop())
        change(listed_model, CONSUMER, lambda d: d["runnables"][0].pop("calls"))
        assert _rules(listed_model) == []

        root = _generate(listed_model, tmp_path)
        for owner, port in (("ExampleSWC", "pCs"), ("ExampleConsumerSWC", "rCs"), ("Root", "rExtCs")):
            assert not [child for child in _port(root, owner, port) if child.tag.endswith("-COM-SPECS")]
        assert len(_com_specs(_port(root, "ExampleSWC", "pSr"))) == 2

    def test_the_wrapper_precedes_the_interface_reference(self, listed_model: Path, tmp_path: Path) -> None:
        tags = [_local(child) for child in _port(_generate(listed_model, tmp_path), "ExampleSWC", "pCs")]

        assert tags[-2:] == ["PROVIDED-COM-SPECS", "PROVIDED-INTERFACE-TREF"]


class TestTheCompositionsOtherTwoBoundaryPorts:
    """A composition's other two boundary port shapes derive their com-specs from the list too.

    A provide port on the client-server stub (a server com-spec per operation) and a
    require port on the sender-receiver stub (a receiver com-spec per data element)."""

    @pytest.fixture()
    def boundary_model(self, listed_model: Path) -> Path:
        def more(document: dict[str, Any]) -> None:
            document["ports"] += [
                {"name": "pExtCs", "direction": "provide", "interface": "IForeignCS"},
                {"name": "rExtSr", "direction": "require", "interface": "IForeignSR"},
            ]
            document["delegations"] += [
                {"name": "Root_pExtCs", "port": "pExtCs", "member": "ExampleSWC", "member_port": "pCs"},
                {"name": "Root_rExtSr", "port": "rExtSr", "member": "ExampleConsumerSWC", "member_port": "rSr"},
            ]

        change(listed_model, ROOT, more)
        assert _rules(listed_model) == []
        return listed_model

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_a_provide_port_on_the_client_server_stub(self, boundary_model: Path, tmp_path: Path, version: str) -> None:
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(boundary_model), str(output), version=version)
        specs = _com_specs(_port(etree.parse(str(output)).getroot(), "Root", "pExtCs"))

        assert [_local(spec) for spec in specs] == ["SERVER-COM-SPEC"] * 2
        assert [_reference(spec, "OPERATION-REF")[1] for spec in specs] == [
            f"{_CS_REF}/{name}" for name in LISTED_OPERATIONS
        ]
        assert [_shape(spec) for spec in specs] == [[("QUEUE-LENGTH", "1")]] * 2

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_a_require_port_on_the_sender_receiver_stub(
        self, boundary_model: Path, tmp_path: Path, version: str
    ) -> None:
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(boundary_model), str(output), version=version)
        specs = _com_specs(_port(etree.parse(str(output)).getroot(), "Root", "rExtSr"))

        assert [_local(spec) for spec in specs] == ["NONQUEUED-RECEIVER-COM-SPEC"] * 2
        assert [_reference(spec, "DATA-ELEMENT-REF")[1] for spec in specs] == [
            f"{_SR_REF}/{name}" for name in LISTED_DATA_ELEMENTS
        ]
        assert [_shape(spec) for spec in specs] == [_RECEIVER_SHAPE] * 2


class TestAListedNameIsHeldToTheIdentifierRule:
    """Every ``identifier.declared`` refusal of ``check-model`` on a listed name is the writer's too.

    A listed name is the last segment of a member path, so the writer refuses what ``check-model`` reports.
    """

    @pytest.mark.parametrize(
        ("relative", "field", "names"),
        [
            ("interfaces/IForeignCS.yaml", "operations", [*LISTED_OPERATIONS, "bad-name"]),
            ("interfaces/IForeignSR.yaml", "data_elements", [*LISTED_DATA_ELEMENTS, "bad-name"]),
        ],
        ids=["operations", "data_elements"],
    )
    def test_a_listed_name_that_is_no_identifier_is_refused(
        self, listed_model: Path, tmp_path: Path, relative: str, field: str, names: list[str]
    ) -> None:
        change(listed_model, relative, lambda d: d.update({field: names}))

        assert _rules(listed_model) == ["identifier.declared"]
        stub = Path(relative).stem
        _refused(listed_model, tmp_path, f"External interface stub '{stub}'", "'bad-name'")


# Nothing is emitted for a stub


class TestNothingIsEmittedForAStub:
    def test_no_short_name_in_the_document_is_a_stub_name(self, generated: etree._Element) -> None:
        names = {short_name_of(element) for element in generated.iter() if short_name_of(element)}

        assert names.isdisjoint(_STUB_REFS)

    def test_port_interfaces_holds_the_declared_interfaces_alone(self, generated: etree._Element) -> None:
        package = _package(generated, "PortInterfaces")
        emitted = [short_name_of(e) for e in package.find(ar_qname("ELEMENTS"))]

        assert emitted == ["IDiagnosticService", "IExample"]

    def test_data_types_holds_no_stub(self, generated: etree._Element) -> None:
        package = _package(generated, "DataTypes")
        # Every layer package's elements; ``/DataTypes`` itself holds none.
        assert package.find(ar_qname("ELEMENTS")) is None
        emitted = {
            short_name_of(e)
            for layer in package.find(ar_qname("AR-PACKAGES"))
            for e in layer.find(ar_qname("ELEMENTS"))
        }

        assert TYPE_STUB[0] not in emitted
        assert "RefForeign_T" in emitted

    def test_an_unused_stub_of_each_kind_leaves_the_bytes_unchanged(self, baseline: Path, tmp_path: Path) -> None:
        before = tmp_path / "before" / "model.arxml"
        generate_arxml(str(baseline), str(before))
        interface_stub(baseline, *INTERFACE_STUBS[0])
        type_stub(baseline, *TYPE_STUB)
        assert _rules(baseline) == []
        after = tmp_path / "after" / "model.arxml"
        generate_arxml(str(baseline), str(after))

        assert after.read_bytes() == before.read_bytes()


# Type references


class TestATypeReferenceToAStub:
    def test_a_data_element_typed_by_the_stub(self, generated: etree._Element) -> None:
        assert _typed(generated, "VARIABLE-DATA-PROTOTYPE", "foreign") == (TYPE_STUB[2], TYPE_STUB[1])

    def test_an_argument_typed_by_the_stub(self, generated: etree._Element) -> None:
        assert _typed(generated, "ARGUMENT-DATA-PROTOTYPE", "foreign") == (TYPE_STUB[2], TYPE_STUB[1])

    def test_a_reference_type_over_the_stub(self, generated: etree._Element) -> None:
        (node,) = [
            e for e in generated.iter(ar_qname("IMPLEMENTATION-DATA-TYPE")) if short_name_of(e) == "RefForeign_T"
        ]
        (reference,) = node.iter(ar_qname("IMPLEMENTATION-DATA-TYPE-REF"))

        assert node.findtext(ar_qname("CATEGORY")) == "TYPE_REFERENCE"
        assert (reference.get("DEST"), reference.text) == (TYPE_STUB[2], TYPE_STUB[1])

    def test_a_data_element_typed_by_that_reference_type_names_the_declared_type(
        self, generated: etree._Element
    ) -> None:
        assert _typed(generated, "VARIABLE-DATA-PROTOTYPE", "foreignRef") == (
            "IMPLEMENTATION-DATA-TYPE",
            "/DataTypes/Implementation/RefForeign_T",
        )

    def test_a_reference_type_over_a_declared_type_is_unchanged(self, baseline: Path, tmp_path: Path) -> None:
        write_model_element(baseline, {"name": "RefU8", "kind": "reference_type", "base": "uint8"})
        root = _generate(baseline, tmp_path)
        (node,) = [e for e in root.iter(ar_qname("IMPLEMENTATION-DATA-TYPE")) if short_name_of(e) == "RefU8"]
        (reference,) = node.iter(ar_qname("IMPLEMENTATION-DATA-TYPE-REF"))

        assert (reference.get("DEST"), reference.text) == (
            "IMPLEMENTATION-DATA-TYPE",
            "/DataTypes/Implementation/uint8",
        )


class TestARoleThatAdmitsNoStub:
    """The role table's *never* rows: the writer refuses what ``check-model`` reports."""

    def test_a_value_type_over_a_stub(self, baseline: Path, tmp_path: Path) -> None:
        type_stub(baseline, *TYPE_STUB)
        write_model_element(baseline, {"name": "V", "kind": "value_type", "base": TYPE_STUB[0]})

        # A value type's base resolves in the base type layer alone, where no stub
        # is: an undeclared base, refused as not declared there.
        assert _rules(baseline) == ["data-type.value-type-undeclared-base"]
        assert "data-type.value-type-base-not-base-type" not in _rules(baseline)
        _refused(baseline, tmp_path, "'V'", TYPE_STUB[0], "is not declared in data_types/base/")

    def test_a_record_field_typed_by_a_stub(self, baseline: Path, tmp_path: Path) -> None:
        type_stub(baseline, *TYPE_STUB)
        write_model_element(
            baseline, {"name": "Rec", "kind": "record", "fields": [{"name": "f", "type": TYPE_STUB[0]}]}
        )

        # A record field's type resolves in the application type layer alone, where
        # no stub is: an undeclared type, refused as not declared.
        assert _rules(baseline) == ["data-type.record-field-undeclared-type"]
        assert "data-type.composite-member-implementation-type" not in _rules(baseline)
        _refused(
            baseline, tmp_path, f"Data type '{TYPE_STUB[0]}' referenced by record 'Rec' field 'f'", "is not declared"
        )

    def test_an_array_element_typed_by_a_stub(self, baseline: Path, tmp_path: Path) -> None:
        type_stub(baseline, *TYPE_STUB)
        write_model_element(baseline, {"name": "Arr", "kind": "array", "element_type": TYPE_STUB[0], "length": 2})

        assert _rules(baseline) == ["data-type.array-undeclared-element-type"]
        assert "data-type.composite-member-implementation-type" not in _rules(baseline)
        _refused(
            baseline, tmp_path, f"Data type '{TYPE_STUB[0]}' referenced by array 'Arr' element type", "is not declared"
        )

    def test_a_mapping_set_naming_a_stub(self, baseline: Path, tmp_path: Path) -> None:
        type_stub(baseline, *TYPE_STUB)
        write_model_element(baseline, {"name": "App", "kind": "application_type"})
        write_model_element(
            baseline,
            {"name": "M", "maps": [{"application": "App", "implementation": TYPE_STUB[0]}]},
            layer="mapping_sets",
        )

        assert "mapping-set.unknown-implementation" in _rules(baseline)
        _refused(baseline, tmp_path, "Mapping set 'M'", TYPE_STUB[0])


# Connectors and delegations across a stub port


class TestAcrossAStubPort:
    def test_both_assembly_connectors_are_emitted(self, generated: etree._Element) -> None:
        names = {short_name_of(e) for e in generated.iter(ar_qname("ASSEMBLY-SW-CONNECTOR"))}

        assert {"Sr_across_stub", "Cs_across_stub"} <= names

    def test_both_delegations_are_emitted(self, generated: etree._Element) -> None:
        names = {short_name_of(e) for e in generated.iter(ar_qname("DELEGATION-SW-CONNECTOR"))}

        assert names == {"Root_pExtSr", "Root_rExtCs"}

    def test_a_connector_across_a_kind_the_stub_contradicts_is_refused(self, stub_model: Path, tmp_path: Path) -> None:
        # The consumer's `rIn` re-pointed at the CS stub: its concrete DEST names
        # client-server, a certain mismatch with the provider's SR port.
        change(stub_model, CONSUMER, lambda d: d["ports"][0].update(interface="IForeignCS"))

        assert "connector.interface-mismatch" in _rules(stub_model)
        _refused(stub_model, tmp_path, "Connector consistency check failed", "IForeignCS")

    def test_a_delegation_across_a_kind_the_stub_contradicts_is_refused(self, stub_model: Path, tmp_path: Path) -> None:
        # The outer SR stub port delegated onto the member's CS stub port.
        change(stub_model, ROOT, lambda d: d["delegations"][0].update(member_port="pCs"))

        assert "composition.delegation-interface-mismatch" in _rules(stub_model)
        _refused(stub_model, tmp_path, "Delegation 'Root_pExtSr'", "interface kinds differ")

    def test_a_delegation_across_an_abstract_dest_is_emitted(self, stub_model: Path, tmp_path: Path) -> None:
        def abstract(document: dict[str, Any]) -> None:
            document["ports"].append({"name": "rExtAny", "direction": "require", "interface": "IForeignAny"})
            document["delegations"].append(
                {"name": "Root_rExtAny", "port": "rExtAny", "member": "ExampleConsumerSWC", "member_port": "rSr"}
            )

        change(stub_model, ROOT, abstract)
        assert _rules(stub_model) == []

        names = {short_name_of(e) for e in _generate(stub_model, tmp_path).iter(ar_qname("DELEGATION-SW-CONNECTOR"))}
        assert "Root_rExtAny" in names


# The refusals

_MALFORMED = (
    ("blank-ref", "", None, "external_ref"),
    ("not-a-path", "Foreign Pkg", None, "external_ref"),
    ("blank-dest", None, "", "external_dest"),
    ("dest-out-of-set", None, "FOREIGN-SENDER-RECEIVER-INTERFACE", "external_dest"),
)


class TestAMalformedStubIsRefused:
    """``generate arxml`` directly, on a model whose malformed stub nothing names."""

    @pytest.mark.parametrize(("case", "ref", "dest", "field"), _MALFORMED, ids=[c[0] for c in _MALFORMED])
    def test_an_interface_stub(
        self, baseline: Path, tmp_path: Path, case: str, ref: str | None, dest: str | None, field: str
    ) -> None:
        name, good_ref, good_dest = INTERFACE_STUBS[1]
        interface_stub(baseline, name, good_ref if ref is None else ref, good_dest if dest is None else dest)

        assert _rules(baseline) == ["interface.external-incomplete-reference"]
        _refused(baseline, tmp_path, "interfaces/", f"'{name}'", field)

    @pytest.mark.parametrize(("case", "ref", "dest", "field"), _MALFORMED, ids=[c[0] for c in _MALFORMED])
    def test_a_type_stub(
        self, baseline: Path, tmp_path: Path, case: str, ref: str | None, dest: str | None, field: str
    ) -> None:
        name, good_ref, good_dest = TYPE_STUB
        type_stub(baseline, name, good_ref if ref is None else ref, good_dest if dest is None else dest)

        assert _rules(baseline) == ["data-type.external-incomplete-reference"]
        _refused(baseline, tmp_path, "data_types/implementation/", f"'{name}'", field)

    @pytest.mark.parametrize(
        "ref", ["/PortInterfaces/IExample", "/PortInterfaces/INothingDeclares", "/DataTypes/uint8", "componenttypes/X"]
    )
    def test_an_interface_stub_into_a_generated_package(self, baseline: Path, tmp_path: Path, ref: str) -> None:
        """A path inside a package this writer declares the project's own elements in."""
        interface_stub(baseline, INTERFACE_STUBS[1][0], ref, INTERFACE_STUBS[1][2])

        assert _rules(baseline) == ["interface.external-incomplete-reference"]
        _refused(
            baseline,
            tmp_path,
            "interfaces/",
            f"'{INTERFACE_STUBS[1][0]}'",
            f"'{ref}'",
            "a package the generated ARXML declares this project's own elements in",
        )

    @pytest.mark.parametrize("ref", ["/DataTypes/uint8", "/DataTypes/NothingDeclares", "/PortInterfaces/X"])
    def test_a_type_stub_into_a_generated_package(self, baseline: Path, tmp_path: Path, ref: str) -> None:
        type_stub(baseline, TYPE_STUB[0], ref, TYPE_STUB[2])

        assert _rules(baseline) == ["data-type.external-incomplete-reference"]
        _refused(baseline, tmp_path, "data_types/implementation/", f"'{TYPE_STUB[0]}'", f"'{ref}'")

    def test_the_probe_shape(self, baseline: Path, tmp_path: Path) -> None:
        """Each stub aimed at the declared element of its own name, and each used.

        Guards a defect: such a path resolved to the declared element in the same file,
        and the schema accepted the document.
        """
        interface_stub(baseline, "IExample_ext", "/PortInterfaces/IExample", "SENDER-RECEIVER-INTERFACE")
        type_stub(baseline, "uint8_ext", "/DataTypes/uint8", "IMPLEMENTATION-DATA-TYPE")
        change(
            baseline,
            CONSUMER,
            lambda d: d["ports"].append({"name": "rX", "direction": "require", "interface": "IExample_ext"}),
        )
        change(
            baseline,
            SR_INTERFACE,
            lambda d: d["data_elements"].append({"name": "x", "implementation_type": "uint8_ext"}),
        )

        assert _rules(baseline) == [
            "data-type.external-incomplete-reference",
            "interface.external-incomplete-reference",
        ]
        _refused(
            baseline, tmp_path, "'IExample_ext'", "'/PortInterfaces/IExample'", "'uint8_ext'", "'/DataTypes/uint8'"
        )

    def test_an_abstract_interface_dest_is_not_malformed(self, baseline: Path, tmp_path: Path) -> None:
        interface_stub(baseline, *INTERFACE_STUBS[3])

        assert _rules(baseline) == []
        _generate(baseline, tmp_path)

    def test_an_interface_dest_is_not_a_type_dest(self, baseline: Path, tmp_path: Path) -> None:
        type_stub(baseline, TYPE_STUB[0], TYPE_STUB[1], "APPLICATION-PRIMITIVE-DATA-TYPE")

        assert _rules(baseline) == ["data-type.external-incomplete-reference"]
        _refused(baseline, tmp_path, "APPLICATION-PRIMITIVE-DATA-TYPE")


_ACCESS = (
    ("reads", {"port": "rSr", "element": "value"}, "unknown-element"),
    ("writes", {"port": "pSr", "element": "value"}, "unknown-element"),
    ("calls", {"port": "rCs", "operation": "ReadDtc"}, "unknown-operation"),
)


class TestAnAccessThroughAStubPortIsRefused:
    @pytest.mark.parametrize(("list_name", "entry", "rule"), _ACCESS, ids=[a[0] for a in _ACCESS])
    def test_through_each_list(
        self, stub_model: Path, tmp_path: Path, list_name: str, entry: dict[str, str], rule: str
    ) -> None:
        relative = CONSUMER if entry["port"].startswith("r") else PROVIDER
        change(stub_model, relative, lambda d: d["runnables"][0].update({list_name: [entry]}))

        assert _rules(stub_model) == [f"swc.access.{rule}"]
        _refused(stub_model, tmp_path, f"port '{entry['port']}'", "external interface stub", f"swc.access.{rule}")

    def test_the_wrong_direction_is_refused_under_the_member_rule(self, stub_model: Path, tmp_path: Path) -> None:
        """A write through a ``require`` stub port is refused by both gates; the writer names the member rule once.

        The writer's refusal does not repeat the direction.
        """
        change(stub_model, CONSUMER, lambda d: d["runnables"][0].update(writes=[{"port": "rSr", "element": "value"}]))

        assert sorted(_rules(stub_model)) == ["swc.access.direction-mismatch", "swc.access.unknown-element"]
        message = _refused(stub_model, tmp_path, "port 'rSr'", "external interface stub", "swc.access.unknown-element")
        assert "swc.access.direction-mismatch" not in message

    def test_a_concrete_dest_of_the_other_kind_still_names_the_member_rule(
        self, stub_model: Path, tmp_path: Path
    ) -> None:
        # A read through the CS stub names the member rule, never the kind rule.
        change(stub_model, CONSUMER, lambda d: d["runnables"][0].update(reads=[{"port": "rCs", "element": "x"}]))

        assert _rules(stub_model) == ["swc.access.unknown-element"]
        message = _refused(stub_model, tmp_path, "swc.access.unknown-element")
        assert "client-server" not in message


class TestAnInitOnAStubTypedElementIsRefused:
    def test_directly(self, stub_model: Path, tmp_path: Path) -> None:
        change(stub_model, SR_INTERFACE, lambda d: d["data_elements"][1].update(init=0))

        assert _rules(stub_model) == ["interface.init-on-external-type"]
        _refused(stub_model, tmp_path, "data element 'foreign'", TYPE_STUB[0], "interface.init-on-external-type")

    def test_through_one_reference_type(self, stub_model: Path, tmp_path: Path) -> None:
        change(stub_model, SR_INTERFACE, lambda d: d["data_elements"][2].update(init=0))

        assert _rules(stub_model) == ["interface.init-on-external-type"]
        _refused(stub_model, tmp_path, "data element 'foreignRef'", "RefForeign_T", "interface.init-on-external-type")

    def test_through_two_reference_types_and_a_structured_value(self, stub_model: Path, tmp_path: Path) -> None:
        write_model_element(stub_model, {"name": "RefRef_T", "kind": "reference_type", "base": "RefForeign_T"})
        change(
            stub_model,
            SR_INTERFACE,
            lambda d: d["data_elements"].append({"name": "deep", "implementation_type": "RefRef_T", "init": [1, 2]}),
        )

        assert _rules(stub_model) == ["interface.init-on-external-type"]
        _refused(stub_model, tmp_path, "data element 'deep'", "RefRef_T", "interface.init-on-external-type")


# Names


class TestAStubClaimsNoEmittedName:
    def test_a_type_stub_named_like_an_enums_compu_method(self, baseline: Path, tmp_path: Path) -> None:
        write_model_element(
            baseline,
            {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}, {"name": "ON", "value": 1}]},
        )
        type_stub(baseline, "Mode_CompuMethod", TYPE_STUB[1], TYPE_STUB[2])

        assert _rules(baseline) == []
        _generate(baseline, tmp_path)

    @pytest.mark.parametrize("suffix", ["_Unit", "_Compu", "_Range"])
    def test_a_type_stub_named_like_an_application_types_form(
        self, baseline: Path, tmp_path: Path, suffix: str
    ) -> None:
        write_model_element(
            baseline,
            {
                "name": "Speed",
                "kind": "application_type",
                "unit": "km/h",
                "conversion": {"factor": 0.1},
                "range": {"min": 0.0, "max": 300.0},
            },
        )
        type_stub(baseline, f"Speed{suffix}", TYPE_STUB[1], TYPE_STUB[2])

        assert _rules(baseline) == []
        _generate(baseline, tmp_path)

    def test_an_interface_stub_named_like_an_init_constant(self, baseline: Path, tmp_path: Path) -> None:
        change(baseline, SR_INTERFACE, lambda d: d["data_elements"][0].update(init=1.5))
        interface_stub(baseline, "IExample_value_Init", *INTERFACE_STUBS[1][1:])

        assert _rules(baseline) == []
        _generate(baseline, tmp_path)

    def test_a_mapping_set_named_like_a_type_stub(self, baseline: Path, tmp_path: Path) -> None:
        type_stub(baseline, *TYPE_STUB)
        write_model_element(baseline, {"name": TYPE_STUB[0], "maps": []}, layer="mapping_sets")

        assert _rules(baseline) == []
        root = _generate(baseline, tmp_path)
        assert [short_name_of(e) for e in root.iter(ar_qname("DATA-TYPE-MAPPING-SET"))] == [TYPE_STUB[0]]

    def test_the_writer_and_the_validator_reference_a_declared_interface_by_one_dest(self) -> None:
        """``validator._DECLARED_INTERFACE_DESTS`` equals the writer's table.

        It restates the table so ``validator`` stays importable without ``writer``.
        """
        from swcraft_tools.schema import CSInterface, SRInterface
        from swcraft_tools.validator.compatibility import _DECLARED_INTERFACE_DESTS
        from swcraft_tools.writer.arxml_generation import _INTERFACE_REF_DEST

        by_kind = {"sender_receiver": SRInterface, "client_server": CSInterface}

        assert {kind: _INTERFACE_REF_DEST[model] for kind, model in by_kind.items()} == _DECLARED_INTERFACE_DESTS
        assert set(_INTERFACE_REF_DEST) == set(by_kind.values())

    def test_a_type_stub_is_placed_in_the_implementation_layer_and_emits_nothing_there(
        self, stub_model: Path, generated: etree._Element
    ) -> None:
        """A type stub's layer is the implementation one; no written package declares an element of its name."""
        from swcraft_tools.reader import read_data_types

        (stub,) = [t for t in read_data_types(str(stub_model)) if t.name == TYPE_STUB[0]]

        assert data_description_package_path(stub) == "DataTypes/Implementation"
        declared = [
            element
            for package in generated.iter(ar_qname("AR-PACKAGE"))
            for elements in package.findall(ar_qname("ELEMENTS"))
            for element in elements
            if short_name_of(element) == TYPE_STUB[0]
        ]
        assert declared == []


class TestModelIdentityStillHolds:
    def test_a_type_stub_beside_a_declared_type_of_its_name(self, baseline: Path, tmp_path: Path) -> None:
        put(
            baseline,
            model_element_storage_name("uint8~1", kind="external_type"),
            {"name": "uint8", "kind": "external_type", **_pair(TYPE_STUB)},
        )

        assert "data-type.duplicate-name" in _rules(baseline)
        # The refusal names the layer the stub and the declared type share.
        _refused(baseline, tmp_path, "Duplicate data type names: 'uint8' (implementation_types)")

    def test_an_interface_stub_beside_a_declared_interface_of_its_name(self, baseline: Path, tmp_path: Path) -> None:
        put(
            baseline,
            "interfaces/IExample~1.yaml",
            {"name": "IExample", "kind": "external_interface", **_pair(INTERFACE_STUBS[1])},
        )

        assert "name.duplicate-interface" in _rules(baseline)
        _refused(baseline, tmp_path, "Duplicate interface names", "IExample")

    def test_a_stubs_own_name_is_identifier_checked(self, baseline: Path, tmp_path: Path) -> None:
        interface_stub(baseline, "1Foreign", *INTERFACE_STUBS[1][1:])

        assert _rules(baseline) == ["identifier.declared"]
        message = _refused(baseline, tmp_path, "1Foreign")
        assert "'1Foreign' must be an AUTOSAR Identifier" in message, message


def _pair(stub: tuple[str, str, str]) -> dict[str, str]:
    return {"external_ref": stub[1], "external_dest": stub[2]}
