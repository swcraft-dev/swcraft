"""The importer's physics reader, branch by branch.

Unit tests over hand-built ``AppPrimitiveDataTypeInfo`` / ``CompuMethodInfo`` / ``DataConstrInfo`` records:
the refusal branches of ``type_mapping._read_conversion``, ``_read_range`` and ``read_physics`` that the
shape corpus does not reach (every ``DATA-CONSTR`` there declares one rule and one ``PHYS-CONSTRS``), and
whether two declarations merge or conflict over their ranges (``merge_verdict._data_constr_signature``).

**Nothing here runs ``swcraft import model``**, so no claim is about a written file, a rendered report or
an exit code; the end-to-end half is ``test_arxml_import_application_types.py``'s.
"""

from __future__ import annotations

import pytest
from swcraft_tools.arxml_import.merge import _coefficient, _denominator_is_one, declared_paths
from swcraft_tools.arxml_import.merge_verdict import _Context, _data_constr_signature, _Token
from swcraft_tools.arxml_import.type_mapping import (
    INFINITE_LIMIT_REPRESENTED,
    _read_conversion,
    _read_range,
    infinite_limit_lines,
    read_physics,
)
from swcraft_tools.arxml_reader import (
    AppPrimitiveDataTypeInfo,
    CompuMethodInfo,
    CompuRationalScaleInfo,
    DataConstrInfo,
    ImportModel,
    PhysLimit,
    UnitInfo,
    phys_limits,
)


def _compu(
    *,
    category: str = "LINEAR",
    scale_count: int = 1,
    numerator: tuple[str, ...] = ("0", "0.1"),
    denominator: tuple[str, ...] = ("1",),
    rational: bool = True,
) -> CompuMethodInfo:
    return CompuMethodInfo(
        path="/Acme/AppTypes/Compu",
        name="Compu",
        category=category,
        scales=[],
        description="",
        rational_scales=([CompuRationalScaleInfo(numerator=numerator, denominator=denominator)] if rational else []),
        scale_count=scale_count,
    )


def _constr(
    *,
    lower: float | None = 0.0,
    upper: float | None = 100.0,
    rule_count: int = 1,
    unreadable: frozenset[str] = frozenset(),
    inexact: frozenset[str] = frozenset(),
    lower_text: str | None = None,
    upper_text: str | None = None,
    lower_interval: str | None = None,
    upper_interval: str | None = None,
) -> DataConstrInfo:
    return DataConstrInfo(
        path="/Acme/AppTypes/Range",
        name="Range",
        phys_lower=lower,
        phys_upper=upper,
        rule_count=rule_count,
        description="",
        unreadable_limits=unreadable,
        inexact_limits=inexact,
        # The reader sets the texts beside the numbers; the merge signature reads the texts.
        # A text of its own is a limit the reader read as no number: an infinite spelling.
        phys_lower_text=lower_text if lower is None else str(lower),
        phys_upper_text=upper_text if upper is None else str(upper),
        phys_lower_interval=lower_interval,
        phys_upper_interval=upper_interval,
    )


def _app(
    *,
    compu_ref: str | None = None,
    unit_ref: str | None = None,
    constr_ref: str | None = None,
) -> AppPrimitiveDataTypeInfo:
    return AppPrimitiveDataTypeInfo(
        path="/Acme/AppTypes/Speed",
        name="Speed",
        compu_method_ref=compu_ref,
        description="",
        unit_ref=unit_ref,
        data_constr_ref=constr_ref,
        category="VALUE",
    )


class TestACoefficientIsSkippedAndNeverRaises:
    """``_coefficient`` skips and reports a coefficient, never raises."""

    @pytest.mark.parametrize("literal", ["", "  ", "NaN", "inf", "-inf", "one", "0x10", "1,5"])
    def test_a_value_the_reader_cannot_carry_answers_none(self, literal: str) -> None:
        assert _coefficient(literal) == (None, False)

    @pytest.mark.parametrize(("literal", "expected"), [("0", 0.0), ("-40", -40.0), ("1e3", 1000.0), ("0.1", 0.1)])
    def test_a_finite_number_answers_it(self, literal: str, expected: float) -> None:
        value, inexact = _coefficient(literal)
        assert (value, type(value), inexact) == (expected, float, False)

    @pytest.mark.parametrize("literal", ["9007199254740993", "-9223372036854775808", "18446744073709551615"])
    def test_an_integer_no_float_holds_is_the_integer_itself(self, literal: str) -> None:
        """A ``V`` is a ``Numerical``, a string: above 2**53 a float would state another number."""
        value, inexact = _coefficient(literal)
        assert (value, type(value), inexact) == (int(literal), int, False)

    @pytest.mark.parametrize("literal", ["16777217.000000001", "0.10000000000000001", "1e-400"])
    def test_a_float_spelling_no_float_holds_exactly_is_not_carried_and_says_so(self, literal: str) -> None:
        assert _coefficient(literal) == (None, True)

    @pytest.mark.parametrize(
        ("literal", "expected"), [("- 8", -8.0), ("+ 8", 8.0), ("-\t010", -8.0), ("-\r\n0.5", -0.5), ("- 1e3", -1000.0)]
    )
    def test_whitespace_between_the_sign_and_the_literal_is_skipped(self, literal: str, expected: float) -> None:
        """Whitespace between a coefficient's sign and its literal is skipped: ``- 8`` is -8.

        A coefficient is a formula field, whose ``WS`` is hidden between tokens (GST R4.3.0:4809; R4.2.2:3957-3958), so
        the ``float()`` filter reads the text as the formula grammar spells it.
        """
        value, inexact = _coefficient(literal)
        assert (value, type(value), inexact) == (expected, float, False)

    @pytest.mark.parametrize("literal", ["- 8 .5", "1 0", "- -8", "8 -", "- 0x10"])
    def test_whitespace_inside_a_literal_or_a_second_sign_is_still_no_number(self, literal: str) -> None:
        assert _coefficient(literal) == (None, False)


class TestTheDenominatorIsJudgedOnTheSourceLiteral:
    @pytest.mark.parametrize("denominator", [("1",), ("1.0",), ("1/1",), ()])
    def test_the_polynomial_one_is_recognised_in_every_spelling(self, denominator: tuple[str, ...]) -> None:
        # `COMPU-DENOMINATOR` is `minOccurs="0"` and an omitted denominator IS 1, so the
        # EMPTY tuple is a legal LINEAR conversion, not one dropped quoting `[]`.
        assert _denominator_is_one(denominator) is True

    @pytest.mark.parametrize("denominator", [("3",), ("1", "1"), ("x",), ("1/0",)])
    def test_everything_else_is_not(self, denominator: tuple[str, ...]) -> None:
        assert _denominator_is_one(denominator) is False


class TestEveryConversionRefusalNamesItsShape:
    """The five branches of ``_read_conversion`` that answer ``None``."""

    def test_a_non_linear_category_is_named(self) -> None:
        conversion, reason = _read_conversion(_compu(category="RAT_FUNC"))
        assert conversion is None
        assert reason is not None
        assert "category 'RAT_FUNC'" in reason

    def test_several_scales_are_named(self) -> None:
        conversion, reason = _read_conversion(_compu(scale_count=3))
        assert conversion is None
        assert reason is not None
        assert "3 COMPU-SCALE(s)" in reason

    def test_a_scale_with_no_rational_coefficients_is_named(self) -> None:
        conversion, reason = _read_conversion(_compu(rational=False))
        assert conversion is None
        assert reason is not None
        assert "no COMPU-RATIONAL-COEFFS" in reason

    @pytest.mark.parametrize("numerator", [("0",), ("0", "1", "2"), ()])
    def test_a_numerator_of_other_than_two_coefficients_is_named(self, numerator: tuple[str, ...]) -> None:
        conversion, reason = _read_conversion(_compu(numerator=numerator))
        assert conversion is None
        assert reason is not None
        assert f"COMPU-NUMERATOR of {len(numerator)} coefficient(s)" in reason

    def test_a_denominator_that_is_not_one_is_named_with_its_own_spelling(self) -> None:
        conversion, reason = _read_conversion(_compu(denominator=("100",)))
        assert conversion is None
        assert reason is not None
        assert "COMPU-DENOMINATOR ['100']" in reason

    def test_a_non_finite_coefficient_is_named_and_does_not_raise(self) -> None:
        conversion, reason = _read_conversion(_compu(numerator=("0", "NaN")))
        assert conversion is None
        assert reason is not None
        assert "not a finite number" in reason

    def test_an_absent_denominator_is_read_as_one(self) -> None:
        conversion, reason = _read_conversion(_compu(denominator=()))
        assert reason is None
        assert conversion is not None
        assert (conversion.offset, conversion.factor) == (0.0, 0.1)


class TestEveryRangeNoteNamesItsShape:
    """``_read_range``'s four notes."""

    def test_several_rules_are_noted_and_the_first_is_still_read(self) -> None:
        value_range, notes = _read_range(_constr(rule_count=3))
        assert value_range is not None
        assert (value_range.min, value_range.max) == (0.0, 100.0)
        assert len(notes) == 1
        assert "declares 3 DATA-CONSTR-RULEs" in notes[0]

    def test_no_limit_at_all_is_noted_and_the_range_is_dropped(self) -> None:
        value_range, notes = _read_range(_constr(lower=None, upper=None))
        assert value_range is None
        assert notes == [
            "DATA-CONSTR /Acme/AppTypes/Range declares no PHYS-CONSTRS limit; the type is imported without a range"
        ]

    @pytest.mark.parametrize(
        ("lower_text", "upper_text"),
        [("-INF", "INF"), (" -inf ", "+Inf"), ("-INF", None), (None, "INF")],
    )
    def test_limits_infinite_on_their_own_side_alone_drop_the_range_with_no_note(
        self, lower_text: str | None, upper_text: str | None
    ) -> None:
        value_range, notes = _read_range(_constr(lower=None, upper=None, lower_text=lower_text, upper_text=upper_text))
        assert value_range is None
        assert notes == []

    def test_an_own_side_infinity_beside_a_finite_limit_is_a_half_open_range_with_no_note(self) -> None:
        value_range, notes = _read_range(_constr(lower=None, lower_text="-INF"))
        assert notes == []
        assert value_range is not None
        assert (value_range.min, value_range.max) == (None, 100.0)

    def test_a_lower_limit_above_its_upper_is_noted_and_the_range_is_dropped(self) -> None:
        value_range, notes = _read_range(_constr(lower=100.0, upper=0.0))
        assert value_range is None
        assert len(notes) == 1
        assert "lower limit above its upper one" in notes[0]

    def test_an_unreadable_limit_is_noted_and_the_other_bound_survives(self) -> None:
        value_range, notes = _read_range(_constr(lower=None, unreadable=frozenset({"LOWER-LIMIT"})))
        assert value_range is not None
        assert (value_range.min, value_range.max) == (None, 100.0)
        assert len(notes) == 1
        assert "LOWER-LIMIT this release cannot read as a number" in notes[0]

    def test_a_limit_no_model_value_holds_exactly_is_noted_and_the_other_bound_survives(self) -> None:
        value_range, notes = _read_range(_constr(lower=None, inexact=frozenset({"LOWER-LIMIT"})))
        assert value_range is not None
        assert (value_range.min, value_range.max) == (None, 100.0)
        assert notes == [
            "DATA-CONSTR /Acme/AppTypes/Range declares a PHYS-CONSTRS LOWER-LIMIT no model value holds "
            "exactly; that bound is not imported"
        ]

    def test_two_inexact_limits_do_not_also_claim_the_export_declares_none(self) -> None:
        value_range, notes = _read_range(
            _constr(lower=None, upper=None, inexact=frozenset({"LOWER-LIMIT", "UPPER-LIMIT"}))
        )
        assert value_range is None
        assert len(notes) == 1
        assert "LOWER-LIMIT, UPPER-LIMIT no model value holds exactly" in notes[0]

    def test_an_integer_bound_no_float_holds_is_carried_as_the_integer(self) -> None:
        value_range, notes = _read_range(_constr(lower=-9223372036854775808, upper=18446744073709551615))
        assert notes == []
        assert (value_range.min, value_range.max) == (-9223372036854775808, 18446744073709551615)
        assert (type(value_range.min), type(value_range.max)) == (int, int)

    def test_two_unreadable_limits_do_not_also_claim_the_export_declares_none(self) -> None:
        value_range, notes = _read_range(
            _constr(lower=None, upper=None, unreadable=frozenset({"LOWER-LIMIT", "UPPER-LIMIT"}))
        )
        assert value_range is None
        assert len(notes) == 1
        assert "declares no PHYS-CONSTRS limit" not in notes[0]

    def test_a_half_open_range_is_read(self) -> None:
        value_range, notes = _read_range(_constr(lower=None))
        assert notes == []
        assert value_range is not None
        assert (value_range.min, value_range.max) == (None, 100.0)

    @pytest.mark.parametrize(
        ("lower_interval", "upper_interval"),
        [("INFINITE", "INFINITE"), ("INFINITE", None), (None, "INFINITE")],
        ids=["both", "lower", "upper"],
    )
    def test_an_infinite_interval_is_a_declared_limit_and_never_declares_no_limit(
        self, lower_interval: str | None, upper_interval: str | None
    ) -> None:
        """``INFINITE`` holding a number is a declared infinite limit, never *declares no PHYS-CONSTRS limit*."""
        value_range, notes = _read_range(
            _constr(
                lower=None,
                upper=None,
                lower_text="5" if lower_interval else None,
                upper_text="9" if upper_interval else None,
                lower_interval=lower_interval,
                upper_interval=upper_interval,
            )
        )
        assert value_range is None
        assert notes == []

    def test_an_infinite_interval_beside_a_finite_limit_is_a_half_open_range(self) -> None:
        value_range, notes = _read_range(_constr(lower=None, lower_text="5", lower_interval="INFINITE"))
        assert notes == []
        assert value_range is not None
        assert (value_range.min, value_range.max) == (None, 100.0)


class TestAnInfiniteLimitIsRepresentedInAnotherForm:
    """One line per own-side infinite ``PHYS-CONSTRS`` limit, never a skip entry or a physics note.

    ``infinite_limit_lines`` is the one source both importers record into *Represented in another form*.
    """

    @pytest.mark.parametrize(
        ("side", "text", "interval"),
        [
            pytest.param("LOWER-LIMIT", "-INF", None, id="lower-minus-inf"),
            pytest.param("UPPER-LIMIT", "INF", "CLOSED", id="upper-inf"),
            pytest.param("UPPER-LIMIT", "+INF", None, id="upper-plus-inf"),
            pytest.param("LOWER-LIMIT", "5", "INFINITE", id="lower-infinite-with-a-number"),
        ],
    )
    def test_one_line_per_infinite_side(self, side: str, text: str, interval: str | None) -> None:
        lower = side == "LOWER-LIMIT"
        constr = _constr(
            lower=None if lower else 0.0,
            upper=100.0 if lower else None,
            lower_text=text if lower else None,
            upper_text=None if lower else text,
            lower_interval=interval if lower else None,
            upper_interval=None if lower else interval,
        )
        assert infinite_limit_lines(constr) == [
            f"DATA-CONSTR /Acme/AppTypes/Range PHYS-CONSTRS {side}: {INFINITE_LIMIT_REPRESENTED}"
        ]
        assert infinite_limit_lines(constr, inline=False) == [f"PHYS-CONSTRS {side}: {INFINITE_LIMIT_REPRESENTED}"]

    def test_both_sides_infinite_are_two_lines_in_side_order(self) -> None:
        constr = _constr(lower=None, upper=None, lower_text="-INF", upper_text="7", upper_interval="INFINITE")
        assert infinite_limit_lines(constr, inline=False) == [
            f"PHYS-CONSTRS LOWER-LIMIT: {INFINITE_LIMIT_REPRESENTED}",
            f"PHYS-CONSTRS UPPER-LIMIT: {INFINITE_LIMIT_REPRESENTED}",
        ]

    @pytest.mark.parametrize(
        ("lower_text", "lower_interval"),
        [
            pytest.param("NaN", None, id="nan"),
            pytest.param("NaN", "INFINITE", id="nan-under-infinite"),
            pytest.param("INF", None, id="other-side-infinity"),
            pytest.param("+INF", "INFINITE", id="other-side-infinity-under-infinite"),
            pytest.param("5", "OPEN", id="open"),
        ],
    )
    def test_no_line_for_what_is_not_an_own_side_infinity(self, lower_text: str, lower_interval: str | None) -> None:
        constr = _constr(lower=None, lower_text=lower_text, lower_interval=lower_interval)
        assert infinite_limit_lines(constr) == []

    def test_finite_limits_have_no_line(self) -> None:
        assert infinite_limit_lines(_constr()) == []


class TestADanglingPropertyReferenceIsNotedAndNothingRaises:
    """``read_physics``' three dangling-reference branches."""

    def test_a_dangling_unit_ref(self) -> None:
        unit, conversion, value_range, consumed, notes = read_physics(
            _app(unit_ref="/Acme/Missing/U"), {}, {}, {}, compu_method_ref=None, own_forms=frozenset()
        )
        assert (unit, conversion, value_range) == ("", None, None)
        assert consumed == set()
        assert len(notes) == 1
        assert "/Acme/Missing/U" in notes[0]

    def test_a_dangling_compu_method_ref(self) -> None:
        _, conversion, _, consumed, notes = read_physics(
            _app(compu_ref="/Acme/Missing/C"), {}, {}, {}, compu_method_ref="/Acme/Missing/C", own_forms=frozenset()
        )
        assert conversion is None
        assert consumed == set()
        assert len(notes) == 1
        assert "/Acme/Missing/C" in notes[0]

    def test_a_dangling_data_constr_ref(self) -> None:
        _, _, value_range, consumed, notes = read_physics(
            _app(constr_ref="/Acme/Missing/R"), {}, {}, {}, compu_method_ref=None, own_forms=frozenset()
        )
        assert value_range is None
        assert consumed == set()
        assert len(notes) == 1
        assert "/Acme/Missing/R" in notes[0]

    def test_a_resolved_property_is_consumed_and_noted_by_nothing(self) -> None:
        unit_info = UnitInfo(path="/Acme/U", name="U", display_name="km/h", description="")
        constr = _constr()
        unit, conversion, value_range, consumed, notes = read_physics(
            _app(unit_ref="/Acme/U", constr_ref=constr.path),
            {"/Acme/U": unit_info},
            {constr.path: constr},
            {},
            compu_method_ref=None,
            own_forms=frozenset(),
        )
        assert unit == "km/h"
        assert conversion is None
        assert value_range is not None
        assert notes == []
        assert consumed == {"/Acme/U", constr.path}

    def test_no_note_names_a_filesystem_path(self) -> None:
        """Every note this module can mint names an AUTOSAR path inside the export, never a storage location."""
        _, _, _, _, notes = read_physics(
            _app(unit_ref="/Acme/Missing/U", compu_ref="/Acme/Missing/C", constr_ref="/Acme/Missing/R"),
            {},
            {},
            {},
            compu_method_ref="/Acme/Missing/C",
            own_forms=frozenset(),
        )
        assert len(notes) == 3
        for note in notes:
            assert ".yaml" not in note
            assert "model/" not in note


class TestTheRangeHalfOfTheMergeSignature:
    """The merge verdict's range comparison: ``merge_verdict._data_constr_signature`` through the public signature row.

    Two declarations of one application-primitive ``SHORT-NAME`` merge only when nothing the reader reads,
    the range included, distinguishes them.
    """

    def _model(self, constrs: list[DataConstrInfo]) -> ImportModel:
        return ImportModel(
            source_path="synthetic.arxml",
            packages=[],
            path_index={},
            sw_base_types=[],
            impl_data_types=[],
            app_primitive_data_types=[],
            app_record_data_types=[],
            app_array_data_types=[],
            compu_methods=[],
            sr_interfaces=[],
            cs_interfaces=[],
            swcs=[],
            compositions=[],
            skip_log=[],
            units=[],
            data_constrs=constrs,
        )

    def _row(self, app: AppPrimitiveDataTypeInfo, constrs: list[DataConstrInfo]) -> object:
        model = self._model(constrs)
        ctx = _Context(
            token=_Token({}, frozenset()),
            declared=declared_paths(model),
            compu_by_path={},
            constants_by_path={},
            effective_target_by_app_path={},
            unit_by_path={},
            constr_by_path={constr.path: constr for constr in constrs},
        )
        return _data_constr_signature(app, ctx)

    def test_two_identical_constraints_give_one_signature(self) -> None:
        first = _constr()
        second = DataConstrInfo(
            path="/Acme/Other/Range",
            name="Range",
            phys_lower=0.0,
            phys_upper=100.0,
            rule_count=1,
            description="",
            phys_lower_text="0",
            phys_upper_text="100",
        )
        left = self._row(_app(constr_ref=first.path), [first, second])
        right = self._row(
            AppPrimitiveDataTypeInfo(
                path="/Acme/Copy/Speed",
                name="Speed",
                compu_method_ref=None,
                description="",
                data_constr_ref=second.path,
                category="VALUE",
            ),
            [first, second],
        )
        assert left == right

    def test_differing_bounds_give_two_signatures(self) -> None:
        first = _constr()
        second = DataConstrInfo(
            path="/Acme/Other/Range",
            name="Range",
            phys_lower=0.0,
            phys_upper=400.0,
            rule_count=1,
            description="",
            phys_lower_text="0.0",
            phys_upper_text="400",
        )
        assert self._row(_app(constr_ref=first.path), [first, second]) != self._row(
            _app(constr_ref=second.path), [first, second]
        )

    @pytest.mark.parametrize(
        ("left", "right", "same"),
        [
            ("18446744073709551615", "18446744073709551614", False),
            ("16777217.000000001", "16777217", False),
            ("0x10", "16.0", True),
            ("1e2", "100", True),
            ("eleven", "twelve", False),
        ],
    )
    def test_the_limits_are_compared_as_the_source_states_them_exactly(self, left: str, right: str, same: bool) -> None:
        """By value and never through a float.

        Two limits a float rounds together are two limits, carried or not, and one value in two spellings is one.
        """
        first = DataConstrInfo(
            path="/Acme/AppTypes/Range",
            name="Range",
            phys_lower=None,
            phys_upper=None,
            rule_count=1,
            description="",
            phys_upper_text=left,
        )
        second = DataConstrInfo(
            path="/Acme/Other/Range",
            name="Range",
            phys_lower=None,
            phys_upper=None,
            rule_count=1,
            description="",
            phys_upper_text=right,
        )
        rows = [self._row(_app(constr_ref=constr.path), [first, second]) for constr in (first, second)]
        assert (rows[0] == rows[1]) is same

    @pytest.mark.parametrize(
        ("left", "right", "same"),
        [
            pytest.param(("5", "INFINITE"), ("9", "INFINITE"), True, id="infinite-5-vs-infinite-9"),
            pytest.param(("5", "INFINITE"), ("5", "CLOSED"), False, id="infinite-5-vs-closed-5"),
            pytest.param(("5", None), ("5", "CLOSED"), True, id="omitted-vs-closed"),
            pytest.param(("INF", "INFINITE"), ("INF", None), True, id="own-spelling-under-either"),
            pytest.param(("NaN", "INFINITE"), ("9", "INFINITE"), False, id="nan-under-infinite-is-no-absent-limit"),
            pytest.param(("NaN", "INFINITE"), ("NaN", "INFINITE"), True, id="nan-under-infinite-as-spelled"),
        ],
    )
    def test_an_infinite_interval_is_compared_as_infinite_and_omitted_as_closed(
        self, left: tuple[str, str | None], right: tuple[str, str | None], same: bool
    ) -> None:
        """The physical limit's interval, normalised ``or "CLOSED"`` (the standard's default).

        An ``INFINITE`` limit is infinite whatever number it holds; ``NaN`` under ``INFINITE`` is compared
        as spelled.
        """
        first, second = (
            DataConstrInfo(
                path=path,
                name="Range",
                phys_lower=None,
                phys_upper=None,
                rule_count=1,
                description="",
                phys_upper_text=text,
                phys_upper_interval=interval,
            )
            for path, (text, interval) in (("/Acme/AppTypes/Range", left), ("/Acme/Other/Range", right))
        )
        rows = [self._row(_app(constr_ref=constr.path), [first, second]) for constr in (first, second)]
        assert (rows[0] == rows[1]) is same

    def test_an_equal_first_rule_with_a_different_rule_count_still_differs(self) -> None:
        """Two constraints agreeing in their first rule and differing in how many they have differ."""
        first = _constr()
        second = DataConstrInfo(
            path="/Acme/Other/Range",
            name="Range",
            phys_lower=0.0,
            phys_upper=100.0,
            rule_count=2,
            description="",
        )
        assert self._row(_app(constr_ref=first.path), [first, second]) != self._row(
            _app(constr_ref=second.path), [first, second]
        )

    def test_an_undeclared_data_constr_is_a_fail_closed_token(self) -> None:
        """A reference the export does not declare is compared as the reference.

        So two references naming two absent constraints do not merge.
        """
        left = self._row(_app(constr_ref="/Acme/Missing/A"), [])
        right = self._row(_app(constr_ref="/Acme/Missing/B"), [])
        assert left != right
        assert self._row(_app(constr_ref="/Acme/Missing/A"), []) == left

    def test_no_constraint_at_all_answers_none(self) -> None:
        assert self._row(_app(), []) is None


class TestThePhysicalLimitsArePairedOnce:
    """``arxml_reader.phys_limits``: each side's text, value and interval.

    They are read by the range check, the merge signatures and the represented lines alike.
    """

    def test_each_side_carries_its_own_four_fields(self) -> None:
        constr = _constr(lower=0.0, upper=None, upper_text="INF", upper_interval="OPEN")
        assert phys_limits(constr) == (
            PhysLimit("LOWER-LIMIT", "0.0", 0.0, None),
            PhysLimit("UPPER-LIMIT", "INF", None, "OPEN"),
        )
        assert phys_limits(constr).upper_limit.bounds_nothing
        assert not phys_limits(constr).lower_limit.bounds_nothing

    def test_an_absent_limit_bounds_nothing_only_by_being_absent(self) -> None:
        """``bounds_nothing`` is the reader's judgement of a STATED limit; an absent one is not judged."""
        assert not phys_limits(
            _constr(lower=None, lower_text=None, lower_interval="INFINITE")
        ).lower_limit.bounds_nothing
