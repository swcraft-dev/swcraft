"""Compositions: the chain, the membership and `layers.compositions` met.

`layers.compositions` = C builds the root `Root` and C - 1 nested compositions `Cmp...` in a chain:
the root's members are the entered components and, when C > 1, the first nested composition; each
nested composition's one composition member is the next; the deepest holds the connector-free
components, and may be empty. Every acceptance is judged by the passport pass over the rendered
documents.
"""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

import pytest
from swcraft_specimens.specimen.build import build_project, render_specimen
from swcraft_specimens.specimen.compositions import build_compositions
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.leaves import LEAVES
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.vocabulary import LAYER_TOTAL, MET
from tests.oracle.specimen_helpers import neutral_passport


def with_compositions(count: int, passport: dict[str, Any] | None = None) -> dict[str, Any]:
    result = copy.deepcopy(neutral_passport() if passport is None else passport)
    result["layers"]["compositions"] = count
    return result


def refused(passport: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r.leaf, r.refusal_class) for r in check_feasibility(passport)]


class TestBuildCompositions:
    def test_one_composition_is_the_root_alone_with_every_component(self) -> None:
        (root,) = build_compositions(["Root"], ["Swc1", "Swc2"], ["Swc3"])
        assert root.name == "Root" and root.title == "Root" and root.root
        assert [(m.name, m.type, m.composition) for m in root.members] == [
            ("M0001", "Swc1", None),
            ("M0002", "Swc2", None),
            ("M0003", "Swc3", None),
        ]
        assert root.ports == () and root.delegations == ()

    def test_four_compositions_form_a_chain(self) -> None:
        compositions = build_compositions(["Root", "Cmp1", "Cmp2", "Cmp3"], ["Swc1", "Swc2"], ["Swc3", "Swc4"])
        assert [c.name for c in compositions] == ["Root", "Cmp1", "Cmp2", "Cmp3"]
        assert [c.root for c in compositions] == [True, False, False, False]
        assert [c.title for c in compositions] == ["Root", "Cmp1", "Cmp2", "Cmp3"]
        root, first, second, deepest = compositions
        assert [(m.name, m.type, m.composition) for m in root.members] == [
            ("M0001", "Swc1", None),
            ("M0002", "Swc2", None),
            ("Cmp1", None, "Cmp1"),
        ]
        # A nested composition's member is named as the composition it holds.
        assert [(m.name, m.type, m.composition) for m in first.members] == [("Cmp2", None, "Cmp2")]
        assert [(m.name, m.type, m.composition) for m in second.members] == [("Cmp3", None, "Cmp3")]
        assert [(m.name, m.type, m.composition) for m in deepest.members] == [
            ("M0001", "Swc3", None),
            ("M0002", "Swc4", None),
        ]

    def test_the_deepest_composition_may_be_empty(self) -> None:
        compositions = build_compositions(["Root", "Cmp1"], ["Swc1"], [])
        assert compositions[-1].members == ()
        assert [m.composition for m in compositions[0].members] == [None, "Cmp1"]

    def test_every_component_is_a_member_exactly_once(self) -> None:
        compositions = build_compositions(["Root", "Cmp1", "Cmp2"], ["Swc1", "Swc3"], ["Swc2", "Swc4", "Swc5"])
        types = Counter(m.type for c in compositions for m in c.members if m.type is not None)
        assert types == Counter({f"Swc{index}": 1 for index in range(1, 6)})


class TestTheLeaf:
    def test_layers_compositions_is_met(self) -> None:
        assert LEAVES["layers.compositions"].leaf_class == MET
        assert LEAVES["identifiers.compositions.max_length"].leaf_class == MET

    @pytest.mark.parametrize("count", [1, 2, 4])
    def test_any_count_of_at_least_one_is_accepted(self, count: int) -> None:
        assert refused(with_compositions(count)) == []

    def test_zero_compositions_is_refused_layer_total(self) -> None:
        refusals = check_feasibility(with_compositions(0))
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("layers.compositions", LAYER_TOTAL)]
        assert refusals[0].sentence == "states 0; SWCraft needs one root composition"

    def test_zero_compositions_leaves_their_identifier_leaves_unchecked(self) -> None:
        passport = with_compositions(0)
        passport["identifiers"]["compositions"] = {"max_length": 28, "mean_length": 20.5}
        assert refused(passport) == [("layers.compositions", LAYER_TOTAL)]

    def test_an_unstated_count_builds_the_root_alone(self) -> None:
        passport = neutral_passport()
        del passport["layers"]["compositions"]
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        assert [c.name for c in project.compositions] == ["Root"]


class TestTheBuiltChain:
    def test_one_composition_holds_every_component_as_before(self) -> None:
        project = build_project(SpecimenPreset(DEFAULT_NAME, with_compositions(1)))
        (root,) = project.compositions
        assert [m.type for m in root.members] == [c.name for c in project.components]
        assert [m.name for m in root.members] == ["M0001", "M0002"]

    def test_four_compositions_are_built_rendered_and_counted_by_the_pass(self) -> None:
        rendered = render_specimen(SpecimenPreset(DEFAULT_NAME, with_compositions(4)))
        project = rendered.project
        assert [c.name for c in project.compositions] == ["Root", "Cmp1", "Cmp2", "Cmp3"]
        root, _, _, deepest = project.compositions
        components = [c.name for c in project.components]
        # Every component is entered here: the root holds them all, the deepest none.
        assert [m.type for m in root.members if m.type is not None] == components
        assert [m.composition for m in root.members if m.composition is not None] == ["Cmp1"]
        assert deepest.members == ()
        assert all(c.composition is None for c in project.connectors)
        assert rendered.passport["layers"]["compositions"] == 4
        assert rendered.passport["identifiers"]["compositions"] == {
            "max_length": 4,
            "mean_length": 4.0,
        }
        assert rendered.passport["connectors"]["delegation"] == 0
        assert rendered.passport["components"]["unconnected"]["total"] == 0

    def test_the_composition_names_meet_their_stated_lengths(self) -> None:
        passport = with_compositions(4)
        # Root and Cmp1..Cmp3 (stem minima 4 each): one at 10, 26 in all.
        passport["identifiers"]["compositions"] = {"max_length": 10, "mean_length": 6.5}
        passport["identifiers"]["max_length"] = 10
        assert refused(passport) == []
        rendered = render_specimen(SpecimenPreset(DEFAULT_NAME, passport))
        names = [c.name for c in rendered.project.compositions]
        assert names[0].startswith("Root")
        assert [name[:4] for name in names[1:]] == ["Cmp1", "Cmp2", "Cmp3"]
        assert rendered.passport["identifiers"]["compositions"] == {
            "max_length": 10,
            "mean_length": 6.5,
        }

    def test_the_cmp_index_is_as_wide_as_the_nested_count(self) -> None:
        passport = with_compositions(11)
        del passport["identifiers"]["compositions"]
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        names = [c.name for c in project.compositions]
        assert names[:3] == ["Root", "Cmp01", "Cmp02"] and names[-1] == "Cmp10"
