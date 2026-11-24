"""The architecture metrics' conclusions, each expected sentence written by hand as a literal.

No sentence here was copied from the code's output: each is the sentence the conclusion rules give
for its case, with the figures of the hand-built state beside it worked out in the comment.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from swcraft_tools import metrics, metrics_conclusions
from swcraft_tools.metrics import (
    COHESION_REASONS,
    EMPTY_COMPOSITION,
    END_NOT_A_MEMBER,
    LOWER_THRESHOLD_BELOW_ZERO,
    NO_COHESION_FIGURE,
    NO_COMPONENT,
    NO_COMPONENT_ACCESS,
    NO_REQUIREMENT,
    NO_RUNNABLE,
    NO_SAFETY_LEVEL,
    NO_TASK,
    ONE_DIRECTION,
    REPEATED_NAME,
    TOO_FEW_FOR_OUTLIERS,
    UNREACHED_END,
    UNRESOLVED_OWNER,
    UNRESOLVED_PORT,
    ArchitectureState,
    AsilCoverage,
    Cohesion,
    ComponentRow,
    CompositionTable,
    Coupling,
    Coverage,
    Integrity,
    LookAtEntry,
    LookAtLine,
    MemberRow,
    MetricsReport,
    NotAvailable,
    RequirementCoverage,
    RunnableCoverage,
    Size,
    TaskCoverage,
    compare,
)
from swcraft_tools.metrics_conclusions import (
    HEADING_ANSWER_SENTENCES,
    LOOK_AT_LABELS,
    NO_BASE_NAMED,
    SECTION_KEYS,
    STATE_ANSWER_SENTENCES,
    Conclusions,
    SectionConclusion,
    conclude,
)
from swcraft_tools.metrics_render import (
    DEFINITIONS,
    DEFINITIONS_TITLE,
    OVERVIEW_TITLE,
    SECTION_TERMS,
    render_json,
    render_markdown,
    render_text,
)
from swcraft_tools.schema import SWComponent
from swcraft_tools.validator import ConsistencyIssue
from test_metrics import (
    HEAD_STATES,
    accessing,
    bare_project,
    commit,
    component_yaml,
    composition,
    connector,
    connector_yaml,
    head_state,
    metrics_of,
    model,
    periodic,
    requirement,
    server,
    short_sha,
    side_of,
    stub,
    swc,
    task,
    write,
)
from test_metrics import component as model_component

BASE = "v1 (abc1234)"

# ---------------------------------------------------------------------------
# Hand-built states: conclude reads figures only, so a state is written as its figures
# ---------------------------------------------------------------------------


def component(name: str, ct: int = 0) -> ComponentRow:
    """A declared component whose ``Ct`` is *ct*, all of it ``Ca``."""
    return ComponentRow(name, "application", ct, 0, 0, 0, NotAvailable("no runnable"), 0, None if ct else "no port")


def components(**cts: int) -> tuple[ComponentRow, ...]:
    return tuple(component(name, ct) for name, ct in sorted(cts.items()))


def coupling_of(
    rows: tuple[ComponentRow, ...],
    hubs: tuple[str, ...] | NotAvailable = (),
    not_measured: tuple[tuple[str, int], ...] = (),
    isolated: tuple[str, ...] | NotAvailable = (),
) -> Coupling:
    # Each hand-built row's Ct is all Ca, so its dependencies are its Ct, and the model's their sum.
    total = sum(row.ct for row in rows)
    return Coupling(
        n=len(rows),
        dependencies=total,
        mean=Fraction(total, len(rows)),
        variance=Fraction(0),
        hubs=hubs,
        isolated=isolated,
        trimmed_mean=Fraction(total, len(rows)),
        trimmed_max=max(row.ct for row in rows),
        external_stubs=0,
        stubs_depended_on=(),
        not_measured_by_reason=not_measured,
    )


CLEAN = Integrity(0, 0, (), (), (), 0, ())
NO_COVERAGE = Coverage(
    NotAvailable(NO_SAFETY_LEVEL), NotAvailable(NO_RUNNABLE), NotAvailable(NO_TASK), NotAvailable(NO_REQUIREMENT)
)


def size(count: int, *, stubs: int = 0, compositions: int = 1, depth: int = 1) -> Size:
    return Size((), count, stubs, compositions, depth if compositions else 0, 0, 0, 0, 0, 0, 0)


def state(
    rows: tuple[ComponentRow, ...] = (),
    *,
    label: str = "working tree",
    hubs: tuple[str, ...] | NotAvailable = (),
    isolated: tuple[str, ...] | NotAvailable = (),
    not_measured: tuple[tuple[str, int], ...] = (),
    cohesion: Cohesion | NotAvailable = NotAvailable(NO_COMPONENT_ACCESS),
    coverage: Coverage = NO_COVERAGE,
    integrity: Integrity = CLEAN,
    tables: tuple[CompositionTable, ...] = (),
    not_placed: tuple[tuple[str, int], ...] = (),
    findings: tuple[ConsistencyIssue, ...] = (),
    compositions: int = 1,
) -> ArchitectureState:
    coupling: Coupling | NotAvailable = (
        coupling_of(rows, hubs, not_measured, isolated) if rows else NotAvailable(NO_COMPONENT)
    )
    return ArchitectureState(
        label=label,
        size=size(len(rows), compositions=compositions),
        coupling=coupling,
        integrity=integrity,
        coverage=coverage,
        cohesion=cohesion,
        components=rows,
        composition_tables=tables,
        findings=findings,
        links_not_placed_by_reason=not_placed,
    )


def report(
    current: ArchitectureState,
    *,
    base: ArchitectureState | None = None,
    no_base: str | None = None,
    look_at: tuple[LookAtLine, ...] = (),
) -> MetricsReport:
    series = (current,) if base is None else (base, current)
    return MetricsReport("demo", series, compare(series) if base is not None else None, no_base, look_at)


def section(conclusions: Conclusions, key: str) -> SectionConclusion:
    found = conclusions.section(key)
    assert found is not None, key
    return found


def sentences(current: ArchitectureState, key: str, **kwargs: object) -> tuple[str, ...]:
    return section(conclude(report(current, **kwargs)), key).sentences  # type: ignore[arg-type]


def limit(current: ArchitectureState, key: str) -> str | None:
    return section(conclude(report(current)), key).limit


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------


class TestNameList:
    """Every list of the report counts its rest one way: the list's own separator, then *and N more*."""

    @pytest.mark.parametrize(
        ("names", "cap", "separator", "more", "expected"),
        [
            (("A", "B", "C"), 3, ", ", 0, "A, B, C"),
            (("A", "B", "C", "D", "E"), 3, ", ", 0, "A, B, C, and 2 more"),
            # A cell's cap of ten, the same style.
            (tuple(f"N{i}" for i in range(12)), 10, ", ", 0, ", ".join(f"N{i}" for i in range(10)) + ", and 2 more"),
            # What to look at hands its capped entries and the count it left out.
            (("A (no port)", "B (incomplete)"), 2, "; ", 3, "A (no port); B (incomplete); and 3 more"),
        ],
    )
    def test_the_rest_is_counted_one_way(
        self, names: tuple[str, ...], cap: int, separator: str, more: int, expected: str
    ) -> None:
        assert metrics_conclusions.name_list(names, cap, separator=separator, more=more) == expected


class TestOutlierBound:
    """A ``Ct`` is an integer, so the printed bound never puts one on the wrong side of it."""

    @pytest.mark.parametrize(
        ("a", "b", "v", "expected"),
        [
            # 2·√6.240004 = 4.996, printed 5.00: a hub at Ct 5 is above 4.996, so the bound is "5 or more".
            (Fraction(0), 2, Fraction(6240004, 10**6), "5 or more"),
            # Exactly 5: "above 5.00" holds of every hub.
            (Fraction(0), 2, Fraction(25, 4), "above 5.00"),
            # 5.003, printed 5.00: a hub needs Ct 6, which is above 5.00.
            (Fraction(0), 2, Fraction(625750225, 10**8), "above 5.00"),
            (Fraction(1), 2, Fraction(1), "above 3.00"),
            (Fraction(10, 21), 2, Fraction(110, 441), "above 1.48"),
            # 10 − 4.997 = 5.003, printed 5.00: an isolated component at Ct 5 is below 5.003.
            (Fraction(10), -2, Fraction(624250225, 10**8), "5 or less"),
            # 10 − 5.004 = 4.996, printed 5.00: Ct 5 is not isolated, Ct 4 is.
            (Fraction(10), -2, Fraction(626000400, 10**8), "below 5.00"),
        ],
    )
    def test_the_bound(self, a: Fraction, b: int, v: Fraction, expected: str) -> None:
        assert metrics_conclusions.outlier_bound(a, b, v) == expected


class TestSize:
    def test_components_in_compositions(self) -> None:
        current = dataclasses.replace(state(components(A=1)), size=size(42, compositions=5, depth=3))
        assert sentences(current, "size") == ("The model declares 42 components and 5 compositions, nested 3 deep.",)

    def test_one_component_in_one_composition_agrees(self) -> None:
        current = dataclasses.replace(state(components(A=0)), size=size(1))
        assert sentences(current, "size") == ("The model declares 1 component and 1 composition, nested 1 deep.",)

    def test_no_composition(self) -> None:
        current = dataclasses.replace(state(components(A=0)), size=size(3, compositions=0))
        assert sentences(current, "size") == ("The model declares 3 components and no composition.",)

    def test_stubs_too(self) -> None:
        current = dataclasses.replace(state(components(A=1)), size=size(42, stubs=6, compositions=5, depth=3))
        assert sentences(current, "size") == (
            "The model declares 42 components and 5 compositions, nested 3 deep.",
            "6 more are external stubs, declared by reference.",
        )

    def test_unplaced_components_and_an_empty_composition_are_not_said_to_be_in_it(self, tmp_path: Path) -> None:
        # Two components placed nowhere and one composition with no member: the counts are side by side.
        measured = side_of(model(swc("A", "pa"), swc("B", "ra"), composition("Empty")), tmp_path)
        assert sentences(measured, "size") == ("The model declares 2 components and 1 composition, nested 1 deep.",)

    def test_one_stub_agrees(self) -> None:
        current = dataclasses.replace(state(components(A=1)), size=size(2, stubs=1))
        assert sentences(current, "size")[1] == "1 more is an external stub, declared by reference."

    def test_none(self) -> None:
        assert sentences(state(), "size") == ("The model declares no component.",)

    def test_none_with_stubs(self) -> None:
        current = dataclasses.replace(state(), size=size(0, stubs=2))
        assert sentences(current, "size") == (
            "The model declares no component.",
            "It declares 2 external stubs by reference.",
        )

    def test_two_connected_stubs_in_a_composition_are_described_below(self, tmp_path: Path) -> None:
        # No declared component, yet the composition table describes the two stubs and their one link:
        # the headline says no component and claims nothing of the figures below.
        measured = side_of(
            model(
                stub("S", "pX"),
                stub("T", "rX"),
                composition("Root", root=True, members=(("s", "S"), ("t", "T"))),
                connector("c", "S.pX", "T.rX"),
            ),
            tmp_path,
        )
        (table,) = measured.composition_tables
        assert (len(table.rows), table.links) == (2, 1)
        assert sentences(measured, "size") == (
            "The model declares no component.",
            "It declares 2 external stubs by reference.",
        )
        assert section(conclude(report(measured)), "tables").sentences == (
            "1 composition; Root has the most links among its direct members: 1.",
        )

    def test_a_stub_declaring_a_runnable_is_not_said_to_hold_ports_only(self, tmp_path: Path) -> None:
        # Size counts the stub's runnable, so no size sentence says what a stub holds, in any of its forms.
        (tmp_path / "alone").mkdir()
        (tmp_path / "beside").mkdir()
        alone = side_of(model(model_component("S", periodic("run"), external=True)), tmp_path / "alone")
        assert alone.size.runnables == 1
        assert sentences(alone, "size") == (
            "The model declares no component.",
            "It declares 1 external stub by reference.",
        )
        beside = side_of(
            model(swc("A", "pa"), model_component("S", periodic("run"), external=True)), tmp_path / "beside"
        )
        assert beside.size.runnables == 1
        assert sentences(beside, "size")[1] == "1 more is an external stub, declared by reference."
        for measured in (alone, beside):
            assert not any("ports only" in sentence for sentence in every_sentence(conclude(report(measured))))

    def test_size_has_no_limit(self) -> None:
        assert limit(state(components(A=1)), "size") is None


# ---------------------------------------------------------------------------
# Coupling
# ---------------------------------------------------------------------------


def population(count: int, **cts: int) -> tuple[ComponentRow, ...]:
    """*count* components named C00, C01, …, each Ct 1, with *cts* overriding or adding."""
    base = {f"C{index:02d}": 1 for index in range(count)}
    return components(**{**base, **cts})


class TestCoupling:
    def test_not_available(self) -> None:
        assert sentences(state(), "coupling") == ("No component is declared, so dependencies are unanswered.",)
        assert limit(state(), "coupling") == "Each type counts once, however often it is placed."

    def test_hubs_one_in_k_takes_the_singular(self) -> None:
        # 2 hubs of 42 reduce to one in 21.
        current = state(population(40, Hub1=9, Hub2=8), hubs=("Hub1", "Hub2"))
        assert sentences(current, "coupling") == (
            "One component in 21 is a hub, with a Ct more than two deviations above this model's mean: Hub1, Hub2.",
        )

    def test_hubs_that_do_not_reduce_take_the_plural(self) -> None:
        # 3 of 40 does not reduce.
        current = state(population(37, X=9, Y=9, Z=9), hubs=("X", "Y", "Z"))
        assert sentences(current, "coupling") == (
            "3 of 40 components are hubs, with a Ct more than two deviations above this model's mean: X, Y, Z.",
        )

    def test_three_names_and_n_more(self) -> None:
        # 5 of 42 does not reduce; the hubs' order is theirs, highest first.
        current = state(population(37, E=9, D=10, C=11, B=12, A=13), hubs=("A", "B", "C", "D", "E"))
        assert sentences(current, "coupling")[0] == (
            "5 of 42 components are hubs, with a Ct more than two deviations above this model's mean: A, B, C, and "
            "2 more."
        )

    def test_no_hub(self) -> None:
        # Ct 3, 1, 1, 1, 1, 1: total 8 over 6, mean 4/3 = 1.33.
        current = state(population(5, Top=3), hubs=())
        assert sentences(current, "coupling") == (
            "No component is a hub: the highest Ct is 3, in Top, against a mean of 1.33.",
        )

    def test_no_hub_and_an_isolated_component(self) -> None:
        # Nine at Ct 20 and one at Ct 2: mean 18.2, deviation 5.4, lower bound 7.4, so Low is isolated and
        # no hub; the sentence says no hub and names the isolated one, never that none stands out.
        current = state(population(9, **{f"C{index:02d}": 20 for index in range(9)}, Low=2), isolated=("Low",))
        assert sentences(current, "coupling") == (
            "No component is a hub: the highest Ct is 20, in C00, C01, C02, and 6 more, against a mean of 18.20.",
            "One component in 10 is statistically isolated, with a Ct more than two deviations below this "
            "model's mean: Low.",
        )

    def test_hubs_and_isolated_components_are_both_said(self) -> None:
        # Thirty at Ct 10, Top at 20, Low at 0: 32 components, total 320 (even, as every link counts at both
        # ends; Top's 20 partners are among the thirty), mean 10, variance 200/32 = 6.25, deviation 2.5.
        # 20 > 15 makes Top the hub; 0 < 5 makes Low isolated, and mean² 100 >= 4σ² 25 lets isolated be
        # decided: figures a measured model can give, both lists non-empty.
        rows = components(**{f"C{index:02d}": 10 for index in range(30)}, Top=20, Low=0)
        measured = metrics._statistics(rows, 0, (), metrics.Links(frozenset()), 160)
        assert (measured.mean, measured.variance) == (10, Fraction(25, 4))
        assert (measured.hubs, measured.isolated) == (("Top",), ("Low",))
        current = state(rows, hubs=measured.hubs, isolated=measured.isolated)
        assert sentences(current, "coupling") == (
            "One component in 32 is a hub, with a Ct more than two deviations above this model's mean: Top.",
            "One component in 32 is statistically isolated, with a Ct more than two deviations below this "
            "model's mean: Low.",
            "One component in 32 depends on nobody and nobody depends on it: Low.",
        )

    def test_isolated_not_available_says_nothing_of_it(self) -> None:
        current = state(population(5, Top=3), isolated=NotAvailable(LOWER_THRESHOLD_BELOW_ZERO))
        assert sentences(current, "coupling") == (
            "No component is a hub: the highest Ct is 3, in Top, against a mean of 1.33.",
        )

    def test_no_hub_names_every_component_at_the_highest(self) -> None:
        # Ct 3, 3, 1, 1, 1, 1: total 10 over 6, mean 5/3 = 1.67; X and Y share the highest.
        current = state(population(4, X=3, Y=3), hubs=())
        assert sentences(current, "coupling") == (
            "No component is a hub: the highest Ct is 3, in X and Y, against a mean of 1.67.",
        )

    def test_hubs_not_available(self) -> None:
        # Three components: A 2, B 2, C 0; the highest is 2, held by A and B.
        current = state(components(A=2, B=2, C=0), hubs=NotAvailable(TOO_FEW_FOR_OUTLIERS))
        assert sentences(current, "coupling") == (
            "With 3 components no Ct can lie more than two deviations from the mean, so which components are "
            "hubs is unanswered; the highest Ct is 2, in A and B.",
            "One component in 3 depends on nobody and nobody depends on it: C.",
        )

    def test_nobody_in_the_plural(self) -> None:
        # 2 of 5 does not reduce: the verb and the pronoun are plural.
        current = state(components(A=2, B=1, C=1, X=0, Y=0), hubs=NotAvailable(TOO_FEW_FOR_OUTLIERS))
        assert sentences(current, "coupling")[1] == (
            "2 of 5 components depend on nobody and nobody depends on them: X, Y."
        )

    def test_the_only_component(self) -> None:
        current = state(components(A=0), hubs=NotAvailable(TOO_FEW_FOR_OUTLIERS))
        assert sentences(current, "coupling") == (
            "With 1 component no Ct can lie more than two deviations from the mean, so which components are "
            "hubs is unanswered; the highest Ct is 0, in A.",
            "The only component depends on nobody and nobody depends on it: A.",
        )

    def test_both_components(self) -> None:
        current = state(components(A=0, B=0), hubs=NotAvailable(TOO_FEW_FOR_OUTLIERS))
        assert sentences(current, "coupling")[1] == "Both components depend on nobody and nobody depends on them: A, B."

    def test_all_components(self) -> None:
        current = state(components(A=0, B=0, C=0, D=0), hubs=NotAvailable(TOO_FEW_FOR_OUTLIERS))
        assert sentences(current, "coupling")[1] == (
            "All 4 components depend on nobody and nobody depends on them: A, B, C, and 1 more."
        )

    @pytest.mark.parametrize(
        ("reasons", "expected"),
        [
            ((), "Each type counts once, however often it is placed."),
            (
                ((UNREACHED_END, 1),),
                "Giving no measured dependency: 1 connector with an end that reaches no component. "
                "Each type counts once, however often it is placed.",
            ),
            (
                ((ONE_DIRECTION, 1),),
                "Giving no measured dependency: 1 pair of ends of one direction. "
                "Each type counts once, however often it is placed.",
            ),
            (
                ((UNREACHED_END, 200), (ONE_DIRECTION, 21), (REPEATED_NAME, 2)),
                "Giving no measured dependency: 200 connectors with an end that reaches no component, 21 pairs of "
                "ends of one direction, 2 connectors repeating a name declared before. "
                "Each type counts once, however often it is placed.",
            ),
        ],
    )
    def test_the_limit_gives_each_reason_with_its_count(
        self, reasons: tuple[tuple[str, int], ...], expected: str
    ) -> None:
        assert limit(state(components(A=1), not_measured=reasons), "coupling") == expected


# ---------------------------------------------------------------------------
# The composition tables
# ---------------------------------------------------------------------------


def table(name: str, links: int, *rows: MemberRow) -> CompositionTable:
    return CompositionTable(name, rows, links)


def member(name: str, ca: int, ce: int, kind: str = "component") -> MemberRow:
    return MemberRow(name, kind, ca, ce, name)


def tables_of(*tables: CompositionTable, not_placed: tuple[tuple[str, int], ...] = ()) -> Conclusions:
    return conclude(report(state(components(A=1), tables=tables, not_placed=not_placed)))


VEHICLE_ROOT = table(
    "VehicleRoot",
    4,
    # Chassis and Powertrain each Ct 3, the stub ComM Ct 2, Logger 0: Ca sums 4, Ce sums 4.
    member("Chassis", 1, 2, "composition"),
    member("Powertrain", 1, 2, "composition"),
    member("ComM", 2, 0, "stub"),
    member("Logger", 0, 0),
)


class TestTables:
    def test_no_composition(self) -> None:
        assert section(tables_of(), "tables").sentences == (
            "The model declares no composition, so coupling inside a composition is unanswered.",
        )

    def test_no_link_in_any(self) -> None:
        conclusions = tables_of(
            table("Root", 0, member("A", 0, 0)), CompositionTable("Empty", NotAvailable(EMPTY_COMPOSITION), 0)
        )
        assert section(conclusions, "tables").sentences == ("No composition has a link among its members.",)

    def test_a_tie_for_the_most_links_names_both(self) -> None:
        # Wheels ties VehicleRoot at 4 links: both are named, by name. Three tables, one n/a.
        sub = table("Wheels", 4, *(member(name, 1, 1) for name in "PQRST"))
        empty = CompositionTable("Empty", NotAvailable(EMPTY_COMPOSITION), 0)
        assert section(tables_of(VEHICLE_ROOT, sub, empty), "tables").sentences == (
            "3 compositions; VehicleRoot and Wheels have the most links among their direct members: 4 each.",
        )

    def test_one_composition_one_link_agrees(self) -> None:
        assert section(tables_of(table("Root", 1, member("A", 0, 1), member("B", 1, 0))), "tables").sentences == (
            "1 composition; Root has the most links among its direct members: 1.",
        )

    def test_one_table_names_every_member_tied_at_the_most(self) -> None:
        # Chassis and Powertrain share Ct 3.
        assert dict(tables_of(VEHICLE_ROOT).tables) == {
            "VehicleRoot": "4 rows, 4 links; the most linked are Chassis and Powertrain (Ct 3); 1 has no link here."
        }

    def test_one_table_with_one_most_linked(self) -> None:
        # Hub Ct 3, the three others Ct 1.
        star = table("Star", 3, member("Hub", 0, 3), member("P", 1, 0), member("Q", 1, 0), member("R", 1, 0))
        assert dict(tables_of(star).tables) == {"Star": "4 rows, 3 links; the most linked is Hub (Ct 3)."}

    def test_more_than_three_tied_are_counted(self) -> None:
        ring = table("Ring", 5, *(member(name, 1, 1) for name in "ABCDE"))
        assert dict(tables_of(ring).tables) == {
            "Ring": "5 rows, 5 links; the most linked are A, B, C, and 2 more (Ct 2)."
        }

    def test_one_table_with_several_unlinked_and_no_unlinked(self) -> None:
        two = table("Two", 1, member("A", 0, 1), member("B", 1, 0), member("X", 0, 0), member("Y", 0, 0))
        linked = table("Linked", 1, member("A", 0, 1), member("B", 1, 0))
        assert dict(tables_of(two, linked).tables) == {
            "Two": "4 rows, 1 link; the most linked are A and B (Ct 1); 2 have no link here.",
            "Linked": "2 rows, 1 link; the most linked are A and B (Ct 1).",
        }

    def test_one_table_with_no_link(self) -> None:
        three = table("Three", 0, member("A", 0, 0), member("B", 0, 0), member("C", 0, 0))
        alone = table("Alone", 0, member("A", 0, 0))
        assert dict(tables_of(three, alone).tables) == {
            "Three": "3 rows, no link among them.",
            "Alone": "1 row, no link.",
        }

    def test_one_table_not_available(self) -> None:
        empty = CompositionTable("Empty", NotAvailable(EMPTY_COMPOSITION), 0)
        assert dict(tables_of(empty).tables) == {"Empty": "The composition has no member."}

    @pytest.mark.parametrize(
        ("reasons", "expected"),
        [
            ((), None),
            (((UNRESOLVED_OWNER, 1),), "Placed in no table: 1 connector whose composition does not resolve."),
            (
                ((END_NOT_A_MEMBER, 2), (UNRESOLVED_PORT, 1), (ONE_DIRECTION, 3)),
                "Placed in no table: 2 connectors with an end that is no member of its composition, 1 connector "
                "with a port that does not resolve, 3 connectors joining two ends of one direction.",
            ),
        ],
    )
    def test_the_limit_gives_each_reason_with_its_count(
        self, reasons: tuple[tuple[str, int], ...], expected: str | None
    ) -> None:
        assert section(tables_of(VEHICLE_ROOT, not_placed=reasons), "tables").limit == expected


# ---------------------------------------------------------------------------
# Cohesion
# ---------------------------------------------------------------------------


def cohesion(
    count: int,
    mean: Fraction,
    below: tuple[tuple[str, Fraction], ...] = (),
    not_available: tuple[tuple[str, int], ...] = (),
    runnables: tuple[int, int] = (61, 97),
) -> Cohesion:
    return Cohesion(count, mean, below, not_available, runnables[1], runnables[0])


class TestCohesion:
    def test_figures_with_some_below(self) -> None:
        below = (("V", Fraction(1, 3)), ("W", Fraction(2, 5)), ("X", Fraction(9, 20)), ("Y", Fraction(49, 100)))
        missing = tuple(zip(COHESION_REASONS, (2, 9, 2, 3), strict=True))
        current = state(components(A=1), cohesion=cohesion(26, Fraction(71, 100), below, missing))
        assert sentences(current, "cohesion") == (
            "Mean cohesion is 0.71 over 26 components; 4 fall below 0.5, a starting threshold not calibrated: V, W, "
            "X, and 1 more.",
        )
        # 2 + 9 + 2 + 3 = 16 components with no figure.
        assert limit(current, "cohesion") == (
            "Rests on the 61 runnables that use a port or a variable in the 26 components with a figure, of 97 "
            "runnables in "
            "the declared components; 16 components have no figure."
        )

    def test_one_below_agrees(self) -> None:
        current = state(
            components(A=1), cohesion=cohesion(2, Fraction(3, 4), (("V", Fraction(1, 2) - Fraction(1, 100)),))
        )
        assert sentences(current, "cohesion") == (
            "Mean cohesion is 0.75 over 2 components; 1 falls below 0.5, a starting threshold not calibrated: V.",
        )

    def test_none_below(self) -> None:
        current = state(
            components(A=1),
            cohesion=cohesion(1, Fraction(4, 5), not_available=((COHESION_REASONS[0], 1),), runnables=(1, 1)),
        )
        assert sentences(current, "cohesion") == (
            "Mean cohesion is 0.80 over 1 component; none falls below 0.5, a starting threshold not calibrated.",
        )
        assert limit(current, "cohesion") == (
            "Rests on the 1 runnable that uses a port or a variable in the 1 component with a figure, of 1 runnable in "
            "the "
            "declared components; 1 component has no figure."
        )

    def test_every_component_with_a_figure(self) -> None:
        current = state(components(A=1), cohesion=cohesion(3, Fraction(1), runnables=(7, 7)))
        assert limit(current, "cohesion") == (
            "Rests on the 7 runnables that use a port or a variable in the 3 components with a figure, of 7 runnables "
            "in the "
            "declared components."
        )

    def test_no_access(self) -> None:
        current = state(components(A=1), cohesion=NotAvailable(NO_COMPONENT_ACCESS))
        assert sentences(current, "cohesion") == (
            "No component's runnables declare an access, so whether components hold together is unanswered; "
            "reads, writes, calls, or variable reads and writes on runnables would answer it.",
        )
        assert limit(current, "cohesion") is None

    def test_no_component_gives_a_figure(self) -> None:
        current = state(components(A=1), cohesion=NotAvailable(NO_COHESION_FIGURE))
        assert sentences(current, "cohesion") == (
            "Some runnables declare an access, but no component's runnables give a figure, so whether components "
            "hold together is unanswered; a figure needs a component whose only runnable declares an access, or "
            "two runnables declaring accesses over more than one port or variable.",
        )


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def coverage_sentences(coverage: Coverage) -> tuple[str, ...]:
    return sentences(state(components(A=1), coverage=coverage), "coverage")


FULL_COVERAGE = Coverage(
    asil=AsilCoverage(42, 30, 8, ("A", "B", "C", "D")),
    runnable=RunnableCoverage(42, 40, ("AuditSink", "Logger")),
    task=TaskCoverage(86, 81, 11, ("A.r", "B.r", "C.r", "D.r", "E.r")),
    requirement=RequirementCoverage(
        120, 110, 0, tuple(f"R{index}" for index in range(10)), 42, 35, tuple(f"S{index}" for index in range(7))
    ),
)


class TestCoverage:
    def test_one_sentence_per_coverage_in_their_order(self) -> None:
        assert coverage_sentences(FULL_COVERAGE) == (
            "30 of 42 components declare a safety level and 8 inherit one above QM from their requirements; "
            "4 state none and inherit none above QM: A, B, C, and 1 more.",
            "40 of 42 components declare a runnable; without one: AuditSink, Logger.",
            "In the declared components, 81 of 86 non-server runnables are on a task; on none: A.r, B.r, C.r, "
            "and 2 more.",
            "110 of 120 requirements are allocated, and 35 of 42 components have a requirement link; "
            "not allocated: R0, R1, R2, and 7 more; no link to a declared requirement: S0, S1, S2, and 4 more.",
        )

    def test_the_singular_shares(self) -> None:
        coverage = Coverage(
            asil=AsilCoverage(8, 1, 6, ("Z",)),
            runnable=RunnableCoverage(1, 1, ()),
            task=TaskCoverage(2, 1, 0, ("X.r",)),
            requirement=RequirementCoverage(1, 1, 0, (), 2, 2, ()),
        )
        assert coverage_sentences(coverage) == (
            "One component in 8 declares a safety level and 6 inherit one above QM from their requirements; 1 "
            "states none and inherits none above QM: Z.",
            "The only component declares a runnable.",
            "In the declared components, one runnable in 2 is on a task; on none: X.r.",
            "The only requirement is allocated, and both components have a requirement link.",
        )

    def test_none_declared_one_inherits(self) -> None:
        coverage = dataclasses.replace(NO_COVERAGE, asil=AsilCoverage(3, 0, 1, ("A", "B")))
        assert coverage_sentences(coverage)[0] == (
            "No component declares a safety level and 1 inherits one above QM from its requirements; 2 state "
            "none and inherit none above QM: A, B."
        )

    def test_every_runnable_a_server(self) -> None:
        coverage = dataclasses.replace(FULL_COVERAGE, task=TaskCoverage(0, 0, 3, ()))
        assert coverage_sentences(coverage)[2] == (
            "Every runnable of the declared components is a server runnable, run in its callers' tasks, so none "
            "needs a task of its own."
        )

    def test_a_stubs_periodic_runnable_is_outside_the_task_sentences(self, tmp_path: Path) -> None:
        # The declared component's only runnable is a server runnable; the stub's periodic one, on no task,
        # is not read, so the sentence names the declared components and never claims every runnable.
        measured = side_of(
            model(
                model_component("A", server("serve")),
                model_component("S", periodic("run"), external=True),
                tasks=[task("T")],
            ),
            tmp_path,
        )
        assert measured.size.runnables == 2
        assert section(conclude(report(measured)), "coverage").sentences[2] == (
            "Every runnable of the declared components is a server runnable, run in its callers' tasks, so none "
            "needs a task of its own."
        )

    @pytest.mark.parametrize(
        ("periodic_names", "scheduled", "with_server", "expected"),
        [
            # Each form of the share, on a model where a declared server runnable and a stub's scheduled
            # periodic runnable both stand beside the counted ones: the share counts and names only the
            # declared components' runnables that are not server runnables.
            (("p1",), (), True, "In the declared components, no non-server runnable is on a task; on none: A.p1."),
            (("p1",), ("p1",), True, "In the declared components, the only non-server runnable is on a task."),
            (("p1", "p2"), ("p1", "p2"), True, "In the declared components, both non-server runnables are on a task."),
            (
                ("p1", "p2", "p3"),
                ("p1", "p2", "p3"),
                True,
                "In the declared components, all 3 non-server runnables are on a task.",
            ),
            (
                ("p1", "p2"),
                ("p1",),
                True,
                "In the declared components, one non-server runnable in 2 is on a task; on none: A.p2.",
            ),
            (
                ("p1", "p2", "p3"),
                ("p1", "p2"),
                True,
                "In the declared components, 2 of 3 non-server runnables are on a task; on none: A.p3.",
            ),
            # With no server runnable in the declared components every runnable is counted, so the share
            # says runnable; the stub's scheduled runnable is still outside it.
            (("p1",), (), False, "In the declared components, no runnable is on a task; on none: A.p1."),
            (("p1",), ("p1",), False, "In the declared components, the only runnable is on a task."),
            (
                ("p1", "p2"),
                ("p1",),
                False,
                "In the declared components, one runnable in 2 is on a task; on none: A.p2.",
            ),
            # Zero counted runnables: every one of the declared components is a server runnable.
            (
                (),
                (),
                True,
                "Every runnable of the declared components is a server runnable, run in its callers' tasks, so "
                "none needs a task of its own.",
            ),
            # No runnable in the declared components at all, the stub's aside.
            ((), (), False, "No declared component has a runnable, so task coverage is unanswered."),
        ],
    )
    def test_every_form_of_the_task_sentence_counts_what_the_figure_counts(
        self,
        tmp_path: Path,
        periodic_names: tuple[str, ...],
        scheduled: tuple[str, ...],
        with_server: bool,
        expected: str,
    ) -> None:
        runnables = [periodic(name) for name in periodic_names] + ([server("serve")] if with_server else [])
        entries = [{"swc": "A", "runnable": name} for name in scheduled] + [{"swc": "S", "runnable": "run"}]
        measured = side_of(
            model(
                model_component("A", *runnables),
                model_component("S", periodic("run"), external=True),
                tasks=[task("T", *entries)],
            ),
            tmp_path,
        )
        assert measured.size.runnables == len(runnables) + 1
        assert section(conclude(report(measured)), "coverage").sentences[2] == expected

    def test_the_task_sentence_without_a_task(self, tmp_path: Path) -> None:
        measured = side_of(
            model(
                model_component("A", periodic("p1"), server("serve")),
                model_component("S", periodic("run"), external=True),
            ),
            tmp_path,
        )
        assert section(conclude(report(measured)), "coverage").sentences[2] == (
            "The model has no task, so whether runnables are scheduled is unanswered."
        )

    def test_every_requirement_on_a_stub(self) -> None:
        coverage = dataclasses.replace(FULL_COVERAGE, requirement=RequirementCoverage(0, 0, 2, (), 2, 2, ()))
        assert coverage_sentences(coverage)[3] == (
            "Every requirement id is held by a stub requirement, a placeholder, so none counts as allocated, "
            "and both components have a requirement link."
        )

    def test_a_placeholder_and_a_requirement_sharing_an_id_are_one_id_held_by_a_stub(self, tmp_path: Path) -> None:
        # Two files declare R1, a placeholder and a requirement that is not one: the stub classification
        # is the id's, so R1 is excluded whole and the sentence speaks of ids, never of every declaration.
        measured = side_of(
            model(
                model_component("A", satisfies=["R1@1"]),
                requirements=[requirement("R1", stub=True), requirement("R1")],
            ),
            tmp_path,
        )
        assert measured.coverage.requirement == RequirementCoverage(0, 0, 1, (), 1, 1, ())
        # The one coverage with a number is the requirements', so its sentence is the headline.
        assert section(conclude(report(measured)), "coverage").sentences[0] == (
            "Every requirement id is held by a stub requirement, a placeholder, so none counts as allocated, "
            "and the only component has a requirement link."
        )
        assert "| Requirements allocated | 0 of 0; 1 id held by a stub requirement, a placeholder, excluded |" in (
            render_markdown(report(measured))
        )

    def test_every_reason_of_not_available(self) -> None:
        # The model holds no data of any kind: the headline is the first, the safety level.
        assert coverage_sentences(NO_COVERAGE) == (
            "No declared component or requirement states a safety level, so which components are safety-relevant "
            "is unanswered.",
            "No declared component has a runnable, so what the components run is unanswered.",
            "The model has no task, so whether runnables are scheduled is unanswered.",
            "The model declares no requirement, so what the components are traced to is unanswered.",
        )

    def test_a_runnable_on_a_stub_alone_is_not_one_of_a_declared_component(self, tmp_path: Path) -> None:
        # The stub's runnable is counted in Size; coverage reads the declared components, which have none.
        measured = side_of(
            model(
                swc("A", "pa"),
                model_component("S", periodic("run"), external=True),
                tasks=[task("T", {"swc": "S", "runnable": "run"})],
            ),
            tmp_path,
        )
        assert measured.size.runnables == 1
        assert section(conclude(report(measured)), "coverage").sentences[1:3] == (
            "No declared component has a runnable, so what the components run is unanswered.",
            "No declared component has a runnable, so task coverage is unanswered.",
        )

    def test_tasks_with_no_runnable(self) -> None:
        coverage = dataclasses.replace(NO_COVERAGE, task=NotAvailable(NO_RUNNABLE))
        assert coverage_sentences(coverage)[2] == (
            "No declared component has a runnable, so task coverage is unanswered."
        )

    def test_the_headline_is_the_first_that_is_a_number(self) -> None:
        coverage = dataclasses.replace(NO_COVERAGE, runnable=RunnableCoverage(2, 1, ("B",)))
        assert coverage_sentences(coverage) == (
            "One component in 2 declares a runnable; without one: B.",
            "No declared component or requirement states a safety level, so which components are safety-relevant "
            "is unanswered.",
            "The model has no task, so whether runnables are scheduled is unanswered.",
            "The model declares no requirement, so what the components are traced to is unanswered.",
        )


# ---------------------------------------------------------------------------
# A name two component files share: one rule for every section
# ---------------------------------------------------------------------------


def _plain_a(*, external: bool = False) -> SWComponent:
    """A requiring ``rX``, its one runnable reading it: no level, no requirement link, no task."""
    return model_component(
        "A",
        periodic("run", reads=[{"port": "rX", "element": "d"}]),
        external=external,
        ports=[{"name": "rX", "direction": "require", "interface": "I"}],
    )


def _rich_a(*, external: bool) -> SWComponent:
    """A providing ``pY`` with every field that would move a figure if this file were read.

    No connector names it; it has ASIL D, a link to R1, and two runnables on one port, one of them on a task."""
    return model_component(
        "A",
        periodic("s1", reads=[{"port": "pY", "element": "d"}]),
        periodic("s2", writes=[{"port": "pY", "element": "d"}]),
        external=external,
        asil="D",
        satisfies=["R1@1"],
        ports=[{"name": "pY", "direction": "provide", "interface": "I"}],
    )


def _repeated_name_side(tmp_path: Path, *files: SWComponent) -> ArchitectureState:
    """*files* of the name A beside B, which provides ``pX`` to A's ``rX`` in Root, measured."""
    b = model_component("B", ports=[{"name": "pX", "direction": "provide", "interface": "I"}])
    return side_of(
        model(
            *files,
            b,
            composition("Root", root=True, members=(("a", "A"), ("b", "B"))),
            connector("c", "B.pX", "A.rX"),
            requirements=[requirement("R1", asil="B")],
            tasks=[task("T", {"swc": "A", "runnable": "s1"})],
        ),
        tmp_path,
    )


#: Every section's sentences when A is read off the plain file: the rich file, a stub sharing the
#: declared name in either order or an earlier declared file, moves none of them.
_PLAIN_A_SENTENCES = {
    "size": ("The model declares 3 components and 1 composition, nested 1 deep.",),
    "coupling": (
        "With 2 components no Ct can lie more than two deviations from the mean, so which components are hubs "
        "is unanswered; the highest Ct is 1, in A and B.",
    ),
    "tables": ("1 composition; Root has the most links among its direct members: 1.",),
    "cohesion": ("Mean cohesion is 1.00 over 1 component; none falls below 0.5, a starting threshold not calibrated.",),
    "coverage": (
        "No component declares a safety level; 2 state none and inherit none above QM: A, B.",
        "One component in 2 declares a runnable; without one: B.",
        "In the declared components, no runnable is on a task; on none: A.run.",
        "No requirement is allocated, and no component has a requirement link; not allocated: R1; no link to a "
        "declared requirement: A, B.",
    ),
    "integrity": ("check-model reports no error and no warning.",),
}


class TestARepeatedComponentName:
    """One rule for a name two component files share, in every section.

    The name is read off its last declared file, or its last stub when no declared file holds it. Size counts every
    file."""

    @pytest.mark.parametrize(
        "order",
        ["declared, then a stub with ASIL D", "a stub with ASIL D, then declared", "declared twice, the last read"],
    )
    def test_every_section_reads_the_one_file(self, tmp_path: Path, order: str) -> None:
        files = {
            "declared, then a stub with ASIL D": (_plain_a(), _rich_a(external=True)),
            "a stub with ASIL D, then declared": (_rich_a(external=True), _plain_a()),
            "declared twice, the last read": (_rich_a(external=False), _plain_a()),
        }[order]
        measured = _repeated_name_side(tmp_path, *files)
        # Size counts both files of A as components, and no stub: the stub shares a declared name.
        assert (measured.size.components, measured.size.external_stubs) == (3, 0)
        assert measured.size.runnables == 3
        conclusions = conclude(report(measured))
        for key, expected in _PLAIN_A_SENTENCES.items():
            assert section(conclusions, key).sentences == expected, key
        assert conclusions.state_sentences == (
            "The model declares 3 components and 1 composition, nested 1 deep.",
            "check-model reports no error and no warning.",
            "With 2 components, which of them stand out by their links is not measured.",
            "In the one component that gives a figure, no fewer than half the pairs of runnables that use a port or a "
            "variable "
            "have one in common; half is a starting threshold, not calibrated.",
        )

    def test_a_declared_component_beside_a_stub_of_its_name_is_still_declared(self, tmp_path: Path) -> None:
        # A declared component followed by a stub of its name and nothing else: Size says one component,
        # and Coupling measures one, never "No component is declared".
        measured = side_of(model(swc("A", "pX"), swc("A", "pX", external=True)), tmp_path)
        assert (measured.size.components, measured.size.external_stubs) == (2, 0)
        assert section(conclude(report(measured)), "coupling").sentences[0].startswith("With 1 component ")

    def test_the_last_declared_file_is_read_whatever_an_earlier_one_holds(self, tmp_path: Path) -> None:
        # The other order of two declared files: the rich one is last, so it is A, and the plain file's
        # requirement on rX is read by no figure: rX is no port of A, so the connector resolves no end.
        measured = _repeated_name_side(tmp_path, _plain_a(), _rich_a(external=False))
        conclusions = conclude(report(measured))
        assert section(conclusions, "coverage").sentences == (
            "One component in 2 declares a safety level; 1 states none and inherits none above QM: B.",
            "One component in 2 declares a runnable; without one: B.",
            "In the declared components, one runnable in 2 is on a task; on none: A.s2.",
            "The only requirement is allocated, and one component in 2 has a requirement link; no link to a "
            "declared requirement: B.",
        )
        assert section(conclusions, "integrity").sentences == (
            "check-model reports no error and no warning.",
            "One component port in 2 is unconnected: A.pY.",
        )

    def test_the_findings_read_every_file_of_the_name(self, tmp_path: Path) -> None:
        # The first file of A is set aside by every figure below Size, yet check-model reads it: its port
        # on an undeclared interface is one more error, as the Size definition says.
        counts = {}
        for variant, first in (
            ("clean", component_yaml("A", "pX")),
            (
                "ghost",
                "name: A\ntype: application\nports:\n  - name: pG\n    direction: provide\n    interface: IGhost\n",
            ),
        ):
            project = bare_project(tmp_path / variant)
            write(project / "model" / "swc" / "g1" / "A.yaml", first)
            write(project / "model" / "swc" / "g2" / "A.yaml", component_yaml("A", "pX"))
            current = metrics_of(project).series[-1]
            # The set-aside file's port is in no figure: A is read off its last file, with pX alone.
            assert (current.integrity.ports, current.integrity.unconnected) == (1, ("A.pX",))
            counts[variant] = dict(current.integrity.by_rule)
        assert counts["ghost"].pop("port.undeclared-interface") == 1
        assert counts["ghost"] == counts["clean"]
        assert "but the errors and warnings, which check-model reports over every file" in dict(DEFINITIONS)["Size"]


class TestASoleRunnable:
    @pytest.mark.parametrize("ports", [("rX",), ("rX", "rY")])
    def test_it_has_no_pair_and_the_definitions_say_it_gives_1(self, tmp_path: Path, ports: tuple[str, ...]) -> None:
        # No pair, over one port or two, yet a figure of 1 on a measured model: the n/a definition names
        # this case as its exception and Cohesion's states it, so neither reads the figure as n/a.
        measured = side_of(model(model_component("A", accessing("a", *ports))), tmp_path)
        assert [(row.cohesion, row.measured_runnables) for row in measured.components] == [(Fraction(1), 1)]
        definitions = dict(DEFINITIONS)
        assert "save one whose only runnable uses a port or a variable, which gives 1" in definitions["n/a"]
        assert "the figure is 1 when that runnable is the component's only runnable" in definitions["Cohesion"]


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def integrity(
    errors: tuple[tuple[str, int], ...] = (),
    warnings: tuple[tuple[str, int], ...] = (),
    ports: int = 0,
    unconnected: tuple[str, ...] = (),
) -> Integrity:
    return Integrity(
        sum(count for _, count in errors), sum(count for _, count in warnings), errors, warnings, (), ports, unconnected
    )


class TestIntegrity:
    def test_errors(self) -> None:
        # connector 2 + safety 1 against trace 12: trace holds the most.
        current = state(components(A=1), integrity=integrity((("connector", 2), ("safety", 1)), (("trace", 12),)))
        assert sentences(current, "integrity") == (
            "check-model reports 3 errors and 12 warnings, most from trace rules: the model does not pass as it "
            "stands.",
        )

    def test_one_error_in_one_family(self) -> None:
        current = state(components(A=1), integrity=integrity((("connector", 1),)))
        assert sentences(current, "integrity") == (
            "check-model reports 1 error and no warning, all from connector rules: the model does not pass as it "
            "stands.",
        )

    def test_warnings_only(self) -> None:
        current = state(components(A=1), integrity=integrity(warnings=(("trace", 1),)))
        assert sentences(current, "integrity") == ("check-model reports no error and 1 warning, all from trace rules.",)

    def test_warnings_of_two_families_tied_name_both(self) -> None:
        current = state(components(A=1), integrity=integrity(warnings=(("naming", 2), ("identifier", 2))))
        assert sentences(current, "integrity") == (
            "check-model reports no error and 4 warnings, the largest shares from identifier and naming rules "
            "(2 of 4 each).",
        )

    def test_the_largest_family_under_half_is_not_most(self) -> None:
        # hardware 3 of 10: the largest family, well under half, so it is no "most".
        errors = (("connector", 2), ("hardware", 3), ("name", 2), ("safety", 1), ("trace", 2))
        current = state(components(A=1), integrity=integrity(errors))
        assert sentences(current, "integrity") == (
            "check-model reports 10 errors and no warning, the largest share from hardware rules (3 of 10): the "
            "model does not pass as it stands.",
        )
        # It is the first answer's second sentence as well.
        assert conclude(report(current)).state_sentences[1] == sentences(current, "integrity")[0]

    def test_exactly_half_is_not_most(self) -> None:
        current = state(components(A=1), integrity=integrity(warnings=(("naming", 2), ("trace", 1), ("name", 1))))
        assert sentences(current, "integrity") == (
            "check-model reports no error and 4 warnings, the largest share from naming rules (2 of 4).",
        )

    def test_more_than_half_is_most(self) -> None:
        current = state(components(A=1), integrity=integrity(warnings=(("naming", 3), ("trace", 2))))
        assert sentences(current, "integrity") == (
            "check-model reports no error and 5 warnings, most from naming rules.",
        )

    def test_clean(self) -> None:
        assert sentences(state(components(A=1)), "integrity") == ("check-model reports no error and no warning.",)

    def test_unconnected_one_in_k(self) -> None:
        # 4 of 296 reduce to one in 74.
        current = state(components(A=1), integrity=integrity(ports=296, unconnected=("A.p", "B.p", "C.p", "D.p")))
        assert (
            sentences(current, "integrity")[1] == "One component port in 74 is unconnected: A.p, B.p, C.p, and 1 more."
        )

    def test_unconnected_in_the_plural(self) -> None:
        current = state(components(A=1), integrity=integrity(ports=10, unconnected=("A.p", "B.p", "C.p")))
        assert sentences(current, "integrity")[1] == "3 of 10 component ports are unconnected: A.p, B.p, C.p."

    def test_the_limit(self) -> None:
        assert limit(state(components(A=1)), "integrity") == (
            "Ports of external stubs and of composition boundaries are left out, and a port delegated to an "
            "unwired boundary port counts as connected."
        )


# ---------------------------------------------------------------------------
# Movement and the second answer
# ---------------------------------------------------------------------------


def moved(base: ArchitectureState, current: ArchitectureState) -> tuple[str, ...]:
    return section(conclude(report(current, base=base)), "movement").sentences


def at_base(rows: tuple[ComponentRow, ...], **kwargs: object) -> ArchitectureState:
    return state(rows, label=BASE, **kwargs)  # type: ignore[arg-type]


def finding(index: int) -> ConsistencyIssue:
    return ConsistencyIssue("trace.x", f"finding {index}")


class TestMovement:
    def test_a_rise_most_in_at_exactly_half(self) -> None:
        # 30 → 34: 4/30 = 13.33 %, 13 %. Rises A +2, B +1, C +1: A holds exactly half, so "most in".
        base, current = at_base(components(A=10, B=10, C=10)), state(components(A=12, B=11, C=11))
        assert moved(base, current) == (
            "Dependencies between components grew by 13 % since v1 (abc1234), from 30 to 34, most in A (Ct +2).",
        )

    def test_a_rise_mostly_in_at_just_over_half(self) -> None:
        # 30 → 35: 16.67 %, 17 %. Rises A +3, B +1, C +1: 3 of 5 is over half.
        base, current = at_base(components(A=10, B=10, C=10)), state(components(A=13, B=11, C=11))
        assert moved(base, current) == (
            "Dependencies between components grew by 17 % since v1 (abc1234), from 30 to 35, mostly in A (Ct +3).",
        )

    def test_a_fall_mostly_in(self) -> None:
        # 30 → 26: 13.33 %, 13 %. Falls A −3, C −1: 3 of 4 is over half.
        base, current = at_base(components(A=10, B=10, C=10)), state(components(A=7, B=10, C=9))
        assert moved(base, current) == (
            "Dependencies between components fell by 13 % since v1 (abc1234), from 30 to 26, mostly in A (Ct −3).",
        )

    def test_a_fall_most_in_at_exactly_half(self) -> None:
        # 30 → 26 again, by A −2, B −1, C −1: A holds exactly half.
        base, current = at_base(components(A=10, B=10, C=10)), state(components(A=8, B=9, C=9))
        assert moved(base, current) == (
            "Dependencies between components fell by 13 % since v1 (abc1234), from 30 to 26, most in A (Ct −2).",
        )

    def test_by_under_one_percent(self) -> None:
        # 300 → 301: 0.33 %, which rounds to 0 from a change.
        base, current = at_base(components(A=100, B=100, C=100)), state(components(A=101, B=100, C=100))
        assert moved(base, current) == (
            "Dependencies between components grew by under 1 % since v1 (abc1234), from 300 to 301, mostly in A (Ct "
            "+1).",
        )

    def test_half_a_percent_rounds_up(self) -> None:
        # 200 → 201: 0.5 % exactly, half-up to 1 %.
        base, current = at_base(components(A=100, B=100)), state(components(A=101, B=100))
        assert moved(base, current)[0] == (
            "Dependencies between components grew by 1 % since v1 (abc1234), from 200 to 201, mostly in A (Ct +1)."
        )

    def test_from_a_base_of_zero(self) -> None:
        # No percentage from 0. A +1 and B +1 tie at exactly half: both are named.
        base, current = at_base(components(A=0, B=0)), state(components(A=1, B=1))
        assert moved(base, current) == (
            "Dependencies between components grew from 0 to 2 since v1 (abc1234), most in A and B (Ct +1 each).",
        )

    def test_from_zero_to_one_dependency(self) -> None:
        base, current = at_base(components(A=0, B=0)), state(components(A=1, B=0))
        assert moved(base, current) == (
            "Dependencies between components grew from 0 to 1 since v1 (abc1234), mostly in A (Ct +1).",
        )

    def test_where_names_only_a_component_declared_at_both_sides(self) -> None:
        # 5 → 8: 60 %. Added +2 is new, so the rise is named in A (+1), the one on both sides, as the
        # rises of what to look at are; A holds 1 of 3, so "most in". The mean 2.50 → 2.67 rose too.
        # The sentence says the restriction, since Added moved more than A.
        base, current = at_base(components(A=3, B=2)), state(components(A=4, Added=2, B=2))
        assert moved(base, current)[0] == (
            "Dependencies between components grew by 60 % since v1 (abc1234), from 5 to 8, most in A (Ct +1) among "
            "the components present at both revisions."
        )

    def test_a_held_total_says_the_restriction_when_an_added_component_moved(self) -> None:
        # 3 → 3: A −1, B −1 and New +2 (added); the most named is among A and B.
        base, current = at_base(components(A=2, B=1)), state(components(A=1, B=0, New=2))
        assert moved(base, current)[0] == (
            "Dependencies between components held at 3 since v1 (abc1234), though 3 components' Ct changed, most "
            "in A and B (Ct −1 each) among the components present at both revisions."
        )

    def test_a_rise_only_in_added_components_names_none(self) -> None:
        # 1 → 3: 200 %; only New rose, and it is new.
        base, current = at_base(components(A=1)), state(components(A=1, New=2))
        assert (
            moved(base, current)[0] == "Dependencies between components grew by 200 % since v1 (abc1234), from 1 to 3."
        )

    def test_a_rise_beside_a_falling_mean_states_both(self) -> None:
        # 5 → 6: 20 %, all of it A's +1; the mean 5/2 = 2.50 → 6/3 = 2.00 fell, New adding no link.
        base, current = at_base(components(A=3, B=2)), state(components(A=4, B=2, New=0))
        assert moved(base, current)[0] == (
            "Dependencies between components grew by 20 % since v1 (abc1234), from 5 to 6, mostly in A (Ct +1); "
            "the mean Ct per component fell from 2.50 to 2.00."
        )

    def test_a_fall_beside_a_rising_mean_states_both(self) -> None:
        # 6 → 5: 17 %, A −1; Gone (Ct 0) removed: the mean 6/3 = 2.00 → 5/2 = 2.50 rose.
        base, current = at_base(components(A=4, B=2, Gone=0)), state(components(A=3, B=2))
        assert moved(base, current)[0] == (
            "Dependencies between components fell by 17 % since v1 (abc1234), from 6 to 5, mostly in A (Ct −1); "
            "the mean Ct per component rose from 2.00 to 2.50."
        )

    def test_equal_totals_with_moved_rows(self) -> None:
        # 6 → 6; A −2, B +2: two moved by the same size, each named with its sign, by name.
        base, current = at_base(components(A=3, B=2, C=1)), state(components(A=1, B=4, C=1))
        assert moved(base, current) == (
            "Dependencies between components held at 6 since v1 (abc1234), though 2 components' Ct changed, most in "
            "A (Ct −2) and B (Ct +2).",
        )

    def test_equal_totals_with_one_row_moving_most(self) -> None:
        # 6 → 6; A −2, B +1, C +1.
        base, current = at_base(components(A=3, B=2, C=1)), state(components(A=1, B=3, C=2))
        assert moved(base, current) == (
            "Dependencies between components held at 6 since v1 (abc1234), though 3 components' Ct changed, most in "
            "A (Ct −2).",
        )

    def test_equal_totals_with_no_row_moved(self) -> None:
        # Only an error appeared: the total held and nothing moved inside it.
        base = at_base(components(A=1, B=1))
        current = state(components(A=1, B=1), integrity=integrity((("connector", 1),)))
        assert moved(base, current)[0] == "Dependencies between components held at 2 since v1 (abc1234)."

    def test_a_total_back_at_the_base_is_not_said_to_have_held(self) -> None:
        # 1 → 2 → 1: equal at both ends, not between, so "held" would be false of the middle revision.
        base = at_base(components(A=1, B=0))
        middle = state(components(A=1, B=1), label="HEAD (def5678)")
        current = state(components(A=1, B=0))
        series = (base, middle, current)
        conclusions = conclude(MetricsReport("demo", series, compare(series), None, ()))
        assert section(conclusions, "movement").sentences[0] == (
            "Dependencies between components are back at 1, as at v1 (abc1234)."
        )
        # Equal at every revision of the series, an error appearing beside it: held.
        errored = state(components(A=1, B=0), integrity=integrity((("connector", 1),)))
        series = (base, dataclasses.replace(current, label="HEAD (def5678)"), errored)
        conclusions = conclude(MetricsReport("demo", series, compare(series), None, ()))
        assert section(conclusions, "movement").sentences[0] == (
            "Dependencies between components held at 1 since v1 (abc1234)."
        )

    def test_components_added(self) -> None:
        base = at_base(components(A=1, B=1))
        current = state(components(A=1, B=1, ParkAssist=0, ThermalGuard=0))
        assert moved(base, current) == (
            "Dependencies between components held at 2 since v1 (abc1234).",
            "2 components added: ParkAssist, ThermalGuard.",
        )

    def test_components_added_and_removed(self) -> None:
        base = at_base(components(A=1, Old1=0, Old2=0))
        current = state(components(A=1, New=0))
        assert moved(base, current)[1] == "1 component added: New; 2 removed: Old1, Old2."

    def test_a_component_removed(self) -> None:
        base, current = at_base(components(A=1, Old=0)), state(components(A=1))
        assert moved(base, current)[1] == "1 component removed: Old."

    def test_findings(self) -> None:
        # The base's 13: 4 resolved, 9 still open; 2 introduced.
        base_findings = tuple(finding(index) for index in range(13))
        current_findings = base_findings[4:] + (finding(13), finding(14))
        base = at_base(components(A=1), findings=base_findings)
        current = state(components(A=1), findings=current_findings)
        assert moved(base, current) == (
            "Dependencies between components held at 1 since v1 (abc1234).",
            "2 findings introduced and 4 resolved since v1 (abc1234); 9 of the base's 13 are still open.",
        )

    def test_no_finding_moved(self) -> None:
        base_findings = tuple(finding(index) for index in range(35))
        base = at_base(components(A=1), findings=base_findings)
        current = state(components(A=1, B=0), findings=base_findings)
        assert moved(base, current)[2] == (
            "No finding introduced or resolved since v1 (abc1234); all 35 of the base's findings are still open."
        )

    def test_one_finding_of_the_base_still_open(self) -> None:
        base = at_base(components(A=1), findings=(finding(0), finding(1)))
        current = state(components(A=1), findings=(finding(1),))
        assert moved(base, current)[1] == (
            "0 findings introduced and 1 resolved since v1 (abc1234); 1 of the base's 2 is still open."
        )

    def test_every_finding_of_the_base_resolved(self) -> None:
        base = at_base(components(A=1), findings=(finding(0), finding(1)))
        current = state(components(A=1))
        assert moved(base, current)[1] == (
            "0 findings introduced and 2 resolved since v1 (abc1234); none of the base's 2 is still open."
        )

    def test_the_base_s_one_finding(self) -> None:
        base = at_base(components(A=1), findings=(finding(0),))
        still = state(components(A=1, B=0), findings=(finding(0),))
        gone = state(components(A=1))
        assert moved(base, still)[2] == (
            "No finding introduced or resolved since v1 (abc1234); the base's one finding is still open."
        )
        assert moved(base, gone)[1] == (
            "0 findings introduced and 1 resolved since v1 (abc1234); the base's one finding is resolved."
        )

    def test_one_finding_introduced_from_none(self) -> None:
        base = at_base(components(A=1))
        current = state(components(A=1), findings=(finding(0),))
        assert moved(base, current)[1] == "1 finding introduced; the base had none."

    def test_one_side_not_available(self) -> None:
        base = at_base(components(A=1), cohesion=NotAvailable(NO_COMPONENT_ACCESS))
        current = state(components(A=1), cohesion=cohesion(1, Fraction(1), runnables=(1, 1)))
        assert moved(base, current) == (
            "Dependencies between components held at 1 since v1 (abc1234).",
            "Mean cohesion cannot be compared: it is n/a at v1 (abc1234).",
            "Cohesion below 0.5 (not calibrated) cannot be compared: it is n/a at v1 (abc1234).",
        )

    def test_the_working_tree_side_not_available_takes_its_article(self) -> None:
        base = at_base(components(A=1), cohesion=cohesion(1, Fraction(1), runnables=(1, 1)))
        current = state(components(A=1), cohesion=NotAvailable(NO_COMPONENT_ACCESS))
        assert moved(base, current)[1] == "Mean cohesion cannot be compared: it is n/a at the working tree."

    def test_no_component_at_either_side(self) -> None:
        base, current = at_base((), compositions=1), state((), compositions=2)
        assert moved(base, current) == (
            "Neither v1 (abc1234) nor the working tree declares a component, so dependencies cannot be compared.",
        )

    def test_nothing_differs(self) -> None:
        base, current = at_base(components(A=1)), state(components(A=1))
        assert moved(base, current) == ("No figure this report follows changed since v1 (abc1234).",)

    def test_the_second_answer_is_short_and_the_section_keeps_the_detail(self) -> None:
        base = at_base(components(A=10, B=10, C=10))
        current = state(components(A=12, B=11, C=11, New=0))
        conclusions = conclude(report(current, base=base))
        # The overview says the change and not where it shows, so it names no Ct.
        assert conclusions.heading_answer == (
            "Dependencies between components grew by 13 % since v1 (abc1234), from 30 to 34. 1 component added: New."
        )
        assert section(conclusions, "movement").text == (
            "Dependencies between components grew by 13 % since v1 (abc1234), from 30 to 34, most in A (Ct +2); the "
            "mean Ct per component "
            "fell from 10.00 to 8.50. 1 component added: New."
        )

    def test_the_second_answer_says_a_held_total_in_plain_words(self) -> None:
        base, current = at_base(components(A=3, B=2, C=1)), state(components(A=1, B=4, C=1))
        assert conclude(report(current, base=base)).heading_sentences == (
            "Dependencies between components held at 6 since v1 (abc1234), though the dependencies, in and out, "
            "of 2 components present at both revisions changed.",
        )

    def test_the_second_answer_keeps_the_findings_and_leaves_an_n_a_side_to_the_section(self) -> None:
        base = at_base(components(A=1), cohesion=cohesion(1, Fraction(1), runnables=(1, 1)))
        current = state(components(A=1), cohesion=NotAvailable(NO_COMPONENT_ACCESS))
        conclusions = conclude(report(current, base=base))
        assert (
            "Mean cohesion cannot be compared: it is n/a at the working tree."
            in section(conclusions, "movement").sentences
        )
        assert conclusions.heading_sentences == ("Dependencies between components held at 1 since v1 (abc1234).",)

    def test_no_base_named(self) -> None:
        conclusions = conclude(report(state(components(A=1))))
        assert conclusions.heading_answer == (
            "No base revision was named, so the direction is unanswered; --since names one."
        )
        assert NO_BASE_NAMED == conclusions.heading_answer
        assert conclusions.section("movement") is None


def wired_project(directory: Path) -> tuple[Path, Path]:
    """A committed model of three components, A providing to B, C with a require port nothing wires.

    Ct: A 1, B 1, C 0; one dependency, B on A. Returns the project and its ``swc`` directory.
    """
    project = bare_project(directory)
    model_root = project / "model"
    write(model_root / "swc" / "A.yaml", component_yaml("A", "pA1", "pA2"))
    write(model_root / "swc" / "B.yaml", component_yaml("B", "rB"))
    write(model_root / "swc" / "C.yaml", component_yaml("C", "rC"))
    write(model_root / "connectors" / "AB.yaml", connector_yaml("AB", "A.pA1", "B.rB"))
    commit(project)
    return project, model_root


def measured(project: Path, *since: str) -> tuple[tuple[str, ...], str]:
    """The movement section's sentences and the second answer of a measured report."""
    conclusions = conclude(metrics_of(project, *since))
    return section(conclusions, "movement").sentences, conclusions.heading_answer


class TestMeasuredMovement:
    """The movement sentences on two measured models, a link adding one to the Ct of both its ends."""

    def test_one_link_added_between_two_components_at_both_revisions(self, tmp_path: Path) -> None:
        # A.pA2 to C.rC: one dependency more, C on A. Ct A 1 -> 2, C 0 -> 1: two rises of 1, tied, so
        # "most in" both. 1 -> 2 is 100 %; the mean 2/3 -> 4/3 rose with it, so it is not said.
        project, model_root = wired_project(tmp_path)
        write(model_root / "connectors" / "AC.yaml", connector_yaml("AC", "A.pA2", "C.rC"))
        sentences, heading = measured(project, "HEAD")
        base = f"HEAD ({short_sha(project)})"
        assert sentences[0] == (
            f"Dependencies between components grew by 100 % since {base}, from 1 to 2, most in A and C (Ct +1 each)."
        )
        assert heading.startswith(f"Dependencies between components grew by 100 % since {base}, from 1 to 2. ")

    def test_a_rise_carried_by_an_added_component(self, tmp_path: Path) -> None:
        # D added, wired from A.pA2: Ct A 1 -> 2, D none -> 1. Of the two rises only A's is at both
        # revisions; it holds half of the whole 2, not more, so "most in", and D is the added sentence's.
        project, model_root = wired_project(tmp_path)
        write(model_root / "swc" / "D.yaml", component_yaml("D", "rD"))
        write(model_root / "connectors" / "AD.yaml", connector_yaml("AD", "A.pA2", "D.rD"))
        sentences, _ = measured(project, "HEAD")
        base = f"HEAD ({short_sha(project)})"
        assert sentences[:2] == (
            f"Dependencies between components grew by 100 % since {base}, from 1 to 2, most in A (Ct +1) among "
            "the components present at both revisions.",
            "1 component added: D.",
        )

    def test_a_link_to_a_stub_rises_mostly_in_its_one_declared_end(self, tmp_path: Path) -> None:
        # A stub S wired from A.pA2: the pair S on A counts, the stub has no row. A 1 -> 2 is the whole
        # rise, over half of it: "mostly in". The mean 1 -> 3/2 rose with it.
        project, model_root = wired_project(tmp_path)
        stub_file = component_yaml("S", "rS") + (
            "external: true\nexternal_ref: /Other/S\nexternal_dest: APPLICATION-SW-COMPONENT-TYPE\n"
        )
        write(model_root / "swc" / "S.yaml", stub_file)
        write(model_root / "connectors" / "AS.yaml", connector_yaml("AS", "A.pA2", "S.rS"))
        sentences, _ = measured(project, "HEAD")
        base = f"HEAD ({short_sha(project)})"
        assert sentences[0] == (
            f"Dependencies between components grew by 100 % since {base}, from 1 to 2, mostly in A (Ct +1)."
        )

    def test_a_link_removed(self, tmp_path: Path) -> None:
        # AB removed: no dependency left. Ct A 1 -> 0, B 1 -> 0; the mean 2/3 -> 0 fell with it.
        project, model_root = wired_project(tmp_path)
        (model_root / "connectors" / "AB.yaml").unlink()
        sentences, _ = measured(project, "HEAD")
        base = f"HEAD ({short_sha(project)})"
        assert sentences[0] == (
            f"Dependencies between components fell by 100 % since {base}, from 1 to 0, most in A and B (Ct −1 each)."
        )

    def test_a_held_total_with_moved_rows(self, tmp_path: Path) -> None:
        # AB rewired to C: still one dependency, now C on A. Ct A stays 1, B 1 -> 0, C 0 -> 1.
        project, model_root = wired_project(tmp_path)
        (model_root / "connectors" / "AB.yaml").unlink()
        write(model_root / "connectors" / "AC.yaml", connector_yaml("AC", "A.pA1", "C.rC"))
        sentences, heading = measured(project, "HEAD")
        base = f"HEAD ({short_sha(project)})"
        assert sentences[0] == (
            f"Dependencies between components held at 1 since {base}, though 2 components' Ct changed, most in "
            "B (Ct −1) and C (Ct +1)."
        )
        assert heading.startswith(
            f"Dependencies between components held at 1 since {base}, though the dependencies, in and out, of 2 "
            "components present at both revisions changed."
        )

    def test_a_stub_promoted_to_a_component_changes_no_link(self, tmp_path: Path) -> None:
        # S, a stub wired from A.pA2, becomes a declared component; no connector moves. The dependencies
        # hold at 2 and only S, absent as a component at the base, has a Ct move: the overview says no
        # component changed its dependencies, and S is the added sentence's.
        project, model_root = wired_project(tmp_path)
        stub_file = component_yaml("S", "rS") + (
            "external: true\nexternal_ref: /Other/S\nexternal_dest: APPLICATION-SW-COMPONENT-TYPE\n"
        )
        write(model_root / "swc" / "S.yaml", stub_file)
        write(model_root / "connectors" / "AS.yaml", connector_yaml("AS", "A.pA2", "S.rS"))
        commit(project)
        write(model_root / "swc" / "S.yaml", component_yaml("S", "rS"))
        _, heading = measured(project, "HEAD")
        base = f"HEAD ({short_sha(project)})"
        assert heading.startswith(f"Dependencies between components held at 2 since {base}. 1 component added: S.")
        assert "gained or lost" not in heading and "in and out" not in heading

    def test_a_change_only_movement_does_not_follow_reads_as_no_figure_changed(self, tmp_path: Path) -> None:
        # A second interface, which C's port now uses: a size row Movement does not follow, and no
        # finding moves. The sentence claims no more than that.
        project, model_root = wired_project(tmp_path)
        write(
            model_root / "interfaces" / "ISecond.yaml",
            "name: ISecond\nkind: client_server\noperations:\n  - name: op\n",
        )
        write(model_root / "swc" / "C.yaml", component_yaml("C", "rC").replace("IExample", "ISecond"))
        sentences, heading = measured(project, "HEAD")
        expected = f"No figure this report follows changed since HEAD ({short_sha(project)})."
        assert (sentences, heading) == ((expected,), expected)

    def test_a_column_between_the_two_ends_that_differs_is_a_change(self, tmp_path: Path) -> None:
        # HEAD~1 is the working tree's model; HEAD added D. The two ends agree, the middle does not: the
        # table stands and the section speaks of the ends.
        project, model_root = wired_project(tmp_path)
        write(model_root / "swc" / "D.yaml", component_yaml("D", "rD"))
        commit(project)
        (model_root / "swc" / "D.yaml").unlink()
        report_of_three = metrics_of(project, "HEAD~1", "HEAD")
        assert report_of_three.movement is not None
        assert not report_of_three.movement.unchanged
        base = f"HEAD~1 ({short_sha(project, 'HEAD~1')})"
        assert section(conclude(report_of_three), "movement").sentences[0] == (
            f"Dependencies between components held at 1 since {base}."
        )
        assert "| Components | 3 | 4 | 3 | 0 |" in render_markdown(report_of_three)


#: The five causes a base revision cannot be read, each with its fixed sentence; the one that does not
#: parse names the file by its storage name, and the parser's own reason follows, which is libyaml's
#: and not pinned here.
HEAD_CAUSES = {
    "no git repository": "Nothing to compare: the project is not in a git repository.",
    "no commit": "Nothing to compare: the current branch has no commit yet.",
    "no model at the commit": "Nothing to compare: HEAD holds no model yet.",
    "a retired layout at the commit": "Nothing to compare: HEAD keeps the model in a retired layout.",
    "a file at the commit that does not parse": (
        "Nothing to compare: a model file at HEAD does not parse: Invalid task YAML in tasks/Task_10ms.yaml: "
    ),
}


class TestUnreadableHead:
    def test_every_cause_has_its_sentence(self) -> None:
        assert tuple(HEAD_CAUSES) == HEAD_STATES

    @pytest.mark.parametrize("kind", HEAD_STATES)
    def test_the_movement_and_the_second_answer_give_the_cause(self, tmp_path: Path, kind: str) -> None:
        conclusions = conclude(metrics_of(head_state(kind, tmp_path), since_head=True))
        (sentence,) = section(conclusions, "movement").sentences
        assert conclusions.heading_answer == sentence
        if kind == "a file at the commit that does not parse":
            assert sentence.startswith(HEAD_CAUSES[kind]) and sentence.endswith(".")
        else:
            assert sentence == HEAD_CAUSES[kind]


# ---------------------------------------------------------------------------
# What to look at
# ---------------------------------------------------------------------------


def lines(*keys: str) -> tuple[LookAtLine, ...]:
    return tuple(LookAtLine(key, (LookAtEntry("X", 1),), 0) for key in keys)


class TestLookAt:
    def test_lines(self) -> None:
        look = lines("errors", "hubs", "no link", "low cohesion", "unscheduled", "rose most")
        current = state(components(A=1))
        conclusions = conclude(report(current, look_at=look))
        assert section(conclusions, "look_at").sentences == ("6 lists below have content; the first is errors.",)

    def test_one_line(self) -> None:
        conclusions = conclude(report(state(components(A=1)), look_at=lines("unscheduled")))
        assert section(conclusions, "look_at").sentences == ("1 list below has content: runnables on no task.",)

    def test_the_first_line_keeps_its_threshold_note(self) -> None:
        conclusions = conclude(report(state(components(A=1)), look_at=lines("low cohesion", "unscheduled")))
        assert section(conclusions, "look_at").sentences == (
            "2 lists below have content; the first is cohesion below 0.5, not calibrated.",
        )

    def test_none(self) -> None:
        conclusions = conclude(report(state(components(A=1))))
        assert section(conclusions, "look_at") == SectionConclusion(
            ("No list below has content.",), "Each line is a place to look, not a defect."
        )


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def full_state(tmp_path: Path) -> ArchitectureState:
    """A measured model with a hub, a stub, two compositions, and every coverage a number."""
    providers = [swc(name, "pa") for name in "BCDE"]
    measured = side_of(
        model(
            swc("H", "ra", "rb", "rc", "rd", "re"),
            swc("A", "pa", "rs"),
            *providers,
            stub("S", "ps"),
            composition("Root", root=True, members=(("h", "H"), ("a", "A"), ("b", "B"), ("s", "S"))),
            composition("Empty"),
            connector("ca", "A.pa", "H.ra"),
            connector("cb", "B.pa", "H.rb"),
            connector("cc", "C.pa", "H.rc"),
            connector("cd", "D.pa", "H.rd"),
            connector("ce", "E.pa", "H.re"),
            connector("cs", "S.ps", "A.rs"),
        ),
        tmp_path,
    )
    return dataclasses.replace(
        measured,
        cohesion=cohesion(3, Fraction(2, 3), (("B", Fraction(1, 3)),), ((COHESION_REASONS[0], 3),), (4, 7)),
        coverage=FULL_COVERAGE,
        integrity=integrity((("connector", 2),), (("trace", 3),), 12, ("H.rx",)),
    )


def fresh_state(tmp_path: Path) -> ArchitectureState:
    """A fresh import's shape: components and a connector, and no runnable, task, requirement or level."""
    return side_of(model(swc("A", "pa"), swc("B", "ra"), connector("c", "A.pa", "B.ra")), tmp_path)


def sample_reports(tmp_path: Path) -> list[MetricsReport]:
    full, fresh = full_state(tmp_path), fresh_state(tmp_path)
    return [
        report(full, look_at=lines("errors", "low cohesion")),
        report(fresh),
        report(state()),
        report(full, base=dataclasses.replace(fresh, label=BASE)),
        report(full, base=dataclasses.replace(full, label=BASE)),
        report(fresh, no_base="not a git repository: no HEAD"),
        # A held total with moved rows: the second answer counts them in plain words.
        report(state(components(A=1, B=4, C=1)), base=at_base(components(A=3, B=2, C=1))),
        report(
            state(
                components(A=0, B=0), hubs=NotAvailable(TOO_FEW_FOR_OUTLIERS), cohesion=NotAvailable(NO_COHESION_FIGURE)
            )
        ),
    ]


def every_sentence(conclusions: Conclusions) -> list[str]:
    found = [conclusions.state_answer, conclusions.heading_answer, *(sentence for _, sentence in conclusions.tables)]
    for _, conclusion in conclusions.sections:
        found += [*conclusion.sentences, *([conclusion.limit] if conclusion.limit else [])]
    return found


VERDICT_WORDS = re.compile(r"\b(good|bad|healthy|poor|risky|critical|score)\b", re.I)


#: The symbols and technical terms the definitions define: an answer of the overview stands alone, so
#: it uses none of them. A dependency and a finding keep their everyday sense and are not here.
TECHNICAL_WORDS = re.compile(r"\b(Ct|Ca|Ce|hubs?|deviations?|cohesion|stubs?|n/a)\b", re.I)


MOVEMENT_KEY = "movement"


def own_section(sentence: str) -> str:
    """The section a plain-words sentence of the first answer shortens."""
    if "pairs of runnables" in sentence:
        return "cohesion"
    assert "stand out" in sentence or "far more dependencies" in sentence, sentence
    return "coupling"


class TestTheOverview:
    def test_the_first_answer_opens_with_the_size_and_integrity_headlines(self, tmp_path: Path) -> None:
        for each in sample_reports(tmp_path):
            conclusions = conclude(each)
            assert conclusions.state_sentences[:2] == (
                section(conclusions, "size").headline,
                section(conclusions, "integrity").headline,
            )

    def test_the_first_answer_in_full(self, tmp_path: Path) -> None:
        conclusions = conclude(report(full_state(tmp_path)))
        assert conclusions.state_sentences == (
            "The model declares 6 components and 2 compositions, nested 1 deep.",
            "check-model reports 2 errors and 3 warnings, most from trace rules: the model does not pass as it stands.",
            "One component in 6 has far more dependencies, in and out, than the rest: H.",
            "In 1 of the 3 components that give a figure, fewer than half the pairs of runnables that use a port or a "
            "variable "
            "have one in common; half is a starting threshold, not calibrated.",
        )

    def test_the_first_answer_on_a_fresh_import(self, tmp_path: Path) -> None:
        # Too few components to stand out, and no runnable access: the cohesion is the section's alone.
        assert conclude(report(fresh_state(tmp_path))).state_sentences == (
            "The model declares 2 components and no composition.",
            "check-model reports no error and no warning.",
            "With 2 components, which of them stand out by their links is not measured.",
        )

    def test_hubs_and_no_hub_in_plain_words(self) -> None:
        many = conclude(report(state(population(37, E=9, D=10, C=11, B=12, A=13), hubs=("A", "B", "C", "D", "E"))))
        assert (
            many.state_sentences[2]
            == "5 of 42 components have far more dependencies, in and out, than the rest: A, B, C, and 2 more."
        )
        none = conclude(report(state(population(5, Top=3), hubs=())))
        assert none.state_sentences[2] == "No component has far more dependencies, in and out, than the rest."

    @pytest.mark.parametrize(
        ("figures", "expected"),
        [
            (
                cohesion(1, Fraction(1, 3), (("V", Fraction(1, 3)),)),
                "In the one component that gives a figure, fewer than half the pairs of runnables that use a port or a "
                "variable "
                "have one in common; half is a starting threshold, not calibrated.",
            ),
            (
                cohesion(2, Fraction(1, 3), (("V", Fraction(1, 3)), ("W", Fraction(1, 3)))),
                "In all the 2 components that give a figure, fewer than half the pairs of runnables that use a port or "
                "a variable "
                "have one in common; half is a starting threshold, not calibrated.",
            ),
            (
                # One component whose only runnable gives 1 with no pair: "no fewer than half" holds of it.
                cohesion(1, Fraction(1)),
                "In the one component that gives a figure, no fewer than half the pairs of runnables that use a port "
                "or a variable "
                "have one in common; half is a starting threshold, not calibrated.",
            ),
            (
                cohesion(26, Fraction(4, 5)),
                "In each of the 26 components that give a figure, no fewer than half the pairs of runnables that use a "
                "port or a variable have one in common; half is a starting threshold, not calibrated.",
            ),
        ],
    )
    def test_cohesion_in_plain_words(self, figures: Cohesion, expected: str) -> None:
        assert conclude(report(state(components(A=1), cohesion=figures))).state_sentences[-1] == expected

    def test_the_cohesion_sentence_speaks_of_the_runnables_that_use_a_port(self, tmp_path: Path) -> None:
        # r1 reads P and Q, r2 reads P, r3 and r4 declare nothing: the one pair of runnables that use a
        # port shares P, so the figure is 1, though five of the six pairs of all four runnables share none.
        runnables = (accessing("r1", "P", "Q"), accessing("r2", "P"), periodic("r3"), periodic("r4"))
        measured = side_of(model(model_component("C", *runnables)), tmp_path)
        assert isinstance(measured.cohesion, Cohesion)
        assert (measured.cohesion.mean, measured.cohesion.below) == (Fraction(1), ())
        assert conclude(report(measured)).state_sentences[-1] == (
            "In the one component that gives a figure, no fewer than half the pairs of runnables that use a port or a "
            "variable "
            "have one in common; half is a starting threshold, not calibrated."
        )

    def test_each_answer_is_a_few_sentences(self, tmp_path: Path) -> None:
        # The bound: four sentences for the state, three for the heading, each naming at most three
        # components; the detail is the sections'.
        assert (STATE_ANSWER_SENTENCES, HEADING_ANSWER_SENTENCES) == (4, 3)
        for each in sample_reports(tmp_path):
            conclusions = conclude(each)
            assert 1 <= len(conclusions.state_sentences) <= STATE_ANSWER_SENTENCES
            assert 1 <= len(conclusions.heading_sentences) <= HEADING_ANSWER_SENTENCES
            assert all(sentence.endswith(".") for sentence in conclusions.state_sentences)
            assert all(sentence.endswith(".") for sentence in conclusions.heading_sentences)

    def test_an_answer_uses_no_symbol_and_no_technical_term(self, tmp_path: Path) -> None:
        for each in sample_reports(tmp_path):
            conclusions = conclude(each)
            for sentence in (*conclusions.state_sentences, *conclusions.heading_sentences):
                assert not TECHNICAL_WORDS.search(sentence), sentence

    def test_every_figure_of_an_answer_is_stated_in_its_own_section(self, tmp_path: Path) -> None:
        # The answers restate; nothing is said only in the overview. Each plain-words sentence is held to
        # the section it shortens, not to the page: the coupling, the cohesion or the movement.
        for each in sample_reports(tmp_path):
            conclusions = conclude(each)
            owned = [(sentence, own_section(sentence)) for sentence in conclusions.state_sentences[2:]]
            owned += [(sentence, MOVEMENT_KEY) for sentence in conclusions.heading_sentences]
            for sentence, key in owned:
                found = conclusions.section(key)
                if found is None:
                    assert sentence == NO_BASE_NAMED
                    continue
                for number in re.findall(r"\d+", sentence):
                    assert re.search(rf"(?<!\d){number}(?!\d)", found.text), (number, sentence, key)

    def test_the_second_answer_is_the_movement_s_short_form(self, tmp_path: Path) -> None:
        for each in sample_reports(tmp_path):
            conclusions = conclude(each)
            movement = conclusions.section("movement")
            if movement is None:
                assert conclusions.heading_sentences == (NO_BASE_NAMED,)
                continue
            for sentence in conclusions.heading_sentences:
                short = sentence.rstrip(".").split(", though")[0]
                assert sentence in movement.sentences or movement.headline.startswith(short), sentence
                if ", though " in sentence:
                    # The count is the section's Ct changes of the components present at both revisions:
                    # never above the section's count, which takes added and removed ones in too.
                    said = re.search(r"in and out, of (\d+) components? present at both revisions", sentence)
                    counted = re.search(r"though (\d+) component(?:'s|s') Ct changed", movement.headline)
                    assert said is not None and counted is not None, (sentence, movement.headline)
                    assert each.movement is not None
                    kept = [c for c in each.movement.ct_changes if c.before is not None and c.after is not None]
                    assert int(said.group(1)) == len(kept) <= int(counted.group(1))


class TestRules:
    def test_the_sections_come_in_their_order(self, tmp_path: Path) -> None:
        full = full_state(tmp_path)
        with_base = conclude(report(full, base=dataclasses.replace(full, label=BASE)))
        assert tuple(key for key, _ in with_base.sections) == SECTION_KEYS
        without = conclude(report(full))
        assert tuple(key for key, _ in without.sections) == tuple(key for key in SECTION_KEYS if key != "movement")

    @pytest.mark.parametrize("shape", ["full", "fresh"])
    def test_every_section_has_its_three_things(self, tmp_path: Path, shape: str) -> None:
        current = full_state(tmp_path) if shape == "full" else fresh_state(tmp_path)
        page = render_markdown(report(current, base=dataclasses.replace(current, label=BASE)))
        conclusions = conclude(report(current, base=dataclasses.replace(current, label=BASE)))
        titles = {
            "size": "### Size",
            "coupling": "### Coupling",
            "tables": "### Coupling inside each composition",
            "cohesion": "### Cohesion",
            "coverage": "### Coverage",
            "integrity": "### Integrity",
            "movement": "## Movement",
            "look_at": "## What to look at",
        }
        starts = [page.index(f"\n{titles[key]}\n") for key in SECTION_KEYS]
        assert starts == sorted(starts)
        for index, key in enumerate(SECTION_KEYS):
            end = starts[index + 1] if index + 1 < len(starts) else len(page)
            body = page[starts[index] : end]
            # The pointer to the terms it uses, and its conclusion for this model.
            terms = "; ".join(SECTION_TERMS[key])
            assert f"*Terms:* {terms}; see [{DEFINITIONS_TITLE}](#definitions-and-method)." in body, key
            assert section(conclusions, key).text in body, key
            # No section defines a term again: the definitions stand once, at the top.
            for _, definition in DEFINITIONS:
                assert definition not in body, key
            assert "*Measures:*" not in body and "*Read:*" not in body, key

    def test_the_conclusion_follows_the_figures_and_the_limit_follows_it(self, tmp_path: Path) -> None:
        page = render_markdown(report(full_state(tmp_path)))
        coupling = page[page.index("\n### Coupling\n") : page.index("\n### Coupling inside each composition\n")]
        assert coupling.index("| Mean Ct, all components") < coupling.index("One component in 6 is a hub")
        assert coupling.index("One component in 6 is a hub") < coupling.index("*Limit:* Each type counts once")

    def test_the_overview_opens_the_page_and_the_definitions_follow(self, tmp_path: Path) -> None:
        each = report(full_state(tmp_path))
        conclusions = conclude(each)
        markdown = render_markdown(each).splitlines()
        # Title, subtitle, then the overview before anything else, then the definitions, then the sections.
        assert markdown[4] == f"## {OVERVIEW_TITLE}"
        assert markdown[6] == f"**What state is the architecture in?** {conclusions.state_answer}"
        assert markdown[8] == f"**Where is it heading?** {conclusions.heading_answer}"
        assert markdown[10] == f"## {DEFINITIONS_TITLE}"
        bullets = markdown[12 : 12 + len(DEFINITIONS)]
        assert [line.split(":**", 1)[0] for line in bullets] == [f"- **{term}" for term, _ in DEFINITIONS]
        assert markdown[12 + len(DEFINITIONS) + 1] == "## State"
        text = render_text(each).splitlines()
        assert text[3] == OVERVIEW_TITLE.upper()
        assert text[5] == f"What state is the architecture in? {conclusions.state_answer}"
        assert text[7] == f"Where is it heading? {NO_BASE_NAMED}"
        assert text[9] == DEFINITIONS_TITLE.upper()
        assert text[11 : 11 + len(DEFINITIONS)] == [f"- {term}: {definition}" for term, definition in DEFINITIONS]
        assert text[11 + len(DEFINITIONS) + 1] == "STATE"

    def test_every_term_a_section_names_is_defined_and_every_definition_is_used(self) -> None:
        defined = [term for term, _ in DEFINITIONS]
        assert len(defined) == len(set(defined))
        named = {term for terms in SECTION_TERMS.values() for term in terms}
        assert named == set(defined)
        assert set(SECTION_TERMS) == set(SECTION_KEYS)

    def test_the_definitions_name_every_required_term(self) -> None:
        # The quantities the definitions must define, each with how it is counted.
        page = " ".join(f"{term}: {definition}" for term, definition in DEFINITIONS)
        for term in (
            "Ca",
            "Ce",
            "Ct = Ca + Ce",
            "Hub",
            "Isolated",
            "trimmed mean",
            "Cohesion",
            "Coverage",
            "n/a",
            "require port",
            "provide port",
            "Node",
            "External stub",
            "delegation",
            "Components are counted, not connectors",
            "over the declared components only",
            "n < 6",
            "crosses a composition's border",
        ):
            assert term in page, term

    @pytest.mark.parametrize(
        ("term", "statement"),
        [
            # Each statement as the code computes it.
            ("External stub", "A link between two stubs gives no dependency and counts in no Ca or Ce"),
            ("External stub", "it is a link of the composition table both stubs are members of"),
            ("n/a", "the model lacks the data it needs, or the method does not apply"),
            ("Composition table", "A link is a distinct directed pair of members"),
            ("Composition table", "several connectors between two members in one direction are one link"),
            ("Composition table", "a type placed twice is one row, under its first member's name"),
            ("Coverage", "it states none and the highest level of the requirements allocated to it is above QM"),
            ("Coverage", "declared when the component states a level, QM included"),
            ("Coverage", "over the requirement ids no stub requirement holds, a stub requirement being a placeholder"),
            ("Coverage", "an id a stub requirement holds is excluded with every file declaring it"),
            ("External stub", "and whatever else its file declares, such as a runnable, which the size counts"),
            ("Coverage", "a server runnable, one an operation starts and no timing event does"),
            ("Coverage", "A task naming one placement's runnable covers that runnable of the type"),
            ("Cohesion", "C = s / (m(m − 1)/2)"),
            ("Cohesion", "This is the measure as computed here, not a textbook one"),
            ("Cohesion", "two or more use one and all use one and the same port or variable"),
            # The inter-runnable variables count, apart from the ports.
            ("Cohesion", "the inter-runnable variables its reads_variables and writes_variables name"),
            ("Cohesion", "a port and a variable of one name are two things, never one in common"),
            ("Size", "nested N deep is the longest chain of compositions"),
            (
                "Size",
                "a name declared twice counts twice here and once in the figures below, except a composition table",
            ),
            ("Size", "two connectors sharing a name and joining different pairs are two links there"),
            ("Size", "its last declared file, or its last external stub when no declared file holds the name"),
            ("Size", "a stub sharing a declared component's name counted with them"),
            (
                "n/a",
                "a component whose runnables give no pair, save one whose only runnable uses a port or a variable, "
                "which gives 1",
            ),
            ("Model elements", "a placement is a member of a composition that puts a type in it"),
            ("Mean and deviation", "σ = √(Σ(Ct − μ)² / n)"),
            ("Movement", "A finding is the same at both revisions when its rule and message are"),
            ("Movement", "a coverage row shows a count, not a share"),
            ("Movement", "named among the components present at both revisions"),
            ("Movement", "It does not follow the other size rows"),
            (
                "External stub",
                "a stub's link still allocates a declared requirement id no stub requirement holds",
            ),
            (
                "Size",
                "are read by no figure below but the errors and warnings, which check-model reports over every file",
            ),
            ("Model elements", "an interface, a composition, a task or a hardware element"),
            ("Node", "A composition is a container, not a node"),
            ("Dependency", "The Dependencies figure counts"),
        ],
    )
    def test_a_corrected_definition_is_on_the_page(self, term: str, statement: str) -> None:
        # What the code computes behind each statement is held by the behavioural tests of
        # test_metrics.py and of TestMeasuredMovement.
        assert statement in dict(DEFINITIONS)[term]

    def test_no_definition_keeps_a_corrected_statement(self) -> None:
        page = " ".join(definition for _, definition in DEFINITIONS)
        for wrong in (
            "a link between two stubs counts nowhere",
            "satisfied only by a stub",
            "the model holds none of the data a figure needs",
            "Each connector drawn inside the composition is one link",
            "a container and no node",
            "Dependencies counts",
            "named by a component, one of its ports or one of its runnables",
            "hub, isolated, cohesion and coverage figure",
            "the same figures at the base",
            "a stub's link to a requirement still allocates it",
            "a stub's link to a declared requirement that is not a stub requirement still allocates it",
            "requirement links, are read by no figure below.",
            "over the requirement ids that are not stub requirements",
            "a component declared only by reference, with the ports an export showed. ",
        ):
            assert wrong not in page, wrong

    def test_the_json_carries_the_answers_and_the_conclusions_and_no_explanation(self, tmp_path: Path) -> None:
        full = full_state(tmp_path)
        each = report(full, base=dataclasses.replace(full, label=BASE))
        conclusions = conclude(each)
        document = json.loads(render_json(each))
        assert document["answers"] == {"state": conclusions.state_answer, "heading": conclusions.heading_answer}
        assert list(document["conclusions"]) == list(SECTION_KEYS)
        assert document["conclusions"]["coupling"] == {
            "conclusion": section(conclusions, "coupling").text,
            "limit": "Each type counts once, however often it is placed.",
        }
        assert document["conclusions"]["size"]["limit"] is None
        assert [entry["conclusion"] for entry in document["state"]["composition_tables"]] == [
            sentence for _, sentence in conclusions.tables
        ]
        flat = json.dumps(document, ensure_ascii=False)
        for term, definition in DEFINITIONS:
            assert definition not in flat, term
        assert DEFINITIONS_TITLE not in flat

    def test_no_verdict_word_in_any_sentence_or_fixed_line(self, tmp_path: Path) -> None:
        texts = [line for pair in DEFINITIONS for line in pair] + list(LOOK_AT_LABELS.values())
        for each in sample_reports(tmp_path):
            texts += every_sentence(conclude(each))
        offending = [text for text in texts if VERDICT_WORDS.search(text)]
        assert offending == []

    def test_the_cohesion_threshold_always_stands_beside_not_calibrated(self, tmp_path: Path) -> None:
        texts = [line for pair in DEFINITIONS for line in pair] + list(LOOK_AT_LABELS.values())
        for each in sample_reports(tmp_path):
            conclusions = conclude(each)
            texts += every_sentence(conclusions)
            if each.movement is not None:
                texts += [figure.figure for figure in each.movement.figures]
        for text in texts:
            for found in re.finditer(r"0\.5\b", text):
                assert "not calibrated" in text[found.start() : found.start() + 60], text

    def test_no_float_in_the_module(self) -> None:
        source = Path(metrics_conclusions.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), ast.dump(node)
            assert not (isinstance(node, ast.Name) and node.id in {"float", "round"}), ast.dump(node)


#: Builds one model, concludes and renders it three ways; ``reverse`` gives the elements in reverse order.
_SUBPROCESS = """
import dataclasses, locale, sys, tempfile
from pathlib import Path
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass
sys.stderr.write(locale.setlocale(locale.LC_ALL))
from test_metrics import composition, connector, model, side_of, stub, swc
from swcraft_tools.metrics import MetricsReport, compare
from swcraft_tools.metrics_render import render_json, render_markdown, render_text

elements = [
    swc("H", "ra", "rb", "rc", "rd", "re"),
    swc("A", "pa", "rs"),
    *(swc(name, "pa") for name in "BCDE"),
    *(swc(f"Z{index}") for index in range(4)),
    stub("S", "ps"),
    composition("Root", root=True, members=(("h", "H"), ("a", "A"), ("b", "B"), ("s", "S"))),
    composition("Empty"),
    connector("ca", "A.pa", "H.ra"),
    connector("cb", "B.pa", "H.rb"),
    connector("cc", "C.pa", "H.rc"),
    connector("cd", "D.pa", "H.rd"),
    connector("ce", "E.pa", "H.re"),
    connector("cs", "S.ps", "A.rs"),
]
base_elements = elements[:6]
if sys.argv[1] == "reverse":
    elements.reverse()
    base_elements.reverse()
current = side_of(model(*elements), Path(tempfile.mkdtemp()))
base = side_of(model(*base_elements), Path(tempfile.mkdtemp()))
base = dataclasses.replace(base, label="v1 (abc1234)")
series = (base, current)
report = MetricsReport("demo", series, compare(series), None, ())
sys.stdout.buffer.write((render_text(report) + render_markdown(report) + render_json(report)).encode("utf-8"))
"""


class TestTheSameBytes:
    def test_two_processes_with_other_seeds_locales_and_order_agree(self) -> None:
        tools = REPO_ROOT / "packages" / "tools"
        outputs, locales = [], []
        for seed, lang, order in (("0", "C", "forward"), ("4242", "de_DE.UTF-8", "reverse")):
            environment = {
                **os.environ,
                "PYTHONHASHSEED": seed,
                "LC_ALL": lang,
                "LANG": lang,
                "PYTHONPATH": os.pathsep.join(
                    (str(tools / "tests"), str(tools / "tests" / "support"), str(tools / "src"))
                ),
            }
            completed = subprocess.run(
                [sys.executable, "-c", _SUBPROCESS, order],
                cwd=tools,
                env=environment,
                capture_output=True,
                check=True,
            )
            outputs.append(completed.stdout)
            locales.append(completed.stderr.decode("utf-8", errors="replace"))
        assert outputs[0] == outputs[1]
        assert b"What state is the architecture in?" in outputs[0]
        if not locales[1].startswith("de_DE"):
            # The seeds and the order were still compared; only the locale half did not run.
            pytest.skip(f"de_DE.UTF-8 is not installed here: the second process ran in {locales[1]!r}")
