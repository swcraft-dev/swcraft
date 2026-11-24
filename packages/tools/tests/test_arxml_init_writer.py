"""The `*_Init` constants and the `INIT-VALUE`s that reference them.

**The expected fragments are hand-derived, not captured**, from the licensed schemas' own sequences
(`CONSTANT-SPECIFICATION` = `SHORT-NAME` then `VALUE-SPEC`; `VALUE-SPECIFICATION`'s `SHORT-LABEL` ahead of
every subtype's own content; `ARRAY-VALUE-SPECIFICATION/ELEMENTS` and `RECORD-VALUE-SPECIFICATION/FIELDS`,
both a repeated choice of value specifications), in the serialization `test_arxml_xml.py` pins. A slice is
addressed by `SHORT-NAME` through `conftest.golden_slice()`, never by line number.

What this module is **not**:

* an XSD oracle: the licensed schemas are the `test-xsd` stage's;
* the additive-delta gate (`test_init_values_acceptance.py`): this module asserts the positive half, the
  exact bytes of a prototype and both com-spec kinds with and without a value;
* the round trip (`test_arxml_golden.py`): the one reader-facing property here is that every emitted leaf
  is a spelling the reader reads back as the type it went in as.

The refusal tests drive `_emit_init_constants` directly, because the property is that the package holds
nothing when the refusal is raised; a whole-document generation would only witness "no file on disk".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import golden_slice, make_empty_model_root, model_element_storage_name
from swcraft_tools.data_description import DataDescription
from swcraft_tools.reader import read_data_types, read_interfaces, read_layer
from swcraft_tools.schema import SRInterface
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import (
    INIT_CONSTANT_SUFFIX,
    _emit_init_constants,
    generate_arxml,
    init_constant_name,
)
from swcraft_tools.writer.arxml_xml import ArxmlDocument, find_element

# The throwaway model root the whole module generates from

#: Every data type the interfaces below reference. Written as plain documents so
#: the model is authored the way a user's would be — through the loader — rather
#: than assembled as Pydantic objects a YAML file could not produce.
DATA_TYPES: list[dict[str, Any]] = [
    # Every prototype names a value type over a `{name}_base` base type.
    {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
    {"name": "float32_base", "kind": "base_type", "size": 32, "encoding": "IEEE754", "native_declaration": "float32"},
    {"name": "boolean_base", "kind": "base_type", "size": 8, "encoding": "BOOLEAN", "native_declaration": "boolean"},
    {"name": "uint8", "kind": "value_type", "base": "uint8_base"},
    {"name": "float32", "kind": "value_type", "base": "float32_base"},
    {"name": "boolean", "kind": "value_type", "base": "boolean_base"},
    {"name": "Percent_T", "kind": "value_type", "base": "float32_base", "value_min": 0.0, "value_max": 100.0},
    # A composite member names the physical view, so each member below names one of these two
    # instead of the value type that stores it. Neither declares a `range`, so the values the
    # elements carry are judged as under an unbounded value type.
    {"name": "Count_P", "kind": "application_type"},
    {"name": "Measure_P", "kind": "application_type"},
    {"name": "Gear_T", "kind": "enum", "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 2}]},
    {"name": "Pair_T", "kind": "array", "element_type": "Count_P", "length": 2},
    {"name": "FloatPair_T", "kind": "array", "element_type": "Measure_P", "length": 2},
    {
        "name": "Config_T",
        "kind": "record",
        "fields": [{"name": "count", "type": "Count_P"}, {"name": "pair", "type": "Pair_T"}],
    },
    {
        "name": "FloatRec_T",
        "kind": "record",
        "fields": [{"name": "big", "type": "Measure_P"}, {"name": "round", "type": "Measure_P"}],
    },
    # Depth 3: a record whose field is an array of records.
    {"name": "Row_T", "kind": "record", "fields": [{"name": "x", "type": "Count_P"}]},
    {"name": "Rows_T", "kind": "array", "element_type": "Row_T", "length": 2},
    {"name": "Deep_T", "kind": "record", "fields": [{"name": "rows", "type": "Rows_T"}]},
]

#: One interface, every shape, in the order the constants must be emitted in.
#: `plain` deliberately carries no `init` and sits **between** two elements that
#: do, so "data-element order" is not satisfiable by accident.
SIGNALS_ELEMENTS: list[dict[str, Any]] = [
    {"name": "speed", "implementation_type": "uint8", "init": 7},
    {"name": "plain", "implementation_type": "uint8"},
    {"name": "ratio", "implementation_type": "Percent_T", "init": 12.5},
    {"name": "flag", "implementation_type": "boolean", "init": True},
    {"name": "off", "implementation_type": "boolean", "init": False},
    {"name": "gear", "application_type": "Gear_T", "init": "DRIVE"},
    {"name": "pair", "application_type": "Pair_T", "init": [1, 2]},
    {"name": "config", "application_type": "Config_T", "init": {"count": 3, "pair": [4, 5]}},
    {"name": "floats", "application_type": "FloatPair_T", "init": [1e20, 100.0]},
    {"name": "float_rec", "application_type": "FloatRec_T", "init": {"big": 1e20, "round": 100.0}},
    {"name": "deep", "application_type": "Deep_T", "init": {"rows": [{"x": 1}, {"x": 2}]}},
]


def _write(model_root: Path, layer: str, stem: str, document: Any) -> None:
    """Write one element file in the canonical writer form."""
    directory = model_root / layer
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"{stem}.yaml", "w", encoding="utf-8", newline="\n") as handle:
        handle.write(yaml.safe_dump(document, default_flow_style=False, sort_keys=False))


def _write_model(
    base: Path,
    interfaces: list[dict[str, Any]],
    data_types: list[dict[str, Any]] | None = None,
    swcs: list[dict[str, Any]] | None = None,
) -> Path:
    """Create a throwaway model root under *base* carrying *interfaces*."""
    model_root = make_empty_model_root(base)
    for data_type in data_types if data_types is not None else DATA_TYPES:
        # The payload's kind picks the layer directory (a base type: data_types/base/).
        directory = model_element_storage_name(data_type["name"], kind=data_type["kind"]).rpartition("/")[0]
        _write(model_root, directory, data_type["name"], data_type)
    for interface in interfaces:
        _write(model_root, "interfaces", interface["name"], interface)
    for swc in swcs or []:
        _write(model_root, "swc", swc["name"], swc)
    return model_root


def _sr(name: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "kind": "sender_receiver", "data_elements": elements}


def _generate(base: Path, interfaces: list[dict[str, Any]], **kwargs: Any) -> str:
    """Generate a whole document from a throwaway root and return its text."""
    model_root = _write_model(base, interfaces, **kwargs)
    output = base / "model.arxml"
    generate_arxml(str(model_root), str(output))
    return output.read_text(encoding="utf-8")


@pytest.fixture
def signals_document(tmp_path: Path) -> str:
    """The whole document generated from the one interface carrying every shape."""
    return _generate(tmp_path / "signals", [_sr("ISignals", SIGNALS_ELEMENTS)])


# The ported model — the only shape that reaches a com-spec

#: Two data elements, one with an ``init`` and one without, so a com-spec that referenced *every* element
#: rather than every valued one would fail. Small because the port fragments below are frozen in full.
PORTED_ELEMENTS: list[dict[str, Any]] = [
    {"name": "level", "implementation_type": "uint8", "init": 4},
    {"name": "plain", "implementation_type": "uint8"},
]

#: One client-server interface, so a client-server port's bytes can be compared
#: with and without the sender-receiver values beside them.
PORTED_CS: dict[str, Any] = {
    "name": "ICompute",
    "kind": "client_server",
    "operations": [{"name": "run", "arguments": [{"name": "a", "direction": "in", "implementation_type": "uint8"}]}],
}

#: One component carrying all three port kinds: the provide and the require port
#: on the valued interface (without which no com-spec ``INIT-VALUE`` exists at
#: all) and a client-server require port beside them.
PORTED_SWC: dict[str, Any] = {
    "name": "Ported",
    "type": "application",
    "ports": [
        {"name": "pPorted", "direction": "provide", "interface": "IPorted"},
        {"name": "rPorted", "direction": "require", "interface": "IPorted"},
        {"name": "rCompute", "direction": "require", "interface": "ICompute"},
    ],
    "runnables": [],
    "handle_termination_and_restart": "no_support",
}


def _ported(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """The interface the ports name, carrying *elements*."""
    return _sr("IPorted", elements)


@pytest.fixture
def ported_document(tmp_path: Path) -> str:
    """A whole document whose one component has a provide, a require and a CS port."""
    return _generate(
        tmp_path / "ported",
        [_ported(PORTED_ELEMENTS), PORTED_CS],
        swcs=[PORTED_SWC],
    )


# The eighth generated name


class TestTheEighthGeneratedName:
    """`{Interface}_{Element}_Init`.

    An `SR_` prefix is an interface naming convention, not the constant rule's, so an `SR_…` interface keeps one.
    """

    def test_the_suffix_is_the_one_constant_the_form_is_built_from(self) -> None:
        assert INIT_CONSTANT_SUFFIX == "_Init"

    def test_the_name_joins_the_two_declared_names_with_an_underscore(self) -> None:
        assert init_constant_name("ISignals", "speed") == "ISignals_speed_Init"

    def test_an_interface_already_named_sr_gains_no_second_prefix(self) -> None:
        """The derivable name's rule: `SR_Vehicle` + `speed` is `SR_Vehicle_speed_Init`."""
        assert init_constant_name("SR_Vehicle", "speed") == "SR_Vehicle_speed_Init"

    def test_the_form_costs_six_characters_of_the_identifier_budget(self) -> None:
        """One `_` plus `_Init`: the joint budget for the two names is 122."""
        assert len(init_constant_name("", "")) == 6


# Frozen fragments — the three value shapes


class TestScalarValueSpecifications:
    """The scalar leaves of this document, by the value form each takes.

    A number (implementation-typed, or a composite's member) is a ``NUMERICAL-VALUE-SPECIFICATION``; an
    enumeration leaf an ``APPLICATION-VALUE-SPECIFICATION`` whose one ``VT`` is the literal's name. The ``V``
    form and the ``TEXT-VALUE-SPECIFICATION`` are ``test_application_value_form.py``'s and
    ``test_init_value_judgement.py``'s.
    """

    def test_an_integer_scalar(self, signals_document: str) -> None:
        assert golden_slice(signals_document, "ISignals_speed_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>ISignals_speed_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <NUMERICAL-VALUE-SPECIFICATION>\n"
            "      <VALUE>7</VALUE>\n"
            "    </NUMERICAL-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )

    def test_a_float_scalar_under_a_bounded_alias(self, signals_document: str) -> None:
        assert golden_slice(signals_document, "ISignals_ratio_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>ISignals_ratio_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <NUMERICAL-VALUE-SPECIFICATION>\n"
            "      <VALUE>12.5</VALUE>\n"
            "    </NUMERICAL-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )

    @pytest.mark.parametrize(("element", "expected"), [("flag", "1"), ("off", "0")])
    def test_a_boolean_is_one_or_zero_and_never_true_or_false(
        self, signals_document: str, element: str, expected: str
    ) -> None:
        """The lossy `bool` row: it returns as the int."""
        assert golden_slice(signals_document, f"ISignals_{element}_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            f"  <SHORT-NAME>ISignals_{element}_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <NUMERICAL-VALUE-SPECIFICATION>\n"
            f"      <VALUE>{expected}</VALUE>\n"
            "    </NUMERICAL-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )

    def test_an_enum_leaf_is_the_literals_text(self, signals_document: str) -> None:
        """An enum leaf is an application value whose one ``VT`` is the literal's name, no unit, not its number.

        ``[constr_1221]`` asks a value matching one of the scales' texts. `DRIVE` is not the first literal and
        its value is not its index, so an emitter writing an ordinal or a value would fail.
        """
        assert golden_slice(signals_document, "ISignals_gear_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>ISignals_gear_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <APPLICATION-VALUE-SPECIFICATION>\n"
            "      <CATEGORY>VALUE</CATEGORY>\n"
            "      <SW-VALUE-CONT>\n"
            "        <SW-VALUES-PHYS>\n"
            "          <VT>DRIVE</VT>\n"
            "        </SW-VALUES-PHYS>\n"
            "      </SW-VALUE-CONT>\n"
            "    </APPLICATION-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )

    def test_no_scalar_carries_a_short_label_or_a_binding_time(self, signals_document: str) -> None:
        """Both are optional in the schema and both are absent by decision."""
        speed = golden_slice(signals_document, "ISignals_speed_Init")

        assert "SHORT-LABEL" not in speed
        assert "BINDING-TIME" not in signals_document

    def test_the_application_form_only_for_an_enumeration_where_no_value_is_an_application_primitive(
        self, signals_document: str
    ) -> None:
        """A member's number keeps the numerical form; only the enumeration takes the application form, unitless."""
        assert signals_document.count("<APPLICATION-VALUE-SPECIFICATION>") == 1
        assert signals_document.count("<SW-VALUE-CONT>") == 1
        assert "<V>" not in signals_document
        assert "TEXT-VALUE-SPECIFICATION" not in signals_document


class TestCompositeValueSpecifications:
    """`ARRAY-`/`RECORD-VALUE-SPECIFICATION`, and where a `SHORT-LABEL` goes."""

    def test_an_array_of_two_labels_no_member(self, signals_document: str) -> None:
        assert golden_slice(signals_document, "ISignals_pair_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>ISignals_pair_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <ARRAY-VALUE-SPECIFICATION>\n"
            "      <ELEMENTS>\n"
            "        <NUMERICAL-VALUE-SPECIFICATION>\n"
            "          <VALUE>1</VALUE>\n"
            "        </NUMERICAL-VALUE-SPECIFICATION>\n"
            "        <NUMERICAL-VALUE-SPECIFICATION>\n"
            "          <VALUE>2</VALUE>\n"
            "        </NUMERICAL-VALUE-SPECIFICATION>\n"
            "      </ELEMENTS>\n"
            "    </ARRAY-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )

    def test_a_record_labels_every_member_including_a_composite_one(self, signals_document: str) -> None:
        """The label is the declared field name, on the member's own value spec."""
        assert golden_slice(signals_document, "ISignals_config_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>ISignals_config_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <RECORD-VALUE-SPECIFICATION>\n"
            "      <FIELDS>\n"
            "        <NUMERICAL-VALUE-SPECIFICATION>\n"
            "          <SHORT-LABEL>count</SHORT-LABEL>\n"
            "          <VALUE>3</VALUE>\n"
            "        </NUMERICAL-VALUE-SPECIFICATION>\n"
            "        <ARRAY-VALUE-SPECIFICATION>\n"
            "          <SHORT-LABEL>pair</SHORT-LABEL>\n"
            "          <ELEMENTS>\n"
            "            <NUMERICAL-VALUE-SPECIFICATION>\n"
            "              <VALUE>4</VALUE>\n"
            "            </NUMERICAL-VALUE-SPECIFICATION>\n"
            "            <NUMERICAL-VALUE-SPECIFICATION>\n"
            "              <VALUE>5</VALUE>\n"
            "            </NUMERICAL-VALUE-SPECIFICATION>\n"
            "          </ELEMENTS>\n"
            "        </ARRAY-VALUE-SPECIFICATION>\n"
            "      </FIELDS>\n"
            "    </RECORD-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )

    def test_a_record_member_follows_declared_field_order_not_the_maps_own(self, tmp_path: Path) -> None:
        """The walk is over the **declared type**, so an author's key order is irrelevant."""
        text = _generate(
            tmp_path,
            [_sr("IOrder", [{"name": "config", "application_type": "Config_T", "init": {"pair": [4, 5], "count": 3}}])],
        )
        fragment = golden_slice(text, "IOrder_config_Init")

        assert fragment.index("<SHORT-LABEL>count</SHORT-LABEL>") < fragment.index("<SHORT-LABEL>pair</SHORT-LABEL>")

    def test_three_levels_of_nesting_record_array_record(self, signals_document: str) -> None:
        """Three levels: a record inside an array inside a record."""
        assert golden_slice(signals_document, "ISignals_deep_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>ISignals_deep_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <RECORD-VALUE-SPECIFICATION>\n"
            "      <FIELDS>\n"
            "        <ARRAY-VALUE-SPECIFICATION>\n"
            "          <SHORT-LABEL>rows</SHORT-LABEL>\n"
            "          <ELEMENTS>\n"
            "            <RECORD-VALUE-SPECIFICATION>\n"
            "              <FIELDS>\n"
            "                <NUMERICAL-VALUE-SPECIFICATION>\n"
            "                  <SHORT-LABEL>x</SHORT-LABEL>\n"
            "                  <VALUE>1</VALUE>\n"
            "                </NUMERICAL-VALUE-SPECIFICATION>\n"
            "              </FIELDS>\n"
            "            </RECORD-VALUE-SPECIFICATION>\n"
            "            <RECORD-VALUE-SPECIFICATION>\n"
            "              <FIELDS>\n"
            "                <NUMERICAL-VALUE-SPECIFICATION>\n"
            "                  <SHORT-LABEL>x</SHORT-LABEL>\n"
            "                  <VALUE>2</VALUE>\n"
            "                </NUMERICAL-VALUE-SPECIFICATION>\n"
            "              </FIELDS>\n"
            "            </RECORD-VALUE-SPECIFICATION>\n"
            "          </ELEMENTS>\n"
            "        </ARRAY-VALUE-SPECIFICATION>\n"
            "      </FIELDS>\n"
            "    </RECORD-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )


class TestAFloatKeepsItsTypeAtEveryDepth:
    """A float keeps its type at the three depths it can occur at.

    `1e20` and `100.0` (which `normalize()` prints `100`) would read back as `int` without the appended `.0`.
    """

    def test_at_depth_zero(self, tmp_path: Path) -> None:
        text = _generate(tmp_path, [_sr("IScalar", [{"name": "big", "implementation_type": "float32", "init": 1e20}])])

        assert "<VALUE>100000000000000000000.0</VALUE>" in golden_slice(text, "IScalar_big_Init")

    def test_inside_an_array_value(self, signals_document: str) -> None:
        assert golden_slice(signals_document, "ISignals_floats_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>ISignals_floats_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <ARRAY-VALUE-SPECIFICATION>\n"
            "      <ELEMENTS>\n"
            "        <NUMERICAL-VALUE-SPECIFICATION>\n"
            "          <VALUE>100000000000000000000.0</VALUE>\n"
            "        </NUMERICAL-VALUE-SPECIFICATION>\n"
            "        <NUMERICAL-VALUE-SPECIFICATION>\n"
            "          <VALUE>100.0</VALUE>\n"
            "        </NUMERICAL-VALUE-SPECIFICATION>\n"
            "      </ELEMENTS>\n"
            "    </ARRAY-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )

    def test_inside_a_record_value(self, signals_document: str) -> None:
        assert golden_slice(signals_document, "ISignals_float_rec_Init") == (
            "<CONSTANT-SPECIFICATION>\n"
            "  <SHORT-NAME>ISignals_float_rec_Init</SHORT-NAME>\n"
            "  <VALUE-SPEC>\n"
            "    <RECORD-VALUE-SPECIFICATION>\n"
            "      <FIELDS>\n"
            "        <NUMERICAL-VALUE-SPECIFICATION>\n"
            "          <SHORT-LABEL>big</SHORT-LABEL>\n"
            "          <VALUE>100000000000000000000.0</VALUE>\n"
            "        </NUMERICAL-VALUE-SPECIFICATION>\n"
            "        <NUMERICAL-VALUE-SPECIFICATION>\n"
            "          <SHORT-LABEL>round</SHORT-LABEL>\n"
            "          <VALUE>100.0</VALUE>\n"
            "        </NUMERICAL-VALUE-SPECIFICATION>\n"
            "      </FIELDS>\n"
            "    </RECORD-VALUE-SPECIFICATION>\n"
            "  </VALUE-SPEC>\n"
            "</CONSTANT-SPECIFICATION>"
        )


# Where the constants go, and which elements get one


class TestConstantPlacement:
    """In `PortInterfaces`, immediately before their interface, in element order."""

    def test_every_constant_is_a_sibling_of_the_interfaces(self, signals_document: str) -> None:
        from lxml import etree

        root = etree.fromstring(signals_document.encode("utf-8"))

        assert find_element(root, "/PortInterfaces/ISignals_speed_Init") is not None
        assert find_element(root, "/PortInterfaces/ISignals") is not None

    def test_the_constants_precede_their_own_interface(self, signals_document: str) -> None:
        """References point backwards — the `COMPU-METHOD`-before-its-enum precedent."""
        interface = signals_document.index("<SHORT-NAME>ISignals</SHORT-NAME>")

        for element in SIGNALS_ELEMENTS:
            if "init" not in element:
                continue
            name = init_constant_name("ISignals", element["name"])
            assert signals_document.index(f"<SHORT-NAME>{name}</SHORT-NAME>") < interface, name

    def test_the_constants_follow_data_element_order(self, signals_document: str) -> None:
        positions = [
            signals_document.index(f"<SHORT-NAME>{init_constant_name('ISignals', element['name'])}</SHORT-NAME>")
            for element in SIGNALS_ELEMENTS
            if "init" in element
        ]

        assert positions == sorted(positions)

    def test_an_element_without_init_gets_no_constant(self, signals_document: str) -> None:
        assert "ISignals_plain_Init" not in signals_document

    def test_each_interfaces_constants_sit_between_the_two_interfaces(self, tmp_path: Path) -> None:
        """Two interfaces, so "immediately before" is distinguishable from "all first"."""
        text = _generate(
            tmp_path,
            [
                _sr("IFirst", [{"name": "a", "implementation_type": "uint8", "init": 1}]),
                _sr("ISecond", [{"name": "b", "implementation_type": "uint8", "init": 2}]),
            ],
        )

        first_constant = text.index("<SHORT-NAME>IFirst_a_Init</SHORT-NAME>")
        first = text.index("<SHORT-NAME>IFirst</SHORT-NAME>")
        second_constant = text.index("<SHORT-NAME>ISecond_b_Init</SHORT-NAME>")
        second = text.index("<SHORT-NAME>ISecond</SHORT-NAME>")

        assert first_constant < first < second_constant < second

    def test_an_interface_with_no_init_anywhere_emits_no_constant(self, tmp_path: Path) -> None:
        text = _generate(
            tmp_path,
            [
                _sr(
                    "IBare",
                    [{"name": "a", "implementation_type": "uint8"}, {"name": "b", "application_type": "Pair_T"}],
                )
            ],
        )

        assert "CONSTANT-SPECIFICATION" not in text
        assert INIT_CONSTANT_SUFFIX not in text

    def test_a_client_server_interface_is_untouched(self, tmp_path: Path) -> None:
        text = _generate(
            tmp_path,
            [
                {
                    "name": "ICompute",
                    "kind": "client_server",
                    "operations": [
                        {"name": "run", "arguments": [{"name": "a", "direction": "in", "implementation_type": "uint8"}]}
                    ],
                }
            ],
        )

        assert "CONSTANT-SPECIFICATION" not in text


#: The one reference shape, at the one indentation `golden_slice()` dedents to, hand-derived from the
#: schemas' sequences: `INIT-VALUE` carries a `CONSTANT-REFERENCE`, which carries a `CONSTANT-REF` whose
#: `DEST` is `CONSTANT-SPECIFICATION` and whose text is the constant's package path.
def _reference(indent: str, constant: str) -> str:
    return (
        f"{indent}<INIT-VALUE>\n"
        f"{indent}  <CONSTANT-REFERENCE>\n"
        f'{indent}    <CONSTANT-REF DEST="CONSTANT-SPECIFICATION">/PortInterfaces/{constant}</CONSTANT-REF>\n'
        f"{indent}  </CONSTANT-REFERENCE>\n"
        f"{indent}</INIT-VALUE>\n"
    )


class TestTheInitValueOnThePrototype:
    """The first insertion point: after `TYPE-TREF`, on the element's own prototype.

    `INIT-VALUE` opens the `VARIABLE-DATA-PROTOTYPE` group, which follows the `AUTOSAR-DATA-PROTOTYPE` group
    `TYPE-TREF` belongs to; it is the last child only because this writer emits no `VARIATION-POINT`.
    """

    def test_a_valued_element_carries_the_reference_after_its_type_tref(self, signals_document: str) -> None:
        assert golden_slice(signals_document, "speed") == (
            "<VARIABLE-DATA-PROTOTYPE>\n"
            "  <SHORT-NAME>speed</SHORT-NAME>\n"
            '  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/uint8</TYPE-TREF>\n'
            + _reference("  ", "ISignals_speed_Init")
            + "</VARIABLE-DATA-PROTOTYPE>"
        )

    def test_an_element_without_init_is_the_prototype_0_7_0_emitted(self, signals_document: str) -> None:
        """An element without `init` carries no `INIT-VALUE`, byte for byte."""
        assert golden_slice(signals_document, "plain") == (
            "<VARIABLE-DATA-PROTOTYPE>\n"
            "  <SHORT-NAME>plain</SHORT-NAME>\n"
            '  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/uint8</TYPE-TREF>\n'
            "</VARIABLE-DATA-PROTOTYPE>"
        )

    def test_every_valued_element_gets_exactly_one_reference(self, signals_document: str) -> None:
        valued = [element for element in SIGNALS_ELEMENTS if "init" in element]

        for element in valued:
            fragment = golden_slice(signals_document, element["name"], tag="VARIABLE-DATA-PROTOTYPE")
            assert fragment.count("<INIT-VALUE>") == 1, element["name"]
            assert init_constant_name("ISignals", element["name"]) in fragment

    def test_the_reference_follows_the_type_tref_and_never_precedes_it(self, signals_document: str) -> None:
        """The schema refuses the swapped order, so it never occurs."""
        fragment = golden_slice(signals_document, "speed")

        assert fragment.index("<TYPE-TREF") < fragment.index("<INIT-VALUE>")

    def test_a_composite_value_is_referenced_and_never_inlined(self, signals_document: str) -> None:
        """The port and the prototype repeat the reference, never a value."""
        fragment = golden_slice(signals_document, "config", tag="VARIABLE-DATA-PROTOTYPE")

        assert "ISignals_config_Init" in fragment
        assert "RECORD-VALUE-SPECIFICATION" not in fragment


class TestTheInitValueOnTheTwoComSpecs:
    """The other two insertion points, both frozen in full.

    The receiver's `INIT-VALUE` follows `HANDLE-TIMEOUT-TYPE` (after `HANDLE-NEVER-RECEIVED` in the schema
    sequence) and the sender's `USES-END-TO-END-PROTECTION`. The XSD evidence for the receiver's neighbour is
    `test_arxml_xsd_conformance.py::TestTheWriterDefaultsConform`.
    """

    def test_a_provided_sender_receiver_port(self, ported_document: str) -> None:
        assert golden_slice(ported_document, "pPorted") == (
            "<P-PORT-PROTOTYPE>\n"
            "  <SHORT-NAME>pPorted</SHORT-NAME>\n"
            "  <PROVIDED-COM-SPECS>\n"
            "    <NONQUEUED-SENDER-COM-SPEC>\n"
            '      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/IPorted/level'
            "</DATA-ELEMENT-REF>\n"
            "      <HANDLE-OUT-OF-RANGE>NONE</HANDLE-OUT-OF-RANGE>\n"
            "      <USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION>\n"
            + _reference("      ", "IPorted_level_Init")
            + "    </NONQUEUED-SENDER-COM-SPEC>\n"
            "    <NONQUEUED-SENDER-COM-SPEC>\n"
            '      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/IPorted/plain'
            "</DATA-ELEMENT-REF>\n"
            "      <HANDLE-OUT-OF-RANGE>NONE</HANDLE-OUT-OF-RANGE>\n"
            "      <USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION>\n"
            "    </NONQUEUED-SENDER-COM-SPEC>\n"
            "  </PROVIDED-COM-SPECS>\n"
            '  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/PortInterfaces/IPorted'
            "</PROVIDED-INTERFACE-TREF>\n"
            "</P-PORT-PROTOTYPE>"
        )

    def test_a_required_sender_receiver_port(self, ported_document: str) -> None:
        assert golden_slice(ported_document, "rPorted") == (
            "<R-PORT-PROTOTYPE>\n"
            "  <SHORT-NAME>rPorted</SHORT-NAME>\n"
            "  <REQUIRED-COM-SPECS>\n"
            "    <NONQUEUED-RECEIVER-COM-SPEC>\n"
            '      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/IPorted/level'
            "</DATA-ELEMENT-REF>\n"
            "      <HANDLE-OUT-OF-RANGE>NONE</HANDLE-OUT-OF-RANGE>\n"
            "      <USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION>\n"
            "      <ALIVE-TIMEOUT>0</ALIVE-TIMEOUT>\n"
            "      <ENABLE-UPDATE>false</ENABLE-UPDATE>\n"
            "      <HANDLE-NEVER-RECEIVED>false</HANDLE-NEVER-RECEIVED>\n"
            "      <HANDLE-TIMEOUT-TYPE>NONE</HANDLE-TIMEOUT-TYPE>\n"
            + _reference("      ", "IPorted_level_Init")
            + "    </NONQUEUED-RECEIVER-COM-SPEC>\n"
            "    <NONQUEUED-RECEIVER-COM-SPEC>\n"
            '      <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/IPorted/plain'
            "</DATA-ELEMENT-REF>\n"
            "      <HANDLE-OUT-OF-RANGE>NONE</HANDLE-OUT-OF-RANGE>\n"
            "      <USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION>\n"
            "      <ALIVE-TIMEOUT>0</ALIVE-TIMEOUT>\n"
            "      <ENABLE-UPDATE>false</ENABLE-UPDATE>\n"
            "      <HANDLE-NEVER-RECEIVED>false</HANDLE-NEVER-RECEIVED>\n"
            "      <HANDLE-TIMEOUT-TYPE>NONE</HANDLE-TIMEOUT-TYPE>\n"
            "    </NONQUEUED-RECEIVER-COM-SPEC>\n"
            "  </REQUIRED-COM-SPECS>\n"
            '  <REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/PortInterfaces/IPorted'
            "</REQUIRED-INTERFACE-TREF>\n"
            "</R-PORT-PROTOTYPE>"
        )

    def test_the_receivers_reference_is_the_com_specs_last_child(self, ported_document: str) -> None:
        """`HANDLE-TIMEOUT-TYPE` then `INIT-VALUE`, in that order and nothing between."""
        body = [line.strip() for line in golden_slice(ported_document, "rPorted").splitlines()]

        assert body.index("<HANDLE-TIMEOUT-TYPE>NONE</HANDLE-TIMEOUT-TYPE>") + 1 == body.index("<INIT-VALUE>")

    def test_the_senders_reference_follows_uses_end_to_end_protection(self, ported_document: str) -> None:
        body = [line.strip() for line in golden_slice(ported_document, "pPorted").splitlines()]

        assert body.index("<USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION>") + 1 == body.index(
            "<INIT-VALUE>"
        )

    def test_a_client_server_port_is_byte_identical_with_and_without_the_values(self, tmp_path: Path) -> None:
        """The port kind that can never carry a value is unchanged by the values beside it."""
        with_values = _generate(tmp_path / "with", [_ported(PORTED_ELEMENTS), PORTED_CS], swcs=[PORTED_SWC])
        without = _generate(
            tmp_path / "without",
            [
                _ported(
                    [{key: value for key, value in element.items() if key != "init"} for element in PORTED_ELEMENTS]
                ),
                PORTED_CS,
            ],
            swcs=[PORTED_SWC],
        )

        assert golden_slice(with_values, "rCompute") == golden_slice(without, "rCompute")
        assert "INIT-VALUE" not in golden_slice(with_values, "rCompute")

    def test_a_port_on_an_interface_with_no_values_is_byte_identical(self, tmp_path: Path) -> None:
        """The whole-port form of the same claim, for the sender-receiver kind."""
        bare_elements = [{key: value for key, value in element.items() if key != "init"} for element in PORTED_ELEMENTS]
        without = _generate(tmp_path / "without", [_ported(bare_elements), PORTED_CS], swcs=[PORTED_SWC])

        assert "INIT-VALUE" not in without
        assert "CONSTANT-SPECIFICATION" not in without


class TestEveryConstantReferenceResolves:
    """No reference points at a constant the same document does not carry, or at another element's.

    The walk resolves through the writer's own `find_element()`, the way `arxml_reader.py` does.
    """

    @staticmethod
    def _references(text: str) -> list[str]:
        from lxml import etree

        root = etree.fromstring(text.encode("utf-8"))
        return [
            node.text or ""
            for node in root.iter(f"{{{root.nsmap[None]}}}CONSTANT-REF")  # type: ignore[index]
        ]

    def test_every_reference_in_the_ported_document_resolves(self, ported_document: str) -> None:
        from lxml import etree

        root = etree.fromstring(ported_document.encode("utf-8"))
        references = self._references(ported_document)

        assert references, "the document carries no CONSTANT-REF at all"
        for path in references:
            assert find_element(root, path) is not None, path

    def test_every_reference_in_the_signals_document_resolves(self, signals_document: str) -> None:
        from lxml import etree

        root = etree.fromstring(signals_document.encode("utf-8"))
        references = self._references(signals_document)

        assert len(references) == len([element for element in SIGNALS_ELEMENTS if "init" in element])
        for path in references:
            assert find_element(root, path) is not None, path

    def test_the_ported_documents_references_are_the_three_the_model_asks_for(self, ported_document: str) -> None:
        """One prototype plus one per com-spec whose element carries a value."""
        assert self._references(ported_document) == ["/PortInterfaces/IPorted_level_Init"] * 3

    def test_the_walk_is_not_blind(self, signals_document: str) -> None:
        """Non-vacuity: `find_element()` really answers None for a path that is absent."""
        from lxml import etree

        root = etree.fromstring(signals_document.encode("utf-8"))

        assert find_element(root, "/PortInterfaces/ISignals_plain_Init") is None


# The refusals — the writer's half


class TestTheWriterRefusesAValueItCannotSpell:
    """A refusal raises before anything is created, and leaves no file.

    `check-model` reports these as `rule:interface.init-string-scalar` and `rule:interface.init-unwritable-integer`.
    """

    @staticmethod
    def _emit(interface: SRInterface, types: list[Any]) -> ArxmlDocument:
        """Drive `_emit_init_constants` directly against a fresh document."""
        document = ArxmlDocument()
        _emit_init_constants(
            document.port_interfaces, interface, DataDescription.last_declared({dt.name: dt for dt in types}.values())
        )
        return document

    @pytest.fixture
    def types(self, tmp_path: Path) -> list[Any]:
        model_root = _write_model(tmp_path, [])
        return [
            *read_layer(str(model_root), "base_types"),
            *read_data_types(str(model_root)),
            *read_layer(str(model_root), "application_types"),
        ]

    def test_a_string_scalar_is_refused_by_name(self, types: list[Any]) -> None:
        interface = SRInterface.model_validate(
            _sr("IStrings", [{"name": "label", "implementation_type": "uint8", "init": "abc"}])
        )

        with pytest.raises(ValueError, match="label"):
            self._emit(interface, types)

    def test_a_string_scalar_nested_in_a_composite_is_refused_too(self, types: list[Any]) -> None:
        interface = SRInterface.model_validate(
            _sr("IStrings", [{"name": "pair", "application_type": "Pair_T", "init": ["a", 2]}])
        )

        with pytest.raises(ValueError, match="pair"):
            self._emit(interface, types)

    def test_the_refusal_names_the_string_and_leaves_the_package_empty(self, types: list[Any]) -> None:
        """ "Resolve everything first": a refused interface leaves nothing behind."""
        interface = SRInterface.model_validate(
            _sr(
                "IStrings",
                [
                    {"name": "ok", "implementation_type": "uint8", "init": 1},
                    {"name": "bad", "implementation_type": "uint8", "init": "abc"},
                ],
            )
        )
        document = ArxmlDocument()

        with pytest.raises(ValueError) as excinfo:
            _emit_init_constants(
                document.port_interfaces,
                interface,
                DataDescription.last_declared({dt.name: dt for dt in types}.values()),
            )

        assert "'abc'" in str(excinfo.value)
        assert document.port_interfaces.has_elements is False

    def test_a_quoted_zero_one_zero_is_a_string_and_is_refused(self, types: list[Any]) -> None:
        """A quoted `"010"` is a string, refused rather than read as a number."""
        interface = SRInterface.model_validate(
            _sr("IStrings", [{"name": "code", "implementation_type": "uint8", "init": "010"}])
        )

        with pytest.raises(ValueError, match="code"):
            self._emit(interface, types)

    def test_an_integer_this_interpreter_cannot_spell_is_refused_by_name(self, types: list[Any]) -> None:
        """The bound is the interpreter's, read at run time and never hard-coded."""
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip("this interpreter has no integer-to-string limit, so nothing is unwritable")
        interface = SRInterface.model_validate(
            _sr("IHuge", [{"name": "huge", "implementation_type": "uint8", "init": 10 ** (limit + 10)}])
        )
        document = ArxmlDocument()

        with pytest.raises(ValueError, match="huge"):
            _emit_init_constants(
                document.port_interfaces,
                interface,
                DataDescription.last_declared({dt.name: dt for dt in types}.values()),
            )

        assert document.port_interfaces.has_elements is False

    def test_the_unwritable_integer_message_names_bits_and_never_digits(self, types: list[Any]) -> None:
        """A digit count of such a value is the conversion that raises (`rendering.py`'s precedent)."""
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip("this interpreter has no integer-to-string limit, so nothing is unwritable")
        value = 10 ** (limit + 10)
        interface = SRInterface.model_validate(
            _sr("IHuge", [{"name": "huge", "implementation_type": "uint8", "init": value}])
        )

        with pytest.raises(ValueError) as excinfo:
            self._emit(interface, types)

        assert f"{value.bit_length()} bits" in str(excinfo.value)

    def test_a_boolean_is_not_mistaken_for_an_unwritable_integer(self, types: list[Any]) -> None:
        """`bool` is an `int` subclass; the bound applies to `int` and never `bool`."""
        interface = SRInterface.model_validate(
            _sr("IFlags", [{"name": "flag", "implementation_type": "boolean", "init": True}])
        )

        assert self._emit(interface, types).port_interfaces.has_elements is True

    def test_a_refused_model_writes_no_file(self, tmp_path: Path) -> None:
        """End to end: a rejected model leaves no partial document."""
        model_root = _write_model(
            tmp_path, [_sr("IStrings", [{"name": "bad", "implementation_type": "uint8", "init": "abc"}])]
        )
        output = tmp_path / "model.arxml"

        with pytest.raises(ValueError, match="bad"):
            generate_arxml(str(model_root), str(output))

        assert not output.exists()


class TestTheGateAndTheWriterAgreeAboutAValueTheExporterCannotSpell:
    """`check-model` and the writer refuse the same model, naming the same element and rule identifier.

    Not claimed: the same sentence (the gate emits one record per value, the writer one batched `ValueError`,
    deliberately); nor that the writer reports a value the gate misses, which the refusal class above covers.
    """

    @pytest.mark.parametrize(
        ("label", "init", "rule"),
        [
            ("an alphabetic string", "abc", "interface.init-string-scalar"),
            ("a quoted numeric string", "010", "interface.init-string-scalar"),
        ],
        ids=lambda value: value if isinstance(value, str) and " " in value else "",
    )
    def test_both_surfaces_refuse_the_same_element_under_the_same_rule(
        self, tmp_path: Path, label: str, init: str, rule: str
    ) -> None:
        model_root = _write_model(
            tmp_path, [_sr("IStrings", [{"name": "offender", "implementation_type": "uint8", "init": init}])]
        )

        result = check_cross_layer_consistency(str(model_root))
        reported = [record for record in result.records if record.rule == rule]

        assert [record.rule for record in result.records if record.rule.startswith("interface.init-")] == [rule], label
        assert "offender" in reported[0].message

        with pytest.raises(ValueError) as excinfo:
            generate_arxml(str(model_root), str(tmp_path / "model.arxml"))

        assert "offender" in str(excinfo.value)
        assert rule in str(excinfo.value), "the writer names the rule the finding carries"

    def test_the_unwritable_integer_agrees_too(self, tmp_path: Path) -> None:
        """The hand-authored `0x…` spelling, the only way such a value loads.

        Written as text: `yaml.safe_dump` raises on the value itself (`rendering.py`'s stated limit).
        """
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip("this interpreter has no integer-to-string limit, so nothing is unwritable")
        model_root = _write_model(tmp_path, [])
        (model_root / "interfaces").mkdir(parents=True, exist_ok=True)
        (model_root / "interfaces" / "IHuge.yaml").write_text(
            "name: IHuge\nkind: sender_receiver\ndata_elements:\n"
            f"  - name: offender\n    implementation_type: uint8\n    init: 0x{'f' * limit}\n",
            encoding="utf-8",
            newline="\n",
        )

        result = check_cross_layer_consistency(str(model_root))

        assert [record.rule for record in result.records if record.rule.startswith("interface.init-")] == [
            "interface.init-unwritable-integer"
        ]

        with pytest.raises(ValueError) as excinfo:
            generate_arxml(str(model_root), str(tmp_path / "model.arxml"))

        assert "offender" in str(excinfo.value)
        assert "interface.init-unwritable-integer" in str(excinfo.value)

    def test_a_value_both_surfaces_accept_is_written(self, tmp_path: Path) -> None:
        """Non-vacuity: the agreement is about refusals, not about refusing everything."""
        model_root = _write_model(
            tmp_path, [_sr("IStrings", [{"name": "offender", "implementation_type": "uint8", "init": 7}])]
        )

        assert [
            record.rule
            for record in check_cross_layer_consistency(str(model_root)).records
            if record.rule.startswith("interface.init-")
        ] == []

        output = tmp_path / "model.arxml"
        generate_arxml(str(model_root), str(output))

        assert "IStrings_offender_Init" in output.read_text(encoding="utf-8")


class TestTheExistingRefusalsStillCome_First:
    """An unresolvable type is the interface emitter's message, not the constant's."""

    def test_an_undeclared_type_still_fails_with_the_interfaces_own_message(self, tmp_path: Path) -> None:
        model_root = _write_model(
            tmp_path, [_sr("IGhost", [{"name": "a", "implementation_type": "Missing_T", "init": 1}])]
        )

        with pytest.raises(ValueError, match="Data type 'Missing_T' not declared"):
            generate_arxml(str(model_root), str(tmp_path / "model.arxml"))


# Non-vacuity


def test_the_fixture_model_really_declares_every_shape(tmp_path: Path) -> None:
    """The model the frozen fragments are generated from is the one they claim."""
    model_root = _write_model(tmp_path, [_sr("ISignals", SIGNALS_ELEMENTS)])
    interfaces = read_interfaces(str(model_root))
    (signals,) = [iface for iface in interfaces if isinstance(iface, SRInterface)]
    with_init = [element for element in signals.data_elements if element.init is not None]

    assert len(signals.data_elements) == len(SIGNALS_ELEMENTS)
    assert len(with_init) == len(SIGNALS_ELEMENTS) - 1
    assert {type(element.init).__name__ for element in with_init} == {"int", "float", "bool", "str", "list", "dict"}
