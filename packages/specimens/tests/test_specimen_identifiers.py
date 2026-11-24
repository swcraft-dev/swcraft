"""Identifiers, exact per layer, with the length limit.

Each met layer's names are a neutral stem and an index padded with a lowercase filler to the length
each element needs. The stated per-layer maximum and mean are met exactly, the overall maximum is
carried by a layer whose lengths are unstated, and every layer's limit leaves room for the names
SWCraft's writer derives from it (111 for a component, 120 for a sender-receiver interface, 128
elsewhere). Every acceptance runs the full pipeline (the model check with the specimen limits, the
emitter, the contract, the rendered-tree oracle) and is judged by the passport pass over the
rendered documents, never by the builder's own lists.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import (
    ConnectorEndpoint,
    Project,
    _limited_names,
    _validate_limited_names,
    validate_project,
)
from swcraft_specimens.names import IDENTIFIER
from swcraft_specimens.passport import PASSPORT_LAYERS
from swcraft_specimens.specimen.build import build_project, render_specimen
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.identifiers import (
    SPECIMEN_LIMITS,
    IdentifierCounts,
    _hardware_kinds,
    _index_width,
    _slots,
    identifier_counts,
)
from swcraft_specimens.specimen.leaves import LEAVES, stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.types import TYPE_LAYERS, TYPE_STEMS
from swcraft_specimens.specimen.vocabulary import IDENTIFIER_LENGTH, IDENTIFIER_MEAN, MET
from tests.oracle.new_kinds import project_with_new_kinds
from tests.oracle.specimen_helpers import MET_IDENTIFIER_LAYERS, neutral_passport

ACCEPTANCE = Path(__file__).parent / "fixtures" / "specimen-acceptance"

#: The least name of each met layer's elements in the wide passport's model (the index is as wide
#: as the kind's count).
STEM_LENGTHS = {
    "swc": 4,  # Swc1
    "interfaces": 3,  # If1
    "connectors": 5,  # Conn1
    "implementation_types": 3,  # Vt1
    "base_types": 3,  # Bt1
    "compositions": 4,  # Root
    "hardware": 3,  # Cp1, Co1, Me1, Pa1
}


def unstated(passport: dict[str, Any], *layers: str) -> dict[str, Any]:
    """The passport with these met layers' identifier lengths unstated."""
    result = copy.deepcopy(passport)
    for layer in layers:
        del result["identifiers"][layer]
    return result


def wide_passport() -> dict[str, Any]:
    """The neutral passport with more elements in the layers whose counts are free.

    Four interfaces (two named by no port, two client-server), seven value types, three base types
    and eight hardware elements; every met layer's lengths unstated, the overall maximum 8.
    """
    passport = unstated(neutral_passport(), *MET_IDENTIFIER_LAYERS)
    passport["layers"].update(interfaces=4, implementation_types=7, base_types=3, hardware=1 + 2 + 2 + 3)
    passport["interfaces"].update(sender_receiver=2, client_server=2)
    passport["hardware"].update(cores=2, memories=2, partitions=3)
    passport["identifiers"]["max_length"] = 5  # Conn1, the longest stem minimum
    return passport


def state(passport: dict[str, Any], layer: str, max_length: int, mean: float) -> dict[str, Any]:
    passport["identifiers"][layer] = {"max_length": max_length, "mean_length": mean}
    return passport


def computed(passport: dict[str, Any]) -> dict[str, Any]:
    """The passport the pass computes over the rendered specimen of this passport."""
    preset = SpecimenPreset(DEFAULT_NAME, passport)
    assert check_feasibility(passport) == []
    return render_specimen(preset).passport


def identifier_refusals(passport: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r.leaf, r.refusal_class) for r in check_feasibility(passport) if r.leaf.startswith("identifiers.")]


def names_of(passport: dict[str, Any]) -> dict[str, list[str]]:
    """Every met identifier layer's element names in the built project."""
    project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
    hardware = [
        project.processor.name,
        *(c.name for c in project.cores),
        *(m.name for m in project.memories),
        *(p.name for p in project.partitions or ()),
    ]
    return {
        "swc": [c.name for c in project.components],
        "interfaces": [i.name for i in project.interfaces],
        "connectors": [c.name for c in project.connectors],
        "implementation_types": [t.name for t in project.data_types if type(t).__name__ != "BaseType"],
        "base_types": [t.name for t in project.data_types if type(t).__name__ == "BaseType"],
        "hardware": hardware,
        "compositions": [c.name for c in project.compositions],
        **{layer: [e.name for e in getattr(project, layer)] for layer in TYPE_LAYERS},
        "tasks": [t.name for t in project.tasks],
        "sequences": [s.name for s in project.sequences],
        "requirements": [r.id for r in project.requirements],
    }


# --- the per-layer lengths are met --------------------------------------------------------------


class TestPerLayerLengthsAreMet:
    def test_max_and_mean_are_met_to_three_places(self) -> None:
        passport = wide_passport()
        state(passport, "implementation_types", 30, 19.857)  # 139 / 7
        state(passport, "base_types", 12, 9.333)  # 28 / 3
        state(passport, "hardware", 40, 23.375)  # 187 / 8
        passport["identifiers"]["max_length"] = 40
        result = computed(passport)["identifiers"]
        assert result["implementation_types"] == {"max_length": 30, "mean_length": 19.857}
        assert result["base_types"] == {"max_length": 12, "mean_length": 9.333}
        assert result["hardware"] == {"max_length": 40, "mean_length": 23.375}
        assert result["max_length"] == 40

    def test_the_neutral_passport_is_met_at_the_stems(self) -> None:
        result = computed(neutral_passport())["identifiers"]
        for layer in MET_IDENTIFIER_LAYERS:
            assert result[layer] == neutral_passport()["identifiers"][layer]
        assert result["max_length"] == 8

    def test_a_stated_max_alone_leaves_the_rest_at_the_stem(self) -> None:
        passport = wide_passport()
        passport["identifiers"]["base_types"] = {"max_length": 20}
        passport["identifiers"]["max_length"] = 20
        result = computed(passport)["identifiers"]["base_types"]
        assert result == {"max_length": 20, "mean_length": round((20 + 3 + 3) / 3, 3)}

    def test_a_stated_mean_alone_keeps_the_maximum_least(self) -> None:
        passport = wide_passport()
        passport["identifiers"]["implementation_types"] = {"mean_length": 10.0}
        passport["identifiers"]["max_length"] = 10
        result = computed(passport)["identifiers"]["implementation_types"]
        assert result == {"max_length": 10, "mean_length": 10.0}

    def test_an_unstated_layer_keeps_its_stem(self) -> None:
        result = computed(wide_passport())["identifiers"]
        for layer, length in STEM_LENGTHS.items():
            assert result[layer]["max_length"] == length
        assert result["hardware"]["mean_length"] == 3.0

    def test_an_empty_layer_states_zero(self) -> None:
        passport = neutral_passport()
        passport["layers"].update(swc=0, connectors=0, interfaces=0)
        passport["layers"].update(implementation_types=0, base_types=0)
        passport["interfaces"].update(sender_receiver=0, fan_out={"max": 0, "top": []})
        passport["components"].update(ports={"total": 0, "top": []}, hubs=[], runnables=0)
        passport["connectors"].update(assembly=0)
        for layer in ("swc", "interfaces", "connectors", "implementation_types", "base_types"):
            passport["identifiers"][layer] = {"max_length": 0, "mean_length": 0}
        assert identifier_refusals(passport) == []
        passport["identifiers"]["swc"] = {"max_length": 7, "mean_length": 7.0}
        assert identifier_refusals(passport) == [
            ("identifiers.swc.max_length", IDENTIFIER_LENGTH),
            ("identifiers.swc.mean_length", IDENTIFIER_MEAN),
        ]


# --- one element ------------------------------------------------------------------------------


class TestOneElement:
    def test_the_mean_of_one_element_is_its_maximum(self) -> None:
        passport = wide_passport()
        state(passport, "compositions", 30, 30.0)
        passport["identifiers"]["max_length"] = 30
        assert computed(passport)["identifiers"]["compositions"] == {
            "max_length": 30,
            "mean_length": 30.0,
        }

    def test_a_mean_off_the_maximum_of_one_element_is_refused(self) -> None:
        passport = wide_passport()
        state(passport, "compositions", 30, 29.0)
        passport["identifiers"]["max_length"] = 30
        assert identifier_refusals(passport) == [("identifiers.compositions.mean_length", IDENTIFIER_MEAN)]


# --- the corrected T interval -------------------------------------------------------------------


class TestTheTotalInterval:
    """Three base types, the stem 3 (`Bt1`), one at the maximum x = 10.

    The total T is in [10 + 2 x 3, 3 x 10] = [16, 30], and round(T / 3, 3) is the mean.
    """

    @pytest.mark.parametrize("mean", [7.333, 8.333, 8.667, 10.0])
    def test_a_mean_inside_the_interval_is_met(self, mean: float) -> None:
        passport = state(wide_passport(), "base_types", 10, mean)
        passport["identifiers"]["max_length"] = 10
        assert computed(passport)["identifiers"]["base_types"] == {
            "max_length": 10,
            "mean_length": mean,
        }

    @pytest.mark.parametrize("mean", [5.0, 5.332, 8.5, 10.001, 10.5])
    def test_an_unreachable_mean_is_refused(self, mean: float) -> None:
        passport = state(wide_passport(), "base_types", 10, mean)
        passport["identifiers"]["max_length"] = 10
        assert identifier_refusals(passport) == [("identifiers.base_types.mean_length", IDENTIFIER_MEAN)]

    @pytest.mark.parametrize("longest", [None, 8])
    @pytest.mark.parametrize("mean", [1e308, 10**400], ids=["1e308", "10**400"])
    @pytest.mark.parametrize("layer", ["base_types", "connectors"])
    def test_a_mean_far_past_every_total_is_refused(self, layer: str, mean: float, longest: int | None) -> None:
        # A finite, schema-valid mean of 1e308 once overflowed n x mean, and an integer mean of
        # 10**400 overflowed float(), with the maximum stated or not.
        passport = wide_passport()
        passport["identifiers"][layer] = {"mean_length": mean}
        if longest is not None:
            passport["identifiers"][layer]["max_length"] = longest
        assert identifier_refusals(passport) == [(f"identifiers.{layer}.mean_length", IDENTIFIER_MEAN)]

    def test_a_maximum_below_the_stem_is_refused(self) -> None:
        passport = state(wide_passport(), "base_types", 2, 2.0)
        assert ("identifiers.base_types.max_length", IDENTIFIER_LENGTH) in identifier_refusals(passport)

    def test_a_mean_alone_below_the_stem_is_refused(self) -> None:
        passport = wide_passport()
        passport["identifiers"]["base_types"] = {"mean_length": 2.5}
        assert identifier_refusals(passport) == [("identifiers.base_types.mean_length", IDENTIFIER_MEAN)]

    def test_the_mean_is_spread_as_evenly_as_the_total_allows(self) -> None:
        passport = state(wide_passport(), "implementation_types", 30, 19.857)
        passport["identifiers"]["max_length"] = 30
        lengths = sorted(len(name) for name in names_of(passport)["implementation_types"])
        assert lengths[-1] == 30
        assert max(lengths[:-1]) - min(lengths[:-1]) <= 1
        assert sum(lengths) == 139


# --- the overall maximum ------------------------------------------------------------------------


class TestTheOverallMaximum:
    def test_it_is_carried_by_the_first_unstated_layer(self) -> None:
        passport = state(wide_passport(), "swc", 9, 8.5)
        passport["identifiers"]["max_length"] = 50
        result = computed(passport)["identifiers"]
        assert result["max_length"] == 50
        # `swc` is stated; `interfaces` is the first unstated layer in the registry order.
        assert result["interfaces"]["max_length"] == 50
        assert result["swc"] == {"max_length": 9, "mean_length": 8.5}
        for layer in ("connectors", "implementation_types", "base_types", "hardware"):
            assert result[layer]["max_length"] == STEM_LENGTHS[layer]

    def test_it_equals_a_stated_layer_maximum_with_no_carrier(self) -> None:
        passport = neutral_passport()
        passport["identifiers"]["swc"] = {"max_length": 20, "mean_length": 13.5}
        passport["identifiers"]["max_length"] = 20
        assert computed(passport)["identifiers"]["max_length"] == 20

    def test_it_is_refused_when_no_unstated_layer_can_carry_it(self) -> None:
        passport = neutral_passport()
        passport["identifiers"]["max_length"] = 9
        assert identifier_refusals(passport) == [("identifiers.max_length", IDENTIFIER_LENGTH)]

    def test_it_is_refused_above_every_unstated_layer_s_limit(self) -> None:
        # Only the components are unstated, and 112 is above their limit of 111.
        passport = unstated(neutral_passport(), "swc")
        passport["identifiers"]["max_length"] = 112
        assert identifier_refusals(passport) == [("identifiers.max_length", IDENTIFIER_LENGTH)]
        passport["identifiers"]["max_length"] = 111
        assert computed(passport)["identifiers"]["swc"]["max_length"] == 111

    def test_it_is_refused_below_a_stated_layer_maximum(self) -> None:
        passport = neutral_passport()
        passport["identifiers"]["swc"] = {"max_length": 20, "mean_length": 13.5}
        passport["identifiers"]["max_length"] = 19
        assert identifier_refusals(passport) == [("identifiers.max_length", IDENTIFIER_LENGTH)]

    def test_it_is_refused_below_an_unstated_layer_s_stem(self) -> None:
        passport = unstated(neutral_passport(), "connectors")  # Conn1: 5
        passport["identifiers"]["max_length"] = 4
        refusals = identifier_refusals(passport)
        assert ("identifiers.max_length", IDENTIFIER_LENGTH) in refusals

    def test_the_refusal_sentence_names_the_layer_and_the_value(self) -> None:
        passport = neutral_passport()
        passport["identifiers"]["max_length"] = 9
        (refusal,) = [r for r in check_feasibility(passport) if r.leaf.startswith("identifiers")]
        assert "states 9" in refusal.sentence
        assert "8" in refusal.sentence


# --- the limits, at the boundary, through the full pipeline -------------------------------------


def at_length(layer: str, length: int, *, client_server: bool = False) -> dict[str, Any]:
    """The wide passport with one element of `layer` at `length`, the rest at the stem."""
    passport = wide_passport()
    if not client_server:
        passport["interfaces"].update(sender_receiver=4, client_server=0)
    passport["identifiers"][layer] = {"max_length": length}
    passport["identifiers"]["max_length"] = length
    return passport


def rename_component(project: Project, new: str) -> Project:
    """The project with its first component renamed everywhere it is named."""
    old = project.components[0].name

    def end(endpoint: ConnectorEndpoint) -> ConnectorEndpoint:
        return dataclasses.replace(endpoint, swc=new) if endpoint.swc == old else endpoint

    (root,) = project.compositions
    return dataclasses.replace(
        project,
        components=(
            dataclasses.replace(project.components[0], name=new),
            *project.components[1:],
        ),
        domains=tuple(
            dataclasses.replace(d, components=tuple(new if c == old else c for c in d.components))
            for d in project.domains
        ),
        connectors=tuple(
            dataclasses.replace(c, source=end(c.source), target=end(c.target)) for c in project.connectors
        ),
        compositions=(
            dataclasses.replace(
                root,
                members=tuple(dataclasses.replace(m, type=new) if m.type == old else m for m in root.members),
            ),
        ),
    )


class TestTheLimits:
    def test_the_limits_are_the_decided_table(self) -> None:
        assert SPECIMEN_LIMITS == {
            "swc": 111,
            "interfaces.sender_receiver": 120,
            "interfaces.client_server": 128,
            "connectors": 128,
            "implementation_types": 128,
            "base_types": 128,
            "interfaces.external": 128,
            "hardware": 128,
            "compositions": 128,
            "application_types": 128,
            "mapping_sets": 128,
            "units": 128,
            "compu_methods": 128,
            "data_constraints": 128,
            "requirements": 128,
            "tasks": 128,
            "sequences": 128,
        }

    @pytest.mark.parametrize(
        ("layer", "limit"),
        [
            ("swc", 61),  # above the 60 of a WATERS preset, which does not bind a specimen
            ("swc", 111),
            ("interfaces", 120),
            ("connectors", 128),
            ("implementation_types", 128),
            ("base_types", 128),
            ("hardware", 128),
            ("compositions", 128),
        ],
    )
    def test_a_name_at_the_limit_is_generated_and_validated(self, layer: str, limit: int) -> None:
        passport = at_length(layer, limit)
        rendered = computed(passport)
        assert rendered["identifiers"][layer]["max_length"] == limit
        assert rendered["identifiers"]["max_length"] == limit

    @pytest.mark.parametrize(
        ("layer", "above"),
        [
            ("swc", 112),
            ("interfaces", 121),
            ("connectors", 129),
            ("implementation_types", 129),
            ("base_types", 129),
            ("hardware", 129),
            ("compositions", 129),
        ],
    )
    def test_a_name_one_above_the_limit_is_refused(self, layer: str, above: int) -> None:
        passport = at_length(layer, above)
        refusals = check_feasibility(passport)
        assert (f"identifiers.{layer}.max_length", IDENTIFIER_LENGTH) in [(r.leaf, r.refusal_class) for r in refusals]
        (sentence,) = [r.sentence for r in refusals if r.leaf == f"identifiers.{layer}.max_length"]
        assert str(above - 1) in sentence

    def test_a_client_server_interface_carries_up_to_128(self) -> None:
        passport = at_length("interfaces", 128, client_server=True)
        names = names_of(passport)["interfaces"]
        assert computed(passport)["identifiers"]["interfaces"]["max_length"] == 128
        # The two sender-receiver interfaces come first and stay within 120.
        assert [len(name) for name in names[:2]] == [3, 3]
        assert len(names[2]) == 128
        assert identifier_refusals(at_length("interfaces", 129, client_server=True)) == [
            ("identifiers.interfaces.max_length", IDENTIFIER_LENGTH)
        ]

    def test_a_sender_receiver_mean_cannot_pass_120(self) -> None:
        passport = wide_passport()
        passport["interfaces"].update(sender_receiver=4, client_server=0)
        passport["identifiers"]["interfaces"] = {"mean_length": 120.25}
        assert identifier_refusals(passport) == [("identifiers.interfaces.mean_length", IDENTIFIER_MEAN)]

    def test_the_model_check_holds_the_specimen_limits(self) -> None:
        project = build_project(SpecimenPreset(DEFAULT_NAME, at_length("swc", 111)))
        validate_project(project, SPECIMEN_LIMITS)
        renamed = rename_component(project, project.components[0].name + "x")
        with pytest.raises(GenerationError, match="max 111 characters"):
            validate_project(renamed, SPECIMEN_LIMITS)

    def test_the_model_check_holds_a_sender_receiver_interface_to_120(self) -> None:
        project = build_project(SpecimenPreset(DEFAULT_NAME, at_length("interfaces", 120)))
        validate_project(project, SPECIMEN_LIMITS)
        first = project.interfaces[0]
        assert len(first.name) == 120
        renamed = dataclasses.replace(
            project,
            interfaces=(dataclasses.replace(first, name=first.name + "x"), *project.interfaces[1:]),
        )
        with pytest.raises(GenerationError, match="max 120 characters"):
            validate_project(renamed, SPECIMEN_LIMITS)

    def test_without_the_table_the_waters_limit_of_60_stays(self) -> None:
        project = build_project(SpecimenPreset(DEFAULT_NAME, at_length("swc", 61)))
        with pytest.raises(GenerationError, match="max 60 characters"):
            validate_project(project)

    def test_an_incomplete_limit_table_is_refused(self) -> None:
        project = build_project(SpecimenPreset(DEFAULT_NAME, neutral_passport()))
        with pytest.raises(GenerationError, match="limit table"):
            validate_project(project, {"swc": 111})


# --- the names -----------------------------------------------------------------------------------


class TestNames:
    def long_passport(self) -> dict[str, Any]:
        passport = wide_passport()
        state(passport, "implementation_types", 100, 99.857)
        state(passport, "hardware", 90, 89.875)
        state(passport, "interfaces", 128, 98.5)
        passport["identifiers"]["max_length"] = 128
        return passport

    def test_names_are_legal_and_unique_ignoring_case(self) -> None:
        names = names_of(self.long_passport())
        for layer, layer_names in names.items():
            assert all(IDENTIFIER.fullmatch(name) for name in layer_names), layer
            assert len({name.casefold() for name in layer_names}) == len(layer_names), layer
        everything = [name for layer_names in names.values() for name in layer_names]
        assert len({name.casefold() for name in everything}) == len(everything)

    def test_names_are_a_stem_an_index_and_a_lowercase_filler(self) -> None:
        names = names_of(self.long_passport())
        assert names["compositions"] == ["Root"]
        assert names["swc"] == ["Swc1", "Swc2"]
        assert all(name.startswith("Vt") for name in names["implementation_types"])
        assert names["hardware"][0].startswith("Cp1")
        filler = names["hardware"][0][len("Cp1") :]
        assert filler and filler.islower() and filler.isalpha()

    def test_the_index_widens_beyond_9999(self) -> None:
        passport = wide_passport()
        passport["layers"]["base_types"] = 10000
        passport["identifiers"]["max_length"] = 7  # Bt00001
        names = names_of(passport)["base_types"]
        assert names[0] == "Bt00001" and names[-1] == "Bt10000"

    def test_the_index_is_as_wide_as_the_count(self) -> None:
        # The digits of the count, no least width.
        passport = wide_passport()
        passport["layers"]["base_types"] = 12
        names = names_of(passport)["base_types"]
        assert names[0] == "Bt01" and names[-1] == "Bt12"
        assert names_of(wide_passport())["swc"] == ["Swc1", "Swc2"]

    def test_the_rendered_files_are_unique_ignoring_case(self) -> None:
        file_map = render_specimen(SpecimenPreset(DEFAULT_NAME, self.long_passport())).file_map
        assert len({path.casefold() for path in file_map}) == len(file_map)

    def test_the_build_is_deterministic(self) -> None:
        preset = SpecimenPreset(DEFAULT_NAME, self.long_passport())
        assert render_specimen(preset).file_map == render_specimen(preset).file_map


class TestIndexWidthAndHardwareStems:
    """The index is as wide as the count, and the hardware stems have two letters."""

    @pytest.mark.parametrize(("count", "width"), [(1, 1), (9, 1), (12, 2), (10000, 5)])
    def test_the_index_width_is_the_digits_of_the_count(self, count: int, width: int) -> None:
        assert _index_width(count) == width

    def test_the_hardware_stems_have_two_letters(self) -> None:
        counts = IdentifierCounts(
            components=0,
            sender_receiver=0,
            client_server=0,
            connectors=0,
            implementation_types=0,
            base_types=0,
            cores=2,
            memories=1,
            partitions=3,
            stubs=0,
        )
        assert _hardware_kinds(counts) == (("Cp", 1), ("Co", 2), ("Me", 1), ("Pa", 3))

    def test_three_hardware_names_meet_a_maximum_of_10_with_a_mean_of_6(self) -> None:
        # One processor, one core, one partition: the stem minima 3 + 3 + 3, one name at 10 and
        # the other two totalling 8.
        passport = neutral_passport()
        passport["layers"]["hardware"] = 3
        passport["hardware"].update(cores=1, memories=0, partitions=1)
        state(passport, "hardware", 10, 6.0)
        passport["identifiers"]["max_length"] = 10
        assert identifier_refusals(passport) == []
        assert computed(passport)["identifiers"]["hardware"] == {
            "max_length": 10,
            "mean_length": 6.0,
        }
        assert sorted(len(name) for name in names_of(passport)["hardware"]) == [4, 4, 10]


class TestStubNames:
    """The external interface stubs are `If…` slots of the interfaces layer after the client-server ones.

    They are at the limit 128 (a stub emits no SHORT-NAME of its own).
    """

    @staticmethod
    def stub_passport(stubs: int) -> dict[str, Any]:
        passport = neutral_passport()
        passport["layers"]["interfaces"] = 1 + stubs
        return passport

    def test_the_stubs_are_counted(self) -> None:
        leaves = stated_leaves(self.stub_passport(2))
        leaves.update({"hardware.cores": 1, "hardware.partitions": 0, "hardware.memories": 0})
        assert identifier_counts(leaves, 1).stubs == 2

    def test_the_stubs_are_named_after_the_client_server_interfaces(self) -> None:
        passport = self.stub_passport(2)
        state(passport, "interfaces", 6, 6.0)
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        assert [i.kind for i in project.interfaces] == ["sender_receiver"] + ["external_interface"] * 2
        assert len(names_of(passport)["interfaces"]) == 3
        assert computed(passport)["identifiers"]["interfaces"] == {
            "max_length": 6,
            "mean_length": 6.0,
        }

    def test_a_stub_carries_a_name_of_128(self) -> None:
        passport = self.stub_passport(1)
        state(passport, "interfaces", 128, 67.0)
        passport["identifiers"]["max_length"] = 128
        assert identifier_refusals(passport) == []
        names = names_of(passport)["interfaces"]
        assert [len(name) for name in names] == [6, 128]
        assert computed(passport)["identifiers"]["interfaces"] == {
            "max_length": 128,
            "mean_length": 67.0,
        }

    def test_without_a_stub_the_sender_receiver_limit_refuses_128(self) -> None:
        passport = self.stub_passport(0)
        state(passport, "interfaces", 128, 128.0)
        passport["identifiers"]["max_length"] = 128
        assert identifier_refusals(passport) == [("identifiers.interfaces.max_length", IDENTIFIER_LENGTH)]


class TestTypeLayerNames:
    """The five type layers are named by the identifier plan at the limit 128, their lengths met exactly.

    `At`, `Un`, `Cm`, `Dc` and `Ms` slots with the index as wide as the count, and their stated
    maximum and mean are met exactly.
    """

    @staticmethod
    def typed(**counts: int) -> dict[str, Any]:
        """The neutral passport with these type-layer counts, their identifier leaves unstated."""
        passport = neutral_passport()
        for layer, count in counts.items():
            passport["layers"][layer] = count
            del passport["identifiers"][layer]
        return passport

    def test_the_new_limits_are_128(self) -> None:
        for key in (*TYPE_LAYERS, "interfaces.external"):
            assert SPECIMEN_LIMITS[key] == 128, key

    def test_the_slots_of_the_type_layers_and_the_stubs(self) -> None:
        counts = IdentifierCounts(
            components=1,
            sender_receiver=1,
            client_server=1,
            connectors=0,
            implementation_types=1,
            base_types=1,
            cores=1,
            memories=0,
            partitions=0,
            stubs=2,
            application_types=12,
            mapping_sets=2,
            units=2,
            compu_methods=3,
            data_constraints=1,
        )
        slots = _slots(counts)
        assert [s.prefix for s in slots["application_types"]] == [f"At{i:02d}" for i in range(1, 13)]
        assert [s.prefix for s in slots["mapping_sets"]] == ["Ms1", "Ms2"]
        assert [s.prefix for s in slots["units"]] == ["Un1", "Un2"]
        assert [s.prefix for s in slots["compu_methods"]] == ["Cm1", "Cm2", "Cm3"]
        assert [s.prefix for s in slots["data_constraints"]] == ["Dc1"]
        for layer in TYPE_LAYERS:
            assert {s.limit for s in slots[layer]} == {128}, layer
        # If1 sender-receiver (120), If2 client-server, If3 and If4 the stubs, each at 128.
        assert [s.prefix for s in slots["interfaces"]] == ["If1", "If2", "If3", "If4"]
        assert [s.limit for s in slots["interfaces"]] == [120, 128, 128, 128]

    def test_the_counts_come_from_the_layer_leaves(self) -> None:
        leaves = stated_leaves(self.typed(application_types=4, mapping_sets=1, units=2, compu_methods=3))
        counts = identifier_counts(leaves, 1)
        assert (
            counts.application_types,
            counts.mapping_sets,
            counts.units,
            counts.compu_methods,
            counts.data_constraints,
        ) == (4, 1, 2, 3, 0)

    @pytest.mark.parametrize("layer", TYPE_LAYERS)
    def test_a_stated_maximum_and_mean_are_met(self, layer: str) -> None:
        passport = state(self.typed(**{layer: 3}), layer, 20, 10.0)
        passport["identifiers"]["max_length"] = 20
        assert identifier_refusals(passport) == []
        assert computed(passport)["identifiers"][layer] == {"max_length": 20, "mean_length": 10.0}
        names = names_of(passport)[layer]
        assert sorted(len(name) for name in names) == [5, 5, 20]
        assert all(name[:2] == TYPE_STEMS[layer] for name in names)

    @pytest.mark.parametrize("layer", TYPE_LAYERS)
    def test_a_mean_below_the_stem_minimum_is_refused(self, layer: str) -> None:
        passport = self.typed(**{layer: 3})
        passport["identifiers"][layer] = {"mean_length": 2.5}
        assert identifier_refusals(passport) == [(f"identifiers.{layer}.mean_length", IDENTIFIER_MEAN)]

    def test_an_unstated_type_layer_keeps_its_stems(self) -> None:
        passport = self.typed(application_types=12, units=2)
        result = computed(passport)["identifiers"]
        assert result["application_types"] == {"max_length": 4, "mean_length": 4.0}
        assert result["units"] == {"max_length": 3, "mean_length": 3.0}
        assert names_of(passport)["units"] == ["Un1", "Un2"]

    def test_the_overall_maximum_is_carried_by_the_only_layer_that_can(self) -> None:
        # Every previous layer states its lengths, so the units are the one layer whose lengths
        # are unstated: one unit carries the overall maximum.
        passport = self.typed(units=2)
        passport["identifiers"]["max_length"] = 50
        assert identifier_refusals(passport) == []
        result = computed(passport)["identifiers"]
        assert result["max_length"] == 50
        assert result["units"] == {"max_length": 50, "mean_length": 26.5}

    def test_without_the_type_layer_the_overall_maximum_has_no_carrier(self) -> None:
        passport = self.typed()
        passport["identifiers"]["max_length"] = 50
        assert identifier_refusals(passport) == [("identifiers.max_length", IDENTIFIER_LENGTH)]


def test_the_limit_table_and_the_model_agree_on_their_keys() -> None:
    """`model._limited_names` and `SPECIMEN_LIMITS` name the same keys.

    The stubs are under their own and every type layer under its own.
    """
    project = project_with_new_kinds()
    limited = {key: names for key, _, names in _limited_names(project)}
    assert sorted(limited) == sorted(SPECIMEN_LIMITS)
    assert limited["interfaces.external"] == ["IfStub"]
    assert "IfStub" not in limited["interfaces.client_server"]
    assert limited["units"] == ["Un1", "Un2"]
    assert limited["compu_methods"] == ["Cm1", "Cm2"]
    assert limited["data_constraints"] == ["Dc1"]
    assert limited["application_types"] == ["At1", "At2"]
    assert limited["mapping_sets"] == ["Ms1"]


@pytest.mark.parametrize("layer", TYPE_LAYERS)
def test_a_type_name_above_128_is_refused_by_the_model(layer: str) -> None:
    passport = TestTypeLayerNames.typed(**{layer: 1})
    passport["identifiers"][layer] = {"max_length": 128, "mean_length": 128.0}
    passport["identifiers"]["max_length"] = 128
    project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
    validate_project(project, limits=SPECIMEN_LIMITS)
    (element,) = getattr(project, layer)
    changes: dict[str, Any] = {layer: (dataclasses.replace(element, name=element.name + "x"),)}
    renamed = dataclasses.replace(project, **changes)
    context = layer.removesuffix("s").replace("_", " ")
    with pytest.raises(GenerationError, match=f"invalid {context} name .* max 128 characters"):
        validate_project(renamed, limits=SPECIMEN_LIMITS)


def test_an_external_interface_name_above_128_is_refused_by_the_model() -> None:
    """The stubs' own limit key, `interfaces.external`: 128 passes, 129 is refused."""
    project = project_with_new_kinds()

    def renamed(length: int) -> Project:
        interfaces = tuple(
            dataclasses.replace(i, name="I" * length) if i.name == "IfStub" else i for i in project.interfaces
        )
        return dataclasses.replace(project, interfaces=interfaces)

    _validate_limited_names(renamed(128), SPECIMEN_LIMITS)
    with pytest.raises(GenerationError, match="invalid external interface name .* max 128"):
        validate_project(renamed(129), limits=SPECIMEN_LIMITS)


# --- the frame ----------------------------------------------------------------------------------


def test_the_identifier_leaves_are_built() -> None:
    expected = {"identifiers.max_length"} | {
        f"identifiers.{layer}.{leaf}" for layer in MET_IDENTIFIER_LAYERS for leaf in ("max_length", "mean_length")
    }
    assert expected == {
        path for path, leaf in LEAVES.items() if path.startswith("identifiers.") and leaf.leaf_class == MET
    }


def test_the_registry_order_is_the_passport_s() -> None:
    assert [layer for layer in PASSPORT_LAYERS if layer in STEM_LENGTHS] == list(STEM_LENGTHS)


def test_names_of_refused_lengths_are_a_generation_error() -> None:
    from swcraft_specimens.specimen.identifiers import identifier_counts, identifier_names
    from swcraft_specimens.specimen.leaves import stated_leaves

    passport = neutral_passport()
    passport["identifiers"]["swc"] = {"max_length": 200, "mean_length": 7.0}
    leaves = stated_leaves(passport)
    with pytest.raises(GenerationError, match="refused after feasibility"):
        identifier_names(leaves, identifier_counts(leaves, 1))


def test_the_connector_count_filter_agrees_with_the_identifier_check() -> None:
    """`ConnectorNames.admits` agrees with the identifier check over the counts the stated leaves admit.

    When some count passes the identifier check, only such counts are admitted, so a refusal at the
    chosen count is never the count's; when none does, some count is admitted, so a conflict the
    stated leaves decide alone is refused with its own class, never as `derived`; when every count
    gives one same refusal, only the counts that give it alone are admitted."""
    from swcraft_specimens.specimen.identifiers import (
        connector_names,
        identifier_counts,
        identifier_refusals,
    )
    from swcraft_specimens.specimen.leaves import stated_leaves
    from swcraft_specimens.specimen.vocabulary import MAX_LENGTH

    decided = {
        "identifiers.connectors.max_length",
        "identifiers.connectors.mean_length",
        MAX_LENGTH,
    }
    for total in (2, 9, 30):
        for x in (None, 0, 7, 8, 9, 13, 128, 129):
            for mean in (None, 0.0, 8.0, 8.5, 9.0, 10.571, 12.25, 128.0):
                for overall in (8, 9, 13, 120, 129):
                    passport = neutral_passport()
                    # Six components, one interface holding every port, and a hub beside two
                    # ports, so the fan-out and link leaves bound neither end of the counts.
                    passport["layers"]["swc"] = 6
                    passport["components"]["ports"] = {"total": total}
                    passport["components"]["hubs"] = [] if total == 2 else [total * 3 // 8]
                    passport["interfaces"]["fan_out"] = {"max": total, "top": [total]}
                    connectors: dict[str, Any] = {}
                    if x is not None:
                        connectors["max_length"] = x
                    if mean is not None:
                        connectors["mean_length"] = mean
                    if connectors:
                        passport["identifiers"]["connectors"] = connectors
                    else:
                        del passport["identifiers"]["connectors"]
                    passport["identifiers"]["max_length"] = overall
                    stated = stated_leaves(passport)
                    names = connector_names(stated)
                    domain = [*([0] if names.zero else []), *names.domain]
                    assert domain, total
                    refused = {
                        count: {
                            (refusal.leaf, refusal.refusal_class)
                            for refusal in identifier_refusals(stated, identifier_counts(stated, count))
                            if refusal.leaf in decided
                        }
                        for count in domain
                    }
                    case = (total, x, mean, overall)
                    admitted = [count for count in domain if names.admits(count)]
                    assert admitted, case
                    assert all(names.refused_at(count) == refused[count] for count in domain)
                    if any(not refused[count] for count in domain):
                        assert all(not refused[count] for count in admitted), case
                    else:
                        common = set.intersection(*refused.values())
                        if common:
                            assert all(refused[count] == common for count in admitted), case


# --- the task and sequence layers ---------------------------------------------------------------


class TestTaskAndSequenceNames:
    @staticmethod
    def tasked(tasks: int, idle: int, sequences: int) -> dict[str, Any]:
        """The neutral passport with tasks on one partition and four runnables.

        The task and sequence identifier lengths are unstated (the overall maximum 8 is the
        connectors').
        """
        passport = neutral_passport()
        passport["layers"].update(tasks=tasks, sequences=sequences, hardware=3)
        passport["tasks"]["idle"] = idle
        passport["components"]["runnables"] = 4
        passport["hardware"]["partitions"] = 1
        for layer in ("hardware", "tasks", "sequences"):
            del passport["identifiers"][layer]
        return passport

    def test_the_slots_are_ts_and_sq_as_wide_as_their_counts(self) -> None:
        counts = dataclasses.replace(identifier_counts(stated_leaves(self.tasked(12, 2, 3)), 1), tasks=12, sequences=3)
        slots = _slots(counts)
        assert [s.prefix for s in slots["tasks"]] == [f"Ts{i:02d}" for i in range(1, 13)]
        assert [s.prefix for s in slots["sequences"]] == ["Sq1", "Sq2", "Sq3"]
        assert {s.limit for s in [*slots["tasks"], *slots["sequences"]]} == {128}

    def test_the_counts_come_from_the_leaves_and_the_busy_tasks(self) -> None:
        counts = identifier_counts(stated_leaves(self.tasked(5, 1, 3)), 1)
        assert (counts.tasks, counts.sequences) == (5, 3)
        unstated = self.tasked(5, 1, 3)
        del unstated["layers"]["sequences"]
        counts = identifier_counts(stated_leaves(unstated), 1)
        assert (counts.tasks, counts.sequences) == (5, 4)

    @pytest.mark.parametrize("layer", ["tasks", "sequences"])
    def test_a_stated_maximum_and_mean_are_met(self, layer: str) -> None:
        passport = state(self.tasked(4, 1, 3), layer, 20, 10.0)
        passport["identifiers"]["max_length"] = 20
        assert identifier_refusals(passport) == []
        assert computed(passport)["identifiers"][layer] == {"max_length": 20, "mean_length": 10.0}
        names = names_of(passport)[layer]
        assert all(name.startswith("Ts" if layer == "tasks" else "Sq") for name in names)
        assert max(len(name) for name in names) == 20

    def test_the_busy_tasks_are_named_before_the_idle_ones(self) -> None:
        project = build_project(SpecimenPreset(DEFAULT_NAME, self.tasked(3, 1, 2)))
        assert [t.name for t in project.tasks] == ["Ts1", "Ts2", "Ts3"]
        assert [len(t.runnables) > 0 for t in project.tasks] == [True, True, False]
        assert [s.name for s in project.sequences] == ["Sq1", "Sq2"]

    @pytest.mark.parametrize("layer", ["tasks", "sequences"])
    def test_a_maximum_above_128_is_identifier_length(self, layer: str) -> None:
        passport = state(self.tasked(2, 0, 2), layer, 129, 129.0)
        assert (f"identifiers.{layer}.max_length", IDENTIFIER_LENGTH) in identifier_refusals(passport)

    def test_basic_twin_s_overall_maximum_is_carried_by_its_sequence_layer(self) -> None:
        # The overall 23 is the stated sequence maximum; the requirement layer is met and named
        # too, so no identifier leaf is refused.
        passport = json.loads((ACCEPTANCE / "basic-twin.json").read_text(encoding="utf-8"))
        assert passport["identifiers"]["max_length"] == 23
        assert passport["identifiers"]["sequences"]["max_length"] == 23
        assert identifier_refusals(passport) == []


# --- the requirement layer ----------------------------------------------------------------------


class TestRequirementNames:
    @staticmethod
    def required(total: int) -> dict[str, Any]:
        """The neutral passport with `total` general requirements (links derived).

        The requirement identifier lengths are unstated (the overall maximum 8 is the connectors').
        """
        passport = neutral_passport()
        passport["layers"]["requirements"] = total
        del passport["requirements"]["links"]
        del passport["identifiers"]["requirements"]
        return passport

    def test_the_slots_are_rq_as_wide_as_their_count(self) -> None:
        counts = identifier_counts(stated_leaves(self.required(12)), 1)
        assert counts.requirements == 12
        slots = _slots(counts)
        assert [s.prefix for s in slots["requirements"]] == [f"Rq{i:02d}" for i in range(1, 13)]
        assert {s.limit for s in slots["requirements"]} == {128}

    def test_a_stated_maximum_and_mean_are_met(self) -> None:
        passport = state(self.required(4), "requirements", 20, 10.0)
        passport["identifiers"]["max_length"] = 20
        assert identifier_refusals(passport) == []
        assert computed(passport)["identifiers"]["requirements"] == {
            "max_length": 20,
            "mean_length": 10.0,
        }
        names = names_of(passport)["requirements"]
        assert all(name.startswith("Rq") for name in names)
        assert max(len(name) for name in names) == 20
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        assert [r.id for r in project.requirements] == names

    def test_a_maximum_above_128_is_identifier_length(self) -> None:
        passport = state(self.required(2), "requirements", 129, 129.0)
        assert ("identifiers.requirements.max_length", IDENTIFIER_LENGTH) in identifier_refusals(passport)
