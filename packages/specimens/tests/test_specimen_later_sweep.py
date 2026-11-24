"""A seeded sweep over the later-shape leaves.

The task, sequence, requirement and hardware leaves are drawn with fixed seeds over two bases: the
neutral passport (two components) and the `topology` fixture (30 components, declared levels, hubs).
For every draw `check_feasibility` admits:

- the build succeeds and self-checks against every stated leaf (`build_specimen_files`);
- `requirement_sentence_count`, which the `ceiling` refusal reads, equals the description sentences
  the build writes;
- every component's runnables run in tasks of one partition.

What this does not reach: the draws stay small (a few dozen tasks, requirements and runnables), the
bases are two, and a refused draw is only counted, its refusal not judged; the refusals are held by
`test_specimen_tasks.py`, `test_specimen_requirements.py` and `test_specimen_hardware.py`.
"""

from __future__ import annotations

import copy
import random
from collections.abc import Callable
from typing import Any

import pytest
from swcraft_specimens.specimen.build import build_project, build_specimen_files
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.leaves import stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.requirements import requirement_sentence_count
from tests.oracle.specimen_helpers import load_specimen, neutral_passport, sentences_built

#: Draws per base and seed (the 30-component base builds slower), and the least of them that must
#: build, so the sweep is not vacuous.
DRAWS = {"neutral": 60, "topology": 15}
LEAST_BUILT = {"neutral": 15, "topology": 4}


def _topology() -> dict[str, Any]:
    return copy.deepcopy(dict(load_specimen("topology").passport))


BASES: dict[str, Callable[[], dict[str, Any]]] = {
    "neutral": lambda: copy.deepcopy(neutral_passport()),
    "topology": _topology,
}


def draw(base: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """The base with random task, sequence, requirement, runnable and hardware leaves.

    The four layers' identifier lengths are unstated.
    """
    passport = copy.deepcopy(base)
    components = int(passport["layers"]["swc"])
    tasks = rng.randint(0, 8)
    passport["layers"]["tasks"] = tasks
    passport["tasks"]["idle"] = rng.randint(0, tasks)
    if rng.random() < 0.5:
        del passport["layers"]["sequences"]
    else:
        passport["layers"]["sequences"] = rng.randint(0, 6)
    if rng.random() < 0.3:
        del passport["components"]["runnables"]
    else:
        passport["components"]["runnables"] = rng.randint(components // 2, 2 * components + 4)
    cores = rng.randint(1, 3)
    partitions = rng.randint(0, 4)
    memories = rng.randint(0, 4)
    passport["hardware"] = {
        "cores": cores,
        "partitions": partitions,
        "memories": memories,
        "rated_partitions": rng.randint(0, partitions),
    }
    passport["layers"]["hardware"] = 1 + cores + partitions + memories
    spread, one_port, unallocated = rng.randint(0, 3), rng.randint(0, 3), rng.randint(0, 4)
    total = spread + one_port + unallocated + rng.randint(0, 6)
    passport["layers"]["requirements"] = total
    passport["requirements"] = {
        "on_all_site_kinds": spread,
        "on_one_port": one_port,
        "unallocated": unallocated,
        "stale_links": rng.randint(0, 4),
    }
    for layer in ("hardware", "tasks", "sequences", "requirements"):
        passport["identifiers"].pop(layer, None)
    return passport


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("base", sorted(BASES))
def test_every_feasible_draw_builds_and_agrees_with_the_checks(base: str, seed: int) -> None:
    rng = random.Random(f"{base}-{seed}")
    built = 0
    for _ in range(DRAWS[base]):
        passport = draw(BASES[base](), rng)
        if check_feasibility(passport):
            continue
        preset = SpecimenPreset(DEFAULT_NAME, passport)
        assert "generation.json" in build_specimen_files(preset), passport
        project = build_project(preset)
        assert requirement_sentence_count(stated_leaves(passport)) == sentences_built(project)
        partitions_of: dict[str, set[str | None]] = {}
        for task in project.tasks:
            for component, _ in task.runnables:
                partitions_of.setdefault(component, set()).add(task.partition)
        assert all(len(named) == 1 for named in partitions_of.values()), partitions_of
        built += 1
    print(f"{base}, seed {seed}: {built} of {DRAWS[base]} draws built")
    assert built >= LEAST_BUILT[base], f"{built} of {DRAWS[base]} draws built"
