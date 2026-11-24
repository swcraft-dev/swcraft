"""Every functional scenario, one test per scenario file and variant (``-m functional`` selects them).

A scenario's ``tier`` gives the case its marks (:data:`TIER_MARKS`) and, with its ``specimen``, its
project (:func:`project_for`): the session's copy of the specimen, a copy of the variant's own for a
``"project": "fresh"`` scenario, or the one large-bench copy, asked for only by a large-bench case.
Its ``guide`` is installed before the steps run. A ``minus_variant`` expectation runs that variant
within the same test, in a project of its own when the scenario is ``fresh``.

A scenario written for a defect is committed red as ``xfail(strict=True)`` in :data:`XFAIL` until
the fix turns it green; the strict XPASS is the second witness, and the entry is removed then.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import pytest

from tests.functional.harness import (
    DEFAULT_VARIANT,
    GENERATED_SMALL,
    LARGE_BENCH,
    FunctionalProject,
    Run,
    Scenario,
    evaluate,
    load_scenario,
    resolve_targets,
    run_steps,
    scenario_paths,
)

#: The marks a scenario case carries by its tier; every case also carries ``functional``.
TIER_MARKS: dict[str, tuple[pytest.MarkDecorator, ...]] = {
    GENERATED_SMALL: (),
    LARGE_BENCH: (pytest.mark.large_bench,),
}

#: Scenario id -> the xfail reason of a scenario committed red before its fix.
XFAIL: dict[str, str] = {}


def _cases() -> list[Any]:
    cases = []
    for path in scenario_paths():
        scenario = load_scenario(path)
        marks = [*TIER_MARKS[scenario.tier]]
        if scenario.id in XFAIL:
            marks.append(pytest.mark.xfail(strict=True, reason=XFAIL[scenario.id]))
        for variant in scenario.variant_names():
            name = scenario.id if variant == DEFAULT_VARIANT else f"{scenario.id}[{variant}]"
            cases.append(pytest.param(path, variant, id=name, marks=marks))
    return cases


class FixtureRequest(Protocol):
    """What :func:`project_for` asks of pytest's ``request``."""

    def getfixturevalue(self, name: str) -> Any: ...


def project_for(scenario: Scenario, request: FixtureRequest) -> Callable[[], FunctionalProject]:
    """A maker of *scenario*'s project, by its tier; the large bench is asked for by name here alone."""
    if scenario.tier == LARGE_BENCH:
        return lambda: request.getfixturevalue("large_bench_project")
    make = request.getfixturevalue("fresh_project" if scenario.fresh else "functional_project")
    return lambda: make(scenario.specimen)


@pytest.mark.functional
@pytest.mark.parametrize(("path", "variant"), _cases())
def test_scenario(
    path: Path,
    variant: str,
    request: pytest.FixtureRequest,
    mcp: Callable[[FunctionalProject], None],
    guide: Callable[[str], None],
) -> None:
    scenario = load_scenario(path)
    make_project = project_for(scenario, request)
    others: dict[str, Run] = {}
    guide(scenario.guide)

    def run_variant(name: str) -> Run:
        project = make_project()
        mcp(project)
        return run_steps(Run(scenario, name, project, resolve_targets(scenario, project.locator), other_variant=other))

    def other(name: str) -> Run:
        if name not in others:
            others[name] = run_variant(name)
        return others[name]

    evaluate(run_variant(variant))
