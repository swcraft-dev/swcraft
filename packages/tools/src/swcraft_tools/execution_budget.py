"""An execution-time budget as written: a whole number and one of ``ns``, ``us``, ``ms``, ``s``.

A runnable's and a task's budget is spelled ``wcet: 400us``: the value carries
its unit. This module owns the grammar of that text, the four units, the CSE code
each unit has in ARXML and the exact arithmetic over them; every consumer asks
:func:`parse_budget` for the duration.

**Why a whole number and these four units.** ARXML writes a duration as a
``MultidimensionalTime``: a ``cseCode`` naming a scale and a
``cseCodeFactor``, an ``Integer`` (TPS GST R4.3.0 Table 4.90; R4.2.2 Table
4.69). The four units are exactly the scales of 1 of their unit in the Time
Domain table, so a budget is written as its integer and its unit's code with
no conversion at all (:data:`CSE_CODE`). A decimal such as ``1.5ms`` has no
``cseCodeFactor`` and is refused with its exact spelling in a finer unit.

**The factor cap.** A budget's integer has at most :data:`MAX_FACTOR_DIGITS`
digits — about 31 years in ``ns`` — far below CPython's 4 300-digit
``int``/``str`` conversion limit. The cap applies at authoring and on import
after :func:`from_cse` normalised a coarse code's factor. What the cap does not
reach: a downstream tool's own integer width (an 18-digit factor exceeds 32
bits and fits 64), which nothing here checks.

**What this module is not.** It knows no layer, kind or field: it is handed a
value and the field name to put in a sentence. Positivity is the field's rule,
not the grammar's (``0us`` parses), and so is the pairing of ``wcet`` with
``wcet_type``. It imports nothing from ``swcraft_tools``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, NoReturn

#: The four units a budget may be written in, coarsest first.
UNITS: Final = ("s", "ms", "us", "ns")

#: The ``cseCode`` of 1 of each unit: TPS GST R4.3.0 Table 4.92 *CseCodeType in
#: Time Domain* (``0`` = 1 µsec, ``3`` = 1 msec, ``6`` = 1 sec, ``26`` = 1 ns);
#: in R4.2.2 the same list is the body of ``[TPS_GST_00354]``. The R4.2.2 TPS
#: example's code ``2`` is 100 µsec, not 1 µsec — the known trap.
CSE_CODE: Final = {"us": 0, "ms": 3, "s": 6, "ns": 26}
#: :data:`CSE_CODE` reversed: the unit a code of one of the four names.
_UNIT_OF_CSE_CODE: Final = {code: unit for unit, code in CSE_CODE.items()}

#: Every Time Domain code of TPS GST R4.3.0 Table 4.92 (R4.2.2
#: ``[TPS_GST_00354]``) and the length of 1 of it, in seconds.
TIME_DOMAIN_SCALE: Final = {
    0: Fraction(1, 10**6),
    1: Fraction(10, 10**6),
    2: Fraction(100, 10**6),
    3: Fraction(1, 10**3),
    4: Fraction(10, 10**3),
    5: Fraction(100, 10**3),
    6: Fraction(1),
    7: Fraction(10),
    8: Fraction(60),
    9: Fraction(3600),
    10: Fraction(86400),
    20: Fraction(1, 10**15),
    21: Fraction(10, 10**15),
    22: Fraction(100, 10**15),
    23: Fraction(1, 10**12),
    24: Fraction(10, 10**12),
    25: Fraction(100, 10**12),
    26: Fraction(1, 10**9),
    27: Fraction(10, 10**9),
    28: Fraction(100, 10**9),
}

#: The exact length of one of each unit, in seconds: its code's Time Domain scale.
SECONDS_PER_UNIT: Final = {unit: TIME_DOMAIN_SCALE[CSE_CODE[unit]] for unit in UNITS}

#: The Angle Domain (Table 4.93) and Other Domain (Table 4.94) codes: a
#: ``MultidimensionalTime`` in them is no duration, so no budget.
ANGLE_DOMAIN_CODES: Final = frozenset({100, 101, 102})
OTHER_DOMAIN_CODES: Final = frozenset({997, 998, 999, 1000})

#: The authoring cap; also the import cap after :func:`from_cse` normalised.
MAX_FACTOR_DIGITS: Final = 18

#: :data:`UNITS` finest first, as every budget refusal names them.
UNIT_LIST: Final = ", ".join(reversed(UNITS))
_WHOLE = re.compile(r"(?P<digits>[0-9]+)(?P<unit>ns|us|ms|s)")
_DECIMAL = re.compile(r"(?P<whole>[0-9]+)\.(?P<fraction>[0-9]+)(?P<unit>ns|us|ms|s)")
_DIGITS = re.compile(r"[0-9]+")
_MICRO = re.compile(r"(?P<digits>[0-9]+)(?P<unit>[µμ]s)")
_LETTERS = re.compile(r"(?P<digits>[0-9]+)(?P<unit>[^\W\d_]+)")
_WHITESPACE = re.compile(r"\s")


@dataclass(frozen=True)
class Budget:
    """A parsed budget: its integer and its unit, exactly as written."""

    factor: int
    """``>= 0`` as parsed; positivity is the field's rule, not the grammar's."""
    unit: str
    """One of :data:`UNITS`."""

    @property
    def seconds(self) -> Fraction:
        """The exact duration in seconds."""
        return self.factor * SECONDS_PER_UNIT[self.unit]

    @property
    def cse_code(self) -> int:
        """The ``cseCode`` the writer emits beside :attr:`factor` (Table 4.92)."""
        return CSE_CODE[self.unit]

    @property
    def text(self) -> str:
        """The budget's one spelling, equal to the text it was parsed from."""
        return f"{self.factor}{self.unit}"


class BudgetFormatError(ValueError):
    """A value the grammar refuses; its ``str()`` is the refusal sentence."""


class CseNotABudget(ValueError):
    """A ``MultidimensionalTime`` no budget can hold; its ``str()`` is the reason."""


class CseOverTheCap(CseNotABudget):
    """A ``MultidimensionalTime`` whose integer has more than :data:`MAX_FACTOR_DIGITS` digits of its unit.

    Its own class, so an importer tells *larger than a budget holds* from *no unit holds it exactly*.
    """


def _coarsest_exact(seconds: Fraction) -> str | None:
    """The coarsest of :data:`UNITS` in which ``seconds`` is a whole number."""
    for unit in UNITS:
        if (seconds / SECONDS_PER_UNIT[unit]).denominator == 1:
            return unit
    return None


def _refuse_text(value: str, field: str) -> BudgetFormatError:
    """The sentence for a string the whole-number form did not accept."""
    decimal = _DECIMAL.fullmatch(value)
    # A decimal longer than twice the cap never reaches ``int()``; it takes the last sentence.
    if decimal is not None and len(decimal["whole"]) + len(decimal["fraction"]) <= 2 * MAX_FACTOR_DIGITS:
        unit = decimal["unit"]
        prefix = f"{field} '{value}' is not a whole number of {unit} (an ARXML cseCodeFactor is an Integer): "
        seconds = Fraction(int(decimal["whole"] + decimal["fraction"]), 10 ** len(decimal["fraction"]))
        seconds *= SECONDS_PER_UNIT[unit]
        exact = _coarsest_exact(seconds)
        if exact is None:
            return BudgetFormatError(prefix + "a time below 1 ns cannot be written: the finest unit is ns")
        return BudgetFormatError(prefix + f"write '{format_exact(seconds, exact)}'")
    if _WHITESPACE.search(value):
        compact = _WHITESPACE.sub("", value)
        whole = _WHOLE.fullmatch(compact)
        if whole is not None and _canonical_digits(whole["digits"]):
            return BudgetFormatError(f"{field} '{value}' has a space: write '{compact}'")
    if _DIGITS.fullmatch(value):
        return BudgetFormatError(
            f"{field} '{value}' names no unit: write it with one of {UNIT_LIST}, e.g. '{_no_leading_zero(value)}us'"
        )
    micro = _MICRO.fullmatch(value)
    if micro is not None:
        return BudgetFormatError(
            f"{field} '{value}' uses '{micro['unit']}': write '{_no_leading_zero(micro['digits'])}us'"
        )
    letters = _LETTERS.fullmatch(value)
    if letters is not None:
        return BudgetFormatError(f"{field} '{value}' has an unknown unit '{letters['unit']}': write one of {UNIT_LIST}")
    return BudgetFormatError(f"{field} '{value}' is not a whole number followed by one of {UNIT_LIST}")


def _no_leading_zero(digits: str) -> str:
    """``digits`` without its leading zeros, by text: never through ``int()``."""
    return digits.lstrip("0") or "0"


def _canonical_digits(digits: str) -> bool:
    """No leading zero (``0`` itself excepted) and at most the cap's digits."""
    return (digits == "0" or not digits.startswith("0")) and len(digits) <= MAX_FACTOR_DIGITS


def parse_budget(value: object, field: str = "wcet") -> Budget:
    """Parse a budget as written, or raise :class:`BudgetFormatError`.

    ``field`` is the name the sentence gives the value — the caller's, never a
    path. A string is accepted only in its one spelling: decimal digits with no
    leading zero and at most :data:`MAX_FACTOR_DIGITS` of them, then one of
    :data:`UNITS`, nothing around it.
    """

    def not_a_budget(example: str) -> NoReturn:
        _raise(f"{field} {value} is not a budget: write a whole number with one of {UNIT_LIST}, e.g. '{example}'")

    if isinstance(value, bool):
        not_a_budget("400us")
    if isinstance(value, int):
        if value < 0:
            not_a_budget("400us")
        _raise(f"{field} {value} names no unit: write it with one of {UNIT_LIST}, e.g. '{value}us'")
    if isinstance(value, float):
        not_a_budget("1500us")
    if not isinstance(value, str):
        _raise(f"{field} {value!r} is not a whole number followed by one of {UNIT_LIST}")
    whole = _WHOLE.fullmatch(value)
    if whole is None:
        raise _refuse_text(value, field)
    digits, unit = whole["digits"], whole["unit"]
    if digits != "0" and digits.startswith("0"):
        _raise(f"{field} '{value}' has a leading zero: write '{_no_leading_zero(digits)}{unit}'")
    if len(digits) > MAX_FACTOR_DIGITS:
        _raise(
            f"{field} '{value}' has {len(digits)} digits: a budget is at most {MAX_FACTOR_DIGITS} digits of its "
            f"unit ({'9' * MAX_FACTOR_DIGITS}); write it in a coarser unit"
        )
    return Budget(int(digits), unit)


def _raise(sentence: str) -> NoReturn:
    raise BudgetFormatError(sentence)


def finest(units: Iterable[str]) -> str:
    """The finest of ``units`` (each one of :data:`UNITS`); refuses an empty iterable."""
    return min(units, key=lambda unit: SECONDS_PER_UNIT[unit])


def format_exact(seconds: Fraction, unit: str) -> str:
    """``seconds`` written as a whole number of ``unit``; refuses an inexact one."""
    count = Fraction(seconds) / SECONDS_PER_UNIT[unit]
    if count.denominator != 1:
        raise ValueError(f"{seconds} s is not a whole number of {unit}")
    return f"{count.numerator}{unit}"


def from_cse(code: int, factor: int) -> Budget:
    """The budget an ARXML ``MultidimensionalTime`` states, or :class:`CseNotABudget`.

    A code of one of the four units keeps its factor unchanged (``1000`` µs stays
    ``1000us``). Another Time Domain code is normalised to the coarsest unit in
    which ``factor × scale`` is a whole number (``10 ns × 85`` is ``850ns``, ``1
    min × 2`` is ``120s``); only the sub-nanosecond codes ``20``–``25`` can have
    none. A code outside the Time Domain, a factor that is not positive, and a
    normalised integer over :data:`MAX_FACTOR_DIGITS` digits are refused, each
    with its own reason.
    """
    if code in ANGLE_DOMAIN_CODES:
        raise CseNotABudget(f"CSE code {code} is an angle, not a duration (TPS GST Table 4.93)")
    if code in OTHER_DOMAIN_CODES:
        raise CseNotABudget(f"CSE code {code} is no time scale (TPS GST Table 4.94)")
    if code not in TIME_DOMAIN_SCALE:
        raise CseNotABudget(f"CSE code {code} is not a code of the Time Domain (TPS GST Table 4.92)")
    if factor <= 0:
        raise CseNotABudget("the CSE code factor is not positive: a budget is a positive duration")
    if code in _UNIT_OF_CSE_CODE:
        budget = Budget(factor, _UNIT_OF_CSE_CODE[code])
    else:
        seconds = factor * TIME_DOMAIN_SCALE[code]
        unit = _coarsest_exact(seconds)
        if unit is None:
            raise CseNotABudget(
                f"CSE code {code} with its factor is not a whole number of ns: a time below 1 ns cannot be written"
            )
        budget = Budget(int(seconds / SECONDS_PER_UNIT[unit]), unit)
    # Compared as a number, never through ``str()``: an imported factor can be
    # longer than CPython's 4 300-digit conversion limit.
    if budget.factor >= 10**MAX_FACTOR_DIGITS:
        raise CseOverTheCap(
            f"CSE code {code} with its factor is more than {MAX_FACTOR_DIGITS} digits of {budget.unit}: "
            f"a budget is at most {MAX_FACTOR_DIGITS} digits of its unit"
        )
    return budget
