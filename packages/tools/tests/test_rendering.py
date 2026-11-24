"""`rendering.render_model_scalar` — the one place a model value becomes message text.

CPython bounds ``int.__repr__`` by ``sys.get_int_max_str_digits()`` but exempts
``int(s, 16)`` / ``int(s, 2)`` from it, so a hand-authored ``init: 0x…`` loads as an
integer that cannot be printed, and a message describing it would crash.

An ordinary value and a wide one take **different** branches and each is asserted on
its own; every container case is a pair, because printable containers alone pass
against a renderer with no container path at all.

**What this suite does not reach.** It pins the message policy only. That a
production site calls this function is pinned where that site lives —
`test_validator_init.py`'s ``TestAValueTooWideToRenderIsReportedRatherThanAborting``
and ``TestAnUnwritableIntegerIsAFinding``, `test_differ.py`'s
``test_an_integer_too_wide_to_render_is_described_instead_of_aborting_the_diff``
and ``test_a_composite_identity_key_renders_through_the_same_policy``,
`test_editing_delta.py`'s ``TestAValueTooWideToRenderIsDescribedInTheSummary``,
`test_editing_validation.py`'s ``TestAnAddressRendersAModelValueThroughTheOnePolicy``
and `test_component_import.py`'s
``TestAWideTargetInitIsDescribedRatherThanCrashingTheConflict`` — and that the
*serializers* still raise is asserted in the differ case. **Nothing anywhere sees a
message site that does not call the function at all**: no gate exists (`rendering.py`'s
docstring says why an `ast` scan was rejected). This list records what is pinned,
never a claim that it is everything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import DIGIT_LIMIT_FLOOR, int_max_str_digits
from swcraft_tools import rendering
from swcraft_tools.rendering import render_model_scalar

MODULE_PATH = Path(rendering.__file__)

#: The widest run of hexadecimal ``f`` whose DECIMAL form still fits in
#: ``DIGIT_LIMIT_FLOOR`` digits, found by measurement rather than by the
#: function's own arithmetic. Hex is denser than decimal, so a guard that
#: (wrongly) bounded the SOURCE width would differ only at this boundary.
_WIDEST_HEX_RUN_THAT_PRINTS = next(
    n for n in range(1, DIGIT_LIMIT_FLOOR + 2) if len(str(int("f" * (n + 1), 16))) > DIGIT_LIMIT_FLOOR
)


class TestAnOrdinaryValueIsRepr:
    """An ordinary value renders as its ``repr``, which the messages' quoting depends on."""

    @pytest.mark.parametrize(
        "value",
        [7, -7, 0, 1.5, float("-0.0"), "abc", "", True, False, None, [1, 2], {"a": 1}, (1, 2)],
    )
    def test_it_matches_repr_exactly(self, value: object) -> None:
        assert render_model_scalar(value) == repr(value)


#: A value ``repr`` refuses to print, and the description it is replaced by.
#: 4 000 hexadecimal digits is 16 000 bits, well over the 4 300-digit default.
_WIDE = int("f" * 4000, 16)
_WIDE_RENDERED = "<an integer of 16000 bits, too wide to render>"


class TestAContainerCarryingSuchAValueIsDescribedInPlace:
    """A ``dict`` or ``list`` ``init`` holding a too-wide value is described member by member.

    ``init`` is recursive (`schema.py`'s ``InitValue``) and `differ.py` passes the whole
    changed field value, so ``repr`` of the container would raise. Each case is a pair of
    an ordinary and a wide-bearing container of the same shape, taking different branches.
    """

    @pytest.mark.parametrize(
        ("ordinary", "carrying", "expected"),
        [
            pytest.param([1, 2], [1, _WIDE], f"[1, {_WIDE_RENDERED}]", id="list"),
            pytest.param([1], [_WIDE], f"[{_WIDE_RENDERED}]", id="single-item-list"),
            pytest.param({"load": 1}, {"load": _WIDE}, "{'load': " + _WIDE_RENDERED + "}", id="record"),
            pytest.param(
                {"a": 1, "b": 2},
                {"a": 1, "b": _WIDE},
                "{'a': 1, 'b': " + _WIDE_RENDERED + "}",
                id="record-second-field",
            ),
            pytest.param({1: "a"}, {_WIDE: "a"}, "{" + _WIDE_RENDERED + ": 'a'}", id="record-key"),
            pytest.param(
                {"outer": [{"inner": 1}]},
                {"outer": [{"inner": _WIDE}]},
                "{'outer': [{'inner': " + _WIDE_RENDERED + "}]}",
                id="three-levels-deep",
            ),
            pytest.param([[1], [2]], [[1], [_WIDE]], f"[[1], [{_WIDE_RENDERED}]]", id="list-of-lists"),
        ],
    )
    def test_the_ordinary_container_is_repr_and_the_wide_one_is_described(
        self, ordinary: object, carrying: object, expected: str
    ) -> None:
        assert render_model_scalar(ordinary) == repr(ordinary)
        assert render_model_scalar(carrying) == expected

    @pytest.mark.parametrize(
        "carrying",
        [
            pytest.param([_WIDE], id="list"),
            pytest.param({"load": _WIDE}, id="record"),
            pytest.param({_WIDE: "a"}, id="record-key"),
            pytest.param({"outer": [{"inner": _WIDE}]}, id="three-levels-deep"),
        ],
    )
    def test_the_same_containers_are_unprintable_by_the_ordinary_route(self, carrying: object) -> None:
        """Non-vacuity control: each ``carrying`` container is unprintable by ``repr``."""
        with pytest.raises(ValueError, match="Exceeds the limit"):
            repr(carrying)


class TestAnUnrenderableValueOfAnyOtherShapeStillRaises:
    """The rebuild covers only ``init`` shapes; any other ``ValueError`` is re-raised, not guessed at."""

    def test_an_object_whose_repr_raises_is_not_described(self) -> None:
        class Unrenderable:
            def __repr__(self) -> str:
                raise ValueError("raised for a reason of its own")

        with pytest.raises(ValueError, match="a reason of its own"):
            render_model_scalar(Unrenderable())

    def test_a_set_carrying_a_wide_value_is_not_rebuilt_either(self) -> None:
        """A ``set`` is not an ``InitValue`` shape and is deliberately not rebuilt."""
        with pytest.raises(ValueError, match="Exceeds the limit"):
            render_model_scalar({_WIDE})

    def test_a_tuple_carrying_a_wide_value_is_not_rebuilt_either(self) -> None:
        """A ``tuple`` is not rebuilt: no caller produces one (``model_dump()`` spells sequences as lists)."""
        with pytest.raises(ValueError, match="Exceeds the limit"):
            render_model_scalar((_WIDE,))


class TestAValueTooWideToPrintIsDescribed:
    """The guarded branch, and the boundary it sits on."""

    def test_a_value_over_the_limit_is_described_by_its_bit_length(self) -> None:
        value = int("f" * 4000, 16)

        assert render_model_scalar(value) == "<an integer of 16000 bits, too wide to render>"

    def test_the_same_value_is_unprintable_by_the_ordinary_route(self) -> None:
        """Non-vacuity control: the value above is unprintable by ``repr``."""
        with pytest.raises(ValueError, match="Exceeds the limit"):
            repr(int("f" * 4000, 16))

    def test_the_widest_printable_value_is_still_printed(self) -> None:
        """A value of exactly the limit's decimal digits still prints (lower half of an adjacent pair)."""
        digits = _WIDEST_HEX_RUN_THAT_PRINTS
        value = int("f" * digits, 16)
        assert len(str(value)) == DIGIT_LIMIT_FLOOR

        with int_max_str_digits(DIGIT_LIMIT_FLOOR):
            rendered = render_model_scalar(value)

        assert rendered == str(value)
        assert "too wide" not in rendered

    def test_one_hexadecimal_digit_wider_is_described_instead(self) -> None:
        """One hex digit wider is described (upper half), so an off-by-one fails one of the two."""
        value = int("f" * (_WIDEST_HEX_RUN_THAT_PRINTS + 1), 16)
        assert len(str(value)) == DIGIT_LIMIT_FLOOR + 1

        with int_max_str_digits(DIGIT_LIMIT_FLOOR):
            rendered = render_model_scalar(value)

        assert rendered == f"<an integer of {value.bit_length()} bits, too wide to render>"

    def test_a_disabled_limit_prints_a_value_of_any_width(self) -> None:
        """A limit of ``0`` (no limit) substitutes nothing: the function inherits it from ``repr``."""
        value = int("f" * 4000, 16)

        with int_max_str_digits(0):
            assert render_model_scalar(value) == str(value)


class TestTheModuleIsALeaf:
    """``rendering.py`` imports nothing of SWCraft.

    The ``validator`` package and ``differ.py`` both read it, so any import is a potential cycle.
    """

    def _imported_modules(self) -> set[str]:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
        return modules

    def test_it_imports_nothing_from_swcraft_tools(self) -> None:
        offenders = sorted(m for m in self._imported_modules() if m.split(".")[0] == "swcraft_tools")
        assert offenders == [], f"rendering.py must stay a leaf module; found {offenders}"

    def test_it_uses_no_relative_imports_either(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        relative = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]
        assert relative == [], "rendering.py must stay a leaf module; found a relative import"
