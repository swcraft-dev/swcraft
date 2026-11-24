"""The contract-derived reference traversal, held to a frozen oracle.

``query.edges`` walks ``registry.references()`` and ``registry.relations()``; ``impact.py`` renders and
delegates. The oracle ``fixtures/recorded_oracles/impact_edges.json`` is an ordered graph, ordered
``impact_of`` results and four rendered ``format_impact`` texts, reached through ``impact``'s public
names. It pins impact's two observable orders, which ``test_impact.py`` does not (it compares dicts),
and both are in the CLI's and the MCP tool's output:

- the **layer-grouping** order of ``impact_of``/``format_impact``, the order the identity-key maps
  are built in; and
- the **per-node dependent** order, edge *insertion* order, because ``impact_of``'s LIFO walk
  iterates ``graph[node].items()``.

The oracle is re-recorded when the contract moves the model, each re-recording checked against the
old state for every row that did not move (the fixture README records the checks). For a row a move
added it is a recording of the code it grades, not an oracle: ``test_impact.py`` grades those, one
isolated snapshot per edge, with the id set read off the shipped registry.

**What this suite does not reach.** Only the shipped contract's edges and one synthetic layer's
reference. Nothing about a relation resolution the contract does not declare (``resolution`` is a
closed three-value vocabulary and :mod:`swcraft_tools.relations` owns each one's own refusals),
nothing about what ``check_hardware_topology`` makes of the edges the graph emits for a memory budget
no memory answers to, and nothing about the identity-key uniqueness refusal, which is
``differ.build_key_map``'s and is covered by ``test_impact.py`` and ``test_cli.py``. ``_targets``'
matching branch (a relation whose target field is not the target layer's identity) has no shipped
edge and is exercised on a planted declaration.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_tools import impact, query
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.impact import (
    Dependent,
    ElementRef,
    build_reference_graph,
    format_impact,
    impact_of,
)
from swcraft_tools.model_registry import (
    ModelRegistry,
    build_model_registry,
    current_registry,
    production_registry,
    use_registry,
)
from swcraft_tools.model_state import load_model_state
from swcraft_tools.query import EdgeDetail
from swcraft_tools.query import edges as query_edges
from swcraft_tools.query.edges import _render_details
from swcraft_tools.query.formatting import _detail_json
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from synthetic_support import merged_registry

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "recorded_oracles"
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def oracle() -> dict[str, Any]:
    """``impact_edges.json``, module-scoped because it is frozen and no test writes it."""
    return json.loads((FIXTURE_ROOT / "impact_edges.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixture_snapshot(oracle: dict[str, Any]) -> ModelSnapshot:
    """The model the oracle was recorded over, loaded through the shipped registry."""
    return load_snapshot(str(FIXTURE_ROOT / oracle["model"]))


def _oracle_row(ref: ElementRef, details: tuple[EdgeDetail, ...]) -> dict[str, Any]:
    """One row in the oracle's shape: the rendered ``detail`` and the structured ``details``."""
    return {
        "layer": ref.layer,
        "name": ref.name,
        "detail": _render_details(details),
        "details": [_detail_json(detail) for detail in details],
    }


def _ordered_graph(graph: dict[ElementRef, dict[ElementRef, tuple[EdgeDetail, ...]]]) -> list[dict[str, Any]]:
    """The graph in the oracle's own shape, so a mismatch reads as a diff of it."""
    return [
        {
            "layer": node.layer,
            "name": node.name,
            "dependents": [_oracle_row(ref, details) for ref, details in dependents.items()],
        }
        for node, dependents in graph.items()
    ]


def _ordered_result(result: dict[str, list[Dependent]]) -> dict[str, list[dict[str, Any]]]:
    return {layer: [_oracle_row(dep.ref, dep.details) for dep in dependents] for layer, dependents in result.items()}


def _pairs(graph: dict[ElementRef, dict[ElementRef, tuple[EdgeDetail, ...]]]) -> set[tuple[ElementRef, ElementRef]]:
    return {(node, dependent) for node, dependents in graph.items() for dependent in dependents}


def _single_edge_graph(snapshot: ModelSnapshot, registry: ModelRegistry, field_id: str) -> Any:
    """The reverse graph (``build_reference_graph``'s orientation) a single declared edge produces."""
    edge = query_edges._declared_edges(registry)[field_id]
    reverse, _forward = query_edges._emit_both(snapshot.state, registry, ((edge.source_layer, (edge,)),))
    return reverse


# ---------------------------------------------------------------------------
# The frozen ordered graph
# ---------------------------------------------------------------------------


def test_the_graph_equals_the_frozen_ordered_structure(fixture_snapshot, oracle) -> None:
    """Every node, dependent and detail in order: the oracle is a list, so a wrong order fails."""
    assert _ordered_graph(build_reference_graph(fixture_snapshot)) == _relabel_base_types(
        oracle["graph"], {base.name for base in fixture_snapshot.base_types}
    )


def _relabel_base_types(value: Any, base_names: set[str]) -> Any:
    """The frozen oracle with each base type's layer token ``data_type`` rewritten to ``base_type``.

    The oracle was recorded while a base type was a ``data_type``; every edge, detail and position is
    still the recorded one.
    """
    if isinstance(value, list):
        return [_relabel_base_types(item, base_names) for item in value]
    if isinstance(value, dict):
        relabelled = {key: _relabel_base_types(item, base_names) for key, item in value.items()}
        if relabelled.get("layer") == "data_type" and relabelled.get("name") in base_names:
            relabelled["layer"] = "base_type"
        return relabelled
    return value


def test_impact_of_reproduces_the_frozen_ordered_results(fixture_snapshot, oracle) -> None:
    """Three starts, ``Shared`` included, compared as ordered items so the layer-grouping order counts."""
    for name, expected in oracle["impact_of"].items():
        result = _ordered_result(impact_of(fixture_snapshot, name))
        assert result == expected, name
        assert list(result.items()) == list(expected.items()), f"{name}: layer-grouping order moved"


def test_format_impact_renders_byte_identical_text(fixture_snapshot, oracle) -> None:
    """The rendered report is what the CLI prints and the MCP tool returns."""
    for name, expected in oracle["format_impact"].items():
        assert format_impact(name, impact_of(fixture_snapshot, name)) == expected, name


# ---------------------------------------------------------------------------
# The two observable orders
# ---------------------------------------------------------------------------


def test_the_layer_grouping_follows_the_declared_display_order() -> None:
    """The identity-key maps are built in ``registry.display_order``, the layer order the CLI prints."""
    registry = production_registry()
    state = load_model_state(str(FIXTURE_ROOT / "impact_model"))
    expected = [query_edges.layer_token(registry.layer(name)) for name in registry.display_order]
    assert list(query_edges._all_key_maps(state, registry)) == expected


def test_a_name_two_layers_carry_is_grouped_in_display_order(fixture_snapshot, oracle) -> None:
    """``Shared`` is a requirement and an SWC; ``requirements`` sorts before ``swc``."""
    assert list(impact_of(fixture_snapshot, "Shared")) == list(oracle["impact_of"]["Shared"]) == ["requirement", "swc"]


def test_the_edge_detail_row_order_is_the_emission_order() -> None:
    """``_EDGE_DETAILS``' row order is the order walked, which reproduces the frozen dependent order."""
    registry = production_registry()
    assert [edge.field_id for edge in query_edges._emission_order(registry)] == list(query_edges._EDGE_DETAILS)


def test_the_emission_plan_groups_consecutive_rows_of_one_source_layer() -> None:
    """Consecutive rows of one source layer form one pass: twenty-five passes for fifty rows.

    Grouping makes the traversal element-outer within a layer, which yields the frozen per-node
    dependent order. A new edge is appended after every listed row, opening a pass when its layer's rows
    stop being consecutive; one inserted *among* the rows would move the dependent order of every node
    the later rows reach, and the frozen graph would report it as a diff of everything.
    """
    plan = query_edges._emission_plan(production_registry())
    assert [layer for layer, _ in plan] == [
        "swc",
        "tasks",
        "interfaces",
        "implementation_types",
        "application_types",
        "connectors",
        "compositions",
        "sequences",
        "swc",
        "tasks",
        "hardware",
        "swc",
        "mapping_sets",
        "implementation_types",
        "application_types",
        "implementation_types",
        "compu_methods",
        "hardware",
        "swc",
        "interfaces",
        "requirements",
        "compositions",
        "tasks",
        "hardware",
        # A variable's two type rows, appended last.
        "swc",
    ]
    assert [len(edges) for _, edges in plan] == [
        1, 1, 4, 2, 2, 3, 3, 2, 1, 1, 1, 1, 2, 3, 4, 2, 1, 1, 3, 3, 1, 1, 1, 4, 2
    ]  # fmt: skip


# ---------------------------------------------------------------------------
# `_EDGE_DETAILS` completeness, both directions
# ---------------------------------------------------------------------------


def test_every_edge_detail_row_names_a_declared_edge() -> None:
    declared = query_edges._declared_edges(production_registry())
    assert [field_id for field_id in query_edges._EDGE_DETAILS if field_id not in declared] == []


def test_every_declared_edge_of_the_shipped_contract_has_a_row() -> None:
    """The other direction: an edge added to the contract but not the table would render with no detail."""
    declared = query_edges._declared_edges(production_registry())
    assert [field_id for field_id in declared if field_id not in query_edges._EDGE_DETAILS] == []


def test_the_shipped_edges_are_the_thirty_two_references_and_the_sixteen_relations() -> None:
    """The shipped contract's edge counts; a contract that declares or retires an edge moves them."""
    registry = production_registry()
    assert len(registry.references()) == 34
    assert len(registry.relations()) == 16
    assert len(query_edges._declared_edges(registry)) == 50


def test_accessible_and_derived_from_are_the_relations_whose_source_and_target_are_one_layer() -> None:
    """The edges whose source and target are one layer, ``True`` marking a relation.

    Pinned because the orientation check below compares the two layer tokens, which are equal for a
    same-layer edge; ``accessible``'s orientation is carried by
    ``test_a_memory_depends_on_every_core_it_admits`` instead.
    """
    registry = production_registry()
    same_layer = {
        field_id: edge.resolver is not None
        for field_id, edge in query_edges._declared_edges(registry).items()
        if edge.source_layer == edge.target_layer
    }
    assert same_layer == {
        # `value_type.base` resolves in `base_types` alone, asserted below.
        "field:implementation_types.reference_type.base": False,
        "field:application_types.record_field.type": False,
        "field:application_types.array.element_type": False,
        "field:compositions.composition_member.composition": False,
        "field:hardware.memory.accessible": True,
        "field:implementation_types.implementation_element.type": False,
        "field:implementation_types.implementation_array.element_type": False,
        "field:implementation_types.pointer.target": False,
        "field:hardware.partition.core": False,
        "field:requirements.requirement.derived_from": True,
    }
    value_base = query_edges._declared_edges(registry)["field:implementation_types.value_type.base"]
    assert (value_base.source_layer, value_base.target_layer) == ("implementation_types", "base_types")


# ---------------------------------------------------------------------------
# Attribution and orientation
# ---------------------------------------------------------------------------


def test_every_emitted_edge_is_explained_by_exactly_one_reference_or_relation(fixture_snapshot) -> None:
    """No edge in the graph comes from anywhere but a declared field."""
    registry = current_registry()
    whole = _pairs(build_reference_graph(fixture_snapshot))
    explained: dict[tuple[ElementRef, ElementRef], list[str]] = {}
    for field_id in query_edges._declared_edges(registry):
        for pair in _pairs(_single_edge_graph(fixture_snapshot, registry, field_id)):
            explained.setdefault(pair, []).append(field_id)

    assert set(explained) == whole
    assert [pair for pair, owners in explained.items() if len(owners) != 1] == []


def test_every_declared_edge_emits_at_least_one_pair_on_the_fixture(fixture_snapshot) -> None:
    """Every declared edge but :data:`SILENT_ON_THE_FIXTURE` emits a pair on the fixture.

    The test above checks that every *emitted* edge has one owner; without this one, a traversal that
    stopped producing an edge would pass both and only the frozen graph would notice, and only if the
    ordered output changed.
    """
    registry = current_registry()
    declared = query_edges._declared_edges(registry)
    silent = [field_id for field_id in declared if not _pairs(_single_edge_graph(fixture_snapshot, registry, field_id))]

    assert silent == sorted(SILENT_ON_THE_FIXTURE), f"declared edges the fixture never exercises: {silent}"
    # The shipped contract's declared edges; a declared or retired edge moves it.
    assert len(declared) == 50


#: The one declared edge the fixture may leave silent: an external interface stub's ``satisfies``.
#: ``impact_model`` may hold no stub (``test_external_stubs_byte_identity.py`` refuses one in a
#: recorded tree), so the edge is witnessed on a planted tree by
#: :func:`test_the_external_interface_link_emits_its_pair_on_a_planted_tree`.
SILENT_ON_THE_FIXTURE = frozenset({"field:interfaces.external_interface.satisfies"})


def test_the_external_interface_link_emits_its_pair_on_a_planted_tree(tmp_path: Path) -> None:
    """The allowance above, witnessed: a stub carrying ``satisfies`` emits the one pair."""
    from conftest import write_model_element

    model_root = tmp_path / "model"
    write_model_element(
        model_root,
        {
            "name": "IOutside",
            "kind": "external_interface",
            "external_ref": "/Outside/IOutside",
            "external_dest": "SENDER-RECEIVER-INTERFACE",
            "satisfies": ["REQ_OUT_001@1"],
        },
    )
    (model_root / "requirements").mkdir(parents=True, exist_ok=True)
    (model_root / "requirements" / "REQ_OUT_001.yaml").write_text(
        "id: REQ_OUT_001\ntitle: T\ndescription: D\nversion: 1\nsource: architecture\n",
        encoding="utf-8",
        newline="\n",
    )
    registry = current_registry()
    pairs = _pairs(_single_edge_graph(load_snapshot(str(model_root)), registry, sorted(SILENT_ON_THE_FIXTURE)[0]))
    assert {((node.layer, node.name), (dependent.layer, dependent.name)) for node, dependent in pairs} == {
        (("requirement", "REQ_OUT_001"), ("interface", "IOutside"))
    }


#: The seven edges into the three layers of the forms a type names by reference, in contract field
#: order, each with the one pair it emits on ``impact_model/``; no ``impact_of`` start reaches them.
ELEMENT_EDGES: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "field:application_types.enum.compu_method_ref": (
        ("compu_method", "ProbeGearTable"),
        ("application_type", "ProbeGear"),
    ),
    "field:application_types.application_type.unit_ref": (
        ("unit", "ProbeKmh"),
        ("application_type", "ProbeSpeedPhys"),
    ),
    "field:application_types.application_type.compu_method_ref": (
        ("compu_method", "ProbeSpeedCompu"),
        ("application_type", "ProbeSpeedPhys"),
    ),
    "field:application_types.application_type.data_constr_ref": (
        ("data_constraint", "ProbeSpeedLimits"),
        ("application_type", "ProbeSpeedPhys"),
    ),
    "field:implementation_types.value_type.compu_method_ref": (
        ("compu_method", "ProbeGearTable"),
        ("implementation_type", "ProbeGearValue"),
    ),
    "field:implementation_types.reference_type.compu_method_ref": (
        ("compu_method", "ProbeGearTable"),
        ("implementation_type", "ProbeGearRef"),
    ),
    "field:compu_methods.linear.unit": (("unit", "ProbeKmh"), ("compu_method", "ProbeSpeedCompu")),
}


def test_each_element_edge_emits_its_pair_on_the_fixture(fixture_snapshot) -> None:
    """Each of :data:`ELEMENT_EDGES`, from the element it names to the element that names it, and nothing else."""
    registry = current_registry()

    for field_id, (target, source) in ELEMENT_EDGES.items():
        pairs = {
            ((node.layer, node.name), (dependent.layer, dependent.name))
            for node, dependent in _pairs(_single_edge_graph(fixture_snapshot, registry, field_id))
        }
        assert pairs == {(target, source)}, field_id


def test_every_edge_points_from_the_target_to_the_dependent(fixture_snapshot) -> None:
    """Orientation is ``target -> dependent`` for every declared edge, without exception.

    The node is an element of the *referenced* layer and the dependent the top-level element of the
    *source* layer.
    """
    registry = current_registry()
    for field_id, edge in query_edges._declared_edges(registry).items():
        source_token = query_edges.layer_token(registry.layer(edge.source_layer))
        target_token = query_edges.layer_token(registry.layer(edge.target_layer))
        for node, dependent in _pairs(_single_edge_graph(fixture_snapshot, registry, field_id)):
            assert node.layer == target_token, field_id
            assert dependent.layer == source_token, field_id


# ---------------------------------------------------------------------------
# The four shapes the fixture model exists for
# ---------------------------------------------------------------------------


def test_a_memory_budget_key_naming_no_declared_memory_still_yields_an_edge(fixture_snapshot) -> None:
    """``ScratchPad`` is declared by no hardware file; the edge is unconditional.

    ``check_hardware_topology``'s ``hardware.undeclared-memory`` owns that complaint, not the graph.
    """
    graph = build_reference_graph(fixture_snapshot)
    node = ElementRef("hardware", "ScratchPad")
    assert node in graph
    assert graph[node] == {ElementRef("swc", "SpeedSensor"): (EdgeDetail("budget", "512"),)}
    assert "ScratchPad" not in {element.name for element in fixture_snapshot.hardware}


def test_two_hardware_elements_cannot_share_a_name(fixture_snapshot) -> None:
    """A hardware element's ``name`` is the layer identity, so a duplicate is refused before a graph.

    ``differ.build_key_map``, which ``build_reference_graph`` calls, refuses it.
    """
    graph = build_reference_graph(fixture_snapshot)
    # A task names its partition, and the partition its core.
    assert graph[ElementRef("hardware", "PARTITION0")][ElementRef("task", "Task_10ms")] == (
        EdgeDetail("partition", "PARTITION0"),
    )
    assert graph[ElementRef("hardware", "PARTITION1")][ElementRef("task", "Task_20ms")] == (
        EdgeDetail("partition", "PARTITION1"),
    )
    assert graph[ElementRef("hardware", "CORE0")][ElementRef("hardware", "PARTITION0")] == (
        EdgeDetail("core", "CORE0"),
    )
    assert ElementRef("task", "Task_10ms") not in graph[ElementRef("hardware", "CORE0")]

    duplicated = ModelSnapshot.from_state(load_model_state(str(FIXTURE_ROOT / "impact_model")))
    duplicated.hardware.append(duplicated.hardware[0])
    with pytest.raises(ValueError, match="CORE0|CPU|RAM0|CORE1|PARTITION0|PARTITION1"):
        build_reference_graph(duplicated)


def test_a_memory_depends_on_every_core_it_admits(fixture_snapshot) -> None:
    """``field:hardware.memory.accessible``: the core is the node, the memory naming it the dependent.

    So removing a core surfaces every memory that would be left admitting nothing.
    """
    graph = build_reference_graph(fixture_snapshot)
    memory = ElementRef("hardware", "RAM0")
    assert graph[ElementRef("hardware", "CORE0")][memory] == (EdgeDetail("access", "CORE0"),)
    assert graph[ElementRef("hardware", "CORE1")][memory] == (EdgeDetail("access", "CORE1"),)
    # And not the reverse: the memory is nobody's node through this edge.
    assert ElementRef("hardware", "CORE0") not in graph.get(memory, {})


def test_a_relation_whose_target_is_not_an_identity_matches_every_carrier(tmp_path: Path) -> None:
    """``_targets``' matching branch, which no shipped edge reaches: one key naming several elements.

    ``field:swc.swc.memory``'s relation is re-pointed from the memory's ``name`` to its ``type`` and a
    component budgets ``RAM``; both ``type: RAM`` memories take the edge.
    """
    import shutil

    root = tmp_path / "model"
    shutil.copytree(FIXTURE_ROOT / "impact_model", root)
    (root / "hardware" / "RAM1.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "RAM1",
                "kind": "memory",
                "type": "RAM",
                "size": 4096,
                "accessible": ["CORE0"],
                "threshold": 0.9,
                "enforcement": "strict",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    sensor = root / "swc" / "sensor" / "SpeedSensor.yaml"
    sensor.write_text(sensor.read_text(encoding="utf-8").replace("  RAM0: 2048", "  RAM: 2048"), encoding="utf-8")

    document = json.loads(json.dumps(load_contract()))
    field = next(
        field
        for layer in document["layers"]
        for kind in layer["kinds"]
        for field in kind.get("fields", [])
        if field["id"] == "field:swc.swc.memory"
    )
    field["relation"]["field"] = "type"
    registry = build_model_registry(contract=document, binding=load_binding())

    with use_registry(registry):
        snapshot = ModelSnapshot.from_state(load_model_state(str(root)))
        graph = build_reference_graph(snapshot)

    sensor_ref = ElementRef("swc", "SpeedSensor")
    assert graph[ElementRef("hardware", "RAM0")][sensor_ref] == (EdgeDetail("budget", "2048"),)
    assert graph[ElementRef("hardware", "RAM1")][sensor_ref] == (EdgeDetail("budget", "2048"),)
    # And the key itself is not a node: it is not any element's identity.
    assert ElementRef("hardware", "RAM") not in graph


def test_a_satisfies_member_is_a_relation_and_yields_an_edge_to_the_requirement(fixture_snapshot) -> None:
    """``satisfies`` publishes no ``reference``: the edge comes from the relation.

    The requirement is the node and the element its dependent — a port's link as its component, with
    the ``port`` detail, a runnable's with the ``runnable`` one; the version after ``@`` takes no part
    in the resolution.
    """
    graph = build_reference_graph(fixture_snapshot)
    requirement = graph[ElementRef("requirement", "REQ_SPEED_001")]
    assert requirement[ElementRef("swc", "SpeedSensor")] == (EdgeDetail("port", "pOut"),)
    assert requirement[ElementRef("swc", "SpeedControl")] == (EdgeDetail("runnable", "SpeedControl_Run"),)
    assert requirement[ElementRef("swc", "Shared")] == ()
    assert requirement[ElementRef("requirement", "REQ_SPEED_002")] == ()
    assert graph[ElementRef("requirement", "REQ_SPEED_002")] == {ElementRef("composition", "Powertrain"): ()}
    assert production_registry().field_facts("swc", "satisfies").reference is None


def test_one_name_carried_by_two_layers_reports_each_match_separately(fixture_snapshot) -> None:
    """Two groups for one name, and the empty one is still a group.

    ``Shared`` is a requirement no element names and an SWC. A name matched in a layer contributes its
    group whether or not the graph reaches it, which keeps the two matches reported *separately*.
    """
    result = impact_of(fixture_snapshot, "Shared")
    assert list(result) == ["requirement", "swc"]
    assert result["requirement"] == []
    assert result["swc"] == [
        Dependent(ElementRef("task", "Task_20ms"), (EdgeDetail("runnable", "Shared_Run"),)),
        Dependent(ElementRef("composition", "Diagnostics"), (EdgeDetail("member", "shared"),)),
        # The connector is reached from `Diagnostics` through its `from` endpoint and names that
        # endpoint, owner first; `Powertrain`, the other endpoint's owner, is not a dependent.
        Dependent(ElementRef("connector", "Diag_to_Powertrain"), (EdgeDetail("endpoint", "Diagnostics.pDiag"),)),
        Dependent(ElementRef("composition", "VehicleTop"), (EdgeDetail("member", "diag"),)),
    ]


# ---------------------------------------------------------------------------
# The tokens, and the rows the deleted derived view projected
# ---------------------------------------------------------------------------
#
# No module holds the layer-key rows as a table; the tests below compose them from the two
# functions in `query/edges.py`, so each asserts the projection rather than a frozen copy.


def _layer_key_rows(registry: ModelRegistry) -> list[tuple[str, str, Callable[[Any], str]]]:
    """``(snapshot attribute, singular token, identity key function)`` per layer, in display order.

    A layer the binding gives no snapshot attribute contributes no row.
    """
    return [
        (layer.snapshot_attribute, query_edges.layer_token(layer), query_edges._identity_key(layer))
        for layer in (registry.layer(name) for name in registry.display_order)
        if layer.snapshot_attribute is not None
    ]


def test_the_module_declares_no_layer_key_table() -> None:
    """No module of ``impact`` or the ``query`` package declares a ``_LAYER_KEY_FNS`` table.

    The ``query`` facade binds only its ``__all__`` names, so each submodule is read on its own.
    """
    names = [info.name for info in pkgutil.walk_packages(query.__path__, prefix=f"{query.__name__}.")]
    query_modules = [query, *(importlib.import_module(name) for name in names)]
    assert len(query_modules) > 1, "the query package's submodules were not found"
    assert [module.__name__ for module in (impact, *query_modules) if hasattr(module, "_LAYER_KEY_FNS")] == []


def test_the_element_ref_tokens_are_the_twelve_singular_snapshot_attributes() -> None:
    """``ElementRef.layer`` is public output: the CLI, the MCP tool and ``format_impact`` print it.

    Each token is the singular of its layer's name but ``hardware``, a mass noun that
    ``layerDecl.token`` declares rather than derives. The order is the contract's presentational
    ``displayOrder``, not the compatibility adapters' field order.
    """
    assert [token for _, token, _ in _layer_key_rows(production_registry())] == [
        "requirement",
        "composition",
        "swc",
        "interface",
        "application_type",
        "implementation_type",
        "base_type",
        "unit",
        "compu_method",
        "data_constraint",
        "mapping_set",
        "connector",
        "task",
        "sequence",
        "hardware",
    ]


def test_the_layer_key_rows_cover_every_layer_the_adapter_carries() -> None:
    """One row per layer with a snapshot attribute and none for a layer without one, per the registry."""
    registry = production_registry()
    rows = _layer_key_rows(registry)
    assert [attribute for attribute, _, _ in rows] == [
        registry.layer(name).snapshot_attribute
        for name in registry.display_order
        if registry.layer(name).snapshot_attribute is not None
    ]
    assert len(rows) == len({attribute for attribute, _, _ in rows})


def test_the_layer_key_functions_read_the_declared_identity_field() -> None:
    """``requirements`` keys on ``id``, the others on ``name``: each key function is called on a probe."""
    registry = production_registry()
    rows = {attribute: key_fn for attribute, _, key_fn in _layer_key_rows(registry)}
    probe = type("Probe", (), {"name": "by-name", "id": "by-id"})()
    for layer in registry.layers:
        assert rows[layer.snapshot_attribute](probe) == getattr(probe, layer.identity_field)


# ---------------------------------------------------------------------------
# The shipped example, unchanged
# ---------------------------------------------------------------------------


def test_impact_of_on_the_shipped_example_model() -> None:
    """``swcraft impact --project examples/basic float32`` names ``IExample``, through the library.

    An absent example is a failure, not a skip: it ships in this repository, so a missing one is a
    broken tree.
    """
    manifest = REPO_ROOT / "examples" / "basic" / "swcraft.yaml"
    assert manifest.exists(), f"the shipped example is missing: {manifest}"
    snapshot = load_snapshot(str(manifest.parent / "model"))
    assert "IExample" in format_impact("float32", impact_of(snapshot, "float32"))


# ---------------------------------------------------------------------------
# The sixteenth layer, through the same traversal and no source edit
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_model(tmp_path: Path) -> Path:
    """The oracle's model plus a ``synthetic/`` layer naming an existing composition."""
    import shutil

    root = tmp_path / "model"
    shutil.copytree(FIXTURE_ROOT / "impact_model", root)
    (root / "synthetic").mkdir()
    (root / "synthetic" / "X.yaml").write_text(
        yaml.safe_dump({"name": "X", "title": "the synthetic layer", "composition": "VehicleTop"}, sort_keys=False),
        encoding="utf-8",
    )
    return root


def test_a_synthetic_layers_reference_traverses_with_no_source_edit(synthetic_model: Path) -> None:
    """``composition:VehicleTop -> synthetic:X``, with the default empty detail and no source edit.

    The layer has no ``_EDGE_DETAILS`` row, no ``snapshotAttributes`` row and no line anywhere in the
    ``query`` package or ``impact.py``.
    """
    registry = merged_registry()
    with use_registry(registry):
        snapshot = ModelSnapshot.from_state(load_model_state(str(synthetic_model)))
        graph = build_reference_graph(snapshot)
        result = impact_of(snapshot, "VehicleTop")

    dependent = ElementRef("synthetic", "X")
    assert graph[ElementRef("composition", "VehicleTop")][dependent] == ()
    assert Dependent(dependent) in result["composition"]


def test_the_synthetic_edge_is_emitted_after_every_listed_row(synthetic_model: Path) -> None:
    """An edge with no row is emitted last, in contract field order."""
    registry = merged_registry()
    order = [edge.field_id for edge in query_edges._emission_order(registry)]
    assert order[: len(query_edges._EDGE_DETAILS)] == list(query_edges._EDGE_DETAILS)
    assert order[len(query_edges._EDGE_DETAILS) :] == ["field:synthetic.synthetic.composition"]


def test_the_synthetic_layer_is_searchable_by_name(synthetic_model: Path) -> None:
    """A layer with no snapshot attribute is still searchable, under its declared token (its own name)."""
    registry = merged_registry()
    with use_registry(registry):
        snapshot = ModelSnapshot.from_state(load_model_state(str(synthetic_model)))
        assert list(impact_of(snapshot, "X")) == ["synthetic"]
        assert query_edges.layer_token(registry.layer("synthetic")) == "synthetic"
