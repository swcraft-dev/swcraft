"""``ModelDiff`` as an adapter over ``ModelStateDiff``.

What must not change: the dataclass fields, their order, their absence of
defaults, positional and keyword construction, ``dataclasses.replace`` and the two
renderers' bytes. ``test_differ.py``, ``test_differ_authorization_compat.py``,
``test_session.py``, the CLI diff goldens and the MCP diff tests exercise most of
that; here: the fields keep the binding's declared order and the labels and
identity fields come from the contract.

What is new: :class:`~swcraft_tools.differ.ModelStateDiff` and the hidden
``_by_layer`` that carries it. Its fallback is asserted at byte level: a diff with
no carried state must render identically.

Does not reach: a sixteenth layer never becomes a dataclass field, and a
``replace``\\ d or keyword-built diff drops it — asserted in both directions as a
documented limitation.
"""

from __future__ import annotations

import dataclasses
import json
import pickle
from typing import Any, get_type_hints

import pytest
from swcraft_tools import differ
from swcraft_tools.differ import (
    ChangedItem,
    LayerDiff,
    ModelDiff,
    ModelStateDiff,
    collection_identity,
    collection_rule,
    diff_model_states,
    diff_snapshots,
    format_diff,
    format_diff_json,
)
from swcraft_tools.model_registry import ModelRegistry, production_registry, use_registry
from swcraft_tools.model_state import ModelState
from synthetic_support import SyntheticElement, merged_registry
from test_differ import DEEP_NEW, DEEP_OLD, GOLDEN_DIFF_DIR

#: The field order, written out independently of `differ.py` and of the binding,
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
    # The three data description layers, in the binding's key order.
    "application_types",
    "implementation_types",
    "base_types",
    # The three layers of the forms a type names by reference, in the binding's key order.
    "units",
    "compu_methods",
    "data_constraints",
)

#: The layer name of each attribute, in the same order; `swc` -> `swcs` is the
#: one the binding exists for.
THE_TEN_LAYERS = (
    "compositions",
    "swc",
    "interfaces",
    "connectors",
    "requirements",
    "tasks",
    "sequences",
    "hardware",
    "mapping_sets",
    # The three data description layers, in the binding's key order.
    "application_types",
    "implementation_types",
    "base_types",
    # The three layers of the forms a type names by reference, in the binding's key order.
    "units",
    "compu_methods",
    "data_constraints",
)

#: ``(owner class, field name) -> CollectionRule`` over the **production**
#: contract: the projection :func:`~swcraft_tools.differ.collection_rule` resolves per call.
COLLECTION_IDENTITY = collection_identity(production_registry())


@pytest.fixture
def merged() -> ModelRegistry:
    return merged_registry()


def _empty_kwargs() -> dict[str, LayerDiff]:
    return {attribute: LayerDiff() for attribute in THE_TEN_ATTRIBUTES}


def _empty_state() -> ModelState:
    return ModelState(layers={layer: [] for layer in THE_TEN_LAYERS})


def _deep_diff() -> ModelDiff:
    return diff_snapshots(DEEP_OLD, DEEP_NEW)


def _stripped(diff: ModelDiff) -> ModelDiff:
    """*diff*'s fifteen fields, in a diff built by keyword that carries no ``_by_layer``."""
    return ModelDiff(**{attribute: getattr(diff, attribute) for attribute in THE_TEN_ATTRIBUTES})


# ---------------------------------------------------------------------------
# The fields
# ---------------------------------------------------------------------------


def test_the_fields_are_todays_ten_attributes() -> None:
    names = [field.name for field in dataclasses.fields(ModelDiff)]

    assert names == list(THE_TEN_ATTRIBUTES)
    assert list(ModelDiff.__dataclass_fields__) == list(THE_TEN_ATTRIBUTES)


def test_the_field_order_is_the_bindings_own_key_order_and_not_the_display_order() -> None:
    """The fields follow the binding's key order (``compositions`` first), not ``displayOrder``."""
    registry = production_registry()
    expected = [registry.layer(name).snapshot_attribute for name in registry.adapter_order]
    displayed = [registry.layer(name).snapshot_attribute for name in registry.display_order]

    assert expected == list(THE_TEN_ATTRIBUTES)
    assert displayed[0] == "requirements"
    assert displayed != expected


def test_no_field_carries_a_default_so_positional_construction_still_works() -> None:
    """No field has a default, so ``test_session.py::_empty_diff``'s positional call keeps its meaning."""
    assert all(
        field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING
        for field in dataclasses.fields(ModelDiff)
    )

    built = ModelDiff(*(LayerDiff() for _ in ModelDiff.__dataclass_fields__))

    assert built.is_empty
    assert built == ModelDiff(**_empty_kwargs())


def test_every_field_is_annotated_as_a_layer_diff() -> None:
    assert set(get_type_hints(ModelDiff).values()) == {LayerDiff}


# ---------------------------------------------------------------------------
# Module identity and pickling
# ---------------------------------------------------------------------------


def test_a_diff_survives_a_pickle_round_trip() -> None:
    """Pickle finds the class again by ``__module__`` and ``__qualname__``."""
    original = _deep_diff()

    restored = pickle.loads(pickle.dumps(original))

    assert restored == original
    assert format_diff(restored) == format_diff(original)


def test_the_carried_state_diff_survives_the_round_trip_too() -> None:
    """``_by_layer`` is in the instance ``__dict__``, which is what pickle carries."""
    restored = pickle.loads(pickle.dumps(_deep_diff()))

    assert restored.by_layer == _deep_diff().by_layer
    assert "_by_layer" in restored.__dict__


# ---------------------------------------------------------------------------
# The hidden `_by_layer`
# ---------------------------------------------------------------------------


def test_the_carried_state_diff_is_not_a_dataclass_field() -> None:
    assert "_by_layer" not in {field.name for field in dataclasses.fields(ModelDiff)}
    assert "_by_layer" not in ModelDiff.__dataclass_fields__


def test_the_carried_state_diff_is_invisible_to_repr_and_comparison() -> None:
    loaded = _deep_diff()
    stripped = _stripped(loaded)

    assert loaded == stripped
    assert repr(loaded) == repr(stripped)
    assert "_by_layer" not in repr(loaded)


def test_from_state_diff_carries_the_state_diff_it_was_built_from() -> None:
    state_diff = diff_model_states(DEEP_OLD.state, DEEP_NEW.state)

    built = ModelDiff.from_state_diff(state_diff)

    assert built.by_layer is state_diff
    assert built.swcs is state_diff["swc"]
    assert built.implementation_types is state_diff["implementation_types"]


def test_a_keyword_built_diff_builds_a_state_diff_from_its_fields() -> None:
    """The rebuilt diff is in the registry's **contract** order, as ``ModelSnapshot.state``'s is.

    Nothing reads a ``ModelStateDiff``'s key order (both renderers walk
    ``displayOrder``), and the two orders differ, so this is asserted against
    the registry.
    """
    stripped = _stripped(_deep_diff())

    by_layer = stripped.by_layer

    assert isinstance(by_layer, ModelStateDiff)
    assert by_layer.layer_names == production_registry().layer_names
    assert by_layer.layer_names != THE_TEN_LAYERS, "contract order and display order really do differ"
    assert by_layer["swc"] is stripped.swcs
    assert by_layer["requirements"] is stripped.requirements


def test_a_replaced_diff_falls_back_too() -> None:
    """A ``dataclasses.replace``\\ d diff has no ``_by_layer`` and renders through the fallback."""
    item = ChangedItem(key="SwcOne", changed_fields=[("type", "application", "service")])
    replaced = dataclasses.replace(_deep_diff(), swcs=LayerDiff(changed=[item]))

    assert "_by_layer" not in replaced.__dict__
    assert replaced.by_layer["swc"].changed == [item]
    assert replaced.by_layer["tasks"] is replaced.tasks


def test_is_empty_reads_every_layer_the_diff_carries() -> None:
    assert ModelDiff(**_empty_kwargs()).is_empty
    assert not _deep_diff().is_empty
    assert diff_snapshots(DEEP_OLD, DEEP_OLD).is_empty


# ---------------------------------------------------------------------------
# `ModelStateDiff` and `diff_model_states`
# ---------------------------------------------------------------------------


def test_diff_model_states_covers_every_layer_the_registry_declares() -> None:
    state_diff = diff_model_states(_empty_state(), _empty_state())

    assert state_diff.layer_names == production_registry().layer_names
    assert state_diff.is_empty
    assert all(layer in state_diff for layer in THE_TEN_LAYERS)


def test_a_layer_absent_from_one_state_reads_as_an_empty_side() -> None:
    """A layer absent from one state reads as an empty side, as a missing directory does."""
    populated = ModelState(layers={"swc": DEEP_OLD.swcs})
    empty = ModelState(layers={})

    added = diff_model_states(empty, populated)["swc"]
    removed = diff_model_states(populated, empty)["swc"]

    assert [element.name for element in added.added] == [element.name for element in DEEP_OLD.swcs]
    assert [element.name for element in removed.removed] == [element.name for element in DEEP_OLD.swcs]


def test_a_state_diff_is_a_plain_value() -> None:
    first = diff_model_states(DEEP_OLD.state, DEEP_NEW.state)
    second = diff_model_states(DEEP_OLD.state, DEEP_NEW.state)

    assert first == second
    assert first is not second


def test_the_duplicate_key_refusal_still_names_the_snapshot_attribute() -> None:
    """The refusal says ``in layer 'swcs'``, the attribute, not the layer name ``'swc'``.

    ``swc`` is the one layer whose name and attribute differ.
    """
    duplicated = ModelState(layers={"swc": [DEEP_OLD.swcs[0], DEEP_OLD.swcs[0]]})

    with pytest.raises(ValueError, match="in layer 'swcs'"):
        diff_model_states(duplicated, duplicated)


def test_diff_snapshots_is_the_adapter_over_diff_model_states() -> None:
    from_snapshots = diff_snapshots(DEEP_OLD, DEEP_NEW)
    from_states = ModelDiff.from_state_diff(diff_model_states(DEEP_OLD.state, DEEP_NEW.state))

    assert from_snapshots == from_states
    assert format_diff(from_snapshots) == format_diff(from_states)


# ---------------------------------------------------------------------------
# The rendered bytes
# ---------------------------------------------------------------------------


def test_the_golden_text_is_reproduced_through_the_state_diff() -> None:
    golden = (GOLDEN_DIFF_DIR / "deep-pair.txt").read_text(encoding="utf-8")

    assert format_diff(_deep_diff()) + "\n" == golden


def test_the_golden_json_is_reproduced_through_the_state_diff() -> None:
    golden = (GOLDEN_DIFF_DIR / "deep-pair.json").read_text(encoding="utf-8")

    assert format_diff_json(_deep_diff()) == golden


@pytest.mark.parametrize("golden_name", ["deep-pair.txt", "deep-pair.json"])
def test_a_diff_with_no_carried_state_renders_the_same_bytes(golden_name: str) -> None:
    """A diff with no carried state renders the same bytes.

    ``cli.cmd_diff`` and ``test_differ.py`` render such diffs, so a fallback in a
    different order would change what a reviewer is shown.
    """
    golden = (GOLDEN_DIFF_DIR / golden_name).read_text(encoding="utf-8")
    stripped = _stripped(_deep_diff())

    rendered = format_diff(stripped) + "\n" if golden_name.endswith(".txt") else format_diff_json(stripped)

    assert rendered == golden


def test_the_json_key_order_is_the_declared_display_order() -> None:
    """The JSON schema is fixed: the key set and its order are both part of it."""
    registry = production_registry()
    document = json.loads(format_diff_json(_deep_diff()))

    assert list(document["layers"]) == [registry.layer(name).snapshot_attribute for name in registry.display_order]


def test_every_declared_layer_is_a_json_key_even_with_no_changes() -> None:
    registry = production_registry()
    document = json.loads(format_diff_json(ModelDiff(**_empty_kwargs())))

    assert list(document["layers"]) == [registry.layer(name).snapshot_attribute for name in registry.display_order]
    assert all(
        layer == {"added": [], "removed": [], "changed": [], "rename_hints": []}
        for layer in document["layers"].values()
    )


def test_the_text_renderer_labels_and_orders_sections_from_the_contract() -> None:
    registry = production_registry()
    populated = {
        layer: LayerDiff(changed=[ChangedItem(key="X", changed_fields=[("f", 1, 2)])]) for layer in THE_TEN_LAYERS
    }

    rendered = format_diff(ModelDiff.from_state_diff(ModelStateDiff(layers=populated)))

    headings = [line for line in rendered.splitlines() if line and not line.startswith(" ")]
    assert headings == [f"{registry.layer(name).display_label}:" for name in registry.display_order]


# ---------------------------------------------------------------------------
# The derived views
# ---------------------------------------------------------------------------


def test_the_rendered_layer_rows_are_the_production_projection() -> None:
    """``_layer_renders`` yields the contract's ``displayOrder``, label and identity field row for row."""
    registry = production_registry()

    assert [(render.attribute, render.label, render.identity_field) for render in differ._layer_renders(registry)] == [
        (
            registry.layer(name).snapshot_attribute,
            registry.layer(name).display_label,
            registry.layer(name).identity_field,
        )
        for name in registry.display_order
    ]


def test_the_collection_identity_projection_is_cached_per_registry() -> None:
    """Same registry, same mapping object — and it covers exactly the declared collections."""
    registry = production_registry()

    assert collection_identity(registry) is collection_identity(registry)
    assert COLLECTION_IDENTITY is collection_identity(registry)
    assert set(COLLECTION_IDENTITY) == {
        (registry.bound_class(collection.kind), collection.field) for collection in registry.collections()
    }


def test_every_collection_rule_keys_on_its_declared_identity() -> None:
    """Every collection rule keys on its declared identity."""
    registry = production_registry()

    for collection in registry.collections():
        rule = collection_rule(registry.bound_class(collection.kind), collection.field)
        assert rule is not None, collection.field_id
        assert rule.strategy == collection.member_identity, collection.field_id
        assert rule.kind == ("list" if collection.collection_kind == "list" else "mapping"), collection.field_id
        if collection.member_identity == "field":
            assert rule.identity_fields == (collection.identity_field,), collection.field_id
        elif collection.member_identity == "composite":
            assert rule.identity_fields == collection.identity_fields, collection.field_id
        else:
            assert rule.identity_fields == (), collection.field_id


def test_a_rule_is_the_same_object_every_time_it_is_asked_for() -> None:
    """``CollectionChange.rule`` is compared with ``is`` by ``test_differ.py``."""
    owner = production_registry().bound_class("swc")

    assert collection_rule(owner, "ports") is collection_rule(owner, "ports")


# ---------------------------------------------------------------------------
# The sixteenth layer: what survives the adapter, and what does not
# ---------------------------------------------------------------------------


def _synthetic_state(names: tuple[str, ...]) -> ModelState:
    return ModelState(
        layers={
            "synthetic": [SyntheticElement(name=name, title=name, composition="VehicleTop") for name in names],
        }
    )


def test_a_synthetic_layer_is_compared_and_reaches_no_attribute(merged: ModelRegistry) -> None:
    with use_registry(merged):
        state_diff = diff_model_states(_synthetic_state(("S1",)), _synthetic_state(("S1", "S2")))
        built = ModelDiff.from_state_diff(state_diff)

    assert [element.name for element in state_diff["synthetic"].added] == ["S2"]
    assert "synthetic" not in {field.name for field in dataclasses.fields(ModelDiff)}
    assert built.by_layer["synthetic"] is state_diff["synthetic"]
    assert not built.is_empty, "the synthetic layer is the only non-empty one"


def test_the_synthetic_layer_renders_last_under_its_own_name(merged: ModelRegistry) -> None:
    """A layer declaring no ``label`` or ``displayOrder`` renders last under its own name."""
    with use_registry(merged):
        built = ModelDiff.from_state_diff(diff_model_states(_synthetic_state(("S1",)), _synthetic_state(("S1", "S2"))))

        text = format_diff(built)
        document = json.loads(format_diff_json(built))

    assert text.splitlines()[0] == "synthetic:"
    assert "+ S2 (added)" in text
    assert list(document["layers"])[-1] == "synthetic"
    assert document["layers"]["synthetic"]["added"] == ["S2"]


def test_the_synthetic_layer_renders_after_the_declared_ones(merged: ModelRegistry) -> None:
    with use_registry(merged):
        layers: dict[str, LayerDiff] = {
            layer: LayerDiff(changed=[ChangedItem(key="X", changed_fields=[("f", 1, 2)])])
            for layer in (*THE_TEN_LAYERS, "synthetic")
        }
        rendered = format_diff(ModelDiff.from_state_diff(ModelStateDiff(layers=layers)))

    headings = [line for line in rendered.splitlines() if line and not line.startswith(" ")]
    assert headings[-1] == "synthetic:"
    assert headings[0] == "Requirements:"


def test_replace_drops_the_synthetic_layer(merged: ModelRegistry) -> None:
    """``replace`` copies fields, and ``_by_layer`` is not one: a documented limitation."""
    with use_registry(merged):
        built = ModelDiff.from_state_diff(diff_model_states(_synthetic_state(("S1",)), _synthetic_state(("S1", "S2"))))
        replaced = dataclasses.replace(built, compositions=LayerDiff())

        assert "synthetic" in built.by_layer
        assert "synthetic" not in replaced.by_layer
        assert replaced.is_empty, "the only change it carried was in the layer it dropped"


def test_an_unknown_layer_reaches_no_attribute(merged: ModelRegistry) -> None:
    """A diff taken under one registry, adapted under another, is not an error."""
    with use_registry(merged):
        state_diff = diff_model_states(_synthetic_state(("S1",)), _synthetic_state(("S1", "S2")))

    built: Any = ModelDiff.from_state_diff(state_diff, registry=production_registry())

    assert built.by_layer["synthetic"].added
    assert built == ModelDiff(**_empty_kwargs())
