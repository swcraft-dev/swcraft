"""The architecture metrics' text, Markdown and JSON, each expected figure worked by hand.

No figure here was copied from the renderer's output: the comment beside it is the arithmetic.
"""

from __future__ import annotations

import dataclasses
import json
import re
from fractions import Fraction
from pathlib import Path

import pytest
from swcraft_tools.metrics import (
    LOOK_AT_KEYS,
    LOWER_THRESHOLD_BELOW_ZERO,
    ArchitectureState,
    AsilCoverage,
    Cohesion,
    Coverage,
    Integrity,
    LookAtEntry,
    LookAtLine,
    MetricsReport,
    NotAvailable,
    RequirementCoverage,
    RunnableCoverage,
    TaskCoverage,
    compare,
    measure_coupling,
)
from swcraft_tools.metrics_conclusions import LOOK_AT_LABELS, format_change, format_hundredths, round_hundredths
from swcraft_tools.metrics_render import DEFINITIONS, render_json, render_markdown, render_text
from test_metrics import (
    basic_project,
    composition,
    connector,
    metrics_of,
    model,
    no_location,
    side_of,
    stub,
    swc,
)


def test_the_cohesion_definition_names_every_starting_event_whose_port_is_touched() -> None:
    definition = dict(DEFINITIONS)["Cohesion"]
    assert (
        "the port of an operation, of a data reception or of an asynchronous call's return that starts it" in definition
    )


# ---------------------------------------------------------------------------
# Exact rounding
# ---------------------------------------------------------------------------


class TestRoundHundredths:
    def test_the_twenty_one_component_threshold_is_rounded_on_the_whole_expression(self) -> None:
        # Five connected pairs (Ct 1 each, ten components) and eleven isolates (Ct 0): mu = 10/21,
        # variance = (10·(11/21)² + 11·(10/21)²) / 21 = (1210 + 1100) / 441 / 21 = 110/441.
        # mu + 2σ = 0.476190… + 2·0.499433… = 1.475057…, which is 1.48.
        mean, variance = Fraction(10, 21), Fraction(110, 441)
        assert round_hundredths(mean, 2, variance) == 148
        # The trap the helper exists for: the root floored to four places, 0.4994, gives
        # 0.476190… + 0.9988 = 1.474990…, which would print 1.47.
        assert round_hundredths(mean + 2 * Fraction(4994, 10000)) == 147

    def test_the_twenty_one_component_population_renders_its_threshold(self, tmp_path: Path) -> None:
        pairs = [
            item
            for index in range(5)
            for item in (
                swc(f"P{index}", "pa"),
                swc(f"Q{index}", "ra"),
                connector(f"c{index}", f"P{index}.pa", f"Q{index}.ra"),
            )
        ]
        state = model(*pairs, *(swc(f"Z{index:02d}") for index in range(11)))
        side = side_of(state, tmp_path)
        text = render_markdown(report_of(side))
        # Ct = 1 lies below 1.475…, so no hub: 0 of 21. mu − 2σ < 0, so isolated is n/a.
        assert "| Hubs (Ct above 1.48) | 0 of 21 (0.00 %) |" in text
        # The row says what it would hold and points to the components with no link, which the eleven are.
        assert (
            "| Isolated (more than two deviations below the mean) | n/a — that threshold is below zero; "
            "components with no link at all are named below |"
        ) in text
        # mu = 10/21 = 0.4761…; σ = √110 / 21 = 0.4994…
        assert "| Mean Ct, all components | 0.48 (deviation 0.50) |" in text

    @pytest.mark.parametrize(
        ("a", "b", "v", "hundredths"),
        [
            (Fraction(1, 8), 0, 0, 13),  # 0.125 is half: up, 0.13
            (Fraction(-1, 8), 0, 0, -12),  # -0.125 is half: up, towards +∞, -0.12
            (Fraction(0), 1, Fraction(1, 4), 50),  # √(1/4) = 0.5 exactly
            (Fraction(1), -2, Fraction(1, 16), 50),  # 1 − 2·0.25 = 0.5
            (Fraction(0), -1, Fraction(1, 40000), 0),  # −0.005 is half: up, 0.00
            (Fraction(0), 1, Fraction(2), 141),  # √2 = 1.41421…
            (Fraction(1, 3), 0, 0, 33),
            (Fraction(2, 3), 0, 0, 67),
        ],
    )
    def test_half_up_by_exact_comparison(self, a: Fraction, b: int, v: Fraction, hundredths: int) -> None:
        assert round_hundredths(a, b, v) == hundredths

    @pytest.mark.parametrize(("k", "text"), [(148, "1.48"), (5, "0.05"), (0, "0.00"), (-34, "-0.34"), (1200, "12.00")])
    def test_hundredths_print_with_two_places(self, k: int, text: str) -> None:
        assert format_hundredths(k) == text

    @pytest.mark.parametrize(
        ("change", "text"),
        [
            (2, "+2"),
            (-2, "−2"),
            (0, "0"),
            (Fraction(34, 100), "+0.34"),
            (Fraction(-17, 50), "−0.34"),
            (Fraction(0), "0.00"),
        ],
    )
    def test_a_change_carries_its_sign(self, change: int | Fraction, text: str) -> None:
        assert format_change(change) == text


# ---------------------------------------------------------------------------
# A full model
# ---------------------------------------------------------------------------


def report_of(
    current: ArchitectureState, *, base: ArchitectureState | None = None, no_base: str | None = None
) -> MetricsReport:
    series = (current,) if base is None else (base, current)
    movement = compare(series) if base is not None else None
    return MetricsReport("demo", series, movement, no_base, ())


def full_state(tmp_path: Path) -> ArchitectureState:
    """Six declared components, H the hub, a stub, a root table, an empty composition and every block a figure.

    H requires from A, B, C, D and E; A requires from the stub S. So Ct is H 5, A 2 (Ca 1 from H,
    Ce 1 to S), B to E 1 each: total 11, mu = 11/6.
    """
    providers = [swc(name, "pa") for name in "BCDE"]
    state = model(
        swc("H", "ra", "rb", "rc", "rd", "re"),
        swc("A", "pa", "rs"),
        *providers,
        stub("S", "ps"),
        composition(
            "Root",
            root=True,
            members=(("h", "H"), ("a", "A"), ("b", "B"), ("c", "C"), ("d", "D"), ("e", "E"), ("s", "S")),
        ),
        composition("Empty"),
        connector("ca", "A.pa", "H.ra"),
        connector("cb", "B.pa", "H.rb"),
        connector("cc", "C.pa", "H.rc"),
        connector("cd", "D.pa", "H.rd"),
        connector("ce", "E.pa", "H.re"),
        connector("cs", "S.ps", "A.rs"),
    )
    side = side_of(state, tmp_path)
    return dataclasses.replace(
        side,
        cohesion=Cohesion(
            components=3,
            mean=Fraction(2, 3),
            below=(("B", Fraction(1, 3)),),
            not_available=(("no runnable", 3),),
            runnables=7,
            runnables_used=4,
        ),
        coverage=Coverage(
            asil=AsilCoverage(6, 4, 1, ("E",)),
            runnable=RunnableCoverage(6, 5, ("F",)),
            task=TaskCoverage(runnables=8, on_task=6, server=2, not_on_task=("A.r1", "B.r2")),
            requirement=RequirementCoverage(
                requirements=10,
                allocated=7,
                stubs=1,
                unallocated=("R1", "R2", "R3"),
                components=6,
                linked=4,
                unlinked=("D", "E"),
            ),
        ),
        integrity=Integrity(
            errors=3,
            warnings=12,
            errors_by_family=(("connector", 2), ("safety", 1)),
            warnings_by_family=(("trace", 12),),
            by_rule=(("connector.a", 2), ("safety.b", 1), ("trace.c", 12)),
            ports=296,
            unconnected=("H.rx",),
        ),
    )


def section(markdown: str, title: str) -> str:
    """The text under a heading, up to the next heading of the same or a higher level."""
    match = re.search(rf"^(#+) {re.escape(title)}$", markdown, re.M)
    assert match is not None, title
    body = markdown[match.end() :]
    following = re.search(rf"^#{{1,{len(match.group(1))}}} ", body, re.M)
    return body if following is None else body[: following.start()]


class TestFullModel:
    def test_size(self, tmp_path: Path) -> None:
        text = render_markdown(report_of(full_state(tmp_path)))
        size = section(text, "Size")
        # Six declared and one stub; two compositions, Empty nested in nothing, Root holding components: 1 deep.
        assert "| Components | 6 declared, 1 external stub |" in size
        assert "| Compositions | 2, nested 1 deep |" in size
        # Ports: H 5, A 2, B to E 1 each = 11 on components, S 1 on the stub, none on a boundary; six connectors.
        assert "| Ports | 12: 11 on components, 1 on an external stub |" in size
        assert "| Connectors | 6 |" in size

    def test_coupling_figures(self, tmp_path: Path) -> None:
        coupling = section(render_markdown(report_of(full_state(tmp_path))), "Coupling")
        # variance = ((5 − 11/6)² + (2 − 11/6)² + 4·(1 − 11/6)²) / 6 = (361 + 1 + 100) / 36 / 6 = 77/36;
        # σ = √77 / 6 = 1.46249…; mu = 1.8333…; mu + 2σ = 4.75832…, so H (5) is a hub, one of six, 16.67 %.
        # Dependencies: H on each of A to E, A on S: six directed pairs (the sum of Ct is 11).
        assert "| Dependencies | 6 |" in coupling
        assert "| Mean Ct, all components | 1.83 (deviation 1.46) |" in coupling
        assert "| Hubs (Ct above 4.76) | 1 of 6 (16.67 %) |" in coupling
        # mu − 2σ = −1.09…: below zero, and every component has a link.
        assert (
            "| Isolated (more than two deviations below the mean) | n/a — that threshold is below zero; "
            "no component is without a link |"
        ) in coupling
        # Without H: A 2, B to E 1: 6/5 = 1.20, highest 2.
        assert "| Mean Ct without outliers | 1.20; highest 2 |" in coupling
        assert "| External stubs most depended on | S (1 dependent) |" in coupling
        # The top list, Ct first then name: H 5, A 2, B, C, D, E 1.
        rows = [line for line in coupling.splitlines() if line.startswith("| ") and line.count("|") == 6]
        assert rows[2:] == [
            "| H | application | 0 | 5 | 5 |",
            "| A | application | 1 | 1 | 2 |",
            "| B | application | 1 | 0 | 1 |",
            "| C | application | 1 | 0 | 1 |",
            "| D | application | 1 | 0 | 1 |",
            "| E | application | 1 | 0 | 1 |",
        ]
        assert "Links not measured" not in coupling

    def test_links_not_measured_is_a_limit_above_zero(self, tmp_path: Path) -> None:
        side = full_state(tmp_path)
        assert not isinstance(side.coupling, NotAvailable)
        reasons = (("unreached end", 2), ("one direction", 1))
        unmeasured = dataclasses.replace(side.coupling, not_measured_by_reason=reasons)
        side = dataclasses.replace(side, coupling=unmeasured)
        coupling = section(render_markdown(report_of(side)), "Coupling")
        # A limit beside the figures, never a figure row of its own, each reason with its count.
        assert (
            "*Limit:* Giving no measured dependency: 2 connectors with an end that reaches no component, 1 pair "
            "of ends of one direction. Each type counts once, however often it is placed."
        ) in coupling
        assert "| Links not measured" not in coupling

    def test_cohesion_coverage_and_integrity(self, tmp_path: Path) -> None:
        text = render_markdown(report_of(full_state(tmp_path)))
        cohesion = section(text, "Cohesion")
        assert "| Mean cohesion | 0.67 over 3 components |" in cohesion
        assert "| Below 0.5, a starting threshold not calibrated | 1: B (0.33) |" in cohesion
        assert "| No figure | 3 components: 3 with no runnable |" in cohesion
        # The runnables the figure rests on are its limit, beside it.
        assert (
            "*Limit:* Rests on the 4 runnables that use a port or a variable in the 3 components with a figure, of 7 "
            "runnables in "
            "the declared components; 3 components have no figure." in cohesion
        )
        assert "Runnables measured" not in cohesion
        coverage = section(text, "Coverage")
        assert "| ASIL | 4 declared, 1 inherited, 1 neither |" in coverage
        assert "| Components with a runnable | 5 of 6 |" in coverage
        assert "| Runnables on a task | 6 of 8; 2 server runnables excluded (run in their callers' tasks) |" in coverage
        assert (
            "| Requirements allocated | 7 of 10; 1 id held by a stub requirement, a placeholder, excluded |" in coverage
        )
        assert "| Components with a requirement link | 4 of 6 |" in coverage
        integrity = section(text, "Integrity")
        assert "| Errors | 3: connector 2, safety 1 |" in integrity
        assert "| Warnings | 12: trace 12 |" in integrity
        assert "| Unconnected component ports | 1 of 296 |" in integrity
        assert "| Unconnected | H.rx |" in integrity

    def test_the_sections_come_in_their_order_in_text_and_markdown(self, tmp_path: Path) -> None:
        side = full_state(tmp_path)
        report = report_of(side, base=dataclasses.replace(side, label="HEAD (abc1234)"))
        markdown = render_markdown(report)
        headings = re.findall(r"^#+ (.+)$", markdown, re.M)
        assert headings == [
            "Architecture metrics — demo",
            "Overview",
            "Definitions and method",
            "State",
            "Size",
            "Coupling",
            "Coupling inside each composition",
            "Cohesion",
            "Coverage",
            "Integrity",
            "Movement",
            "What to look at",
        ]
        text = render_text(report)
        positions = [
            text.index(f"\n{title}\n")
            for title in (
                "OVERVIEW",
                "DEFINITIONS AND METHOD",
                "STATE",
                "Size",
                "Coupling",
                "Coupling inside each composition",
                "Cohesion",
                "Coverage",
                "Integrity",
                "MOVEMENT",
                "WHAT TO LOOK AT",
            )
        ]
        assert positions == sorted(positions)

    def test_the_text_says_what_the_markdown_says(self, tmp_path: Path) -> None:
        report = report_of(full_state(tmp_path))
        text = render_text(report)
        assert text.startswith(
            "Architecture metrics — demo\nWorking tree. No overall score; swcraft validate is the gate.\n"
        )
        assert re.search(r"^  Hubs \(Ct above 4\.76\) +1 of 6 \(16\.67 %\)$", text, re.M)
        assert re.search(r"^  H +application +0 +5 +5$", text, re.M)
        assert re.search(
            r"^  Runnables on a task +6 of 8; 2 server runnables excluded \(run in their callers' tasks\)$",
            text,
            re.M,
        )

    def test_one_of_each_takes_the_singular(self, tmp_path: Path) -> None:
        side = full_state(tmp_path)
        coverage = dataclasses.replace(
            side.coverage,
            task=dataclasses.replace(side.coverage.task, server=1),
            requirement=dataclasses.replace(side.coverage.requirement, stubs=2),
        )
        cohesion = dataclasses.replace(
            side.cohesion, components=1, not_available=(("only one runnable declares an access", 1),)
        )
        text = render_markdown(report_of(dataclasses.replace(side, coverage=coverage, cohesion=cohesion)))
        assert "| Runnables on a task | 6 of 8; 1 server runnable excluded (runs in its callers' tasks) |" in text
        assert "| Requirements allocated | 7 of 10; 2 ids held by stub requirements, placeholders, excluded |" in text
        assert "| Mean cohesion | 0.67 over 1 component |" in text
        assert "| No figure | 1 component: 1 where only one runnable declares an access |" in text

    def test_the_one_port_reason_speaks_of_the_runnables_that_use_a_port(self, tmp_path: Path) -> None:
        # An initialization runnable that uses no port is left out of the one-port case, so the words
        # never say that every runnable of the component shares the port.
        side = full_state(tmp_path)
        cohesion = dataclasses.replace(
            side.cohesion, not_available=(("one port or variable: every runnable that uses one uses only it", 2),)
        )
        text = render_markdown(report_of(dataclasses.replace(side, cohesion=cohesion)))
        assert (
            "| No figure | 2 components: 2 whose runnables that use a port or a variable all use one and the same port "
            "or variable |" in text
        )


# ---------------------------------------------------------------------------
# The composition tables
# ---------------------------------------------------------------------------


class TestCompositionTables:
    def test_three_formats_mark_the_stub_and_say_n_a(self, tmp_path: Path) -> None:
        report = report_of(full_state(tmp_path))
        markdown = section(render_markdown(report), "Coupling inside each composition")
        # The root first. A row is a member, by its member name: h (H) 0/5, a (A) 1/1, b to e 1/0,
        # s (the stub S) 1/0, marked; Ct first, then name.
        assert markdown.index("*Root.*") < markdown.index("*Empty.*")
        # A member whose name is not its type's shows both, so a stub's row matches its stub line.
        assert "| h (H) | component | 0 | 5 | 5 |" in markdown
        assert "| s (S) | stub | 1 | 0 | 1 |" in markdown
        assert "*Empty.* n/a — the composition has no member" in markdown
        text = render_text(report)
        assert re.search(r"^  s \(S\) +stub +1 +0 +1$", text, re.M)
        assert "\nEmpty. n/a — the composition has no member\n" in text
        tables = json.loads(render_json(report))["state"]["composition_tables"]
        assert [table["composition"] for table in tables] == ["Root", "Empty"]
        assert {"name": "s", "type": "S", "kind": "stub", "ca": 1, "ce": 0, "ct": 1} in tables[0]["rows"]
        assert tables[1]["rows"] == {"not_available": "the composition has no member"}
        # Five links into H and one into A, among Root's members.
        assert tables[0]["links"] == 6

    def test_links_not_placed_is_a_limit_only_above_zero(self, tmp_path: Path) -> None:
        side = full_state(tmp_path)
        assert "placed in no table" not in render_text(report_of(side))
        reasons = (("unresolved composition", 2), ("one direction", 1))
        placed = dataclasses.replace(side, links_not_placed_by_reason=reasons)
        limit = (
            "Placed in no table: 2 connectors whose composition does not resolve, 1 connector joining two ends "
            "of one direction."
        )
        assert f"*Limit:* {limit}" in render_markdown(report_of(placed))
        assert f"\nLimit: {limit}\n" in render_text(report_of(placed))
        assert "| Connectors placed in no table" not in render_markdown(report_of(placed))
        state = json.loads(render_json(report_of(placed)))["state"]
        assert state["links_not_placed"] == 3
        assert state["links_not_placed_by_reason"] == [
            {"reason": "unresolved composition", "count": 2},
            {"reason": "one direction", "count": 1},
        ]

    def test_no_statistics_word_in_a_table(self, tmp_path: Path) -> None:
        tables = section(render_markdown(report_of(full_state(tmp_path))), "Coupling inside each composition")
        # The pointer names its terms; the tables themselves carry no statistics word.
        tables = "\n".join(line for line in tables.splitlines() if not line.startswith("*Terms:*"))
        for word in ("hub", "isolated", "mean", "deviation", "outlier", "threshold", "incomplete"):
            assert word not in tables.lower()

    def test_a_model_with_no_composition_says_so(self, tmp_path: Path) -> None:
        side = side_of(model(swc("A")), tmp_path)
        tables = section(render_markdown(report_of(side)), "Coupling inside each composition")
        assert "n/a — the model declares no composition" in tables


# ---------------------------------------------------------------------------
# n/a, a fresh import, names
# ---------------------------------------------------------------------------


class TestNotAvailable:
    def test_a_fresh_import_reads_n_a_and_never_zero_for_missing_data(self, tmp_path: Path) -> None:
        # Components and a connector, and no runnable, task, requirement or safety level.
        state = model(swc("A", "pa"), swc("B", "ra"), connector("c", "A.pa", "B.ra"))
        text = render_markdown(report_of(side_of(state, tmp_path)))
        coverage = section(text, "Coverage")
        cells = [
            line.split(" | ")[1].rstrip(" |")
            for line in coverage.splitlines()
            if line.startswith("| ") and "---" not in line
        ][1:]
        assert len(cells) == 5
        assert all(cell.startswith("n/a — ") for cell in cells), cells
        assert "\nn/a — no component's runnables declare an access\n" in section(text, "Cohesion")
        document = json.loads(render_json(report_of(side_of(state, tmp_path))))
        assert all(list(value) == ["not_available"] for value in document["state"]["coverage"].values())

    def test_no_block_is_dropped_when_every_figure_is_n_a(self, tmp_path: Path) -> None:
        report = report_of(side_of(model(), tmp_path))
        markdown = render_markdown(report)
        for title in ("Size", "Coupling", "Coupling inside each composition", "Cohesion", "Coverage", "Integrity"):
            assert f"\n### {title}\n" in markdown
        assert "\n\nNo list below has content.\n" in section(markdown, "What to look at")
        assert "No line has content" not in markdown
        assert "\nn/a — the model declares no component\n" in section(markdown, "Coupling")
        assert json.loads(render_json(report))["state"]["coupling"] == {
            "not_available": "the model declares no component"
        }
        text = render_text(report)
        assert "\n\nn/a — the model declares no component\n" in text

    def test_a_name_with_a_pipe_ends_no_cell(self, tmp_path: Path) -> None:
        side = full_state(tmp_path)
        rows = tuple(dataclasses.replace(row, name="H|x") if row.name == "H" else row for row in side.components)
        tables = tuple(dataclasses.replace(table, composition="Ro|ot") for table in side.composition_tables)
        markdown = render_markdown(report_of(dataclasses.replace(side, components=rows, composition_tables=tables)))
        assert "| H\\|x | application | 0 | 5 | 5 |" in markdown
        assert "*Ro\\|ot.*" in markdown
        for line in markdown.splitlines():
            if line.startswith("| "):
                assert "|x" not in line.replace("\\|", "")

    def test_no_location_in_any_output(self, tmp_path: Path) -> None:
        # No git repository, so HEAD cannot be read: the one sentence the model's location could reach.
        project = basic_project(tmp_path)
        report = metrics_of(project, since_head=True)
        for output in (render_text(report), render_markdown(report), render_json(report)):
            assert no_location(output, project)


# ---------------------------------------------------------------------------
# Movement
# ---------------------------------------------------------------------------


class TestMovement:
    def test_no_base_named_prints_no_movement(self, tmp_path: Path) -> None:
        report = report_of(full_state(tmp_path))
        # The definitions are fixed text and keep their Movement line; the section itself is absent.
        assert "\n## Movement\n" not in render_markdown(report)
        assert "\nMOVEMENT\n" not in render_text(report)
        assert json.loads(render_json(report))["movement"] is None

    def test_an_unreadable_head_is_one_line(self, tmp_path: Path) -> None:
        report = report_of(full_state(tmp_path), no_base="not a git repository: no HEAD")
        # The pointer, then the one sentence: no figure.
        assert section(render_markdown(report), "Movement").strip().splitlines()[-1] == (
            "Nothing to compare: not a git repository: no HEAD."
        )
        assert "|" not in section(render_markdown(report), "Movement")
        assert (
            "Terms: Movement (see Definitions and method above).\n\n"
            "Nothing to compare: not a git repository: no HEAD.\n" in render_text(report)
        )
        assert json.loads(render_json(report))["no_base"] == "not a git repository: no HEAD"
        # The subtitle says there is nothing to compare, not only which side it reads.
        assert render_markdown(report).splitlines()[2].startswith("Working tree; nothing to compare it with. ")
        assert render_text(report).splitlines()[1].startswith("Working tree; nothing to compare it with. ")

    def test_nothing_differs_is_one_line(self, tmp_path: Path) -> None:
        side = full_state(tmp_path)
        report = report_of(side, base=dataclasses.replace(side, label="HEAD (abc1234)"))
        movement = section(render_markdown(report), "Movement")
        assert movement.strip().splitlines()[-1] == "No figure this report follows changed since HEAD (abc1234)."
        assert "|" not in movement
        assert "\n\nNo figure this report follows changed since HEAD (abc1234).\n" in render_text(report)
        assert (
            render_markdown(report)
            .splitlines()[2]
            .startswith("Working tree, compared with HEAD (abc1234). No overall score")
        )

    def test_a_change_is_a_table_with_its_sign_and_its_n_a_side(self, tmp_path: Path) -> None:
        current = full_state(tmp_path)
        # The base lacks A's link to the stub: A Ct 1, total 10, mu 10/6 = 1.67; its cohesion is n/a.
        base_state = model(
            swc("H", "ra", "rb", "rc", "rd", "re"),
            swc("A", "pa"),
            *(swc(name, "pa") for name in "BCDE"),
            connector("ca", "A.pa", "H.ra"),
            connector("cb", "B.pa", "H.rb"),
            connector("cc", "C.pa", "H.rc"),
            connector("cd", "D.pa", "H.rd"),
            connector("ce", "E.pa", "H.re"),
        )
        rows, coupling = measure_coupling(base_state)
        base = dataclasses.replace(
            current,
            label="v1 (abc1234)",
            components=rows,
            coupling=coupling,
            cohesion=NotAvailable("no component's runnables declare an access"),
        )
        movement = section(render_markdown(report_of(current, base=base)), "Movement")
        assert "|  | v1 (abc1234) | working tree | change |" in movement
        # Dependencies 5 → 6, the link to the stub; mean Ct 10/6 = 1.67 → 11/6 = 1.83, +0.16 as shown.
        assert "| Dependencies | 5 | 6 | +1 |" in movement
        assert "| Mean Ct | 1.67 | 1.83 | +0.16 |" in movement
        assert "| Mean cohesion | n/a | 0.67 | n/a at v1 (abc1234) |" in movement
        assert "| Findings | 0 introduced, 0 resolved; 0 of the base's 0 still open |" in movement

    def test_a_rounded_figure_s_change_is_the_difference_of_the_figures_shown(self, tmp_path: Path) -> None:
        # 5/6 = 0.8333… shows 0.83, 6/7 = 0.8571… shows 0.86; the exact change 0.0238… would round to
        # 0.02, but the cell is 0.86 − 0.83 = +0.03.
        current = full_state(tmp_path)
        assert isinstance(current.cohesion, Cohesion)
        base = dataclasses.replace(
            current, label="v1 (abc1234)", cohesion=dataclasses.replace(current.cohesion, mean=Fraction(5, 6))
        )
        current = dataclasses.replace(current, cohesion=dataclasses.replace(current.cohesion, mean=Fraction(6, 7)))
        report = report_of(current, base=base)
        assert "| Mean cohesion | 0.83 | 0.86 | +0.03 |" in section(render_markdown(report), "Movement")
        figures = json.loads(render_json(report))["movement"]["figures"]
        assert next(entry for entry in figures if entry["figure"] == "Mean cohesion")["change"] == "0.03"

    def test_a_hub_lost_only_because_the_threshold_moved_says_so(self, tmp_path: Path) -> None:
        # Base: H needs A to E: H 5, the rest 1; mu = 10/6, variance = (100/9 + 5·4/9) / 6 = 20/9,
        # mu + 2σ = 1.6667 + 2.9814 = 4.648…, so H is a hub above 4.65. Working tree: G needs B to E as
        # well: H 5, G 4, B to E 2, A 1; mu = 18/7, variance = 82/49, mu + 2σ = 2.5714 + 2.5872 = 5.158…,
        # so H, unchanged at 5, is no hub above 5.16.
        def wired(*extra: object) -> ArchitectureState:
            return side_of(
                model(
                    swc("H", "ra", "rb", "rc", "rd", "re"),
                    *(swc(name, "pa", "pg") for name in "ABCDE"),
                    *(connector(f"c{name}", f"{name}.pa", f"H.r{name.lower()}") for name in "ABCDE"),
                    *extra,
                ),
                tmp_path,
            )

        base = dataclasses.replace(wired(), label="v1 (abc1234)")
        current = wired(
            swc("G", "rb", "rc", "rd", "re"),
            *(connector(f"g{name}", f"{name}.pg", f"G.r{name.lower()}") for name in "BCDE"),
        )
        movement = section(render_markdown(report_of(current, base=base)), "Movement")
        assert "| Hubs lost | H (its Ct is unchanged at 5; the threshold rose from 4.65 to 5.16) |" in movement

    def test_a_fall_reads_with_a_minus(self, tmp_path: Path) -> None:
        side = full_state(tmp_path)
        integrity = dataclasses.replace(side.integrity, errors=5)
        movement = section(
            render_markdown(report_of(side, base=dataclasses.replace(side, label="v1 (abc1234)", integrity=integrity))),
            "Movement",
        )
        assert "| Errors | 5 | 3 | −2 |" in movement


# ---------------------------------------------------------------------------
# What to look at
# ---------------------------------------------------------------------------


class TestLookAt:
    def test_every_line_has_a_label(self) -> None:
        assert tuple(LOOK_AT_LABELS) == LOOK_AT_KEYS

    def test_each_line_names_its_facts_and_counts_the_rest(self, tmp_path: Path) -> None:
        lines = (
            LookAtLine("errors", (LookAtEntry("connector", 2), LookAtEntry("safety", 1)), 0),
            LookAtLine("hubs", (LookAtEntry("H", 16),), 0),
            LookAtLine(
                "no link",
                (
                    LookAtEntry("Logger", word="incomplete", unconnected=2),
                    LookAtEntry("Probe", word="incomplete", unconnected=1),
                    LookAtEntry("Sink", word="no port"),
                ),
                0,
            ),
            LookAtLine("low cohesion", (LookAtEntry("V", Fraction(1, 3)),), 2),
            LookAtLine("unscheduled", (LookAtEntry("A.run"),), 0),
            LookAtLine("rose most", (LookAtEntry("H", 16, before=11),), 0),
            LookAtLine("introduced", (LookAtEntry("trace.x", message="a finding"),), 0),
        )
        report = dataclasses.replace(report_of(full_state(tmp_path)), look_at=lines)
        look = section(render_markdown(report), "What to look at")
        assert [line for line in look.strip().splitlines() if line.startswith("- ")] == [
            "- **Errors:** connector 2, safety 1.",
            "- **Hubs:** H (Ct 16).",
            "- **No link at all:** Logger (incomplete, 2 unconnected ports); Probe (incomplete, 1 unconnected port); "
            "Sink (no port).",
            "- **Cohesion below 0.5, not calibrated:** V (0.33), and 2 more.",
            "- **Runnables on no task:** A.run.",
            "- **Rose most:** H Ct 11 → 16.",
            "- **Findings introduced:** trace.x: a finding.",
        ]
        document = json.loads(render_json(report))
        assert document["look_at"][3] == {
            "line": "low cohesion",
            "entries": [
                {"name": "V", "figure": "0.33", "before": None, "word": None, "unconnected": 0, "message": None}
            ],
            "more": 2,
        }

    def test_a_finding_message_ending_in_a_period_ends_the_line_once(self, tmp_path: Path) -> None:
        line = LookAtLine(
            "introduced",
            (LookAtEntry("trace.x", message="A has no link."), LookAtEntry("trace.y", message="B has none.")),
            0,
        )
        report = dataclasses.replace(report_of(full_state(tmp_path)), look_at=(line,))
        look = section(render_markdown(report), "What to look at")
        assert "- **Findings introduced:** trace.x: A has no link; trace.y: B has none." in look.splitlines()
        assert ".." not in render_text(report)
        # The JSON keeps the message as check-model wrote it.
        assert json.loads(render_json(report))["look_at"][0]["entries"][0]["message"] == "A has no link."


# ---------------------------------------------------------------------------
# The JSON document
# ---------------------------------------------------------------------------


class TestJson:
    def test_the_keys(self, tmp_path: Path) -> None:
        side = full_state(tmp_path)
        document = json.loads(render_json(report_of(side, base=dataclasses.replace(side, label="v1 (abc1234)"))))
        assert list(document) == [
            "document_version",
            "kind",
            "project",
            "columns",
            "answers",
            "state",
            "movement",
            "no_base",
            "look_at",
            "conclusions",
        ]
        assert (document["document_version"], document["kind"], document["project"]) == (
            1,
            "architecture-metrics",
            "demo",
        )
        assert document["columns"] == ["v1 (abc1234)", "working tree"]
        assert list(document["state"]) == [
            "label",
            "size",
            "coupling",
            "components",
            "composition_tables",
            "links_not_placed",
            "links_not_placed_by_reason",
            "cohesion",
            "coverage",
            "integrity",
        ]
        assert list(document["movement"]) == [
            "base",
            "current",
            "unchanged",
            "figures",
            "added",
            "removed",
            "ct_changes",
            "hubs_gained",
            "hubs_lost",
            "introduced",
            "resolved",
            "base_findings",
            "still_open",
        ]
        # Every component's row, not the top ten alone.
        assert [row["name"] for row in document["state"]["components"]] == ["A", "B", "C", "D", "E", "H"]
        assert document["state"]["components"][5] == {
            "name": "H",
            "type": "application",
            "ca": 0,
            "ce": 5,
            "ct": 5,
            "external_neighbours": 0,
            "unconnected_ports": 0,
            "cohesion": {"not_available": "no runnable"},
            "measured_runnables": 0,
            "no_link": None,
        }

    def test_ratios_are_the_printed_strings(self, tmp_path: Path) -> None:
        coupling = json.loads(render_json(report_of(full_state(tmp_path))))["state"]["coupling"]
        assert (coupling["mean"], coupling["deviation"], coupling["hub_threshold"]) == ("1.83", "1.46", "4.76")
        # mu − 2σ = 1.8333… − 2.92498… = −1.09165…
        assert coupling["isolated_threshold"] == "-1.09"
        assert coupling["hubs"] == ["H"]
        assert coupling["isolated"] == {"not_available": LOWER_THRESHOLD_BELOW_ZERO}

    def test_byte_stable_across_two_runs(self, tmp_path: Path) -> None:
        project = basic_project(tmp_path)
        first, second = metrics_of(project), metrics_of(project)
        assert render_json(first) == render_json(second)
        assert render_markdown(first) == render_markdown(second)
        assert render_text(first) == render_text(second)
        assert render_json(first).endswith("}\n")
