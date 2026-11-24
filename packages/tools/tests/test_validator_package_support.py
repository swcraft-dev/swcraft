"""``validator_package.reached_functions`` and ``tuple_loop_names`` read what they claim, over synthetic sources.

The binding tests assert ``bound in reached_functions(module, check)`` beside the binding's
exact ``(module, callable)`` pair; a helper that answered too much would turn every such
membership into a no-op, so its reach is pinned here in both directions.

**What these tests do not reach.** The real validator's call graph: these modules are
synthetic, so a validator call shape none of them spells is not covered here.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import ModuleType

import pytest
import validator_package

SOURCE = """
def check():
    phase_one()
    helpers.phase_two()
    run(phase_three)

    def nested():
        nested_only()

    return nested


async def phase_one():
    step()


def step():
    return None


def phase_two():
    return None


def phase_three():
    return None


def nested_only():
    return None


def unreached():
    step()
"""


def _module(tmp_path: Path, source: str) -> ModuleType:
    path = tmp_path / "synthetic.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8", newline="\n")
    module = ModuleType("synthetic")
    module.__file__ = str(path)
    return module


def test_a_bare_name_call_is_followed_transitively_and_through_an_async_def(tmp_path: Path) -> None:
    reached = validator_package.reached_functions(_module(tmp_path, SOURCE), "check")

    assert {"check", "phase_one", "step"} <= reached


def test_an_attribute_call_a_passed_value_and_an_uncalled_function_are_not_reached(tmp_path: Path) -> None:
    reached = validator_package.reached_functions(_module(tmp_path, SOURCE), "check")

    assert reached.isdisjoint({"phase_two", "phase_three", "unreached"})


def test_a_call_inside_a_nested_def_is_reached_though_it_may_never_run(tmp_path: Path) -> None:
    """The over-reach the docstring states: the walk does not tell a nested body from the function's own."""
    assert "nested_only" in validator_package.reached_functions(_module(tmp_path, SOURCE), "check")


def test_the_whole_answer_is_exactly_the_reached_set(tmp_path: Path) -> None:
    assert validator_package.reached_functions(_module(tmp_path, SOURCE), "check") == {
        "check",
        "phase_one",
        "step",
        "nested_only",
    }


def test_an_entry_the_module_does_not_define_fails_with_its_name(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="absent"):
        validator_package.reached_functions(_module(tmp_path, SOURCE), "absent")


LOOPS = """
def compose():
    for check in (first, second, helpers.third):
        check()
    run()


def run():
    return None


def two_loops():
    for check in (first,):
        check()
    again()


def again():
    for check in (second,):
        check()


def no_loop():
    for check in [first]:
        check()


def no_names():
    for value in (1, 2):
        value()
"""


def test_the_one_tuple_loops_names_are_answered_in_the_order_it_lists_them(tmp_path: Path) -> None:
    """An attribute element (``helpers.third``) is not a name and is left out."""
    assert validator_package.tuple_loop_names(_module(tmp_path, LOOPS), "compose") == ("first", "second")


def test_a_second_tuple_loop_in_a_reached_function_fails(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="reaches 2 loops over a tuple"):
        validator_package.tuple_loop_names(_module(tmp_path, LOOPS), "two_loops")


def test_no_loop_over_a_tuple_literal_fails(tmp_path: Path) -> None:
    """A ``for`` over a list literal is not the shape read."""
    with pytest.raises(AssertionError, match="reaches 0 loops over a tuple"):
        validator_package.tuple_loop_names(_module(tmp_path, LOOPS), "no_loop")


def test_a_tuple_holding_no_name_fails(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="names no check"):
        validator_package.tuple_loop_names(_module(tmp_path, LOOPS), "no_names")
