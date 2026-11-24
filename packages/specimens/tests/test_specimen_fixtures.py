"""The specimen fixtures, their recorded passports, and the sweep.

The specimen presets under `swcraft_specimens/presets/specimens/` are each held to the passport
`swcraft passport` computed on their generated tree, recorded once as an external program's output
(`tests/fixtures/passport/<fixture>.swcraft-passport.json`): the generator's pass equals it, and
every leaf the preset states is met by it. Each fixture is deterministic, a fixed point of preset
-> sidecar passport -> preset, and pinned by its goldens.

The sweep shows the construction's sufficiency on generated configurations (the inequalities are
necessary; the construction is shown sufficient on a sweep, not proved):

- **Positive cases.** Each is an independent **witness**: a small model built directly in the
  connection model, its passport computed by the pass over the witness's own documents and fed
  back whole as the configuration. Every positive case must be met; a refusal of any class fails.
- **Negative cases.** One per inequality, each refused with its class.

What the positive half does not reach: a passport does not fix the port counts outside `top`, so a
witness of more than eight components takes those from the placement of its own `top`, `hubs` and
`total` (tested on its own) and is independent in everything else; and a witness names at most
eight interfaces, so the fan-out list is stated whole.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.model import LinearCompuMethod, Project
from swcraft_specimens.passport import (
    PassportRefused,
    compute_passport,
    validate_passport,
)
from swcraft_specimens.sidecar import project_tree_sha256
from swcraft_specimens.specimen.build import build_project, build_specimen_files, self_check
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.leaves import LEAVES, stated_leaves
from swcraft_specimens.specimen.preset import (
    DEFAULT_NAME,
    SpecimenPreset,
    parse_specimen_text,
    preset_text,
)
from swcraft_specimens.specimen.shape import Shape, specimen_shape
from swcraft_specimens.specimen.vocabulary import VERSION
from tests.oracle.sidecar_golden import sidecar_golden_sha256
from tests.oracle.specimen_helpers import (
    clear_search_caches,
    load_specimen,
    preset_path,
    shaped_witness,
    topology,
    witness,
)

ROOT = Path(__file__).parents[1]
SPECIMENS = (
    "minimal",
    "topology",
    "large-scale-topology",
    "shapes",
    "chain-empty",
    "large-scale-shapes",
)
RECORDED = ROOT / "tests/fixtures/passport"

EXPECTED_SPECIMEN_TREE_SHA256 = {
    # Update only after an intentional change of the specimen builder or a fixture preset, with a
    # reviewed regeneration; the recorded `swcraft passport` is re-recorded unless the change keeps
    # every leaf (a renaming moves the trees, not the recordings). Taken with no SWCraft found (the
    # autouse fixture).
    "minimal": "cf17d320581986520ad08d62ca7146797962288e060f1cff0c868076c4581138",
    "topology": "d27a2da2ad411b2ab86db843b4d0b5ff768f5547033ebb29ce884a7f955177e6",
    "large-scale-topology": "4eb1142d8022919394f5ba0f4aec875536200060457888a5730643faa4548264",
    "shapes": "a3cc4996ac88e87e1a52471bd12da342a785c319e7dd54d70b93610e270350eb",
    "chain-empty": "8caad6d27523c28ea8588e51cbf3b6a85964e3e441cfc1bb98021b7ddcbc0a1c",
    "large-scale-shapes": "4a8304f5d62cbcc77010929bfc94102792203b7bc1035336da8a1d35447f5c03",
}
EXPECTED_SPECIMEN_SIDECAR_SHA256 = {
    # Taken with `generator.version` read back to GOLDEN_GENERATOR_VERSION (tests/oracle/sidecar_golden.py),
    # so a SWCraft version bump moves none of them.
    "minimal": "dac9acccc510074442cda3441fdc0d7b4a652f9d59984cede2815213f20ec57a",
    "topology": "f2344630cba71e2447c0c006fb28b4d406fd5c1d5bc35722587e32368dc5c00d",
    "large-scale-topology": "21d165fba57b5dd050ee7c1a95bdcdbb8badf8d30d877154bcaa7be2cb889a6f",
    "shapes": "ce1e86f9a3e76e2e429b029981bfa5ac535fa9a0ad32ddc0a906ea38eb94c4d7",
    "chain-empty": "940790ca3c854f5627bd8be873440f49d836dfb570529c2da1d8afd2448c1e8a",
    "large-scale-shapes": "5a0fc418f7f8867d9bb09af6b647910550f26aa3205b2d551b135dced88a9e41",
}

#: The recorded SWCraft passports' file digests: a renaming that keeps every stated length moves the
#: trees, never these.
EXPECTED_RECORDED_PASSPORT_SHA256 = {
    "minimal": "315b212f91cd1dbfad303137fe0f62c7cac09b4473d70c75bcab8964f636db49",
    "topology": "2edaad26dcacbc6711cb8a283e19f14d09d694548cdd9242359e6a042e89a511",
    "large-scale-topology": "14a8b55c5b12d0936039cf76040d1e449caeaa647d994475ce58d144c7b7bdeb",
    "shapes": "0277ed7e97b287c456e5dc8ec4b6626400d0683da616603f7398a7725874fb44",
    "chain-empty": "bb747a833e4c8b7fdd7d142badfd991f742700274bddb7be1e8952d0bea1ef3f",
    "large-scale-shapes": "086667aa605e1afd91d68c6de993ccc878f0469905d879e78913d2e7403ba77f",
}


def strict(value: Any) -> str:
    """The canonical text of a passport: key order and int/float kept, so `0` is not `0.0`."""
    return json.dumps(value, ensure_ascii=False)


def recorded(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((RECORDED / f"{name}.swcraft-passport.json").read_text(encoding="utf-8"))
    return document


#: The generator's requirement that a large preset generates in seconds, not minutes: the bound of a
#: large-scale fixture's build, end to end. Minutes start at 120 s; the slower Windows runner meets 60 s.
LARGE_SCALE_SPEC_SECONDS = 60.0

_BUILT: dict[str, tuple[dict[str, bytes], float]] = {}


def timed_build(name: str) -> tuple[dict[str, bytes], float]:
    """The fixture's complete file map and the seconds its build took, built once per test process.

    The search caches are emptied before the build, so an earlier test's search never shortens it.
    """
    if name not in _BUILT:
        clear_search_caches()
        started = time.perf_counter()
        files = build_specimen_files(load_specimen(name))
        _BUILT[name] = (files, time.perf_counter() - started)
    return _BUILT[name]


def built(name: str) -> dict[str, bytes]:
    """The fixture's complete file map, built once per test process."""
    return timed_build(name)[0]


def project_part(files: dict[str, bytes]) -> dict[str, bytes]:
    return {path: data for path, data in files.items() if path != "generation.json"}


# --- the fixtures ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", SPECIMENS)
class TestFixtures:
    def test_the_preset_is_a_specimen_and_the_recording_a_valid_passport(self, name: str) -> None:
        assert load_specimen(name).passport
        assert strict(validate_passport(recorded(name))) == strict(recorded(name))

    def test_the_pass_equals_the_recorded_swcraft_passport(self, name: str) -> None:
        # The sidecar's passport is the pass over the rendered documents (`render_specimen`),
        # recomputed by the sidecar's validator from a second parse of the whole tree.
        sidecar = json.loads(built(name)["generation.json"])
        assert strict(sidecar["passport"]) == strict(recorded(name))

    def test_every_stated_leaf_is_met_by_the_recorded_passport(self, name: str) -> None:
        assert self_check(load_specimen(name).passport, recorded(name)) == []

    def test_generation_is_deterministic(self, name: str) -> None:
        assert build_specimen_files(load_specimen(name)) == built(name)

    def test_the_fixed_point(self, name: str) -> None:
        sidecar = json.loads(built(name)["generation.json"])
        again = parse_specimen_text(preset_text(sidecar["passport"]), "fixed point")
        assert project_part(build_specimen_files(again)) == project_part(built(name))

    def test_the_goldens(self, name: str) -> None:
        files = built(name)
        assert project_tree_sha256(project_part(files)) == EXPECTED_SPECIMEN_TREE_SHA256[name]
        sidecar_digest = sidecar_golden_sha256(files["generation.json"])
        assert sidecar_digest == EXPECTED_SPECIMEN_SIDECAR_SHA256[name]
        sidecar = json.loads(files["generation.json"])
        assert sidecar["project"]["project_tree_sha256"] == EXPECTED_SPECIMEN_TREE_SHA256[name]

    def test_the_recorded_passport_is_not_re_recorded(self, name: str) -> None:
        data = (RECORDED / f"{name}.swcraft-passport.json").read_bytes()
        assert hashlib.sha256(data).hexdigest() == EXPECTED_RECORDED_PASSPORT_SHA256[name]


@pytest.mark.slow
@pytest.mark.parametrize("name", ["large-scale-topology", "large-scale-shapes"])
def test_the_large_scale_fixture_generates_in_seconds(name: str) -> None:
    # Measured end to end (build, gates, sidecar) on the one build of this process, whichever test
    # made it first.
    _, seconds = timed_build(name)
    assert seconds < LARGE_SCALE_SPEC_SECONDS


def test_the_large_scale_fixture_states_its_invented_figures() -> None:
    """The preset's rounded, invented figures at a large synthetic scale."""
    passport = load_specimen("large-scale-topology").passport
    assert passport["layers"]["swc"] == 75
    assert passport["layers"]["interfaces"] == 500
    assert passport["layers"]["connectors"] == passport["connectors"]["assembly"] == 1400
    assert passport["layers"]["implementation_types"] == 500
    assert passport["layers"]["base_types"] == 12
    assert passport["layers"]["hardware"] == 3
    assert passport["components"]["ports"]["top"][0] == 430
    assert passport["components"]["unconnected"] == {"total": 290, "max_on_one": 140}
    assert passport["interfaces"]["fan_out"]["max"] == 600
    text = preset_path("large-scale-topology").read_text(encoding="utf-8")
    assert "invented" in text and "neutral" in text and "record" not in text


def test_the_large_scale_shapes_fixture_states_the_rounded_figures() -> None:
    """The rounded figures, the two identifier means adjusted to an exact mean.

    Each is adjusted by the least amount an exact mean over the layer's count needs.
    """
    passport = load_specimen("large-scale-shapes").passport
    assert passport["layers"] == {
        "swc": 80,
        "interfaces": 500,
        "connectors": 1400,
        "application_types": 600,
        "implementation_types": 500,
        "base_types": 12,
        "requirements": 0,
        "compositions": 16,
        "tasks": 0,
        "sequences": 0,
        "hardware": 3,
        "mapping_sets": 1,
        "units": 30,
        "compu_methods": 300,
        "data_constraints": 200,
    }
    assert passport["interfaces"]["sender_receiver"] == 240
    assert passport["interfaces"]["client_server"] == 80
    assert passport["interfaces"]["fan_out"] == {
        "max": 600,
        "top": [600, 600, 60, 50, 30, 25, 20, 20],
    }
    assert passport["connectors"] == {"assembly": 1400, "delegation": 3800, "links": 15}
    assert passport["components"]["ports"] == {
        "total": 2750,
        "top": [430, 360, 310, 230, 200, 150, 70, 70],
    }
    assert len(passport["components"]["hubs"]) == 19
    assert passport["components"]["hubs"][:8] == passport["components"]["ports"]["top"]
    assert passport["components"]["unconnected"] == {"total": 290, "max_on_one": 140}
    assert passport["components"]["runnables"] == 520
    assert passport["hardware"] == {
        "cores": 1,
        "partitions": 1,
        "memories": 0,
        "rated_partitions": 0,
    }
    assert passport["identifiers"]["hardware"] == {"max_length": 10, "mean_length": 6.0}
    assert passport["identifiers"]["base_types"]["mean_length"] == 6.417  # 77 / 12 for 6.4
    assert passport["identifiers"]["compositions"]["mean_length"] == 17.625  # 282 / 16 for 17.6


#: The five type layers, neutral at 0.
NEUTRAL_TYPE_LAYERS = ("application_types", "mapping_sets", "units", "compu_methods", "data_constraints")


def _delegation_sources(project: Project) -> dict[str, int]:
    """The built delegations by source.

    A reach delegates a port no connector reaches, a chain step a nested composition's boundary
    port, a repeat a port a connector reaches.
    """
    connected = {
        (end.swc, end.port) for connector in project.connectors for end in (connector.source, connector.target)
    }
    sources = {"reach": 0, "step": 0, "repeat": 0}
    for composition in project.compositions:
        types = {member.name: member.type for member in composition.members}
        for delegation in composition.delegations:
            component = types[delegation.member]
            if component is None:
                sources["step"] += 1
            elif (component, delegation.member_port) in connected:
                sources["repeat"] += 1
            else:
                sources["reach"] += 1
    return sources


def test_the_shapes_fixture_states_every_new_leaf_off_its_neutral_value() -> None:
    passport = load_specimen("shapes").passport
    layers, interfaces = passport["layers"], passport["interfaces"]
    assert layers["interfaces"] > interfaces["sender_receiver"] + interfaces["client_server"]
    assert layers["compositions"] == 3
    assert passport["connectors"]["delegation"] > 0
    assert all(layers[layer] > 0 for layer in NEUTRAL_TYPE_LAYERS)
    assert layers["units"] >= 1
    for layer in (*NEUTRAL_TYPE_LAYERS, "interfaces", "compositions"):
        assert passport["identifiers"][layer]["max_length"] > 0
        assert passport["identifiers"][layer]["mean_length"] > 0


def test_the_shapes_fixture_builds_every_new_shape() -> None:
    """Stubs reached by delegation, a chain of three, all three delegation sources, linear compu methods.

    The chain holds a component in the deepest composition.
    """
    project = build_project(load_specimen("shapes"))
    stubs = {i.name for i in project.interfaces if i.kind == "external_interface"}
    assert len(stubs) == 8
    delegated = {
        ({m.name: m.type for m in composition.members}[d.member], d.member_port)
        for composition in project.compositions
        for d in composition.delegations
    }
    stub_ports = {
        (component.name, port.name)
        for component in project.components
        for port in component.ports
        if port.interface in stubs
    }
    assert stub_ports & delegated and stub_ports - delegated
    root, middle, deepest = project.compositions
    assert root.root and not middle.root and not deepest.root
    assert any(member.type for member in deepest.members)
    assert all(value > 0 for value in _delegation_sources(project).values())
    assert sum(_delegation_sources(project).values()) == 160
    assert project.compu_methods and all(isinstance(m, LinearCompuMethod) for m in project.compu_methods)


def test_the_chain_empty_fixture_leaves_the_deepest_composition_empty() -> None:
    project = build_project(load_specimen("chain-empty"))
    root, deepest = project.compositions
    assert {member.type for member in root.members if member.type} == {
        component.name for component in project.components
    }
    assert [member.composition for member in root.members if member.composition] == [deepest.name]
    assert deepest.members == ()
    assert _delegation_sources(project) == {"reach": 0, "step": 0, "repeat": 1}


# --- the sweep: positive cases from witnesses -----------------------------------------------------

#: (seed, n): 42 witnesses of two to eight components, whose `top` states every port count,
#: and 18 of nine to sixteen, whose counts outside `top` are the placement's.
POSITIVE_CASES = [(seed, 2 + seed % 7) for seed in range(42)] + [(100 + seed, 9 + seed % 8) for seed in range(18)]


@pytest.mark.parametrize(("seed", "n"), POSITIVE_CASES)
def test_a_witnessed_passport_is_met(seed: int, n: int) -> None:
    passport = validate_passport(compute_passport(witness(seed, n)))
    preset = SpecimenPreset("SweepSpecimen", passport)
    try:
        files = build_specimen_files(preset)
    except PassportRefused as refused:
        pytest.fail(f"witness {seed} (n = {n}) refused: {refused.refusals}")
    sidecar = json.loads(files["generation.json"])
    assert strict(sidecar["passport"]) == strict(passport)


def test_the_witnesses_cover_the_shapes() -> None:
    """The sweep is not a set of trivial models.

    It holds hubs, links, unconnected ports, more than one connector per provide port and
    components with no port.
    """
    passports = [compute_passport(witness(seed, n)) for seed, n in POSITIVE_CASES]
    assert sum(1 for p in passports if p["components"]["hubs"]) >= 10
    assert sum(1 for p in passports if p["connectors"]["links"] > 0) >= 10
    assert sum(1 for p in passports if p["components"]["unconnected"]["total"] > 0) >= 30
    assert sum(1 for p in passports if p["hardware"]["rated_partitions"] > 0) >= 10
    assert max(p["layers"]["swc"] for p in passports) == 16
    assert len({strict(p) for p in passports}) == len(passports)


# --- the sweep over the new shapes: stubs, the chain, delegations, the type layers ----------------

#: (seed, n): 42 shaped witnesses of two to eight components and 18 of nine to sixteen.
SHAPED_CASES = [(500 + seed, 2 + seed % 7) for seed in range(42)] + [(600 + seed, 9 + seed % 8) for seed in range(18)]

#: The shaped witnesses the preferred candidate (the ascending layout on the connector search's first
#: placement at the top R_c) does not build, so the entry planner lays them out. Every other shaped
#: witness with delegations keeps the preferred candidate.
SOLVER_WITNESSES = {
    501: 3,
    502: 4,
    509: 4,
    517: 5,
    518: 6,
    529: 3,
    537: 4,
    538: 5,
    605: 14,
    606: 15,
    608: 9,
    617: 10,
}

#: The canonical layouts the relaxed program gives, recorded on macOS arm64: step 2 builds on exactly
#: these (c, u).
CANONICAL = ROOT / "tests/fixtures/layout/canonical.json"


def _shaped_passport(seed: int, n: int) -> dict[str, Any]:
    return validate_passport(compute_passport(shaped_witness(seed, n)))


@pytest.mark.parametrize(("seed", "n"), SHAPED_CASES)
def test_a_shaped_witness_passport_is_met(seed: int, n: int) -> None:
    passport = _shaped_passport(seed, n)
    preset = SpecimenPreset("SweepSpecimen", passport)
    try:
        files = build_specimen_files(preset)
    except PassportRefused as refused_:
        pytest.fail(f"shaped witness {seed} (n = {n}) refused: {refused_.refusals}")
    sidecar = json.loads(files["generation.json"])
    assert strict(sidecar["passport"]) == strict(passport)


def test_the_planner_lays_out_exactly_the_pinned_witnesses_canonically() -> None:
    """The sweep's sources: the layouts the solver builds are exactly `SOLVER_WITNESSES`'.

    Shown by seed in the failure message, each on the canonical (c, u) recorded for it; every
    other witness with delegations keeps the preferred candidate, and those without have none.
    """
    recorded_layouts = {
        case["name"]: case["canonical"] for case in json.loads(CANONICAL.read_text(encoding="utf-8"))["cases"]
    }
    solver = {}
    for seed, n in SHAPED_CASES:
        passport = _shaped_passport(seed, n)
        shape = specimen_shape(stated_leaves(passport))
        assert isinstance(shape, Shape), (seed, n, shape)
        if not passport["connectors"]["delegation"]:
            assert shape.layout.source == "none", seed
            continue
        if shape.layout.source == "solver":
            solver[seed] = n
            pinned = recorded_layouts[f"seed {seed}"]
            assert shape.layout.connectors == pinned["connectors"], seed
            assert list(shape.layout.connected) == pinned["connected"], seed
            assert list(shape.layout.unconnected) == pinned["unconnected"], seed
        else:
            assert shape.layout.source == "preferred", seed
    assert solver == SOLVER_WITNESSES, f"solver-built: {len(solver)}: {solver}"


@pytest.mark.parametrize("name", SPECIMENS)
def test_every_recorded_fixture_keeps_the_preferred_candidate(name: str) -> None:
    passport = load_specimen(name).passport
    shape = specimen_shape(stated_leaves(passport))
    assert isinstance(shape, Shape)
    expected = "preferred" if passport["connectors"]["delegation"] else "none"
    assert shape.layout.source == expected


#: The topology variant with the total unstated: the total the derivation lays out first, and
#: its layout's source.
TOPOLOGY_UNSTATED_TOTAL = (281, "solver")


def _topology_variant() -> dict[str, Any]:
    passport = copy.deepcopy(load_specimen("topology").passport)
    passport["connectors"]["delegation"] = 30
    passport["layers"]["compositions"] = 2
    passport["components"]["ports"]["total"] = 289
    return passport


def test_the_topology_preset_with_thirty_delegations_in_two_compositions_is_met() -> None:
    """R = 277, A = 250, D = 30 in two compositions: the planner lays it out.

    The repeats give at most 27, so 3 delegations need a connector-free component, which the
    preferred candidate finds only among the hubs the stars need.
    """
    passport = _topology_variant()
    files = build_specimen_files(SpecimenPreset(DEFAULT_NAME, passport))
    sidecar = json.loads(files["generation.json"])
    assert strict(sidecar["passport"]) == strict(validate_passport(passport))
    shape = specimen_shape(stated_leaves(passport))
    assert isinstance(shape, Shape) and shape.layout.source == "solver"


def test_the_topology_variant_with_the_total_unstated_is_met() -> None:
    """The same with `components.ports.total` unstated: the derivation's first total is laid out."""
    passport = _topology_variant()
    del passport["components"]["ports"]["total"]
    files = build_specimen_files(SpecimenPreset(DEFAULT_NAME, passport))
    generated = json.loads(files["generation.json"])["passport"]
    for leaf, value in stated_leaves(passport).items():
        assert stated_leaves(generated)[leaf] == value, leaf
    shape = specimen_shape(stated_leaves(passport))
    assert isinstance(shape, Shape)
    assert (sum(shape.counts), shape.layout.source) == TOPOLOGY_UNSTATED_TOTAL


#: The planner's last step, the exact program, reached on no case measured here.
EXACT_STEP = 3


@pytest.mark.parametrize("name", SPECIMENS)
def test_no_recorded_fixture_reaches_the_exact_program(name: str) -> None:
    shape = specimen_shape(stated_leaves(load_specimen(name).passport))
    assert isinstance(shape, Shape)
    assert shape.layout.step < EXACT_STEP


def test_no_shaped_witness_and_no_topology_variant_reaches_the_exact_program() -> None:
    """Every layout of the shaped witnesses is the preferred candidate's, D = 0's or step 2's.

    The 60 shaped witnesses and the topology variant, total stated and unstated (shown by case in
    the failure message).
    """
    passports = {f"seed {seed}": _shaped_passport(seed, n) for seed, n in SHAPED_CASES}
    passports["topology variant"] = _topology_variant()
    unstated = _topology_variant()
    del unstated["components"]["ports"]["total"]
    passports["topology variant, total unstated"] = unstated
    steps = {}
    for case, passport in passports.items():
        shape = specimen_shape(stated_leaves(validate_passport(passport)))
        assert isinstance(shape, Shape), (case, shape)
        steps[case] = shape.layout.step
    assert len(steps) == 62
    assert {case: step for case, step in steps.items() if step >= EXACT_STEP} == {}
    assert set(steps.values()) == {0, 2}


def test_the_shaped_witnesses_cover_the_new_shapes() -> None:
    """Stubs, chains, delegations and the type layers in most witnesses.

    Every composition count of 1..4, and passports pairwise distinct.
    """
    passports = [compute_passport(shaped_witness(seed, n)) for seed, n in SHAPED_CASES]
    declared = [p["interfaces"]["sender_receiver"] + p["interfaces"]["client_server"] for p in passports]
    assert sum(1 for p, i in zip(passports, declared) if p["layers"]["interfaces"] > i) == 54
    assert sum(1 for p in passports if p["layers"]["compositions"] > 1) == 45
    assert {p["layers"]["compositions"] for p in passports} == {1, 2, 3, 4}
    assert sum(1 for p in passports if p["connectors"]["delegation"] > 0) == 57
    assert sum(1 for p in passports if p["layers"]["units"] > 0) == 48
    assert sum(1 for p in passports if p["layers"]["application_types"] > 0) >= 40
    assert len({strict(p) for p in passports}) == len(passports)


# --- the sweep: negative cases, one per inequality -----------------------------------------------

Mutation = Callable[[dict[str, Any]], None]


def minimal() -> dict[str, Any]:
    return copy.deepcopy(load_specimen("minimal").passport)


def _set(**changes: Any) -> Mutation:
    """A mutation setting dotted leaves (`components__hubs` for `components.hubs`)."""

    def apply(passport: dict[str, Any]) -> None:
        for key, value in changes.items():
            *parents, leaf = key.split("__")
            node = passport
            for parent in parents:
                node = node.setdefault(parent, {})
            node[leaf] = value

    return apply


def _unset_identifiers(*layers: str) -> Mutation:
    """A mutation removing the identifier leaves of the given layers."""

    def apply(passport: dict[str, Any]) -> None:
        for layer in layers:
            del passport["identifiers"][layer]

    return apply


def _both(first: Mutation, second: Mutation) -> Mutation:
    """Two mutations, one after the other."""

    def apply(passport: dict[str, Any]) -> None:
        first(passport)
        second(passport)

    return apply


def _small(n: int, top: list[int], hubs: list[int], fan_out: list[int], **more: Any) -> Mutation:
    """The minimal passport reshaped to n components, its port and fan-out leaves restated."""

    def apply(passport: dict[str, Any]) -> None:
        interfaces = max(1, len(fan_out))
        _set(
            layers__swc=n,
            layers__interfaces=interfaces,
            components__ports={"total": sum(top), "top": top},
            components__hubs=hubs,
            components__runnables=n,
            interfaces__sender_receiver=interfaces,
            interfaces__fan_out={"max": fan_out[0] if fan_out else 0, "top": fan_out},
            **more,
        )(passport)
        for layer in ("swc", "interfaces", "connectors"):
            del passport["identifiers"][layer]

    return apply


NEGATIVE_CASES: list[tuple[str, Callable[[], dict[str, Any]], Mutation, tuple[str, str]]] = [
    # The port distribution.
    (
        "top not descending",
        topology,
        _set(components__ports={"total": 400, "top": [28, 30, 26, 24, 22, 20, 20, 20]}),
        ("components.ports.top", "order"),
    ),
    (
        "len(top) != min(8, n)",
        topology,
        _set(components__ports={"total": 400, "top": [30, 28, 26, 24, 22, 20, 20]}),
        ("components.ports.top", "order"),
    ),
    (
        "hubs not descending",
        topology,
        _set(components__hubs=[28, 30, 26, 24, 22, 20, 20, 20, 18, 18]),
        ("components.hubs", "order"),
    ),
    (
        "hubs[:8] not a prefix of top",
        topology,
        _set(components__hubs=[30, 28, 26, 24, 22, 20, 20, 19, 18, 18]),
        ("components.hubs", "order"),
    ),
    (
        "sum of declared levels above n",
        topology,
        _set(components__by_declared_level={"QM": 28, "A": 3, "B": 0, "C": 0, "D": 0}),
        ("components.by_declared_level.A", "sum"),
    ),
    ("k >= n / 2", minimal, _set(components__hubs=[1]), ("components.hubs", "hub-rule")),
    (
        "no median separates the hubs (even n)",
        minimal,
        _small(6, [10, 7, 6, 1, 0, 0], [10, 7], [24]),
        ("components.hubs", "hub-rule"),
    ),
    (
        "free sum above its range",
        topology,
        _set(components__ports={"total": 1000, "top": [30, 28, 26, 24, 22, 20, 20, 20]}),
        ("components.ports.total", "port-total"),
    ),
    (
        "free sum below zero",
        topology,
        _set(components__ports={"total": 220, "top": [30, 28, 26, 24, 22, 20, 20, 20]}),
        ("components.ports.total", "port-total"),
    ),
    # Interfaces, connectors, links and unconnected ports.
    (
        "assembly != layers.connectors",
        topology,
        _set(layers__connectors=251),
        ("connectors.assembly", "sum"),
    ),
    (
        "A > R - 1",
        topology,
        _set(layers__connectors=388, connectors__assembly=388),
        ("connectors.assembly", "connectors"),
    ),
    (
        "A < ceil(R / 2)",
        topology,
        _set(layers__connectors=150, connectors__assembly=150),
        ("connectors.assembly", "connectors"),
    ),
    (
        "A > sum(f - 1)",
        minimal,
        _small(2, [2, 2], [], [2, 2], layers__connectors=3, connectors__assembly=3),
        ("connectors.assembly", "connectors"),
    ),
    (
        "u1 > U",
        topology,
        _set(components__unconnected={"total": 12, "max_on_one": 13}),
        ("components.unconnected.max_on_one", "unconnected"),
    ),
    (
        "u1 above the largest component",
        topology,
        _set(components__unconnected={"total": 40, "max_on_one": 31}),
        ("components.unconnected.max_on_one", "unconnected"),
    ),
    (
        "U > sum(min(p, u1))",
        topology,
        _set(components__unconnected={"total": 31, "max_on_one": 1}),
        ("components.unconnected.max_on_one", "unconnected"),
    ),
    (
        "u1 = 0 with U > 0",
        topology,
        _set(components__unconnected={"total": 12, "max_on_one": 0}),
        ("components.unconnected.max_on_one", "unconnected"),
    ),
    (
        "fan-out-1 interfaces beyond U",
        minimal,
        _small(2, [1, 1], [], [1, 1], connectors__assembly=0, layers__connectors=0),
        ("interfaces.fan_out.top", "fan-out"),
    ),
    (
        "sum of fan-outs != P",
        topology,
        _set(
            layers__interfaces=8,
            interfaces__sender_receiver=6,
            interfaces__client_server=2,
        ),
        ("interfaces.fan_out.top", "fan-out"),
    ),
    (
        "fan_out.max != top[0]",
        topology,
        _set(interfaces__fan_out={"max": 61, "top": [60, 50, 40, 30, 30, 25, 25, 20]}),
        ("interfaces.fan_out.max", "order"),
    ),
    (
        "k = 0, L = 0 and A > m // 2",
        minimal,
        _small(4, [2, 2, 2, 2], [], [8], layers__connectors=4, connectors__assembly=4),
        ("connectors.links", "links"),
    ),
    (
        "L > 0 and m // 2 + L above what the ordinary components can join",
        topology,
        _set(connectors__links=200),
        ("connectors.links", "links"),
    ),
    (
        "layers.interfaces below SR + CS",
        topology,
        _set(layers__interfaces=15),
        ("layers.interfaces", "layer-total"),
    ),
    (
        "no value type for the interfaces",
        topology,
        _set(layers__implementation_types=0),
        ("layers.implementation_types", "layer-total"),
    ),
    (
        "no base type for the value types",
        topology,
        _set(layers__base_types=0),
        ("layers.base_types", "layer-total"),
    ),
    # Hardware.
    (
        "no core",
        topology,
        _set(
            layers__hardware=3,
            hardware={"cores": 0, "partitions": 0, "memories": 2, "rated_partitions": 0},
        ),
        ("hardware.cores", "hardware-topology"),
    ),
    (
        "partitions with no core",
        topology,
        _set(
            layers__hardware=7,
            hardware={"cores": 0, "partitions": 4, "memories": 2, "rated_partitions": 0},
        ),
        ("hardware.partitions", "hardware-topology"),
    ),
    (
        "rated above partitions",
        topology,
        _set(hardware={"cores": 2, "partitions": 4, "memories": 2, "rated_partitions": 5}),
        ("hardware.rated_partitions", "hardware-topology"),
    ),
    (
        "layers.hardware != 1 + cores + partitions + memories",
        topology,
        _set(layers__hardware=10),
        ("layers.hardware", "hardware-topology"),
    ),
    ("layers.hardware = 0", topology, _set(layers__hardware=0), ("layers.hardware", "later-shape")),
    # Identifiers.
    (
        "an unreachable mean",
        topology,
        _set(identifiers__swc={"max_length": 24, "mean_length": 15.51}),
        ("identifiers.swc.mean_length", "identifier-mean"),
    ),
    (
        "a component name above its limit of 111",
        topology,
        _set(identifiers__max_length=112, identifiers__swc={"max_length": 112, "mean_length": 15.5}),
        ("identifiers.swc.max_length", "identifier-length"),
    ),
    (
        "the overall maximum below a stated layer's",
        topology,
        _set(identifiers__max_length=29),
        ("identifiers.max_length", "identifier-length"),
    ),
    (
        "the overall maximum with no unstated layer to carry it",
        topology,
        _set(identifiers__max_length=31),
        ("identifiers.max_length", "identifier-length"),
    ),
    (
        "n_L = 1 with the mean other than the maximum",
        topology,
        _set(identifiers__compositions={"max_length": 12, "mean_length": 11.0}),
        ("identifiers.compositions.mean_length", "identifier-mean"),
    ),
    # The chain and the delegations.
    (
        "no root composition",
        topology,
        _set(layers__compositions=0),
        ("layers.compositions", "layer-total"),
    ),
    (
        "delegations beyond what the reached ports and compositions admit",
        topology,
        _set(connectors__delegation=5000),
        ("connectors.delegation", "delegations"),
    ),
    # The version.
    (
        "another contract major",
        topology,
        _set(contract_version="11.0.0"),
        ("contract_version", "version"),
    ),
]


#: The acceptance vectors of leaves now `met`: each mutation of a fixture is built and self-checked.
ACCEPTANCE_CASES = [
    (
        # One external interface stub beyond SR + CS; 17 names at a mean of 12.0.
        "layers.interfaces above SR + CS",
        topology,
        _set(
            layers__interfaces=17,
            identifiers__interfaces={"max_length": 30, "mean_length": 12.0},
        ),
    ),
    (
        # The five type layers off zero, their identifier leaves unstated.
        "the type layers off zero",
        topology,
        _both(
            _set(
                layers__application_types=5,
                layers__units=2,
                layers__compu_methods=3,
                layers__data_constraints=2,
                layers__mapping_sets=1,
            ),
            _unset_identifiers("application_types", "units", "compu_methods", "data_constraints", "mapping_sets"),
        ),
    ),
]


@pytest.mark.parametrize(("name", "base", "mutate"), ACCEPTANCE_CASES, ids=[case[0] for case in ACCEPTANCE_CASES])
def test_a_leaf_turned_met_is_built(name: str, base: Callable[[], dict[str, Any]], mutate: Mutation) -> None:
    passport = base()
    mutate(passport)
    passport = validate_passport(passport)
    assert check_feasibility(passport) == []
    files = build_specimen_files(SpecimenPreset(DEFAULT_NAME, passport))
    generated = json.loads(files["generation.json"])["passport"]
    assert self_check(passport, generated) == []


#: The other refusals a negative case's mutation brings: a `top` out of order no longer has the
#: hubs as its prefix; a connector count beyond the fan-out also leaves too few for the links;
#: partitions with no core are refused at both leaves.
ALSO_REFUSED: dict[str, list[tuple[str, str]]] = {
    "top not descending": [("components.hubs", "order")],
    "len(top) != min(8, n)": [("components.hubs", "order")],
    "A > sum(f - 1)": [("connectors.links", "links")],
    "partitions with no core": [("hardware.cores", "hardware-topology")],
}


@pytest.mark.parametrize(
    ("name", "base", "mutate", "expected"),
    NEGATIVE_CASES,
    ids=[case[0] for case in NEGATIVE_CASES],
)
def test_an_inequality_is_refused_with_its_class(
    name: str, base: Callable[[], dict[str, Any]], mutate: Mutation, expected: tuple[str, str]
) -> None:
    """The whole ordered list of refusals is pinned.

    The expected one, and the few cases whose mutation also breaks another stated leaf
    (`ALSO_REFUSED`) with it, in leaf order.
    """
    passport = base()
    assert check_feasibility(passport) == []
    mutate(passport)
    refusals = [(r.leaf, r.refusal_class) for r in check_feasibility(validate_passport(passport))]
    assert refusals == sorted([expected, *ALSO_REFUSED.get(name, [])], key=lambda item: list(LEAVES).index(item[0]))


# --- partial passports: optional leaves dropped from a witnessed model's passport ---------------

#: The optional leaf groups of the schema the sweep drops, one group at a time (every per-layer
#: identifier leaf at once, and the two connector counts together, which leaves the count to the
#: construction): a model meeting every remaining leaf exists, the witness itself.
OPTIONAL_GROUPS: list[tuple[tuple[str, ...], ...]] = [
    (("components", "ports"),),
    (("components", "ports", "total"),),
    (("components", "ports", "top"),),
    (("components", "by_declared_level"),),
    (("components", "runnables"),),
    (("connectors", "assembly"),),
    (("layers", "connectors"),),
    (("connectors", "assembly"), ("layers", "connectors")),
    (("layers", "hardware"),),
    (("interfaces", "fan_out", "top"),),
    (("identifiers",),),
]

#: Witnesses whose partial passports the sweep builds: small ones, and ones beyond eight
#: components whose counts outside `top` are free.
PARTIAL_CASES = [(300 + seed, 2 + seed % 7) for seed in range(14)] + [(400 + seed, 9 + seed % 8) for seed in range(8)]


def _dropped(passport: dict[str, Any], group: tuple[tuple[str, ...], ...]) -> dict[str, Any]:
    partial = copy.deepcopy(passport)
    for path in group:
        if path == ("identifiers",):
            partial["identifiers"] = {"max_length": partial["identifiers"]["max_length"]}
            continue
        node = partial
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    return partial


def _named(group: tuple[tuple[str, ...], ...]) -> str:
    return " and ".join(".".join(path) for path in group)


@pytest.mark.parametrize(("seed", "n"), PARTIAL_CASES)
def test_a_partial_passport_of_a_witness_is_met(seed: int, n: int) -> None:
    """Dropping optional leaves from a witnessed model's passport leaves a passport some model meets.

    The specimen meets every leaf still stated (the self-check), and derives the rest.
    """
    full = validate_passport(compute_passport(witness(seed, n)))
    for group in OPTIONAL_GROUPS:
        partial = validate_passport(_dropped(full, group))
        try:
            files = build_specimen_files(SpecimenPreset("PartialSpecimen", partial))
        except PassportRefused as refused:
            pytest.fail(f"witness {seed} (n = {n}) without {_named(group)}: {refused}")
        generated = stated_leaves(json.loads(files["generation.json"])["passport"])
        for path, value in stated_leaves(partial).items():
            if LEAVES[path].leaf_class != VERSION:
                assert generated[path] == value, (group, path)


#: The optional leaves the random sweep drops, each with probability 0.4: the leaf groups above,
#: one by one, and the compositions count; every per-layer identifier leaf with 0.35, one by one.
RANDOM_DROPS: list[tuple[str, ...]] = [
    ("components", "ports", "total"),
    ("components", "ports", "top"),
    ("components", "runnables"),
    ("connectors", "assembly"),
    ("layers", "connectors"),
    ("layers", "hardware"),
    ("interfaces", "fan_out", "top"),
    ("layers", "compositions"),
]


def _randomly_dropped(full: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    partial = copy.deepcopy(full)
    for path in RANDOM_DROPS:
        node = partial
        for key in path[:-1]:
            node = node.get(key, {})
        if path[-1] in node and rng.random() < 0.4:
            del node[path[-1]]
    if not partial["components"].get("ports", True):
        del partial["components"]["ports"]
    identifiers = partial["identifiers"]
    for layer in [layer for layer in identifiers if layer != "max_length"]:
        for leaf in ("max_length", "mean_length"):
            if leaf in identifiers[layer] and rng.random() < 0.35:
                del identifiers[layer][leaf]
        if not identifiers[layer]:
            del identifiers[layer]
    return partial


def test_random_partial_passports_of_witnesses_are_never_refused_as_a_stated_inequality() -> None:
    """Several optional leaves dropped at once: a witness meets every leaf still stated.

    Single identifier leaves are among them, so no stated inequality can refuse its partial
    passport. A refusal is `derived` only (the shape's documented limit, about 1 in 100), and a
    passport the check accepts is built with every stated leaf met.
    """
    refusals: list[str] = []
    derived = 0
    for seed in range(40):
        n = 2 + seed % 15
        rng = random.Random(seed * 7 + 1)
        full = compute_passport(witness(seed, n))
        for _ in range(3):
            partial = validate_passport(_randomly_dropped(full, rng))
            refused = check_feasibility(partial)
            refusals.extend(f"witness {seed}: {refusal}" for refusal in refused if refusal.refusal_class != "derived")
            if refused:
                derived += 1
                continue
            files = build_specimen_files(SpecimenPreset("PartialSpecimen", partial))
            generated = stated_leaves(json.loads(files["generation.json"])["passport"])
            for path, value in stated_leaves(partial).items():
                if LEAVES[path].leaf_class != VERSION:
                    assert generated[path] == value, (seed, path)
    assert refusals == []
    assert derived <= 6
