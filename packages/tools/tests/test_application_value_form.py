"""The application value form: an application primitive's value, its unit and its judgement.

The writer: a prototype's own value of a non-enumeration application primitive is an
``APPLICATION-VALUE-SPECIFICATION`` (``[constr_1221]``) with ``CATEGORY`` ``VALUE``, the type's own
``UNIT-REF`` where it states a unit, and one ``V``. A member's number keeps the numerical form; an
enumeration leaf is its literal's text.

The importer judges such a value by the **source's** type -- the application form under an application
primitive, never under an implementation type (``[constr_1385]``); a member's number is carried in either
form; a collapsed twin's value and a foreign unit each take one descriptive line; a port's own value that
is not carried is recorded with its category and rule.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, write_model_element
from lxml import etree
from project_support import init_project as _init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.type_mapping import (
    APPLICATION_TEXT_WHERE_A_NUMBER_IS_TAKEN,
    APPLICATION_VALUE_UNDER_AN_IMPLEMENTATION_TYPE,
    NUMBER_WHERE_AN_APPLICATION_VALUE_IS_TAKEN,
    TWIN_NUMERICAL_SENTENCE,
    VALUE_UNIT_SENTENCE,
    SourceTypes,
    carried_value_lines,
    init_value_unit_differs,
    judge_init_value,
)
from swcraft_tools.arxml_reader import (
    AppPrimitiveDataTypeInfo,
    AppRecordDataTypeInfo,
    CompuMethodInfo,
    CompuScaleInfo,
    ConstantArray,
    ConstantRecord,
    ConstantRecordField,
    ConstantScalar,
    DataTypeMapInfo,
    DataTypeMappingSetInfo,
    ImplDataTypeInfo,
    ImportModel,
    RecordElementInfo,
    SkipCategory,
    StandardRule,
    SwBaseTypeInfo,
    ValueForm,
)
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml
from test_init_value_judgement import _CONSTANT, _element, _mapped

INIT_VALUES_MODEL_ROOT = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "init_values" / "model"

#: The prototypes' own values the extended tree adds to ``ISignals``: each type, each unit form, and
#: an integer, a negative, a zero, a fraction, a very large and a very small value, as an ``int`` and
#: as a ``float``.
OWN_VALUES: dict[str, tuple[str, Any]] = {
    "own_bare": ("Count_T", 42),
    "own_unit": ("Sample_T", 2.5),
    "own_ref": ("Ref_T", -7),
    "own_zero": ("Sample_T", 0),
    "own_fraction": ("Count_T", 0.125),
    "own_large": ("Count_T", 1e20),
    "own_large_int": ("Count_T", 10**20),
    "own_small": ("Count_T", 1e-7),
    "own_bool": ("Count_T", True),
}


def application_value_model_root(tmp_path: Path) -> Path:
    """A copy of ``fixtures/init_values``' model with a prototype's own value under every application type form.

    The copy adds a ``unit`` element ``Volt`` named by ``unit_ref`` from ``Ref_T``, the data elements of
    :data:`OWN_VALUES`, and a port's own value under ``Sample_T``.
    """
    model_root = tmp_path / "model"
    shutil.copytree(INIT_VALUES_MODEL_ROOT, model_root)
    write_model_element(model_root, {"name": "Volt", "display_name": "V"}, layer="units")
    write_model_element(model_root, {"name": "Ref_T", "kind": "application_type", "unit_ref": "Volt"})
    mapping_set = model_root / "data_types" / "mapping_sets" / "PhysicalTypes.yaml"
    document = yaml.safe_load(mapping_set.read_text(encoding="utf-8"))
    document["maps"].append({"application": "Ref_T", "implementation": "float32"})
    mapping_set.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    interface = model_root / "interfaces" / "ISignals.yaml"
    document = yaml.safe_load(interface.read_text(encoding="utf-8"))
    for name, (type_name, value) in OWN_VALUES.items():
        document["data_elements"].append({"name": name, "application_type": type_name, "init": value})
    interface.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    component = model_root / "swc" / "signals" / "SignalsSwc.yaml"
    document = yaml.safe_load(component.read_text(encoding="utf-8"))
    ports = {port["name"]: port for port in document["ports"]}
    ports["pSignals"]["com_specs"] = [{"data_element": "own_unit", "init": 7.5}]
    component.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return model_root


def _generated(tmp_path: Path, version: str = "4.3.0") -> etree._Element:
    output = tmp_path / f"application-values-{version}.arxml"
    generate_arxml(str(application_value_model_root(tmp_path)), str(output), version=version)
    return etree.parse(str(output)).getroot()


def _ns(tag: str) -> str:
    return f"{{http://autosar.org/schema/r4.0}}{tag}"


def _constant_value(root: etree._Element, element: str) -> etree._Element:
    (constant,) = [
        node
        for node in root.iter(_ns("CONSTANT-SPECIFICATION"))
        if node.findtext(_ns("SHORT-NAME")) == f"ISignals_{element}_Init"
    ]
    (value,) = list(constant.find(_ns("VALUE-SPEC")))
    return value


def _shape(node: etree._Element) -> str:
    """``TAG=text`` for a leaf, ``TAG[child,child]`` otherwise; ``DEST`` kept, other attributes left out."""
    tag = etree.QName(node).localname
    children = [child for child in node if isinstance(child.tag, str)]
    if not children:
        dest = f"@{node.get('DEST')}" if node.get("DEST") else ""
        return f"{tag}{dest}={node.text}"
    return f"{tag}[{','.join(_shape(child) for child in children)}]"


# The writer


class TestAPrototypesOwnValueIsAnApplicationValue:
    """A prototype's own value of an application primitive, at both versions."""

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_without_a_unit(self, version: str, tmp_path: Path) -> None:
        assert _shape(_constant_value(_generated(tmp_path, version), "own_bare")) == (
            "APPLICATION-VALUE-SPECIFICATION[CATEGORY=VALUE,SW-VALUE-CONT[SW-VALUES-PHYS[V=42]]]"
        )

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_with_a_unit_stated_inline_names_the_types_own_generated_unit(self, version: str, tmp_path: Path) -> None:
        root = _generated(tmp_path, version)
        assert _shape(_constant_value(root, "own_unit")) == (
            "APPLICATION-VALUE-SPECIFICATION[CATEGORY=VALUE,SW-VALUE-CONT["
            "UNIT-REF@UNIT=/DataTypes/Application/Sample_T_Unit,SW-VALUES-PHYS[V=2.5]]]"
        )

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_with_a_unit_by_reference_names_the_declared_unit(self, version: str, tmp_path: Path) -> None:
        root = _generated(tmp_path, version)
        assert _shape(_constant_value(root, "own_ref")) == (
            "APPLICATION-VALUE-SPECIFICATION[CATEGORY=VALUE,SW-VALUE-CONT["
            "UNIT-REF@UNIT=/DataTypes/Units/Volt,SW-VALUES-PHYS[V=-7]]]"
        )

    def test_the_unit_is_the_one_the_types_own_properties_name(self, tmp_path: Path) -> None:
        """The value's ``UNIT-REF`` and the type's are one element referenced twice."""
        root = _generated(tmp_path)
        for element, type_name in (("own_unit", "Sample_T"), ("own_ref", "Ref_T")):
            (application_type,) = [
                node
                for node in root.iter(_ns("APPLICATION-PRIMITIVE-DATA-TYPE"))
                if node.findtext(_ns("SHORT-NAME")) == type_name
            ]
            type_unit = next(application_type.iter(_ns("UNIT-REF"))).text
            assert _constant_value(root, element).find(f"{_ns('SW-VALUE-CONT')}/{_ns('UNIT-REF')}").text == type_unit

    @pytest.mark.parametrize(
        ("element", "text"),
        [
            ("own_zero", "0"),
            ("own_fraction", "0.125"),
            ("own_large", "100000000000000000000.0"),
            ("own_large_int", "100000000000000000000"),
            ("own_small", "0.0000001"),
            ("own_bool", "1"),
        ],
    )
    def test_v_is_the_text_format_init_number_gives(self, element: str, text: str, tmp_path: Path) -> None:
        """The init-value formatter, unchanged: decimal, fixed point, a ``bool`` ``1``/``0``."""
        value = _constant_value(_generated(tmp_path), element)
        assert value.findtext(f"{_ns('SW-VALUE-CONT')}/{_ns('SW-VALUES-PHYS')}/{_ns('V')}") == text

    def test_no_value_states_a_sw_arraysize_or_a_label(self, tmp_path: Path) -> None:
        root = _generated(tmp_path)
        assert not list(root.iter(_ns("SW-ARRAYSIZE")))
        for element in OWN_VALUES:
            assert _constant_value(root, element).find(_ns("SHORT-LABEL")) is None

    def test_a_ports_own_value_in_place_takes_the_same_form(self, tmp_path: Path) -> None:
        root = _generated(tmp_path)
        (spec,) = [
            node
            for node in root.iter(_ns("NONQUEUED-SENDER-COM-SPEC"))
            if node.findtext(_ns("DATA-ELEMENT-REF")).endswith("/own_unit")
        ]
        (value,) = list(spec.find(_ns("INIT-VALUE")))
        assert _shape(value) == (
            "APPLICATION-VALUE-SPECIFICATION[CATEGORY=VALUE,SW-VALUE-CONT["
            "UNIT-REF@UNIT=/DataTypes/Application/Sample_T_Unit,SW-VALUES-PHYS[V=7.5]]]"
        )


class TestAMembersNumberIsNotTouched:
    """A member's number keeps the numerical form, no unit: SWCraft's choice where the standard prescribes none."""

    def test_an_application_typed_record_member_is_numerical(self, tmp_path: Path) -> None:
        value = _constant_value(_generated(tmp_path), "limits")
        assert _shape(value) == (
            "RECORD-VALUE-SPECIFICATION[FIELDS["
            "NUMERICAL-VALUE-SPECIFICATION[SHORT-LABEL=low,VALUE=-100000000000000000000.0],"
            "NUMERICAL-VALUE-SPECIFICATION[SHORT-LABEL=high,VALUE=100000000000000000000.0],"
            "NUMERICAL-VALUE-SPECIFICATION[SHORT-LABEL=tiny,VALUE=0.0000001],"
            "NUMERICAL-VALUE-SPECIFICATION[SHORT-LABEL=whole,VALUE=100.0]]]"
        )

    def test_an_application_typed_array_member_is_numerical(self, tmp_path: Path) -> None:
        assert _shape(_constant_value(_generated(tmp_path), "bytes")) == (
            "ARRAY-VALUE-SPECIFICATION[ELEMENTS[NUMERICAL-VALUE-SPECIFICATION[VALUE=1],"
            "NUMERICAL-VALUE-SPECIFICATION[VALUE=2]]]"
        )

    def test_three_levels_of_nesting_keep_every_number_numerical(self, tmp_path: Path) -> None:
        """The two numbers stay numerical; the two enumeration members are their literals' text."""
        value = _constant_value(_generated(tmp_path), "deep")
        assert len(list(value.iter(_ns("NUMERICAL-VALUE-SPECIFICATION")))) == 2
        assert [
            node.findtext(f"{_ns('SW-VALUE-CONT')}/{_ns('SW-VALUES-PHYS')}/{_ns('VT')}")
            for node in value.iter(_ns("APPLICATION-VALUE-SPECIFICATION"))
        ] == ["OFF", "ON"]

    def test_an_implementation_typed_value_is_numerical(self, tmp_path: Path) -> None:
        """``[constr_1385]``: never the application form under an implementation type."""
        assert _shape(_constant_value(_generated(tmp_path), "ratio")) == "NUMERICAL-VALUE-SPECIFICATION[VALUE=12.5]"


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_swcrafts_own_application_values_come_back_with_no_line(version: str, tmp_path: Path) -> None:
    """Every value of the extended tree round-trips through writer, reader, judgement and mapper with no line."""
    model_root = application_value_model_root(tmp_path)
    output = tmp_path / f"own-{version}.arxml"
    generate_arxml(str(model_root), str(output), version=version)
    project = _init_project(tmp_path / "imported")
    report = run_import(str(output), str(project / "swcraft.yaml"))
    written = yaml.safe_load((model_root / "interfaces" / "ISignals.yaml").read_text(encoding="utf-8"))
    imported = yaml.safe_load((project / "model" / "interfaces" / "ISignals.yaml").read_text(encoding="utf-8"))
    expected = {element["name"]: element.get("init") for element in written["data_elements"]}
    expected["own_bool"] = 1  # a `bool` is written `1` and read as the number it is
    assert {element["name"]: element.get("init") for element in imported["data_elements"]} == expected
    assert [entry for entry in report.skip_entries if entry.tag in ("INIT-VALUE", "VALUE-SPEC")] == []
    assert not [
        entry for entry in report.skip_entries if entry.reason in (TWIN_NUMERICAL_SENTENCE, VALUE_UNIT_SENTENCE)
    ]
    assert check_cross_layer_consistency(str(project / "model")).consistent


# The importer, on inline models


def _model() -> ImportModel:
    """An inline export: a plain value type, application primitives with and without a unit, and a record."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(path="/P/T/u8", name="u8", size=8, encoding="NONE", native_declaration="uint8", description="")
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/P/T/Plain",
            name="Plain",
            category="VALUE",
            base_type_ref="/P/T/u8",
            impl_type_ref=None,
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(path="/P/A/Count", name="Count", compu_method_ref=None, description="")
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(
            path="/P/A/Speed", name="Speed", compu_method_ref=None, description="", unit_ref="/P/U/Kmh"
        )
    )
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/P/A/Pair",
            name="Pair",
            elements=[
                RecordElementInfo(name="count", type_ref="/P/A/Count"),
                RecordElementInfo(name="speed", type_ref="/P/A/Speed"),
            ],
            description="",
        )
    )
    return model


def _import(model: ImportModel):
    """Map *model*: the one element's init, its INIT-VALUE lines, and the lines at its constant's value."""
    init, lines, skips = _mapped(model)
    value_lines = [entry for entry in skips if entry.path == _CONSTANT and entry.tag == "VALUE-SPEC"]
    return init, lines, value_lines


def _application(value, unit: str | None = None) -> ConstantScalar:
    return ConstantScalar(value=value, form=ValueForm.APPLICATION, unit_ref=unit)


def _numerical(value) -> ConstantScalar:
    return ConstantScalar(value=value, form=ValueForm.NUMERICAL)


class TestAPrototypesOwnValue:
    """An application primitive that is not an enumeration, and an implementation type with no table."""

    def test_an_application_value_under_an_application_primitive_is_carried(self):
        init, lines, value_lines = _import(_element(_model(), "/P/A/Speed", _application(12, "/P/U/Kmh")))
        assert (init, lines, value_lines) == (12, [], [])

    def test_the_numerical_form_under_an_application_primitive_is_constr_1221(self):
        init, lines, _ = _import(_element(_model(), "/P/A/Count", _numerical(12)))
        assert init is None
        (line,) = lines
        assert (line.category, line.rule, line.effect) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
            "abort",
        )
        assert NUMBER_WHERE_AN_APPLICATION_VALUE_IS_TAKEN.words in line.reason

    def test_a_text_in_the_application_form_under_an_application_primitive_is_section_2(self):
        init, lines, _ = _import(_element(_model(), "/P/A/Count", _application("FULL")))
        assert init is None
        (line,) = lines
        assert (line.category, line.rule) == (SkipCategory.NOT_SUPPORTED, None)
        assert APPLICATION_TEXT_WHERE_A_NUMBER_IS_TAKEN.words in line.reason

    def test_the_numerical_form_under_a_plain_implementation_type_is_carried(self):
        assert _import(_element(_model(), "/P/T/Plain", _numerical(3)))[:2] == (3, [])

    @pytest.mark.parametrize("value", [3, "FULL"])
    def test_the_application_form_under_a_plain_implementation_type_is_constr_1385(self, value):
        init, lines, _ = _import(_element(_model(), "/P/T/Plain", _application(value)))
        assert init is None
        (line,) = lines
        assert (line.category, line.rule) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.NO_APPLICATION_VALUE_FOR_AN_IMPLEMENTATION_TYPE,
        )
        assert APPLICATION_VALUE_UNDER_AN_IMPLEMENTATION_TYPE.words in line.reason


class TestAMember:
    """No rule prescribes a member's form."""

    @pytest.mark.parametrize("form", [ValueForm.NUMERICAL, ValueForm.APPLICATION])
    def test_a_members_number_is_carried_in_either_form(self, form):
        value = ConstantRecord(
            fields=[
                ConstantRecordField(label=None, value=ConstantScalar(value=1, form=form)),
                ConstantRecordField(label=None, value=ConstantScalar(value=2, form=form)),
            ]
        )
        init, lines, _ = _import(_element(_model(), "/P/A/Pair", value))
        assert (init, lines) == ({"count": 1, "speed": 2}, [])

    def test_a_members_text_in_the_application_form_is_section_2_and_takes_the_whole_init(self):
        value = ConstantRecord(
            fields=[
                ConstantRecordField(label=None, value=_application("FULL")),
                ConstantRecordField(label=None, value=_numerical(2)),
            ]
        )
        init, lines, _ = _import(_element(_model(), "/P/A/Pair", value))
        assert init is None
        (line,) = lines
        assert line.reason == (
            "the init value is not carried: a text in the application form under a type that takes a number (1 leaf)"
        )
        assert line.category == SkipCategory.NOT_SUPPORTED

    def test_a_top_level_number_is_one_cause_at_one_leaf(self):
        """A prototype's own number under an application primitive: one cause, one leaf, ``[constr_1221]``'s."""
        judged = judge_init_value(_numerical(1), "/P/A/Count", SourceTypes(_model()))
        assert judged == ((NUMBER_WHERE_AN_APPLICATION_VALUE_IS_TAKEN, 1),)

    def test_an_array_of_application_numbers_is_carried(self):
        model = _model()
        from swcraft_tools.arxml_reader import AppArrayDataTypeInfo

        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/P/A/Counts",
                name="Counts",
                element_type_ref="/P/A/Count",
                max_number_of_elements=2,
                description="",
            )
        )
        value = ConstantArray(members=[_application(1), _numerical(2)])
        assert _import(_element(model, "/P/A/Counts", value))[:2] == ([1, 2], [])


class TestTheUnitLine:
    """The unit line, in its three shapes; a member in the numerical form states no unit and gets none."""

    @pytest.mark.parametrize(
        ("type_ref", "unit"),
        [
            ("/P/A/Count", "/P/U/Kmh"),  # a unit its type does not state
            ("/P/A/Speed", None),  # none where its type states one
            ("/P/A/Speed", "/P/U/Mph"),  # another unit
        ],
        ids=["unit-the-type-does-not-state", "none-where-the-type-states-one", "another-unit"],
    )
    def test_a_unit_that_is_not_the_types_is_one_descriptive_line_at_the_constant(self, type_ref, unit):
        init, lines, value_lines = _import(_element(_model(), type_ref, _application(5, unit)))
        assert (init, lines) == (5, [])
        (line,) = value_lines
        assert (line.reason, line.category, line.rule, line.effect) == (
            VALUE_UNIT_SENTENCE,
            SkipCategory.NOT_SUPPORTED,
            None,
            "report",
        )

    def test_a_members_unit_is_judged_too(self):
        value = ConstantRecord(
            fields=[
                ConstantRecordField(label=None, value=_numerical(1)),
                ConstantRecordField(label=None, value=_application(2, "/P/U/Mph")),
            ]
        )
        assert init_value_unit_differs(value, "/P/A/Pair", SourceTypes(_model()))

    @staticmethod
    def _enumeration(model: ImportModel, unit: str | None) -> ImportModel:
        """``/P/A/Gear``, an enumeration (a TEXTTABLE application primitive) stating *unit*."""
        model.compu_methods.append(
            CompuMethodInfo(
                path="/P/C/Gear_CM",
                name="Gear_CM",
                category="TEXTTABLE",
                scales=[CompuScaleInfo(value=0, literal="P"), CompuScaleInfo(value=1, literal="D")],
                description="",
            )
        )
        model.app_primitive_data_types.append(
            AppPrimitiveDataTypeInfo(
                path="/P/A/Gear", name="Gear", compu_method_ref="/P/C/Gear_CM", description="", unit_ref=unit
            )
        )
        return model

    @pytest.mark.parametrize("type_unit", [None, "/P/U/Kmh"], ids=["type-states-none", "type-states-it"])
    def test_an_enumeration_values_unit_is_never_carried(self, type_unit):
        """A unit an enumeration value states is not carried, even its type's: the writer writes none there."""
        model = self._enumeration(_model(), type_unit)
        value = _application("D", "/P/U/Kmh")
        assert init_value_unit_differs(value, "/P/A/Gear", SourceTypes(model))
        # Each line a record with its category, the descriptive bytes kept.
        assert carried_value_lines(value, "/P/A/Gear", SourceTypes(model), application=True) == (
            (VALUE_UNIT_SENTENCE, SkipCategory.NOT_SUPPORTED, None),
        )

    def test_an_enumeration_value_with_no_unit_loses_none(self):
        """Its type states one, the value none: the writer writes none either way, so nothing is lost."""
        model = self._enumeration(_model(), "/P/U/Kmh")
        assert not init_value_unit_differs(_application("D"), "/P/A/Gear", SourceTypes(model))

    def test_a_member_in_the_numerical_form_states_none_and_gets_none(self):
        value = ConstantRecord(
            fields=[
                ConstantRecordField(label=None, value=_numerical(1)),
                ConstantRecordField(label=None, value=_numerical(2)),
            ]
        )
        assert not init_value_unit_differs(value, "/P/A/Pair", SourceTypes(_model()))
        assert _import(_element(_model(), "/P/A/Pair", value))[2] == []


def _twin_model(value) -> ImportModel:
    """An application primitive with no compu method and its implementation twin, which collapses onto it."""
    model = _model()
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/P/Impl/Level",
            name="Level",
            category="VALUE",
            base_type_ref="/P/T/u8",
            impl_type_ref=None,
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(path="/P/A/Level", name="Level", compu_method_ref=None, description="")
    )
    model.data_type_mapping_sets.append(
        DataTypeMappingSetInfo(
            path="/P/M/Maps",
            name="Maps",
            maps=[DataTypeMapInfo(application_ref="/P/A/Level", implementation_ref="/P/Impl/Level")],
        )
    )
    return _element(model, "/P/A/Level", value)


class TestACollapsedTwinWithNoTable:
    """The value conforms by the source's type and is carried in the model type's numerical form, with one line."""

    def test_the_value_is_carried_with_the_twin_line(self):
        init, lines, value_lines = _import(_twin_model(_application(3)))
        assert (init, lines) == (3, [])
        (line,) = value_lines
        assert (line.reason, line.category, line.rule, line.effect) == (
            TWIN_NUMERICAL_SENTENCE,
            SkipCategory.NOT_SUPPORTED,
            None,
            "report",
        )

    def test_the_source_type_judges_a_numerical_twin_value(self):
        """Under a source application primitive the numerical form is ``[constr_1221]``, whatever the model type."""
        init, lines, value_lines = _import(_twin_model(_numerical(3)))
        assert init is None
        assert lines[0].rule == StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE
        assert value_lines == []

    def test_the_lines_are_one_per_value(self):
        source = SourceTypes(_twin_model(_application(3, "/P/U/Kmh")))
        assert carried_value_lines(_application(3, "/P/U/Kmh"), "/P/A/Level", source, application=False) == (
            (TWIN_NUMERICAL_SENTENCE, SkipCategory.NOT_SUPPORTED, None),
            (VALUE_UNIT_SENTENCE, SkipCategory.NOT_SUPPORTED, None),
        )
        assert carried_value_lines(_application(3), "/P/A/Level", source, application=True) == ()


# A port's own value: its reason whole, and its value lines

_PORT_FILE = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>P</SHORT-NAME>
      <ELEMENTS>
        <SW-BASE-TYPE>
          <SHORT-NAME>u8</SHORT-NAME>
          <CATEGORY>FIXED_LENGTH</CATEGORY>
          <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
          <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
          <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
        </SW-BASE-TYPE>
        <APPLICATION-PRIMITIVE-DATA-TYPE>
          <SHORT-NAME>Count</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
        </APPLICATION-PRIMITIVE-DATA-TYPE>
        <CONSTANT-SPECIFICATION>
          <SHORT-NAME>I_e_Init</SHORT-NAME>
          <VALUE-SPEC>
            <APPLICATION-VALUE-SPECIFICATION>
              <CATEGORY>VALUE</CATEGORY>
              <SW-VALUE-CONT><SW-VALUES-PHYS><V>1</V></SW-VALUES-PHYS></SW-VALUE-CONT>
            </APPLICATION-VALUE-SPECIFICATION>
          </VALUE-SPEC>
        </CONSTANT-SPECIFICATION>
        <SENDER-RECEIVER-INTERFACE>
          <SHORT-NAME>I</SHORT-NAME>
          <DATA-ELEMENTS>
            <VARIABLE-DATA-PROTOTYPE>
              <SHORT-NAME>e</SHORT-NAME>
              <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/P/Count</TYPE-TREF>
              <INIT-VALUE>
                <CONSTANT-REFERENCE>
                  <CONSTANT-REF DEST="CONSTANT-SPECIFICATION">/P/I_e_Init</CONSTANT-REF>
                </CONSTANT-REFERENCE>
              </INIT-VALUE>
            </VARIABLE-DATA-PROTOTYPE>
          </DATA-ELEMENTS>
        </SENDER-RECEIVER-INTERFACE>
        <APPLICATION-SW-COMPONENT-TYPE>
          <SHORT-NAME>C</SHORT-NAME>
          <PORTS>
            <P-PORT-PROTOTYPE>
              <SHORT-NAME>p</SHORT-NAME>
              <PROVIDED-COM-SPECS>
                <NONQUEUED-SENDER-COM-SPEC>
                  <DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/P/I/e</DATA-ELEMENT-REF>
                  <INIT-VALUE>
                    {port_value}
                  </INIT-VALUE>
                </NONQUEUED-SENDER-COM-SPEC>
              </PROVIDED-COM-SPECS>
              <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/P/I</PROVIDED-INTERFACE-TREF>
            </P-PORT-PROTOTYPE>
          </PORTS>
        </APPLICATION-SW-COMPONENT-TYPE>
      </ELEMENTS>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


def _port_import(tmp_path: Path, port_value: str):
    source = tmp_path / "port.arxml"
    source.write_text(_PORT_FILE.replace("{port_value}", port_value), encoding="utf-8", newline="\n")
    project = _init_project(tmp_path / "project")
    report = run_import(str(source), str(project / "swcraft.yaml"))
    return [entry for entry in report.skip_entries if entry.path == "/P/C/p/PROVIDED-COM-SPECS"]


class TestAPortsOwnValue:
    """A port's own value is judged as an element's is; its line carries its own category and rule."""

    def test_a_port_value_in_the_numerical_form_is_section_1_with_its_rule(self, tmp_path: Path) -> None:
        (line,) = _port_import(
            tmp_path, "<NUMERICAL-VALUE-SPECIFICATION><VALUE>2</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        assert (line.tag, line.category, line.rule, line.effect) == (
            "INIT-VALUE",
            SkipCategory.EXPORT_DEFECT,
            StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
            "report",
        )
        assert NUMBER_WHERE_AN_APPLICATION_VALUE_IS_TAKEN.words in line.reason

    def test_a_port_value_whose_unit_is_not_its_types_is_carried_with_the_unit_line(self, tmp_path: Path) -> None:
        lines = _port_import(
            tmp_path,
            "<APPLICATION-VALUE-SPECIFICATION><CATEGORY>VALUE</CATEGORY><SW-VALUE-CONT>"
            '<UNIT-REF DEST="UNIT">/P/Elsewhere</UNIT-REF><SW-VALUES-PHYS><V>2</V></SW-VALUES-PHYS>'
            "</SW-VALUE-CONT></APPLICATION-VALUE-SPECIFICATION>",
        )
        assert [(entry.tag, entry.reason, entry.category, entry.effect) for entry in lines] == [
            ("INIT-VALUE", VALUE_UNIT_SENTENCE, SkipCategory.NOT_SUPPORTED, "report")
        ]

    def test_a_conforming_port_value_has_no_line(self, tmp_path: Path) -> None:
        assert (
            _port_import(
                tmp_path,
                "<APPLICATION-VALUE-SPECIFICATION><CATEGORY>VALUE</CATEGORY><SW-VALUE-CONT>"
                "<SW-VALUES-PHYS><V>2</V></SW-VALUES-PHYS></SW-VALUE-CONT></APPLICATION-VALUE-SPECIFICATION>",
            )
            == []
        )
