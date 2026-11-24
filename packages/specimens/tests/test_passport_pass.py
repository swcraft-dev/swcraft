"""The specimen generator's own passport pass over the rendered documents.

The hand-built documents below each carry a passport computed by hand from the definitions of
SWCraft's `swcraft_tools/passport.py`. The independent oracle is
`tests/fixtures/passport/small.swcraft-passport.json`: `swcraft passport` on the `small` preset's
tree, recorded once as an external program's output.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.architecture import build_architecture
from swcraft_specimens.deterministic import SplitMix64
from swcraft_specimens.emit import STORAGE_DIRECTORIES, build_file_map
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.generate import generate_project
from swcraft_specimens.packaged import preset_path
from swcraft_specimens.passport import (
    PASSPORT_LAYERS,
    RenderedDocuments,
    compute_passport,
    validate_passport,
)
from swcraft_specimens.placement import architecture_to_project, build_placement
from swcraft_specimens.preset import load_preset
from swcraft_specimens.sidecar import GENERATION_SCHEMA_VERSION
from swcraft_specimens.validate import validate_file_map

ROOT = Path(__file__).parents[1]
RECORDED_SMALL = ROOT / "tests/fixtures/passport/small.swcraft-passport.json"


def strict(value: Any) -> str:
    """The canonical text of a passport: key order and int/float kept, so `0` is not `0.0`."""
    return json.dumps(value, ensure_ascii=False)


Documents = dict[str, list[dict[str, Any]]]


def documents(**layers: list[dict[str, Any]]) -> Documents:
    unknown = set(layers) - set(PASSPORT_LAYERS)
    assert not unknown, unknown
    return {layer: layers.get(layer, []) for layer in PASSPORT_LAYERS}


def empty_passport() -> dict[str, Any]:
    """The passport of a model with no element, by hand."""
    return {
        "passport_version": "1.0.0",
        "contract_version": "12.0.0",
        "layers": {layer: 0 for layer in PASSPORT_LAYERS},
        "components": {
            "ports": {"total": 0, "top": []},
            "hubs": [],
            "unconnected": {"total": 0, "max_on_one": 0},
            "by_declared_level": {"QM": 0, "A": 0, "B": 0, "C": 0, "D": 0},
            "runnables": 0,
        },
        "interfaces": {"sender_receiver": 0, "client_server": 0, "fan_out": {"max": 0, "top": []}},
        "connectors": {"assembly": 0, "delegation": 0, "links": 0},
        "hardware": {"cores": 0, "partitions": 0, "memories": 0, "rated_partitions": 0},
        "tasks": {"idle": 0},
        "requirements": {
            "on_all_site_kinds": 0,
            "on_one_port": 0,
            "unallocated": 0,
            "stale_links": 0,
            "links": 0,
        },
        "identifiers": {
            "max_length": 0,
            **{layer: {"max_length": 0, "mean_length": 0} for layer in PASSPORT_LAYERS},
        },
    }


def port(name: str, interface: str | None, *satisfies: str) -> dict[str, Any]:
    value: dict[str, Any] = {"name": name, "direction": "provide"}
    if interface is not None:
        value["interface"] = interface
    if satisfies:
        value["satisfies"] = list(satisfies)
    return value


def end(swc: str | None = None, composition: str | None = None, *, port: str) -> dict[str, Any]:
    value: dict[str, Any] = {}
    if swc is not None:
        value["swc"] = swc
    if composition is not None:
        value["composition"] = composition
    value["port"] = port
    return value


def test_the_passport_layers_are_the_storage_directories_in_registry_order() -> None:
    assert tuple(PASSPORT_LAYERS) == (
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
    assert tuple(PASSPORT_LAYERS.values()) == STORAGE_DIRECTORIES


def test_an_empty_model_has_the_empty_passport_with_int_zero_means() -> None:
    computed = compute_passport(documents())
    assert strict(computed) == strict(empty_passport())
    for layer in PASSPORT_LAYERS:
        assert type(computed["identifiers"][layer]["mean_length"]) is int
    validate_passport(computed)


def topology_documents() -> Documents:
    """A model with the port and connector cases the pass must count.

    A hub (even n, the median a mean), unconnected ports, a connector end on a composition port, a
    delegation reaching a member port and one whose member has no type, a decomposed level, an
    undeclared interface named by a port, and a port naming no interface.
    """
    swc = [
        {
            "name": "Ha",
            "ports": [port(f"p{i}", "I1" if i <= 3 else "I2") for i in range(1, 7)],
            "runnables": [{"name": "R1"}, {"name": "R2"}],
            "asil": "B(D)",
        },
        {
            "name": "Sb",
            "ports": [port("q1", "I1"), port("q2", "I2")],
            "runnables": [{"name": "R1"}],
            "asil": "QM",
        },
        {"name": "Sc", "ports": [port("r1", "I3")], "runnables": []},
        {
            "name": "Sd",
            "ports": [port("s1", "I1"), port("s2", "I2"), port("s3", None)],
            "runnables": [],
            "asil": "D",
        },
    ]
    interfaces = [
        {"name": "I1", "kind": "sender_receiver"},
        {"name": "I2", "kind": "client_server"},
    ]
    connectors = [
        {"name": "C1", "from": end("Ha", port="p1"), "to": end("Sb", port="q1")},
        {"name": "C2", "from": end(composition="Root", port="cp1"), "to": end("Sd", port="s1")},
        {"name": "C3", "from": end("Sb", port="q2"), "to": end("Sd", port="s2")},
        {"name": "C4", "from": end("Sb", port="q2"), "to": end("Sc", port="r1")},
    ]
    compositions = [
        {
            "name": "Root",
            "ports": [port("cp1", "I1"), port("cp2", "I2")],
            "members": [
                {"name": "mHa", "type": "Ha"},
                {"name": "mSc", "type": "Sc"},
                {"name": "mX", "composition": "Sub"},
            ],
            "delegations": [
                {"name": "d1", "port": "cp1", "member": "mSc", "member_port": "r1"},
                {"name": "d2", "port": "cp2", "member": "mX", "member_port": "z"},
            ],
        }
    ]
    return documents(swc=swc, interfaces=interfaces, connectors=connectors, compositions=compositions)


def topology_passport() -> dict[str, Any]:
    expected = empty_passport()
    expected["layers"].update(swc=4, interfaces=2, connectors=4, compositions=1)
    # Counts [6, 2, 1, 3]: the median is (2 + 3) / 2 = 2.5, so a hub has more than 5 ports.
    expected["components"] = {
        "ports": {"total": 12, "top": [6, 3, 2, 1]},
        "hubs": [6],
        # Reached: (Ha,p1) (Sb,q1) (Sd,s1) (Sb,q2) (Sd,s2) (Sc,r1), the last by C4 and by d1.
        # Ha keeps p2..p6, Sd keeps s3; the composition end of C2 reaches nothing.
        "unconnected": {"total": 6, "max_on_one": 5},
        "by_declared_level": {"QM": 1, "A": 0, "B": 1, "C": 0, "D": 1},
        "runnables": 3,
    }
    # I1 and I2 are named by five ports each; I3 is undeclared and named once; s3 names none.
    expected["interfaces"] = {
        "sender_receiver": 1,
        "client_server": 1,
        "fan_out": {"max": 5, "top": [5, 5, 1]},
    }
    # Non-hubs Sb, Sc, Sd (3): C3 and C4 join two of them, so links = max(0, 2 - 3 // 2) = 1.
    expected["connectors"] = {"assembly": 4, "delegation": 2, "links": 1}
    identifiers = expected["identifiers"]
    identifiers["max_length"] = 4
    identifiers["swc"] = {"max_length": 2, "mean_length": 2.0}
    identifiers["interfaces"] = {"max_length": 2, "mean_length": 2.0}
    identifiers["connectors"] = {"max_length": 2, "mean_length": 2.0}
    identifiers["compositions"] = {"max_length": 4, "mean_length": 4.0}
    return expected


def test_the_topology_passport_is_computed_by_hand() -> None:
    computed = compute_passport(topology_documents())
    assert strict(computed) == strict(topology_passport())
    validate_passport(computed)


@pytest.mark.parametrize(
    ("counts", "hubs"),
    [
        ([6, 2, 1, 3], [6]),  # median 2.5: the upper middle value (3) would find no hub
        ([5, 2, 1, 3], []),  # median 2.5: the lower middle value (2) would find a hub of 5
        ([9, 1, 1], [9]),  # odd n: the middle value
        ([4, 3, 0, 0], [4]),  # median 1.5
    ],
)
def test_hubs_exceed_twice_the_median_the_mean_of_the_middle_two(counts: list[int], hubs: list[int]) -> None:
    swc = [{"name": f"S{i}", "ports": [port(f"p{j}", "I") for j in range(count)]} for i, count in enumerate(counts)]
    assert compute_passport(documents(swc=swc))["components"]["hubs"] == hubs


def traceability_documents() -> Documents:
    """A model with the requirement and hardware cases the pass must count.

    A requirement linked from all seven site kinds (the four hardware kinds as one), one linked
    from one port, a stub, an unallocated one, a duplicated id, an undeclared id, an element-form
    member, a stale stated link, a bare id, a malformed suffix, an idle task and a partition rated
    `B`.
    """
    swc = [
        {
            "name": "Ha",
            "ports": [port("P1", "I1", "REQ1@2"), port("P2", "I1", "REQ2@1")],
            "runnables": [{"name": "R1", "satisfies": ["REQ1@1"]}],
            "asil": "QM",
            "satisfies": ["REQ1@2", "REQ1@x"],
        }
    ]
    interfaces = [{"name": "I1", "kind": "sender_receiver", "satisfies": ["REQ1"]}]
    compositions = [{"name": "Top", "members": [], "delegations": [], "satisfies": ["REQ1@2", "DUP@1"]}]
    tasks: list[dict[str, Any]] = [
        {
            "name": "T1",
            "runnables": [{"swc": "Ha", "runnable": "R1"}],
            "satisfies": ["REQ1@2", "hardware:Core0"],
        },
        {"name": "T2", "runnables": []},
    ]
    hardware: list[dict[str, Any]] = [
        {"name": "Proc", "kind": "processor"},
        {"name": "Core0", "kind": "core", "satisfies": ["REQ1@2"]},
        {"name": "Mem0", "kind": "memory"},
        {"name": "Part0", "kind": "partition", "asil": "B", "satisfies": ["REQ1@2", "GHOST@3"]},
        {"name": "Part1", "kind": "partition"},
    ]
    requirements = [
        {"id": "REQ1", "version": 2},
        {"id": "REQ2", "version": 1},
        {"id": "REQ3", "version": 1, "stub": True},
        {"id": "REQ4", "version": 1},
        {"id": "DUP", "version": 1},
        {"id": "DUP", "version": 2},
    ]
    return documents(
        swc=swc,
        interfaces=interfaces,
        compositions=compositions,
        tasks=tasks,
        hardware=hardware,
        requirements=requirements,
    )


def traceability_passport() -> dict[str, Any]:
    expected = empty_passport()
    expected["layers"].update(swc=1, interfaces=1, compositions=1, tasks=2, hardware=5, requirements=6)
    expected["components"] = {
        # One component: the median is 2, nothing exceeds 4.
        "ports": {"total": 2, "top": [2]},
        "hubs": [],
        "unconnected": {"total": 2, "max_on_one": 2},
        "by_declared_level": {"QM": 1, "A": 0, "B": 0, "C": 0, "D": 0},
        "runnables": 1,
    }
    expected["interfaces"] = {
        "sender_receiver": 1,
        "client_server": 0,
        "fan_out": {"max": 2, "top": [2]},
    }
    expected["hardware"] = {"cores": 1, "partitions": 2, "memories": 1, "rated_partitions": 1}
    expected["tasks"] = {"idle": 1}
    expected["requirements"] = {
        # REQ1: swc, port, runnable, interface, composition, task, hardware element.
        "on_all_site_kinds": 1,
        # REQ2: one link, on port P2.
        "on_one_port": 1,
        # REQ4: nothing names it; REQ3 is a stub, neither allocated nor unallocated.
        "unallocated": 1,
        # REQ1@1 states 1 where REQ1 is at 2. The bare REQ1 states none; REQ1@x is malformed;
        # DUP@1 names an id declared twice; GHOST@3 names an id nothing declares.
        "stale_links": 1,
        # Every member but hardware:Core0, the element form: 2 + 1 + 1 + 1 + 1 + 2 + 1 + 2 + 1.
        "links": 12,
    }
    identifiers = expected["identifiers"]
    identifiers["max_length"] = 5
    identifiers["swc"] = {"max_length": 2, "mean_length": 2.0}
    identifiers["interfaces"] = {"max_length": 2, "mean_length": 2.0}
    identifiers["compositions"] = {"max_length": 3, "mean_length": 3.0}
    identifiers["tasks"] = {"max_length": 2, "mean_length": 2.0}
    identifiers["hardware"] = {"max_length": 5, "mean_length": 4.6}
    # REQ1..REQ4 (4 each) and DUP twice (3 each): 22 / 6 = 3.666...
    identifiers["requirements"] = {"max_length": 4, "mean_length": 3.667}
    return expected


def test_the_traceability_passport_is_computed_by_hand() -> None:
    computed = compute_passport(traceability_documents())
    assert strict(computed) == strict(traceability_passport())
    validate_passport(computed)


def test_a_link_on_one_port_beside_another_link_is_not_on_one_port() -> None:
    model = copy.deepcopy(traceability_documents())
    model["swc"][0]["runnables"][0]["satisfies"].append("REQ2@1")
    assert compute_passport(model)["requirements"]["on_one_port"] == 0


def test_a_requirement_missing_one_site_kind_is_not_on_all_site_kinds() -> None:
    model = copy.deepcopy(traceability_documents())
    model["tasks"][0]["satisfies"] = ["hardware:Core0"]
    assert compute_passport(model)["requirements"]["on_all_site_kinds"] == 0


def _rendered(name: str) -> RenderedDocuments:
    """The rendered documents of a shipped WATERS preset, parsed by the file-map gate."""
    preset = load_preset(preset_path(name))
    rng = SplitMix64(preset.seed)
    architecture = build_architecture(preset, rng)
    placement = build_placement(architecture, preset, rng)
    project = architecture_to_project(placement, preset)
    file_map = build_file_map(project)
    return validate_file_map(file_map, project, task_overhead_us=preset.task_overhead_us)


def test_the_pass_equals_swcraft_passport_recorded_on_small() -> None:
    recorded = json.loads(RECORDED_SMALL.read_text(encoding="utf-8"))
    assert strict(compute_passport(_rendered("small"))) == strict(recorded)


@pytest.mark.parametrize("name", ["small", "medium", "large"])
def test_every_waters_sidecar_carries_a_valid_passport_of_its_tree(name: str, tmp_path: Path) -> None:
    """The sidecar's passport is valid and equals the pass over the same rendered documents.

    That is the pass against itself; only `small` is checked against an independent oracle here
    (`tests/fixtures/passport/small.swcraft-passport.json`), and `medium` and `large` meet the live
    `swcraft passport` only in the integration gate (`tests/test_production_preset_golden.py`)."""
    generate_project(load_preset(preset_path(name)), tmp_path / "out")
    text = (tmp_path / "out/generation.json").read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["schema_version"] == GENERATION_SCHEMA_VERSION == "2.1"
    validate_passport(document["passport"])
    assert strict(document["passport"]) == strict(compute_passport(_rendered(name)))


def test_a_delegation_is_the_only_reach_of_a_member_port() -> None:
    """Without C4, (Sc, r1) is reached by the delegation d1 alone.

    A delegation naming a member no composition declares reaches nothing, though the declared
    member has a type.
    """
    layers = topology_documents()
    layers["connectors"] = [c for c in layers["connectors"] if c["name"] != "C4"]
    computed = compute_passport(layers)
    assert computed["components"]["unconnected"] == {"total": 6, "max_on_one": 5}
    layers["compositions"][0]["delegations"][0]["member"] = "mUndeclared"
    computed = compute_passport(layers)
    # (Sc, r1) is now reached by nothing: seven unconnected ports.
    assert computed["components"]["unconnected"] == {"total": 7, "max_on_one": 5}


def test_documents_lacking_a_layer_are_a_generation_error() -> None:
    layers = dict(topology_documents())
    del layers["sequences"]
    with pytest.raises(GenerationError, match="lack layer sequences"):
        compute_passport(layers)


def shapes_documents() -> Documents:
    """The remaining shapes: stubs, a chain of three compositions and the five type layers.

    Two external interface stubs, one of them named by two ports and reached by delegation only, a
    chain `Root` > `Mid` > `Deep` with delegations of all three sources (reach, chain steps, a
    repeat onto a provide port a connector reaches), and the five type layers with their
    references.
    """
    swc = [
        {
            "name": "A",
            "ports": [port("a1", "I1"), port("a2", "I1"), port("a3", "X1")],
            "runnables": [{"name": "R1"}],
            "asil": "QM",
        },
        {
            "name": "B",
            "ports": [port("b1", "I1"), port("b2", "I2")],
            "runnables": [{"name": "R1"}, {"name": "R2"}],
        },
        {
            "name": "C",
            "ports": [port("c1", "I2"), port("c2", "X1"), port("c3", "X2")],
            "runnables": [],
            "asil": "B",
        },
        {"name": "D", "ports": [port("d1", "I2")], "runnables": [{"name": "R1"}]},
    ]
    interfaces = [
        {"name": "I1", "kind": "sender_receiver"},
        {"name": "I2", "kind": "client_server"},
        {
            "name": "X1",
            "kind": "external_interface",
            "external_ref": "/External/Interfaces/X1",
            "external_dest": "CLIENT-SERVER-INTERFACE",
        },
        {
            "name": "X2",
            "kind": "external_interface",
            "external_ref": "/External/Interfaces/X2",
            "external_dest": "CLIENT-SERVER-INTERFACE",
        },
    ]
    # Drawn in the root between root members: the star of b1.
    connectors = [
        {"name": "K1", "from": end("B", port="b1"), "to": end("A", port="a1")},
        {"name": "K2", "from": end("B", port="b1"), "to": end("A", port="a2")},
    ]
    compositions = [
        {
            "name": "Root",
            "title": "Root",
            "root": True,
            "ports": [port("BP1", "I2"), port("BP2", "X1"), port("BP3", "I1")],
            "members": [
                {"name": "M0001", "type": "A"},
                {"name": "M0002", "type": "B"},
                {"name": "Mid", "composition": "Mid"},
            ],
            "delegations": [
                # A chain step onto Mid's boundary port, a reach onto a stub port of a root
                # member, a repeat onto the star centre b1.
                {"name": "Dlg1", "port": "BP1", "member": "Mid", "member_port": "BP1"},
                {"name": "Dlg2", "port": "BP2", "member": "M0001", "member_port": "a3"},
                {"name": "Dlg3", "port": "BP3", "member": "M0002", "member_port": "b1"},
            ],
        },
        {
            "name": "Mid",
            "title": "Mid",
            "root": False,
            "ports": [port("BP1", "I2")],
            "members": [{"name": "Deep", "composition": "Deep"}],
            "delegations": [
                {"name": "Dlg1", "port": "BP1", "member": "Deep", "member_port": "BP1"},
            ],
        },
        {
            "name": "Deep",
            "title": "Deep",
            "root": False,
            "ports": [port("BP1", "I2"), port("BP2", "X1"), port("BP3", "I2")],
            "members": [{"name": "M0001", "type": "C"}, {"name": "M0002", "type": "D"}],
            "delegations": [
                {"name": "Dlg1", "port": "BP1", "member": "M0001", "member_port": "c1"},
                {"name": "Dlg2", "port": "BP2", "member": "M0001", "member_port": "c2"},
                {"name": "Dlg3", "port": "BP3", "member": "M0002", "member_port": "d1"},
            ],
        },
    ]
    units = [{"name": "Un1"}, {"name": "Unit22"}]
    compu_methods = [
        {"name": "Cm1", "kind": "linear", "unit": "Un1", "factor": 1, "offset": 0},
        {"name": "Cm2", "kind": "linear", "unit": "Unit22", "factor": 1, "offset": 0},
        {"name": "Cm10", "kind": "linear", "unit": "Un1", "factor": 1, "offset": 0},
    ]
    data_constraints = [{"name": "Dc1", "min": 0, "max": 100}]
    application_types = [
        {
            "name": f"At{index}",
            "kind": "application_type",
            "unit_ref": units[(index - 1) % 2]["name"],
            "compu_method_ref": compu_methods[(index - 1) % 3]["name"],
            "data_constr_ref": "Dc1",
        }
        for index in range(1, 5)
    ]
    mapping_sets = [
        {
            "name": "Ms1",
            "maps": [{"application": f"At{index}", "implementation": "Vt1"} for index in range(1, 5)],
        }
    ]
    return documents(
        swc=swc,
        interfaces=interfaces,
        connectors=connectors,
        compositions=compositions,
        implementation_types=[{"name": "Vt1", "kind": "value", "base": "Bt1"}],
        base_types=[{"name": "Bt1"}],
        units=units,
        compu_methods=compu_methods,
        data_constraints=data_constraints,
        application_types=application_types,
        mapping_sets=mapping_sets,
    )


def shapes_passport() -> dict[str, Any]:
    expected = empty_passport()
    expected["layers"].update(
        swc=4,
        interfaces=4,
        connectors=2,
        application_types=4,
        implementation_types=1,
        base_types=1,
        compositions=3,
        mapping_sets=1,
        units=2,
        compu_methods=3,
        data_constraints=1,
    )
    expected["components"] = {
        # Counts [3, 2, 3, 1]: the median is 2.5, nothing exceeds 5.
        "ports": {"total": 9, "top": [3, 3, 2, 1]},
        "hubs": [],
        # Reached: a1, a2 and b1 by the connectors (b1 by the repeat Dlg3 too); a3 by the root's
        # reach Dlg2; c1, c2, d1 by Deep's reaches. The chain steps reach no component port.
        # Left: b2 on B and c3 on C.
        "unconnected": {"total": 2, "max_on_one": 1},
        "by_declared_level": {"QM": 1, "A": 0, "B": 1, "C": 0, "D": 0},
        "runnables": 4,
    }
    # I1: a1, a2, b1; I2: b2, c1, d1; the stub X1: a3, c2; the stub X2: c3. The composition
    # boundary ports count nowhere; the stubs count in the layer and in the fan-out, in neither
    # kind count.
    expected["interfaces"] = {
        "sender_receiver": 1,
        "client_server": 1,
        "fan_out": {"max": 3, "top": [3, 3, 2, 1]},
    }
    # Root 3 + Mid 1 + Deep 3. No hub: m = 4 ordinary components, both connectors join two of
    # them, so links = max(0, 2 - 4 // 2) = 0.
    expected["connectors"] = {"assembly": 2, "delegation": 7, "links": 0}
    identifiers = expected["identifiers"]
    identifiers["max_length"] = 6
    identifiers["swc"] = {"max_length": 1, "mean_length": 1.0}
    identifiers["interfaces"] = {"max_length": 2, "mean_length": 2.0}
    identifiers["connectors"] = {"max_length": 2, "mean_length": 2.0}
    identifiers["application_types"] = {"max_length": 3, "mean_length": 3.0}
    identifiers["implementation_types"] = {"max_length": 3, "mean_length": 3.0}
    identifiers["base_types"] = {"max_length": 3, "mean_length": 3.0}
    # Root, Mid, Deep: 11 / 3 = 3.666...
    identifiers["compositions"] = {"max_length": 4, "mean_length": 3.667}
    identifiers["mapping_sets"] = {"max_length": 3, "mean_length": 3.0}
    # Un1 and Unit22: 9 / 2.
    identifiers["units"] = {"max_length": 6, "mean_length": 4.5}
    # Cm1, Cm2, Cm10: 10 / 3 = 3.333...
    identifiers["compu_methods"] = {"max_length": 4, "mean_length": 3.333}
    identifiers["data_constraints"] = {"max_length": 3, "mean_length": 3.0}
    return expected


def test_the_remaining_shapes_passport_is_computed_by_hand() -> None:
    computed = compute_passport(shapes_documents())
    assert strict(computed) == strict(shapes_passport())
    validate_passport(computed)


def test_a_chain_step_reaches_no_component_port_and_a_repeat_none_twice() -> None:
    """Without Deep's reach onto c1 the port is left, though Mid and Root step its boundary port up.

    Without the repeat, b1 stays reached by its connectors, and D drops by one.
    """
    layers = shapes_documents()
    deep = layers["compositions"][2]
    deep["delegations"] = deep["delegations"][1:]
    computed = compute_passport(layers)
    assert computed["components"]["unconnected"] == {"total": 3, "max_on_one": 2}
    assert computed["connectors"]["delegation"] == 6
    layers = shapes_documents()
    layers["compositions"][0]["delegations"].pop()
    computed = compute_passport(layers)
    assert computed["components"]["unconnected"] == {"total": 2, "max_on_one": 1}
    assert computed["connectors"]["delegation"] == 6
