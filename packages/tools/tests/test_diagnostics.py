"""``swcraft_tools.diagnostics`` -- the leaf module that says *where* a finding is.

Every ``Subject`` of the ``validator/`` package and ``resource_analysis.py`` is built from its three constructors and
one combinator, and ``editing/diagnosis.py`` translates it into an address, so a defect here moves every rejection's
address at once. Four properties: the three step kinds are exactly those an address path admits (no ordinal one);
``at()`` extends and never mutates; a step value is unconstrained (legal members are ``schema.py``'s business); the
module is a standard-library leaf that does not import the protocol package it mirrors.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from swcraft_tools import diagnostics
from swcraft_tools.diagnostics import (
    FIELD,
    KEY,
    MEMBER,
    Subject,
    SubjectStep,
    field_step,
    key_step,
    member_step,
)

MODULE = Path(diagnostics.__file__)


class TestTheStepKinds:
    def test_there_are_exactly_three_and_none_is_positional(self) -> None:
        # An ordinal step would name a different thing the moment anything is inserted before it.
        assert {FIELD, MEMBER, KEY} == {"field", "member", "key"}
        assert "index" not in {FIELD, MEMBER, KEY}

    @pytest.mark.parametrize(
        ("make", "kind", "value"),
        [
            (field_step, FIELD, "memory"),
            (member_step, MEMBER, "pOut"),
            (key_step, KEY, "RAM"),
        ],
        ids=["field", "member", "key"],
    )
    def test_each_constructor_makes_its_own_kind(self, make, kind: str, value: object) -> None:
        assert make(value) == SubjectStep(kind, value)

    @pytest.mark.parametrize(
        "identity",
        [
            "pOut",
            {"swc": "ExampleSWC", "runnable": "ExampleRun"},
            {"type": "timing", "period": 0.01},
            "a link value",
        ],
        ids=["name", "composite", "whole-value", "scalar"],
    )
    def test_a_member_identity_is_not_constrained(self, identity: object) -> None:
        # Which values are legal members of which collection is `schema.py`'s
        # business; a step only says how the value names the member.
        assert member_step(identity).value == identity

    def test_a_step_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            field_step("memory").kind = KEY  # type: ignore[misc]


class TestTheSubject:
    def test_a_layer_alone_is_a_subject(self) -> None:
        subject = Subject("resources")
        assert (subject.element, subject.path) == (None, ())

    def test_at_extends_rather_than_mutates(self) -> None:
        element = Subject("swc", "ExampleSWC")
        nested = element.at(field_step("memory"), key_step("RAM"))

        assert nested == Subject("swc", "ExampleSWC", (field_step("memory"), key_step("RAM")))
        assert element.path == (), "at() must not mutate the subject it extends"
        assert (nested.layer, nested.element) == (element.layer, element.element)

    def test_at_is_chainable_and_appends_in_order(self) -> None:
        # Outermost step first: the order an address path is read in.
        chained = Subject("swc", "ExampleSWC").at(field_step("ports")).at(member_step("pOut"), field_step("interface"))
        assert chained.path == (field_step("ports"), member_step("pOut"), field_step("interface"))

    def test_at_with_no_steps_is_the_same_place(self) -> None:
        subject = Subject("tasks", "Task_10ms", (field_step("period"),))
        assert subject.at() == subject

    def test_two_subjects_naming_one_place_are_equal(self) -> None:
        # Equality lets a test compare a rule's subjects against a table instead of rendered prose.
        assert Subject("swc", "A", (field_step("f"),)) == Subject("swc", "A", (field_step("f"),))
        assert Subject("swc", "A") != Subject("swc", "B")

    def test_a_subject_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            Subject("swc", "A").element = "B"  # type: ignore[misc]


class TestTheJsonSpelling:
    """``to_json()``: the spelling ``swcraft check-model --format json`` prints.

    That it equals the protocol's ``Address.to_json`` is ``test_editing_validator_messages.py``'s agreement test:
    this module may not import the protocol.
    """

    @pytest.mark.parametrize(
        ("step", "expected"),
        [
            (field_step("ports"), {"field": "ports"}),
            (member_step("pOut"), {"member": "pOut"}),
            (member_step({"swc": "A", "runnable": "r"}), {"member": {"swc": "A", "runnable": "r"}}),
            (key_step("RAM0"), {"key": "RAM0"}),
        ],
        ids=["field", "member-name", "member-composite", "key"],
    )
    def test_each_step_kind_is_one_key_holding_the_value_as_written(self, step: SubjectStep, expected: dict) -> None:
        assert step.to_json() == expected

    def test_a_layer_wide_subject_spells_neither_element_nor_path(self) -> None:
        assert Subject("resources").to_json() == {"layer": "resources"}

    def test_an_element_level_subject_spells_no_path(self) -> None:
        assert Subject("swc", "ExampleSWC").to_json() == {"layer": "swc", "element": "ExampleSWC"}

    def test_a_nested_subject_spells_its_steps_outermost_first(self) -> None:
        subject = Subject("swc", "ExampleSWC").at(
            field_step("ports"), member_step("pOut"), field_step("satisfies"), member_step("REQ-12@3")
        )
        assert subject.to_json() == {
            "layer": "swc",
            "element": "ExampleSWC",
            "path": [{"field": "ports"}, {"member": "pOut"}, {"field": "satisfies"}, {"member": "REQ-12@3"}],
        }


class TestTheModuleIsALeaf:
    """The module imports the standard library only.

    Every finding producer imports this module, and a ``swcraft_tools.editing`` import would be a cycle.
    """

    @staticmethod
    def _imported_roots() -> set[str]:
        roots: set[str] = set()
        for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def test_it_imports_nothing_from_swcraft(self) -> None:
        offenders = sorted(root for root in self._imported_roots() if root.startswith("swcraft"))
        assert offenders == [], f"diagnostics.py is a leaf module and imports {offenders}"

    def test_it_imports_only_the_standard_library(self) -> None:
        assert self._imported_roots() <= {"__future__", "dataclasses", "typing"}
