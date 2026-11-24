"""The specimen tests' shared helpers: the neutral passport, the fixtures, the witnesses.

The witnesses are independent models built directly in the connection model, as the documents the
passport pass reads; the partitions are the exhaustive oracles' enumeration of port counts; the
port-level enumeration of every connection structure is the oracle of the connector search
(`tests/test_specimen_connectors.py`) and, with every split of the open ports and the delegation
range, of the entry planner's programs (`tests/test_specimen_layout.py`).
"""

from __future__ import annotations

import copy
import functools
import itertools
import json
import multiprocessing
import os
import random
import statistics
import string
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from swcraft_specimens.packaged import preset_path as packaged_preset
from swcraft_specimens.passport import PASSPORT_LAYERS, Refusal, compute_passport
from swcraft_specimens.preset import load_any_preset
from swcraft_specimens.specimen import connectors as connectors_module
from swcraft_specimens.specimen import feasibility
from swcraft_specimens.specimen import shape as shape_module
from swcraft_specimens.specimen.build import RenderedSpecimen, build_specimen_files, render_specimen
from swcraft_specimens.specimen.components import place_ports, port_spec
from swcraft_specimens.specimen.compositions import composition_count
from swcraft_specimens.specimen.connectors import connector_spec
from swcraft_specimens.specimen.delegations import delegation_count
from swcraft_specimens.specimen.feasibility import check_feasibility
from swcraft_specimens.specimen.interfaces import connectable_fan_outs
from swcraft_specimens.specimen.layout import (
    Configuration,
    SolverOptions,
    Tally,
    _Sequence,
    exact_program,
    layout_bounds,
    relaxed_program,
    solve,
    star_search,
)
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.specimen.requirements import REQUIREMENT_TOPICS, sentence

ROOT = Path(__file__).parents[2]


def configuration_of(stated: dict[str, Any], counts: Sequence[int], fan_outs: Sequence[int]) -> Configuration:
    """The planner's configuration of a stated passport at these counts and fan-outs.

    The fan-outs are every named interface's, the stubs' included, as the shape maps them
    (`Configuration.of_spec`).
    """
    return Configuration.of_spec(
        connector_spec(stated),
        counts,
        connectable_fan_outs(stated, fan_outs),
        delegation_count(stated),
        composition_count(stated),
    )


LAYERS = (
    "swc",
    "interfaces",
    "connectors",
    "application_types",
    "implementation_types",
    "base_types",
    "requirements",
    "compositions",
    "tasks",
    "sequences",
    "hardware",
    "mapping_sets",
    "units",
    "compu_methods",
    "data_constraints",
)
MET_IDENTIFIER_LAYERS = (
    "swc",
    "interfaces",
    "connectors",
    "implementation_types",
    "base_types",
    "hardware",
    "compositions",
    # The five type layers.
    "application_types",
    "mapping_sets",
    "units",
    "compu_methods",
    "data_constraints",
    # The task and sequence layers.
    "tasks",
    "sequences",
    # The requirement layer.
    "requirements",
)


def neutral_passport() -> dict[str, Any]:
    """A passport whose leaves sit at their neutral values (two components, one link)."""
    identifiers: dict[str, Any] = {"max_length": 8}
    for layer in LAYERS:
        identifiers[layer] = {"max_length": 0, "mean_length": 0}
    for layer, length in (
        ("swc", 7),
        ("interfaces", 6),
        ("connectors", 8),
        ("implementation_types", 6),
        ("base_types", 6),
        ("compositions", 4),
    ):
        identifiers[layer] = {"max_length": length, "mean_length": float(length)}
    # Cpu0001 and Core0001.
    identifiers["hardware"] = {"max_length": 8, "mean_length": 7.5}
    return {
        "passport_version": "1.0.0",
        "contract_version": "12.0.0",
        "layers": {
            "swc": 2,
            "interfaces": 1,
            "connectors": 1,
            "application_types": 0,
            "implementation_types": 1,
            "base_types": 1,
            "requirements": 0,
            "compositions": 1,
            "tasks": 0,
            "sequences": 0,
            "hardware": 2,
            "mapping_sets": 0,
            "units": 0,
            "compu_methods": 0,
            "data_constraints": 0,
        },
        "components": {
            "ports": {"total": 2, "top": [1, 1]},
            "hubs": [],
            "unconnected": {"total": 0, "max_on_one": 0},
            "by_declared_level": {"QM": 0, "A": 0, "B": 0, "C": 0, "D": 0},
            "runnables": 2,
        },
        "interfaces": {
            "sender_receiver": 1,
            "client_server": 0,
            "fan_out": {"max": 2, "top": [2]},
        },
        "connectors": {"assembly": 1, "delegation": 0, "links": 0},
        "hardware": {"cores": 1, "partitions": 0, "memories": 0, "rated_partitions": 0},
        "tasks": {"idle": 0},
        "requirements": {
            "on_all_site_kinds": 0,
            "on_one_port": 0,
            "unallocated": 0,
            "stale_links": 0,
            "links": 0,
        },
        "identifiers": identifiers,
    }


def write_json(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8", newline="\n")
    return path


def preset_path(name: str) -> Path:
    return packaged_preset(name)


def load_specimen(name: str) -> SpecimenPreset:
    preset = load_any_preset(preset_path(name))
    assert isinstance(preset, SpecimenPreset)
    return preset


def no_components(passport: dict[str, Any], *, runnables: bool = False) -> dict[str, Any]:
    """The passport with no component, port, interface or connector.

    With `runnables`, its `components.runnables` is stated 0 too.
    """
    passport["layers"].update(swc=0, interfaces=0, connectors=0, implementation_types=0, base_types=0)
    passport["components"]["ports"] = {"total": 0, "top": []}
    if runnables:
        passport["components"]["runnables"] = 0
    passport["interfaces"] = {
        "sender_receiver": 0,
        "client_server": 0,
        "fan_out": {"max": 0, "top": []},
    }
    passport["connectors"] = {"assembly": 0, "delegation": 0, "links": 0}
    for layer in ("swc", "interfaces", "connectors", "implementation_types", "base_types"):
        passport["identifiers"][layer] = {"max_length": 0, "mean_length": 0}
    return passport


def built_specimen(passport: dict[str, Any]) -> RenderedSpecimen:
    """A feasible passport's specimen, rendered for its project and the pass's passport.

    It goes through the full generation, its self-check and its whole check
    (`build_specimen_files`) first.
    """
    assert check_feasibility(passport) == []
    preset = SpecimenPreset(DEFAULT_NAME, passport)
    assert "generation.json" in build_specimen_files(preset)
    return render_specimen(preset)


#: The words every case's description sentence starts with, taken from the product's own
#: `requirements.sentence` (case 0's sentence less its topic).
SENTENCE_START = sentence(0).removesuffix(f"{REQUIREMENT_TOPICS[0][0].lower()}.")


def sentences_built(project: Any) -> int:
    """The description sentences a built project's components carry."""
    return sum(component.description.count(SENTENCE_START) for component in project.components)


def topology() -> dict[str, Any]:
    return copy.deepcopy(load_specimen("topology").passport)


Documents = dict[str, list[dict[str, Any]]]

#: The witnesses' name stems: those of the specimen builder, so a witness's names are never
#: shorter than the specimen element that has to carry the same length.
INDEX = 4


def name_of(rng: random.Random, stem: str, index: int | None) -> str:
    base = stem if index is None else f"{stem}{index:0{INDEX}d}"
    return base + "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(0, 5)))


def counts_from_placement(counts: list[int]) -> list[int]:
    """The port counts the placement gives for the passport leaves of `counts` (n > 8)."""
    passport = compute_passport(
        {
            layer: (
                [{"name": f"C{i}", "ports": [{"name": f"P{k}"} for k in range(c)]} for i, c in enumerate(counts)]
                if layer == "swc"
                else []
            )
            for layer in PASSPORT_LAYERS
        }
    )
    stated = {
        "layers.swc": len(counts),
        "components.ports.top": passport["components"]["ports"]["top"],
        "components.hubs": passport["components"]["hubs"],
        "components.ports.total": passport["components"]["ports"]["total"],
    }
    return list(place_ports(port_spec(stated)))


def witness(seed: int, n: int) -> Documents:
    """An independent model in the connection model, as the documents the pass reads."""
    rng = random.Random(seed)
    counts = [rng.randint(0, 6) for _ in range(n)]
    if rng.random() < 0.4:
        counts[0] = rng.randint(7, 14)  # most often a hub
    if sum(counts) < 2:
        counts[0] += 2
    if n > 8:
        counts = counts_from_placement(counts)
    total = sum(counts)
    swc_names = [name_of(rng, "Swc", i + 1) for i in range(n)]
    named = rng.randint(1, min(8, total))
    declared = named + rng.randint(0, 2)
    interface_names = [name_of(rng, "If", i + 1) for i in range(declared)]
    free = list(counts)
    ports: list[list[dict[str, Any]]] = [[] for _ in range(n)]
    connectors: list[dict[str, Any]] = []

    def take(component: int, interface: int) -> str:
        free[component] -= 1
        port = f"P{len(ports[component]) + 1}"
        ports[component].append({"name": port, "interface": interface_names[interface]})
        return port

    while any(free):
        interface = rng.randrange(named)
        open_components = [j for j in range(n) if free[j] > 0]
        if len(open_components) >= 2 and rng.random() < 0.75:
            # A star: one provide port, require ports on other components, one connector each.
            provider = rng.choice(open_components)
            others = [j for j in open_components if j != provider]
            requirers = rng.sample(others, rng.randint(1, min(3, len(others))))
            provide_port = take(provider, interface)
            for requirer in requirers:
                require_port = take(requirer, interface)
                connectors.append(
                    {
                        "name": name_of(rng, "Conn", len(connectors) + 1),
                        "from": {"swc": swc_names[provider], "port": provide_port},
                        "to": {"swc": swc_names[requirer], "port": require_port},
                    }
                )
        else:
            take(rng.choice(open_components), interface)  # an unconnected port
    levels = [None, "QM", "A", "B", "C", "D"]
    swcs = []
    for index, name in enumerate(swc_names):
        document: dict[str, Any] = {
            "name": name,
            "ports": ports[index],
            "runnables": [{"name": f"R{k}"} for k in range(1, rng.randint(0, 3) + 1)],
        }
        level = rng.choice(levels)
        if level is not None:
            document["asil"] = level
        swcs.append(document)
    cores = rng.randint(1, 3)
    memories = rng.randint(0, 2)
    partitions = rng.randint(0, 4)
    rated = rng.randint(0, partitions)
    hardware: list[dict[str, Any]] = [{"name": name_of(rng, "Cpu", 1), "kind": "processor"}]
    hardware += [{"name": name_of(rng, "Core", i + 1), "kind": "core"} for i in range(cores)]
    hardware += [{"name": name_of(rng, "Mem", i + 1), "kind": "memory"} for i in range(memories)]
    for i in range(partitions):
        partition: dict[str, Any] = {"name": name_of(rng, "Part", i + 1), "kind": "partition"}
        if i < rated:
            partition["asil"] = "QM"
        hardware.append(partition)
    layers: Documents = {layer: [] for layer in PASSPORT_LAYERS}
    layers["swc"] = swcs
    layers["interfaces"] = [
        {"name": name, "kind": rng.choice(("sender_receiver", "client_server"))} for name in interface_names
    ]
    layers["connectors"] = connectors
    layers["implementation_types"] = [{"name": name_of(rng, "Vt", i + 1)} for i in range(rng.randint(1, 3))]
    layers["base_types"] = [{"name": name_of(rng, "Bt", i + 1)} for i in range(rng.randint(1, 3))]
    layers["compositions"] = [{"name": name_of(rng, "Root", None)}]
    layers["hardware"] = hardware
    return layers


#: The type layers a shaped witness declares, with their name stems (the specimen builder's).
TYPE_STEMS = (
    ("units", "Un"),
    ("compu_methods", "Cm"),
    ("data_constraints", "Dc"),
    ("application_types", "At"),
    ("mapping_sets", "Ms"),
)


def shaped_witness(seed: int, n: int) -> Documents:
    """A witness with the remaining shapes.

    External interface stubs, a chain of compositions with delegations of the three sources, and
    the five type layers.

    It extends `witness(seed, n)` independently of the specimen builder's choices:

    - **stubs:** some interfaces no connector end reaches become `external_interface` (a stub never
      carries a connector), and up to two unnamed stubs are declared;
    - **the chain:** C in 1..4 compositions `Root` > `Cmp0001` > ...; the components with a
      connector end are root members, the others members of the deepest composition, so the
      witness stays in the chain class the construction covers, but which components carry the
      connectors is the witness's own, not the ascending entry;
    - **delegations:** a port no connector reaches is reached by one delegation with probability
      1/2 (reach), a deep reach takes 0 to C - 1 chain steps up, and a star's provide port is
      delegated by the root with probability 1/3 (a repeat), each with a boundary port of its own;
    - **the type layers:** 0 to 4 elements each, named by the builder's stems (their content is
      not read by the pass).
    """
    rng = random.Random(seed * 31 + 7)
    layers = witness(seed, n)
    connected = {
        (end["swc"], end["port"]) for connector in layers["connectors"] for end in (connector["from"], connector["to"])
    }
    # Stubs: interfaces whose ports no connector reaches, each with probability 1/2.
    carrying = {
        port["interface"] for swc in layers["swc"] for port in swc["ports"] if (swc["name"], port["name"]) in connected
    }
    for interface in layers["interfaces"]:
        if interface["name"] not in carrying and rng.random() < 0.5:
            interface["kind"] = "external_interface"
    for index in range(rng.randint(0, 2)):
        layers["interfaces"].append({"name": name_of(rng, "IfX", index + 1), "kind": "external_interface"})
    count = rng.randint(1, 4)
    names = [name_of(rng, "Root", None)] + [name_of(rng, "Cmp", i + 1) for i in range(count - 1)]
    entered = {end[0] for end in connected}
    compositions: list[dict[str, Any]] = [
        {"name": name, "members": [], "ports": [], "delegations": []} for name in names
    ]
    member_of: dict[str, int] = {}
    for swc in layers["swc"]:
        depth = 0 if swc["name"] in entered or count == 1 else count - 1
        member_of[swc["name"]] = depth
        compositions[depth]["members"].append({"name": swc["name"], "type": swc["name"]})
    for depth in range(1, count):
        compositions[depth - 1]["members"].append({"name": names[depth], "composition": names[depth]})

    def delegate(depth: int, member: str, member_port: str, interface: str) -> str:
        composition = compositions[depth]
        port = f"BP{len(composition['ports']) + 1}"
        composition["ports"].append({"name": port, "interface": interface})
        composition["delegations"].append(
            {
                "name": f"Dlg{len(composition['delegations']) + 1}",
                "port": port,
                "member": member,
                "member_port": member_port,
            }
        )
        return port

    providers = {(c["from"]["swc"], c["from"]["port"]) for c in layers["connectors"]}
    for swc in layers["swc"]:
        for item in swc["ports"]:
            key = (swc["name"], item["name"])
            if key in providers and rng.random() < 1 / 3:
                delegate(0, swc["name"], item["name"], item["interface"])  # a repeat
            elif key not in connected and rng.random() < 0.5:
                depth = member_of[swc["name"]]
                port = delegate(depth, swc["name"], item["name"], item["interface"])  # reach
                for _ in range(rng.randint(0, depth)):  # chain steps, one parent at a time
                    port = delegate(depth - 1, names[depth], port, item["interface"])
                    depth -= 1
    layers["compositions"] = compositions
    for layer, stem in TYPE_STEMS:
        layers[layer] = [{"name": name_of(rng, stem, i + 1)} for i in range(rng.randint(0, 4))]
    return layers


def descending_partitions(total: int, most: int | None = None) -> list[tuple[int, ...]]:
    """Every descending list of positive integers summing to `total`."""
    if total == 0:
        return [()]
    most = total if most is None else most
    result: list[tuple[int, ...]] = []
    for first in range(min(total, most), 0, -1):
        result.extend((first, *rest) for rest in descending_partitions(total - first, first))
    return result


# --- the port-level enumeration (the oracle of the connector search and of the entry planner) ---


def oracle_hub_count(counts: Sequence[int]) -> int:
    """The hubs by the pass's definition: above twice the median of every component's count."""
    if not counts:
        return 0
    threshold = 2 * statistics.median(counts)
    return sum(1 for count in counts if count > threshold)


def packable(stars: Sequence[int], fan_outs: Sequence[int]) -> bool:
    """Whether the stars (ports each) fit into the interfaces, the rest unconnected."""
    ordered = sorted(stars, reverse=True)

    @functools.cache
    def fits(index: int, rooms: tuple[int, ...]) -> bool:
        if index == len(ordered):
            return True
        for room in sorted(set(rooms), reverse=True):
            if room >= ordered[index]:
                left = list(rooms)
                left.remove(room)
                left.append(room - ordered[index])
                if fits(index + 1, tuple(sorted(left))):
                    return True
        return False

    return fits(0, tuple(sorted(fan_outs)))


#: A connection structure: per component its ports a connector end reaches, the connectors, the
#: connectors between two ordinary components, and the star sizes (ports each), ascending.
Structure = tuple[tuple[int, ...], int, int, tuple[int, ...]]


@functools.cache
def connection_structures(counts: tuple[int, ...]) -> frozenset[Structure]:
    """Every connection structure on these components, port by port.

    A port is a provide port, a require port of one provide port on another component, or open
    (no connector end); every provide port has at least one require port.
    """
    owner = [component for component, count in enumerate(counts) for _ in range(count)]
    hubs = oracle_hub_count(counts)
    found: set[Structure] = set()
    for mask in range(1 << len(owner)):
        provide = [port for port in range(len(owner)) if mask >> port & 1]
        rest = [port for port in range(len(owner)) if not mask >> port & 1]
        choices = [[None, *(p for p in provide if owner[p] != owner[port])] for port in rest]
        for choice in itertools.product(*choices):
            served = dict.fromkeys(provide, 0)
            reached = [0] * len(counts)
            between = 0
            for port, chosen in zip(rest, choice, strict=True):
                if chosen is None:
                    continue
                served[chosen] += 1
                reached[owner[port]] += 1
                if owner[port] >= hubs and owner[chosen] >= hubs:
                    between += 1
            if any(count == 0 for count in served.values()):
                continue
            for port in provide:
                reached[owner[port]] += 1
            found.add(
                (
                    tuple(reached),
                    sum(served.values()),
                    between,
                    tuple(sorted(count + 1 for count in served.values())),
                )
            )
    return frozenset(found)


#: A case of the entry planner's oracle: (U, u1, A, L, C, D).
LayoutCase = tuple[int, int, int, int, int, int]


def layout_witnesses(
    counts: tuple[int, ...], fan_outs: tuple[int, ...], compositions: Sequence[int]
) -> frozenset[LayoutCase]:
    """Every case some layout has.

    A connection structure whose stars pack into the fan-outs, every split of each component's
    open ports into unconnected and delegation-only ones, and the delegation range over the chain:
    from R - R_c (at least 1) to R - A + (C - 1) x D_deep, D_deep the ports of the components no
    connector end reaches (members of the deepest composition).
    """
    ordinary = len(counts) - oracle_hub_count(counts)
    found: set[LayoutCase] = set()
    for reached, connectors, between, stars in connection_structures(counts):
        if not packable(stars, fan_outs):
            continue
        links = max(0, between - ordinary // 2)
        connected = sum(reached)
        opens = [count - r for count, r in zip(counts, reached, strict=True)]
        for split in itertools.product(*(range(o + 1) for o in opens)):
            total = sum(split)
            on_one = max(split, default=0)
            reach = sum(counts) - total
            deep = sum(o - u for o, u, r in zip(opens, split, reached, strict=True) if r == 0)
            for c in compositions:
                most = reach - connectors + (c - 1) * deep
                for d in range(max(1, reach - connected), most + 1):
                    found.add((total, on_one, connectors, links, c, d))
    return frozenset(found)


def layout_box(max_ports: int) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """The oracle's configurations: every partition of the counts and of the fan-outs.

    2 to 4 components of 2 to `max_ports` ports in all.
    """
    return [
        (counts, fan_outs)
        for ports in range(2, max_ports + 1)
        for counts in descending_partitions(ports)
        if 2 <= len(counts) <= 4
        for fan_outs in descending_partitions(ports)
    ]


BOX_COMPOSITIONS = (1, 3)


def layout_box_cases(counts: tuple[int, ...]) -> Iterator[LayoutCase]:
    """Every case of one configuration.

    Every U and u1, A below P, L up to 2, C of `BOX_COMPOSITIONS` and D from 1 to C x R + 1.
    """
    ports = sum(counts)
    for total in range(ports + 1):
        for on_one in range(0 if total == 0 else 1, total + 1):
            if on_one > max(counts):
                continue
            reach = ports - total
            for connectors in range(ports):
                for links in range(3):
                    for c in BOX_COMPOSITIONS:
                        for d in range(1, c * reach + 2):
                            yield total, on_one, connectors, links, c, d


Disagreement = tuple[tuple[int, ...], tuple[int, ...], LayoutCase, bool]
#: A failed check beside the verdicts: what failed, the configuration and the case.
Failure = tuple[str, tuple[int, ...], tuple[int, ...], tuple[int, ...]]
#: What `compare_layout_configuration` returns: the cases checked, the disagreements, the misses
#: and the other failures.
Comparison = tuple[int, list[Disagreement], list[Disagreement], list[Failure]]


def compare_layout_configuration(box: tuple[tuple[int, ...], tuple[int, ...]]) -> Comparison:
    """One configuration of the box, checked against the oracle.

    The cases checked, those where the exact program's verdict differs from the oracle's (with
    the oracle's), those where the relaxed program is infeasible while the oracle has a layout,
    and the other failures:

    - `bounds`: a case the oracle has a layout for lies outside `layout_bounds` (A stated);
    - `stars`: the exact program's star counts have no placement by the deterministic star search
      (step 3 relies on it finding one);
    - `unstated`: with A unstated (every count up to A-bar choosable), the exact program's verdict
      differs from "some A has a layout", its smallest admitted A is not the oracle's smallest, or
      a case with a layout lies outside the A-unstated bounds."""
    counts, fan_outs = box
    witnesses = layout_witnesses(counts, fan_outs, BOX_COMPOSITIONS)
    connectable = sum(fan_outs)
    checked = 0
    disagreements: list[Disagreement] = []
    misses: list[Disagreement] = []
    failures: list[Failure] = []
    smallest: dict[tuple[int, int, int, int, int], int | None] = {}
    for case in layout_box_cases(counts):
        total, on_one, connectors, links, c, d = case
        configuration = Configuration(counts, fan_outs, total, on_one, connectors, links, d, c)
        expected = case in witnesses
        checked += 1
        reached = sum(counts) - total
        group = (total, on_one, links, c, d)
        if group not in smallest:
            smallest[group] = None
        if expected:
            if smallest[group] is None:
                smallest[group] = connectors
            least, most = layout_bounds(reached, connectors, c, connectable)
            if not least <= d <= most:
                failures.append(("bounds", counts, fan_outs, case))
        program = exact_program(configuration)
        values = solve(program)
        if (values is not None) != expected:
            disagreements.append((counts, fan_outs, case, expected))
        if values is not None:
            providers = [values[v] for v in program.providers]
            outs = [p + values[v] for p, v in zip(providers, program.extras, strict=True)]
            if star_search(fan_outs, providers, outs) is None:
                failures.append(("stars", counts, fan_outs, case))
        if expected and solve(relaxed_program(configuration)) is None:
            misses.append((counts, fan_outs, case, expected))
    for group, least_count in smallest.items():
        total, on_one, links, c, d = group
        configuration = Configuration(counts, fan_outs, total, on_one, None, links, d, c)
        program = exact_program(configuration)
        choosable = range(program.upper[program.connectors] + 1)
        witness = _Sequence(program, SolverOptions(), Tally()).smallest_admitted(choosable)
        found = None if witness is None else witness[program.connectors]
        if found != least_count:
            failures.append(("unstated", counts, fan_outs, group))
        reached = sum(counts) - total
        least, most = layout_bounds(reached, None, c, connectable, choosable)
        if least_count is not None and not least <= d <= most:
            failures.append(("unstated bounds", counts, fan_outs, group))
    return checked, disagreements, misses, failures


def compare_layout_box(max_ports: int) -> Comparison:
    """`compare_layout_configuration` over the box, on up to eight processes."""
    checked = 0
    disagreements: list[Disagreement] = []
    misses: list[Disagreement] = []
    failures: list[Failure] = []
    processes = max(1, min(8, os.cpu_count() or 1))
    with multiprocessing.get_context("spawn").Pool(processes) as pool:
        for count, different, missed, failed in pool.imap_unordered(
            compare_layout_configuration, layout_box(max_ports)
        ):
            checked += count
            disagreements.extend(different)
            misses.extend(missed)
            failures.extend(failed)
    return checked, sorted(disagreements), sorted(misses), sorted(failures)


# --- counting the work a check does ---------------------------------------------------------------
#
# The cost tests bound counts, not the wall clock: a count is the same on any machine under any
# load. Each wrapper replaces the module attribute for the test alone, so a function is counted
# wherever its module looks the name up.


def count_calls(monkeypatch: pytest.MonkeyPatch, module: ModuleType, *names: str) -> Counter[str]:
    """The calls of these functions of `module`, counted while the test runs."""
    calls = Counter[str]()

    def counted(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            calls[name] += 1
            return function(*args, **kwargs)

        return wrapper

    for name in names:
        monkeypatch.setattr(module, name, counted(name, getattr(module, name)))
    return calls


def record_calls(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Each call of `module.name` while the test runs, its positional and keyword arguments."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    function = getattr(module, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, dict(kwargs)))
        return function(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return calls


def count_yields(monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str) -> Counter[str]:
    """The items the generator function `module.name` yields while the test runs, under `name`.

    The counter is the one `count_calls` returns, so a test reads a call count and a yield count
    the same way.
    """
    yielded = Counter[str]()
    function = getattr(module, name)

    def wrapper(*args: Any, **kwargs: Any) -> Iterator[Any]:
        for item in function(*args, **kwargs):
            yielded[name] += 1
            yield item

    monkeypatch.setattr(module, name, wrapper)
    return yielded


def clear_search_caches() -> None:
    """Empty the shape's and the connector search's caches.

    A cache hit then never hides the work a counted call does.
    """
    shape_module._shape_cached.cache_clear()
    connectors_module._solve_cached.cache_clear()
    connectors_module._solve_open_cached.cache_clear()


#: The construction checks a count above the ceiling must never reach: each grows with the
#: counts, so a refusal before them does no work growing with the count.
CONSTRUCTION_CHECKS = ("component_refusals", "specimen_shape", "hardware_refusals")


def refused_before_construction(
    monkeypatch: pytest.MonkeyPatch, passports: Sequence[dict[str, Any]]
) -> list[list[Refusal]]:
    """Each passport's refusals (`check_feasibility`), asserted to come before any construction check.

    The same counters then see the neutral passport's checks, so they are shown on the path.
    """
    calls = count_calls(monkeypatch, feasibility, *CONSTRUCTION_CHECKS)
    found = []
    for passport in passports:
        clear_search_caches()
        found.append(check_feasibility(passport))
        assert calls == Counter()
    clear_search_caches()
    assert check_feasibility(neutral_passport()) == []
    assert set(calls) == set(CONSTRUCTION_CHECKS)
    return found
