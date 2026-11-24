"""The data type emitters, each tested directly against a named slice of a golden.

The rules held here:

* a base type emits its ``SW-BASE-TYPE`` and nothing else -- no generated ``{name}_impl`` twin;
* ``NATIVE-DECLARATION`` is emitted exactly as declared and never derived; a base type without one is
  refused by the writer and reported by ``check-model``;
* the ``{enum}_CompuMethod`` name is a contract: ``arxml_import/merge.py``'s ``inline_forms`` recognises an
  enumeration's own ``TEXTTABLE`` by it;
* the ``CATEGORY`` values ``TEXTTABLE`` (an enum's compu method; its type is ``VALUE``)/``STRUCTURE``/
  ``ARRAY``, the array's ``Element`` wrapper and the empty ``SW-DATA-DEF-PROPS-VARIANTS``.

The golden tests read their inputs from ``conftest.GOLDEN_MODEL_ROOTS``, the roots the goldens were
generated from. Shapes no golden reaches (a composite typed by another composite, a negative or
non-contiguous enum value) run through ``generate_arxml()`` in the ``nested_composites`` fixture.

A ``*_match_the_generator_path`` test compares the emitter against the dispatcher's use of it, which is
not an independent oracle; the ``*_written_verbatim`` twins restate the bytes, and the three byte goldens
and ``TestSemanticRoundTrip`` are the independent oracles.
"""

from __future__ import annotations

import pytest
import yaml
from conftest import (
    GOLDEN_MODEL_ROOTS,
    golden_slice,
    make_empty_model_root,
    model_element_path,
    read_golden,
    write_model_element,
)
from lxml import etree
from swcraft_tools.data_description import DataDescription, read_data_description
from swcraft_tools.reader import read_data_types, read_layer
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    DataType,
    EnumDataType,
    EnumLiteral,
    RecordDataType,
    RecordField,
    ValueDataType,
)
from swcraft_tools.writer.arxml_generation import (
    _check_base_type_native_declarations,
    _check_data_type_names,
    _emit_array_type,
    _emit_composite_types,
    _emit_enum_type,
    _emit_implementation_data_type,
    _emit_record_type,
    _emit_sw_base_type,
    _emit_value_type,
    build_workspace,
    generate_arxml,
    generated_application_forms,
)
from swcraft_tools.writer.arxml_xml import AR_NAMESPACE, ArxmlDocument, serialize_fragment


def _model_types(golden: str) -> dict[str, object]:
    """A golden's model root's declared data types by name: the index of ``build_workspace()``'s ``type_registry``."""
    root = str(GOLDEN_MODEL_ROOTS[golden])
    # The three data description layers: no golden declares one name in two.
    return {
        dt.name: dt
        for dt in (*read_layer(root, "base_types"), *read_data_types(root), *read_layer(root, "application_types"))
    }


def _both_layers(model_root) -> list[DataType]:
    """The list ``build_workspace()`` hands its guards: both data description layers."""
    return list(read_data_description(str(model_root)).data_types)


def _names_of_kind(golden: str, kind: type) -> list[str]:
    """Every declared data type name of *kind* in a golden's model, in read order."""
    return [name for name, dt in _model_types(golden).items() if isinstance(dt, kind)]


def _write_data_type(model_root, data: dict) -> None:
    """Write one ``data_types/<name>.yaml`` file into a materialized model root."""
    path = model_element_path(model_root, data["name"], kind=data["kind"])
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _emitted(dt: DataType, registry: dict[str, DataType]) -> str:
    """Emit one composite into a fresh document and return its serialized fragment."""
    package = ArxmlDocument().data_types
    if isinstance(dt, EnumDataType):
        _compu, node = _emit_enum_type(package, dt)
    elif isinstance(dt, RecordDataType):
        node = _emit_record_type(package, dt, DataDescription.last_declared(registry.values()))
    else:
        node = _emit_array_type(package, dt, DataDescription.last_declared(registry.values()))
    return serialize_fragment(node)


#: Every base and value type in each golden's model, as ``(golden, type name)`` pairs.
PRIMITIVES = [(golden, name) for golden in GOLDEN_MODEL_ROOTS for name in _names_of_kind(golden, BaseDataType)]
ALIASES = [(golden, name) for golden in GOLDEN_MODEL_ROOTS for name in _names_of_kind(golden, ValueDataType)]

#: Every enum/record/array in the composite golden's model. ``basic`` declares
#: none, so unlike PRIMITIVES/ALIASES these need no ``(golden, name)`` pair.
COMPOSITE_GOLDEN = "composite-4.2.2"
ENUMS = _names_of_kind(COMPOSITE_GOLDEN, EnumDataType)
RECORDS = _names_of_kind(COMPOSITE_GOLDEN, RecordDataType)
ARRAYS = _names_of_kind(COMPOSITE_GOLDEN, ArrayDataType)

#: The element tags a composite pass emits, in no particular order — used to read
#: the composites' emission order back out of a whole document.
_COMPOSITE_TAGS = frozenset(
    {
        "COMPU-METHOD",
        "APPLICATION-PRIMITIVE-DATA-TYPE",
        "APPLICATION-RECORD-DATA-TYPE",
        "APPLICATION-ARRAY-DATA-TYPE",
    }
)

#: A model exercising every composite nesting the committed goldens miss: a
#: record field and an array element typed by another composite, and an enum with
#: a negative and a non-contiguous literal value.
NESTED_COMPOSITES: list[DataType] = [
    BaseDataType(name="uint8", kind="base_type", size=8, encoding="NONE", native_declaration="uint8_t"),
    # A composite member names the PHYSICAL view, so `Inner_T`'s field is typed by a declared
    # application type.
    ApplicationDataType(name="Count_T", kind="application_type"),
    EnumDataType(
        name="Gear_T",
        kind="enum",
        literals=[
            EnumLiteral(name="REVERSE", value=-1),
            EnumLiteral(name="PARK", value=0),
            EnumLiteral(name="DRIVE", value=7),
        ],
    ),
    RecordDataType(name="Inner_T", kind="record", fields=[RecordField(name="count", type="Count_T")]),
    ArrayDataType(name="Samples_T", kind="array", element_type="Inner_T", length=3),
    RecordDataType(
        name="Outer_T",
        kind="record",
        fields=[
            RecordField(name="inner", type="Inner_T"),
            RecordField(name="samples", type="Samples_T"),
            RecordField(name="gear", type="Gear_T"),
        ],
    ),
]

NESTED_REGISTRY: dict[str, DataType] = {dt.name: dt for dt in NESTED_COMPOSITES}


def _application_owned_short_names(types) -> frozenset[str]:
    """Every ``SHORT-NAME`` the application-type pass owns, for the types in *types*.

    The type's own name and the forms its declared fields generate, asked of
    :func:`~swcraft_tools.writer.arxml_generation.generated_application_forms` so the presence rule is read,
    not restated; ``_Compu`` shares the enum's ``COMPU-METHOD`` tag.
    """
    owned: set[str] = set()
    for dt in types:
        if isinstance(dt, ApplicationDataType):
            owned.add(dt.name)
            owned.update(dt.name + suffix for suffix in generated_application_forms(dt))
    return frozenset(owned)


def _composite_order(text: str, *, declared_application_types: frozenset[str] = frozenset()) -> list[str]:
    """The ``SHORT-NAME``s of every composite element in *text*'s ``DataTypes/Application`` package.

    *declared_application_types* are excluded: an application type shares the enum's tags but an earlier
    pass emits it, and neither tag nor suffix tells the two apart (:func:`_application_owned_short_names`
    computes the set).
    """
    root = etree.fromstring(text.encode("utf-8"))
    namespaces = {"ar": AR_NAMESPACE}
    elements = root.findall(
        ".//ar:AR-PACKAGE[ar:SHORT-NAME='DataTypes']/ar:AR-PACKAGES/ar:AR-PACKAGE[ar:SHORT-NAME='Application']"
        "/ar:ELEMENTS/*",
        namespaces,
    )
    return [
        short_name
        for element in elements
        if etree.QName(element).localname in _COMPOSITE_TAGS
        and (short_name := element.find("ar:SHORT-NAME", namespaces).text) not in declared_application_types
    ]


def _package_short_names(package) -> list[str]:
    """The ``SHORT-NAME``s of a package's elements, in emission order."""
    return [element.find(f"{{{AR_NAMESPACE}}}SHORT-NAME").text for element in package.elements]


@pytest.fixture(scope="module")
def nested_composites(tmp_path_factory) -> str:
    """``NESTED_COMPOSITES`` generated through ``generate_arxml()``, as text; unreferenced types are emitted too."""
    tmp_path = tmp_path_factory.mktemp("nested_composites")
    model_root = make_empty_model_root(tmp_path)
    for dt in NESTED_COMPOSITES:
        _write_data_type(model_root, dt.model_dump())
    output = tmp_path / "out.arxml"
    generate_arxml(str(model_root), str(output))
    return output.read_text(encoding="utf-8")


class TestPrimitiveEmitter:
    """A base type's ``SW-BASE-TYPE``, and nothing else; the ``primitive``-era class name keeps test ids stable."""

    @pytest.mark.parametrize(("golden", "name"), PRIMITIVES)
    def test_sw_base_type_matches_the_golden_slice(self, golden, name):
        document = ArxmlDocument()
        base = _emit_sw_base_type(document.data_types, _model_types(golden)[name])
        assert serialize_fragment(base) == golden_slice(read_golden(golden), name, tag="SW-BASE-TYPE")

    @pytest.mark.parametrize("golden", sorted(GOLDEN_MODEL_ROOTS))
    def test_no_impl_twin_is_generated_for_a_golden_models_base_types(self, golden, tmp_path):
        """No ``_impl`` twin is generated for a golden model's base types."""
        output = tmp_path / "out.arxml"
        generate_arxml(str(GOLDEN_MODEL_ROOTS[golden]), str(output))
        text = output.read_text(encoding="utf-8")
        base_types = _names_of_kind(golden, BaseDataType)
        assert base_types
        for name in base_types:
            assert f"<SHORT-NAME>{name}_impl</SHORT-NAME>" not in text, name
        assert text.count("<IMPLEMENTATION-DATA-TYPE>") == len(_names_of_kind(golden, ValueDataType))

    def test_no_element_is_named_base_plus_impl(self):
        """No element is named after a base type plus ``_impl``."""
        document = ArxmlDocument()
        _emit_sw_base_type(document.data_types, _model_types("composite-4.2.2")["float32_base"])
        assert "float32_base_impl" not in _package_short_names(document.data_types)

    def test_only_the_sw_base_type_lands_in_the_data_types_package(self):
        """The data types package holds the ``SW-BASE-TYPE`` alone."""
        document = ArxmlDocument()
        _emit_sw_base_type(document.data_types, _model_types("composite-4.2.2")["float32_base"])
        assert [child.tag.split("}")[1] for child in document.data_types.elements] == ["SW-BASE-TYPE"]

    def test_a_value_type_references_its_base_type_with_a_dest(self):
        """The implementation type references its base type with a DEST, which arxml_reader.py needs to resolve it."""
        registry = _model_types("composite-4.2.2")
        node = _emit_value_type(
            ArxmlDocument().data_types, registry["uint8"], DataDescription.last_declared(registry.values())
        )
        assert '<BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/Base/uint8_base</BASE-TYPE-REF>' in serialize_fragment(
            node
        )

    def test_size_and_encoding_come_from_the_model(self):
        document = ArxmlDocument()
        base = _emit_sw_base_type(
            document.data_types,
            BaseDataType(name="int16", kind="base_type", size=16, encoding="2C", native_declaration="int16_t"),
        )
        fragment = serialize_fragment(base)
        assert "<BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>" in fragment
        assert "<BASE-TYPE-ENCODING>2C</BASE-TYPE-ENCODING>" in fragment

    def test_a_description_is_written_as_its_desc(self):
        # Written exactly, between SHORT-NAME and CATEGORY.
        document = ArxmlDocument()
        base = _emit_sw_base_type(
            document.data_types,
            BaseDataType(
                name="uint8",
                kind="base_type",
                size=8,
                encoding="NONE",
                native_declaration="uint8_t",
                description="a byte",
            ),
        )
        assert (
            '<SHORT-NAME>uint8</SHORT-NAME>\n  <DESC>\n    <L-2 L="FOR-ALL">a byte</L-2>\n  </DESC>\n'
            "  <CATEGORY>FIXED_LENGTH</CATEGORY>"
        ) in serialize_fragment(base)


class TestNativeDeclarationDefault:
    """``NATIVE-DECLARATION`` is emitted exactly as declared and never derived.

    The class and two inverted tests keep their names so their ids stay stable. A default to the type's own
    name would change the generated C type name whenever a base type is renamed.
    """

    def test_an_unset_native_declaration_defaults_to_the_type_name(self):
        assert _model_types("composite-4.2.2")["float32_base"].native_declaration == "float32"
        document = ArxmlDocument()
        base = _emit_sw_base_type(document.data_types, _model_types("composite-4.2.2")["float32_base"])
        assert "<NATIVE-DECLARATION>float32</NATIVE-DECLARATION>" in serialize_fragment(base)
        with pytest.raises(ValueError, match=r"Base type\(s\) \['float32_base'\] in .* declare no native_declaration"):
            _check_base_type_native_declarations(
                [BaseDataType(name="float32_base", kind="base_type", size=32, encoding="IEEE754")]
            )

    def test_a_declared_native_declaration_is_used_verbatim(self):
        assert _model_types("basic-4.2.2")["uint32_base"].native_declaration == "uint32_t"
        document = ArxmlDocument()
        base = _emit_sw_base_type(document.data_types, _model_types("basic-4.2.2")["uint32_base"])
        assert "<NATIVE-DECLARATION>uint32_t</NATIVE-DECLARATION>" in serialize_fragment(base)

    def test_the_default_is_the_name_even_for_a_type_no_golden_declares(self):
        # A base type no golden declares, with no native declaration or a blank one, is refused
        # before anything is emitted -- nothing falls back to the name `uint64`.
        for declared in ("", "   "):
            with pytest.raises(ValueError, match=r"Base type\(s\) \['uint64'\]"):
                _check_base_type_native_declarations(
                    [
                        BaseDataType(
                            name="uint64", kind="base_type", size=64, encoding="NONE", native_declaration=declared
                        )
                    ],
                )

    @pytest.mark.parametrize("declared", ["", "   "], ids=["absent", "blank"])
    def test_with_the_refusal_bypassed_the_emitter_raises_rather_than_derive_one(self, tmp_path, monkeypatch, declared):
        # The refusal in `_build_document` is meant to be the only path a base type
        # without a native declaration can take. Bypass it, and the emitter itself
        # must still refuse: a reordering of the refusal can never revive a
        # derivation (the name `raw_base`, or the blank, as C type name).
        monkeypatch.setattr(
            "swcraft_tools.writer.arxml_generation._check_base_type_native_declarations", lambda *_args: None
        )
        model_root = make_empty_model_root(tmp_path)
        document = {"name": "raw_base", "kind": "base_type", "size": 8, "encoding": "NONE"}
        if declared:
            document["native_declaration"] = declared
        _write_data_type(model_root, document)
        output = tmp_path / "out.arxml"

        with pytest.raises(ValueError, match=r"SW-BASE-TYPE 'raw_base' has no declared NATIVE-DECLARATION"):
            generate_arxml(str(model_root), str(output))
        assert not output.exists()


class TestAliasEmitter:
    """A value type is an ``IMPLEMENTATION-DATA-TYPE`` of category ``VALUE`` over its base type's ``SW-BASE-TYPE``.

    The class keeps its ``alias``-era name, as :class:`TestPrimitiveEmitter` does.
    """

    @pytest.mark.parametrize(("golden", "name"), ALIASES)
    def test_alias_matches_the_golden_slice(self, golden, name):
        document = ArxmlDocument()
        registry = _model_types(golden)
        node = _emit_value_type(document.data_types, registry[name], DataDescription.last_declared(registry.values()))
        assert serialize_fragment(node) == golden_slice(read_golden(golden), name)

    def test_alias_references_its_base_primitives_sw_base_type(self):
        registry = _model_types("composite-4.2.2")
        document = ArxmlDocument()
        node = _emit_value_type(
            document.data_types, registry["Speed_T"], DataDescription.last_declared(registry.values())
        )
        assert '<BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/Base/float32_base</BASE-TYPE-REF>' in serialize_fragment(
            node
        )

    def test_a_value_type_is_exactly_the_value_implementation_shape(self):
        """The value type is held to the one ``VALUE`` implementation shape itself."""
        registry = _model_types("composite-4.2.2")
        value = serialize_fragment(
            _emit_value_type(
                ArxmlDocument().data_types, registry["Speed_T"], DataDescription.last_declared(registry.values())
            )
        )
        shape = serialize_fragment(
            _emit_implementation_data_type(ArxmlDocument().data_types, "Speed_T", "float32_base")
        )
        assert value == shape

    def test_alias_unit_and_limits_are_not_emitted(self):
        # component_exchange/divergence.py's conflict projection ignores a value type's
        # unit and limits *because* they never reach the ARXML.
        registry = {
            "float32": BaseDataType(name="float32", kind="base_type", size=32, encoding="IEEE754"),
            "Speed_T": ValueDataType(
                name="Speed_T", kind="value_type", base="float32", unit="m/s", value_min=0.0, value_max=88.0
            ),
        }
        fragment = serialize_fragment(
            _emit_value_type(
                ArxmlDocument().data_types, registry["Speed_T"], DataDescription.last_declared(registry.values())
            )
        )
        assert "m/s" not in fragment
        assert "LIMIT" not in fragment

    def test_a_description_xml_cannot_carry_is_refused_before_anything_is_emitted(self):
        """Neither the ``{name}_Range`` constraint nor the type is left in the package."""
        registry = {
            "float32": BaseDataType(name="float32", kind="base_type", size=32, encoding="IEEE754"),
            "Speed_T": ValueDataType(
                name="Speed_T", kind="value_type", base="float32", value_min=0.0, description="bell\x07"
            ),
        }
        document = ArxmlDocument()
        with pytest.raises(
            ValueError,
            match="rule:text.xml-unrepresentable-character: Data type 'Speed_T' `description` holds U\\+0007",
        ):
            _emit_value_type(document.data_types, registry["Speed_T"], DataDescription.last_declared(registry.values()))
        assert document.data_types._elements is None

    def test_a_non_primitive_base_raises(self):
        registry = _model_types("composite-4.2.2")
        over_value = ValueDataType(name="Faster_T", kind="value_type", base="Speed_T")
        # A value type's base resolves in the base type layer alone, so a value type named there
        # is simply not declared in it.
        with pytest.raises(ValueError, match=r"base type 'Speed_T' is not declared in data_types/base/"):
            _emit_value_type(ArxmlDocument().data_types, over_value, DataDescription.last_declared(registry.values()))

    def test_an_undeclared_base_raises(self):
        registry = _model_types("composite-4.2.2")
        dangling = ValueDataType(name="Ghost_T", kind="value_type", base="nosuchtype")
        with pytest.raises(ValueError, match="is not declared"):
            _emit_value_type(ArxmlDocument().data_types, dangling, DataDescription.last_declared(registry.values()))

    def test_a_rejected_alias_leaves_nothing_behind_in_the_package(self):
        # A partially emitted element would survive into the document and be
        # serialized by a caller that catches the ValueError.
        document = ArxmlDocument()
        with pytest.raises(ValueError):
            _emit_value_type(
                document.data_types,
                ValueDataType(name="Ghost_T", kind="value_type", base="nope"),
                DataDescription.last_declared([]),
            )
        assert not document.data_types.has_elements


class TestDataTypeNameGuards:
    """The collision guards keep the messages ``build_workspace()`` raises today."""

    def _model_root(self, tmp_path, types: list[dict]):
        from conftest import make_empty_model_root

        model_root = make_empty_model_root(tmp_path)
        for data in types:
            _write_data_type(model_root, data)
        return model_root

    def _both_messages(self, model_root) -> tuple[str, str]:
        """Raise through the guard and through ``build_workspace()``, returning both messages."""
        with pytest.raises(ValueError) as via_guard:
            _check_data_type_names(_both_layers(model_root))
        with pytest.raises(ValueError) as via_workspace:
            build_workspace(str(model_root))
        return str(via_guard.value), str(via_workspace.value)

    def test_duplicate_data_type_names_raise(self, tmp_path):
        # Two files, one name: the filename is not the identity, the `name` field is.
        model_root = self._model_root(
            tmp_path,
            [{"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"}],
        )
        (model_element_path(model_root, "copy", kind="base_type")).write_text(
            yaml.safe_dump(
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"}
            ),
            encoding="utf-8",
        )
        guard_message, workspace_message = self._both_messages(model_root)
        # The refusal names the layer, as the validator's record does.
        assert "Duplicate data type names: 'uint8' (base_types)" in guard_message
        assert "data_types/" not in guard_message
        assert guard_message == workspace_message

    def test_an_impl_suffixed_type_beside_a_base_type_is_allowed(self, tmp_path):
        """No ``{x}_impl`` twin is generated, so the name collides with nothing."""
        model_root = self._model_root(
            tmp_path,
            [
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
                {"name": "uint8_impl", "kind": "value_type", "base": "uint8"},
            ],
        )
        _check_data_type_names(_both_layers(model_root))
        build_workspace(str(model_root))

    def test_a_compu_method_suffixed_type_colliding_with_an_enum_raises(self, tmp_path):
        model_root = self._model_root(
            tmp_path,
            [
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
                {"name": "Gear_T", "kind": "enum", "literals": [{"name": "PARK", "value": 0}]},
                # A form collides inside its owner's package only (each data description layer
                # has its own package), so the occupant is an application-layer type.
                {"name": "Gear_T_CompuMethod", "kind": "application_type"},
            ],
        )
        guard_message, workspace_message = self._both_messages(model_root)
        assert "Gear_T_CompuMethod" in guard_message
        assert "COMPU-METHOD" in guard_message
        assert guard_message == workspace_message

    def test_a_compu_method_suffixed_type_without_a_matching_enum_is_allowed(self, tmp_path):
        # As with "_impl", only a collision with an auto-generated name is a
        # problem — "_CompuMethod" is not a reserved suffix on its own.
        model_root = self._model_root(
            tmp_path,
            [
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
                {"name": "Other_CompuMethod", "kind": "value_type", "base": "uint8"},
            ],
        )
        _check_data_type_names(_both_layers(model_root))

    def test_an_impl_suffixed_type_without_a_matching_primitive_is_allowed(self, tmp_path):
        # Only a collision with an auto-generated name is a problem; "_impl" is
        # not a reserved suffix on its own.
        model_root = self._model_root(
            tmp_path,
            [
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
                {"name": "other_impl", "kind": "value_type", "base": "uint8"},
            ],
        )
        _check_data_type_names(_both_layers(model_root))

    def test_a_clean_model_passes_the_guard(self, tmp_path):
        model_root = self._model_root(
            tmp_path,
            [
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
                {"name": "Count_T", "kind": "value_type", "base": "uint8"},
            ],
        )
        _check_data_type_names(_both_layers(model_root))

    def test_a_name_the_writer_no_longer_generates_is_accepted(self, tmp_path):
        """``{x}_AppView`` and ``DataTypeMappingSet`` are ordinary names: the writer mints neither."""
        model_root = self._model_root(
            tmp_path,
            [
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
                {"name": "Count_T", "kind": "value_type", "base": "uint8"},
                {"name": "Count_T_AppView", "kind": "value_type", "base": "uint8"},
                {"name": "DataTypeMappingSet", "kind": "value_type", "base": "uint8"},
            ],
        )
        _check_data_type_names(_both_layers(model_root))
        build_workspace(str(model_root))

    @pytest.mark.parametrize(
        ("suffix", "field", "element"),
        [
            ("_Unit", {"unit": "m"}, "UNIT"),
            ("_Compu", {"conversion": {}}, "COMPU-METHOD"),
            ("_Range", {"range": {"min": 0}}, "DATA-CONSTR"),
        ],
    )
    def test_an_application_form_colliding_with_its_stem_raises(self, tmp_path, suffix, field, element):
        """The three application forms, reserved for a declared **field**, collide in the owner's package only."""
        model_root = self._model_root(
            tmp_path,
            [
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
                {"name": "Speed", "kind": "application_type", **field},
                {"name": f"Speed{suffix}", "kind": "application_type"},
            ],
        )
        guard_message, workspace_message = self._both_messages(model_root)
        assert f"Speed{suffix}" in guard_message
        assert element in guard_message
        assert guard_message == workspace_message

    @pytest.mark.parametrize(
        ("stem", "occupant"),
        [
            ({"name": "Gear_T", "kind": "enum", "literals": [{"name": "PARK", "value": 0}]}, "Gear_T_CompuMethod"),
            ({"name": "Speed", "kind": "application_type", "unit": "m"}, "Speed_Unit"),
            ({"name": "Speed", "kind": "application_type", "range": {"min": 0}}, "Speed_Range"),
        ],
    )
    def test_a_form_name_taken_by_a_type_of_another_package_is_allowed(self, tmp_path, stem, occupant):
        """A form and a value type of one name are no siblings.

        The form lies in ``/DataTypes/Application`` and a value type in ``/DataTypes/Implementation``.
        """
        model_root = self._model_root(
            tmp_path,
            [
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
                stem,
                {"name": occupant, "kind": "value_type", "base": "uint8"},
            ],
        )
        _check_data_type_names(_both_layers(model_root))
        build_workspace(str(model_root))

    @pytest.mark.parametrize("suffix", ["_Unit", "_Compu", "_Range"])
    def test_an_application_form_whose_stem_declares_no_such_field_is_allowed(self, tmp_path, suffix):
        """The presence rule: a type with no ``unit`` mints no ``{name}_Unit``."""
        model_root = self._model_root(
            tmp_path,
            [
                {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
                {"name": "Speed", "kind": "application_type"},
                {"name": f"Speed{suffix}", "kind": "value_type", "base": "uint8"},
            ],
        )
        _check_data_type_names(_both_layers(model_root))
        build_workspace(str(model_root))


class TestNoApplicationViewIsGenerated:
    """The writer invents no application view and no mapping set.

    Both licensed schemas restrict ``APPLICATION-RECORD-ELEMENT/TYPE-TREF`` and an array ``ELEMENT``'s
    ``TYPE-TREF`` to ``APPLICATION-*`` ``DEST`` values, so a member names a declared application-side type.
    ``test_arxml_application_types_writer.py`` owns the emitted bytes; this class what is *not* emitted.
    """

    @staticmethod
    def _generate(tmp_path, types: list[dict]) -> str:
        from conftest import make_empty_model_root

        model_root = make_empty_model_root(tmp_path)
        for data in [TestNoApplicationViewIsGenerated._BASE, *types]:
            _write_data_type(model_root, data)
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))
        return output.read_text(encoding="utf-8")

    #: A value type, which rests on a base type every model here declares.
    _BASE = {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"}
    _PRIMITIVE = {"name": "uint8", "kind": "value_type", "base": "uint8_base"}

    def test_a_model_with_no_application_type_emits_no_set_and_no_view(self, tmp_path):
        """The condition every unchanged golden rests on."""
        text = self._generate(
            tmp_path,
            [self._PRIMITIVE, {"name": "Gear_T", "kind": "enum", "literals": [{"name": "PARK", "value": 0}]}],
        )
        assert "_AppView" not in text
        assert "DATA-TYPE-MAPPING-SET" not in text

    def test_an_enum_typed_field_references_the_enum_directly(self, tmp_path):
        """An enum already *is* an APPLICATION-PRIMITIVE-DATA-TYPE."""
        text = self._generate(
            tmp_path,
            [
                self._PRIMITIVE,
                {"name": "Gear_T", "kind": "enum", "literals": [{"name": "PARK", "value": 0}]},
                {"name": "Rec_T", "kind": "record", "fields": [{"name": "g", "type": "Gear_T"}]},
            ],
        )
        assert '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/Gear_T</TYPE-TREF>' in text
        assert "_AppView" not in text
        assert "DATA-TYPE-MAPPING-SET" not in text

    def test_two_members_naming_one_application_type_emit_it_once(self, tmp_path):
        text = self._generate(
            tmp_path,
            [
                self._PRIMITIVE,
                {"name": "Count_T", "kind": "application_type"},
                {
                    "name": "Rec_T",
                    "kind": "record",
                    "fields": [{"name": "a", "type": "Count_T"}, {"name": "b", "type": "Count_T"}],
                },
                {"name": "Arr_T", "kind": "array", "element_type": "Count_T", "length": 2},
            ],
        )
        assert text.count("<SHORT-NAME>Count_T</SHORT-NAME>") == 1

    def test_the_application_type_precedes_the_composite_and_the_set_follows_it(self, tmp_path):
        """References point backwards, as everywhere else in this document."""
        from conftest import make_empty_model_root

        model_root = make_empty_model_root(tmp_path)
        for data in [
            self._BASE,
            self._PRIMITIVE,
            {"name": "Count_T", "kind": "application_type"},
            {"name": "Arr_T", "kind": "array", "element_type": "Count_T", "length": 2},
        ]:
            _write_data_type(model_root, data)
        write_model_element(
            model_root,
            {"name": "Counts", "maps": [{"application": "Count_T", "implementation": "uint8"}]},
            layer="mapping_sets",
        )
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))
        text = output.read_text(encoding="utf-8")
        application = text.index("<SHORT-NAME>Count_T</SHORT-NAME>")
        composite = text.index("<SHORT-NAME>Arr_T</SHORT-NAME>")
        mapping_set = text.index("<SHORT-NAME>Counts</SHORT-NAME>")
        assert application < composite < mapping_set

    def test_a_declared_map_names_the_implementation_type_the_model_declares(self, tmp_path):
        """The pair is the model's, not one the writer derived from a name."""
        from conftest import make_empty_model_root

        model_root = make_empty_model_root(tmp_path)
        for data in [
            self._BASE,
            self._PRIMITIVE,
            {"name": "Count_T", "kind": "value_type", "base": "uint8_base"},
            {"name": "Counts_P", "kind": "application_type"},
            {"name": "Arr_T", "kind": "array", "element_type": "Counts_P", "length": 2},
        ]:
            _write_data_type(model_root, data)
        write_model_element(
            model_root,
            {"name": "Counts", "maps": [{"application": "Counts_P", "implementation": "Count_T"}]},
            layer="mapping_sets",
        )
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))
        assert (
            '<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/Count_T'
            "</IMPLEMENTATION-DATA-TYPE-REF>" in output.read_text(encoding="utf-8")
        )

    def test_an_interface_still_references_the_implementation_type(self, tmp_path):
        """The restriction is on composite members only — nothing else may move."""
        from conftest import make_empty_model_root

        model_root = make_empty_model_root(tmp_path)
        _write_data_type(model_root, self._BASE)
        _write_data_type(model_root, self._PRIMITIVE)
        _write_data_type(model_root, {"name": "Count_T", "kind": "application_type"})
        _write_data_type(model_root, {"name": "Arr_T", "kind": "array", "element_type": "Count_T", "length": 2})
        (model_root / "interfaces" / "IRaw.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "IRaw",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "raw", "implementation_type": "uint8"}],
                }
            ),
            encoding="utf-8",
        )
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))
        text = output.read_text(encoding="utf-8")
        assert '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/uint8</TYPE-TREF>' in text


class TestEnumEmitter:
    """An enum is a ``VALUE`` application primitive plus its own ``TEXTTABLE`` ``COMPU-METHOD``."""

    @pytest.mark.parametrize("name", ENUMS)
    def test_the_compu_method_matches_the_golden_slice(self, name):
        registry = _model_types(COMPOSITE_GOLDEN)
        compu, _node = _emit_enum_type(ArxmlDocument().data_types, registry[name])
        assert serialize_fragment(compu) == golden_slice(read_golden(COMPOSITE_GOLDEN), f"{name}_CompuMethod")

    @pytest.mark.parametrize("name", ENUMS)
    def test_the_application_primitive_type_matches_the_golden_slice(self, name):
        registry = _model_types(COMPOSITE_GOLDEN)
        _compu, node = _emit_enum_type(ArxmlDocument().data_types, registry[name])
        assert serialize_fragment(node) == golden_slice(read_golden(COMPOSITE_GOLDEN), name)

    def test_the_compu_method_is_named_enum_plus_compu_method(self):
        # arxml_import/type_mapping.py's enum reduction keys on this exact name, so it is a
        # contract and not a golden detail.
        registry = _model_types(COMPOSITE_GOLDEN)
        compu, _node = _emit_enum_type(ArxmlDocument().data_types, registry["Gear_T"])
        assert serialize_fragment(compu).splitlines()[1].strip() == "<SHORT-NAME>Gear_T_CompuMethod</SHORT-NAME>"

    def test_the_compu_method_is_emitted_before_the_type_that_references_it(self):
        document = ArxmlDocument()
        _emit_enum_type(document.data_types, _model_types(COMPOSITE_GOLDEN)["Gear_T"])
        assert [child.tag.split("}")[1] for child in document.data_types.elements] == [
            "COMPU-METHOD",
            "APPLICATION-PRIMITIVE-DATA-TYPE",
        ]

    def test_the_type_references_its_compu_method_with_a_dest(self):
        registry = _model_types(COMPOSITE_GOLDEN)
        _compu, node = _emit_enum_type(ArxmlDocument().data_types, registry["Gear_T"])
        assert (
            '<COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/Application/Gear_T_CompuMethod</COMPU-METHOD-REF>'
            in serialize_fragment(node)
        )

    def test_each_literal_becomes_one_closed_single_point_compu_scale(self):
        registry = _model_types(COMPOSITE_GOLDEN)
        compu, _node = _emit_enum_type(ArxmlDocument().data_types, registry["Gear_T"])
        fragment = serialize_fragment(compu)
        assert fragment.count("<COMPU-SCALE>") == len(registry["Gear_T"].literals)
        assert '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT>' in fragment
        assert '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">1</UPPER-LIMIT>' in fragment

    def test_negative_and_non_contiguous_literal_values_match_the_generator_path(self, nested_composites):
        # No golden declares either: composite-4.2.2's Gear_T is 0, 1.
        compu, _node = _emit_enum_type(ArxmlDocument().data_types, NESTED_REGISTRY["Gear_T"])
        assert serialize_fragment(compu) == golden_slice(nested_composites, "Gear_T_CompuMethod")

    def test_negative_and_non_contiguous_literal_values_are_written_verbatim(self):
        # The FROZEN half of the pair above. That one compares the emitter with
        # the dispatcher's use of it -- both this code -- so it cannot see a
        # value transform applied in the emitter itself (`abs(literal.value)` would
        # emit REVERSE as 1). These are the bytes, restated independently of any code path.
        compu, _node = _emit_enum_type(ArxmlDocument().data_types, NESTED_REGISTRY["Gear_T"])
        fragment = serialize_fragment(compu)
        for literal in NESTED_REGISTRY["Gear_T"].literals:
            assert f"<SHORT-LABEL>{literal.name}</SHORT-LABEL>" in fragment
            assert f'<LOWER-LIMIT INTERVAL-TYPE="CLOSED">{literal.value}</LOWER-LIMIT>' in fragment
            assert f'<UPPER-LIMIT INTERVAL-TYPE="CLOSED">{literal.value}</UPPER-LIMIT>' in fragment
        # Restated as literals as well, so a mutated `literals` fixture cannot
        # turn the loop above into a tautology.
        assert '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">-1</LOWER-LIMIT>' in fragment
        assert '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">-1</UPPER-LIMIT>' in fragment
        assert '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">7</LOWER-LIMIT>' in fragment
        assert [scale.strip() for scale in fragment.splitlines() if "<SHORT-LABEL>" in scale] == [
            "<SHORT-LABEL>REVERSE</SHORT-LABEL>",
            "<SHORT-LABEL>PARK</SHORT-LABEL>",
            "<SHORT-LABEL>DRIVE</SHORT-LABEL>",
        ]

    def test_duplicate_literal_names_raise(self):
        dt = EnumDataType(
            name="Bad_T",
            kind="enum",
            literals=[EnumLiteral(name="PARK", value=0), EnumLiteral(name="PARK", value=1)],
        )
        with pytest.raises(ValueError, match="duplicate literal name"):
            _emit_enum_type(ArxmlDocument().data_types, dt)

    def test_duplicate_literal_values_raise(self):
        dt = EnumDataType(
            name="Bad_T",
            kind="enum",
            literals=[EnumLiteral(name="PARK", value=0), EnumLiteral(name="DRIVE", value=0)],
        )
        with pytest.raises(ValueError, match="duplicate literal value"):
            _emit_enum_type(ArxmlDocument().data_types, dt)

    def test_a_rejected_enum_leaves_nothing_behind_in_the_package(self):
        # Not even the COMPU-METHOD, which is emitted first: the literals are
        # validated before the first element is created.
        document = ArxmlDocument()
        dt = EnumDataType(
            name="Bad_T",
            kind="enum",
            literals=[EnumLiteral(name="PARK", value=0), EnumLiteral(name="PARK", value=1)],
        )
        with pytest.raises(ValueError):
            _emit_enum_type(document.data_types, dt)
        assert not document.data_types.has_elements


class TestTheEnumTypeIsCategoryValue:
    """An enum's type is ``CATEGORY`` ``VALUE``; its ``COMPU-METHOD`` keeps ``TEXTTABLE``.

    Neither licensed schema constrains the value, so no XSD run can see this (the rationale is
    ``_emit_enum_type``'s docstring). A physical type was already ``VALUE`` and must not move.
    """

    _TYPES = [
        {"name": "Gear_T", "kind": "enum", "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}]},
        {"name": "Speed_T", "kind": "application_type", "unit": "km/h", "conversion": {"factor": 0.5, "offset": 0.0}},
    ]

    @staticmethod
    def _categories(text: str) -> dict[tuple[str, str], str]:
        """``(element tag, SHORT-NAME) -> CATEGORY`` for every data-types element carrying one."""
        root = etree.fromstring(text.encode("utf-8"))
        found: dict[tuple[str, str], str] = {}
        for tag in ("APPLICATION-PRIMITIVE-DATA-TYPE", "COMPU-METHOD"):
            for node in root.iter(f"{{{AR_NAMESPACE}}}{tag}"):
                short = node.find(f"{{{AR_NAMESPACE}}}SHORT-NAME").text
                found[(tag, short)] = node.find(f"{{{AR_NAMESPACE}}}CATEGORY").text
        return found

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_enum_type_is_value_and_its_compu_method_texttable(self, tmp_path, version):
        model_root = make_empty_model_root(tmp_path)
        for data in self._TYPES:
            _write_data_type(model_root, data)
        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output), version=version)
        categories = self._categories(output.read_text(encoding="utf-8"))
        assert categories[("APPLICATION-PRIMITIVE-DATA-TYPE", "Gear_T")] == "VALUE"
        assert categories[("COMPU-METHOD", "Gear_T_CompuMethod")] == "TEXTTABLE"
        # The physical type and its own method are what they were.
        assert categories[("APPLICATION-PRIMITIVE-DATA-TYPE", "Speed_T")] == "VALUE"
        speed_elements = {
            short: category for (_tag, short), category in categories.items() if short.startswith("Speed_T_")
        }
        assert speed_elements == {"Speed_T_Compu": "LINEAR"}


class TestRecordEmitter:
    """A record is an ``APPLICATION-RECORD-DATA-TYPE`` of ``APPLICATION-RECORD-ELEMENT``s."""

    @pytest.mark.parametrize("name", RECORDS)
    def test_record_matches_the_golden_slice(self, name):
        registry = _model_types(COMPOSITE_GOLDEN)
        node = _emit_record_type(
            ArxmlDocument().data_types, registry[name], DataDescription.last_declared(registry.values())
        )
        assert serialize_fragment(node) == golden_slice(read_golden(COMPOSITE_GOLDEN), name)

    def test_one_record_element_per_field_in_declaration_order(self):
        registry = _model_types(COMPOSITE_GOLDEN)
        node = _emit_record_type(
            ArxmlDocument().data_types, registry["Pose_T"], DataDescription.last_declared(registry.values())
        )
        fragment = serialize_fragment(node)
        assert fragment.count("<APPLICATION-RECORD-ELEMENT>") == 2
        assert fragment.index("<SHORT-NAME>x</SHORT-NAME>") < fragment.index("<SHORT-NAME>gear</SHORT-NAME>")

    def test_a_field_type_ref_carries_an_application_dest(self):
        # AUTOSAR restricts APPLICATION-RECORD-ELEMENT/TYPE-TREF to an APPLICATION-* DEST: the
        # member names a DECLARED application type, and an enum -- already an application
        # type -- is referenced directly.
        registry = _model_types(COMPOSITE_GOLDEN)
        fragment = serialize_fragment(
            _emit_record_type(
                ArxmlDocument().data_types, registry["Pose_T"], DataDescription.last_declared(registry.values())
            )
        )
        assert (
            '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/Distance_T</TYPE-TREF>'
            in fragment
        )
        assert '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/Gear_T</TYPE-TREF>' in fragment
        assert "IMPLEMENTATION-DATA-TYPE" not in fragment

    def test_a_record_carries_no_sw_data_def_props(self):
        # Unlike the enum and array types — an asymmetry the goldens pin.
        registry = _model_types(COMPOSITE_GOLDEN)
        fragment = serialize_fragment(
            _emit_record_type(
                ArxmlDocument().data_types, registry["Pose_T"], DataDescription.last_declared(registry.values())
            )
        )
        assert "SW-DATA-DEF-PROPS" not in fragment

    def test_a_record_and_array_typed_field_match_the_generator_path(self, nested_composites):
        # Neither golden nests a composite inside a record.
        assert _emitted(NESTED_REGISTRY["Outer_T"], NESTED_REGISTRY) == golden_slice(nested_composites, "Outer_T")

    def test_a_composite_typed_field_reference_is_written_verbatim(self):
        # The FROZEN half of the pair above, and the only oracle for it:
        # `nested_composites` is generated by the very emitters under test, so
        # a dangling TYPE-TREF in every model that nests composites would pass it. One line
        # per referent *kind*, since the DEST comes from a lookup table that a
        # new type kind must extend.
        fragment = _emitted(NESTED_REGISTRY["Outer_T"], NESTED_REGISTRY)
        assert '<TYPE-TREF DEST="APPLICATION-RECORD-DATA-TYPE">/DataTypes/Application/Inner_T</TYPE-TREF>' in fragment
        assert '<TYPE-TREF DEST="APPLICATION-ARRAY-DATA-TYPE">/DataTypes/Application/Samples_T</TYPE-TREF>' in fragment
        assert '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/Gear_T</TYPE-TREF>' in fragment

    def test_an_application_typed_field_reference_is_written_verbatim(self):
        # A declared application type is referenced by its own name, with the
        # APPLICATION-PRIMITIVE-DATA-TYPE DEST its kind gives it, with no generated view between.
        assert (
            '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/Count_T</TYPE-TREF>'
            in _emitted(NESTED_REGISTRY["Inner_T"], NESTED_REGISTRY)
        )

    def test_duplicate_field_names_raise(self):
        dt = RecordDataType(
            name="Bad_T",
            kind="record",
            fields=[RecordField(name="x", type="uint8"), RecordField(name="x", type="uint8")],
        )
        with pytest.raises(ValueError, match="duplicate field name"):
            _emit_record_type(ArxmlDocument().data_types, dt, DataDescription.last_declared(NESTED_REGISTRY.values()))

    def test_an_undeclared_field_type_raises(self):
        dt = RecordDataType(name="Bad_T", kind="record", fields=[RecordField(name="x", type="nosuchtype")])
        with pytest.raises(ValueError, match="is not declared in data_types/"):
            _emit_record_type(ArxmlDocument().data_types, dt, DataDescription.last_declared(NESTED_REGISTRY.values()))

    def test_a_rejected_record_leaves_nothing_behind_in_the_package(self):
        # The *last* field is the bad one, so a writer that validated as it went
        # would already have emitted the element and its first child.
        document = ArxmlDocument()
        dt = RecordDataType(
            name="Bad_T",
            kind="record",
            fields=[RecordField(name="ok", type="uint8"), RecordField(name="bad", type="nosuchtype")],
        )
        with pytest.raises(ValueError):
            _emit_record_type(document.data_types, dt, DataDescription.last_declared(NESTED_REGISTRY.values()))
        assert not document.data_types.has_elements


class TestArrayEmitter:
    """An array is an ``APPLICATION-ARRAY-DATA-TYPE`` with one ``Element`` wrapper."""

    @pytest.mark.parametrize("name", ARRAYS)
    def test_array_matches_the_golden_slice(self, name):
        registry = _model_types(COMPOSITE_GOLDEN)
        node = _emit_array_type(
            ArxmlDocument().data_types, registry[name], DataDescription.last_declared(registry.values())
        )
        assert serialize_fragment(node) == golden_slice(read_golden(COMPOSITE_GOLDEN), name)

    def test_the_element_wrapper_is_always_named_element(self):
        # The name is the writer's, not the model's.
        registry = _model_types(COMPOSITE_GOLDEN)
        fragment = serialize_fragment(
            _emit_array_type(
                ArxmlDocument().data_types, registry["Samples_T"], DataDescription.last_declared(registry.values())
            )
        )
        assert "<SHORT-NAME>Element</SHORT-NAME>" in fragment

    def test_the_length_becomes_max_number_of_elements_with_fixed_size_semantics(self):
        registry = _model_types(COMPOSITE_GOLDEN)
        assert registry["Samples_T"].length == 4
        fragment = serialize_fragment(
            _emit_array_type(
                ArxmlDocument().data_types, registry["Samples_T"], DataDescription.last_declared(registry.values())
            )
        )
        assert "<MAX-NUMBER-OF-ELEMENTS>4</MAX-NUMBER-OF-ELEMENTS>" in fragment
        assert "<ARRAY-SIZE-SEMANTICS>FIXED-SIZE</ARRAY-SIZE-SEMANTICS>" in fragment

    def test_the_enclosing_type_carries_an_empty_sw_data_def_props_variants(self):
        # The wrapper is emitted unconditionally with nothing in it, and must not be
        # self-closed either.
        registry = _model_types(COMPOSITE_GOLDEN)
        fragment = serialize_fragment(
            _emit_array_type(
                ArxmlDocument().data_types, registry["Samples_T"], DataDescription.last_declared(registry.values())
            )
        )
        assert "<SW-DATA-DEF-PROPS-VARIANTS>\n" in fragment
        assert "</SW-DATA-DEF-PROPS-VARIANTS>" in fragment
        assert "/>" not in fragment

    def test_a_composite_element_type_matches_the_generator_path(self, nested_composites):
        # Neither golden has an array over a record.
        assert _emitted(NESTED_REGISTRY["Samples_T"], NESTED_REGISTRY) == golden_slice(nested_composites, "Samples_T")

    def test_a_composite_element_type_reference_is_written_verbatim(self):
        # The FROZEN half of the pair above — see the record twin for why the
        # generator-path comparison alone is not an oracle.
        assert '<TYPE-TREF DEST="APPLICATION-RECORD-DATA-TYPE">/DataTypes/Application/Inner_T</TYPE-TREF>' in _emitted(
            NESTED_REGISTRY["Samples_T"], NESTED_REGISTRY
        )

    def test_an_undeclared_element_type_raises(self):
        dt = ArrayDataType(name="Bad_T", kind="array", element_type="nosuchtype", length=2)
        with pytest.raises(ValueError, match="is not declared in data_types/"):
            _emit_array_type(ArxmlDocument().data_types, dt, DataDescription.last_declared(NESTED_REGISTRY.values()))

    def test_a_rejected_array_leaves_nothing_behind_in_the_package(self):
        document = ArxmlDocument()
        dt = ArrayDataType(name="Bad_T", kind="array", element_type="nosuchtype", length=2)
        with pytest.raises(ValueError):
            _emit_array_type(document.data_types, dt, DataDescription.last_declared(NESTED_REGISTRY.values()))
        assert not document.data_types.has_elements


class TestCompositeEmissionOrder:
    """``_emit_composite_types()`` reuses Pass 3's topological order unchanged."""

    @staticmethod
    def _composites(types) -> list:
        return [dt for dt in types if isinstance(dt, (EnumDataType, RecordDataType, ArrayDataType))]

    def test_the_golden_models_composites_are_emitted_in_the_goldens_order(self):
        """The golden's composites come in its order, minus the application types an earlier pass emits."""
        registry = _model_types(COMPOSITE_GOLDEN)
        document = ArxmlDocument()
        _emit_composite_types(
            document.data_types, self._composites(registry.values()), DataDescription.last_declared(registry.values())
        )
        assert _package_short_names(document.data_types) == _composite_order(
            read_golden(COMPOSITE_GOLDEN),
            declared_application_types=_application_owned_short_names(registry.values()),
        )

    def test_a_record_is_emitted_after_the_composite_it_references(self):
        # Declaration order is deliberately the reverse of dependency order, so
        # an emitter that simply iterated its input would fail here.
        registry = _model_types(COMPOSITE_GOLDEN)
        document = ArxmlDocument()
        _emit_composite_types(
            document.data_types,
            [registry["Pose_T"], registry["Gear_T"]],
            DataDescription.last_declared(registry.values()),
        )
        assert _package_short_names(document.data_types) == ["Gear_T_CompuMethod", "Gear_T", "Pose_T"]

    def test_the_exclusion_is_not_vacuous(self):
        """Non-vacuity: the exclusion removes exactly the composite-tag elements the composite pass never emits.

        Both shapes occur: an application type's own name and the ``_Compu`` its ``conversion`` mints.
        """
        registry = _model_types(COMPOSITE_GOLDEN)
        owned = _application_owned_short_names(registry.values())
        golden = read_golden(COMPOSITE_GOLDEN)
        unfiltered = _composite_order(golden)
        filtered = _composite_order(golden, declared_application_types=owned)

        removed = [name for name in unfiltered if name not in filtered]
        assert removed == sorted(owned & set(unfiltered), key=unfiltered.index)
        assert any(name.endswith("_Compu") for name in removed)
        assert any(not name.endswith(("_Unit", "_Compu", "_Range")) for name in removed)

    def test_a_transitive_chain_matches_the_generator_paths_order(self, nested_composites):
        document = ArxmlDocument()
        composites = self._composites(NESTED_COMPOSITES)
        _emit_composite_types(document.data_types, composites, DataDescription.last_declared(NESTED_REGISTRY.values()))
        assert _package_short_names(document.data_types) == _composite_order(
            nested_composites,
            declared_application_types=_application_owned_short_names(NESTED_COMPOSITES),
        )

    def test_a_cycle_still_raises(self):
        left = RecordDataType(name="Left_T", kind="record", fields=[RecordField(name="r", type="Right_T")])
        right = RecordDataType(name="Right_T", kind="record", fields=[RecordField(name="l", type="Left_T")])
        registry: dict[str, DataType] = {"Left_T": left, "Right_T": right}
        with pytest.raises(ValueError, match="dependency cycle"):
            _emit_composite_types(
                ArxmlDocument().data_types, [left, right], DataDescription.last_declared(registry.values())
            )

    def test_a_model_with_no_composites_emits_nothing(self):
        document = ArxmlDocument()
        assert _emit_composite_types(document.data_types, [], DataDescription.last_declared([])) == []
        assert not document.data_types.has_elements


# Three emitted shapes, and no invented twin


def _generated(tmp_path, types: list[dict], interfaces: list[dict] = ()) -> str:
    """*types* and *interfaces* written into a fresh model root and generated, as text."""
    model_root = make_empty_model_root(tmp_path)
    for data in types:
        _write_data_type(model_root, data)
    for data in interfaces:
        (model_root / "interfaces" / f"{data['name']}.yaml").write_text(
            yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8"
        )
    output = tmp_path / "out.arxml"
    generate_arxml(str(model_root), str(output))
    return output.read_text(encoding="utf-8")


_UINT8_BASE = {
    "name": "uint8_base",
    "kind": "base_type",
    "size": 8,
    "encoding": "NONE",
    "native_declaration": "uint8_t",
}
_UINT8 = {"name": "uint8", "kind": "value_type", "base": "uint8_base"}


class TestTheThreeEmittedShapes:
    """One element per declared data type of the three standard kinds, in its standard shape.

    The bytes are restated as literals, since slicing the generator's output would compare the emitter with
    itself; the XSD suite validates all three at both output versions.
    """

    def test_a_base_type_is_one_sw_base_type_with_no_impl_twin(self, tmp_path):
        text = _generated(tmp_path, [_UINT8_BASE])
        assert golden_slice(text, "uint8_base") == (
            "<SW-BASE-TYPE>\n"
            "  <SHORT-NAME>uint8_base</SHORT-NAME>\n"
            "  <CATEGORY>FIXED_LENGTH</CATEGORY>\n"
            "  <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>\n"
            "  <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n"
            "  <NATIVE-DECLARATION>uint8_t</NATIVE-DECLARATION>\n"
            "</SW-BASE-TYPE>"
        )
        assert "IMPLEMENTATION-DATA-TYPE" not in text
        assert "_impl" not in text

    def test_a_value_type_is_one_value_implementation_type_with_one_base_type_ref(self, tmp_path):
        text = _generated(tmp_path, [_UINT8_BASE, _UINT8])
        fragment = golden_slice(text, "uint8")
        assert fragment == (
            "<IMPLEMENTATION-DATA-TYPE>\n"
            "  <SHORT-NAME>uint8</SHORT-NAME>\n"
            "  <CATEGORY>VALUE</CATEGORY>\n"
            "  <SW-DATA-DEF-PROPS>\n"
            "    <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "      <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/Base/uint8_base</BASE-TYPE-REF>\n'
            "        <SW-CALIBRATION-ACCESS>NOT-ACCESSIBLE</SW-CALIBRATION-ACCESS>\n"
            "      </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "    </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "  </SW-DATA-DEF-PROPS>\n"
            "</IMPLEMENTATION-DATA-TYPE>"
        )
        assert fragment.count("BASE-TYPE-REF DEST") == 1
        assert text.count("<IMPLEMENTATION-DATA-TYPE>") == 1, "exactly the one the model declares"

    def test_a_reference_type_is_one_type_reference_implementation_type_with_one_implementation_ref(self, tmp_path):
        text = _generated(
            tmp_path, [_UINT8_BASE, _UINT8, {"name": "Count_T", "kind": "reference_type", "base": "uint8"}]
        )
        fragment = golden_slice(text, "Count_T")
        assert fragment == (
            "<IMPLEMENTATION-DATA-TYPE>\n"
            "  <SHORT-NAME>Count_T</SHORT-NAME>\n"
            "  <CATEGORY>TYPE_REFERENCE</CATEGORY>\n"
            "  <SW-DATA-DEF-PROPS>\n"
            "    <SW-DATA-DEF-PROPS-VARIANTS>\n"
            "      <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '        <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/uint8'
            "</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "      </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "    </SW-DATA-DEF-PROPS-VARIANTS>\n"
            "  </SW-DATA-DEF-PROPS>\n"
            "</IMPLEMENTATION-DATA-TYPE>"
        )
        assert "BASE-TYPE-REF" not in fragment.replace("IMPLEMENTATION-DATA-TYPE-REF", "")
        assert text.count("<IMPLEMENTATION-DATA-TYPE>") == 2, "the value type and the reference type, no third"

    def test_an_interface_names_the_value_or_reference_type_and_no_type_tref_names_a_base_type(self, tmp_path):
        text = _generated(
            tmp_path,
            [_UINT8_BASE, _UINT8, {"name": "Count_T", "kind": "reference_type", "base": "uint8"}],
            [
                {
                    "name": "ICount",
                    "kind": "sender_receiver",
                    "data_elements": [
                        {"name": "raw", "implementation_type": "uint8"},
                        {"name": "count", "implementation_type": "Count_T"},
                    ],
                }
            ],
        )
        assert '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/uint8</TYPE-TREF>' in text
        assert '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/Count_T</TYPE-TREF>' in text
        root = etree.fromstring(text.encode("utf-8"))
        trefs = [(ref.get("DEST"), ref.text) for ref in root.iter(f"{{{AR_NAMESPACE}}}TYPE-TREF")]
        assert trefs and all(dest != "SW-BASE-TYPE" and not path.endswith("_base") for dest, path in trefs), trefs


class TestABaseTypeIsNeverATypeReference:
    """``resolve_type_ref`` answers ``None`` for a base type and every caller refuses, writing nothing.

    A ``SwBaseType`` is not an ``AutosarDataType``, so it cannot type a data prototype; every prototype role
    resolves in ``data_types``, where a base type is not declared.
    """

    def test_the_resolver_answers_none_for_a_base_type(self):
        from swcraft_tools.writer.arxml_generation import resolve_type_ref

        registry = {
            "uint8_base": BaseDataType(name="uint8_base", kind="base_type", size=8, encoding="NONE"),
            "uint8": ValueDataType(name="uint8", kind="value_type", base="uint8_base"),
        }
        description = DataDescription.last_declared(registry.values())
        assert resolve_type_ref("uint8_base", description, implementation=True) is None
        assert resolve_type_ref("uint8_base", description, implementation=False) is None
        assert resolve_type_ref("uint8", description, implementation=True) == "/DataTypes/Implementation/uint8"

    _REFERRERS = {
        "data-element": (
            [],
            [
                {
                    "name": "I",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "raw", "implementation_type": "uint8_base"}],
                }
            ],
        ),
        "argument": (
            [],
            [
                {
                    "name": "I",
                    "kind": "client_server",
                    "operations": [
                        {
                            "name": "Op",
                            "arguments": [{"name": "a", "direction": "in", "implementation_type": "uint8_base"}],
                        }
                    ],
                }
            ],
        ),
        "record-field": ([{"name": "Rec_T", "kind": "record", "fields": [{"name": "f", "type": "uint8_base"}]}], []),
        "array-element": ([{"name": "Arr_T", "kind": "array", "element_type": "uint8_base", "length": 2}], []),
    }

    @pytest.mark.parametrize("referrer", sorted(_REFERRERS))
    def test_a_base_type_named_by_a_prototype_is_refused_and_nothing_is_written(self, referrer, tmp_path):
        types, interfaces = self._REFERRERS[referrer]
        model_root = make_empty_model_root(tmp_path)
        for data in [_UINT8_BASE, *types]:
            _write_data_type(model_root, data)
        for data in interfaces:
            (model_root / "interfaces" / f"{data['name']}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match=r"'uint8_base'.*not declared in data_types/"):
            generate_arxml(str(model_root), str(output))

        assert not output.exists()


class TestTheReferenceChainIsEmittedInDependencyOrder:
    """A ``TYPE_REFERENCE`` type is emitted after the type it references."""

    def test_a_three_deep_chain_declared_top_first_is_emitted_bottom_up(self, tmp_path):
        # The reader orders a layer by declared name, so these names put the top of
        # the chain first: a pass that emitted in reader order would fail here.
        text = _generated(
            tmp_path,
            [
                _UINT8_BASE,
                _UINT8,
                {"name": "A_Top", "kind": "reference_type", "base": "B_Mid"},
                {"name": "B_Mid", "kind": "reference_type", "base": "C_Bottom"},
                {"name": "C_Bottom", "kind": "reference_type", "base": "uint8"},
            ],
        )
        positions = [text.index(f"<SHORT-NAME>{name}</SHORT-NAME>") for name in ("uint8", "C_Bottom", "B_Mid", "A_Top")]
        assert positions == sorted(positions), positions

    def test_a_chain_that_closes_on_itself_is_refused_and_nothing_is_written(self, tmp_path):
        model_root = make_empty_model_root(tmp_path)
        for data in [
            _UINT8_BASE,
            {"name": "Left_T", "kind": "reference_type", "base": "Right_T"},
            {"name": "Right_T", "kind": "reference_type", "base": "Left_T"},
        ]:
            _write_data_type(model_root, data)
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match="dependency cycle"):
            generate_arxml(str(model_root), str(output))

        assert not output.exists()

    @pytest.mark.parametrize(
        ("base", "message"),
        # A reference type's base resolves in `data_types`, which holds no base type.
        [("uint8_base", r"base 'uint8_base' is not declared in data_types/"), ("nosuchtype", "is not declared")],
        ids=["base-type", "undeclared"],
    )
    def test_a_reference_type_whose_base_is_not_an_implementation_type_is_refused(self, base, message, tmp_path):
        model_root = make_empty_model_root(tmp_path)
        for data in [_UINT8_BASE, {"name": "Bad_T", "kind": "reference_type", "base": base}]:
            _write_data_type(model_root, data)
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match=message):
            generate_arxml(str(model_root), str(output))

        assert not output.exists()


class TestACompositeMemberNamesTheDeclaredPhysicalView:
    """The writer derives no view, and refuses what it cannot reference: the model chooses the physical view."""

    def test_a_record_over_two_application_types_references_each_by_its_own_name(self, tmp_path):
        text = _generated(
            tmp_path,
            [
                _UINT8_BASE,
                _UINT8,
                {"name": "Raw_P", "kind": "application_type"},
                {"name": "Count_P", "kind": "application_type", "unit": "1"},
                {
                    "name": "Rec_T",
                    "kind": "record",
                    "fields": [{"name": "raw", "type": "Raw_P"}, {"name": "count", "type": "Count_P"}],
                },
            ],
        )
        assert '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/Raw_P</TYPE-TREF>' in text
        assert '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/DataTypes/Application/Count_P</TYPE-TREF>' in text
        # No map is emitted from a composite member: a mapping set is a declared element, and
        # this model declares none.
        assert "<DATA-TYPE-MAP>" not in text
        assert "_AppView" not in text

    @pytest.mark.parametrize(
        ("kind", "extra"),
        [("value_type", {"base": "uint8_base"}), ("reference_type", {"base": "uint8"})],
    )
    def test_a_member_naming_an_implementation_type_is_refused_as_undeclared(self, tmp_path, kind, extra):
        """A record field's ``type`` resolves in ``application_types`` alone: an implementation type is undeclared."""
        model_root = make_empty_model_root(tmp_path)
        for data in [
            _UINT8_BASE,
            _UINT8,
            {"name": "Stored_T", "kind": kind, **extra},
            {"name": "Rec_T", "kind": "record", "fields": [{"name": "x", "type": "Stored_T"}]},
        ]:
            _write_data_type(model_root, data)
        output = tmp_path / "never-written.arxml"
        with pytest.raises(
            ValueError, match=r"Data type 'Stored_T' referenced by record 'Rec_T' field 'x' is not declared"
        ):
            generate_arxml(str(model_root), str(output))
        assert not output.exists()
