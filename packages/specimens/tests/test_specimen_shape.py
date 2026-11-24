"""The shape: unstated port and fan-out leaves derived, and the refusal classes.

A partial passport (any subset the schema accepts) is met when some derivation of its unstated
leaves is; a refusal a derivation caused is `derived`, never an inequality class, while a conflict
the stated leaves decide alone keeps its class; a count above the ceiling, stated or one the
derivation would build, is refused before any work grows with it; an unstated connector count is
chosen so that no stated identifier leaf it decides is refused (the connector layer's maximum and
mean, the overall maximum it carries); a layer whose count is refused has its identifier leaves
left unchecked; and the overall identifier maximum may ride on a layer that states only its mean.
"""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.passport import Refusal, compute_passport, read_passport, validate_passport
from swcraft_specimens.specimen import connectors as connectors_module
from swcraft_specimens.specimen import feasibility
from swcraft_specimens.specimen import layout as layout_module
from swcraft_specimens.specimen import shape as shape_module
from swcraft_specimens.specimen.build import build_project, build_specimen_files
from swcraft_specimens.specimen.common import COUNT_CEILING
from swcraft_specimens.specimen.components import place_ports, port_spec
from swcraft_specimens.specimen.connectors import (
    FULL_SEARCH,
    QUICK_SEARCH,
    ConnectorSpec,
    construction_spec,
    solve_open,
    unconnected_placements,
)
from swcraft_specimens.specimen.delegations import EntryOrder, admitted_reach, most_connectors
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.identifiers import connector_names
from swcraft_specimens.specimen.interfaces import connectable_fan_outs, split_fan_outs
from swcraft_specimens.specimen.layout import (
    Configuration,
    Planned,
    Trace,
    derived_total_clause,
    no_layout_refusal,
)
from swcraft_specimens.specimen.leaves import MET_IDENTIFIER_LAYERS, stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.shape import (
    SHAPE_BUDGET,
    Shape,
    _admits_choosable,
    _connector_range,
    _count_range,
    _totals,
    specimen_shape,
    star_size_total,
)
from swcraft_specimens.specimen.vocabulary import CEILING, DELEGATIONS, DERIVED, NO_LAYOUT
from tests.oracle.specimen_helpers import (
    clear_search_caches,
    configuration_of,
    count_calls,
    count_yields,
    load_specimen,
    neutral_passport,
    record_calls,
    refused_before_construction,
    shaped_witness,
    topology,
    witness,
)


def refused(passport: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r.leaf, r.refusal_class) for r in check_feasibility(passport)]


def generated(passport: dict[str, Any]) -> dict[str, Any]:
    files = build_specimen_files(SpecimenPreset(DEFAULT_NAME, passport))
    result: dict[str, Any] = json.loads(files["generation.json"])["passport"]
    return result


#: `shape._try`'s signature, taken before any test wraps the name.
_TRY_SIGNATURE = inspect.signature(shape_module._try)


def _tried_total(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
    """The total of the configuration a `shape._try` call received."""
    configuration = _TRY_SIGNATURE.bind(*args, **kwargs).arguments["configuration"]
    total: int = configuration[0]
    return total


def without_identifiers(passport: dict[str, Any]) -> dict[str, Any]:
    passport["identifiers"] = {"max_length": passport["identifiers"]["max_length"]}
    return passport


class TestTheDerivedTotal:
    def test_an_unstated_total_follows_a_short_fan_out_top(self) -> None:
        # The placement's own default would be 2 ports per component (8); fan_out.top of fewer
        # than eight entries is every named interface, so the total is its sum, 12.
        passport = without_identifiers(neutral_passport())
        passport["layers"].update(swc=4, interfaces=3)
        passport["components"]["ports"] = {"top": [3, 3, 3, 3]}
        passport["interfaces"].update(sender_receiver=3, fan_out={"max": 4, "top": [4, 4, 4]})
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        passport["connectors"]["links"] = 7
        assert refused(passport) == []
        result = generated(passport)
        assert result["components"]["ports"] == {"total": 12, "top": [3, 3, 3, 3]}
        assert result["interfaces"]["fan_out"]["top"] == [4, 4, 4]

    def test_an_unstated_port_top_is_placed_unevenly_when_the_unconnected_ports_need_it(
        self,
    ) -> None:
        # Two components, four ports, all four unconnected on one: only [4, 0] holds them.
        passport = without_identifiers(neutral_passport())
        passport["components"]["ports"] = {"total": 4}
        passport["components"]["unconnected"] = {"total": 4, "max_on_one": 4}
        passport["interfaces"]["fan_out"] = {"max": 4, "top": [4]}
        passport["connectors"]["assembly"] = 0
        passport["layers"]["connectors"] = 0
        passport["connectors"]["links"] = 0
        assert refused(passport) == []
        assert generated(passport)["components"]["ports"]["top"] == [4, 0]

    def test_an_unstated_fan_out_top_takes_more_interfaces_when_the_stars_need_them(
        self,
    ) -> None:
        # Six ports, three connectors between three pairs: three stars, so three interfaces of
        # two, where the fewest interfaces for a fan-out of 2 are already three; a fan-out max
        # of 3 makes the fewest two (3 + 3), which holds only two stars.
        passport = without_identifiers(neutral_passport())
        passport["layers"].update(swc=6, interfaces=3)
        passport["components"]["ports"] = {"total": 6, "top": [1, 1, 1, 1, 1, 1]}
        passport["interfaces"].update(sender_receiver=3, fan_out={"max": 2})
        passport["connectors"]["assembly"] = 3
        passport["layers"]["connectors"] = 3
        passport["connectors"]["links"] = 0
        assert refused(passport) == []
        shape = specimen_shape(stated_leaves(passport))
        assert isinstance(shape, Shape) and shape.fan_outs == (2, 2, 2)

    def test_a_links_conflict_with_an_unstated_count_keeps_its_class(self) -> None:
        # Two components of two ports each, no hub, no unconnected port, no link: the links
        # inequality allows at most one connector, and the four reached ports need two, whatever
        # count is chosen, so the stated leaves decide it alone.
        passport = neutral_passport()
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        passport["components"]["ports"] = {"total": 4, "top": [2, 2]}
        passport["interfaces"]["fan_out"] = {"max": 4, "top": [4]}
        passport["connectors"]["links"] = 0
        passport["identifiers"]["connectors"] = {"max_length": 9, "mean_length": 8.5}
        passport["identifiers"]["max_length"] = 9
        refusals = check_feasibility(validate_passport(passport))
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("connectors.links", "links")]
        assert "number between 0 and 1" in refusals[0].sentence
        assert "need at least 2" in refusals[0].sentence

    def test_a_refusal_with_a_derived_leaf_is_derived_and_says_so(self) -> None:
        # The witness of seed 25 with its port total and connector count dropped and a connector
        # name mean no count the construction fits at the totals tried meets. The total was
        # derived, so the refusal is `derived`, names the derived leaves, and quotes no connector
        # count of its own.
        passport = compute_passport(witness(25, 14))
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        del passport["components"]["ports"]["total"]
        passport["identifiers"]["connectors"] = {"mean_length": 11.593}
        refusals = check_feasibility(validate_passport(passport))
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("identifiers.connectors.mean_length", DERIVED)]
        sentence = refusals[0].sentence
        assert "components.ports.total derived as" in sentence
        assert "connectors.assembly derived" in sentence
        assert "at no connector count the construction could choose" in sentence
        assert "names hold between" not in sentence

    def test_a_count_the_stated_leaves_pin_keeps_the_identifier_class(self) -> None:
        # Two components, two ports, no unconnected port: the stated leaves admit one connector
        # only, and one name has its maximum as its mean, never 8.5. A stated A = 1 gives the same
        # refusal, so it is not the construction's.
        passport = neutral_passport()
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        passport["identifiers"]["connectors"] = {"max_length": 9, "mean_length": 8.5}
        passport["identifiers"]["max_length"] = 9
        expected = [("identifiers.connectors.mean_length", "identifier-mean")]
        assert refused(passport) == expected
        passport["connectors"]["assembly"] = 1
        passport["layers"]["connectors"] = 1
        assert refused(passport) == expected

    def test_one_component_with_one_reached_port_keeps_the_unconnected_class(self) -> None:
        # The stated fan-out top [3] fixes three ports, two of them unconnected: one is reached,
        # whatever total is derived, so the stated leaves decide it alone and it keeps its class.
        passport = without_identifiers(neutral_passport())
        passport["layers"].update(swc=1, connectors=0, hardware=2)
        del passport["components"]["ports"]
        passport["components"]["runnables"] = 1
        passport["components"]["unconnected"] = {"total": 2, "max_on_one": 2}
        passport["interfaces"]["fan_out"] = {"max": 3, "top": [3]}
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        assert refused(passport) == [("components.unconnected.total", "unconnected")]

    def test_with_every_leaf_stated_a_refusal_keeps_its_inequality_class(self) -> None:
        passport = neutral_passport()
        passport["connectors"]["assembly"] = 2
        passport["layers"]["connectors"] = 2
        assert refused(passport) == [
            ("connectors.assembly", "connectors"),
            ("connectors.links", "links"),
        ]

    def test_the_shape_is_shared_by_the_check_and_the_builder(self) -> None:
        passport = neutral_passport()
        assert specimen_shape(stated_leaves(passport)) is specimen_shape(stated_leaves(passport))


class TestAStatedConflictKeepsItsClass:
    """The conflicts the stated leaves decide alone are refused with their own classes.

    Whether or not another leaf is derived (on the topology specimen).
    """

    def test_b_more_ports_than_the_connectors_reach(self) -> None:
        # P = 400 > U + 2A = 12 + 200, every leaf of it stated.
        passport = topology()
        passport["connectors"]["assembly"] = 100
        passport["layers"]["connectors"] = 100
        assert refused(passport) == [("connectors.assembly", "connectors")]
        del passport["interfaces"]["fan_out"]["top"]
        assert refused(passport) == [("connectors.assembly", "connectors")]

    def test_d_more_unconnected_ports_than_ports(self) -> None:
        passport = topology()
        passport["components"]["unconnected"] = {"total": 500, "max_on_one": 30}
        assert refused(passport) == [("components.unconnected.max_on_one", "unconnected")]
        del passport["interfaces"]["fan_out"]["top"]
        # Over 400 ports, 500 unconnected also leave the 250 connectors no room.
        assert refused(passport) == [
            ("components.unconnected.max_on_one", "unconnected"),
            ("connectors.assembly", "connectors"),
        ]

    def test_l_a_short_fan_out_top_against_the_stated_total(self) -> None:
        passport = topology()
        passport["interfaces"]["fan_out"] = {"max": 60, "top": [60, 50]}
        assert refused(passport) == [("interfaces.fan_out.top", "fan-out")]
        del passport["components"]["ports"]["top"]
        assert refused(passport) == [("interfaces.fan_out.top", "fan-out")]
        assert "admits exactly 110" in check_feasibility(passport)[0].sentence

    def test_the_hubs_against_the_fan_outs_with_the_total_derived(self) -> None:
        # Ten hubs of 500 place 5000 ports; the full fan-out top and 16 interfaces hold at most
        # 270 + 8 x 20.
        passport = topology()
        passport["components"]["hubs"] = [500] * 10
        del passport["components"]["ports"]
        assert refused(passport) == [("interfaces.fan_out.top", "fan-out")]

    def test_the_connector_range_with_the_total_derived(self) -> None:
        passport = without_identifiers(neutral_passport())
        del passport["components"]["ports"]
        passport["connectors"]["assembly"] = 5
        passport["layers"]["connectors"] = 5
        assert refused(passport) == [
            ("connectors.assembly", "connectors"),
            ("connectors.links", "links"),
        ]


class TestAnUnstatedConnectorCount:
    def test_it_is_chosen_to_carry_the_stated_connector_name_mean(self) -> None:
        # The fewest connectors the construction fits for this witness, 13, have no total length
        # with the stated mean 10.571 and maximum 13; its own 14 do.
        full = validate_passport(compute_passport(witness(6, 8)))
        assert full["connectors"]["assembly"] == 14
        assert full["identifiers"]["connectors"] == {"max_length": 13, "mean_length": 10.571}
        partial = copy.deepcopy(full)
        del partial["connectors"]["assembly"]
        del partial["layers"]["connectors"]
        assert check_feasibility(partial) == []
        result = generated(partial)
        assert result["identifiers"]["connectors"] == {"max_length": 13, "mean_length": 10.571}

    def test_no_connector_is_not_chosen_when_a_connector_name_leaf_is_stated(self) -> None:
        # Two-component witnesses whose derived total came out at U once gave A = 0, and the stated
        # connector name leaves were refused as having no element.
        for seed, length in ((270, 9), (75, 8)):
            full = validate_passport(compute_passport(witness(seed, 2)))
            assert full["connectors"]["assembly"] == 1
            assert full["identifiers"]["connectors"] == {
                "max_length": length,
                "mean_length": float(length),
            }
            for kept in ("both", "max_length", "mean_length"):
                partial = copy.deepcopy(full)
                del partial["components"]["ports"]
                del partial["interfaces"]["fan_out"]["top"]
                del partial["connectors"]["assembly"]
                del partial["layers"]["connectors"]
                if kept != "both":
                    partial["identifiers"]["connectors"] = {kept: full["identifiers"]["connectors"][kept]}
                assert check_feasibility(partial) == [], (seed, kept)
                result = generated(partial)
                assert result["connectors"]["assembly"] >= 1
                for leaf, value in partial["identifiers"]["connectors"].items():
                    assert result["identifiers"]["connectors"][leaf] == value

    def test_with_no_reached_port_by_the_stated_leaves_a_name_leaf_keeps_its_class(
        self,
    ) -> None:
        # Every port is unconnected and the total is stated, so A = 0 is forced: both stated
        # connector name leaves are refused with their own class, as a stated A = 0 has them.
        passport = neutral_passport()
        passport["components"]["unconnected"] = {"total": 2, "max_on_one": 1}
        passport["connectors"]["links"] = 0
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        expected = [
            ("identifiers.connectors.max_length", "identifier-length"),
            ("identifiers.connectors.mean_length", "identifier-mean"),
        ]
        assert refused(passport) == expected
        stated_zero = copy.deepcopy(passport)
        stated_zero["connectors"]["assembly"] = 0
        stated_zero["layers"]["connectors"] = 0
        assert refused(stated_zero) == expected
        # The fan-out leaves still fix the total at the two ports, so A = 0 stays forced and the
        # classes are kept.
        del passport["components"]["ports"]
        assert refused(passport) == expected

    def test_a_count_only_the_links_admit_keeps_the_identifier_class(self) -> None:
        # The witness of seed 24 with its connector count dropped and a connector name mean of
        # 9.467. With no hub and 17 links the stated leaves admit A = 23 only, where no name total
        # has that mean; 30 would meet it, but no structure has 30. Unstated, A gives what a stated
        # A = 23 gives, and no stated A is met.
        passport = compute_passport(witness(24, 13))
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        passport["identifiers"]["connectors"] = {"mean_length": 9.467}
        expected = [("identifiers.connectors.mean_length", "identifier-mean")]
        assert refused(validate_passport(passport)) == expected
        for count in range(60):
            stated = copy.deepcopy(passport)
            stated["connectors"]["assembly"] = count
            stated["layers"]["connectors"] = count
            refusals = refused(validate_passport(stated))
            assert refusals, count
            if count == 23:
                assert refusals == expected

    def test_the_totals_follow_the_counts_whose_names_pass(self) -> None:
        # The witness of seed 112 with its port total and head, its fan-out head and its connector
        # count dropped, and a connector name mean of 8.375 that only a multiple of eight
        # connectors meets. The preferred total leaves too few reached ports for 24; the totals
        # tried are those that admit a count whose names pass, so the passport is met.
        passport = compute_passport(witness(112, 10))
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        del passport["components"]["ports"]
        del passport["interfaces"]["fan_out"]["top"]
        passport["identifiers"]["connectors"] = {"mean_length": 8.375}
        assert refused(validate_passport(passport)) == []
        result = generated(passport)
        assert result["connectors"]["assembly"] % 8 == 0
        assert result["identifiers"]["connectors"]["mean_length"] == 8.375

    def test_it_is_chosen_to_carry_a_stated_overall_maximum(self) -> None:
        # The connectors state their mean alone and every other layer its maximum, so the
        # connectors carry identifiers.max_length = 120; the fewest connectors the construction
        # fits, 194, cannot (a total in [120 + 7 x 193, 194 x 120] with the mean 7.5 needs at
        # least 226; `Conn194` is 7 long), which must not be refused as a stated inequality.
        passport = topology()
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        passport["identifiers"]["connectors"] = {"mean_length": 7.5}
        passport["identifiers"]["max_length"] = 120
        assert check_feasibility(passport) == []
        shape = specimen_shape(stated_leaves(passport))
        assert isinstance(shape, Shape)
        assert shape.connectors >= 226
        result = generated(passport)["identifiers"]
        assert result["max_length"] == 120
        assert result["connectors"] == {"max_length": 120, "mean_length": 7.5}

    def test_a_refusal_no_count_changes_keeps_its_class(self) -> None:
        # A connector maximum above the limit is refused at every count, and an overall maximum
        # the stated connector maximum keeps the connectors from carrying is refused whatever A.
        passport = topology()
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        passport["identifiers"]["connectors"] = {"max_length": 129}
        assert ("identifiers.connectors.max_length", "identifier-length") in refused(passport)
        passport["identifiers"]["connectors"] = {"max_length": 8}
        passport["identifiers"]["max_length"] = 120
        assert ("identifiers.max_length", "identifier-length") in refused(passport)


#: Connector name leaves (and the overall maximum) the stated leaves refuse at every connector
#: count, with the class a stated A gives them.
_COUNT_INDEPENDENT = [
    ({"max_length": 40}, 31, ("identifiers.max_length", "identifier-length")),
    ({"mean_length": 40.0}, 31, ("identifiers.max_length", "identifier-length")),
    (
        # Below 7 + 23 / n at every count: the other names cannot sit under their stem of 7.
        {"max_length": 30, "mean_length": 7.0},
        30,
        ("identifiers.connectors.mean_length", "identifier-mean"),
    ),
    (
        {"max_length": 20, "mean_length": 0},
        30,
        ("identifiers.connectors.mean_length", "identifier-mean"),
    ),
    ({"max_length": 0}, 30, ("identifiers.connectors.max_length", "identifier-length")),
    (
        {"max_length": 0, "mean_length": 9.0},
        30,
        ("identifiers.connectors.max_length", "identifier-length"),
    ),
    ({"mean_length": 0}, 30, ("identifiers.connectors.mean_length", "identifier-mean")),
    ({"max_length": 16, "mean_length": 8.5}, 24, ("identifiers.max_length", "identifier-length")),
]


@pytest.mark.parametrize(("connectors", "overall", "expected"), _COUNT_INDEPENDENT)
def test_a_refusal_every_count_gives_keeps_its_class_with_a_derived_count(
    connectors: dict[str, Any], overall: int, expected: tuple[str, str]
) -> None:
    # The topology specimen states its total, so A is unstated but at least one connector is
    # reached; the stated conflict must not be reported as `derived` at connectors.links.
    passport = topology()
    passport["identifiers"]["connectors"] = connectors
    passport["identifiers"]["max_length"] = overall
    stated_a = refused(validate_passport(copy.deepcopy(passport)))
    del passport["connectors"]["assembly"]
    del passport["layers"]["connectors"]
    derived_a = refused(validate_passport(passport))
    assert expected in stated_a
    assert derived_a == [expected]


def test_an_overall_maximum_below_a_stated_layer_names_it_with_a_derived_count() -> None:
    # With A derived, the connector lengths follow the count chosen; a stated layer whose names
    # also exceed the overall maximum is named instead.
    passport = topology()
    passport["identifiers"]["connectors"] = {"max_length": 40}
    passport["identifiers"]["max_length"] = 26
    del passport["connectors"]["assembly"]
    del passport["layers"]["connectors"]
    refusals = check_feasibility(validate_passport(passport))
    assert [(r.leaf, r.refusal_class) for r in refusals] == [("identifiers.max_length", "identifier-length")]
    assert refusals[0].sentence == "states 26; the interfaces names reach 30"


def test_the_backstop_relabels_only_a_refusal_some_count_avoids() -> None:
    refusal = Refusal("identifiers.connectors.mean_length", "identifier-mean", "states 8.5")
    passport = topology()
    del passport["connectors"]["assembly"]
    del passport["layers"]["connectors"]
    passport["identifiers"]["connectors"] = {"max_length": 30, "mean_length": 7.0}
    assert not connector_names(stated_leaves(passport)).avoidable(refusal)
    passport["identifiers"]["connectors"] = {"mean_length": 8.5}
    assert connector_names(stated_leaves(passport)).avoidable(refusal)


class TestTheConnectorSearchIsBounded:
    def test_the_large_scale_topology_without_its_total_and_count_walks_no_placements(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without components.ports.total and both connector count leaves, a hundred counts were
        # each once refused only after some 70,000 placements of the unconnected ports; the kept
        # reached ports of the largest hub skip them whole (`_solve_fixed`), so the placements
        # walked are at most one per count tried, not `VARIANT_STEPS` per pass.
        passport = copy.deepcopy(load_specimen("large-scale-topology").passport)
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        del passport["components"]["ports"]["total"]
        passes = count_calls(monkeypatch, connectors_module, "_solve_fixed")
        placements = count_yields(monkeypatch, connectors_module, "_unconnected_variants")
        clear_search_caches()
        refusals = check_feasibility(validate_passport(passport))
        assert passes["_solve_fixed"] > 1
        assert 1 <= placements["_unconnected_variants"] <= passes["_solve_fixed"]
        assert refusals == []


class TestTheCeiling:
    def test_a_count_above_the_ceiling_is_refused_before_any_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        edits: list[Callable[[dict[str, Any]], object]] = [
            lambda p: p["components"].update(ports={"total": 10**9}),
            lambda p: p["hardware"].update(cores=10**7),
            lambda p: p["layers"].update(swc=10**9),
            lambda p: p["connectors"].update(assembly=10**12),
            lambda p: p["components"].update(runnables=10**12),
            lambda p: p["components"].update(hubs=[10**6]),
            lambda p: p["components"].update(hubs=[COUNT_CEILING] * 10),
            lambda p: p["components"].update(hubs=[3] * (COUNT_CEILING + 1)),
        ]
        passports = []
        for edit in edits:
            passport = neutral_passport()
            edit(passport)
            passports.append(passport)
        for refusals in refused_before_construction(monkeypatch, passports):
            assert CEILING in {refusal.refusal_class for refusal in refusals}

    def test_the_least_total_the_connector_leaves_need_is_bounded_with_the_total_derived(
        self,
    ) -> None:
        passport = without_identifiers(neutral_passport())
        del passport["components"]["ports"]
        passport["components"]["unconnected"] = {"total": 9000, "max_on_one": 9000}
        passport["connectors"]["assembly"] = 3000
        passport["layers"]["connectors"] = 3000
        assert refused(passport) == [("connectors.assembly", CEILING)]

    def test_no_total_above_the_ceiling_is_derived(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # One hub of 10,000 among 10,000 components: the placement's own default total is near
        # 30,000, and only 10,000 is at or below the ceiling; it alone is tried, and refused.
        passport = without_identifiers(neutral_passport())
        passport["layers"].update(swc=COUNT_CEILING, interfaces=COUNT_CEILING)
        passport["interfaces"].update(sender_receiver=COUNT_CEILING, fan_out={"max": COUNT_CEILING})
        del passport["components"]["ports"]
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        passport["components"]["hubs"] = [COUNT_CEILING]
        passport["components"]["runnables"] = COUNT_CEILING
        tries = record_calls(monkeypatch, shape_module, "_try")
        clear_search_caches()
        refusals = check_feasibility(validate_passport(passport))
        assert [_tried_total(*call) for call in tries] == [COUNT_CEILING]
        assert [(r.leaf, r.refusal_class) for r in refusals] == [("connectors.links", DERIVED)]
        assert f"components.ports.total derived as {COUNT_CEILING}" in refusals[0].sentence
        assert "stop at the count ceiling" in refusals[0].sentence

    def test_the_shape_alone_states_placed_ports_above_the_ceiling_as_its_range(self) -> None:
        # Asked directly, the shape once stated an empty range ("between 20000 and 10000"); the
        # feasibility check refuses the hubs as ceiling first.
        passport = topology()
        passport["components"]["hubs"] = [COUNT_CEILING] * 2
        del passport["components"]["ports"]
        shape = specimen_shape(stated_leaves(passport))
        assert not isinstance(shape, Shape)
        assert [(r.leaf, r.refusal_class) for r in shape] == [("interfaces.fan_out.top", "fan-out")]
        assert shape[0].sentence.endswith(f"the port leaves admit exactly {2 * COUNT_CEILING}")
        assert ("components.hubs", CEILING) in refused(passport)

    def test_the_ceiling_itself_is_not_refused_as_ceiling(self) -> None:
        passport = neutral_passport()
        passport["components"]["runnables"] = COUNT_CEILING
        assert refused(passport) == []

    def test_a_large_total_on_two_components_is_decided_without_a_search(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The stated leaves refuse it alone (`_stated_refusals`), so no configuration is tried.
        passport = without_identifiers(neutral_passport())
        passport["components"]["ports"] = {"total": COUNT_CEILING}
        shapes = count_calls(monkeypatch, feasibility, "specimen_shape")
        searches = count_calls(monkeypatch, shape_module, "_search")
        clear_search_caches()
        check_feasibility(passport)
        assert shapes["specimen_shape"] == 1
        assert searches["_search"] == 0
        # The control: the same passport at its own port total is searched, so the counter is on the path.
        clear_search_caches()
        check_feasibility(without_identifiers(neutral_passport()))
        assert searches["_search"] >= 1


class TestIdentifiersOfARefusedLayer:
    def test_a_refused_task_count_leaves_its_identifier_leaves_unchecked(self) -> None:
        # Four busy tasks over the two stated runnables are refused `tasks`, so the task layer has
        # no count to check its lengths against.
        passport = neutral_passport()
        passport["layers"]["tasks"] = 4
        passport["identifiers"]["tasks"] = {"max_length": 28, "mean_length": 20.5}
        assert refused(passport) == [("layers.tasks", "tasks")]

    def test_a_refused_composition_count_leaves_its_identifier_leaves_unchecked(self) -> None:
        # layers.compositions is met, 0 refused layer-total.
        passport = neutral_passport()
        passport["layers"]["compositions"] = 0
        passport["identifiers"]["compositions"] = {"max_length": 28, "mean_length": 20.5}
        assert refused(passport) == [("layers.compositions", "layer-total")]

    def test_a_refused_hardware_count_leaves_the_hardware_identifiers_unchecked(self) -> None:
        passport = neutral_passport()
        passport["hardware"]["cores"] = 0
        passport["layers"]["hardware"] = 1
        assert refused(passport) == [("hardware.cores", "hardware-topology")]


class TestTheOverallMaximumOnAMeanOnlyLayer:
    def test_a_layer_stating_only_its_mean_carries_the_overall_maximum(self) -> None:
        # Every met layer states a length but the components, which state their mean alone:
        # 7.5 over two components holds one name of 8 and one of 7.
        passport = neutral_passport()
        passport["identifiers"]["swc"] = {"mean_length": 7.5}
        passport["identifiers"]["max_length"] = 8
        passport["identifiers"]["connectors"] = {"max_length": 8, "mean_length": 8.0}
        passport["identifiers"]["hardware"] = {"max_length": 8, "mean_length": 7.5}
        assert refused(passport) == []
        passport = copy.deepcopy(passport)
        passport["identifiers"]["max_length"] = 9
        passport["identifiers"]["swc"] = {"mean_length": 8.0}
        assert refused(passport) == []
        result = generated(passport)["identifiers"]
        assert result["swc"] == {"max_length": 9, "mean_length": 8.0}
        assert result["max_length"] == 9

    def test_no_layer_can_carry_it_when_every_mean_forbids(self) -> None:
        passport = neutral_passport()
        passport["identifiers"]["swc"] = {"mean_length": 7.0}
        passport["identifiers"]["max_length"] = 20
        assert refused(passport) == [("identifiers.max_length", "identifier-length")]


def _unstated_count(swc: int, interfaces: int, links: int) -> dict[str, Any]:
    passport = without_identifiers(neutral_passport())
    del passport["connectors"]["assembly"]
    del passport["layers"]["connectors"]
    passport["layers"].update(swc=swc, interfaces=interfaces)
    passport["interfaces"]["sender_receiver"] = interfaces
    passport["connectors"]["links"] = links
    return passport


def _room_on_fixed_counts() -> dict[str, Any]:
    # `top` fixes every count, [3, 2, 1, 1], so at most 2 + 2 + 1 + 1 = 6 ports can be unconnected
    # at two on one, not n x u1 = 8.
    passport = _unstated_count(4, 2, 0)
    passport["components"]["ports"] = {"top": [3, 2, 1, 1]}
    passport["components"]["unconnected"] = {"total": 7, "max_on_one": 2}
    passport["interfaces"]["fan_out"] = {"max": 4}
    return passport


def _ones_above_the_unconnected() -> dict[str, Any]:
    # A full fan-out top [3, 1, 1, 1] names three interfaces of fan-out 1, each an unconnected port,
    # beside two unconnected ports.
    passport = _unstated_count(6, 4, 0)
    passport["components"]["ports"] = {"top": [1] * 6}
    passport["components"]["unconnected"] = {"total": 2, "max_on_one": 1}
    passport["interfaces"]["fan_out"] = {"max": 3, "top": [3, 1, 1, 1]}
    return passport


def _links_above_the_capacity() -> dict[str, Any]:
    # Four fan-outs of 2 hold four stars, so at most four connectors; with no hub and L = 3 the
    # links inequality fixes A = 4 // 2 + 3 = 5.
    passport = _unstated_count(4, 4, 3)
    passport["components"]["ports"] = {"top": [2, 2, 2, 2]}
    passport["interfaces"]["fan_out"] = {"max": 2, "top": [2, 2, 2, 2]}
    return passport


def _stated_count_above_the_capacity() -> dict[str, Any]:
    # A stated A = 3 beside a full fan-out top [2, 2] (at most two connectors), the total
    # derived.
    passport = _unstated_count(2, 2, 2)
    passport["connectors"]["assembly"] = 3
    passport["layers"]["connectors"] = 3
    passport["components"]["ports"] = {}
    passport["interfaces"]["fan_out"] = {"max": 2, "top": [2, 2]}
    return passport


def _one_reached_port() -> dict[str, Any]:
    # The fan-out top [3] fixes three ports, two of them unconnected: one is reached, whatever
    # the derivation chooses.
    passport = _unstated_count(2, 1, 0)
    passport["components"]["ports"] = {}
    passport["components"]["unconnected"] = {"total": 2, "max_on_one": 2}
    passport["interfaces"]["fan_out"] = {"max": 3, "top": [3]}
    return passport


def _links_above_the_ordinary_ports() -> dict[str, Any]:
    # A hub of 10 in twelve ports leaves the two ordinary components two ports, which hold at
    # most one connector between them; L = 1 needs 2 // 2 + 1 = 2.
    passport = _unstated_count(3, 2, 1)
    passport["components"]["ports"] = {}
    passport["components"]["hubs"] = [10]
    passport["interfaces"]["fan_out"] = {"max": 6, "top": [6, 6]}
    return passport


def _hub_ends_above_the_hub_connectors() -> dict[str, Any]:
    # Two hubs of 10 keep 20 reached ports; with A = 12 and L = 1, at most 12 - 3 = 9 connectors
    # have a hub end, 18 ends in all.
    passport = _unstated_count(6, 8, 1)
    passport["connectors"]["assembly"] = 12
    passport["layers"]["connectors"] = 12
    passport["components"]["ports"] = {}
    passport["components"]["hubs"] = [10, 10]
    passport["interfaces"]["fan_out"] = {"max": 4}
    return passport


def _kept_above_the_capacity() -> dict[str, Any]:
    # A hub of 9 keeps nine reached ports, each with a connector of its own; the full fan-out
    # top [3, 3, 3, 2] holds at most 11 - 4 = 7.
    passport = _unstated_count(3, 4, 0)
    passport["components"]["ports"] = {"top": [9, 1, 1]}
    passport["components"]["hubs"] = [9]
    passport["interfaces"]["fan_out"] = {"max": 3, "top": [3, 3, 3, 2]}
    return passport


def _kept_above_the_reached() -> dict[str, Any]:
    # A component of two ports holds both reached ports: each needs a connector to another
    # component's reached port, and there is none, whatever count is chosen.
    passport = _unstated_count(2, 1, 0)
    passport["components"]["ports"] = {"total": 2, "top": [2, 0]}
    passport["interfaces"]["fan_out"] = {"max": 2, "top": [2]}
    return passport


def _links_with_no_reached_port() -> dict[str, Any]:
    # One port, unconnected: no connector, and with no hub L = 2 needs 2 // 2 + 2 = 3.
    passport = _unstated_count(2, 1, 2)
    passport["components"]["ports"] = {"total": 1, "top": [1, 0]}
    passport["components"]["unconnected"] = {"total": 1, "max_on_one": 1}
    passport["interfaces"]["fan_out"] = {"max": 1}
    return passport


def _stated_total_at_the_ceiling() -> dict[str, Any]:
    # A stated total of exactly the count ceiling decides the most total, so the full fan-out top
    # [5000, 5000] (at most 9,998 connectors) refuses A = 9,999 although `top` is derived.
    passport = _unstated_count(2, 2, 9998)
    passport["connectors"]["assembly"] = 9999
    passport["layers"]["connectors"] = 9999
    passport["components"]["ports"] = {"total": COUNT_CEILING}
    passport["components"]["unconnected"] = {"total": 0, "max_on_one": 0}
    passport["interfaces"]["fan_out"] = {"max": 5000, "top": [5000, 5000]}
    return passport


def _zero_fan_out_with_no_port() -> dict[str, Any]:
    # No component port, and the fan-out top [0] names an interface no port names; the zero-port
    # path once returned before the check and generation raised.
    passport = _unstated_count(2, 1, 0)
    passport["components"]["ports"] = {"total": 0, "top": [0, 0]}
    passport["interfaces"]["fan_out"] = {"max": 0, "top": [0]}
    return passport


def _zero_fan_out_with_the_ports_derived() -> dict[str, Any]:
    # The same top [0] with the port leaves unstated: the fan-out leaves alone fix no port.
    passport = _zero_fan_out_with_no_port()
    passport["components"]["ports"] = {}
    return passport


def _fan_out_top_above_the_declared() -> dict[str, Any]:
    # The fan-out top [2, 2] names two interfaces; one is declared.
    passport = _unstated_count(3, 1, 1)
    passport["components"]["ports"] = {"top": [2, 1, 1]}
    passport["interfaces"]["fan_out"] = {"max": 2, "top": [2, 2]}
    return passport


def _fan_out_max_with_no_interface() -> dict[str, Any]:
    # A positive fan-out max names an interface; none is declared (the port total derived).
    passport = _unstated_count(2, 0, 0)
    passport["components"]["ports"] = {}
    passport["components"]["unconnected"] = {"total": 1, "max_on_one": 1}
    passport["interfaces"]["fan_out"] = {"max": 1}
    passport["layers"].update(implementation_types=0, base_types=0)
    return passport


#: Conflicts the stated leaves decide alone that must not be reported as `derived`, each with the
#: class and the leaf a closed-form inequality gives it.
_STATED_CONFLICTS = [
    (_room_on_fixed_counts, ("components.unconnected.max_on_one", "unconnected")),
    (_ones_above_the_unconnected, ("interfaces.fan_out.top", "fan-out")),
    (_links_above_the_capacity, ("connectors.links", "links")),
    (_stated_count_above_the_capacity, ("connectors.assembly", "connectors")),
    (_one_reached_port, ("components.unconnected.total", "unconnected")),
    (_links_above_the_ordinary_ports, ("connectors.links", "links")),
    (_hub_ends_above_the_hub_connectors, ("connectors.links", "links")),
    (_kept_above_the_capacity, ("interfaces.fan_out.top", "fan-out")),
    (_kept_above_the_reached, ("connectors.links", "connectors")),
    (_links_with_no_reached_port, ("connectors.links", "links")),
    (_stated_total_at_the_ceiling, ("connectors.assembly", "connectors")),
    (_zero_fan_out_with_no_port, ("interfaces.fan_out.top", "fan-out")),
    (_zero_fan_out_with_the_ports_derived, ("interfaces.fan_out.top", "fan-out")),
    (_fan_out_top_above_the_declared, ("interfaces.fan_out.top", "fan-out")),
    (_fan_out_max_with_no_interface, ("interfaces.fan_out.max", "fan-out")),
]


@pytest.mark.parametrize(("make", "expected"), _STATED_CONFLICTS)
def test_a_conflict_the_stated_leaves_decide_alone_keeps_its_class(
    make: Callable[[], dict[str, Any]], expected: tuple[str, str]
) -> None:
    refusals = check_feasibility(validate_passport(make()))
    assert [(r.leaf, r.refusal_class) for r in refusals] == [expected]
    assert "derived" not in refusals[0].sentence


def test_the_bounds_of_the_stated_conflicts_are_reached_by_met_passports() -> None:
    # The forward direction at each new bound's edge: one unconnected port fewer than the room,
    # one fewer interface of fan-out 1, and L one less than the capacity allows.
    passport = _room_on_fixed_counts()
    passport["components"]["unconnected"] = {"total": 5, "max_on_one": 2}
    assert refused(passport) == []
    passport = _ones_above_the_unconnected()
    passport["components"]["unconnected"] = {"total": 3, "max_on_one": 1}
    assert refused(passport) == []
    passport = _links_above_the_capacity()
    passport["connectors"]["links"] = 2
    assert refused(passport) == []


class TestTheRangeOverConnectorReachedPorts:
    """The shape's closed forms in R_c.

    A stated A admits U + A + 1 to U + 2A + D totals (up to D reached ports delegated), a count a
    the reached ports R when some R_c from R - D to R lies in [a + 1, 2a], and with delegations no
    kept reached port forces a connector.
    """

    def test_the_connector_range_widens_by_d(self) -> None:
        assert _connector_range(ConnectorSpec(2, 1, 3, None, 0), 0) == (6, 8)
        assert _connector_range(ConnectorSpec(2, 1, 3, None, 0), 4) == (6, 12)
        assert _connector_range(ConnectorSpec(2, 1, 0, None, 0), 3) == (2, 5)
        assert _connector_range(ConnectorSpec(2, 1, None, None, 0), 3) is None

    def test_the_count_range_widens_by_d(self) -> None:
        assert _count_range(2, [3], 0) == (6, 8)
        assert _count_range(2, [0, 3], 4) == (2, 12)

    def test_a_count_is_admitted_over_r_c(self) -> None:
        # R = 10: a = 3 needs R_c in [4, 6], so D >= 4.
        assert not _admits_choosable(12, 2, [3])
        assert not _admits_choosable(12, 2, [3], 3)
        assert _admits_choosable(12, 2, [3], 4)
        # No connector: every reached port delegated.
        assert _admits_choosable(4, 2, [0], 2)
        assert not _admits_choosable(4, 2, [0], 1)

    def _six(self, delegations: int) -> dict[str, Any]:
        passport = without_identifiers(neutral_passport())
        passport["layers"].update(swc=6, interfaces=3, compositions=3)
        del passport["layers"]["connectors"]
        passport["components"]["ports"] = {"top": [2, 2, 2, 2, 2, 2]}
        passport["components"]["unconnected"] = {"total": 2, "max_on_one": 1}
        passport["components"]["runnables"] = 6
        passport["interfaces"] = {
            "sender_receiver": 3,
            "client_server": 0,
            "fan_out": {"max": 4, "top": [4, 4, 4]},
        }
        passport["connectors"] = {"delegation": delegations, "links": 0}
        return passport

    def test_more_delegations_than_the_stated_leaves_admit_are_refused_first(self) -> None:
        # A derived: at most R = 10 reached ports and C = 3 compositions, so at most 30.
        found = check_feasibility(self._six(31))
        assert [(r.leaf, r.refusal_class) for r in found] == [("connectors.delegation", "delegations")]
        # A unstated: the sentence names no connector count the passport never gave.
        assert found[0].sentence == ("states 31; the at most 10 reached ports and 3 compositions admit at most 30")

    def test_the_stated_delegation_bound_is_met_at_its_edge(self) -> None:
        # D = 30 = C x R: no connector, every reached port delegated and stepped to the root.
        assert refused(self._six(30)) == []
        assert generated(self._six(30))["connectors"]["delegation"] == 30

    def test_with_a_stated_count_the_bound_reads_the_connector_reached_ports(self) -> None:
        # A = 3 stated, R = 10, C = 3: R_c > A, so D_deep <= R - A - 1 = 6 and D <= 3 x 7 - 2 = 19,
        # the exact most (`TestTheInequality` in the delegation tests), not C x R - A = 27. The
        # fan-outs beyond `max` are left derived, so the stated conflicts are looked for first.
        passport = self._six(20)
        passport["connectors"]["assembly"] = 3
        passport["layers"]["connectors"] = 3
        del passport["interfaces"]["fan_out"]["top"]
        found = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in found] == [("connectors.delegation", "delegations")]
        assert found[0].sentence == (
            "states 20; the at most 10 reached ports, 3 compositions and 3 connectors admit at most 19"
        )
        passport["connectors"]["delegation"] = 19
        assert refused(passport) == []

    @pytest.mark.parametrize(
        ("passport", "delegations"),
        [(_kept_above_the_capacity, 5), (_kept_above_the_reached, 2)],
        ids=["kept-above-the-capacity", "kept-above-the-reached"],
    )
    def test_a_kept_conflict_is_lifted_by_delegations(
        self, passport: Callable[[], dict[str, Any]], delegations: int
    ) -> None:
        # Without delegations each is refused for its kept reached ports (`_STATED_CONFLICTS`); with
        # them those ports may be delegation-only, so the kept checks are skipped and the passport
        # is met.
        vector = passport()
        assert refused(vector) != []
        vector["connectors"]["delegation"] = delegations
        assert refused(vector) == []
        assert generated(vector)["connectors"]["delegation"] == delegations

    def test_delegations_with_the_count_derived_are_met(self) -> None:
        passport = self._six(10)
        assert refused(passport) == []
        result = generated(passport)
        assert result["connectors"]["delegation"] == 10
        assert result["components"]["unconnected"] == {"total": 2, "max_on_one": 1}


# --- the newer classes, the order and the ceiling ----------------------------------------------

#: The seven count leaves of the newer shapes, now `met`.
NEW_COUNT_LEAVES = (
    "layers.application_types",
    "layers.compositions",
    "layers.mapping_sets",
    "layers.units",
    "layers.compu_methods",
    "layers.data_constraints",
    "connectors.delegation",
)
TYPE_LAYERS = ("application_types", "mapping_sets", "units", "compu_methods", "data_constraints")


def _delegated(delegations: int, compositions: int = 3) -> dict[str, Any]:
    """Six components of two ports, U = 2, three SR interfaces of fan-out 4, A = 3.

    R = 10, so with three compositions D is admitted from 4 to 12 (the delegation tests' vector).
    """
    passport = neutral_passport()
    passport["identifiers"] = {"max_length": 8}
    passport["layers"].update(swc=6, interfaces=3, connectors=3, compositions=compositions)
    passport["components"]["ports"] = {"total": 12, "top": [2, 2, 2, 2, 2, 2]}
    passport["components"]["unconnected"] = {"total": 2, "max_on_one": 1}
    passport["components"]["runnables"] = 6
    passport["interfaces"] = {
        "sender_receiver": 3,
        "client_server": 0,
        "fan_out": {"max": 4, "top": [4, 4, 4]},
    }
    passport["connectors"] = {"assembly": 3, "delegation": delegations, "links": 0}
    return passport


class TestTheNewClassesInLeafTableOrder:
    def test_a_delegation_refusal_sits_between_the_layers_and_the_hardware(self) -> None:
        passport = _delegated(20)
        # Seven busy tasks over the six stated runnables.
        passport["layers"]["tasks"] = 7
        passport["layers"]["hardware"] = 1
        passport["hardware"]["cores"] = 0
        assert refused(passport) == [
            ("layers.tasks", "tasks"),
            ("connectors.delegation", "delegations"),
            ("hardware.cores", "hardware-topology"),
        ]

    def test_a_refused_composition_count_leaves_the_delegations_undecided(self) -> None:
        # D = 4 is admitted with one composition and with three; the delegation bound reads C,
        # so with C refused (0) it is not decided against a count the passport cannot have.
        assert refused(_delegated(4, compositions=1)) == []
        assert refused(_delegated(4, compositions=0)) == [("layers.compositions", "layer-total")]
        assert refused(_delegated(20, compositions=0)) == [("layers.compositions", "layer-total")]

    def test_a_composition_refusal_comes_before_an_identifier_refusal(self) -> None:
        passport = neutral_passport()
        passport["layers"]["compositions"] = 0
        passport["identifiers"]["swc"] = {"max_length": 7, "mean_length": 3.0}
        assert refused(passport) == [
            ("layers.compositions", "layer-total"),
            ("identifiers.swc.mean_length", "identifier-mean"),
        ]


class TestTheCeilingOverTheNewLeaves:
    @pytest.mark.parametrize("leaf", NEW_COUNT_LEAVES)
    def test_a_new_count_above_the_ceiling_is_refused_ceiling_alone(
        self, leaf: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        passport = neutral_passport()
        group, key = leaf.split(".")
        passport[group][key] = COUNT_CEILING + 1
        [refusals] = refused_before_construction(monkeypatch, [passport])
        assert [(r.leaf, r.refusal_class) for r in refusals] == [(leaf, CEILING)]

    @pytest.mark.parametrize("leaf", NEW_COUNT_LEAVES[:-1])
    def test_a_new_layer_at_the_ceiling_is_not_refused_ceiling(self, leaf: str) -> None:
        passport = neutral_passport()
        layer = leaf.split(".")[1]
        passport["layers"][layer] = COUNT_CEILING
        del passport["identifiers"][layer]
        assert CEILING not in {refusal_class for _, refusal_class in refused(passport)}

    def test_the_mapping_rows_are_not_derived_above_the_ceiling(self) -> None:
        # The rows number the application types (one value type present): at the ceiling the
        # rows are the ceiling, a stated count, and nothing is refused.
        passport = neutral_passport()
        passport["layers"].update(application_types=COUNT_CEILING, mapping_sets=3)
        del passport["identifiers"]["application_types"]
        del passport["identifiers"]["mapping_sets"]
        assert refused(passport) == []

    def test_the_built_products_equal_their_stated_counts(self) -> None:
        # The boundary ports number the delegations (one each) and the mapping rows the application
        # types: neither product exceeds a count the ceiling already bounds.
        passport = _delegated(9)
        passport["layers"].update(application_types=7, mapping_sets=2)
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        delegations = sum(len(c.delegations) for c in project.compositions)
        boundary_ports = sum(len(c.ports) for c in project.compositions)
        rows = sum(len(mapping.maps) for mapping in project.mapping_sets)
        assert delegations == boundary_ports == 9
        assert rows == 7


class TestTheNewLayersCountLeaves:
    def test_every_met_identifier_layer_names_its_count_leaves(self) -> None:
        assert set(feasibility._COUNT_LEAVES) == MET_IDENTIFIER_LAYERS
        for layer in TYPE_LAYERS:
            assert feasibility._COUNT_LEAVES[layer] == frozenset({f"layers.{layer}"})


# --- the entry planner's switch -----------------------------------------------------------------

DELEGATION_LEAF = "connectors.delegation"

#: The preferred-candidate fields of the fixtures with delegations: U on the first unconnected
#: placement, R_c (the top of the ascending interval), the root members, the connector-free
#: components, their delegation-only ports and D.
PREFERRED_FIELDS = {
    "shapes": (12, 358, 29, (0,), 30, 160),
    "chain-empty": (0, 2, 2, (), 0, 1),
    "large-scale-shapes": (290, 2100, 79, (1,), 360, 3800),
}


@pytest.fixture
def fresh_shapes() -> Iterator[None]:
    """The shape cache cleared before and after a test that monkeypatches what it computes."""
    shape_module._shape_cached.cache_clear()
    yield
    shape_module._shape_cached.cache_clear()


def _seed_501(delegations: int = 6) -> dict[str, Any]:
    """Seed 501's shaped witness (counts 12, 5, 2; R = 10, A = 3, C = 4), every leaf stated."""
    passport = validate_passport(compute_passport(shaped_witness(501, 3)))
    passport["connectors"]["delegation"] = delegations
    return passport


def _seed_501_configuration(delegations: int = 6) -> Configuration:
    stated = stated_leaves(_seed_501(delegations))
    counts = place_ports(port_spec(stated))
    fan_outs = split_fan_outs(stated, sum(counts), None)
    assert not isinstance(fan_outs, Refusal)
    return configuration_of(stated, counts, fan_outs)


def _counterexample() -> dict[str, Any]:
    """A counterexample as a passport: two stars of three ports cannot share (4, 2).

    Counts (3, 3), SR fan-outs (4, 2), U = 0, A = 4, L = 3, D = 1, C = 1.
    """
    passport = neutral_passport()
    passport["layers"].update(swc=2, interfaces=2, connectors=4, compositions=1)
    passport["components"]["ports"] = {"total": 6, "top": [3, 3]}
    passport["components"]["unconnected"] = {"total": 0, "max_on_one": 0}
    passport["interfaces"] = {
        "sender_receiver": 2,
        "client_server": 0,
        "fan_out": {"max": 4, "top": [4, 2]},
    }
    passport["connectors"] = {"assembly": 4, "delegation": 1, "links": 3}
    return passport


def _no_layout(*_: Any, **__: Any) -> Planned:
    return Planned(None, Trace(relaxed=False))


def _no_layout_asked(*_: Any, **__: Any) -> Planned:
    raise AssertionError("the planner was asked of a configuration the layout bounds refuse")


class TestThePreferredCandidate:
    @pytest.mark.parametrize("name", sorted(PREFERRED_FIELDS))
    def test_a_fixture_with_delegations_keeps_the_preferred_candidate(self, name: str) -> None:
        stated = stated_leaves(load_specimen(name).passport)
        shape = specimen_shape(stated)
        assert isinstance(shape, Shape)
        layout = shape.layout
        assert (layout.source, layout.step) == ("preferred", 0)
        unconnected, connected, root, deep, deep_delegated, delegations = PREFERRED_FIELDS[name]
        first = next(unconnected_placements(construction_spec(stated), shape.counts))
        assert layout.unconnected == first and sum(first) == unconnected
        reached = [c - u for c, u in zip(shape.counts, first, strict=True)]
        entry = EntryOrder.of(reached)
        spec = construction_spec(stated)
        ports = sum(connectable_fan_outs(stated, shape.fan_outs))
        compositions = int(stated.get("layers.compositions", 1))
        top = admitted_reach(entry, spec.connectors, delegations, compositions, ports)[0]
        assert layout.connector_reached == top == connected
        assert len(layout.root) == root and layout.deep == deep
        assert layout.deep_delegated == deep_delegated and layout.delegations == delegations
        # The ascending split at that R_c: its entered components are the root members.
        split = entry.split(connected)
        assert (layout.root, layout.deep) == (split.entered, split.connector_free)
        assert layout.delegated == split.delegated

    @pytest.mark.parametrize("name", ["minimal", "topology", "large-scale-topology"])
    def test_a_fixture_without_delegations_has_no_planned_layout(self, name: str) -> None:
        shape = specimen_shape(stated_leaves(load_specimen(name).passport))
        assert isinstance(shape, Shape)
        assert (shape.layout.source, shape.layout.step) == ("none", 0)
        assert shape.layout.delegations == 0 and shape.layout.deep == ()
        assert shape.layout.root == tuple(range(len(shape.counts)))

    def test_it_takes_today_s_search_and_most_and_the_planner_is_asked_only_where_it_fails(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        # Two copies of the first configuration: the preferred candidate refused on the first
        # (the planner finding nothing there), built on the second with the quick search.
        passport = _delegated(10, compositions=2)
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        original_configurations = shape_module._configurations
        original_solve_open = solve_open

        def twice(*args: Any) -> Iterator[Any]:
            first = next(original_configurations(*args))
            yield from (first, first)

        calls: list[tuple[Any, Any]] = []

        def spied(spec: Any, counts: Any, units: Any, fan_outs: Any, search: Any, most: Any) -> Any:
            calls.append((search, most))
            if len(calls) == 1:
                return None
            return original_solve_open(spec, counts, units, fan_outs, search, most)

        planned: list[Configuration] = []

        def no_layout(configuration: Configuration, *_: Any, **__: Any) -> Planned:
            planned.append(configuration)
            return Planned(None, Trace(relaxed=False))

        monkeypatch.setattr(shape_module, "_configurations", twice)
        monkeypatch.setattr(shape_module, "solve_open", spied)
        monkeypatch.setattr(layout_module, "plan", no_layout)
        shape = specimen_shape(stated_leaves(passport))
        assert isinstance(shape, Shape) and shape.layout.source == "preferred"
        assert [search for search, _ in calls] == [FULL_SEARCH, QUICK_SEARCH]
        assert len(planned) == 1
        reached = [c - u for c, u in zip(shape.counts, shape.layout.unconnected, strict=True)]
        most = most_connectors(EntryOrder.of(reached), 10, shape.layout.connector_reached, 2)
        assert [m for _, m in calls] == [most, most]

    def test_when_it_builds_the_planner_is_never_called(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        def refused(*_: Any, **__: Any) -> Planned:
            raise AssertionError("the planner ran beside a preferred candidate that builds")

        monkeypatch.setattr(layout_module, "plan", refused)
        shape = specimen_shape(stated_leaves(load_specimen("shapes").passport))
        assert isinstance(shape, Shape) and shape.layout.source == "preferred"


class TestTheNoLayoutRefusal:
    def test_seed_501_at_twenty_delegations_has_no_layout(self) -> None:
        found = check_feasibility(_seed_501(20))
        assert [(r.leaf, r.refusal_class) for r in found] == [(DELEGATION_LEAF, NO_LAYOUT)]
        expected = no_layout_refusal(_seed_501_configuration(20), 1)
        assert found[0].sentence == expected.sentence
        assert found[0].sentence.endswith("(1 tried)")

    @pytest.mark.parametrize(("delegations", "edge"), [(3, "least"), (26, "most")])
    def test_the_delegations_bounds_of_every_layout_with_every_leaf_stated(self, delegations: int, edge: str) -> None:
        found = check_feasibility(_seed_501(delegations))
        assert [(r.leaf, r.refusal_class) for r in found] == [(DELEGATION_LEAF, DELEGATIONS)]
        assert found[0].sentence == (
            f"states {delegations}; with 10 reached ports, 3 connectors and 4 compositions the "
            "delegations number at least 4 and at most 25"
        )

    def test_the_ceiling_case_is_refused_before_any_placement_is_drawn(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        # 5 hubs of 1,000 ports and 4,995 of one, U = 3,000 (at most 1,000 on one): R = 6,995;
        # A = 4,000, C = 2 admit at most 2 x 2,995 - 1 = 5,989 delegations.
        def drawn(*_: Any) -> Iterator[tuple[int, ...]]:
            raise AssertionError("an unconnected placement was drawn")

        monkeypatch.setattr(shape_module, "unconnected_placements", drawn)
        passport = without_identifiers(neutral_passport())
        passport["layers"].update(swc=5000, interfaces=100, connectors=4000, compositions=2)
        passport["components"]["ports"] = {"total": 9995, "top": [1000] * 5 + [1, 1, 1]}
        passport["components"]["hubs"] = [1000] * 5
        passport["components"]["unconnected"] = {"total": 3000, "max_on_one": 1000}
        passport["components"]["runnables"] = 5000
        passport["interfaces"] = {
            "sender_receiver": 100,
            "client_server": 0,
            "fan_out": {"max": 100, "top": [100] * 8},
        }
        passport["connectors"] = {"assembly": 4000, "delegation": 6000, "links": 0}
        found = specimen_shape(stated_leaves(passport))
        assert isinstance(found, list)
        assert [(r.leaf, r.refusal_class) for r in found] == [(DELEGATION_LEAF, DELEGATIONS)]
        assert "at most 5989" in found[0].sentence

    def test_with_the_total_derived_the_refusal_is_derived_with_the_budget_clause(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        passport = _delegated(10)
        del passport["components"]["ports"]
        monkeypatch.setattr(shape_module, "solve_open", lambda *_: None)
        monkeypatch.setattr(layout_module, "plan", _no_layout)
        found = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in found] == [(DELEGATION_LEAF, DERIVED)]
        sentence = found[0].sentence
        assert "no layout of the connectors and the delegations exists" in sentence
        assert derived_total_clause(SHAPE_BUDGET) in sentence
        assert "components.ports.total derived as 12" in sentence

    def test_the_budget_counts_configurations_only(self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None) -> None:
        original = shape_module._configurations

        def many(*args: Any) -> Iterator[Any]:
            first = next(original(*args))
            for _ in range(3 * SHAPE_BUDGET):
                yield first

        calls: list[Configuration] = []

        def counted(configuration: Configuration, *_: Any, **__: Any) -> Planned:
            calls.append(configuration)
            return Planned(None, Trace(relaxed=False))

        monkeypatch.setattr(shape_module, "_configurations", many)
        monkeypatch.setattr(layout_module, "plan", counted)
        found = specimen_shape(stated_leaves(_seed_501()))
        assert isinstance(found, list) and len(calls) == SHAPE_BUDGET
        assert found[0].refusal_class == NO_LAYOUT
        assert found[0].sentence.endswith(f"({SHAPE_BUDGET} tried)")

    def test_no_layout_sits_in_leaf_table_order_after_a_task_refusal(self) -> None:
        passport = _seed_501(20)
        # More busy tasks than the stated runnables.
        passport["layers"]["tasks"] = passport["components"]["runnables"] + 1
        assert refused(passport) == [
            ("layers.tasks", "tasks"),
            (DELEGATION_LEAF, NO_LAYOUT),
        ]

    def test_the_counterexample_passport_has_no_layout_after_all_three_steps(self) -> None:
        passport = _counterexample()
        found = check_feasibility(passport)
        assert [(r.leaf, r.refusal_class) for r in found] == [(DELEGATION_LEAF, NO_LAYOUT)]
        assert found[0].sentence.endswith("(1 tried)")
        stated = stated_leaves(passport)
        configuration = configuration_of(stated, (3, 3), (4, 2))
        planned = layout_module.plan(configuration)
        assert planned.layout is None
        assert planned.trace == Trace(relaxed=True, builder=None, exact=False)


def _three_tails() -> dict[str, Any]:
    """Seed 501 reshaped so that its one total has exactly three fan-out tails the planner tells apart.

    Every leaf is stated: four components of 24 ports, 14 interfaces (11 SR + CS), and a fan-out
    `top` of 19 ending at 2, so the 5 ports beyond it spread over 3, 4 or 5 interfaces, the
    SR + CS slots' fan-outs (2, 2, 1), (2, 1, 1) and (1, 1, 1).
    """
    passport = _seed_501()
    passport["layers"].update(swc=4, interfaces=14)
    passport["interfaces"]["client_server"] = 8
    passport["components"]["ports"] = {"total": 24, "top": [12, 6, 4, 2]}
    passport["components"]["unconnected"]["total"] = 12
    passport["interfaces"]["fan_out"] = {"max": 5, "top": [5, 2, 2, 2, 2, 2, 2, 2]}
    passport["identifiers"]["swc"]["mean_length"] = 8.5
    passport["identifiers"]["interfaces"]["mean_length"] = 8.714
    return validate_passport(passport)


def _guard_the_three_tails(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_configurations` guarded to yield exactly the three configurations of `_three_tails`.

    One total, three distinct fan-out tails (3, 4 and 5 interfaces beyond the head).
    """
    original = shape_module._configurations

    def three(*args: Any) -> Iterator[Any]:
        configurations = list(original(*args))
        assert [parts for _, _, parts in configurations] == [3, 4, 5]
        yield from configurations

    monkeypatch.setattr(shape_module, "_configurations", three)


class TestTheConfigurations:
    def test_a_layout_on_the_third_tail_alone_builds_the_passport_from_it(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        real_plan = layout_module.plan
        calls: list[Configuration] = []

        def third(configuration: Configuration, *args: Any, **kwargs: Any) -> Planned:
            calls.append(configuration)
            if len(calls) < 3:
                return Planned(None, Trace(relaxed=False))
            return real_plan(configuration, *args, **kwargs)

        _guard_the_three_tails(monkeypatch)
        monkeypatch.setattr(layout_module, "plan", third)
        passport = _three_tails()
        stated = stated_leaves(passport)
        shape = specimen_shape(stated)
        assert isinstance(shape, Shape) and shape.layout.source == "solver"
        assert len(calls) == 3 and len({c.fan_outs for c in calls}) == 3
        assert shape.fan_outs == split_fan_outs(stated, sum(shape.counts), 5)
        assert calls[2].fan_outs == connectable_fan_outs(stated, shape.fan_outs)
        files = build_specimen_files(SpecimenPreset(DEFAULT_NAME, passport))
        sidecar = json.loads(files["generation.json"])
        assert sidecar["passport"]["connectors"] == passport["connectors"]

    def test_no_layout_on_every_tail_is_no_layout_naming_three(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        calls: list[Configuration] = []

        def none(configuration: Configuration, *_: Any, **__: Any) -> Planned:
            calls.append(configuration)
            return Planned(None, Trace(relaxed=False))

        _guard_the_three_tails(monkeypatch)
        monkeypatch.setattr(layout_module, "plan", none)
        found = check_feasibility(_three_tails())
        assert [(r.leaf, r.refusal_class) for r in found] == [(DELEGATION_LEAF, NO_LAYOUT)]
        assert found[0].sentence.endswith("(3 tried)")
        assert len(calls) == 3 and len({c.fan_outs for c in calls}) == 3


class TestStepTwo:
    def test_solve_open_is_asked_with_the_canonical_count_fixed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        configuration = replace(_seed_501_configuration(), connectors=None)
        original = solve_open
        specs: list[ConnectorSpec] = []

        def spied(spec: ConnectorSpec, *args: Any) -> Any:
            specs.append(spec)
            return original(spec, *args)

        monkeypatch.setattr(layout_module, "solve_open", spied)
        planned = layout_module.plan(configuration)
        assert planned.layout is not None and planned.layout.step == 2
        assert len(specs) == 1 and specs[0].connectors == planned.layout.connectors
        assert specs[0].names is None

    def test_a_solve_open_building_at_another_count_is_not_taken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        configuration = _seed_501_configuration()
        original = solve_open

        def another_count(*args: Any) -> Any:
            solution = original(*args)
            assert solution is not None
            # One star fewer: a construction of fewer connectors than the count fixed.
            return replace(solution, pieces=solution.pieces[:-1])

        monkeypatch.setattr(layout_module, "solve_open", another_count)
        planned = layout_module.plan(configuration)
        assert planned.trace == Trace(relaxed=True, builder=layout_module.FIRST_FIT)
        assert planned.layout is not None and planned.layout.connectors == 3
        assert planned.layout.step == 2


class TestTheUnmetNames:
    def test_with_delegations_the_unmet_names_path_returns_the_count_they_refuse(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        # The construction finds nothing with the stated connector names (the preferred candidate
        # refused, the planner answering no layout): the re-search with the names dropped asks the
        # planner over every count, and the count it lays out is the one the identifier check
        # refuses (`Shape.names_unmet`).
        passport = TestTheRangeOverConnectorReachedPorts()._six(10)
        stated = stated_leaves(passport)
        names = construction_spec(stated).names
        assert names is not None
        real_plan = layout_module.plan
        asked: list[Any] = []

        def through(configuration: Configuration, choosable: Any = None, **kwargs: Any) -> Planned:
            asked.append(None if choosable is None else tuple(choosable))
            if len(asked) == 1:
                return Planned(None, Trace(relaxed=False))
            return real_plan(configuration, choosable, **kwargs)

        monkeypatch.setattr(shape_module, "solve_open", lambda *_: None)
        monkeypatch.setattr(layout_module, "plan", through)
        monkeypatch.setattr(type(names), "constrains", property(lambda _: True))
        monkeypatch.setattr(type(names), "refused_at", lambda _, count: frozenset({("x", "y")}))
        shape = specimen_shape(stated)
        assert isinstance(shape, Shape) and shape.names_unmet
        assert shape.layout.source == "solver"
        # The first search asks the counts the names admit from 0 to min(R, the SR + CS ports);
        # the re-search with the names dropped every count: R = 10 reached ports on 12 SR ports.
        assert asked[0] == tuple(a for a in range(11) if names.admits(a))
        assert asked[-1] == tuple(range(11))

    def test_an_unstated_count_s_bounds_refuse_before_the_planner(
        self, monkeypatch: pytest.MonkeyPatch, fresh_shapes: None
    ) -> None:
        # A unstated, every other leaf stated: R = 10, C = 3, the SR + CS ports 12. With the names
        # admitting 5 connectors and more, the most D is 3 x (10 - 5) - 2 = 13.
        passport = _delegated(14)
        del passport["connectors"]["assembly"]
        del passport["layers"]["connectors"]
        stated = stated_leaves(passport)
        names = construction_spec(stated).names
        assert names is not None
        monkeypatch.setattr(type(names), "admits", lambda _, count: count >= 5)
        monkeypatch.setattr(layout_module, "plan", _no_layout_asked)
        found = specimen_shape(stated)
        assert isinstance(found, list)
        assert [(r.leaf, r.refusal_class) for r in found] == [(DELEGATION_LEAF, DERIVED)]
        assert found[0].sentence.startswith(
            "states 14; with 10 reached ports, the connectors chosen and 3 compositions the "
            "delegations number at least 0 and at most 13; with connectors.assembly derived"
        )


# --- the paired tail -----------------------------------------------------------------------------


def _passport_small() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "specimen-acceptance" / "passport-small.json"
    return read_passport(path.read_text(encoding="utf-8"))


class TestThePairedTail:
    def test_passport_small_is_shaped_with_twenty_paired_tails(self) -> None:
        shape = specimen_shape(stated_leaves(_passport_small()))
        assert isinstance(shape, Shape)
        # The fewest tails, 13, are refused (their pairs exceed 15, their even split holds 12
        # odd fan-outs); the next tried, 20, takes the paired split.
        assert shape.fan_outs == (16, 16, 16, 16, 15, 15, 15, 15, *[10] * 15, *[8] * 5)
        assert sum(1 for value in shape.fan_outs if value % 2) == 4

    def test_no_tail_meeting_the_bound_is_refused_at_the_fan_out_leaf(self) -> None:
        passport = _passport_small()
        passport["interfaces"]["fan_out"] = {"max": 15, "top": [15] * 8}
        assert [(r.leaf, r.refusal_class, r.sentence) for r in check_feasibility(passport)] == [
            (
                "interfaces.fan_out.top",
                "fan-out",
                "needs 20 interfaces of odd fan-out for 314 ports; an interface of odd fan-out "
                "needs a port no connector reaches or an extra require port of a star, and "
                "components.unconnected.total, connectors.delegation and the 154 connectors "
                "leave at most 6",
            )
        ]


def _one_component() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "specimen-acceptance" / "scenario-unconnected-one-component.json"
    return read_passport(path.read_text(encoding="utf-8"))


#: The witnesses the bound is held to: the interface tests' cases.
_BOUND_WITNESS_CASES = [(seed, 2 + seed % 7) for seed in range(42)] + [(100 + seed, 9 + seed % 8) for seed in range(18)]
_BOUND_SHAPED_WITNESS_CASES = [(500 + seed, 2 + seed % 7) for seed in range(42)] + [
    (600 + seed, 9 + seed % 8) for seed in range(18)
]


class TestTheStarSizeBound:
    """The least total is U + ceil(f x A / (f - 1)).

    s = R_c - A stars of at most f = `fan_out.max` ports hold the A connectors, so
    R_c >= ceil(f x A / (f - 1)).
    """

    def test_the_bound_is_480_on_the_passport(self) -> None:
        # U = 60, A = m // 2 + L = 10 + 400 = 410 (forced: no hub), f = 46:
        # R_c >= ceil(46 x 410 / 45) = 420.
        assert star_size_total(stated_leaves(_one_component())) == 480

    def test_the_bound_raises_the_least_total_of_the_sweep_to_480(self) -> None:
        stated = stated_leaves(_one_component())
        names = construction_spec(stated).names
        totals = _totals(stated, port_spec(stated), None if names is None else names.choosable)
        assert totals[0] == 480
        assert min(totals) == 480

    def test_no_bound_without_a_stated_or_forced_count(self) -> None:
        stated = stated_leaves(_one_component())
        stated["components.hubs"] = [60]
        # With a hub, `links` bounds A from below only: A is neither stated nor forced.
        assert star_size_total(stated) is None
        stated = stated_leaves(_one_component())
        stated["connectors.links"] = 0
        assert star_size_total(stated) is None

    def test_a_total_below_the_bound_is_a_stated_conflict(self) -> None:
        passport = _one_component()
        passport["components"]["ports"] = {"total": 474}
        assert [(r.leaf, r.refusal_class, r.sentence) for r in check_feasibility(passport)] == [
            (
                "connectors.links",
                "connectors",
                "states 400, with 20 components and no hub: the 410 connectors, in stars of at "
                "most 46 ports (interfaces.fan_out.max), reach at least 420 ports, so with 60 "
                "unconnected at least 480 component ports in all; the port and fan-out leaves "
                "admit exactly 474",
            )
        ]

    #: The fixtures no derivation shapes once `components.ports.total` and `.top` are dropped,
    #: each with the leaf refused `derived`; the dropped half of the next test asserts the bound
    #: on the other four only.
    REFUSED_ONCE_DROPPED = {
        "large-scale-topology": "connectors.assembly",
        "shapes": "connectors.delegation",
    }

    @pytest.mark.parametrize(
        "name",
        [
            "minimal",
            "topology",
            "large-scale-topology",
            "shapes",
            "chain-empty",
            "large-scale-shapes",
        ],
    )
    def test_the_bound_never_raises_a_fixture_s_first_built_total(self, name: str) -> None:
        stated = stated_leaves(load_specimen(name).passport)
        shape = specimen_shape(stated)
        assert isinstance(shape, Shape)
        bound = star_size_total(stated)
        assert bound is None or bound <= sum(shape.counts)
        # The same with the derivable leaves dropped: the sweep's first success is not raised.
        dropped = {
            leaf: value
            for leaf, value in stated.items()
            if leaf not in ("components.ports.total", "components.ports.top")
        }
        derived = specimen_shape(dropped)
        dropped_bound = star_size_total(dropped)
        assert dropped_bound is not None
        if name in self.REFUSED_ONCE_DROPPED:
            # No derivation meets these with the totals dropped (`derived`): no total to bound.
            assert not isinstance(derived, Shape)
            assert [(r.leaf, r.refusal_class) for r in derived] == [(self.REFUSED_ONCE_DROPPED[name], "derived")]
        else:
            assert isinstance(derived, Shape)
            assert dropped_bound <= sum(derived.counts)

    def test_the_bound_never_raises_a_witness_s_built_total(self) -> None:
        for seed, n in _BOUND_WITNESS_CASES:
            stated = stated_leaves(validate_passport(compute_passport(witness(seed, n))))
            shape = specimen_shape(stated)
            assert isinstance(shape, Shape), (seed, n)
            bound = star_size_total(stated)
            assert bound is None or bound <= sum(shape.counts), (seed, n)
        for seed, n in _BOUND_SHAPED_WITNESS_CASES:
            stated = stated_leaves(validate_passport(compute_passport(shaped_witness(seed, n))))
            shape = specimen_shape(stated)
            assert isinstance(shape, Shape), (seed, n)
            bound = star_size_total(stated)
            assert bound is None or bound <= sum(shape.counts), (seed, n)
