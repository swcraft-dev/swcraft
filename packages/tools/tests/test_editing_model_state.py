"""The editing engine reads ``ModelState`` by layer name, not composed attribute names.

``ModelSnapshot`` and ``ModelDiff`` carry one field per layer that has a
``snapshotAttributes`` row, so an attribute lookup answers ``None`` for a layer without
one and the package would silently behave as though the layer were not in the model.
The sites in ``address.resolve``, ``apply``, ``passes.py``, ``delta.project_delta`` and
``derive.py`` read ``snapshot.state``, ``diff.by_layer`` and ``state.placement`` instead.

**The oracle is the synthetic layer.** ``synthetic_support`` merges one extra layer into
the contract, bound to a test-local class, with no ``snapshotAttributes`` row: the layer
no adapter field exists for. An attribute lookup misses it without an error, by leaving
the layer out of the answer.

**What these tests do not reach.**

* The end-to-end flow-through (loader, fingerprint, ``impact``, authorization,
  scaffolder) is ``test_synthetic_flow_through.py``'s.
* Every call threads ``reg.current()`` explicitly, so the subject is the sites, not the
  registry seam; the seam's gate is ``packages/tools/tests/test_editing_registry_injection.py``.
* ``ambiguous_member_violations`` is not shown reporting a duplicate member of the
  synthetic layer: ``SyntheticElement`` has no collection, so the call proves only that
  the element walk finds the element.
* ``derive.py``'s placement branch is exercised on ``swc`` alone; the synthetic layer
  declares no placement field.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from swcraft_tools.differ import diff_snapshots
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, Miss, resolve
from swcraft_tools.editing.apply import StagedModel, apply_operations
from swcraft_tools.editing.delta import project_delta
from swcraft_tools.editing.derive import _planned_snapshot, derive_change_set
from swcraft_tools.editing.discovery import discover
from swcraft_tools.editing.passes import ambiguous_baseline_violations, ambiguous_member_violations
from swcraft_tools.editing.protocol import UpdateOperation, parse_change_operation
from swcraft_tools.editing.read import read_value
from swcraft_tools.model_registry import ModelRegistry, use_registry
from swcraft_tools.model_state import ModelState
from swcraft_tools.project import require_project
from swcraft_tools.schema import Port, SWComponent
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from synthetic_support import SyntheticElement, merged_registry

#: The vector fixture every case is built over: the shipped basic example with every
#: layer directory present, so the synthetic layer is the only difference.
BASE_VECTOR = "discover-project-all-layers-present"

S1 = {"name": "S1", "title": "One", "composition": "VehicleTop"}


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8")


@pytest.fixture
def merged() -> ModelRegistry:
    return merged_registry()


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A project whose model carries one ``synthetic/`` element beside the shipped layers."""
    root = fixtures.build_fixture(tmp_path / "eleven", BASE_VECTOR)
    _write(root / "model" / "synthetic" / "S1.yaml", S1)
    return root


@pytest.fixture
def model_root(project_root: Path) -> Path:
    return project_root / "model"


# The two entry points, over a model with the synthetic layer


def test_discover_publishes_the_synthetic_layer(project_root: Path, merged: ModelRegistry) -> None:
    """The precondition for everything below: the protocol can see the layer."""
    project = require_project(str(project_root / "swcraft.yaml"))
    with use_registry(merged):
        response = discover(project)

    published = [layer["layer"] for layer in response["layers"]]
    assert published == list(merged.layer_names)
    (descriptor,) = [layer for layer in response["layers"] if layer["layer"] == "synthetic"]
    assert descriptor["present"] is True
    assert descriptor["identity_field"] == "name"


def test_read_answers_the_synthetic_layers_element(model_root: Path, merged: ModelRegistry) -> None:
    """`address.resolve` reaches the layer by name, so `read` publishes its element."""
    with use_registry(merged):
        snapshot = load_snapshot(str(model_root))
        value = read_value(snapshot, Address("synthetic", "S1"), registry=reg.current())
        # `full` by name: a layer address answers a summary by default, and this
        # asserts the element is published whole.
        whole_layer = read_value(snapshot, Address("synthetic"), registry=reg.current(), form="full")

    assert value == S1
    assert whole_layer == [S1]


# Staging: `apply.StagedModel.from_snapshot`


def test_staging_publishes_the_synthetic_layers_elements(model_root: Path, merged: ModelRegistry) -> None:
    with use_registry(merged):
        staged = StagedModel.from_snapshot(load_snapshot(str(model_root)), registry=reg.current())

    assert list(staged.elements) == list(merged.layer_names)
    assert staged.elements["synthetic"] == [S1]
    assert staged.baseline["synthetic"] == {"S1": S1}


def test_staging_still_merges_the_one_declared_placement_field(model_root: Path, merged: ModelRegistry) -> None:
    """The placement table comes from `state.placement_of(layer)`; a layer without placement gets an empty table."""
    with use_registry(merged):
        staged = StagedModel.from_snapshot(load_snapshot(str(model_root)), registry=reg.current())

    groups = {element["name"]: element["group"] for element in staged.elements["swc"]}
    assert groups == {"ExampleSWC": "example", "ExampleConsumerSWC": "example"}
    assert all("group" not in element for element in staged.elements["synthetic"])


def test_an_operation_on_the_synthetic_layer_stages_and_is_reported_touched(
    model_root: Path, merged: ModelRegistry
) -> None:
    operation = parse_change_operation(
        {
            "operation": "update",
            "address": {"layer": "synthetic", "element": "S1", "path": [{"field": "title"}]},
            "value": "Two",
        },
        0,
    )
    with use_registry(merged):
        registry = reg.current()
        outcome = apply_operations(load_snapshot(str(model_root)), [operation], registry=registry)
        applied = outcome.staged.get(registry.layer("synthetic"), "S1")

    assert outcome.violations == ()
    assert outcome.inapplicable == ()
    assert [(touched.layer, touched.identity) for touched in outcome.touched] == [("synthetic", "S1")]
    assert outcome.touched[0].before == S1
    assert outcome.touched[0].after == {**S1, "title": "Two"}
    assert applied == {**S1, "title": "Two"}


# The delta projection: `delta.project_delta`


def test_the_delta_projects_a_change_to_the_synthetic_layer(model_root: Path, merged: ModelRegistry) -> None:
    """`ModelDiff` has no field for this layer; `by_layer` is the only way to it."""
    with use_registry(merged):
        registry = reg.current()
        baseline = load_snapshot(str(model_root))
        planned = [("synthetic", SyntheticElement(name="S1", title="Two", composition="VehicleTop"), {})]
        staged = _planned_snapshot(baseline, planned, registry)
        diff = diff_snapshots(baseline, staged)

        assert not hasattr(diff, "synthetic"), "the adapter must give this layer no field, or the test is vacuous"
        assert "synthetic" in diff.by_layer

        entries = project_delta(diff, baseline=baseline, staged=staged, registry=registry)

    assert [entry.summary for entry in entries] == ["synthetic/S1: ~ title One -> Two"]
    assert entries[0].address == Address("synthetic", "S1", (FieldStep("title"),))


def test_the_delta_still_projects_a_diff_rebuilt_by_dataclasses_replace(
    model_root: Path, merged: ModelRegistry
) -> None:
    """`ModelDiff.by_layer` falls back to the declared fields for a diff rebuilt by ``dataclasses.replace``.

    ``test_differ.py`` builds such a diff, which drops the hidden ``_by_layer``.
    """
    with use_registry(merged):
        registry = reg.current()
        before = load_snapshot(str(model_root))
        after = load_snapshot(str(model_root))
        after.tasks[0].priority += 1
        replaced = dataclasses.replace(diff_snapshots(before, after))

        assert replaced.by_layer.layer_names == tuple(name for name in merged.layer_names if name != "synthetic"), (
            "a replaced diff carries the shipped fielded layers and drops the synthetic one"
        )

        entries = project_delta(replaced, baseline=before, staged=after, registry=registry)

    assert [entry.summary for entry in entries] == ["tasks/Task_10ms: ~ priority 5 -> 6"]


# The two ambiguity checks in `passes.py`


def test_the_baseline_ambiguity_check_counts_the_synthetic_layers_duplicates(
    model_root: Path, merged: ModelRegistry
) -> None:
    """Two files with one identity in the synthetic layer are reported, not silently picked."""
    _write(model_root / "synthetic" / "S1_twin.yaml", {**S1, "title": "Twin"})

    with use_registry(merged):
        violations = ambiguous_baseline_violations(load_snapshot(str(model_root)), registry=reg.current())

    assert len(violations) == 1
    assert violations[0].error == "consistency-violation"
    assert "2 elements of layer 'synthetic' with the identity 'S1'" in violations[0].message
    assert violations[0].addresses == (Address("synthetic", "S1"),)


def test_the_member_ambiguity_check_reaches_the_synthetic_layer_and_still_reports_a_shipped_duplicate(
    model_root: Path, merged: ModelRegistry
) -> None:
    """The member check walks the synthetic layer and still reports a shipped duplicate.

    The synthetic layer has no collection, so only the ``swc`` case shows a report.
    """
    port = Port(name="pDup", direction="provide", interface="IExample")
    duplicate = SWComponent.model_construct(name="DupSWC", type="application", ports=[port, port])

    with use_registry(merged):
        registry = reg.current()
        snapshot = load_snapshot(str(model_root))
        snapshot.swcs.append(duplicate)

        shipped = ambiguous_member_violations(
            snapshot,
            [UpdateOperation(address=Address("swc", "DupSWC", (FieldStep("ports"),)), value=[])],
            registry=registry,
        )
        eleventh = ambiguous_member_violations(
            snapshot,
            [UpdateOperation(address=Address("synthetic", "S1", (FieldStep("title"),)), value="Two")],
            registry=registry,
        )

    assert len(shipped) == 1
    assert "2 members" in shipped[0].message
    assert shipped[0].addresses[0].layer == "swc"
    assert eleventh == ()


# Derivation: the planned snapshot is composed, never `dataclasses.replace`d


def test_the_derived_snapshot_keeps_the_synthetic_layer(model_root: Path, merged: ModelRegistry) -> None:
    """``derive.py`` composes a ``ModelState``: replacing the derived snapshot would drop the layer.

    ``dataclasses.replace`` rebuilds the adapter from its declared fields alone, and the
    state is an undeclared instance attribute.
    """
    with use_registry(merged):
        baseline = load_snapshot(str(model_root))
        planned = [("synthetic", SyntheticElement(name="S2", title="Second", composition="VehicleTop"), {})]
        derived = _planned_snapshot(baseline, planned, reg.current())

        assert derived.state.layer_names == merged.layer_names
        assert [element.name for element in derived.state["synthetic"]] == ["S1", "S2"]
        assert "synthetic" not in dataclasses.replace(derived).state, (
            "`replace` is exactly what this site stopped doing"
        )

    assert [element.name for element in baseline.state["synthetic"]] == ["S1"], "the baseline is never mutated"


def test_the_derived_snapshot_shares_no_layer_list_with_its_baseline(model_root: Path, merged: ModelRegistry) -> None:
    """Every layer is copied, so nothing done to the derived state reaches the baseline."""
    with use_registry(merged):
        baseline = load_snapshot(str(model_root))
        planned = [("synthetic", SyntheticElement(name="S2", title="Second", composition="VehicleTop"), {})]
        derived = _planned_snapshot(baseline, planned, reg.current())

    for name in merged.layer_names:
        assert derived.state[name] is not baseline.state[name], f"{name} is the baseline's own list"
        if name != "synthetic":
            assert derived.state[name] == baseline.state[name], f"{name} was copied but not faithfully"


def test_a_planned_element_of_the_nesting_layer_carries_its_placement(model_root: Path, merged: ModelRegistry) -> None:
    """A planned ``swc`` in a new group reaches the derived placement table; the baseline's is not edited."""
    element = SWComponent(name="PlannedSWC", type="application")
    with use_registry(merged):
        baseline = load_snapshot(str(model_root))
        derived = _planned_snapshot(baseline, [("swc", element, {"group": "planned"})], reg.current())

    assert derived.state.placement_of("swc")["PlannedSWC"] == {"group": "planned"}
    assert derived.state.placement_of("swc")["ExampleSWC"] == {"group": "example"}
    assert derived.swc_placement["PlannedSWC"] == {"group": "planned"}
    assert "PlannedSWC" not in baseline.state.placement_of("swc"), "the baseline's table is never edited"


def test_derive_change_set_derives_an_update_of_the_synthetic_layers_element(
    model_root: Path, merged: ModelRegistry
) -> None:
    """End to end through the derivation, whose replay raises ``DerivationFault`` if the layer were lost."""
    with use_registry(merged):
        request = derive_change_set(
            load_snapshot(str(model_root)),
            [("synthetic", SyntheticElement(name="S1", title="Two", composition="VehicleTop"), {})],
            baseline_token="rev1:abcdefgh",
            intent="rename the synthetic element's title",
            registry=reg.current(),
        )

    assert request == {
        "operation": "change_set",
        "baseline": "rev1:abcdefgh",
        "intent": "rename the synthetic element's title",
        "operations": [
            {
                "operation": "update",
                "address": {"layer": "synthetic", "element": "S1", "path": [{"field": "title"}]},
                "value": "Two",
            }
        ],
    }


# The miss, word for word


def test_a_declared_layer_the_state_lacks_is_still_no_such_layer_with_the_same_text(
    merged: ModelRegistry,
) -> None:
    """A state lacking a declared layer answers the same no-such-layer text, verbatim.

    It is what a client reads back on a ``read``, so the wording is part of the answer.
    """
    with use_registry(merged):
        registry = reg.current()
        ten = ModelSnapshot.from_state(
            ModelState(layers={name: [] for name in merged.layer_names if name != "synthetic"})
        )
        miss = resolve(Address("synthetic", "S1"), ten, registry=registry)

    assert isinstance(miss, Miss)
    assert miss.reason == "no_such_layer"
    assert miss.detail == "the snapshot carries no 'synthetic'"
    assert miss.address == Address("synthetic", "S1")


def test_an_undeclared_layer_is_still_the_other_no_such_layer_text(merged: ModelRegistry) -> None:
    """The registry lookup fails before the state is consulted at all."""
    with use_registry(merged):
        registry = reg.current()
        miss = resolve(Address("nonesuch"), ModelSnapshot.from_state(ModelState(layers={})), registry=registry)

    assert isinstance(miss, Miss)
    assert miss.reason == "no_such_layer"
    assert miss.detail == "no layer 'nonesuch' is declared"


def test_a_keyword_built_snapshot_resolves_through_the_state_it_rebuilds(model_root: Path) -> None:
    """A keyword-built ``ModelSnapshot`` resolves through the state it rebuilds.

    ``resolve`` reports the layer as the resolved container, so it must be the snapshot's own list.
    """
    snapshot = load_snapshot(str(model_root))
    keyword_built = ModelSnapshot(
        compositions=snapshot.compositions,
        swcs=snapshot.swcs,
        interfaces=snapshot.interfaces,
        implementation_types=snapshot.implementation_types,
        connectors=snapshot.connectors,
        requirements=snapshot.requirements,
        tasks=snapshot.tasks,
        sequences=snapshot.sequences,
        hardware=snapshot.hardware,
        mapping_sets=snapshot.mapping_sets,
        base_types=snapshot.base_types,
        application_types=snapshot.application_types,
        units=snapshot.units,
        compu_methods=snapshot.compu_methods,
        data_constraints=snapshot.data_constraints,
    )

    resolution = resolve(Address("swc"), keyword_built)

    assert resolution.found
    assert resolution.value is keyword_built.swcs
