"""Enumeration init values given as numbers, read through the table.

A numeric leaf (``NUMERICAL-VALUE-SPECIFICATION/VALUE`` or ``SW-VALUES-PHYS/V``) under an
enumeration, or under an implementation type referencing a ``TEXTTABLE``, is read through
the table's internal-to-physical direction: an integral number equal to the single-point
value of exactly one scale becomes that scale's literal. The value is carried and the
report says what the source did: a prototype's own value takes a defect line
(``[constr_1221]`` or ``[constr_1225]``) opening *init value carried*, a member a
descriptive line.

Not carried, each with its line: a number no scale or two scales name, a non-integral
number, one in a range scale, under a ``BITFIELD_TEXTTABLE``, under a table with a
``SYMBOL`` scale or a dropped scale, and under a collapsed twin whose implementation type
references a table.

Two halves: the rows on inline :class:`ImportModel`s (``test_init_value_judgement.py``'s
helpers, reused), and a synthetic export of SWCraft's own build with each value edited
into a number, imported by ``import model`` and ``import component``, written and re-imported.

**What this does not reach:** a scale whose ``UPPER-LIMIT`` the reader cannot parse (the
reading abstains); other spellings of a number (``0x``, octal) beyond the one hexadecimal
case; a member of an inline implementation sub-element, which the reader types by no table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import write_model_element
from lxml import etree
from project_support import init_project as _init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.type_mapping import (
    ENUMERATION_MEMBER_GIVEN_AS_A_NUMBER,
    MEMBER_NUMBER_UNDER_A_TABLE,
    NOT_A_TEXT_VALUE_UNDER_A_TABLE,
    NOT_AN_ENUMERATION_TEXT,
    TWIN_NUMERICAL_SENTENCE,
    TWIN_TABLE_SENTENCE,
    VALUE_UNIT_SENTENCE,
    SourceTypes,
    carried_value_lines,
    init_line_reason,
)
from swcraft_tools.arxml_reader import (
    CompuScaleInfo,
    ConstantScalar,
    ImportModel,
    RecordElementInfo,
    SkipCategory,
    StandardRule,
    ValueForm,
)
from swcraft_tools.component_exchange import LossyComponentImportError, export_component, import_component
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_generation import init_constant_name
from test_component_import_init_values import _value_in_place, _value_of_constant, _valued_component_root
from test_init_value_judgement import (
    _CONSTANT,
    _dropped,
    _element,
    _mapped,
    _members,
    _model,
    _port_outcome,
    _record,
    _twin_model,
)

#: The sentence of one reading, byte for byte (``NUMBER_READ_AS_LITERAL_SENTENCE``).
READ = "given as the number {number}, carried as the literal {literal} its type's table gives that number"
#: The opening of a prototype's own value read through its table (``INIT_CARRIED``).
CARRIED = "init value carried: "

_ED = SkipCategory.EXPORT_DEFECT
_NS = SkipCategory.NOT_SUPPORTED
_C1221 = StandardRule.APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE
_C1225 = StandardRule.TABLE_INIT_IS_A_TEXT_VALUE

FORMS = [ValueForm.NUMERICAL, ValueForm.APPLICATION]


def _number(value: int | float, form: ValueForm, *, radix: str | None = None) -> ConstantScalar:
    return ConstantScalar(value=value, form=form, radix_source=radix)


def _own(literal: str, number: str, rule: StandardRule) -> tuple:
    return (CARRIED + READ.format(number=number, literal=literal), _ED, rule, "report")


def _member(literal: str, number: str) -> tuple:
    return (READ.format(number=number, literal=literal), _NS, None, "report")


def _inline(model: ImportModel):
    """The one element's init, its INIT-VALUE lines and its constant's value lines, as facts."""
    init, lines, skips = _mapped(model)
    value_lines = [
        (entry.reason, entry.category, entry.rule, entry.effect)
        for entry in skips
        if entry.path == _CONSTANT and entry.tag == "VALUE-SPEC"
    ]
    return init, [(entry.reason, entry.category, entry.rule, entry.effect) for entry in lines], value_lines


def _with_scales(model: ImportModel, path: str, scales: list[CompuScaleInfo]) -> ImportModel:
    (method,) = [method for method in model.compu_methods if method.path == path]
    method.scales = scales
    method.scale_count = len(scales)
    return model


# The rows, on inline models: an enumeration (``/P/A/Gear``: P=0, D=1) and an
# implementation type referencing a table (``/P/T/Mode``: OFF=0, ON=1)


@pytest.mark.parametrize("form", FORMS)
class TestANumberNamingOneScaleIsCarriedAsItsLiteral:
    def test_an_enumerations_own_value(self, form: ValueForm) -> None:
        init, lines, value_lines = _inline(_element(_model(), "/P/A/Gear", _number(1, form)))
        assert init == "D"
        assert lines == []
        assert value_lines == [_own("D", "1", _C1221)]

    def test_an_implementation_types_own_value(self, form: ValueForm) -> None:
        init, lines, value_lines = _inline(_element(_model(), "/P/T/Mode", _number(1, form)))
        assert init == "ON"
        assert lines == []
        assert value_lines == [_own("ON", "1", _C1225)]

    def test_an_enumeration_member(self, form: ValueForm) -> None:
        init, lines, value_lines = _inline(_element(_members(_model()), "/P/A/Rec", _record(_number(0, form))))
        assert init == {"gear": "P"}
        assert lines == []
        assert value_lines == [_member("P", "0")]

    def test_a_member_under_a_table(self, form: ValueForm) -> None:
        init, lines, value_lines = _inline(_element(_members(_model()), "/P/T/Pair", _record(_number(1, form))))
        assert init == {"a": "ON"}
        assert lines == []
        assert value_lines == [_member("ON", "1")]

    def test_a_ports_own_value_carries_its_line_with_its_category_and_rule(self, form: ValueForm) -> None:
        outcome = _port_outcome(_model(), "/P/A/Gear", _number(0, form))
        assert (outcome.value, outcome.reason) == ("P", None)
        assert [tuple(line) for line in outcome.value_lines] == [_own("P", "0", _C1221)[:3]]

    def test_a_ports_member_value_is_descriptive(self, form: ValueForm) -> None:
        outcome = _port_outcome(_members(_model()), "/P/T/Pair", _record(_number(0, form)))
        assert (outcome.value, outcome.reason) == ({"a": "OFF"}, None)
        assert [tuple(line) for line in outcome.value_lines] == [_member("OFF", "0")[:3]]


def test_an_integral_float_is_read() -> None:
    init, _lines, value_lines = _inline(_element(_model(), "/P/A/Gear", _number(1.0, ValueForm.NUMERICAL)))
    assert init == "D"
    assert value_lines == [_own("D", "1.0", _C1221)]


def test_a_hexadecimal_spelling_is_named_as_written() -> None:
    init, _lines, value_lines = _inline(_element(_model(), "/P/T/Mode", _number(1, ValueForm.NUMERICAL, radix="0x1")))
    assert init == "ON"
    assert value_lines == [_own("ON", "0x1", _C1225)]


def test_two_members_read_take_one_line_each_at_the_values_key() -> None:
    model = _members(_model())
    model.app_record_data_types[0].elements.append(RecordElementInfo(name="again", type_ref="/P/A/Gear"))
    init, _lines, value_lines = _inline(
        _element(model, "/P/A/Rec", _record(_number(1, ValueForm.NUMERICAL), _number(0, ValueForm.APPLICATION)))
    )
    assert init == {"gear": "D", "again": "P"}
    assert value_lines == [_member("D", "1"), _member("P", "0")]


# What stays not carried, with its line


def _not_carried(model: ImportModel, cause) -> None:
    init, lines, value_lines = _inline(model)
    assert init is None
    reason = init_line_reason([(cause, 1)])
    assert lines == [(reason.text, reason.category, reason.rule, "abort")]
    assert value_lines == []


def _two_scales() -> list[CompuScaleInfo]:
    return [CompuScaleInfo(value=1, literal="A"), CompuScaleInfo(value=1, literal="B")]


_ENUM = ("/P/A/Gear", "/P/C/Gear_CM", NOT_AN_ENUMERATION_TEXT, ENUMERATION_MEMBER_GIVEN_AS_A_NUMBER, "/P/A/Rec")
_TABLE = ("/P/T/Mode", "/P/C/Mode_CM", NOT_A_TEXT_VALUE_UNDER_A_TABLE, MEMBER_NUMBER_UNDER_A_TABLE, "/P/T/Pair")


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("side", [_ENUM, _TABLE], ids=["enumeration", "table"])
@pytest.mark.parametrize("own", [True, False], ids=["own", "member"])
class TestANumberNoSingleScaleNamesIsNotCarried:
    @staticmethod
    def _case(model: ImportModel, side: tuple, own: bool, value: ConstantScalar) -> None:
        own_type, _method, own_cause, member_cause, member_type = side
        if own:
            _not_carried(_element(model, own_type, value), own_cause)
        else:
            _not_carried(_element(_members(model), member_type, _record(value)), member_cause)

    def test_matching_no_scale(self, side: tuple, own: bool, form: ValueForm) -> None:
        self._case(_model(), side, own, _number(7, form))

    def test_matching_two_scales(self, side: tuple, own: bool, form: ValueForm) -> None:
        self._case(_with_scales(_model(), side[1], _two_scales()), side, own, _number(1, form))

    def test_a_number_that_is_not_integral(self, side: tuple, own: bool, form: ValueForm) -> None:
        self._case(_model(), side, own, _number(0.5, form))

    def test_a_table_with_a_symbol(self, side: tuple, own: bool, form: ValueForm) -> None:
        scales = [CompuScaleInfo(value=0, literal="X", states_symbol=True), CompuScaleInfo(value=1, literal="Y")]
        self._case(_with_scales(_model(), side[1], scales), side, own, _number(1, form))

    def test_a_table_that_lost_a_scale(self, side: tuple, own: bool, form: ValueForm) -> None:
        self._case(_dropped(_model()), side, own, _number(1, form))

    def test_a_range_scale(self, side: tuple, own: bool, form: ValueForm) -> None:
        scales = [CompuScaleInfo(value=0, literal="LOW", upper_limit="5"), CompuScaleInfo(value=6, literal="HIGH")]
        self._case(_with_scales(_model(), side[1], scales), side, own, _number(0, form))

    def test_a_table_the_import_cannot_hold_though_one_scale_names_the_number(
        self, side: tuple, own: bool, form: ValueForm
    ) -> None:
        """A defective table is not read, though one scale names the number.

        Two scales of the value ``0`` make the table a defect (``merge.texttable_defect``), so the number ``1``, which
        one scale names, is not read either.
        """
        scales = [
            CompuScaleInfo(value=0, literal="A"),
            CompuScaleInfo(value=0, literal="B"),
            CompuScaleInfo(value=1, literal="C"),
        ]
        self._case(_with_scales(_model(), side[1], scales), side, own, _number(1, form))


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("own", [True, False], ids=["own", "member"])
def test_a_bitfield_texttable_is_not_read(own: bool, form: ValueForm) -> None:
    model = _model(category="BITFIELD_TEXTTABLE")
    if own:
        _not_carried(_element(model, "/P/T/Mode", _number(1, form)), NOT_A_TEXT_VALUE_UNDER_A_TABLE)
    else:
        _not_carried(_element(_members(model), "/P/T/Pair", _record(_number(1, form))), MEMBER_NUMBER_UNDER_A_TABLE)


def test_a_collapsed_twin_keeps_its_line() -> None:
    init, lines, value_lines = _inline(_twin_model(_number(1, ValueForm.APPLICATION)))
    assert init is None
    assert lines == [(TWIN_TABLE_SENTENCE, _NS, None, "abort")]
    assert value_lines == []
    assert "through the file's own type" in TWIN_TABLE_SENTENCE


# The existing descriptive lines keep their bytes, as records with their category


def test_the_descriptive_lines_are_records_of_their_old_category() -> None:
    from test_application_value_form import _application
    from test_application_value_form import _twin_model as _application_twin_model

    source = SourceTypes(_application_twin_model(_application(3)))
    lines = carried_value_lines(_application(3, "/P/U/Kmh"), "/P/A/Level", source, application=False)
    assert [tuple(line) for line in lines] == [
        (TWIN_NUMERICAL_SENTENCE, _NS, None),
        (VALUE_UNIT_SENTENCE, _NS, None),
    ]


# A synthetic export: SWCraft's own build, each value edited into a number

_AR = "{http://autosar.org/schema/r4.0}"
_COMPONENT = "/ComponentTypes/SignalsSwc"
_VARIABLES = f"{_COMPONENT}/SignalsSwc_InternalBehavior"


def _numbered_root(tmp_path: Path) -> Path:
    """``_valued_component_root`` plus a record, two port values and two inter-runnable variables.

    ``pair`` is an implementation record whose ``mode`` member is table-typed; ``pSignals``' own values of
    ``mode`` and ``own_table`` are in place; the variables are ``modeVar`` (``Mode_T``) and ``tableVar``
    (``ExchangeMode``).
    """
    root = _valued_component_root(tmp_path)
    write_model_element(
        root,
        {
            "name": "ExchangePair",
            "kind": "implementation_record",
            "elements": [{"name": "mode", "type": "ExchangeMode"}, {"name": "count", "type": "uint8"}],
        },
    )
    interface = root / "interfaces" / "ISignals.yaml"
    document = yaml.safe_load(interface.read_text(encoding="utf-8"))
    document["data_elements"].append(
        {"name": "pair", "implementation_type": "ExchangePair", "init": {"mode": "BUSY", "count": 3}}
    )
    interface.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    component = root / "swc" / "signals" / "SignalsSwc.yaml"
    document = yaml.safe_load(component.read_text(encoding="utf-8"))
    ports = {port["name"]: port for port in document["ports"]}
    ports["pSignals"]["com_specs"] += [
        {"data_element": "mode", "init": "OFF"},
        {"data_element": "own_table", "init": "IDLE"},
    ]
    document["variables"] = [
        {"name": "modeVar", "access": "explicit", "application_type": "Mode_T", "init": "OFF"},
        {"name": "tableVar", "access": "explicit", "implementation_type": "ExchangeMode", "init": "IDLE"},
    ]
    component.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return root


def _as_number(value: Any, number: str, form: ValueForm) -> None:
    """Rewrite the value specification *value* in place into *number* in *form*, its label kept."""
    label = value.find(f"{_AR}SHORT-LABEL")
    for child in list(value):
        value.remove(child)
    if label is not None:
        value.append(label)
    if form == ValueForm.NUMERICAL:
        value.tag = f"{_AR}NUMERICAL-VALUE-SPECIFICATION"
        etree.SubElement(value, f"{_AR}VALUE").text = number
    else:
        value.tag = f"{_AR}APPLICATION-VALUE-SPECIFICATION"
        etree.SubElement(value, f"{_AR}CATEGORY").text = "VALUE"
        content = etree.SubElement(value, f"{_AR}SW-VALUE-CONT")
        etree.SubElement(etree.SubElement(content, f"{_AR}SW-VALUES-PHYS"), f"{_AR}V").text = number


def _variable_value(root: Any, name: str) -> Any:
    (variable,) = [
        node for node in root.iter(f"{_AR}VARIABLE-DATA-PROTOTYPE") if node.findtext(f"{_AR}SHORT-NAME") == name
    ]
    (value,) = list(variable.find(f"{_AR}INIT-VALUE"))
    return value


def _member_value(root: Any, element: str, tag: str, text: str) -> Any:
    """The member of *element*'s constant written as *tag* holding *text*."""
    constant = _value_of_constant(root, init_constant_name("ISignals", element))
    (member,) = [node for node in constant.iter(f"{_AR}{tag}") if text in [t.text for t in node.iter()]]
    return member


#: Each edit: (name, how it finds the value, the number, the literal it names, own value or member, rule).
EDITS: list[tuple[str, Any, str, str, bool, StandardRule | None]] = [
    (
        "element-enum",
        lambda root: _value_of_constant(root, init_constant_name("ISignals", "mode")),
        "1",
        "ON",
        True,
        _C1221,
    ),
    (
        "element-table",
        lambda root: _value_of_constant(root, init_constant_name("ISignals", "own_table")),
        "1",
        "BUSY",
        True,
        _C1225,
    ),
    (
        "member-enum",
        lambda root: _member_value(root, "deep", "APPLICATION-VALUE-SPECIFICATION", "OFF"),
        "0",
        "OFF",
        False,
        None,
    ),
    (
        "member-table",
        lambda root: _member_value(root, "pair", "TEXT-VALUE-SPECIFICATION", "BUSY"),
        "1",
        "BUSY",
        False,
        None,
    ),
    ("port-enum", lambda root: _value_in_place(root, "pSignals", "mode"), "0", "OFF", True, _C1221),
    ("port-table", lambda root: _value_in_place(root, "pSignals", "own_table"), "0", "IDLE", True, _C1225),
    ("variable-enum", lambda root: _variable_value(root, "modeVar"), "0", "OFF", True, _C1221),
    ("variable-table", lambda root: _variable_value(root, "tableVar"), "0", "IDLE", True, _C1225),
]


def _edited_export(tmp_path: Path, form: ValueForm, version: str, *, component: bool, number: str | None = None):
    root = _numbered_root(tmp_path)
    arxml = tmp_path / f"numbers-{version}.arxml"
    if component:
        export_component(str(root), "SignalsSwc", str(arxml), version=version)
    else:
        generate_arxml(str(root), str(arxml), version=version)
    tree = etree.parse(str(arxml))
    for _name, find, edit_number, _literal, _own_value, _rule in EDITS:
        _as_number(find(tree.getroot()), number or edit_number, form)
    tree.write(str(arxml), xml_declaration=True, encoding="UTF-8")
    return root, arxml


def _reading_lines(entries) -> list[tuple[str, SkipCategory, StandardRule | None]]:
    return sorted(
        (entry.reason, entry.category, entry.rule)
        for entry in entries
        if "its type's table gives that number" in entry.reason
    )


def _expected_reading_lines() -> list[tuple[str, SkipCategory, StandardRule | None]]:
    return sorted(
        (
            (CARRIED if own else "") + READ.format(number=number, literal=literal),
            _ED if own else _NS,
            rule,
        )
        for _name, _find, number, literal, own, rule in EDITS
    )


def _yaml_of(model_root: Path) -> dict[str, str]:
    return {
        path.relative_to(model_root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(model_root.rglob("*.yaml"), key=lambda p: p.as_posix())
        if path.parts[-2] in ("interfaces", "signals")
    }


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
@pytest.mark.parametrize("form", FORMS)
class TestASyntheticExport:
    def test_import_model_carries_every_number_as_its_literal_with_its_line(
        self, tmp_path: Path, form: ValueForm, version: str
    ) -> None:
        source_root, arxml = _edited_export(tmp_path, form, version, component=False)
        project = _init_project(tmp_path / "imported")

        report = run_import(str(arxml), str(project / "swcraft.yaml"))

        assert _reading_lines(report.skip_entries) == _expected_reading_lines()
        reading = [entry for entry in report.skip_entries if "its type's table gives that number" in entry.reason]
        assert {entry.effect for entry in reading} == {"report"}
        assert [entry for entry in report.skip_entries if entry.tag == "INIT-VALUE" and entry.effect == "abort"] == []
        source = {i.name: i for i in read_interfaces(str(source_root))}["ISignals"]
        imported = {i.name: i for i in read_interfaces(str(project / "model"))}["ISignals"]
        assert {de.name: de.init for de in imported.data_elements} == {de.name: de.init for de in source.data_elements}
        (swc,) = [s for s in read_swc_model(str(project / "model")) if s.name == "SignalsSwc"]
        (source_swc,) = [s for s in read_swc_model(str(source_root)) if s.name == "SignalsSwc"]
        # The importer orders a port's com-specs by the interface's elements; compared by element.
        assert {p.name: {c.data_element: c for c in p.com_specs} for p in swc.ports} == {
            p.name: {c.data_element: c for c in p.com_specs} for p in source_swc.ports
        }
        assert {v.name: v.init for v in swc.variables} == {"modeVar": "OFF", "tableVar": "IDLE"}
        issues = check_cross_layer_consistency(str(project / "model")).issues
        assert [issue for issue in issues if "init" in issue.lower()] == []

    def test_the_defect_lines_are_under_the_exports_defects(
        self, tmp_path: Path, form: ValueForm, version: str
    ) -> None:
        _source_root, arxml = _edited_export(tmp_path, form, version, component=False)
        project = _init_project(tmp_path / "imported")

        report = run_import(str(arxml), str(project / "swcraft.yaml"))

        keys = {(entry.path, entry.tag) for entry in report.skip_entries if entry.reason.startswith(CARRIED)}
        assert keys == {
            ("/PortInterfaces/ISignals_mode_Init", "VALUE-SPEC"),
            ("/PortInterfaces/ISignals_own_table_Init", "VALUE-SPEC"),
            (f"{_COMPONENT}/pSignals/PROVIDED-COM-SPECS", "INIT-VALUE"),
            (f"{_VARIABLES}/modeVar", "INIT-VALUE"),
            (f"{_VARIABLES}/tableVar", "INIT-VALUE"),
        }

    def test_the_written_file_holds_the_literal_and_a_reimport_is_unchanged(
        self, tmp_path: Path, form: ValueForm, version: str
    ) -> None:
        _source_root, arxml = _edited_export(tmp_path, form, version, component=False)
        first = _init_project(tmp_path / "first")
        run_import(str(arxml), str(first / "swcraft.yaml"))
        written = tmp_path / "written.arxml"
        generate_arxml(str(first / "model"), str(written), version=version)
        root = etree.parse(str(written)).getroot()
        mode = _value_of_constant(root, init_constant_name("ISignals", "mode"))
        assert mode.findtext(f"{_AR}SW-VALUE-CONT/{_AR}SW-VALUES-PHYS/{_AR}VT") == "ON"
        assert _value_of_constant(root, init_constant_name("ISignals", "own_table")).findtext(f"{_AR}VALUE") == "BUSY"
        assert _variable_value(root, "tableVar").findtext(f"{_AR}VALUE") == "IDLE"
        second = _init_project(tmp_path / "second")

        report = run_import(str(written), str(second / "swcraft.yaml"))

        assert _reading_lines(report.skip_entries) == []
        assert _yaml_of(second / "model") == _yaml_of(first / "model")


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
@pytest.mark.parametrize("form", FORMS)
def test_import_component_carries_every_number_as_its_literal(tmp_path: Path, form: ValueForm, version: str) -> None:
    source_root, arxml = _edited_export(tmp_path, form, version, component=True)
    project = _init_project(tmp_path / "imported")

    report = import_component(str(arxml), str(project / "model"))

    assert report.swc_action == "created"
    lines = [line for line in report.not_carried_lines if "its type's table gives that number" in line]
    assert len(lines) == len(EDITS)
    assert sum(f"R{version} [constr_1221]" in line for line in lines) == 3
    assert sum(f"R{version} [constr_1225]" in line for line in lines) == 3
    source = {i.name: i for i in read_interfaces(str(source_root))}["ISignals"]
    imported = {i.name: i for i in read_interfaces(str(project / "model"))}["ISignals"]
    assert {de.name: de.init for de in imported.data_elements} == {de.name: de.init for de in source.data_elements}
    (swc,) = read_swc_model(str(project / "model"))
    assert {v.name: v.init for v in swc.variables} == {"modeVar": "OFF", "tableVar": "IDLE"}


def test_import_component_still_aborts_on_a_number_no_scale_names(tmp_path: Path) -> None:
    _source_root, arxml = _edited_export(tmp_path, ValueForm.NUMERICAL, "4.2.2", component=True, number="7")
    project = _init_project(tmp_path / "imported")

    with pytest.raises(LossyComponentImportError) as caught:
        import_component(str(arxml), str(project / "model"))

    assert "not an application value whose text names one scale of its enumeration" in str(caught.value)
