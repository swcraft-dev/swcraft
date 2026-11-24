"""The execution-time budget grammar, its units, CSE codes and exact arithmetic.

Every expectation is typed from TPS Generic Structure R4.3.0 Table 4.92 *CseCodeType in
Time Domain* (R4.2.2 ``[TPS_GST_00354]``), never read back from the module under test, so
a wrong code in :data:`CSE_CODE` fails here rather than agreeing with itself.

**What this suite does not reach.** It pins the leaf alone: ``schema.py`` refusing through
it, the writer emitting :attr:`Budget.cse_code` and the importer calling :func:`from_cse`
are the load, writer and importer suites'. That the table is the standard's is a
transcription, checked by reading the cited text; no test can.
"""

from __future__ import annotations

import ast
from fractions import Fraction
from pathlib import Path

import pytest
from swcraft_tools import execution_budget as budget_module
from swcraft_tools.execution_budget import (
    MAX_FACTOR_DIGITS,
    Budget,
    BudgetFormatError,
    CseNotABudget,
    CseOverTheCap,
    finest,
    format_exact,
    from_cse,
    parse_budget,
)

MODULE_PATH = Path(budget_module.__file__)

#: TPS GST R4.3.0 Table 4.92, typed by hand: code -> length of 1 of it, seconds.
TABLE_4_92 = {
    0: Fraction(1, 10**6),
    1: Fraction(1, 10**5),
    2: Fraction(1, 10**4),
    3: Fraction(1, 10**3),
    4: Fraction(1, 10**2),
    5: Fraction(1, 10),
    6: Fraction(1),
    7: Fraction(10),
    8: Fraction(60),
    9: Fraction(3600),
    10: Fraction(86400),
    20: Fraction(1, 10**15),
    21: Fraction(1, 10**14),
    22: Fraction(1, 10**13),
    23: Fraction(1, 10**12),
    24: Fraction(1, 10**11),
    25: Fraction(1, 10**10),
    26: Fraction(1, 10**9),
    27: Fraction(1, 10**8),
    28: Fraction(1, 10**7),
}


class TestTheGrammarAccepts:
    @pytest.mark.parametrize(
        ("text", "factor", "unit"),
        [("850ns", 850, "ns"), ("400us", 400, "us"), ("2ms", 2, "ms"), ("1s", 1, "s"), ("0us", 0, "us")],
    )
    def test_a_whole_number_and_a_unit(self, text: str, factor: int, unit: str) -> None:
        budget = parse_budget(text)
        assert budget == Budget(factor, unit)
        assert budget.text == text

    def test_eighteen_digits_is_the_cap_and_parses(self) -> None:
        text = "9" * MAX_FACTOR_DIGITS + "ns"
        assert MAX_FACTOR_DIGITS == 18
        assert parse_budget(text) == Budget(999_999_999_999_999_999, "ns")


class TestTheGrammarRefuses:
    @pytest.mark.parametrize(
        ("value", "sentence"),
        [
            (400, "wcet 400 names no unit: write it with one of ns, us, ms, s, e.g. '400us'"),
            (1.5, "wcet 1.5 is not a budget: write a whole number with one of ns, us, ms, s, e.g. '1500us'"),
            (True, "wcet True is not a budget: write a whole number with one of ns, us, ms, s, e.g. '400us'"),
            (
                "1.5ms",
                "wcet '1.5ms' is not a whole number of ms (an ARXML cseCodeFactor is an Integer): write '1500us'",
            ),
            (
                "1.5s",
                "wcet '1.5s' is not a whole number of s (an ARXML cseCodeFactor is an Integer): write '1500ms'",
            ),
            (
                "1.5ns",
                "wcet '1.5ns' is not a whole number of ns (an ARXML cseCodeFactor is an Integer): "
                "a time below 1 ns cannot be written: the finest unit is ns",
            ),
            ("850 ns", "wcet '850 ns' has a space: write '850ns'"),
            (" 850ns", "wcet ' 850ns' has a space: write '850ns'"),
            ("0850ns", "wcet '0850ns' has a leading zero: write '850ns'"),
            (
                "1234567890123456789ns",
                "wcet '1234567890123456789ns' has 19 digits: a budget is at most 18 digits of its unit "
                "(999999999999999999); write it in a coarser unit",
            ),
            ("400", "wcet '400' names no unit: write it with one of ns, us, ms, s, e.g. '400us'"),
            ("850µs", "wcet '850µs' uses 'µs': write '850us'"),
            ("850\u03bcs", "wcet '850\u03bcs' uses '\u03bcs': write '850us'"),
            ("850xs", "wcet '850xs' has an unknown unit 'xs': write one of ns, us, ms, s"),
            ("-5us", "wcet '-5us' is not a whole number followed by one of ns, us, ms, s"),
            ("", "wcet '' is not a whole number followed by one of ns, us, ms, s"),
        ],
    )
    def test_every_row_says_its_own_sentence(self, value: object, sentence: str) -> None:
        with pytest.raises(BudgetFormatError) as caught:
            parse_budget(value)
        assert str(caught.value) == sentence

    def test_the_field_name_is_the_callers(self) -> None:
        with pytest.raises(BudgetFormatError, match=r"^budget '400' names no unit"):
            parse_budget("400", field="budget")

    def test_a_space_that_hides_another_fault_takes_the_last_sentence(self) -> None:
        with pytest.raises(BudgetFormatError) as caught:
            parse_budget("1.5 ms")
        assert str(caught.value) == "wcet '1.5 ms' is not a whole number followed by one of ns, us, ms, s"

    def test_a_negative_int_is_not_offered_a_negative_spelling(self) -> None:
        with pytest.raises(BudgetFormatError) as caught:
            parse_budget(-5)
        assert "e.g. '400us'" in str(caught.value)

    def test_an_over_long_value_is_refused_without_a_conversion_error(self) -> None:
        # Past CPython's 4 300-digit int/str limit: refused by a sentence, never a crash.
        for text in ("9" * 5000 + "us", "0" + "9" * 5000 + "us", "9" * 5000, "1." + "5" * 5000 + "ms"):
            with pytest.raises(BudgetFormatError):
                parse_budget(text)

    def test_zero_parses_because_positivity_is_the_fields_rule(self) -> None:
        assert parse_budget("0ns").factor == 0


class TestSeconds:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("1s", Fraction(1)),
            ("1ms", Fraction(1, 1000)),
            ("1us", Fraction(1, 10**6)),
            ("1ns", Fraction(1, 10**9)),
            ("1500us", Fraction(3, 2000)),
            ("850ns", Fraction(85, 10**8)),
        ],
    )
    def test_exact_for_every_unit(self, text: str, seconds: Fraction) -> None:
        assert parse_budget(text).seconds == seconds

    def test_two_spellings_of_one_duration_are_equal_in_seconds(self) -> None:
        assert parse_budget("1500us").seconds == parse_budget("1500000ns").seconds

    @pytest.mark.parametrize(("unit", "code"), [("us", 0), ("ms", 3), ("s", 6), ("ns", 26)])
    def test_the_cse_code_of_each_unit_is_table_4_92s(self, unit: str, code: int) -> None:
        assert Budget(1, unit).cse_code == code


class TestFromCse:
    @pytest.mark.parametrize("code", sorted(TABLE_4_92))
    def test_every_time_domain_code_has_an_exact_case(self, code: int) -> None:
        # A factor of 10**6 is a whole number of ns for every code, sub-ns ones included.
        factor = 10**6
        budget = from_cse(code, factor)
        assert budget.seconds == factor * TABLE_4_92[code]

    @pytest.mark.parametrize(("code", "unit"), [(0, "us"), (3, "ms"), (6, "s"), (26, "ns")])
    def test_a_units_own_code_keeps_its_factor_unchanged(self, code: int, unit: str) -> None:
        assert from_cse(code, 1000) == Budget(1000, unit)

    @pytest.mark.parametrize(
        ("code", "factor", "expected"),
        [
            (27, 85, Budget(850, "ns")),
            (8, 2, Budget(120, "s")),
            (10, 12, Budget(1036800, "s")),
            (1, 7, Budget(70, "us")),
            (5, 3, Budget(300, "ms")),
            (25, 20, Budget(2, "ns")),
        ],
    )
    def test_another_code_normalises_to_the_coarsest_exact_unit(self, code: int, factor: int, expected: Budget) -> None:
        assert from_cse(code, factor) == expected

    @pytest.mark.parametrize(
        ("code", "factor", "expected"),
        [
            (20, 10**6, Budget(1, "ns")),
            (21, 10**5, Budget(1, "ns")),
            (22, 10**4, Budget(1, "ns")),
            (23, 1000, Budget(1, "ns")),
            (24, 100, Budget(1, "ns")),
            (25, 10, Budget(1, "ns")),
        ],
    )
    def test_a_sub_nanosecond_code_divisible_into_ns(self, code: int, factor: int, expected: Budget) -> None:
        assert from_cse(code, factor) == expected

    @pytest.mark.parametrize(("code", "factor"), [(20, 1500), (21, 99), (22, 9), (23, 1500), (24, 1), (25, 3)])
    def test_a_sub_nanosecond_code_not_divisible_is_refused(self, code: int, factor: int) -> None:
        with pytest.raises(CseNotABudget, match="not a whole number of ns"):
            from_cse(code, factor)

    def test_an_angle_domain_code_is_refused(self) -> None:
        with pytest.raises(CseNotABudget, match="angle"):
            from_cse(100, 1)

    def test_an_other_domain_code_is_refused(self) -> None:
        with pytest.raises(CseNotABudget, match="no time scale"):
            from_cse(997, 1)

    def test_a_code_outside_the_table_is_refused(self) -> None:
        with pytest.raises(CseNotABudget, match="not a code of the Time Domain"):
            from_cse(11, 1)

    @pytest.mark.parametrize("factor", [0, -1])
    def test_a_factor_that_is_not_positive_is_refused(self, factor: int) -> None:
        with pytest.raises(CseNotABudget, match="not positive"):
            from_cse(0, factor)

    def test_a_normalisation_past_the_cap_is_refused(self) -> None:
        # 10 s x 10**17 is 10**18 s: nineteen digits.
        with pytest.raises(CseOverTheCap, match="more than 18 digits"):
            from_cse(7, 10**17)
        assert from_cse(7, 10**17 - 1).factor == 10**18 - 10

    def test_an_own_code_factor_past_the_cap_is_refused_without_a_conversion_error(self) -> None:
        with pytest.raises(CseOverTheCap, match="more than 18 digits"):
            from_cse(0, 10**5000)

    def test_only_the_cap_is_its_own_class(self) -> None:
        """An importer tells *larger than a budget holds* from *no unit holds it* by the class, never the text."""
        assert issubclass(CseOverTheCap, CseNotABudget)
        for code, factor in ((23, 1), (100, 1), (997, 1), (1, 0), (12345, 1)):
            with pytest.raises(CseNotABudget) as caught:
                from_cse(code, factor)
            assert not isinstance(caught.value, CseOverTheCap)


class TestFinestAndFormatExact:
    def test_finest(self) -> None:
        assert finest(["ms", "s"]) == "ms"
        assert finest(["us", "ns", "s"]) == "ns"
        assert finest(["s"]) == "s"

    def test_format_exact(self) -> None:
        assert format_exact(Fraction(3, 2000), "us") == "1500us"
        assert format_exact(Fraction(0), "ms") == "0ms"
        assert format_exact(Fraction(2), "s") == "2s"

    def test_format_exact_refuses_an_inexact_value(self) -> None:
        with pytest.raises(ValueError, match="not a whole number of ms"):
            format_exact(Fraction(3, 2000), "ms")


class TestDependencyDirection:
    """The module is a leaf, on ``identifier.py``'s and ``safety.py``'s precedent."""

    def test_imports_nothing_from_swcraft_tools_at_all(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
        offenders = sorted(m for m in modules if m.split(".")[0] == "swcraft_tools")
        assert offenders == [], f"execution_budget.py must stay a leaf module; found {offenders}"

    def test_uses_no_relative_imports_either(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        relative = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]
        assert relative == [], "execution_budget.py must stay a leaf module; found a relative import"
