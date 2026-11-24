"""First-party port-interface emitters -- both kinds.

Each emitter is tested directly, against a named slice of a golden (``serialize_fragment()`` /
``conftest.golden_slice()``). The emitters must write:

* ``IS-SERVICE false`` on both interface kinds;
* ``SERVER-ARGUMENT-IMPL-POLICY USE-ARGUMENT-TYPE`` after every CS argument's ``DIRECTION``, except on an
  array-typed argument, where the absent element means ``useArrayBaseType`` (Table 4.8, SWCT R4.2.2);
* ``SW-CALIBRATION-ACCESS NOT-ACCESSIBLE`` in every ``ARGUMENT-DATA-PROTOTYPE`` but not in a
  ``VARIABLE-DATA-PROTOTYPE``, which carries no ``SW-DATA-DEF-PROPS``; asserted from both sides.

The committed goldens reach only three of the five type kinds of ``FIVE_KIND_REFS`` through an interface (value
type, record, array: no reference type, no enum, no record- or enum-typed CS argument); the ``interface_shapes``
fixture runs all five, an argument-less operation and an ``inout`` argument through ``generate_arxml()``.

A ``*_matches_the_generator_path`` test compares the emitter with the dispatcher's use of it -- a real
assertion, but not an independent oracle. The three byte goldens and ``TestSemanticRoundTrip`` are.
"""

from __future__ import annotations

import pytest
import yaml
from conftest import (
    GOLDEN_MODEL_ROOTS,
    golden_slice,
    make_empty_model_root,
    model_element_storage_name,
    read_golden,
    write_model_element,
)
from swcraft_tools.data_description import DataDescription
from swcraft_tools.reader import read_data_types, read_interfaces, read_layer
from swcraft_tools.schema import (
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    SRInterface,
)
from swcraft_tools.writer.arxml_generation import (
    _emit_cs_interface,
    _emit_interfaces,
    _emit_sr_interface,
    build_workspace,
    generate_arxml,
)
from swcraft_tools.writer.arxml_xml import AR_NAMESPACE, ArxmlDocument, serialize_fragment

#: The type every ad-hoc (non-golden) interface in this module references: a value
#: type over a ``{name}_base`` base type, which every model root here declares beside it.
_UINT8_BASE = {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"}
_UINT8 = {"name": "uint8", "kind": "value_type", "base": "uint8_base"}

#: The kinds of the ``application_types`` layer: a prototype names one as its ``application_type``.
_APPLICATION_KINDS = frozenset({"application_type", "enum", "record", "array"})

#: The data types ``interface_shapes`` declares: the five kinds its prototypes are typed by (value type,
#: reference type, enum, record, array), the ``application_type`` members of the composites, and the base types
#: the value types rest on (never a prototype's type, so no row below). A composite member is typed by an
#: ``application_type``; its implementation type belongs in a mapping set.
_SHAPE_DATA_TYPES: list[dict] = [
    _UINT8_BASE,
    _UINT8,
    {"name": "float32_base", "kind": "base_type", "size": 32, "encoding": "IEEE754", "native_declaration": "float32"},
    {"name": "float32", "kind": "value_type", "base": "float32_base"},
    {"name": "Speed_T", "kind": "value_type", "base": "float32_base"},
    {"name": "SpeedRef_T", "kind": "reference_type", "base": "Speed_T"},
    {"name": "Distance_P", "kind": "application_type"},
    {"name": "Speed_P", "kind": "application_type", "unit": "km/h"},
    {"name": "Gear_T", "kind": "enum", "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}]},
    {
        "name": "Pose_T",
        "kind": "record",
        "fields": [{"name": "x", "type": "Distance_P"}, {"name": "gear", "type": "Gear_T"}],
    },
    {"name": "Samples_T", "kind": "array", "element_type": "Speed_P", "length": 4},
]

#: A sender-receiver interface whose data elements are typed by value types, a reference type, an enum, a record
#: and an array. Its ``description`` makes the "a description is written" assertion one about the writer's real
#: behaviour.
_ALL_KINDS_SR: dict = {
    "name": "IAllKinds",
    "kind": "sender_receiver",
    "description": "every declared type kind, once",
    "data_elements": [
        {"name": "raw", "implementation_type": "uint8"},
        {"name": "speed", "implementation_type": "Speed_T"},
        {"name": "ref", "implementation_type": "SpeedRef_T"},
        {"name": "gear", "application_type": "Gear_T"},
        {"name": "pose", "application_type": "Pose_T"},
        {"name": "samples", "application_type": "Samples_T"},
    ],
}

#: A client-server interface reaching the same five kinds, all three directions,
#: and an operation with no arguments at all.
_ALL_KINDS_CS: dict = {
    "name": "IAllKindsCs",
    "kind": "client_server",
    "description": "every declared type kind, once",
    "operations": [
        {
            "name": "Configure",
            "arguments": [
                {"name": "raw", "direction": "in", "implementation_type": "uint8"},
                {"name": "speed", "direction": "inout", "implementation_type": "Speed_T"},
                {"name": "ref", "direction": "in", "implementation_type": "SpeedRef_T"},
                {"name": "gear", "direction": "out", "application_type": "Gear_T"},
                {"name": "pose", "direction": "in", "application_type": "Pose_T"},
                {"name": "samples", "direction": "in", "application_type": "Samples_T"},
            ],
        },
        {"name": "Ping", "arguments": []},
    ],
}

#: ``(type kind, the TYPE-TREF line it must produce)`` for each of the five type kinds,
#: asserted against the ``generate_arxml()`` output too, so a wrong literal here fails.
FIVE_KIND_REFS = [
    ("value_type", '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/Speed_T</TYPE-TREF>'),
    ("reference_type", '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/SpeedRef_T</TYPE-TREF>'),
    ("enum", '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/Gear_T</TYPE-TREF>'),
    ("record", '<TYPE-TREF DEST="APPLICATION-RECORD-DATA-TYPE">/DataTypes/Application/Pose_T</TYPE-TREF>'),
    ("array", '<TYPE-TREF DEST="APPLICATION-ARRAY-DATA-TYPE">/DataTypes/Application/Samples_T</TYPE-TREF>'),
]


def _type_registry(model_root) -> dict:
    """A model root's declared data types of the three data description layers, keyed by name.

    No fixture here declares one name in two layers.
    """
    root = str(model_root)
    return {
        dt.name: dt
        for dt in (*read_layer(root, "base_types"), *read_data_types(root), *read_layer(root, "application_types"))
    }


def _model_types(golden: str) -> dict:
    """A golden's model root's declared data types, keyed by name (the registry shape)."""
    return _type_registry(GOLDEN_MODEL_ROOTS[golden])


def _model_interfaces(golden: str) -> dict:
    """A golden's model root's interfaces, keyed by name."""
    return {iface.name: iface for iface in read_interfaces(str(GOLDEN_MODEL_ROOTS[golden]))}


def _names_of_kind(golden: str, kind: type) -> list[str]:
    """Every interface name of *kind* in a golden's model, in read order."""
    return [name for name, iface in _model_interfaces(golden).items() if isinstance(iface, kind)]


def _write_model(model_root, subdir: str, data: dict) -> None:
    """Write one ``<subdir>/<name>.yaml`` element file; a data type goes to its ``kind``'s layer directory."""
    if subdir == "data_types":
        subdir = model_element_storage_name(data["name"], kind=data["kind"]).rpartition("/")[0]
    path = model_root / subdir / f"{data['name']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _emit(iface, registry) -> str:
    """Emit one interface into a fresh document and return its serialized fragment."""
    package = ArxmlDocument().port_interfaces
    if isinstance(iface, SRInterface):
        node = _emit_sr_interface(package, iface, DataDescription.last_declared(registry.values()))
    else:
        node = _emit_cs_interface(package, iface, DataDescription.last_declared(registry.values()))
    return serialize_fragment(node)


def _package_short_names(package) -> list[str]:
    """The ``SHORT-NAME``s of a package's elements, in emission order."""
    return [element.find(f"{{{AR_NAMESPACE}}}SHORT-NAME").text for element in package.elements]


#: Every SR/CS interface in each golden's model, as ``(golden, name)`` pairs.
SR_INTERFACES = [(golden, name) for golden in GOLDEN_MODEL_ROOTS for name in _names_of_kind(golden, SRInterface)]
CS_INTERFACES = [(golden, name) for golden in GOLDEN_MODEL_ROOTS for name in _names_of_kind(golden, CSInterface)]


@pytest.fixture(scope="module")
def interface_shapes(tmp_path_factory) -> tuple[str, dict, dict]:
    """``_ALL_KINDS_*`` generated through ``generate_arxml()``.

    Returns the ARXML text, the interfaces and the data type registry, all read back from the same model
    root, so the emitter is driven by exactly the YAML that produced the bytes.
    """
    tmp_path = tmp_path_factory.mktemp("interface_shapes")
    model_root = make_empty_model_root(tmp_path)
    for data_type in _SHAPE_DATA_TYPES:
        _write_model(model_root, "data_types", data_type)
    for iface in (_ALL_KINDS_SR, _ALL_KINDS_CS):
        _write_model(model_root, "interfaces", iface)
    output = tmp_path / "out.arxml"
    generate_arxml(str(model_root), str(output))
    return (
        output.read_text(encoding="utf-8"),
        {iface.name: iface for iface in read_interfaces(str(model_root))},
        _type_registry(model_root),
    )


class TestSenderReceiverEmitter:
    """``SENDER-RECEIVER-INTERFACE`` with ``DATA-ELEMENTS`` of ``VARIABLE-DATA-PROTOTYPE``."""

    @pytest.mark.parametrize(("golden", "name"), SR_INTERFACES)
    def test_interface_matches_the_golden_slice(self, golden, name):
        registry = _model_types(golden)
        iface = _model_interfaces(golden)[name]
        emitted = _emit(iface, registry)
        assert emitted == golden_slice(read_golden(golden), name)

    def test_one_variable_data_prototype_per_data_element_in_declaration_order(self, interface_shapes):
        _text, interfaces, registry = interface_shapes
        fragment = _emit(interfaces["IAllKinds"], registry)
        assert fragment.count("<VARIABLE-DATA-PROTOTYPE>") == len(_ALL_KINDS_SR["data_elements"])
        positions = [fragment.index(f"<SHORT-NAME>{de['name']}</SHORT-NAME>") for de in _ALL_KINDS_SR["data_elements"]]
        assert positions == sorted(positions)

    @pytest.mark.parametrize(("kind", "expected"), FIVE_KIND_REFS)
    def test_a_data_element_of_every_declared_kind_resolves_its_type_tref(self, interface_shapes, kind, expected):
        # The committed goldens reach no enum, so the expectation is asserted against the generated document.
        text, interfaces, registry = interface_shapes
        assert expected in _emit(interfaces["IAllKinds"], registry), kind
        assert expected in golden_slice(text, "IAllKinds"), kind

    def test_the_whole_all_kinds_interface_matches_the_generator_path(self, interface_shapes):
        text, interfaces, registry = interface_shapes
        assert _emit(interfaces["IAllKinds"], registry) == golden_slice(text, "IAllKinds")

    def test_is_service_is_false(self, interface_shapes):
        _text, interfaces, registry = interface_shapes
        assert "<IS-SERVICE>false</IS-SERVICE>" in _emit(interfaces["IAllKinds"], registry)

    def test_a_variable_data_prototype_carries_no_sw_data_def_props(self, interface_shapes):
        _text, interfaces, registry = interface_shapes
        assert "SW-DATA-DEF-PROPS" not in _emit(interfaces["IAllKinds"], registry)

    def test_a_description_is_written_as_its_desc(self, interface_shapes):
        text, interfaces, registry = interface_shapes
        assert interfaces["IAllKinds"].description == _ALL_KINDS_SR["description"]
        paragraph = f'<L-2 L="FOR-ALL">{_ALL_KINDS_SR["description"]}</L-2>'
        assert _emit(interfaces["IAllKinds"], registry).count(paragraph) == 1
        assert paragraph in text

    def test_an_undeclared_data_element_type_raises(self):
        iface = SRInterface(
            name="IGhost",
            kind="sender_receiver",
            data_elements=[DataElement(name="value", implementation_type="nosuchtype")],
        )
        with pytest.raises(ValueError, match="not declared in data_types/implementation/"):
            _emit_sr_interface(ArxmlDocument().port_interfaces, iface, DataDescription.last_declared([]))

    def test_a_rejected_interface_leaves_nothing_behind_in_the_package(self, interface_shapes):
        # The *last* data element is the bad one, so a writer validating as it
        # went would already have emitted the element and its first child.
        _text, _interfaces, registry = interface_shapes
        document = ArxmlDocument()
        iface = SRInterface(
            name="IGhost",
            kind="sender_receiver",
            data_elements=[
                DataElement(name="ok", implementation_type="uint8"),
                DataElement(name="bad", implementation_type="nosuchtype"),
            ],
        )
        with pytest.raises(ValueError):
            _emit_sr_interface(document.port_interfaces, iface, DataDescription.last_declared(registry.values()))
        assert not document.port_interfaces.has_elements


class TestClientServerEmitter:
    """``CLIENT-SERVER-INTERFACE`` with ``OPERATIONS`` of ``CLIENT-SERVER-OPERATION``."""

    @pytest.mark.parametrize(("golden", "name"), CS_INTERFACES)
    def test_interface_matches_the_golden_slice(self, golden, name):
        registry = _model_types(golden)
        iface = _model_interfaces(golden)[name]
        emitted = _emit(iface, registry)
        assert emitted == golden_slice(read_golden(golden), name)

    def test_one_operation_per_declared_operation_in_declaration_order(self):
        # basic-4.2.2's IDiagnosticService is the only multi-operation interface
        # either golden declares.
        registry = _model_types("basic-4.2.2")
        fragment = _emit(_model_interfaces("basic-4.2.2")["IDiagnosticService"], registry)
        assert fragment.count("<CLIENT-SERVER-OPERATION>") == 2
        assert fragment.index("<SHORT-NAME>ReadDtc</SHORT-NAME>") < fragment.index("<SHORT-NAME>ClearDtc</SHORT-NAME>")

    @pytest.mark.parametrize(("kind", "expected"), FIVE_KIND_REFS)
    def test_an_argument_of_every_declared_kind_resolves_its_type_tref(self, interface_shapes, kind, expected):
        text, interfaces, registry = interface_shapes
        assert expected in _emit(interfaces["IAllKindsCs"], registry), kind
        assert expected in golden_slice(text, "IAllKindsCs"), kind

    def test_the_whole_all_kinds_interface_matches_the_generator_path(self, interface_shapes):
        text, interfaces, registry = interface_shapes
        assert _emit(interfaces["IAllKindsCs"], registry) == golden_slice(text, "IAllKindsCs")

    @pytest.mark.parametrize(("declared", "emitted"), [("in", "IN"), ("out", "OUT"), ("inout", "INOUT")])
    def test_direction_is_uppercased(self, interface_shapes, declared, emitted):
        # `inout` is reachable through neither golden; the fixture carries it.
        _text, interfaces, registry = interface_shapes
        assert f"<DIRECTION>{emitted}</DIRECTION>" in _emit(interfaces["IAllKindsCs"], registry), declared

    def test_every_non_array_argument_carries_the_server_argument_impl_policy(self, interface_shapes):
        # Table 4.8 (SWCT R4.2.2): every argument but the array-typed one,
        # and it follows DIRECTION.
        _text, interfaces, registry = interface_shapes
        fragment = _emit(interfaces["IAllKindsCs"], registry)
        arguments = _ALL_KINDS_CS["operations"][0]["arguments"]
        assert fragment.count("<SERVER-ARGUMENT-IMPL-POLICY>USE-ARGUMENT-TYPE</SERVER-ARGUMENT-IMPL-POLICY>") == len(
            [argument for argument in arguments if argument.get("application_type") != "Samples_T"]
        )
        assert fragment.index("<DIRECTION>") < fragment.index("<SERVER-ARGUMENT-IMPL-POLICY>")

    def test_every_argument_carries_a_not_accessible_calibration_access(self, interface_shapes):
        # The calibration-access half a VARIABLE-DATA-PROTOTYPE does not have.
        _text, interfaces, registry = interface_shapes
        fragment = _emit(interfaces["IAllKindsCs"], registry)
        arguments = _ALL_KINDS_CS["operations"][0]["arguments"]
        assert fragment.count("<SW-CALIBRATION-ACCESS>NOT-ACCESSIBLE</SW-CALIBRATION-ACCESS>") == len(arguments)

    def test_an_operation_with_no_arguments_emits_no_arguments_wrapper(self, interface_shapes):
        # Captured from the generated document: no golden declares an argument-less operation.
        text, interfaces, registry = interface_shapes
        emitted = _emit(interfaces["IAllKindsCs"], registry)
        expected = golden_slice(text, "Ping")
        assert "ARGUMENTS" not in expected
        assert golden_slice(emitted, "Ping") == expected

    def test_is_service_is_false(self, interface_shapes):
        _text, interfaces, registry = interface_shapes
        assert "<IS-SERVICE>false</IS-SERVICE>" in _emit(interfaces["IAllKindsCs"], registry)

    def test_a_description_is_written_as_its_desc(self, interface_shapes):
        text, interfaces, registry = interface_shapes
        assert interfaces["IAllKindsCs"].description == _ALL_KINDS_CS["description"]
        paragraph = f'<L-2 L="FOR-ALL">{_ALL_KINDS_CS["description"]}</L-2>'
        assert _emit(interfaces["IAllKindsCs"], registry).count(paragraph) == 1
        assert paragraph in text

    def test_an_undeclared_argument_type_raises(self):
        iface = CSInterface(
            name="IGhost",
            kind="client_server",
            operations=[
                CSOperation(
                    name="Op", arguments=[CSArgument(name="a", direction="in", implementation_type="nosuchtype")]
                )
            ],
        )
        with pytest.raises(ValueError, match="not declared in data_types/implementation/"):
            _emit_cs_interface(ArxmlDocument().port_interfaces, iface, DataDescription.last_declared([]))

    def test_a_rejected_interface_leaves_nothing_behind_in_the_package(self, interface_shapes):
        # The bad argument is in the *second* operation, so a writer validating as
        # it went would already have emitted a whole operation.
        _text, _interfaces, registry = interface_shapes
        document = ArxmlDocument()
        iface = CSInterface(
            name="IGhost",
            kind="client_server",
            operations=[
                CSOperation(name="Ok", arguments=[CSArgument(name="a", direction="in", implementation_type="uint8")]),
                CSOperation(
                    name="Bad", arguments=[CSArgument(name="b", direction="in", implementation_type="nosuchtype")]
                ),
            ],
        )
        with pytest.raises(ValueError):
            _emit_cs_interface(document.port_interfaces, iface, DataDescription.last_declared(registry.values()))
        assert not document.port_interfaces.has_elements


class TestUndeclaredTypeMessage:
    """The "not declared" message is one copy, shared with ``build_workspace()``."""

    def _model_root(self, tmp_path, iface: dict):
        model_root = make_empty_model_root(tmp_path)
        write_model_element(model_root, _UINT8_BASE)
        _write_model(model_root, "data_types", _UINT8)
        _write_model(model_root, "interfaces", iface)
        return model_root

    def _both_messages(self, model_root, emit) -> tuple[str, str]:
        """Raise through the emitter and through ``build_workspace()``, returning both messages."""
        registry = _type_registry(model_root)
        iface = read_interfaces(str(model_root))[0]
        with pytest.raises(ValueError) as via_emitter:
            emit(ArxmlDocument().port_interfaces, iface, DataDescription.last_declared(registry.values()))
        with pytest.raises(ValueError) as via_workspace:
            build_workspace(str(model_root))
        return str(via_emitter.value), str(via_workspace.value)

    def test_a_sender_receiver_data_element_message_matches_build_workspace(self, tmp_path):
        model_root = self._model_root(
            tmp_path,
            {
                "name": "IGhost",
                "kind": "sender_receiver",
                "data_elements": [{"name": "value", "implementation_type": "nosuchtype"}],
            },
        )
        emitter_message, workspace_message = self._both_messages(model_root, _emit_sr_interface)
        assert "Data type 'nosuchtype' not declared" in emitter_message
        assert emitter_message == workspace_message

    def test_a_client_server_argument_message_matches_build_workspace(self, tmp_path):
        model_root = self._model_root(
            tmp_path,
            {
                "name": "IGhost",
                "kind": "client_server",
                "operations": [
                    {"name": "Op", "arguments": [{"name": "a", "direction": "in", "implementation_type": "nosuchtype"}]}
                ],
            },
        )
        emitter_message, workspace_message = self._both_messages(model_root, _emit_cs_interface)
        assert "Data type 'nosuchtype' not declared" in emitter_message
        assert emitter_message == workspace_message


class TestInterfaceDispatcher:
    """``_emit_interfaces()`` — the twin of ``build_workspace()``'s interface pass."""

    def test_both_kinds_are_emitted_in_read_order(self):
        # basic-4.2.2's golden emits its CS interface before its SR interface.
        golden = "basic-4.2.2"
        registry = _model_types(golden)
        document = ArxmlDocument()
        interfaces = read_interfaces(str(GOLDEN_MODEL_ROOTS[golden]))
        _emit_interfaces(document.port_interfaces, interfaces, DataDescription.last_declared(registry.values()))
        assert _package_short_names(document.port_interfaces) == [iface.name for iface in interfaces]

    def test_the_whole_port_interfaces_package_matches_the_golden(self):
        for golden in GOLDEN_MODEL_ROOTS:
            model_root = str(GOLDEN_MODEL_ROOTS[golden])
            document = ArxmlDocument()
            _emit_interfaces(
                document.port_interfaces,
                read_interfaces(model_root),
                DataDescription.last_declared(_model_types(golden).values()),
            )
            assert serialize_fragment(document.port_interfaces.node) == golden_slice(
                read_golden(golden), "PortInterfaces"
            )

    def test_a_model_with_no_interfaces_emits_nothing(self):
        document = ArxmlDocument()
        assert _emit_interfaces(document.port_interfaces, [], DataDescription.last_declared([])) == []
        assert not document.port_interfaces.has_elements


def _argument_policies(types: list[dict], argument_types: dict[str, str]) -> dict[str, str | None]:
    """Each argument's emitted ``SERVER-ARGUMENT-IMPL-POLICY`` text, ``None`` where it is omitted."""
    from pydantic import TypeAdapter
    from swcraft_tools.schema import DataType

    adapter = TypeAdapter(DataType)
    registry = {declared["name"]: adapter.validate_python(declared) for declared in types}
    kinds = {declared["name"]: declared["kind"] for declared in types}
    iface = CSInterface.model_validate(
        {
            "name": "IPolicy",
            "kind": "client_server",
            "operations": [
                {
                    "name": "Op",
                    "arguments": [
                        {
                            "name": name,
                            "direction": "in",
                            (
                                "application_type"
                                if kinds.get(type_name) in _APPLICATION_KINDS
                                else "implementation_type"
                            ): type_name,
                        }
                        for name, type_name in argument_types.items()
                    ],
                }
            ],
        }
    )
    package = ArxmlDocument().port_interfaces
    node = _emit_cs_interface(package, iface, DataDescription.last_declared(registry.values()))
    ns = f"{{{AR_NAMESPACE}}}"
    policies: dict[str, str | None] = {}
    for argument in node.iter(f"{ns}ARGUMENT-DATA-PROTOTYPE"):
        policy = argument.find(f"{ns}SERVER-ARGUMENT-IMPL-POLICY")
        policies[argument.findtext(f"{ns}SHORT-NAME")] = None if policy is None else policy.text
        if policy is not None:
            assert policy.getprevious().tag == f"{ns}DIRECTION"
    return policies


class TestTheServerArgumentImplPolicy:
    """An absent policy means ``useArgumentType`` on a primitive or a structure and ``useArrayBaseType`` on an array.

    Table 4.8 (SWCT R4.2.2 ``:4469``, R4.3.0 ``:4852``). So the writer omits the element on an argument whose type
    resolves -- directly or at the end of a reference chain -- to an ``array`` or an ``implementation_array``, and
    states ``USE-ARGUMENT-TYPE`` on every other one. Omission rather than ``USE-ARRAY-BASE-TYPE``, because
    ``[constr_1297]`` conditions that value on the mapped implementation type's category, which the absent attribute
    never has to meet. An external type stub's content is not declared, so neither file knows whether it is an array:
    the writer omits the element there too, directly and through a reference chain.
    """

    _TYPES: list[dict] = [
        *_SHAPE_DATA_TYPES,
        {"name": "Raw_A", "kind": "implementation_array", "element_type": "uint8", "length": 3, "element_name": "e"},
        {"name": "RawRef_T", "kind": "reference_type", "base": "Raw_A"},
        {"name": "RawRefRef_T", "kind": "reference_type", "base": "RawRef_T"},
        {"name": "Rec_R", "kind": "implementation_record", "elements": [{"name": "a", "type": "uint8"}]},
        {
            "name": "Stub_T",
            "kind": "external_type",
            "external_ref": "/Other/Stub_T",
            "external_dest": "IMPLEMENTATION-DATA-TYPE",
        },
        {"name": "StubRef_T", "kind": "reference_type", "base": "Stub_T"},
    ]

    def test_an_array_typed_argument_carries_none(self):
        assert _argument_policies(self._TYPES, {"samples": "Samples_T"}) == {"samples": None}

    def test_an_implementation_array_argument_carries_none(self):
        assert _argument_policies(self._TYPES, {"raw": "Raw_A"}) == {"raw": None}

    def test_an_argument_typed_through_a_reference_chain_to_an_array_carries_none(self):
        assert _argument_policies(self._TYPES, {"one": "RawRef_T", "two": "RawRefRef_T"}) == {"one": None, "two": None}

    @pytest.mark.parametrize("type_name", ["Stub_T", "StubRef_T"])
    def test_an_argument_typed_by_an_external_type_stub_carries_none(self, type_name):
        assert _argument_policies(self._TYPES, {"arg": type_name}) == {"arg": None}

    @pytest.mark.parametrize("type_name", ["uint8", "Speed_T", "SpeedRef_T", "Gear_T", "Pose_T", "Rec_R"])
    def test_every_other_argument_carries_use_argument_type(self, type_name):
        assert _argument_policies(self._TYPES, {"arg": type_name}) == {"arg": "USE-ARGUMENT-TYPE"}

    def test_the_two_stand_side_by_side_in_one_operation(self):
        policies = _argument_policies(self._TYPES, {"a": "uint8", "b": "Samples_T", "c": "Pose_T", "d": "RawRef_T"})
        assert policies == {"a": "USE-ARGUMENT-TYPE", "b": None, "c": "USE-ARGUMENT-TYPE", "d": None}
