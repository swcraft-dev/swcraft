"""The explicit presence or class check that stands where another rule guarantees a value."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from swcraft_tools import guarantee
from swcraft_tools.guarantee import BrokenGuarantee, required, required_instance


def test_a_present_value_is_returned_unchanged() -> None:
    value = ["kept"]
    assert required(value, "the builder sets it") is value


@pytest.mark.parametrize("value", ["", 0, 0.0, False, [], {}, ()])
def test_a_falsy_value_is_present_and_returned(value: object) -> None:
    assert required(value, "the builder sets it") is value


def test_none_raises_the_broken_guarantee_with_its_sentence() -> None:
    with pytest.raises(BrokenGuarantee) as raised:
        required(None, "the builder sets it")
    assert raised.value.args == ("the builder sets it",)


def test_an_instance_of_the_kind_is_returned_unchanged() -> None:
    value = True
    assert required_instance(value, int, "a bool is an int") is value


def test_another_class_raises_the_broken_guarantee_with_its_sentence() -> None:
    with pytest.raises(BrokenGuarantee) as raised:
        required_instance("text", int, "the mapper keeps numbers only")
    assert raised.value.args == ("the mapper keeps numbers only",)


@pytest.mark.parametrize("value", [3, 2.5, True])
def test_an_instance_of_one_of_the_kinds_is_returned_unchanged(value: object) -> None:
    assert required_instance(value, (int, float), "a number") is value


def test_an_instance_of_none_of_the_kinds_raises_the_broken_guarantee() -> None:
    with pytest.raises(BrokenGuarantee) as raised:
        required_instance("3", (int, float), "the walk refused a string")
    assert raised.value.args == ("the walk refused a string",)


def test_none_is_not_an_instance_of_a_kind() -> None:
    with pytest.raises(BrokenGuarantee):
        required_instance(None, str, "the builder sets it")


def test_a_broken_guarantee_is_an_internal_error() -> None:
    assert issubclass(BrokenGuarantee, RuntimeError)
    assert not issubclass(BrokenGuarantee, (ValueError, OSError))


def test_the_leaf_imports_nothing_from_swcraft_tools() -> None:
    tree = ast.parse(Path(guarantee.__file__).read_text(encoding="utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert not any(name.startswith("swcraft_tools") for name in imported)
    assert [node.level for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level] == []
