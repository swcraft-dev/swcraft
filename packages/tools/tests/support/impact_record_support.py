"""The requirement rows contract ``11.0.0`` took out of the recorded ``impact`` reports.

``impact`` walks the declared references and relations in their dependency
direction. Until contract ``11.0.0`` a requirement's ``links`` named the
components that realize it, so a requirement was a *dependent* of each of those
components and appeared in every report that reached one. Since then an element
names the requirements it satisfies (``satisfies``): the requirement is the
depended-upon node, and the rows naming it left every report of the elements
below it — exactly those rows, nothing else moved.

Several suites hold byte records of those reports taken **before** the move,
which a re-recording would throw away. They are not re-recorded. Each instead puts the
removed rows back with :func:`restore_requirement_rows` before comparing, and
the helper asserts the row is absent today, so the subtraction cannot pass by
removing nothing and a requirement row reappearing in a report fails.

Keyed by ``(model root relative to the repository, start name)``; each value
names, per matched layer, the position the row stood at in the dependents list
and the requirement it named. A row with no entry here restores nothing. Shared
by ``test_impact.py`` and ``test_query.py`` so neither test module imports the
other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from swcraft_tools.impact import Dependent, ElementRef

#: ``(root, start) -> ((layer group, index, requirement id), ...)``, the rows the
#: pre-``11.0.0`` reports carried. Every requirement row carried no detail.
REQUIREMENT_ROWS_BEFORE_11_0: Mapping[tuple[str, str], tuple[tuple[str, int, str], ...]] = {
    ("examples/basic/model", "IExample"): (("interface", 4, "REQ_EXAMPLE_001"),),
    ("examples/basic/model", "ExampleSWC"): (("swc", 2, "REQ_EXAMPLE_001"),),
    ("examples/basic/model", "RAM0"): (("hardware", 4, "REQ_EXAMPLE_001"),),
    ("packages/tools/tests/fixtures/showcase/model", "IBrakeRequest"): (("interface", 3, "REQ_SHOWCASE_BASELINE_001"),),
    ("packages/tools/tests/fixtures/showcase/model", "BrakeController"): (("swc", 2, "REQ_SHOWCASE_BASELINE_001"),),
    ("packages/tools/tests/fixtures/compositions/model", "ISignal"): (("interface", 7, "REQ_COMPOSITIONS_001"),),
    ("packages/tools/tests/fixtures/compositions/model", "Consumer"): (("swc", 1, "REQ_COMPOSITIONS_001"),),
    ("packages/tools/tests/fixtures/recorded_oracles/impact_model", "Speed"): (
        ("implementation_type", 10, "REQ_SPEED_001"),
    ),
    ("packages/tools/tests/fixtures/recorded_oracles/impact_model", "float32_base"): (
        ("base_type", 12, "REQ_SPEED_001"),
    ),
    ("packages/tools/tests/fixtures/recorded_oracles/impact_model", "SpeedSensor"): (("swc", 3, "REQ_SPEED_001"),),
    ("packages/tools/tests/fixtures/recorded_oracles/impact_model", "RAM0"): (("hardware", 10, "REQ_SPEED_001"),),
    ("packages/tools/tests/fixtures/recorded_oracles/impact_model", "CORE0"): (("hardware", 13, "REQ_SPEED_001"),),
}


def restore_requirement_rows(
    result: Mapping[str, Sequence[Dependent]], root: str, name: str
) -> dict[str, list[Dependent]]:
    """*result* (an ``impact_of`` answer) with the pre-``11.0.0`` requirement rows put back.

    Asserts each row is absent today before restoring it, so a report the move
    did not reach restores nothing and a report the move did reach cannot be
    restored twice.
    """
    restored = {layer: list(dependents) for layer, dependents in result.items()}
    for layer, index, requirement in REQUIREMENT_ROWS_BEFORE_11_0.get((root, name), ()):
        rows = restored[layer]
        row = ElementRef("requirement", requirement)
        assert all(dependent.ref != row for dependent in rows), (root, name, requirement)
        assert index <= len(rows), (root, name, index, len(rows))
        rows.insert(index, Dependent(row))
    return restored


def restore_requirement_lines(rendered: str, root: str, name: str) -> str:
    """*rendered* (``format_impact``'s text) with the pre-``11.0.0`` requirement lines put back.

    The text twin of :func:`restore_requirement_rows`, for a record taken over
    the rendered report or the CLI's stdout: each row goes back as the line
    ``  requirement <id>`` at its recorded position under its layer's heading,
    after asserting no such line is there today.
    """
    lines = rendered.split("\n")
    for layer, index, requirement in REQUIREMENT_ROWS_BEFORE_11_0.get((root, name), ()):
        row = f"  requirement {requirement}"
        assert row not in lines, (root, name, requirement)
        heading = lines.index(f"{layer}:")
        lines.insert(heading + 1 + index, row)
    return "\n".join(lines)
