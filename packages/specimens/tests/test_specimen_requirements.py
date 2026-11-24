"""The requirement cases and their texts.

The rules are restated from SWCraft's benchmark generator (`workload.py`): every requirement is one
of four cases in id order (spread, one-port, unallocated, general); a spread one is linked once on
each of the seven site kinds, a one-port one on one port, a general one on components and runnables;
the links beyond the least go to the general ones, then the spread ones; the first V links of a
fixed enumeration are stale; the spread and unallocated cases take `workload.py`'s topics, cyclic,
and three components at the end of the component order take one description sentence per case.
Every acceptance is judged by the passport pass over the rendered tree.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import LINK_KINDS, Requirement, RequirementLink
from swcraft_specimens.passport import compute_passport, read_passport
from swcraft_specimens.preset import ASIL
from swcraft_specimens.specimen.build import build_project, build_specimen_files, render_specimen
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.leaves import stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.requirements import (
    NEUTRAL_DESCRIPTION,
    REQUIREMENT_TOPICS,
    PlannedLink,
    PlannedRequirement,
    RequirementCounts,
    RequirementPlan,
    SiteCounts,
    build_requirements,
    descriptions,
    link_total,
    plan_requirements,
    requirement_sentence_count,
    sentence_count,
)
from tests.oracle import specimen_helpers
from tests.oracle.specimen_helpers import built_specimen, neutral_passport, sentences_built

ACCEPTANCE = Path(__file__).parent / "fixtures" / "specimen-acceptance"
CEILING_TAIL = "this version builds at most 10,000 of anything"


def with_requirements(
    total: int,
    *,
    spread: int = 0,
    one_port: int = 0,
    unallocated: int = 0,
    stale: int = 0,
    links: int | None = None,
    tasks: int = 0,
    runnables: int | None = 2,
) -> dict[str, Any]:
    """The neutral passport with requirement cases and `tasks` busy tasks on one partition.

    It has two components of one port each and one interface; the requirement identifier lengths
    are unstated (the overall maximum 8 is the connectors').
    """
    passport = copy.deepcopy(neutral_passport())
    passport["layers"]["requirements"] = total
    passport["requirements"] = {
        "on_all_site_kinds": spread,
        "on_one_port": one_port,
        "unallocated": unallocated,
        "stale_links": stale,
    }
    if links is not None:
        passport["requirements"]["links"] = links
    if tasks:
        passport["layers"]["tasks"] = tasks
        del passport["layers"]["sequences"]
        passport["hardware"]["partitions"] = 1
        passport["layers"]["hardware"] = 3
        for layer in ("hardware", "tasks", "sequences"):
            del passport["identifiers"][layer]
    if runnables is None:
        del passport["components"]["runnables"]
    else:
        passport["components"]["runnables"] = runnables
    del passport["identifiers"]["requirements"]
    return passport


def no_components(passport: dict[str, Any]) -> dict[str, Any]:
    """The passport with no component, port, interface, connector or runnable."""
    return specimen_helpers.no_components(passport, runnables=True)


def refused(passport: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(r.leaf, r.refusal_class, r.sentence) for r in check_feasibility(passport)]


def built(passport: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The built project and the pass's passport, after the full generation and self-check."""
    rendered = built_specimen(passport)
    return rendered.project, rendered.passport


def by_case(project: Any, counts: tuple[int, int, int]) -> dict[str, list[Requirement]]:
    """The project's requirements split into the four cases by id order (S, O, N, then G)."""
    spread, one_port, unallocated = counts
    requirements = list(project.requirements)
    return {
        "spread": requirements[:spread],
        "one_port": requirements[spread : spread + one_port],
        "unallocated": requirements[spread + one_port : spread + one_port + unallocated],
        "general": requirements[spread + one_port + unallocated :],
    }


# --- large-bench --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def large_bench() -> tuple[Any, dict[str, Any]]:
    text = (ACCEPTANCE / "large-bench.json").read_text(encoding="utf-8")
    return built(read_passport(text))


class TestLargeBench:
    def test_the_pass_gives_each_figure(self, large_bench: tuple[Any, dict[str, Any]]) -> None:
        _, passport = large_bench
        assert passport["layers"]["requirements"] == 20
        assert passport["requirements"] == {
            "on_all_site_kinds": 3,
            "on_one_port": 12,
            "unallocated": 4,
            "stale_links": 2,
            "links": 183,
        }

    def test_the_general_requirement_links_exactly_the_150_components(
        self, large_bench: tuple[Any, dict[str, Any]]
    ) -> None:
        project, _ = large_bench
        general = by_case(project, (3, 12, 4))["general"]
        assert len(general) == 1
        assert [(link.kind, link.element, link.member) for link in general[0].links] == [
            ("swc", component.name, None) for component in project.components
        ]
        assert len(project.components) == 150

    def test_the_two_stale_links_are_the_first_spread_one_s_component_and_port(
        self, large_bench: tuple[Any, dict[str, Any]]
    ) -> None:
        project, _ = large_bench
        first, *others = project.requirements
        assert first.version == 2
        assert [(link.kind, link.version) for link in first.links] == [
            ("swc", 1),
            ("port", 1),
            ("runnable", 2),
            ("interface", 2),
            ("composition", 2),
            ("task", 2),
            ("hardware", 2),
        ]
        assert all(r.version == 1 for r in others)
        assert all(link.version == 1 for r in others for link in r.links)

    def test_the_sentence_count_equals_the_sentences_built(self, large_bench: tuple[Any, dict[str, Any]]) -> None:
        project, _ = large_bench
        text = (ACCEPTANCE / "large-bench.json").read_text(encoding="utf-8")
        stated = stated_leaves(read_passport(text))
        # Three spread and four unallocated cases, three components each, none linked there.
        assert requirement_sentence_count(stated) == sentences_built(project) == 21

    def test_the_first_topic_s_sentence_sits_on_the_last_three_components(
        self, large_bench: tuple[Any, dict[str, Any]]
    ) -> None:
        project, _ = large_bench
        last = [component.description for component in project.components[-3:]]
        assert last == ["Takes part in supply voltage monitoring."] * 3
        assert project.requirements[0].title == "Supply voltage monitoring"


# --- the extra links ----------------------------------------------------------------------------


class TestTheExtraLinks:
    def test_showcase_twin_s_figures_put_the_extra_links_on_the_general_ones(self) -> None:
        # G = 57 - 6 - 31 = 20 general requirements, L_r = 43 = 6 + 20 + 17 extra.
        passport = with_requirements(57, one_port=6, unallocated=31, stale=3, links=43)
        project, result = built(passport)
        assert result["requirements"] == {
            "on_all_site_kinds": 0,
            "on_one_port": 6,
            "unallocated": 31,
            "stale_links": 3,
            "links": 43,
        }
        general = by_case(project, (0, 6, 31))["general"]
        assert [len(r.links) for r in general] == [2] * 17 + [1] * 3

    def test_with_no_general_one_the_extra_links_go_to_the_spread_ones(self) -> None:
        # Two spread requirements (seven each), one one-port, five extra: three and two.
        passport = with_requirements(3, spread=2, one_port=1, links=7 * 2 + 1 + 5, tasks=1)
        passport["components"]["runnables"] = 4
        project, result = built(passport)
        assert result["requirements"]["links"] == 20
        assert result["requirements"]["on_all_site_kinds"] == 2
        spread = by_case(project, (2, 1, 0))["spread"]
        assert [len(r.links) for r in spread] == [7 + 3, 7 + 2]

    def test_an_unstated_link_count_with_v_above_n_plus_g_minus_1_is_derived_to_v(self) -> None:
        # n = 2, G = 1: n + G - 1 = 2 links would hold only 2 stale ones; V = 3.
        passport = with_requirements(1, stale=3)
        assert link_total(RequirementCounts(1, 0, 0, 0, 3, None), 2, 2) == 3
        project, result = built(passport)
        assert result["requirements"]["links"] == 3
        assert result["requirements"]["stale_links"] == 3
        assert len(project.requirements[0].links) == 3

    def test_an_unstated_link_count_puts_the_first_general_one_on_every_component(self) -> None:
        counts = RequirementCounts(3, 0, 0, 0, 0, None)
        assert link_total(counts, 5, 5) == 5 + 3 - 1
        plan = plan_requirements(counts, SiteCounts((1,) * 5, (1,) * 5, 1, 0))
        assert [[(link.kind, link.index) for link in r.links] for r in plan.requirements] == [
            [("swc", index) for index in range(5)],
            [("swc", 1)],
            [("swc", 2)],
        ]

    def test_a_derived_link_count_is_capped_at_the_ceiling(self) -> None:
        # n = 80, Rn = 520, G = 9,990: n + G - 1 = 10,069 links, capped at 10,000.
        counts = RequirementCounts(9_990, 0, 0, 0, 0, None)
        assert link_total(counts, 80, 520) == 10_000

    def test_the_first_general_one_takes_only_the_extra_the_cap_leaves(self) -> None:
        # The cap leaves 10 extra links, fewer than the n - 1 = 79 the first general one takes
        # otherwise: it holds 1 + 10, every other one 1.
        counts = RequirementCounts(9_990, 0, 0, 0, 0, None)
        plan = plan_requirements(counts, SiteCounts((1,) * 80, (7,) * 40 + (6,) * 40, 1, 0))
        lengths = [len(r.links) for r in plan.requirements]
        assert lengths[0] == 11
        assert lengths[1:] == [1] * 9_989
        assert sum(lengths) == 10_000


# --- the stale links and the versions ----------------------------------------------------------


class TestTheStaleLinks:
    def test_the_stale_order(self) -> None:
        # One spread, one one-port and one general requirement, three extra links: the general
        # one fills first (up to n + Rn - 1 = 5 extra), so it holds 1 + 3. The enumeration is the
        # spread one's seven, the one-port link, then the general one's in its site order, so
        # the first nine are its seven, the one-port link and the general one's first.
        counts = RequirementCounts(3, 1, 1, 0, 9, 7 + 1 + 1 + 3)
        plan = plan_requirements(counts, SiteCounts((1, 1, 1), (1, 1, 1), 1, 1))
        spread, one_port, general = plan.requirements
        assert [len(r.links) for r in plan.requirements] == [7, 1, 4]
        assert [(link.kind, link.index) for link in spread.links] == [
            ("swc", 0),
            ("port", 0),
            ("runnable", 0),
            ("interface", 0),
            ("composition", 0),
            ("task", 0),
            ("hardware", 0),
        ]
        assert [link.version for link in spread.links] == [1] * 7
        assert [link.version for link in one_port.links] == [1]
        assert [link.version for link in general.links] == [1, 2, 2, 2]
        assert [r.version for r in plan.requirements] == [2, 2, 2]

    def test_with_fewer_stale_links_the_untouched_requirements_stay_at_version_1(self) -> None:
        counts = RequirementCounts(3, 1, 1, 0, 8, 7 + 1 + 1 + 3)
        plan = plan_requirements(counts, SiteCounts((1, 1, 1), (1, 1, 1), 1, 1))
        assert [r.version for r in plan.requirements] == [2, 2, 1]
        assert {link.version for link in plan.requirements[2].links} == {1}

    def test_the_spread_extra_links_come_last_and_are_stale(self) -> None:
        # n = Rn = 2, S = 1, L_r = V = 9.
        passport = with_requirements(1, spread=1, stale=9, links=9, tasks=1)
        project, result = built(passport)
        requirement = project.requirements[0]
        assert len(requirement.links) == 9
        assert [(link.kind, link.element) for link in requirement.links[7:]] == [
            ("swc", project.components[1].name),
            ("runnable", project.components[1].name),
        ]
        assert requirement.version == 2
        assert {link.version for link in requirement.links} == {1}
        assert result["requirements"]["stale_links"] == 9
        assert result["requirements"]["links"] == 9

    def test_the_versions_and_no_asil(self) -> None:
        passport = with_requirements(3, spread=1, one_port=1, stale=2, tasks=1)
        preset = SpecimenPreset(DEFAULT_NAME, passport)
        files = build_specimen_files(preset)
        project = build_project(preset)
        assert [r.version for r in project.requirements] == [2, 1, 1]
        assert all(r.asil is ASIL.ABSENT for r in project.requirements)
        texts = [files[f"model/requirements/{r.id}.yaml"].decode("utf-8") for r in project.requirements]
        assert all("asil:" not in text for text in texts)
        assert all("source: architecture" in text for text in texts)
        assert "version: 2" in texts[0] and "version: 1" in texts[1]
        documents = render_specimen(preset).documents
        assert compute_passport(documents)["requirements"]["stale_links"] == 2


# --- the texts ----------------------------------------------------------------------------------


class TestTheTexts:
    def test_the_topics_are_workload_s_verbatim(self) -> None:
        assert REQUIREMENT_TOPICS == (
            (
                "Supply voltage monitoring",
                "The provider components shall monitor the supply voltage and report a low "
                "supply voltage within 10 ms.",
            ),
            (
                "Output frame timing",
                "Every output frame shall be published within its period, measured at the sender port.",
            ),
            (
                "Supply voltage recovery",
                "After a low supply voltage the components shall resume normal operation within 50 ms.",
            ),
            (
                "Output voltage range",
                "The published output voltage value shall stay inside its declared range.",
            ),
            (
                "Hub forwarding latency",
                "The hub components shall forward every received signal to the counterpart port "
                "of the receiving component.",
            ),
            (
                "Watchdog supervision",
                "Every task shall be supervised by the watchdog; a missed deadline shall be reported.",
            ),
            (
                "Supply line diagnosis",
                "A short circuit of the sensor supply line shall be detected.",
            ),
        )

    def test_the_topics_digest_is_the_one_read_from_workload_s_source(self) -> None:
        # Recorded from SWCraft's `workload.py` `_REQUIREMENT_TOPICS` (read by AST, each topic
        # "title<TAB>description", joined by newlines), so an edit of the constant moves a digest
        # checked against the source, not a copy beside it.
        joined = "\n".join(f"{title}\t{description}" for title, description in REQUIREMENT_TOPICS)
        assert hashlib.sha256(joined.encode("utf-8")).hexdigest() == (
            "e05e30fa5fb6930fd5f34faab9c4127dc1384a36c3a4c82656224cc64824d9c5"
        )

    def test_the_topics_go_spread_then_unallocated_and_cycle(self) -> None:
        passport = with_requirements(10, spread=2, one_port=1, unallocated=6, tasks=1)
        project = build_project(SpecimenPreset(DEFAULT_NAME, passport))
        cases = by_case(project, (2, 1, 6))
        topical = [*cases["spread"], *cases["unallocated"]]
        assert [(r.title, r.description) for r in topical] == [REQUIREMENT_TOPICS[k % 7] for k in range(8)]
        assert topical[7].title == "Supply voltage monitoring"
        for requirement in [*cases["one_port"], *cases["general"]]:
            assert requirement.title == f"Specimen requirement {requirement.id}"
            assert requirement.description == NEUTRAL_DESCRIPTION == "A specimen requirement."

    def test_three_sentences_per_case_at_the_end_skipping_linked_components(self) -> None:
        # n = 5, one port and one runnable each: spread 0 holds component 0, spread 1 component 1.
        counts = RequirementCounts(3, 2, 0, 1, 0, None)
        plan = plan_requirements(counts, SiteCounts((1,) * 5, (1,) * 5, 1, 1))
        # Case 0 (spread 0): 4, 3, 2. Case 1 (spread 1): 1 is linked, so 0, 4, 3.
        # Case 2 (unallocated): 5 - 1 - 6 = -2 = 3 mod 5, then 2, 1.
        assert plan.sentences == ((1,), (2,), (0, 2), (0, 1, 2), (0, 1))
        assert sentence_count(plan) == 9
        texts = descriptions(plan)
        assert texts[4] == ("Takes part in supply voltage monitoring. Takes part in output frame timing.")
        assert texts[0] == "Takes part in output frame timing."

    def test_fewer_components_than_three_take_fewer_sentences(self) -> None:
        counts = RequirementCounts(2, 1, 0, 1, 0, None)
        plan = plan_requirements(counts, SiteCounts((1, 1), (1, 1), 1, 1))
        # The spread case holds component 0: one sentence; the unallocated one: two.
        assert sentence_count(plan) == 3

    def test_with_no_spread_or_unallocated_case_no_description_changes(self) -> None:
        passport = with_requirements(3, one_port=1, tasks=1)
        project, _ = built(passport)
        assert [component.description for component in project.components] == ["", ""]

    def test_the_sentence_count_equals_the_sentences_built(self) -> None:
        passport = with_requirements(4, spread=1, unallocated=2, tasks=1)
        project, _ = built(passport)
        stated = stated_leaves(passport)
        # Spread 0 holds component 0 (its component, port and runnable): one sentence; the two
        # unallocated cases two each.
        assert requirement_sentence_count(stated) == sentences_built(project) == 5


# --- the refusals -------------------------------------------------------------------------------


class TestRefusals:
    def test_the_cases_above_the_layer_are_sum(self) -> None:
        passport = with_requirements(2, spread=1, one_port=1, unallocated=1, tasks=1)
        assert refused(passport) == [
            (
                "layers.requirements",
                "sum",
                "states 2; requirements.on_all_site_kinds, requirements.on_one_port and "
                "requirements.unallocated state 3 in all",
            ),
        ]

    def test_a_spread_requirement_with_no_task(self) -> None:
        passport = with_requirements(1, spread=1)
        assert refused(passport) == [
            (
                "requirements.on_all_site_kinds",
                "requirements",
                "states 1; a spread requirement is linked on every site kind, and the specimen has no task",
            ),
        ]

    def test_a_spread_requirement_with_no_component_names_the_first_missing_kind(self) -> None:
        passport = no_components(with_requirements(1, spread=1))
        assert refused(passport) == [
            (
                "requirements.on_all_site_kinds",
                "requirements",
                "states 1; a spread requirement is linked on every site kind, and the specimen has no component",
            ),
        ]

    def test_a_one_port_requirement_with_no_port(self) -> None:
        passport = no_components(with_requirements(1, one_port=1))
        assert refused(passport) == [
            (
                "requirements.on_one_port",
                "requirements",
                "states 1; a one-port requirement is linked on one port, and the specimen has no port",
            ),
        ]

    @pytest.mark.parametrize("links", [1, None], ids=["stated", "unstated"])
    def test_a_general_requirement_with_no_component_or_runnable(self, links: int | None) -> None:
        # n = Rn = 0 and G = 1, L_r stated or not.
        passport = no_components(with_requirements(1, links=links))
        assert refused(passport) == [
            (
                "layers.requirements",
                "requirements",
                "states 1 requirements that are none of spread, one-port or unallocated; a "
                "general requirement is linked on a component or a runnable, and the specimen "
                "has neither",
            ),
        ]

    def test_links_below_the_least(self) -> None:
        passport = with_requirements(3, spread=1, one_port=1, links=8, tasks=1)
        assert refused(passport) == [
            (
                "requirements.links",
                "requirements",
                "states 8; the 1 spread, 1 one-port and 1 general requirements need at least 9",
            ),
        ]

    def test_links_above_the_capacity(self) -> None:
        # One general requirement on 2 components and 2 runnables: at most 4.
        passport = with_requirements(1, links=5)
        assert refused(passport) == [
            (
                "requirements.links",
                "requirements",
                "states 5; the requirements hold at most 4 links, no site twice",
            ),
        ]

    def test_links_above_the_capacity_through_unstated_runnables_are_derived(self) -> None:
        passport = with_requirements(1, links=5, runnables=None)
        assert refused(passport) == [
            (
                "requirements.links",
                "derived",
                "states 5; the requirements hold at most 4 links, no site twice; "
                "components.runnables is unstated and derived as 2, and a larger stated count "
                "raises the capacity",
            ),
        ]

    def test_stale_links_above_the_stated_links(self) -> None:
        passport = with_requirements(1, links=2, stale=3)
        assert refused(passport) == [
            ("requirements.stale_links", "requirements", "states 3; requirements.links states 2"),
        ]

    def test_stale_links_above_the_capacity_with_the_links_unstated(self) -> None:
        passport = with_requirements(1, stale=5)
        assert refused(passport) == [
            (
                "requirements.stale_links",
                "requirements",
                "states 5; the requirements hold at most 4 links, no site twice",
            ),
        ]

    def test_stale_links_above_the_capacity_through_unstated_runnables_are_derived(self) -> None:
        passport = with_requirements(1, stale=5, runnables=None)
        assert [(leaf, cls) for leaf, cls, _ in refused(passport)] == [
            ("requirements.stale_links", "derived"),
        ]

    def test_links_above_a_capacity_that_reads_no_runnables_keep_their_class(self) -> None:
        # S = G = 0 makes the capacity O whatever Rn is, so an unstated Rn does not make the
        # refusal `derived`.
        passport = with_requirements(2, one_port=2, links=3, runnables=None)
        assert refused(passport) == [
            (
                "requirements.links",
                "requirements",
                "states 3; the requirements hold at most 2 links, no site twice",
            ),
        ]

    def test_stale_links_above_a_capacity_that_reads_no_runnables_keep_their_class(self) -> None:
        # S = G = 0 and O = 0 make the capacity 0 whatever Rn is.
        passport = with_requirements(2, unallocated=2, stale=3, runnables=None)
        assert refused(passport) == [
            (
                "requirements.stale_links",
                "requirements",
                "states 3; the requirements hold at most 0 links, no site twice",
            ),
        ]

    @pytest.mark.parametrize(
        "case",
        [
            {"total": 0},
            {"total": 1},
            {"total": 3, "one_port": 1, "unallocated": 1, "stale": 1},
            {"total": 2, "spread": 1, "stale": 2, "tasks": 1},
        ],
        ids=["none", "general", "mixed", "spread"],
    )
    def test_a_feasible_requirement_layer_is_not_refused(self, case: dict[str, int]) -> None:
        assert refused(with_requirements(**case)) == []


# --- the planned links under their names -------------------------------------------------------


def _one_link_plan(kind: str) -> RequirementPlan:
    return RequirementPlan((PlannedRequirement((PlannedLink(kind, 0, 1),), 1, None),), ((),))


def _named(plan: RequirementPlan) -> tuple[Requirement, ...]:
    return build_requirements(plan, ["Rq1"], ["C1"], [["P1"]], [1], ["If1"], "Root", ["T1"], "CPU")


class TestTheLinkSites:
    @pytest.mark.parametrize("kind", LINK_KINDS)
    def test_each_site_kind_names_its_element(self, kind: str) -> None:
        expected = {
            "swc": ("C1", None),
            "port": ("C1", "P1"),
            "runnable": ("C1", "R1"),
            "interface": ("If1", None),
            "composition": ("Root", None),
            "task": ("T1", None),
            "hardware": ("CPU", None),
        }
        (requirement,) = _named(_one_link_plan(kind))
        assert requirement.links == (RequirementLink(kind, *expected[kind], 1),)

    def test_an_unknown_site_kind_is_refused_not_read_as_hardware(self) -> None:
        with pytest.raises(GenerationError, match="names no site kind: 'swcs'"):
            _named(_one_link_plan("swcs"))


# --- the two ceiling refusals -------------------------------------------------------------------


class TestTheCeilings:
    def test_the_least_links_above_the_ceiling(self) -> None:
        # 1,500 spread requirements need 10,500 links.
        passport = with_requirements(1_500, spread=1_500, tasks=1)
        assert refused(passport) == [
            ("requirements.links", "ceiling", f"needs at least 10500 links; {CEILING_TAIL}"),
        ]

    def test_the_round_s_counterexample_is_not_refused(self) -> None:
        # n = 2, S = 1,000, N = 4,200. Each spread case holds the one component its component, port
        # and runnable sites share, so it writes one sentence; the unallocated ones two each:
        # 1,000 + 8,400.
        passport = with_requirements(5_200, spread=1_000, unallocated=4_200, tasks=1)
        assert requirement_sentence_count(stated_leaves(passport)) == 9_400
        assert refused(passport) == []

    def test_the_unallocated_sentences_refuse_the_ceiling_beside_another_refusal(self) -> None:
        # n = 2 and N = 5,001: 10,002 sentences whatever the shape, while layers.interfaces at 0 is
        # refused layer-total and leaves the exact count undecided; the ceiling refusal alone is reported.
        passport = with_requirements(5_001, unallocated=5_001)
        passport["layers"]["interfaces"] = 0
        assert requirement_sentence_count(stated_leaves(passport)) is None
        assert refused(passport) == [
            (
                "layers.requirements",
                "ceiling",
                f"writes at least 10002 description sentences; {CEILING_TAIL}",
            ),
        ]

    def test_more_unallocated_cases_on_the_same_topology_are_refused(self) -> None:
        passport = with_requirements(5_600, spread=1_000, unallocated=4_600, tasks=1)
        assert requirement_sentence_count(stated_leaves(passport)) == 10_200
        assert refused(passport) == [
            (
                "layers.requirements",
                "ceiling",
                f"writes 10200 description sentences; {CEILING_TAIL}",
            ),
        ]


# --- the acceptance passports that state requirements -------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["example-configuration-only-controlled", "scenario-impact-summary-bounded", "basic-twin"],
)
def test_an_acceptance_passport_s_requirements_are_met(name: str) -> None:
    text = (ACCEPTANCE / f"{name}.json").read_text(encoding="utf-8")
    passport = read_passport(text)
    _, result = built(passport)
    for leaf, value in passport["requirements"].items():
        assert result["requirements"][leaf] == value
    assert json.loads(text)["layers"]["requirements"] == result["layers"]["requirements"]
