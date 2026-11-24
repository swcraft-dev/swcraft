"""The enumeration's application value form: its literal's text, and no number read as a literal outside its table.

The writer: an enumeration leaf, as a prototype's own value and as a member, is an
``APPLICATION-VALUE-SPECIFICATION`` of ``CATEGORY`` ``VALUE`` whose ``SW-VALUES-PHYS``
holds one ``VT``, the literal's name, and no ``UNIT-REF``.

The importer: an enumeration's value conforms in the application form with one text
naming one of its scales; a number its table does not read as one literal is
``[constr_1221]`` as the prototype's own value and SWCraft's own line as a member, and
the whole init is not carried.

The writer cases extend a ``tmp_path`` copy of ``fixtures/init_values`` (the recorded tree
is not extended); the importer cases are inline models, as ``test_init_value_judgement.py``
builds them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from project_support import init_project as _init_project
from swcraft_tools.arxml_import import map_data_types, run_import
from swcraft_tools.arxml_import.type_mapping import (
    ENUMERATION_MEMBER_GIVEN_AS_A_NUMBER,
    ENUMERATION_MEMBER_NAMING_NO_SCALE,
    NOT_AN_ENUMERATION_TEXT,
    TEXT_NAMING_A_SHORT_LABEL,
    TEXT_UNDER_A_SYMBOL_TABLE,
    TEXT_VALUE_UNDER_AN_APPLICATION_TYPE,
    TEXT_VALUE_WITHOUT_A_TABLE,
    _map_init_value,
)
from swcraft_tools.arxml_reader import (
    AppArrayDataTypeInfo,
    AppRecordDataTypeInfo,
    CompuMethodInfo,
    ConstantArray,
    ConstantRecord,
    ConstantRecordField,
    ConstantScalar,
    ImportModel,
    RecordElementInfo,
    SkipCategory,
    StandardRule,
)
from swcraft_tools.data_description import DataDescription
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml
from test_application_value_form import (
    _application,
    _constant_value,
    _ns,
    _numerical,
    _shape,
    application_value_model_root,
)
from test_init_value_judgement import _element, _import, _judge, _model, _scales, _text

# The writer


def _enumeration_model_root(tmp_path: Path) -> Path:
    """The extended init values tree, plus a port's own value in place under the enumeration ``Mode_T``."""
    model_root = application_value_model_root(tmp_path)
    component = model_root / "swc" / "signals" / "SignalsSwc.yaml"
    document = yaml.safe_load(component.read_text(encoding="utf-8"))
    ports = {port["name"]: port for port in document["ports"]}
    ports["pSignals"]["com_specs"].append({"data_element": "mode", "init": "OFF"})
    component.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return model_root


_VT = f"{_ns('SW-VALUE-CONT')}/{_ns('SW-VALUES-PHYS')}/{_ns('VT')}"


def _generated(tmp_path: Path, version: str) -> object:
    from lxml import etree

    output = tmp_path / f"enumerations-{version}.arxml"
    generate_arxml(str(_enumeration_model_root(tmp_path)), str(output), version=version)
    return etree.parse(str(output)).getroot()


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
class TestAnEnumerationLeafIsItsLiteralsText:
    def test_as_a_prototypes_own_value(self, version: str, tmp_path: Path) -> None:
        assert _shape(_constant_value(_generated(tmp_path, version), "mode")) == (
            "APPLICATION-VALUE-SPECIFICATION[CATEGORY=VALUE,SW-VALUE-CONT[SW-VALUES-PHYS[VT=ON]]]"
        )

    def test_as_a_member_at_depth_three_with_its_label(self, version: str, tmp_path: Path) -> None:
        value = _constant_value(_generated(tmp_path, version), "deep")
        leaves = [
            _shape(node)
            for node in value.iter(_ns("APPLICATION-VALUE-SPECIFICATION"))  # type: ignore[attr-defined]
        ]
        assert leaves == [
            "APPLICATION-VALUE-SPECIFICATION[SHORT-LABEL=mode,CATEGORY=VALUE,SW-VALUE-CONT[SW-VALUES-PHYS[VT=OFF]]]",
            "APPLICATION-VALUE-SPECIFICATION[SHORT-LABEL=mode,CATEGORY=VALUE,SW-VALUE-CONT[SW-VALUES-PHYS[VT=ON]]]",
        ]
        # The other members keep the numerical form.
        assert len(list(value.iter(_ns("NUMERICAL-VALUE-SPECIFICATION")))) == 2  # type: ignore[attr-defined]

    def test_a_ports_own_value_in_place_takes_the_same_form_and_no_unit(self, version: str, tmp_path: Path) -> None:
        root = _generated(tmp_path, version)
        values = [
            node
            for node in root.iter(_ns("APPLICATION-VALUE-SPECIFICATION"))  # type: ignore[attr-defined]
            if node.getparent().tag == _ns("INIT-VALUE") and node.findtext(_VT) == "OFF"
        ]
        assert [_shape(node) for node in values] == [
            "APPLICATION-VALUE-SPECIFICATION[CATEGORY=VALUE,SW-VALUE-CONT[SW-VALUES-PHYS[VT=OFF]]]"
        ]

    def test_the_text_names_exactly_one_scale_of_the_types_table(self, version: str, tmp_path: Path) -> None:
        """The written text is the scale's own ``VT``, so it names exactly one."""
        root = _generated(tmp_path, version)
        written = {
            node.text
            for node in root.iter(_ns("VT"))  # type: ignore[attr-defined]
            if node.getparent().tag == _ns("SW-VALUES-PHYS")
        }
        # The type's own table: the compu method `Mode_T`'s COMPU-METHOD-REF names, and its scales alone.
        (mode_type,) = [
            node
            for node in root.iter(_ns("APPLICATION-PRIMITIVE-DATA-TYPE"))  # type: ignore[attr-defined]
            if node.findtext(_ns("SHORT-NAME")) == "Mode_T"
        ]
        (reference,) = list(mode_type.iter(_ns("COMPU-METHOD-REF")))
        (method,) = [
            node
            for node in root.iter(_ns("COMPU-METHOD"))  # type: ignore[attr-defined]
            if node.findtext(_ns("SHORT-NAME")) == reference.text.rsplit("/", 1)[-1]
        ]
        scales = [node.text for node in method.iter(_ns("VT"))]
        assert written == {"ON", "OFF"}
        assert all(scales.count(text) == 1 for text in written), scales


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_swcrafts_own_enumeration_values_come_back_with_no_line(version: str, tmp_path: Path) -> None:
    """An enumeration's own value, its members and a port's own value in place round-trip carried, with no init line."""
    model_root = _enumeration_model_root(tmp_path)
    output = tmp_path / f"own-{version}.arxml"
    generate_arxml(str(model_root), str(output), version=version)
    project = _init_project(tmp_path / "imported")
    report = run_import(str(output), str(project / "swcraft.yaml"))
    imported = yaml.safe_load((project / "model" / "interfaces" / "ISignals.yaml").read_text(encoding="utf-8"))
    by_name = {element["name"]: element.get("init") for element in imported["data_elements"]}
    assert by_name["mode"] == "ON"
    assert by_name["deep"] == [{"row": {"index": 1, "mode": "OFF"}}, {"row": {"index": 2, "mode": "ON"}}]
    component = next((project / "model" / "swc").rglob("SignalsSwc.yaml"))
    ports = {port["name"]: port for port in yaml.safe_load(component.read_text(encoding="utf-8"))["ports"]}
    assert {"data_element": "mode", "init": "OFF"} in [
        {key: spec[key] for key in ("data_element", "init") if key in spec} for spec in ports["pSignals"]["com_specs"]
    ]
    assert [entry for entry in report.skip_entries if entry.tag in ("INIT-VALUE", "VALUE-SPEC")] == []
    assert check_cross_layer_consistency(str(project / "model")).consistent


# The importer: the enumeration's rows


def _with_gear_scales(model: ImportModel, **scales) -> ImportModel:
    """``/P/A/Gear``'s table restated with *scales* (``_scales``' keywords)."""
    model.compu_methods = [
        cm
        if cm.path != "/P/C/Gear_CM"
        else CompuMethodInfo(
            path=cm.path, name=cm.name, category="TEXTTABLE", scales=_scales("P", "D", **scales), description=""
        )
        for cm in model.compu_methods
    ]
    return model


def _gear_record(model: ImportModel) -> ImportModel:
    """``/P/A/GearPair``, an application record of an enumeration member and a number."""
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/P/A/GearPair",
            name="GearPair",
            elements=[
                RecordElementInfo(name="gear", type_ref="/P/A/Gear"),
                RecordElementInfo(name="count", type_ref="/P/A/Count"),
            ],
            description="",
        )
    )
    return model


def _pair(gear: ConstantScalar, count: ConstantScalar | None = None) -> ConstantRecord:
    return ConstantRecord(
        fields=[
            ConstantRecordField(label=None, value=gear),
            ConstantRecordField(label=None, value=count if count is not None else _numerical(2)),
        ]
    )


class TestAnEnumerationsOwnValue:
    """The enumeration's own value."""

    def test_the_application_form_with_a_text_naming_one_scale_is_carried(self):
        assert _import(_element(_model(), "/P/A/Gear", _application("D"))) == ("D", [])

    @pytest.mark.parametrize(
        "value",
        # A literal's value, in either form, is read through the table and carried
        # (`test_enum_number_init_import.py`); a number no scale names is not.
        [_numerical(7), _application(7), _application("R")],
        ids=["a-number-of-no-literal", "an-application-number-of-no-literal", "a-text-of-no-scale"],
    )
    def test_any_other_value_is_constr_1221_section_1_and_not_carried(self, value):
        init, lines = _import(_element(_model(), "/P/A/Gear", value))
        assert init is None
        (line,) = lines
        assert (line.category, line.rule, line.effect) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE,
            "abort",
        )
        assert line.reason == f"the init value is not carried: {NOT_AN_ENUMERATION_TEXT.words} (1 leaf)"

    def test_a_text_value_is_constr_1221_as_a_text_value(self):
        assert _judge(_model(), "/P/A/Gear", _text("D")) == ((TEXT_VALUE_UNDER_AN_APPLICATION_TYPE, 1),)

    def test_a_text_equal_to_a_scales_short_label_alone_is_section_2(self):
        model = _with_gear_scales(_model(), labels=("Park", "Drive"))
        assert _judge(model, "/P/A/Gear", _application("Drive")) == ((TEXT_NAMING_A_SHORT_LABEL, 1),)

    def test_a_text_under_a_table_whose_scales_state_a_symbol_is_section_2(self):
        model = _with_gear_scales(_model(), symbol=True)
        assert _judge(model, "/P/A/Gear", _application("D")) == ((TEXT_UNDER_A_SYMBOL_TABLE, 1),)


class TestAnEnumerationMember:
    """An enumeration member given as a number its table does not read as one literal is SWCraft's own line."""

    @pytest.mark.parametrize("gear", [_numerical(7), _application(7)], ids=["numerical", "application"])
    def test_a_member_given_as_a_number_is_section_2_and_takes_the_whole_init(self, gear):
        init, lines = _import(_element(_gear_record(_model()), "/P/A/GearPair", _pair(gear)))
        assert init is None
        (line,) = lines
        assert (line.reason, line.category, line.rule, line.effect) == (
            "the init value is not carried: an enumeration member given as a number, which no one single-point "
            "scale of a whole table without a SYMBOL names (1 leaf)",
            SkipCategory.NOT_SUPPORTED,
            None,
            "abort",
        )

    def test_a_member_in_the_application_form_naming_one_scale_is_carried(self):
        init, lines = _import(_element(_gear_record(_model()), "/P/A/GearPair", _pair(_application("P"))))
        assert (init, lines) == ({"gear": "P", "count": 2}, [])

    def test_a_member_whose_text_names_no_scale_is_section_2(self):
        judged = _judge(_gear_record(_model()), "/P/A/GearPair", _pair(_application("R")))
        assert judged == ((ENUMERATION_MEMBER_NAMING_NO_SCALE, 1),)

    def test_a_member_as_a_text_value_is_constr_1284(self):
        judged = _judge(_gear_record(_model()), "/P/A/GearPair", _pair(_text("P")))
        assert judged == ((TEXT_VALUE_WITHOUT_A_TABLE, 1),)

    def test_an_array_of_enumeration_numbers_counts_every_leaf(self):
        model = _model()
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/P/A/Gears", name="Gears", element_type_ref="/P/A/Gear", max_number_of_elements=3, description=""
            )
        )
        judged = _judge(model, "/P/A/Gears", ConstantArray(members=[_numerical(5), _application("D"), _numerical(6)]))
        assert judged == ((ENUMERATION_MEMBER_GIVEN_AS_A_NUMBER, 2),)

    def test_a_rules_cause_beside_a_member_number_makes_one_section_1_line(self):
        init, lines = _import(_element(_gear_record(_model()), "/P/A/GearPair", _pair(_numerical(7), _text("FULL"))))
        assert init is None
        (line,) = lines
        assert (line.category, line.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.TEXT_VALUE_UNDER_A_TABLE_ONLY)
        assert line.reason == (
            f"the init value is not carried: {TEXT_VALUE_WITHOUT_A_TABLE.words} (1 leaf); "
            f"{ENUMERATION_MEMBER_GIVEN_AS_A_NUMBER.words} (1 leaf)"
        )


def test_the_mapper_reads_no_number_as_a_literal():
    """Where the walk names nothing, the mapper still reads no number as a literal: SWCraft's own line.

    Only the walk reads a number through the file's table."""
    model = _model()
    forms: list = []
    data_types, _registry, _skips = map_data_types(model, elements=forms)
    description = DataDescription.last_declared([*data_types, *forms])
    mapped, reason = _map_init_value(_numerical(1), "Gear", description, "init", application=True)
    assert mapped is None and reason is not None
    assert (reason.category, reason.rule) == (SkipCategory.NOT_SUPPORTED, None)
    assert reason.text == (
        "init: enum type 'Gear' takes the name of one of its literals, and SWCraft reads the number 1 as a literal "
        "only through the file's own table"
    )
    assert _map_init_value(_application("D"), "Gear", description, "init", application=True) == ("D", None)
