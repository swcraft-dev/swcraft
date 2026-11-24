"""`safety.py` — the ASIL values, their comparison order, and its boundary.

The module holds **one fact**: the values a project may declare are ordered by their ``X``,
``QM < A < B < C < D``, with the decomposed spellings ``X(Y)`` of ``[TPS_SAFEX_00201]`` ranking with their ``X``.
It is neither the contract's ``enum`` (a value set) nor ``schema.py``'s ``Literal`` (a refusal): ISO 26262-1:2018
3.6 Note 1 says QM is not an ASIL at all, so the order is a rule mechanism.

Four properties get their own coverage:

* **``max_asil`` answers ``"QM"`` over an empty iterable**, so a component whose allocated requirements declare no
  level reports nothing;
* **``max_asil``'s tie rule reads the values, never the input order**: the validator's effective level and the
  editing guard's must agree about ``(Y)`` as well as ``X``;
* **``asil_rank`` and ``parse_asil`` raise on a value outside the eighteen** (``A(A)`` included): the loader has
  already refused one, so reaching it is a defect, and a total function would silently rank a typo;
* **the module is a leaf**: the validator and the editing guard both read it, so any ``swcraft_tools`` import here
  is a potential cycle.

**What this suite does not reach:** that the values are the ones the model declares (pinned against the contract
elsewhere), and that they are the standard's eighteen (a transcription, checked by reading the text the comment
beside :data:`ASIL_VALUES` cites).
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest
from swcraft_tools import safety as safety_module
from swcraft_tools.safety import ASIL_LEVELS, ASIL_VALUES, NO_ASIL, asil_rank, max_asil, parse_asil

MODULE_PATH = Path(safety_module.__file__)


class TestTheOrder:
    """``ASIL_LEVELS`` is the order, spelled in ISO 26262's own capitals."""

    def test_it_is_the_five_levels_in_increasing_order(self) -> None:
        assert ASIL_LEVELS == ("QM", "A", "B", "C", "D")

    def test_the_ranks_are_strictly_increasing(self) -> None:
        ranks = [asil_rank(level) for level in ASIL_LEVELS]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)

    def test_no_asil_is_the_bottom_of_the_order(self) -> None:
        # Derived, never spelled: the published name is the order's own first
        # member, so a reordering cannot leave the two disagreeing.
        assert NO_ASIL == ASIL_LEVELS[0] == "QM"
        assert asil_rank(NO_ASIL) == 0

    def test_qm_is_rank_zero(self) -> None:
        # The bottom of the order, and the value `max_asil` answers over an
        # empty iterable — the two facts have to agree or "nothing is below QM"
        # stops being true.
        assert asil_rank("QM") == 0

    @pytest.mark.parametrize(
        ("level", "rank"),
        [("QM", 0), ("A", 1), ("B", 2), ("C", 3), ("D", 4)],
    )
    def test_each_level_ranks_where_the_order_puts_it(self, level: str, rank: int) -> None:
        assert asil_rank(level) == rank


#: The thirteen decomposed spellings with the ``(X, Y)`` each splits into, in
#: the standard's listing order (``[TPS_SAFEX_00201]``, R21-11 p. 18).
_DECOMPOSED = [
    ("QM(A)", "QM", "A"),
    ("QM(B)", "QM", "B"),
    ("QM(C)", "QM", "C"),
    ("QM(D)", "QM", "D"),
    ("A(B)", "A", "B"),
    ("A(C)", "A", "C"),
    ("A(D)", "A", "D"),
    ("B(B)", "B", "B"),
    ("B(C)", "B", "C"),
    ("B(D)", "B", "D"),
    ("C(C)", "C", "C"),
    ("C(D)", "C", "D"),
    ("D(D)", "D", "D"),
]

#: Spellings that are not values: ``A(A)`` is the one ``X(Y)`` form with
#: ``X <= Y`` the standard's list leaves out; the rest are an out-of-range
#: ``Y``, the wrong case, a ``Y`` that is not an ASIL, a space, and the plain
#: misspellings the order has always refused.
_NOT_VALUES = ["A(A)", "A(E)", "a(b)", "QM(QM)", "B (D)", "D(C)", "ASIL_D", "d", "qm", "E", "", "ASIL D", "B()"]


class TestTheValues:
    """``ASIL_VALUES`` is the standard's eighteen, transcribed in its order."""

    def test_it_is_the_eighteen_in_the_standards_listing_order(self) -> None:
        assert ASIL_VALUES == ("QM", "A", "B", "C", "D", *(value for value, _, _ in _DECOMPOSED))
        assert len(ASIL_VALUES) == len(set(ASIL_VALUES)) == 18

    def test_the_five_plain_levels_open_it(self) -> None:
        # The capability set is the rated set's first five, in the same order.
        assert ASIL_VALUES[:5] == ASIL_LEVELS

    def test_the_form_the_standard_leaves_out_is_not_a_member(self) -> None:
        # B(B), C(C) and D(D) are listed and A(A) is not; the comment beside the
        # tuple names it so it is not added back as an oversight.
        assert {"B(B)", "C(C)", "D(D)"} <= set(ASIL_VALUES)
        assert "A(A)" not in ASIL_VALUES


class TestTheSplit:
    @pytest.mark.parametrize("level", ["QM", "A", "B", "C", "D"])
    def test_a_plain_level_has_no_context(self, level: str) -> None:
        assert parse_asil(level) == (level, None)

    @pytest.mark.parametrize(("value", "level", "context"), _DECOMPOSED)
    def test_a_decomposed_value_splits_into_x_and_y(self, value: str, level: str, context: str) -> None:
        assert parse_asil(value) == (level, context)

    def test_every_value_splits_into_two_plain_levels_with_x_not_above_y(self) -> None:
        for value in ASIL_VALUES:
            level, context = parse_asil(value)
            assert level in ASIL_LEVELS
            if context is not None:
                assert context in ASIL_LEVELS
                assert ASIL_LEVELS.index(level) <= ASIL_LEVELS.index(context)

    @pytest.mark.parametrize("value", _NOT_VALUES)
    def test_it_raises_on_a_non_value(self, value: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            parse_asil(value)
        assert repr(value) in str(excinfo.value)


class TestTheOrderIsByX:
    """``(Y)`` never moves a rank: a decomposed value ranks with its ``X``."""

    @pytest.mark.parametrize(("value", "level", "context"), _DECOMPOSED)
    def test_a_decomposed_value_ranks_with_its_x(self, value: str, level: str, context: str) -> None:
        assert asil_rank(value) == asil_rank(level)

    def test_b_and_b_d_rank_alike(self) -> None:
        assert asil_rank("B(D)") == asil_rank("B") == 2

    def test_qm_d_ranks_with_qm(self) -> None:
        # QM(D) declares no ASIL, like QM: nothing is below it.
        assert asil_rank("QM(D)") == asil_rank(NO_ASIL) == 0

    def test_a_d_is_below_b(self) -> None:
        # The context D does not lift A(D) to or above B.
        assert asil_rank("A(D)") < asil_rank("B")


class TestAValueOutsideTheOrderRaises:
    """A defect, not an input: the loader refuses such a value before this."""

    @pytest.mark.parametrize("level", _NOT_VALUES)
    def test_it_raises_rather_than_ranking_it(self, level: str) -> None:
        with pytest.raises(ValueError):
            asil_rank(level)

    @pytest.mark.parametrize("level", _NOT_VALUES)
    def test_the_maximum_raises_rather_than_answering_beside_it(self, level: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            max_asil(["B", level])
        assert repr(level) in str(excinfo.value)

    def test_the_message_names_the_value_and_the_order(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            asil_rank("ASIL_D")
        message = str(excinfo.value)
        assert "ASIL_D" in message
        assert "QM" in message and "D" in message


class TestTheMaximum:
    def test_an_empty_iterable_is_qm(self) -> None:
        # The load-bearing case: a component whose allocated requirements
        # declare no level has no maximum to be below, and this is where that
        # is decided once.
        assert max_asil(()) == "QM"

    def test_it_answers_the_highest_regardless_of_order(self) -> None:
        assert max_asil(("A", "D", "B")) == "D"

    def test_it_is_not_the_lexicographic_maximum(self) -> None:
        # `max()` over the raw strings answers "QM" here, because "Q" sorts
        # above "D". The whole point of the rank is that it does not.
        assert max_asil(("QM", "D")) == "D"

    def test_a_single_level_is_itself(self) -> None:
        assert max_asil(("B",)) == "B"

    def test_it_accepts_a_generator_and_not_only_a_sequence(self) -> None:
        assert max_asil(level for level in ("C", "A")) == "C"

    def test_a_value_outside_the_order_raises_here_too(self) -> None:
        with pytest.raises(ValueError):
            max_asil(("A", "ASIL_D"))

    def test_a_value_the_standard_leaves_out_raises_here_too(self) -> None:
        with pytest.raises(ValueError):
            max_asil(("A", "A(A)"))


class TestTheMaximumCarriesY:
    """The highest ``X`` wins and keeps its ``(Y)``; ties decided by the values."""

    def test_the_winners_context_is_carried(self) -> None:
        assert max_asil(("A", "B(D)", "QM")) == "B(D)"

    def test_a_higher_x_beats_a_higher_context(self) -> None:
        # C outranks B(D): the comparison is by X, the context is not a level.
        assert max_asil(("B(D)", "C")) == "C"
        assert max_asil(("A(D)", "B")) == "B"

    @pytest.mark.parametrize("values", [("B", "B(D)"), ("B(D)", "B")])
    def test_the_plain_spelling_wins_a_tie_on_x(self, values: tuple[str, str]) -> None:
        assert max_asil(values) == "B"

    @pytest.mark.parametrize("values", [("B(C)", "B(D)"), ("B(D)", "B(C)")])
    def test_the_higher_context_wins_a_tie_between_decomposed(self, values: tuple[str, str]) -> None:
        assert max_asil(values) == "B(D)"

    def test_qm_contexts_tie_on_the_higher_context_and_lose_to_qm(self) -> None:
        assert max_asil(("QM(A)", "QM(D)")) == "QM(D)"
        assert max_asil(("QM(D)", "QM")) == "QM"

    def test_the_answer_is_independent_of_the_input_order(self) -> None:
        values = ("B(C)", "A(D)", "B(D)", "QM(D)", "B(B)")
        answers = {max_asil(permutation) for permutation in itertools.permutations(values)}
        assert answers == {"B(D)"}


class TestTheModuleHoldsTheOrderAndNothingElse:
    def test_it_publishes_no_is_below(self) -> None:
        # `is_below` would have exactly one caller, and
        # `asil_rank(declared) < asil_rank(maximum)` says the same thing at the
        # site that means it.
        assert not hasattr(safety_module, "is_below")

    def test_it_defines_exactly_the_six_public_names(self) -> None:
        # Read off the module's own source rather than `vars()`, which would
        # also list what the module imports. The claim is about what this
        # module DEFINES: the values, the order, its bottom, and the functions over them.
        # `NO_ASIL` is no second fact: it is `ASIL_LEVELS[0]`, published so that
        # the validator and `editing/safety_guard.py` hold no private copy.
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        defined: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                defined.append(node.name)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.append(node.target.id)
            elif isinstance(node, ast.Assign):
                defined.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        assert sorted(name for name in defined if not name.startswith("_")) == [
            "ASIL_LEVELS",
            "ASIL_VALUES",
            "NO_ASIL",
            "asil_rank",
            "max_asil",
            "parse_asil",
        ]


class TestDependencyDirection:
    """The module is a leaf: ``validator/`` and ``editing/`` both read the order, so it sits below both."""

    def _imported_modules(self) -> set[str]:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
        return modules

    def test_imports_nothing_from_swcraft_tools_at_all(self) -> None:
        offenders = sorted(m for m in self._imported_modules() if m.split(".")[0] == "swcraft_tools")
        assert offenders == [], f"safety.py must stay a leaf module; found {offenders}"

    def test_uses_no_relative_imports_either(self) -> None:
        # A relative import has no module prefix to catch above, so it is
        # checked separately rather than being invisible to the leaf guard.
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        relative = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]
        assert relative == [], "safety.py must stay a leaf module; found a relative import"
