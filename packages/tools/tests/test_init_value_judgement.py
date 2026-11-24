"""A source init value is judged by the SOURCE's types.

``arxml_import.type_mapping.judge_init_value`` walks the reader's value tree
against the reader's own type records -- never the model's -- and names every
leaf that is not carried, by cause; ``map_interfaces`` then records one line per
element. This module holds an implementation type that references a table, a text
value anywhere, and the line of a collapsed twin whose implementation type references
a table, which the model-side mapper writes.

Every case is an inline :class:`ImportModel`, as ``test_arxml_import_interfaces.py``
builds them, except three that go through a whole import: a hand-built file in
``tmp_path`` with ``check-model`` on the imported model, and two round trips of
SWCraft's own output (an interface element's values and a port's own value).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from project_support import init_project as _init_project
from swcraft_tools.arxml_import import map_data_types, map_interfaces, run_import
from swcraft_tools.arxml_import.type_mapping import (
    INIT_CAUSES,
    INIT_LINE_PATTERNS,
    MEMBER_NAMING_NO_ENTRY,
    MEMBER_NUMBER_UNDER_A_TABLE,
    NOT_A_TEXT_VALUE_UNDER_A_TABLE,
    NOT_AN_ENUMERATION_TEXT,
    TEXT_NAMING_A_SHORT_LABEL,
    TEXT_UNDER_A_SYMBOL_TABLE,
    TEXT_VALUE_UNDER_A_MIXED_TABLE,
    TEXT_VALUE_UNDER_AN_APPLICATION_TYPE,
    TEXT_VALUE_WITHOUT_A_TABLE,
    TWIN_TABLE_SENTENCE,
    SourceTypes,
    init_line_reason,
    init_value_unit_differs,
    judge_init_value,
    port_init_outcome,
)
from swcraft_tools.arxml_reader import (
    AppArrayDataTypeInfo,
    AppPrimitiveDataTypeInfo,
    AppRecordDataTypeInfo,
    CompuMethodInfo,
    CompuScaleInfo,
    ConstantArray,
    ConstantRecord,
    ConstantRecordField,
    ConstantScalar,
    ConstantSpecInfo,
    DataTypeMapInfo,
    DataTypeMappingSetInfo,
    ImplDataTypeInfo,
    ImplSubElementInfo,
    ImportModel,
    PortComSpecInitInfo,
    RecordElementInfo,
    SkipCategory,
    SRDataElementInfo,
    SRInterfaceInfo,
    StandardRule,
    SwBaseTypeInfo,
    ValueForm,
)
from swcraft_tools.data_description import DataDescription
from swcraft_tools.validator import check_cross_layer_consistency

_CONSTANT = "/P/Constants/SR_I_e_Init"


def _scales(*texts: str, symbol: bool = False, labels: tuple[str, ...] = ()) -> list[CompuScaleInfo]:
    return [
        CompuScaleInfo(
            value=index,
            literal=text,
            states_symbol=symbol,
            short_label=labels[index] if index < len(labels) else None,
        )
        for index, text in enumerate(texts)
    ]


def _model(*, category: str = "TEXTTABLE", scales: list[CompuScaleInfo] | None = None) -> ImportModel:
    """A base type, a table, two value types over it, an application primitive and an enumeration.

    One value type references the table (``/P/T/Mode``), one references none (``/P/T/Plain``); the
    application primitive is ``/P/A/Count`` and the enumeration ``/P/A/Gear``.
    """
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(path="/P/T/u8", name="u8", size=8, encoding="NONE", native_declaration="uint8", description="")
    )
    model.compu_methods.append(
        CompuMethodInfo(
            path="/P/C/Mode_CM",
            name="Mode_CM",
            category=category,
            scales=scales if scales is not None else _scales("OFF", "ON"),
            description="",
        )
    )
    model.compu_methods.append(
        CompuMethodInfo(
            path="/P/C/Gear_CM", name="Gear_CM", category="TEXTTABLE", scales=_scales("P", "D"), description=""
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/P/T/Mode",
            name="Mode",
            category="VALUE",
            base_type_ref="/P/T/u8",
            impl_type_ref=None,
            description="",
            compu_method_ref="/P/C/Mode_CM",
        )
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
        AppPrimitiveDataTypeInfo(path="/P/A/Gear", name="Gear", compu_method_ref="/P/C/Gear_CM", description="")
    )
    return model


def _element(model: ImportModel, type_ref: str, value) -> ImportModel:
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/P/I/I",
            name="I",
            description="",
            data_elements=[SRDataElementInfo(name="e", type_ref=type_ref, init_ref=_CONSTANT)],
        )
    )
    model.constant_specs.append(
        ConstantSpecInfo(path=_CONSTANT, name="SR_I_e_Init", value=value, unreadable_reason=None)
    )
    return model


def _mapped(model: ImportModel):
    """Map *model*; the one data element's init, its INIT-VALUE lines, and every line the mapping recorded."""
    forms: list = []
    data_types, registry, _type_skips = map_data_types(model, elements=forms)
    interfaces, skips = map_interfaces(model, registry, data_types, elements=forms)
    (interface,) = [i for i in interfaces if i.name == "I"]
    lines = [entry for entry in skips if entry.path == "/P/I/I/e" and entry.tag == "INIT-VALUE"]
    return interface.data_elements[0].init, lines, skips


def _import(model: ImportModel):
    """Map *model*; the one data element's init and its INIT-VALUE lines."""
    init, lines, _skips = _mapped(model)
    return init, lines


def _judge(model: ImportModel, type_ref: str, value):
    return judge_init_value(value, type_ref, SourceTypes(model))


def _dropped(model: ImportModel) -> ImportModel:
    """*model* as the reader leaves it when it dropped one malformed scale of every table."""
    for method in model.compu_methods:
        method.scale_count = len(method.scales) + 1
    return model


def _port_outcome(model: ImportModel, type_ref: str, value):
    """A port's own value written in place for the one data element, decided."""
    forms: list = []
    data_types, _registry, _skips = map_data_types(model, elements=forms)
    return port_init_outcome(
        PortComSpecInitInfo(
            port_path="/P/C/pOut",
            com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
            data_element_ref="/P/I/I/e",
            constant_ref=None,
            block_tag="PROVIDED-COM-SPECS",
            value=value,
        ),
        None,
        type_ref.rsplit("/", 1)[-1],
        {},
        DataDescription.last_declared([*data_types, *forms]),
        application=type_ref.startswith("/P/A/"),
        source_type_ref=type_ref,
        source_types=SourceTypes(model),
    )


def _text(text: str) -> ConstantScalar:
    return ConstantScalar(value=text, form=ValueForm.TEXT)


def _record(*values) -> ConstantRecord:
    return ConstantRecord(fields=[ConstantRecordField(label=None, value=value) for value in values])


def _structure(model: ImportModel, *members: tuple[str, str | None]) -> ImportModel:
    """``/P/T/Pair``, a STRUCTURE whose sub-elements reference the named implementation types."""
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/P/T/Pair",
            name="Pair",
            category="STRUCTURE",
            base_type_ref=None,
            impl_type_ref=None,
            description="",
            sub_elements=tuple(
                ImplSubElementInfo(
                    name=name,
                    category="TYPE_REFERENCE" if ref is not None else "VALUE",
                    impl_type_ref=ref,
                    base_type_ref=None if ref is not None else "/P/T/u8",
                )
                for name, ref in members
            ),
        )
    )
    return model


def _members(model: ImportModel) -> ImportModel:
    """*model* with the structure ``/P/T/Pair`` and the application record ``/P/A/Rec``.

    ``/P/T/Pair`` is a STRUCTURE whose one sub-element references ``/P/T/Mode``; ``/P/A/Rec``'s one element is
    ``/P/A/Gear``.
    """
    _structure(model, ("a", "/P/T/Mode"))
    model.app_record_data_types.append(
        AppRecordDataTypeInfo(
            path="/P/A/Rec", name="Rec", elements=[RecordElementInfo(name="gear", type_ref="/P/A/Gear")], description=""
        )
    )
    return model


class TestAnImplementationTypeThatReferencesATable:
    """A prototype's own value and a member under a type that references a table."""

    def test_a_text_value_naming_one_scale_is_carried_as_the_entrys_name(self):
        init, lines = _import(_element(_model(), "/P/T/Mode", _text("ON")))
        assert init == "ON"
        assert lines == []

    @pytest.mark.parametrize(
        "value",
        [
            ConstantScalar(value=7),
            ConstantScalar(value=7, form=ValueForm.APPLICATION),
            ConstantScalar(value="ON", form=ValueForm.APPLICATION),
            _text("STANDBY"),
        ],
        # A number one scale names is read and carried
        # (`test_enum_number_init_import.py`), so the numbers here name no scale.
        ids=["a number", "an application number", "an application text", "a text naming no scale"],
    )
    def test_any_other_own_value_is_the_files_defect_constr_1225(self, value):
        init, lines = _import(_element(_model(), "/P/T/Mode", value))
        assert init is None
        (line,) = lines
        assert line.reason == init_line_reason([(NOT_A_TEXT_VALUE_UNDER_A_TABLE, 1)]).text
        assert (line.category, line.rule, line.effect) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.TABLE_INIT_IS_A_TEXT_VALUE,
            "abort",
        )

    def test_the_table_is_reached_along_a_reference_chain(self):
        model = _model()
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/P/T/ModeAlias",
                name="ModeAlias",
                category="TYPE_REFERENCE",
                base_type_ref=None,
                impl_type_ref="/P/T/Mode",
                description="",
            )
        )
        assert _judge(model, "/P/T/ModeAlias", ConstantScalar(value=7)) == ((NOT_A_TEXT_VALUE_UNDER_A_TABLE, 1),)
        # A number the table names is read along the chain too.
        assert _judge(model, "/P/T/ModeAlias", ConstantScalar(value=1)) == ()
        assert _judge(model, "/P/T/ModeAlias", _text("OFF")) == ()

    def test_a_text_equal_to_a_scales_short_label_alone_is_swcrafts_own_line(self):
        model = _model(scales=_scales("OFF", "ON", labels=("Off", "On")))
        init, lines = _import(_element(model, "/P/T/Mode", _text("On")))
        assert init is None
        (line,) = lines
        assert line.reason == init_line_reason([(TEXT_NAMING_A_SHORT_LABEL, 1)]).text
        assert (line.category, line.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_a_text_under_a_table_whose_scales_state_a_symbol_is_swcrafts_own_line(self):
        model = _model(scales=_scales("OFF", "ON", symbol=True))
        assert _judge(model, "/P/T/Mode", _text("ON")) == ((TEXT_UNDER_A_SYMBOL_TABLE, 1),)

    def test_a_text_naming_two_scales_names_no_cause(self):
        """The table is the defect, and the import of the table names it; the text's line names no rule.

        The line stands as the mapper's: its model type holds no table (the repeated entry keeps it from becoming one),
        so the text is not carried, in SWCraft's own sentence with no rule.
        """
        model = _model(scales=_scales("ON", "ON"))
        assert _judge(model, "/P/T/Mode", _text("ON")) == ()
        init, lines = _import(_element(model, "/P/T/Mode", _text("ON")))
        assert init is None
        assert [(line.reason, line.category, line.rule, line.effect) for line in lines] == [
            ("init: type 'Mode' takes a number, not the text 'ON'", SkipCategory.NOT_SUPPORTED, None, "abort")
        ]

    def test_a_bitfield_table_judges_a_number_and_leaves_a_text_to_the_mapper(self):
        model = _model(category="BITFIELD_TEXTTABLE", scales=[])
        assert _judge(model, "/P/T/Mode", ConstantScalar(value=1)) == ((NOT_A_TEXT_VALUE_UNDER_A_TABLE, 1),)
        assert _judge(model, "/P/T/Mode", _text("ON")) == ()

    @pytest.mark.parametrize("category", ["SCALE_LINEAR_AND_TEXTTABLE", "SCALE_RATIONAL_AND_TEXTTABLE"])
    def test_a_mixed_category_is_no_table(self, category):
        """``[constr_1284]`` admits a text value under one in R4.2.2 and not in R4.3.0: SWCraft's own line.

        A number is carried as under any other implementation type.
        """
        model = _model(category=category, scales=[])
        assert _judge(model, "/P/T/Mode", _text("ON")) == ((TEXT_VALUE_UNDER_A_MIXED_TABLE, 1),)
        assert _judge(model, "/P/T/Mode", ConstantScalar(value=1)) == ()

    def test_a_member_under_a_table(self):
        model = _structure(_model(), ("a", "/P/T/Mode"), ("b", "/P/T/Mode"), ("c", "/P/T/Mode"))
        assert _judge(model, "/P/T/Pair", _record(_text("ON"), _text("OFF"), _text("OFF"))) == ()
        assert _judge(model, "/P/T/Pair", _record(ConstantScalar(value=7), _text("NONE"), _text("OFF"))) == (
            (MEMBER_NUMBER_UNDER_A_TABLE, 1),
            (MEMBER_NAMING_NO_ENTRY, 1),
        )
        assert _judge(
            model,
            "/P/T/Pair",
            _record(
                ConstantScalar(value=7), ConstantScalar(value=9), ConstantScalar(value="ON", form=ValueForm.APPLICATION)
            ),
        ) == ((MEMBER_NUMBER_UNDER_A_TABLE, 2), (MEMBER_NAMING_NO_ENTRY, 1))

    def test_a_member_under_a_table_takes_the_whole_init_with_it(self):
        model = _structure(_model(), ("a", "/P/T/Mode"), ("b", "/P/T/Plain"))
        init, lines = _import(_element(model, "/P/T/Pair", _record(ConstantScalar(value=7), ConstantScalar(value=3))))
        assert init is None
        (line,) = lines
        assert line.reason == init_line_reason([(MEMBER_NUMBER_UNDER_A_TABLE, 1)]).text
        assert (line.category, line.rule, line.effect) == (SkipCategory.NOT_SUPPORTED, None, "abort")


class TestATextValueAnywhereElse:
    def test_a_prototypes_own_value_of_an_application_primitive_is_constr_1221(self):
        for type_ref in ("/P/A/Count", "/P/A/Gear"):
            assert _judge(_model(), type_ref, _text("P")) == ((TEXT_VALUE_UNDER_AN_APPLICATION_TYPE, 1),), type_ref

    def test_under_a_plain_implementation_type_it_is_constr_1284(self):
        init, lines = _import(_element(_model(), "/P/T/Plain", _text("FULL")))
        assert init is None
        (line,) = lines
        assert (line.category, line.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.TEXT_VALUE_UNDER_A_TABLE_ONLY)

    def test_a_member_of_an_application_record_is_constr_1284(self):
        model = _model()
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path="/P/A/Rec",
                name="Rec",
                elements=[
                    RecordElementInfo(name="gear", type_ref="/P/A/Gear"),
                    RecordElementInfo(name="count", type_ref="/P/A/Count"),
                ],
                description="",
            )
        )
        assert _judge(model, "/P/A/Rec", _record(_text("D"), _text("1"))) == ((TEXT_VALUE_WITHOUT_A_TABLE, 2),)
        # A number in a member in either form, an enumeration's text in the application form:
        # none names a cause.
        assert (
            _judge(
                model,
                "/P/A/Rec",
                _record(ConstantScalar(value="D", form=ValueForm.APPLICATION), ConstantScalar(value=1)),
            )
            == ()
        )

    def test_several_causes_make_one_line_with_the_first_rule_and_every_count(self):
        model = _structure(_model(), ("a", "/P/T/Mode"), ("b", "/P/T/Plain"), ("c", "/P/T/Plain"))
        value = _record(ConstantScalar(value=7), _text("x"), _text("y"))
        causes = _judge(model, "/P/T/Pair", value)
        assert causes == ((TEXT_VALUE_WITHOUT_A_TABLE, 2), (MEMBER_NUMBER_UNDER_A_TABLE, 1))
        init, lines = _import(_element(model, "/P/T/Pair", value))
        (line,) = lines
        assert line.reason == (
            "the init value is not carried: "
            f"{TEXT_VALUE_WITHOUT_A_TABLE.words} (2 leaves); {MEMBER_NUMBER_UNDER_A_TABLE.words} (1 leaf)"
        )
        assert (line.category, line.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.TEXT_VALUE_UNDER_A_TABLE_ONLY)


class TestTheWalkPairsNothing:
    """Where the walk cannot pair the value with a source type it names no cause.

    The mapper's own line stands, unchanged.
    """

    def test_a_type_the_file_does_not_declare(self):
        assert _judge(_model(), "/Elsewhere/T", _text("ON")) == ()

    def test_a_shape_that_does_not_match(self):
        model = _structure(_model(), ("a", "/P/T/Mode"), ("b", "/P/T/Plain"))
        assert _judge(model, "/P/T/Pair", _record(ConstantScalar(value=1))) == ()
        assert _judge(model, "/P/T/Mode", _record(_text("ON"))) == ()
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/P/A/Two", name="Two", element_type_ref="/P/A/Count", max_number_of_elements=2, description=""
            )
        )
        assert _judge(model, "/P/A/Two", ConstantArray(members=[_text("1")])) == ()
        assert _judge(model, "/P/A/Two", ConstantArray(members=[_text("1"), _text("2")])) == (
            (TEXT_VALUE_WITHOUT_A_TABLE, 2),
        )

    def test_a_sibling_of_another_shape_leaves_an_enumeration_member_to_swcrafts_own_line(self):
        """A member whose shape does not match makes the walk pair nothing for the whole value.

        So an enumeration member's text that names no scale gets no cause; the mapper's line for it, matched
        against the model's literals, names no rule either.
        """
        model = _model()
        model.app_array_data_types.append(
            AppArrayDataTypeInfo(
                path="/P/A/Two", name="Two", element_type_ref="/P/A/Count", max_number_of_elements=2, description=""
            )
        )
        model.app_record_data_types.append(
            AppRecordDataTypeInfo(
                path="/P/A/Rec",
                name="Rec",
                elements=[
                    RecordElementInfo(name="gear", type_ref="/P/A/Gear"),
                    RecordElementInfo(name="two", type_ref="/P/A/Two"),
                ],
                description="",
            )
        )
        value = _record(
            ConstantScalar(value="R", form=ValueForm.APPLICATION), ConstantArray(members=[ConstantScalar(value=1)])
        )
        assert _judge(model, "/P/A/Rec", value) == ()
        init, lines = _import(_element(model, "/P/A/Rec", value))
        assert init is None
        (line,) = lines
        assert "names no literal" in line.reason
        assert (line.category, line.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_a_compu_method_the_file_does_not_declare(self):
        model = _model()
        model.compu_methods = [method for method in model.compu_methods if method.name != "Mode_CM"]
        assert _judge(model, "/P/T/Mode", ConstantScalar(value=1)) == ()

    @pytest.mark.parametrize("type_ref", ["/P/T/Mode", "/P/A/Gear"], ids=["a table", "an enumeration"])
    def test_a_text_under_a_table_the_reader_dropped_a_scale_of(self, type_ref):
        """A text naming none of the kept scales of a table the reader dropped a scale of names no cause.

        It may name the dropped scale, so it is never the file's defect under a rule for an entry SWCraft
        dropped. A text naming a kept scale, and a number, are judged as under the whole table."""
        model = _dropped(_model())
        form = ValueForm.TEXT if type_ref == "/P/T/Mode" else ValueForm.APPLICATION
        assert _judge(model, type_ref, ConstantScalar(value="STANDBY", form=form)) == ()
        assert _judge(model, type_ref, ConstantScalar(value="ON", form=form)) == ()
        number_cause = NOT_A_TEXT_VALUE_UNDER_A_TABLE if type_ref == "/P/T/Mode" else NOT_AN_ENUMERATION_TEXT
        assert _judge(model, type_ref, ConstantScalar(value=1)) == ((number_cause, 1),)
        members = _members(model)
        member_type = "/P/T/Pair" if type_ref == "/P/T/Mode" else "/P/A/Rec"
        assert _judge(members, member_type, _record(ConstantScalar(value="STANDBY", form=form))) == ()

    @pytest.mark.parametrize(
        ("type_ref", "value", "cause"),
        [
            ("/P/T/Mode", ConstantScalar(value="ON", form=ValueForm.APPLICATION), NOT_A_TEXT_VALUE_UNDER_A_TABLE),
            (
                "/P/T/Mode",
                ConstantScalar(value="STANDBY", form=ValueForm.APPLICATION),
                NOT_A_TEXT_VALUE_UNDER_A_TABLE,
            ),
            ("/P/A/Gear", _text("P"), TEXT_VALUE_UNDER_AN_APPLICATION_TYPE),
            ("/P/A/Gear", _text("STANDBY"), TEXT_VALUE_UNDER_AN_APPLICATION_TYPE),
            ("/P/T/Pair", _record(ConstantScalar(value="ON", form=ValueForm.APPLICATION)), MEMBER_NAMING_NO_ENTRY),
            ("/P/A/Rec", _record(_text("P")), TEXT_VALUE_WITHOUT_A_TABLE),
            ("/P/A/Rec", _record(_text("STANDBY")), TEXT_VALUE_WITHOUT_A_TABLE),
        ],
        ids=[
            "a table, an application text naming a kept scale",
            "a table, an application text naming none",
            "an enumeration, a text value naming a kept scale",
            "an enumeration, a text value naming none",
            "a table's member, an application text",
            "an enumeration's member, a text value naming a kept scale",
            "an enumeration's member, a text value naming none",
        ],
    )
    def test_a_form_refused_whatever_the_scales_is_judged_as_under_the_whole_table(self, type_ref, value, cause):
        """A dropped scale leaves unpaired only a text in the form the table takes.

        A form the table refuses whatever its scales gets the very line it gets under the whole table -- the
        same cause, section and rule -- and is not carried, for the element's value and a port's own value
        alike.
        """
        whole = _members(_model())
        dropped = _members(_dropped(_model()))
        assert _judge(dropped, type_ref, value) == _judge(whole, type_ref, value) == ((cause, 1),)
        init, lines = _import(_element(dropped, type_ref, value))
        whole_init, whole_lines = _import(_element(_members(_model()), type_ref, value))
        assert init is None
        assert whole_init is None
        (line,) = lines
        assert line.reason == init_line_reason([(cause, 1)]).text
        assert [(entry.reason, entry.category, entry.rule, entry.effect) for entry in lines] == [
            (entry.reason, entry.category, entry.rule, entry.effect) for entry in whole_lines
        ]
        outcome = _port_outcome(dropped, type_ref, value)
        whole_outcome = _port_outcome(whole, type_ref, value)
        assert outcome.value is None
        assert outcome.reason is not None and whole_outcome.reason is not None
        assert (outcome.reason.text, outcome.reason.category, outcome.reason.rule) == (
            whole_outcome.reason.text,
            whole_outcome.reason.category,
            whole_outcome.reason.rule,
        )

    def test_the_symbol_case_and_the_unit_check_are_judged_as_under_the_whole_table(self):
        """The symbol case and the unit check are judged as under the whole table.

        A text under a table whose kept scales state a ``SYMBOL`` is SWCraft's own line whatever was dropped, and an
        enumeration value's unit is never carried whatever was dropped.
        """
        symbol = _dropped(_model(scales=_scales("OFF", "ON", symbol=True)))
        assert _judge(symbol, "/P/T/Mode", _text("STANDBY")) == ((TEXT_UNDER_A_SYMBOL_TABLE, 1),)
        init, lines = _import(_element(symbol, "/P/T/Mode", _text("STANDBY")))
        assert init is None
        (line,) = lines
        assert line.reason == init_line_reason([(TEXT_UNDER_A_SYMBOL_TABLE, 1)]).text
        value = ConstantScalar(value="P", form=ValueForm.APPLICATION, unit_ref="/P/U/Kmh")
        assert init_value_unit_differs(value, "/P/A/Gear", SourceTypes(_dropped(_model())))
        assert init_value_unit_differs(value, "/P/A/Gear", SourceTypes(_model()))

    @pytest.mark.parametrize("type_ref", ["/P/T/Mode", "/P/A/Gear"], ids=["a table", "an enumeration"])
    def test_the_import_of_a_text_naming_a_dropped_scale_names_no_rule(self, type_ref):
        """Where the walk abstains, the mapper's line stands as SWCraft's own and names no rule.

        The mapper matches the text against the scales the reader KEPT, so naming a rule there would blame the file for
        an entry SWCraft dropped.
        """
        form = ValueForm.TEXT if type_ref == "/P/T/Mode" else ValueForm.APPLICATION
        value = ConstantScalar(value="STANDBY", form=form)
        model = _model()
        for method in model.compu_methods:
            method.scale_count = len(method.scales) + 1
        init, lines = _import(_element(model, type_ref, value))
        assert init is None
        (line,) = lines
        assert "names no" in line.reason
        assert (line.category, line.rule) == (SkipCategory.NOT_SUPPORTED, None)

        forms: list = []
        data_types, _registry, _skips = map_data_types(model, elements=forms)
        outcome = port_init_outcome(
            PortComSpecInitInfo(
                port_path="/P/C/pOut",
                com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
                data_element_ref="/P/I/I/e",
                constant_ref=None,
                block_tag="PROVIDED-COM-SPECS",
                value=value,
            ),
            None,
            type_ref.rsplit("/", 1)[-1],
            {},
            DataDescription.last_declared([*data_types, *forms]),
            application=type_ref == "/P/A/Gear",
            source_type_ref=type_ref,
            source_types=SourceTypes(model),
        )
        assert outcome.value is None
        assert outcome.reason is not None and "names no" in outcome.reason.text
        assert (outcome.reason.category, outcome.reason.rule) == (SkipCategory.NOT_SUPPORTED, None)

    def test_a_text_under_an_inline_value_sub_element(self):
        """The reader keeps no inline sub-element's own ``COMPU-METHOD-REF``.

        So a text there cannot be paired with a table or with its absence: no cause, the mapper's line stands.
        A number is carried, as under a type with no table.
        """
        model = _structure(_model(), ("a", None))
        assert _judge(model, "/P/T/Pair", _record(_text("ON"))) == ()
        assert _judge(model, "/P/T/Pair", _record(ConstantScalar(value="ON", form=ValueForm.APPLICATION))) == ()
        assert _judge(model, "/P/T/Pair", _record(ConstantScalar(value=1))) == ()

    def test_an_implementation_array(self):
        """An ``ARRAY`` with one sub-element: each member judged against it, the declared size enforced."""
        model = _model()
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/P/T/Modes",
                name="Modes",
                category="ARRAY",
                base_type_ref=None,
                impl_type_ref=None,
                description="",
                sub_elements=(
                    ImplSubElementInfo(
                        name="m", category="TYPE_REFERENCE", impl_type_ref="/P/T/Mode", base_type_ref=None, array_size=2
                    ),
                ),
            )
        )
        assert _judge(model, "/P/T/Modes", ConstantArray(members=[_text("ON"), _text("OFF")])) == ()
        assert _judge(model, "/P/T/Modes", ConstantArray(members=[ConstantScalar(value=7), _text("OFF")])) == (
            (MEMBER_NUMBER_UNDER_A_TABLE, 1),
        )
        # A size that is not the declared one: no pairing, whatever the members.
        assert _judge(model, "/P/T/Modes", ConstantArray(members=[ConstantScalar(value=1)])) == ()
        assert _judge(model, "/P/T/Modes", _record(_text("ON"), _text("OFF"))) == ()

    def test_a_type_reference_chain_that_closes_on_itself(self):
        model = _model()
        for name, target in (("Loop1", "/P/T/Loop2"), ("Loop2", "/P/T/Loop1")):
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=f"/P/T/{name}",
                    name=name,
                    category="TYPE_REFERENCE",
                    base_type_ref=None,
                    impl_type_ref=target,
                    description="",
                )
            )
        assert _judge(model, "/P/T/Loop1", _text("ON")) == ()

    def test_a_sub_element_with_nested_members_of_its_own(self):
        model = _model()
        model.impl_data_types.append(
            ImplDataTypeInfo(
                path="/P/T/Nest",
                name="Nest",
                category="STRUCTURE",
                base_type_ref=None,
                impl_type_ref=None,
                description="",
                sub_elements=(
                    ImplSubElementInfo(
                        name="inner",
                        category="STRUCTURE",
                        impl_type_ref=None,
                        base_type_ref=None,
                        nested_sub_elements=1,
                    ),
                ),
            )
        )
        assert _judge(model, "/P/T/Nest", _record(_record(_text("x")))) == ()

    def test_a_structure_that_contains_itself(self):
        """The recursion guard: a sub-element referencing a type already on the walk's chain pairs nothing."""
        model = _structure(_model(), ("self", "/P/T/Pair"))
        assert _judge(model, "/P/T/Pair", _record(_record(_text("x")))) == ()


class TestTheSentencesAreAClosedSet:
    def test_one_pattern_per_cause_and_the_twin_line(self):
        # The two reading lines of a number carried as its table's literal follow the twin line.
        assert len(INIT_LINE_PATTERNS) == len(INIT_CAUSES) + 3
        assert INIT_LINE_PATTERNS[-3] == TWIN_TABLE_SENTENCE
        assert INIT_LINE_PATTERNS[-2:] == (
            "init value carried: given as the number {number}, carried as the literal {literal} its type's table "
            "gives that number",
            "given as the number {number}, carried as the literal {literal} its type's table gives that number",
        )
        for cause, pattern in zip(INIT_CAUSES, INIT_LINE_PATTERNS, strict=False):
            line = init_line_reason([(cause, 2)]).text
            assert line == "the init value is not carried: " + pattern.format(count=2, leaves="leaves")

    def test_the_section_follows_the_rule(self):
        for cause in INIT_CAUSES:
            assert (cause.section == 1) == (cause.rule is not None), cause


# A collapsed twin whose implementation type references a table


def _twin_model(value: ConstantScalar) -> ImportModel:
    """The application primitive ``Mode`` (bare) and its same-name implementation twin.

    The twin is a reference type whose chain reaches the table: it collapses, and the element typed by the
    application side is typed in the model by the implementation one.
    """
    model = _model()
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/P/Impl/Mode",
            name="Mode",
            category="TYPE_REFERENCE",
            base_type_ref=None,
            impl_type_ref="/P/T/Mode",
            description="",
        )
    )
    model.app_primitive_data_types.append(
        AppPrimitiveDataTypeInfo(path="/P/A/Mode", name="Mode", compu_method_ref=None, description="")
    )
    model.data_type_mapping_sets.append(
        DataTypeMappingSetInfo(
            path="/P/M/Maps",
            name="Maps",
            maps=[DataTypeMapInfo(application_ref="/P/A/Mode", implementation_ref="/P/Impl/Mode")],
        )
    )
    return _element(model, "/P/A/Mode", value)


class TestACollapsedTwinThatReachesATable:
    """A conforming number whose model twin references a table is not carried: one section-2 line, effect abort.

    The source's number conforms (an application primitive with no compu method, in the application form), but the model
    types the element by the twin, which references a table -- the model can hold only an entry's name there, and a
    number is read as a literal only through the file's own type, which states no table here.
    """

    def test_the_number_is_carried_on_the_source_side_and_refused_with_the_twin_line(self):
        model = _twin_model(ConstantScalar(value=1, form=ValueForm.APPLICATION))
        assert _judge(model, "/P/A/Mode", ConstantScalar(value=1, form=ValueForm.APPLICATION)) == ()
        init, lines = _import(model)
        assert init is None
        (line,) = lines
        assert line.reason == TWIN_TABLE_SENTENCE
        assert (line.category, line.rule, line.effect) == (SkipCategory.NOT_SUPPORTED, None, "abort")


_TWIN_FILE = """<?xml version="1.0" encoding="utf-8"?>
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
        <COMPU-METHOD>
          <SHORT-NAME>Mode_CM</SHORT-NAME>
          <CATEGORY>TEXTTABLE</CATEGORY>
          <COMPU-INTERNAL-TO-PHYS>
            <COMPU-SCALES>
              <COMPU-SCALE>
                <LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>
                <UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>
                <COMPU-CONST><VT>OFF</VT></COMPU-CONST>
              </COMPU-SCALE>
              <COMPU-SCALE>
                <LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT>
                <UPPER-LIMIT INTERVAL-TYPE="CLOSED">1</UPPER-LIMIT>
                <COMPU-CONST><VT>ON</VT></COMPU-CONST>
              </COMPU-SCALE>
            </COMPU-SCALES>
          </COMPU-INTERNAL-TO-PHYS>
        </COMPU-METHOD>
        <IMPLEMENTATION-DATA-TYPE>
          <SHORT-NAME>ModeRaw</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
          <SW-DATA-DEF-PROPS>
            <SW-DATA-DEF-PROPS-VARIANTS>
              <SW-DATA-DEF-PROPS-CONDITIONAL>
                <BASE-TYPE-REF DEST="SW-BASE-TYPE">/P/u8</BASE-TYPE-REF>
                <COMPU-METHOD-REF DEST="COMPU-METHOD">/P/Mode_CM</COMPU-METHOD-REF>
              </SW-DATA-DEF-PROPS-CONDITIONAL>
            </SW-DATA-DEF-PROPS-VARIANTS>
          </SW-DATA-DEF-PROPS>
        </IMPLEMENTATION-DATA-TYPE>
        <CONSTANT-SPECIFICATION>
          <SHORT-NAME>SR_I_mode_Init</SHORT-NAME>
          <VALUE-SPEC>
            <APPLICATION-VALUE-SPECIFICATION>
              <CATEGORY>VALUE</CATEGORY>
              <SW-VALUE-CONT>
                <SW-VALUES-PHYS><V>1</V></SW-VALUES-PHYS>
              </SW-VALUE-CONT>
            </APPLICATION-VALUE-SPECIFICATION>
          </VALUE-SPEC>
        </CONSTANT-SPECIFICATION>
        <SENDER-RECEIVER-INTERFACE>
          <SHORT-NAME>I</SHORT-NAME>
          <IS-SERVICE>false</IS-SERVICE>
          <DATA-ELEMENTS>
            <VARIABLE-DATA-PROTOTYPE>
              <SHORT-NAME>mode</SHORT-NAME>
              <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/P/App/Mode</TYPE-TREF>
              <INIT-VALUE>
                <CONSTANT-REFERENCE>
                  <CONSTANT-REF DEST="CONSTANT-SPECIFICATION">/P/SR_I_mode_Init</CONSTANT-REF>
                </CONSTANT-REFERENCE>
              </INIT-VALUE>
            </VARIABLE-DATA-PROTOTYPE>
          </DATA-ELEMENTS>
        </SENDER-RECEIVER-INTERFACE>
        <DATA-TYPE-MAPPING-SET>
          <SHORT-NAME>Maps</SHORT-NAME>
          <DATA-TYPE-MAPS>
            <DATA-TYPE-MAP>
              <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/P/App/Mode</APPLICATION-DATA-TYPE-REF>
              <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/P/Impl/Mode</IMPLEMENTATION-DATA-TYPE-REF>
            </DATA-TYPE-MAP>
          </DATA-TYPE-MAPS>
        </DATA-TYPE-MAPPING-SET>
      </ELEMENTS>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>App</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-PRIMITIVE-DATA-TYPE>
              <SHORT-NAME>Mode</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
            </APPLICATION-PRIMITIVE-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Impl</SHORT-NAME>
          <ELEMENTS>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Mode</SHORT-NAME>
              <CATEGORY>TYPE_REFERENCE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
          <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/P/ModeRaw</IMPLEMENTATION-DATA-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


def test_the_twin_line_through_a_whole_import_leaves_check_model_green(tmp_path: Path):
    """The twin line through a whole import leaves ``check-model`` green.

    The twin's number, carried on the source side, is refused with the twin line: an import never hands ``check-model``
    a finding it made itself.
    """
    source = tmp_path / "twin.arxml"
    source.write_text(_TWIN_FILE, encoding="utf-8", newline="\n")
    project = _init_project(tmp_path / "project")
    report = run_import(str(source), str(project / "swcraft.yaml"))
    lines = [entry for entry in report.skip_entries if entry.path == "/P/I/mode" and entry.tag == "INIT-VALUE"]
    assert [(entry.reason, entry.category, entry.rule, entry.effect) for entry in lines] == [
        (TWIN_TABLE_SENTENCE, SkipCategory.NOT_SUPPORTED, None, "abort")
    ]
    assert "collapsed into its implementation twin" in " ".join(
        entry.reason for entry in report.skip_entries if entry.path == "/P/App/Mode"
    )
    interface = (project / "model" / "interfaces" / "I.yaml").read_text(encoding="utf-8")
    assert "init" not in interface
    result = check_cross_layer_consistency(str(project / "model"))
    assert result.consistent, result.issues


# SWCraft's own output: a test-built model with a table-typed implementation type


def table_typed_model_root(tmp_path: Path) -> Path:
    """A copy of ``examples/basic`` under *tmp_path* with a texttable and the types and interface over it.

    It adds a value type that references the texttable, a reference type over that value type, an implementation record
    with one member of that reference type and one of a value type with no table, and an interface whose elements
    state an entry's name under each and the record's value. Never recorded: the recorded tree is not extended.
    """
    import shutil

    from conftest import REPO_ROOT, write_model_element

    project = tmp_path / "table-typed"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    model = project / "model"
    write_model_element(
        model,
        {
            "name": "ProbeModeTable",
            "kind": "texttable",
            "entries": [{"name": "OFF", "value": 0}, {"name": "ON", "value": 1}],
        },
    )
    write_model_element(
        model, {"name": "ProbeMode", "kind": "value_type", "base": "uint8_base", "compu_method_ref": "ProbeModeTable"}
    )
    write_model_element(model, {"name": "ProbeModeAlias", "kind": "reference_type", "base": "ProbeMode"})
    write_model_element(model, {"name": "ProbeCount", "kind": "value_type", "base": "uint8_base"})
    write_model_element(
        model,
        {
            "name": "ProbeModePair",
            "kind": "implementation_record",
            "elements": [{"name": "mode", "type": "ProbeModeAlias"}, {"name": "count", "type": "ProbeCount"}],
        },
    )
    write_model_element(
        model,
        {
            "name": "IProbeModes",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "direct", "implementation_type": "ProbeMode", "init": "ON"},
                {"name": "aliased", "implementation_type": "ProbeModeAlias", "init": "OFF"},
                {"name": "pair", "implementation_type": "ProbeModePair", "init": {"mode": "ON", "count": 3}},
            ],
        },
        layer="interfaces",
    )
    return model


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_swcrafts_own_text_values_come_back_with_no_line(version: str, tmp_path: Path):
    """The round trip gives the model's values again, and no init line is written.

    The writer's text values are read, judged against the file's own types and mapped.
    """
    from swcraft_tools.writer import generate_arxml

    model = table_typed_model_root(tmp_path)
    output = tmp_path / f"own-{version}.arxml"
    generate_arxml(str(model), str(output), version=version)
    project = _init_project(tmp_path / "imported")
    report = run_import(str(output), str(project / "swcraft.yaml"))
    import yaml

    interface = yaml.safe_load((project / "model" / "interfaces" / "IProbeModes.yaml").read_text(encoding="utf-8"))
    assert {element["name"]: element.get("init") for element in interface["data_elements"]} == {
        "direct": "ON",
        "aliased": "OFF",
        "pair": {"mode": "ON", "count": 3},
    }
    assert [entry for entry in report.skip_entries if entry.tag == "INIT-VALUE"] == []
    assert check_cross_layer_consistency(str(project / "model")).consistent


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_a_ports_own_value_under_a_table_typed_type_is_a_text_value_in_place(version: str, tmp_path: Path):
    """A port's own value under a table-typed implementation type is a ``TEXT-VALUE-SPECIFICATION`` in place.

    The value (a ``com_specs`` entry's ``init``) names the entry, written by one dispatch for an element's constant and
    a port's own value alike, and comes back as the port's own value with no line.
    """
    import yaml
    from lxml import etree
    from swcraft_tools.writer import generate_arxml

    model = table_typed_model_root(tmp_path)
    consumer = model / "swc" / "example" / "ExampleConsumerSWC.yaml"
    document = yaml.safe_load(consumer.read_text(encoding="utf-8"))
    document["ports"].append(
        {
            "name": "rModes",
            "direction": "require",
            "interface": "IProbeModes",
            "com_specs": [{"data_element": "direct", "init": "OFF"}],
        }
    )
    consumer.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    assert check_cross_layer_consistency(str(model)).consistent

    output = tmp_path / f"port-{version}.arxml"
    generate_arxml(str(model), str(output), version=version)
    ns = {"a": "http://autosar.org/schema/r4.0"}
    root = etree.parse(str(output)).getroot()
    (com_spec,) = [
        node
        for node in root.iterfind(".//a:NONQUEUED-RECEIVER-COM-SPEC", ns)
        if (node.findtext("a:DATA-ELEMENT-REF", namespaces=ns) or "").endswith("/IProbeModes/direct")
    ]
    (value,) = com_spec.find("a:INIT-VALUE", ns)
    assert etree.QName(value).localname == "TEXT-VALUE-SPECIFICATION"
    assert [etree.QName(child).localname for child in value] == ["VALUE"]
    assert value.findtext("a:VALUE", namespaces=ns) == "OFF"

    project = _init_project(tmp_path / "imported")
    report = run_import(str(output), str(project / "swcraft.yaml"))
    (imported,) = (project / "model" / "swc").rglob("ExampleConsumerSWC.yaml")
    ports = {port["name"]: port for port in yaml.safe_load(imported.read_text(encoding="utf-8"))["ports"]}
    assert ports["rModes"]["com_specs"] == [{"data_element": "direct", "init": "OFF"}]
    assert [entry for entry in report.skip_entries if entry.tag == "INIT-VALUE"] == []
