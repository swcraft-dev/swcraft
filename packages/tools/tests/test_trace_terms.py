"""The trace terms leaf: :func:`numbers`, :func:`words` and :func:`is_reported_term`.

The tokeniser's own tests are in ``test_trace_search_evidence.py``.
"""

from __future__ import annotations

import pytest
from swcraft_tools.trace_terms import (
    REPORTED_STOP_WORDS,
    STOP_WORDS,
    is_reported_term,
    numbers,
    tokens,
    words,
)


class TestNumbers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("limit 600", ["600"]),
            ("down to -40 degrees", ["-40"]),
            ("-40 is the floor", ["-40"]),
            ("a-40", ["40"]),
            ("ratio 1.5", ["1.5"]),
            ("within 10ms", ["10"]),
            ("SR2", []),
            ("SR2.5", ["5"]),
            ("1.5.3", ["1.5", "3"]),
            ("1,5", ["1", "5"]),
            ("", []),
            ("no digits here", []),
        ],
    )
    def test_the_cases(self, text: str, expected: list[str]) -> None:
        assert numbers(text) == expected

    def test_a_number_beside_a_unit_is_the_number_alone(self) -> None:
        assert numbers("from 600 °C to 650 °C") == ["600", "650"]

    def test_the_order_and_the_repeats_are_kept(self) -> None:
        assert numbers("7 then 3 then 7") == ["7", "3", "7"]

    def test_a_digit_or_underscore_before_disqualifies(self) -> None:
        assert numbers("x_12 and 12") == ["12"]


class TestWords:
    def test_nothing_is_dropped(self) -> None:
        assert words("The brake shall NOT be applied, a 2nd time.") == [
            "the", "brake", "shall", "not", "be", "applied", "a", "2", "nd", "time",
        ]  # fmt: skip

    def test_the_split_is_the_tokenisers(self) -> None:
        text = "BrakeTemp2Sensor shall limit SR600 within the 10ms window"
        assert [word for word in words(text) if word in tokens(text)] == tokens(text)
        assert words("SR600") == ["sr", "600"]


class TestIsReportedTerm:
    def test_the_reported_stop_words_are_stop_words(self) -> None:
        """Every other word is named anyway, so a reported word outside the stop words would say nothing."""
        assert REPORTED_STOP_WORDS <= STOP_WORDS

    @pytest.mark.parametrize("word", ["not", "no", "never", "without", "shall", "may", "must", "can", "will", "only"])
    def test_a_negation_a_modal_and_only_are_named(self, word: str) -> None:
        assert is_reported_term(word)

    @pytest.mark.parametrize("word", ["at", "for", "the", "and", "of", "be", "is", "s", "a"])
    def test_another_stop_word_and_a_single_letter_are_not(self, word: str) -> None:
        assert not is_reported_term(word)

    @pytest.mark.parametrize("word", ["most", "least", "brake", "sr", "2", "600"])
    def test_any_other_word_and_a_digit_are_named(self, word: str) -> None:
        assert is_reported_term(word)
