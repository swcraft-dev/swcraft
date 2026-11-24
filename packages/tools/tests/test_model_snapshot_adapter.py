"""``ModelSnapshot`` as an adapter over ``ModelState``.

What must not change: the dataclass fields, their order, their annotations,
keyword construction, ``dataclasses.replace``, equality, ``repr`` and every
message ``load_snapshot`` raises. Other suites exercise those (`test_snapshot.py`,
`test_editing_address.py`, `test_editing_derive.py`, `test_impact.py`,
`test_authorization_scope.py`); here: the fields agree with
`binding.snapshotAttributes` in its own key order (not `displayOrder`) and with the
registry's element types (``adapter_drift``), and the gate reports each kind of
disagreement.

What is new: the hidden ``_state``. It is not a dataclass field, so a layer the
binding gives no attribute survives :meth:`ModelSnapshot.from_state` through
``.state`` — and ``dataclasses.replace`` drops it. Both directions are asserted:
the second is a documented limitation.
"""

from __future__ import annotations

import ast
import dataclasses
import pickle
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml
from conftest import REPO_ROOT
from model_contract_fixtures import merged_contract
from swcraft_tools import reader, snapshot
from swcraft_tools.contract import load_binding
from swcraft_tools.differ import LayerDiff, ModelDiff
from swcraft_tools.model_registry import (
    ModelRegistry,
    build_model_registry,
    current_registry,
    production_registry,
    use_registry,
)
from swcraft_tools.model_state import ModelState, load_model_state
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from synthetic_support import SyntheticElement, adapter_drift, merged_binding, merged_registry

#: The field order, written out independently of `snapshot.py` and of the binding,
#: so a class and a binding that moved together are still caught.
THE_TEN_ATTRIBUTES = (
    "compositions",
    "swcs",
    "interfaces",
    "connectors",
    "requirements",
    "tasks",
    "sequences",
    "hardware",
    "mapping_sets",
    # The three type layers' own attributes, in the binding's key order; the
    # `data_types` attribute is gone, its elements are `implementation_types`'.
    "application_types",
    "implementation_types",
    "base_types",
    # The three layers of the forms a type names by reference, after the type
    # layers in the binding's key order.
    "units",
    "compu_methods",
    "data_constraints",
)

EXAMPLE_MODEL = REPO_ROOT / "examples" / "basic" / "model"


@pytest.fixture
def merged() -> ModelRegistry:
    return merged_registry()


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _empty_kwargs() -> dict[str, list[Any]]:
    return {attribute: [] for attribute in THE_TEN_ATTRIBUTES}


def _todays_snapshot_values(model_root: str) -> dict[str, Any]:
    """What the ``read_*`` calls answer, assembled per layer attribute."""
    with_placement = reader.read_swc_model_with_placement(model_root)
    return {
        "compositions": reader.read_compositions(model_root),
        "swcs": [element for element, _ in with_placement],
        "interfaces": reader.read_interfaces(model_root),
        "connectors": reader.read_connectors(model_root),
        "requirements": reader.read_requirements(model_root),
        "tasks": reader.read_tasks(model_root),
        "sequences": reader.read_sequences(model_root),
        "hardware": reader.read_hardware(model_root),
        "mapping_sets": reader.read_mapping_sets(model_root),
        "application_types": reader.read_layer(model_root, "application_types"),
        # `read_data_types` reads the `implementation_types` layer.
        "implementation_types": reader.read_data_types(model_root),
        "base_types": reader.read_layer(model_root, "base_types"),
        "units": reader.read_layer(model_root, "units"),
        "compu_methods": reader.read_layer(model_root, "compu_methods"),
        "data_constraints": reader.read_layer(model_root, "data_constraints"),
        "swc_placement": {element.name: placement for element, placement in with_placement},
    }


# ---------------------------------------------------------------------------
# The fields
# ---------------------------------------------------------------------------


def test_the_fields_are_todays_ten_attributes_plus_placement() -> None:
    names = [field.name for field in dataclasses.fields(ModelSnapshot)]

    assert names == [*THE_TEN_ATTRIBUTES, "swc_placement"]


def test_the_class_agrees_with_the_production_binding() -> None:
    """The fields, their order and their element types are the binding's.

    Consumers read the fields positionally, so the order is
    ``binding.snapshotAttributes``' keys, not ``displayOrder``. Element types are
    compared with ``Annotated`` stripped.
    """
    registry = production_registry()
    binding_order = [
        attribute
        for layer_id, attribute in load_binding()["snapshotAttributes"].items()
        if layer_id.startswith("layer:")
    ]

    assert [registry.layer(name).snapshot_attribute for name in registry.adapter_order] == binding_order
    assert adapter_drift(ModelSnapshot, registry) == []
    assert adapter_drift(ModelDiff, registry) == []


def test_the_field_order_is_not_the_declared_display_order() -> None:
    """The field order (``compositions`` first) differs from ``displayOrder`` (``requirements`` first).

    Without this, a generator iterating the display order could pass the previous
    test by accident.
    """
    registry = production_registry()
    display_order = [registry.layer(name).snapshot_attribute for name in registry.display_order]

    assert display_order[0] == "requirements"
    assert display_order != list(THE_TEN_ATTRIBUTES)
    assert sorted(display_order) == sorted(THE_TEN_ATTRIBUTES), "the same layers, in a different order"


# ---------------------------------------------------------------------------
# The drift gate fires on each kind of disagreement
# ---------------------------------------------------------------------------


def _planted_registry(rows: dict[str, str]) -> ModelRegistry:
    """The merged registry with ``binding.snapshotAttributes`` replaced by *rows*."""
    binding = merged_binding()
    binding["snapshotAttributes"] = rows
    return build_model_registry(contract=merged_contract(), binding=binding)


def _shipped_rows() -> dict[str, str]:
    return dict(load_binding()["snapshotAttributes"])


def test_the_gate_names_a_field_the_binding_adds_for_both_classes() -> None:
    registry = _planted_registry({**_shipped_rows(), "layer:synthetic": "synthetics"})

    assert adapter_drift(ModelSnapshot, registry) == [
        "ModelSnapshot: field 'synthetics' missing; add 'synthetics: list[SyntheticElement]' after 'data_constraints'"
    ]
    assert adapter_drift(ModelDiff, registry) == [
        "ModelDiff: field 'synthetics' missing; add 'synthetics: LayerDiff' after 'data_constraints'"
    ]


def test_the_gate_names_a_field_the_binding_no_longer_gives() -> None:
    rows = _shipped_rows()
    del rows["layer:units"]
    registry = _planted_registry(rows)

    assert adapter_drift(ModelSnapshot, registry) == ["ModelSnapshot: field 'units' is not in the binding; remove it"]
    assert adapter_drift(ModelDiff, registry) == ["ModelDiff: field 'units' is not in the binding; remove it"]


def test_the_gate_names_two_swapped_rows_as_an_order_change() -> None:
    items = list(_shipped_rows().items())
    items[0], items[1] = items[1], items[0]
    registry = _planted_registry(dict(items))

    for cls in (ModelSnapshot, ModelDiff):
        assert adapter_drift(cls, registry) == [
            f"{cls.__name__}: field 'swcs' is at position 1; the binding puts it at position 0",
            f"{cls.__name__}: field 'compositions' is at position 0; the binding puts it at position 1",
        ]


def test_the_gate_names_a_row_pointing_at_a_layer_of_another_element_type() -> None:
    """``base_types`` and ``units`` keep their places and trade layers: same names, other types.

    The diff's fields are ``LayerDiff`` whatever the layer, so it does not drift.
    """
    rows = {}
    for layer_id, attribute in _shipped_rows().items():
        if layer_id == "layer:base_types":
            rows["layer:units"] = "base_types"
        elif layer_id == "layer:units":
            rows["layer:base_types"] = "units"
        else:
            rows[layer_id] = attribute
    registry = _planted_registry(rows)

    assert adapter_drift(ModelSnapshot, registry) == [
        "ModelSnapshot: field 'base_types' is 'list[BaseDataType]'; retype it 'list[Unit]'",
        "ModelSnapshot: field 'units' is 'list[Unit]'; retype it 'list[BaseDataType]'",
    ]
    assert adapter_drift(ModelDiff, registry) == []


def test_the_gate_names_a_missing_first_field_as_first() -> None:
    registry = _planted_registry({"layer:synthetic": "synthetics", **_shipped_rows()})

    assert adapter_drift(ModelDiff, registry) == [
        "ModelDiff: field 'synthetics' missing; add 'synthetics: LayerDiff' first"
    ]


def test_the_gate_refuses_a_class_that_is_neither_adapter() -> None:
    with pytest.raises(ValueError, match="LayerDiff is not one of the two adapters"):
        adapter_drift(LayerDiff, production_registry())


# ---------------------------------------------------------------------------
# Pickling
# ---------------------------------------------------------------------------


def test_a_snapshot_survives_a_pickle_round_trip() -> None:
    """Pickle finds the class again by ``__module__`` and ``__qualname__``."""
    original = load_snapshot(str(EXAMPLE_MODEL))

    restored = pickle.loads(pickle.dumps(original))

    assert restored == original
    assert [element.name for element in restored.swcs] == [element.name for element in original.swcs]


@pytest.mark.parametrize(
    ("adapter", "module"),
    [("ModelSnapshot", "swcraft_tools.snapshot"), ("ModelDiff", "swcraft_tools.differ")],
)
def test_both_adapters_pickle_and_resolve_their_hints(adapter: str, module: str) -> None:
    """Each adapter survives a pickle round trip and its type hints resolve.

    ``conftest._layer_element_classes`` and ``test_differ_coverage.py`` seed their
    schema walks from those hints.
    """
    import importlib

    cls = getattr(importlib.import_module(module), adapter)

    # Built rather than loaded, so the round trip is about the class; every
    # field is a list except `swc_placement`, the one map.
    empty = cls(**{f.name: {} if f.name == "swc_placement" else [] for f in dataclasses.fields(cls)})
    restored = pickle.loads(pickle.dumps(empty))
    assert restored == empty
    assert type(restored) is cls

    hints = get_type_hints(cls)
    assert set(hints) == {field.name for field in dataclasses.fields(cls)}


# ---------------------------------------------------------------------------
# The hidden state
# ---------------------------------------------------------------------------


def test_the_state_is_not_a_dataclass_field() -> None:
    assert "_state" not in {field.name for field in dataclasses.fields(ModelSnapshot)}
    assert "_state" not in ModelSnapshot.__dataclass_fields__


def test_the_carried_state_is_invisible_to_repr_and_comparison() -> None:
    loaded = load_snapshot(str(EXAMPLE_MODEL))
    keyword_built = ModelSnapshot(
        **{attribute: getattr(loaded, attribute) for attribute in THE_TEN_ATTRIBUTES},
        swc_placement=loaded.swc_placement,
    )

    assert loaded == keyword_built
    assert repr(loaded) == repr(keyword_built)
    assert "_state" not in repr(loaded)


def test_keyword_construction_still_defaults_the_placement_map() -> None:
    built = ModelSnapshot(**_empty_kwargs())

    assert built.swc_placement == {}


def test_from_state_carries_the_state_it_was_built_from() -> None:
    state = load_model_state(EXAMPLE_MODEL)

    built = ModelSnapshot.from_state(state)

    assert built.state is state
    assert built.swcs is state["swc"]
    assert built.implementation_types is state["implementation_types"]


def test_a_keyword_built_snapshot_builds_a_state_from_its_fields() -> None:
    loaded = load_snapshot(str(EXAMPLE_MODEL))
    keyword_built = ModelSnapshot(
        **{attribute: getattr(loaded, attribute) for attribute in THE_TEN_ATTRIBUTES},
        swc_placement=loaded.swc_placement,
    )

    state = keyword_built.state

    assert isinstance(state, ModelState)
    assert tuple(state.layer_names) == current_registry().layer_names
    assert state["swc"] == loaded.swcs
    assert state.placement_of("swc") == loaded.swc_placement


def test_the_built_state_carries_the_placement_of_the_nesting_layer() -> None:
    """``swc_placement`` is a legacy *name*; its content is the nesting layer's table."""
    built = ModelSnapshot(**_empty_kwargs(), swc_placement={"SpeedSensor": {"group": "sensors"}})

    assert built.state.placement_of("swc") == {"SpeedSensor": {"group": "sensors"}}
    assert built.state.placement_of("interfaces") == {}


# ---------------------------------------------------------------------------
# The sixteenth layer: what survives the adapter, and what does not
# ---------------------------------------------------------------------------


def test_a_state_with_a_synthetic_layer_survives_from_state(tmp_path: Path) -> None:
    """A layer with no ``snapshotAttributes`` row survives ``from_state`` and reaches consumers through ``.state``."""
    model = tmp_path / "model"
    _write(model / "swc" / "SwcOne.yaml", {"name": "SwcOne", "type": "application", "description": "one"})
    _write(model / "synthetic" / "S1.yaml", {"name": "S1", "title": "One", "composition": "VehicleTop"})

    with use_registry(merged_registry()):
        built = ModelSnapshot.from_state(load_model_state(model))

    assert [element.name for element in built.state["synthetic"]] == ["S1"]
    assert all(isinstance(element, SyntheticElement) for element in built.state["synthetic"])
    assert [element.name for element in built.swcs] == ["SwcOne"], "the shipped fields are unaffected"
    assert "synthetic" not in {field.name for field in dataclasses.fields(ModelSnapshot)}


def test_replace_drops_the_synthetic_layer(tmp_path: Path, merged: ModelRegistry) -> None:
    """``replace`` copies fields, and ``_state`` is not one: a documented limitation.

    A replaced snapshot rebuilds its state from its fifteen attributes, so a layer
    with no attribute is gone.
    """
    model = tmp_path / "model"
    _write(model / "synthetic" / "S1.yaml", {"name": "S1", "title": "One", "composition": "VehicleTop"})

    with use_registry(merged):
        built = ModelSnapshot.from_state(load_model_state(model))
        replaced = dataclasses.replace(built, compositions=[])

        assert "synthetic" in built.state
        assert "synthetic" not in replaced.state


def test_an_unknown_layer_reaches_no_attribute(tmp_path: Path, merged: ModelRegistry) -> None:
    """A layer in the state that the adapter's registry lacks stays in ``.state`` and populates nothing."""
    model = tmp_path / "model"
    _write(model / "synthetic" / "S1.yaml", {"name": "S1", "title": "One", "composition": "VehicleTop"})
    state = load_model_state(model, registry=merged)

    built = ModelSnapshot.from_state(state, registry=production_registry())

    assert built.state["synthetic"]
    assert built == ModelSnapshot(**_empty_kwargs())


# ---------------------------------------------------------------------------
# `load_snapshot` is unchanged, and `_read_all_layers` is gone
# ---------------------------------------------------------------------------


def test_load_snapshot_reproduces_the_readers_on_the_example_model() -> None:
    loaded = load_snapshot(str(EXAMPLE_MODEL))
    expected = _todays_snapshot_values(str(EXAMPLE_MODEL))

    for attribute, value in expected.items():
        assert getattr(loaded, attribute) == value, attribute


def test_load_snapshot_reproduces_the_ten_readers_at_a_revision(git_repo) -> None:
    _, model_root = git_repo

    loaded = load_snapshot(str(model_root), rev="HEAD")
    expected = _todays_snapshot_values(str(model_root))

    for attribute, value in expected.items():
        assert getattr(loaded, attribute) == value, attribute


def test_a_loaded_snapshot_carries_its_state() -> None:
    loaded = load_snapshot(str(EXAMPLE_MODEL))

    assert isinstance(loaded.state, ModelState)
    assert loaded.state["swc"] is loaded.swcs


def test_the_ten_reader_assembly_helper_is_gone() -> None:
    """`_read_all_layers` is gone: nothing composes the readers here."""
    assert not hasattr(snapshot, "_read_all_layers")


def test_the_module_never_imports_the_readers() -> None:
    """The adapter imports no reader; the elements come from ``model_state``.

    An AST walk, not a substring scan: the module's prose names the readers.
    """
    tree = ast.parse(
        (REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "snapshot.py").read_text(encoding="utf-8")
    )
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            imported.add(target)
            imported.update(f"{target}.{alias.name}" for alias in node.names)

    assert "swcraft_tools.reader" not in imported
    assert "swcraft_tools.model_state" in imported
