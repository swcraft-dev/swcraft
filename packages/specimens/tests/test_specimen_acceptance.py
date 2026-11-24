"""The acceptance passports of the later shapes.

The 18 passports under `tests/fixtures/specimen-acceptance/` are synthetic (that directory's
README). `REFUSED` records the passports refused before generation, with the leaves
`check_feasibility` refuses and their classes, in leaf-table order; every other passport is built
and self-checked here.

Determinism: every passport that builds is built twice in one process with equal file maps;
`large-bench`, `passport-small` and `example-minimal` are built in two subprocesses under
`PYTHONHASHSEED` 0 and 4242 with byte-equal file maps; and the project-tree and sidecar SHA-256 of
`large-bench` and `passport-small` are pinned as goldens, the first to pin task, sequence,
requirement and description bytes.

What this does not reach:

- **Two platforms, no more.** The goldens were recorded on macOS arm64 and are asserted on Linux
  x86_64; no test here runs on Windows. Every write names `encoding="utf-8"` and `newline="\n"`,
  file-map paths are POSIX strings, and the later-shape modules use no float, no set order and no
  platform call: a Windows difference is unlikely, not proven absent.
- **Other acceptance specimens than the two pinned**, held to in-process determinism and their
  self-check only.
- **Other hash seeds than the two tried**, and Python versions outside the CI matrix (3.11-3.14).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml
from swcraft_specimens.passport import read_passport
from swcraft_specimens.sidecar import project_tree_sha256
from swcraft_specimens.specimen import build as build_module
from swcraft_specimens.specimen import layout as layout_module
from swcraft_specimens.specimen.build import build_specimen_files, self_check
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.leaves import stated_leaves
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.requirements import REQUIREMENT_TOPICS
from swcraft_specimens.specimen.shape import Shape, specimen_shape
from tests.oracle.generator_start import generator_command
from tests.oracle.sidecar_golden import sidecar_golden_sha256
from tests.oracle.swcraft_gate import (
    CLEAN_RESOURCE_REPORT,
    assert_live_passport_equals_sidecar,
    assert_swcraft_clean,
    check_commands,
    ordered_findings,
    run_checked,
)

ROOT = Path(__file__).parents[1]
ACCEPTANCE = Path(__file__).parent / "fixtures" / "specimen-acceptance"
PYTHON = sys.executable

#: The 18 acceptance passports.
NAMES = (
    "basic-twin",
    "benchmark-default",
    "example-configuration-only-controlled",
    "example-minimal",
    "harness-two-rows",
    "large-bench",
    "map-bound",
    "no-memory",
    "passport-small",
    "scenario-data-element-readers-paged",
    "scenario-effective-level-paged",
    "scenario-find-sixteen-names",
    "scenario-impact-summary-bounded",
    "scenario-import-conflicts-reported",
    "scenario-level-list-paged",
    "scenario-sender-receiver-table-paged",
    "scenario-unconnected-one-component",
    "showcase-twin",
)

#: The only string leaves a passport holds; every other leaf is a count, a mean or a list.
VERSION_LEAVES = {"passport_version", "contract_version"}

#: The fan-out sentence `showcase-twin` is refused with.
SHOWCASE_TWIN_SENTENCE = "needs 11 named interfaces for 19 ports; layers.interfaces states 10"

#: The passports refused before generation, each with its refusals `(leaf, class)` in leaf-table
#: order; every other passport builds.
REFUSED: dict[str, tuple[tuple[str, str], ...]] = {
    "showcase-twin": (("interfaces.fan_out.top", "fan-out"),),
}
BUILDS = tuple(name for name in NAMES if name not in REFUSED)


def passport_text(name: str) -> str:
    return (ACCEPTANCE / f"{name}.json").read_text(encoding="utf-8")


def string_leaves(node: Any, path: str = "") -> list[str]:
    """The dotted paths of every string anywhere in a JSON document."""
    if isinstance(node, str):
        return [path]
    if isinstance(node, dict):
        return [leaf for key, value in node.items() for leaf in string_leaves(value, f"{path}.{key}" if path else key)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in string_leaves(value, path)]
    return []


def test_the_directory_holds_exactly_the_18_passports_and_its_readme() -> None:
    files = sorted(path.name for path in ACCEPTANCE.iterdir())
    assert files == sorted([*(f"{name}.json" for name in NAMES), "README.md"])
    assert set(REFUSED) <= set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_each_passport_is_schema_valid_and_holds_no_string_beyond_the_versions(name: str) -> None:
    text = passport_text(name)
    read_passport(text)
    assert set(string_leaves(json.loads(text))) == VERSION_LEAVES


@pytest.mark.parametrize("name", NAMES)
def test_the_acceptance_table(name: str) -> None:
    refusals = check_feasibility(read_passport(passport_text(name)))
    assert tuple((r.leaf, r.refusal_class) for r in refusals) == REFUSED.get(name, ())


def test_showcase_twin_keeps_its_fan_out_sentence() -> None:
    refusals = check_feasibility(read_passport(passport_text("showcase-twin")))
    assert [(r.leaf, r.refusal_class, r.sentence) for r in refusals] == [
        ("interfaces.fan_out.top", "fan-out", SHOWCASE_TWIN_SENTENCE)
    ]


def test_scenario_unconnected_one_component_builds_at_480_or_more() -> None:
    # The star-size bound puts the least total at 480; the u1 candidate holds the 42.
    passport = read_passport(passport_text("scenario-unconnected-one-component"))
    shape = specimen_shape(stated_leaves(passport))
    assert isinstance(shape, Shape)
    assert sum(shape.counts) >= 480
    assert max(shape.counts) >= 42


@cache
def built(name: str) -> dict[str, bytes]:
    """The passport's complete file map, built once per test session."""
    return build_specimen_files(SpecimenPreset(DEFAULT_NAME, read_passport(passport_text(name))))


def project_part(files: dict[str, bytes]) -> dict[str, bytes]:
    return {path: data for path, data in files.items() if path != "generation.json"}


@pytest.mark.parametrize("name", BUILDS)
def test_a_passport_that_builds_meets_every_stated_leaf(name: str) -> None:
    # `build_specimen_files` refuses a missed stated leaf (the self-check) and checks the whole
    # tree, sidecar included, before it returns.
    assert "generation.json" in built(name)


# --- determinism ----------------------------------------------------------------------------------

EXPECTED_ACCEPTANCE_TREE_SHA256 = {
    # Update only after an intentional change of the specimen builder or an acceptance passport,
    # with a reviewed regeneration. Recorded on macOS arm64, with no SWCraft found (the autouse fixture).
    "large-bench": "2963239e19aa98b56a67620892046ed2d9fadfd6f662ff520fc1a78e94d229ef",
    "passport-small": "251b95fabf898e01ad5c00b312c990ac6ee28cd096624772729d66aaadaca33e",
}
EXPECTED_ACCEPTANCE_SIDECAR_SHA256 = {
    # Taken with `generator.version` read back to GOLDEN_GENERATOR_VERSION (tests/oracle/sidecar_golden.py),
    # so a SWCraft version bump moves none of them.
    "large-bench": "0e2014c62876da9e0d56b14fe2d9b43bf7eff050106c5801df92cd473d69a08d",
    "passport-small": "018dda0a42243f1ce6e9f1b42e5011a2c11aa7cb7f975387f6676406f007a2b8",
}

#: The specimens built under two hash seeds: the largest, the paired tail's, the smallest.
HASH_SEED_SPECIMENS = ("large-bench", "passport-small", "example-minimal")

#: A child process's build: each generated file's path and SHA-256, as one JSON object. The child sees
#: no installed `swcraft-tools`, as the autouse fixture makes this process see none; otherwise the
#: manifest and sidecar would record the release installed beside the generator.
CHILD_BUILD = (
    "from importlib import metadata\n"
    "from swcraft_specimens import toolchain\n"
    "real = metadata.version\n"
    "def version(name):\n"
    "    if name == toolchain.SWCRAFT_DISTRIBUTION:\n"
    "        raise metadata.PackageNotFoundError(name)\n"
    "    return real(name)\n"
    "metadata.version = version\n"
    "import hashlib, json, sys; from pathlib import Path; "
    "from swcraft_specimens.passport import read_passport; "
    "from swcraft_specimens.specimen.build import build_specimen_files; "
    "from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset; "
    "text = Path(sys.argv[1]).read_text(encoding='utf-8'); "
    "files = build_specimen_files(SpecimenPreset(DEFAULT_NAME, read_passport(text))); "
    "print(json.dumps({p: hashlib.sha256(d).hexdigest() for p, d in sorted(files.items())}))"
)


@pytest.mark.parametrize("name", BUILDS)
def test_two_builds_in_one_process_are_equal(name: str) -> None:
    again = build_specimen_files(SpecimenPreset(DEFAULT_NAME, read_passport(passport_text(name))))
    assert again == built(name)


@pytest.mark.parametrize("name", HASH_SEED_SPECIMENS)
def test_the_build_is_python_hash_seed_independent(name: str) -> None:
    maps = []
    for hash_seed in ("0", "4242"):
        completed = run_checked(
            [PYTHON, "-c", CHILD_BUILD, str(ACCEPTANCE / f"{name}.json")],
            ROOT,
            env=dict(os.environ, PYTHONHASHSEED=hash_seed),
        )
        maps.append(json.loads(completed.stdout))
    differing = sorted(p for p in maps[0].keys() | maps[1].keys() if maps[0].get(p) != maps[1].get(p))
    assert differing == []
    # The children built what this process builds.
    assert maps[0] == {p: hashlib.sha256(d).hexdigest() for p, d in sorted(built(name).items())}


@pytest.mark.parametrize("name", sorted(EXPECTED_ACCEPTANCE_TREE_SHA256))
def test_the_acceptance_goldens(name: str) -> None:
    files = built(name)
    assert project_tree_sha256(project_part(files)) == EXPECTED_ACCEPTANCE_TREE_SHA256[name]
    assert sidecar_golden_sha256(files["generation.json"]) == EXPECTED_ACCEPTANCE_SIDECAR_SHA256[name]
    sidecar = json.loads(files["generation.json"])
    assert sidecar["project"]["project_tree_sha256"] == EXPECTED_ACCEPTANCE_TREE_SHA256[name]


# --- the integration acceptance -------------------------------------------------------------------

#: The pinned SWCraft release, the external oracle; the integration tests skip without it.
SWCRAFT = os.environ.get("SWCRAFT_BIN")


def swcraft() -> str:
    if not SWCRAFT:
        pytest.skip("SWCRAFT_BIN is not set")
    return SWCRAFT


def console_script() -> Path:
    """The installed `swcraft-specimens` executable next to the interpreter."""
    folder = Path(sys.executable).parent
    return folder / ("swcraft-specimens.exe" if sys.platform == "win32" else "swcraft-specimens")


@pytest.fixture(scope="module")
def script_tree(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Path]:
    """An acceptance specimen built by the `swcraft-specimens` script, once per module run."""
    trees: dict[str, Path] = {}

    def tree(name: str) -> Path:
        if name not in trees:
            out = tmp_path_factory.mktemp(name) / "specimen"
            run_checked(
                [*generator_command([str(console_script())]), "specimen", str(ACCEPTANCE / f"{name}.json")]
                + ["--out", str(out)],
                ROOT,
                what=name,
            )
            trees[name] = out
        return trees[name]

    return tree


def live_passport(oracle: str, tree: Path) -> dict[str, Any]:
    """SWCraft's own `swcraft passport` on a built tree."""
    completed = run_checked([oracle, "passport", "--project", str(tree / "swcraft.yaml")], ROOT)
    document: dict[str, Any] = json.loads(completed.stdout)
    return document


@pytest.mark.integration
@pytest.mark.parametrize("name", BUILDS)
def test_swcraft_meets_the_acceptance_specimen(name: str, script_tree: Callable[[str], Path]) -> None:
    """Each of the 17, built by the `swcraft-specimens` script, meets its passport and passes the gates.

    `swcraft passport` meets every stated leaf and equals the sidecar's whole passport,
    `contract_version` by major; `check-model --format json` reports exactly the ordered multiset
    (and `--warnings-as-errors` passes where it is empty); `validate` passes.
    """
    oracle = swcraft()
    tree = script_tree(name)
    live = live_passport(oracle, tree)
    assert self_check(json.loads(passport_text(name)), live) == []
    sidecar = json.loads((tree / "generation.json").read_text(encoding="utf-8"))
    assert_live_passport_equals_sidecar(live, sidecar["passport"])
    ordered = ordered_findings(json.loads(passport_text(name)))
    commands = check_commands(ordered)
    assert_swcraft_clean(oracle, tree / "swcraft.yaml", commands, cwd=ROOT, ordered=ordered)


class MemoryBlock(NamedTuple):
    """One memory's block of `swcraft resource-report`: its size, its use, its consumers."""

    size: int
    used: int
    consumers: tuple[tuple[str, int], ...]


#: SWCraft's resource-report line form.
RESOURCE_LINE = re.compile(r"^RESOURCE (\S+) \((\w+)(?:, size (\d+) bytes)?, \w+\)$")
CONSUMPTION_LINE = re.compile(r"^  CONSUMPTION (\d+) bytes of a ")
CONSUMER_LINE = re.compile(r"^    - (\S+): (\d+) bytes$")


def memory_blocks(report: str) -> dict[str, MemoryBlock]:
    """Each memory block of a resource report, by memory name; core blocks are skipped."""
    blocks: dict[str, MemoryBlock] = {}
    current: str | None = None
    for line in report.splitlines():
        resource = RESOURCE_LINE.match(line)
        if resource:
            name, kind, size = resource.groups()
            current = name if kind == "memory" else None
            if current is not None:
                assert size is not None, line
                blocks[current] = MemoryBlock(int(size), 0, ())
            continue
        if current is None:
            continue
        consumption = CONSUMPTION_LINE.match(line)
        if consumption:
            blocks[current] = blocks[current]._replace(used=int(consumption.group(1)))
        consumer = CONSUMER_LINE.match(line)
        if consumer:
            entry = (consumer.group(1), int(consumer.group(2)))
            blocks[current] = blocks[current]._replace(consumers=(*blocks[current].consumers, entry))
    return blocks


def _terms(place: dict[str, Any]) -> set[str]:
    return {
        term
        for evidence in place.get("evidence", [])
        if evidence.get("kind") == "text"
        for term in evidence.get("terms", [])
    }


#: The kinds of `existing_links` target that sit on a component.
COMPONENT_SITES = {"swc", "port", "runnable"}
#: The places `find {"for_requirement": ...}` answers by default.
DEFAULT_PLACES = 3


@pytest.mark.integration
def test_the_find_calls_on_large_bench(script_tree: Callable[[str], Path]) -> None:
    """`find` answers the spread case's requirement by text and ranks components for it.

    `find {"text": "supply voltage"}` answers first the supply-voltage requirement of the first
    spread case, and `find {"for_requirement": ...}` ranks, within its default places, at least
    three components that hold its terms and none of its links.
    """
    from tests.oracle.mcp_client import McpClient

    oracle = swcraft()
    manifest = script_tree("large-bench") / "swcraft.yaml"
    client = McpClient.start([oracle, "mcp-serve", "--project", str(manifest)], cwd=ROOT)
    try:
        client.initialize()
        answer = client.call_tool("find", {"text": "supply voltage"})
        assert answer["candidates"] != "none", answer
        first = answer["candidates"][0]
        requirement = first["address"]["element"]
        ranking = client.call_tool("find", {"for_requirement": requirement})
    finally:
        client.close()
    # Case 0, the first spread requirement, takes topic 0: "Supply voltage monitoring".
    assert first["identity"] == f"{requirement} \u2014 {REQUIREMENT_TOPICS[0][0]}", first
    assert {"supply", "voltage"} <= set(first["words"]), first
    linked = {
        link["target"]["owner"] or link["target"]["name"]
        for link in ranking["existing_links"]
        if link["target"]["kind"] in COMPONENT_SITES
    }
    assert linked, ranking["existing_links"]
    places = ranking["places"]
    assert len(places) <= DEFAULT_PLACES, places
    sharing = [
        (position, place["group"]["name"])
        for position, place in enumerate(places, start=1)
        if place["group"]["kind"] == "swc" and {"supply", "voltage"} <= _terms(place)
    ]
    print(
        f"large-bench find: {len(answer['candidates'])} requirement(s); the components sharing "
        f"both terms at places {[position for position, _ in sharing]} of {len(places)}; "
        f"{len(linked)} component(s) linked"
    )
    assert len(sharing) >= 3, ranking
    assert not {name for _, name in sharing} & linked, (sharing, linked)


@pytest.mark.integration
def test_the_per_core_ram_on_large_bench(script_tree: Callable[[str], Path]) -> None:
    """A unique largest RAM consumer in `resource-report`, every memory one core's."""
    oracle = swcraft()
    tree = script_tree("large-bench")
    report = run_checked([oracle, "resource-report", "--project", str(tree / "swcraft.yaml")], ROOT)
    assert CLEAN_RESOURCE_REPORT.search(report.stdout), report.stdout
    blocks = memory_blocks(report.stdout)
    assert blocks, report.stdout
    figures = [size for block in blocks.values() for _, size in block.consumers]
    largest = max(figures)
    assert largest > 0
    assert figures.count(largest) == 1, f"the largest figure {largest} is not unique"
    hardware = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((tree / "model" / "hardware").glob("*.yaml"), key=lambda path: path.as_posix())
    ]
    memories = [element for element in hardware if element["kind"] == "memory"]
    cores = [element for element in hardware if element["kind"] == "core"]
    assert sorted(m["name"] for m in memories) == sorted(blocks)
    assert all(len(m["accessible"]) == 1 for m in memories)
    # large-bench states as many memories as cores (4 and 4): each memory is its own core's.
    assert len(memories) == len(cores) == 4
    assert len({m["accessible"][0] for m in memories}) == len(memories)
    print(
        f"large-bench RAM: largest consumer {largest} bytes; memories used "
        + ", ".join(f"{block.used} of {block.size} bytes" for _, block in sorted(blocks.items()))
    )


# --- the ceiling, measured ------------------------------------------------------------------------

RECORDED_PASSPORTS = Path(__file__).parent / "fixtures" / "passport"

#: The later-shape steps of the build, by the name `build` imports them under: their summed wall time
#: is "the later shapes alone" (the paired tail and the u1 candidate run inside the shape and are
#: timed end to end only).
LATER_SHAPE_STEPS = (
    "build_tasks",
    "plan_requirements",
    "descriptions",
    "memory_budgets",
    "rate_partitions",
    "build_requirements",
    "data_accesses",
)


def ceiling_vector(**changes: int) -> dict[str, Any]:
    """The recorded `large-scale-shapes` passport; `changes` overrides `sequences`, `spread` or `unallocated`.

    10,000 runnables, 10,000 tasks (100 idle), 9,900 sequences, 10,000 requirements (S = 100,
    O = 1,000, N = 600, so G = 8,300), 10,000 links and V = 50, the three new layers' identifier
    leaves unstated.
    """
    text = (RECORDED_PASSPORTS / "large-scale-shapes.swcraft-passport.json").read_text(encoding="utf-8")
    passport: dict[str, Any] = json.loads(text)
    passport["components"]["runnables"] = 10_000
    passport["layers"]["tasks"] = 10_000
    passport["tasks"]["idle"] = 100
    passport["layers"]["sequences"] = changes.get("sequences", 9_900)
    passport["layers"]["requirements"] = 10_000
    passport["requirements"] = {
        "on_all_site_kinds": changes.get("spread", 100),
        "on_one_port": 1_000,
        "unallocated": changes.get("unallocated", 600),
        "stale_links": 50,
        "links": 10_000,
    }
    for layer in ("requirements", "tasks", "sequences"):
        del passport["identifiers"][layer]
    return passport


def no_exact_program(line: str) -> None:
    """The step-3 notice, turned into a failure before the exact program runs.

    No test here runs the exact program at the ceiling.
    """
    raise AssertionError(f"the exact program was reached: {line}")


def refused_without_the_exact_program(passport: dict[str, Any]) -> list[tuple[str, str, str]]:
    token = layout_module.NOTICE.set(no_exact_program)
    try:
        refusals = check_feasibility(read_passport(json.dumps(passport)))
    finally:
        layout_module.NOTICE.reset(token)
    return [(r.leaf, r.refusal_class, r.sentence) for r in refusals]


def test_one_more_sequence_than_the_ceiling_vector_is_refused() -> None:
    # 10,000 runnables over 9,900 busy tasks: the first 100 hold two (chunks larger first), so
    # sequence 9,900 repeats task 0's two steps: 10,002 steps.
    assert refused_without_the_exact_program(ceiling_vector(sequences=9_901)) == [
        (
            "layers.sequences",
            "ceiling",
            "writes 10002 steps; this version builds at most 10,000 of anything",
        ),
    ]


def test_more_unallocated_cases_than_the_ceiling_vector_are_refused() -> None:
    # N = 3,400 and S = 0: 3 (S + N) = 10,200 description sentences.
    assert refused_without_the_exact_program(ceiling_vector(spread=0, unallocated=3_400)) == [
        (
            "layers.requirements",
            "ceiling",
            "writes 10200 description sentences; this version builds at most 10,000 of anything",
        ),
    ]


#: The passports timed: the ceiling vector, and the three that run the paired tail's and the u1
#: candidate's closed forms or are the largest acceptance specimen.
TIMED = ("ceiling-vector", "large-bench", "passport-small", "scenario-unconnected-one-component")


@pytest.mark.slow
@pytest.mark.parametrize("name", TIMED)
def test_the_later_shapes_at_the_ceiling(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ceiling vector passes `check_feasibility`, builds and meets every stated leaf.

    Its data accesses are written; it and the three acceptance specimens are timed, the later
    shapes alone and end to end (printed). The exact program is never run (`no_exact_program`).
    """
    passport = ceiling_vector() if name == "ceiling-vector" else json.loads(passport_text(name))
    spent: dict[str, float] = dict.fromkeys(LATER_SHAPE_STEPS, 0.0)

    def timed(step: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                spent[step] += time.perf_counter() - start

        return wrapper

    for step in LATER_SHAPE_STEPS:
        monkeypatch.setattr(build_module, step, timed(step, getattr(build_module, step)))
    start = time.perf_counter()
    token = layout_module.NOTICE.set(no_exact_program)
    try:
        assert check_feasibility(read_passport(json.dumps(passport))) == []
    finally:
        layout_module.NOTICE.reset(token)
    files = build_specimen_files(
        SpecimenPreset(DEFAULT_NAME, read_passport(json.dumps(passport))), notify=no_exact_program
    )
    end_to_end = time.perf_counter() - start
    assert "generation.json" in files
    if name == "ceiling-vector":
        sidecar = json.loads(files["generation.json"])
        assert self_check(passport, sidecar["passport"]) == []
        accesses = sum(
            len(runnable.get(kind, []))
            for path, data in files.items()
            if path.startswith("model/swc/")
            for runnable in yaml.safe_load(data).get("runnables", [])
            for kind in ("reads", "writes")
        )
        # R1 of each component reads and writes through its SR ports.
        assert accesses == 2_750
        print(f"{name}: data accesses {accesses}")
    later = sum(spent.values())
    print(
        f"{name}: later shapes {later:.2f} s, end to end {end_to_end:.2f} s ("
        + ", ".join(f"{step} {seconds:.2f}" for step, seconds in spent.items())
        + ")"
    )
