"""The type layers in the stated counts.

Units, compu methods, data constraints, application types and mapping sets.

Every element is valid under SWCraft's rules and every reference names a declared element; the five
layer counts and their identifier leaves are `met`. Every acceptance is judged by the passport pass
over the rendered documents.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any

import pytest
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.metrics import layer_counts
from swcraft_specimens.model import (
    ApplicationType,
    DataConstr,
    LinearCompuMethod,
    TextTableCompuMethod,
    Unit,
    ValueType,
)
from swcraft_specimens.specimen.build import build_project, build_specimen_files, render_specimen
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.leaves import LEAVES, stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.types import TYPE_LAYERS, TYPE_STEMS, build_types
from swcraft_specimens.specimen.vocabulary import MET
from tests.oracle.specimen_helpers import neutral_passport

DIRECTORIES = {
    "application_types": "data_types/application",
    "units": "data_types/units",
    "compu_methods": "data_types/compu_methods",
    "data_constraints": "data_types/constraints",
    "mapping_sets": "data_types/mapping_sets",
}


def stated_counts(**counts: int) -> dict[str, Any]:
    """The stated leaves of the five type-layer counts (0 where not given)."""
    return {f"layers.{layer}": counts.get(layer, 0) for layer in TYPE_LAYERS}


def typed(**counts: int) -> dict[str, Any]:
    """The neutral passport with the given type-layer counts.

    The five layers' identifier leaves are unstated, so the identifier plan names them at their
    stem minima.
    """
    passport = copy.deepcopy(neutral_passport())
    for layer in TYPE_LAYERS:
        passport["layers"][layer] = counts.get(layer, 0)
        del passport["identifiers"][layer]
    return passport


def value_types(count: int) -> list[ValueType]:
    return [ValueType(f"Vt{index}", "Bt1") for index in range(1, count + 1)]


def type_names(stated: dict[str, Any]) -> dict[str, list[str]]:
    """Each type layer's names at their stem minima, the index as wide as the count needs."""
    names: dict[str, list[str]] = {}
    for layer, stem in TYPE_STEMS.items():
        count = int(stated.get(f"layers.{layer}", 0))
        width = len(str(count))
        names[layer] = [f"{stem}{index:0{width}d}" for index in range(1, count + 1)]
    return names


def types_of(value_count: int = 2, **counts: int) -> Any:
    stated = stated_counts(**counts)
    return build_types(stated, value_types(value_count), type_names(stated))


class TestTheLeafTable:
    @pytest.mark.parametrize("layer", TYPE_LAYERS)
    def test_the_layer_count_and_its_identifier_leaves_are_met(self, layer: str) -> None:
        assert LEAVES[f"layers.{layer}"].leaf_class == MET
        assert LEAVES[f"identifiers.{layer}.max_length"].leaf_class == MET
        assert LEAVES[f"identifiers.{layer}.mean_length"].leaf_class == MET

    def test_every_count_is_accepted_before_generation(self) -> None:
        passport = typed(units=3, compu_methods=4, data_constraints=2, application_types=7, mapping_sets=2)
        assert check_feasibility(passport) == []


class TestBuildTypes:
    def test_the_counts_equal_the_stated_leaves(self) -> None:
        layers = types_of(units=3, compu_methods=5, data_constraints=2, application_types=7, mapping_sets=2)
        assert len(layers.units) == 3
        assert len(layers.compu_methods) == 5
        assert len(layers.data_constraints) == 2
        assert len(layers.application_types) == 7
        assert len(layers.mapping_sets) == 2

    def test_an_unstated_layer_builds_nothing(self) -> None:
        layers = build_types({}, value_types(2), type_names({}))
        assert layers.units == ()
        assert layers.compu_methods == ()
        assert layers.data_constraints == ()
        assert layers.application_types == ()
        assert layers.mapping_sets == ()

    def test_the_names_are_the_stems_indexed_by_count(self) -> None:
        layers = types_of(units=2, compu_methods=12, data_constraints=1, application_types=3, mapping_sets=1)
        assert [u.name for u in layers.units] == ["Un1", "Un2"]
        assert [c.name for c in layers.compu_methods][:2] == ["Cm01", "Cm02"]
        assert layers.compu_methods[-1].name == "Cm12"
        assert [d.name for d in layers.data_constraints] == ["Dc1"]
        assert [a.name for a in layers.application_types] == ["At1", "At2", "At3"]
        assert [m.name for m in layers.mapping_sets] == ["Ms1"]

    def test_units_carry_a_name_only(self) -> None:
        layers = types_of(units=2)
        assert layers.units == (Unit("Un1"), Unit("Un2"))

    def test_constraints_bound_zero_to_a_hundred(self) -> None:
        layers = types_of(data_constraints=2)
        assert layers.data_constraints == (DataConstr("Dc1", 0, 100), DataConstr("Dc2", 0, 100))

    def test_with_units_every_compu_method_is_linear_round_robin(self) -> None:
        layers = types_of(units=2, compu_methods=5)
        assert layers.compu_methods == tuple(
            LinearCompuMethod(f"Cm{index}", f"Un{(index - 1) % 2 + 1}", 1, 0) for index in range(1, 6)
        )

    def test_without_units_every_compu_method_is_a_two_entry_texttable(self) -> None:
        layers = types_of(compu_methods=3)
        assert layers.compu_methods == tuple(
            TextTableCompuMethod(f"Cm{index}", (("E0", 0), ("E1", 1))) for index in range(1, 4)
        )

    def test_with_units_every_application_type_references_round_robin(self) -> None:
        layers = types_of(units=2, compu_methods=3, data_constraints=4, application_types=7)
        assert layers.application_types == tuple(
            ApplicationType(
                f"At{index}",
                f"Un{(index - 1) % 2 + 1}",
                f"Cm{(index - 1) % 3 + 1}",
                f"Dc{(index - 1) % 4 + 1}",
            )
            for index in range(1, 8)
        )

    def test_without_units_no_application_type_references_a_texttable(self) -> None:
        layers = types_of(compu_methods=3, data_constraints=1, application_types=4)
        assert all(a.unit_ref is None for a in layers.application_types)
        assert all(a.compu_method_ref is None for a in layers.application_types)
        assert [a.data_constr_ref for a in layers.application_types] == ["Dc1"] * 4

    def test_an_empty_layer_is_referenced_by_nothing(self) -> None:
        layers = types_of(application_types=3)
        assert layers.application_types == (
            ApplicationType("At1"),
            ApplicationType("At2"),
            ApplicationType("At3"),
        )

    def test_every_reference_names_a_declared_element(self) -> None:
        layers = types_of(
            value_count=3,
            units=4,
            compu_methods=6,
            data_constraints=5,
            application_types=17,
            mapping_sets=3,
        )
        units = {u.name for u in layers.units}
        methods = {c.name for c in layers.compu_methods}
        constraints = {d.name for d in layers.data_constraints}
        applications = {a.name for a in layers.application_types}
        values = {v.name for v in value_types(3)}
        for method in layers.compu_methods:
            assert isinstance(method, LinearCompuMethod) and method.unit in units
        for application in layers.application_types:
            assert application.unit_ref in units
            assert application.compu_method_ref in methods
            assert application.data_constr_ref in constraints
        for mapping_set in layers.mapping_sets:
            for application, implementation in mapping_set.maps:
                assert application in applications and implementation in values

    def test_the_mapping_rows_map_every_application_type_once_spread_evenly(self) -> None:
        layers = types_of(value_count=3, application_types=7, mapping_sets=3)
        sizes = [len(m.maps) for m in layers.mapping_sets]
        assert sizes == [3, 2, 2]
        rows = [row for m in layers.mapping_sets for row in m.maps]
        # Schema order: the application types in order, the value types round-robin.
        assert rows == [(f"At{i}", f"Vt{(i - 1) % 3 + 1}") for i in range(1, 8)]
        assert Counter(application for application, _ in rows) == Counter(f"At{i}" for i in range(1, 8))

    def test_more_sets_than_application_types_leave_the_last_sets_empty(self) -> None:
        layers = types_of(application_types=2, mapping_sets=3)
        assert [len(m.maps) for m in layers.mapping_sets] == [1, 1, 0]

    def test_without_value_types_the_sets_carry_no_rows(self) -> None:
        layers = types_of(value_count=0, application_types=4, mapping_sets=2)
        assert [m.maps for m in layers.mapping_sets] == [(), ()]

    def test_without_application_types_the_sets_carry_no_rows(self) -> None:
        layers = types_of(mapping_sets=2)
        assert [m.maps for m in layers.mapping_sets] == [(), ()]


TYPED = {
    "units": 2,
    "compu_methods": 3,
    "data_constraints": 2,
    "application_types": 12,
    "mapping_sets": 2,
}


class TestTheBuiltSpecimen:
    def test_build_project_carries_the_type_layers(self) -> None:
        project = build_project(SpecimenPreset(DEFAULT_NAME, typed(**TYPED)))
        assert len(project.units) == 2
        assert len(project.compu_methods) == 3
        assert len(project.data_constraints) == 2
        assert len(project.application_types) == 12
        assert len(project.mapping_sets) == 2
        # The one value type of the neutral passport maps every application type.
        (value_type,) = [t for t in project.data_types if isinstance(t, ValueType)]
        assert [row for m in project.mapping_sets for row in m.maps] == [
            (f"At{i:02d}", value_type.name) for i in range(1, 13)
        ]

    def test_layer_counts_report_the_five_layers_from_the_project(self) -> None:
        rendered = render_specimen(SpecimenPreset(DEFAULT_NAME, typed(**TYPED)))
        counts = layer_counts(rendered.project)
        for layer, directory in DIRECTORIES.items():
            assert getattr(counts, layer) == TYPED[layer]
            prefix = f"model/{directory}/"
            emitted = [p for p in rendered.file_map if p.startswith(prefix) and p.endswith(".yaml")]
            assert len(emitted) == TYPED[layer], layer

    def test_layer_counts_report_zero_without_type_layers(self) -> None:
        counts = layer_counts(build_project(SpecimenPreset(DEFAULT_NAME, typed())))
        assert [getattr(counts, layer) for layer in TYPE_LAYERS] == [0] * 5

    def test_the_pass_counts_the_layers_and_their_identifier_lengths(self) -> None:
        rendered = render_specimen(SpecimenPreset(DEFAULT_NAME, typed(**TYPED)))
        passport = rendered.passport
        for layer, count in TYPED.items():
            assert passport["layers"][layer] == count
        # Un1 Un2, Cm1..Cm3, Dc1 Dc2, At01..At12, Ms1 Ms2.
        assert passport["identifiers"]["units"] == {"max_length": 3, "mean_length": 3.0}
        assert passport["identifiers"]["compu_methods"] == {"max_length": 3, "mean_length": 3.0}
        assert passport["identifiers"]["data_constraints"] == {"max_length": 3, "mean_length": 3.0}
        assert passport["identifiers"]["application_types"] == {
            "max_length": 4,
            "mean_length": 4.0,
        }
        assert passport["identifiers"]["mapping_sets"] == {"max_length": 3, "mean_length": 3.0}

    def test_without_units_the_specimen_builds_with_texttables(self) -> None:
        counts = dict(TYPED, units=0)
        rendered = render_specimen(SpecimenPreset(DEFAULT_NAME, typed(**counts)))
        assert all(isinstance(c, TextTableCompuMethod) for c in rendered.project.compu_methods)
        for layer, count in counts.items():
            assert rendered.passport["layers"][layer] == count

    def test_the_specimen_is_generated_and_self_checked(self) -> None:
        passport = typed(**TYPED)
        files = build_specimen_files(SpecimenPreset(DEFAULT_NAME, passport))
        sidecar = json.loads(files["generation.json"])
        generated = stated_leaves(sidecar["passport"])
        for layer, count in TYPED.items():
            assert generated[f"layers.{layer}"] == count
            assert sidecar["project"]["layer_counts"][layer] == count


@pytest.mark.parametrize("layer", TYPE_LAYERS)
def test_a_name_count_other_than_the_stated_count_is_a_generation_error(layer: str) -> None:
    stated = stated_counts(**{layer: 2})
    names = type_names(stated)
    names[layer] = names[layer][:1]
    with pytest.raises(GenerationError, match=f"names 1 elements of the layer {layer}"):
        build_types(stated, value_types(2), names)
