"""``authorization.py``'s layer vocabulary comes from the kernel.

The oracle is the replaced code: ``packages/tools/tests/fixtures/recorded_oracles/authorization_vocabulary.json``
was recorded from its hand-written rows and is never re-recorded, which would compare the code under test with itself.
The rows it cannot grade (replaced, removed and added layers) are a scope derived from the two vocabularies, so
a scope that grows fails :func:`test_the_frozen_exclusion_is_exactly_the_replaced_layers`. The ``data_types``
row was re-recorded for the data type kind rename; its added ``ReferenceDataType`` has no before-state, and
``test_model_registry.py::test_a_multi_kind_layer_publishes_its_top_level_kinds`` is its oracle.
:data:`~swcraft_tools.authorization.DELETE_ELEMENT_LAYERS` stays declared: it is a safety classifier's
vocabulary, and :func:`test_no_structural_rule_over_the_contract_yields_the_classifier_vocabulary` shows that a
derivation would widen it.

Does not reach:

- the removed scope model (``test_authorization_scope.py`` owns what survives);
- the safety classifier's behaviour (``test_authorization_safety.py``);
- the alias rule, read off ``model_fields``: a contract-declared alias is a fact this suite cannot check.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from swcraft_tools import authorization
from swcraft_tools.authorization import (
    DELETE_ELEMENT_LAYERS,
    SafetyClassificationError,
    _layer_fields,
    _layer_identity_fields,
    _layer_models,
    _layer_names,
    _model_classes,
    classify_safety_act,
)
from swcraft_tools.model_registry import ModelRegistry, production_registry, use_registry
from synthetic_support import SyntheticElement, merged_registry

#: The frozen table, recorded from the pre-change code.
FIXTURE = Path(__file__).parent / "fixtures" / "recorded_oracles" / "authorization_vocabulary.json"

#: The four call-time functions, under the name of the deleted view each one fed.
FUNCTIONS = {
    "_LAYER_MODELS": _layer_models,
    "LAYER_IDENTITY_FIELDS": _layer_identity_fields,
    "LAYER_NAMES": _layer_names,
    "LAYER_FIELDS": _layer_fields,
}

#: The four projections over the production contract, computed as the deleted views were.
_LAYER_MODELS = _layer_models(production_registry())
LAYER_IDENTITY_FIELDS = _layer_identity_fields(production_registry())
LAYER_NAMES = _layer_names(production_registry())
LAYER_FIELDS = _layer_fields(production_registry())


#: The oracle's ``data_types`` row is ``implementation_types``: a rename, graded field for field less
#: :data:`MOVED_MEMBERS_8_0`. The binding appends the attribute after every recorded row, so :func:`_frozen`
#: moves the renamed row last.
RENAMED_8_0: dict[str, str] = {"data_types": "implementation_types"}


def _recorded() -> dict[str, Any]:
    """The fixture exactly as recorded."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _frozen() -> dict[str, Any]:
    """The recorded tables under the live names, the renamed row last (:data:`RENAMED_8_0`)."""
    recorded = _recorded()
    renamed = set(RENAMED_8_0.values())

    def last(names: list[str]) -> list[str]:
        live = [RENAMED_8_0.get(name, name) for name in names]
        return [name for name in live if name not in renamed] + [name for name in live if name in renamed]

    result: dict[str, Any] = {"LAYER_NAMES": last(recorded["LAYER_NAMES"])}
    for table in ("_LAYER_MODELS", "LAYER_IDENTITY_FIELDS", "LAYER_FIELDS"):
        rows = {RENAMED_8_0.get(key, key): value for key, value in recorded[table].items()}
        result[table] = {key: rows[key] for key in last(list(rows))}
    result["DELETE_ELEMENT_LAYERS"] = recorded["DELETE_ELEMENT_LAYERS"]
    return result


#: The snapshot attributes the frozen oracle cannot grade (replaced, removed and added layers), derived from the
#: two vocabularies, never transcribed.
REPLACED_ATTRIBUTES: frozenset[str] = frozenset(_frozen()["LAYER_NAMES"]) ^ frozenset(LAYER_NAMES)

#: The frozen layers removed rather than replaced: written out, since the symmetric difference cannot tell a
#: removal from half of a replacement.
REMOVED_ATTRIBUTES: tuple[str, ...] = ("systems",)

#: The layers added with nothing retired for them, written out for the same reason, in the binding's key order.
ADDED_LAYER_ATTRIBUTES: tuple[str, ...] = (
    "mapping_sets",
    "base_types",
    "application_types",
    "units",
    "compu_methods",
    "data_constraints",
)

#: The classes the frozen ``data_types`` row names, by the contract-8.0 layer each belongs to.
MOVED_TO_8_0: dict[str, frozenset[str]] = {
    "base_types": frozenset({"swcraft_tools.schema.BaseDataType"}),
    "application_types": frozenset(
        {
            "swcraft_tools.schema.EnumDataType",
            "swcraft_tools.schema.RecordDataType",
            "swcraft_tools.schema.ArrayDataType",
            "swcraft_tools.schema.ApplicationDataType",
        }
    ),
}

#: The same moves on the frozen side: the moved classes and the fields only they declared. Each moved rather
#: than vanished (:func:`test_what_moved_to_base_types_is_exactly_what_the_frozen_row_loses`), so they are
#: subtracted from the frozen side only, for that row only.
MOVED_MEMBERS_8_0: dict[str, frozenset[str]] = {
    "implementation_types": MOVED_TO_8_0["base_types"] | MOVED_TO_8_0["application_types"],
}
MOVED_FIELDS_8_0: dict[str, frozenset[str]] = {
    "implementation_types": frozenset({"encoding", "native_declaration", "size", "fields", "literals"}),
}


#: Fields added to layers both vocabularies carry, which a layer-level exclusion cannot reach; the frozen file
#: does not grow, so no ``ADDED_FIELDS_*`` table has a before-state.
ADDED_FIELDS_4_0: dict[str, frozenset[str]] = {
    "connectors": frozenset({"composition"}),
    "swcs": frozenset({"external", "external_ref", "external_dest"}),
}

#: The safety integrity level, on a component and on a requirement.
ADDED_FIELDS_4_4: dict[str, frozenset[str]] = {
    "swcs": frozenset({"asil"}),
    "requirements": frozenset({"asil"}),
}

#: ``coexistence``, the one field of its set top-level on a layer element; this vocabulary carries top-level
#: field names alone.
ADDED_FIELDS_4_5: dict[str, frozenset[str]] = {
    "tasks": frozenset({"coexistence"}),
}

#: A component's mapping-set list, and the nested records `conversion` and `range`; the contract's other new
#: fields sit on kinds of a new layer or inside those records, which this vocabulary does not carry.
ADDED_FIELDS_7_0: dict[str, frozenset[str]] = {
    "swcs": frozenset({"data_type_mappings"}),
    "implementation_types": frozenset({"conversion", "range"}),
}

#: The external stub's reference pair, declared by `external_interface` and `external_type`.
ADDED_FIELDS_7_5: dict[str, frozenset[str]] = {
    "interfaces": frozenset({"external_ref", "external_dest"}),
    "implementation_types": frozenset({"external_ref", "external_dest"}),
}

#: The implementation record's `elements`, the implementation array's `element_name` and the pointer's `target`.
ADDED_FIELDS_7_8: dict[str, frozenset[str]] = {
    "implementation_types": frozenset({"elements", "element_name", "target"}),
}

#: A component's `handle_termination_and_restart`.
ADDED_FIELDS_7_9: dict[str, frozenset[str]] = {
    "swcs": frozenset({"handle_termination_and_restart"}),
}

#: `package`, a top-level field of the kinds emitted directly into a root package; the composition's and the
#: mapping set's layers are stated whole below.
ADDED_FIELDS_7_10: dict[str, frozenset[str]] = {
    "swcs": frozenset({"package"}),
    "interfaces": frozenset({"package"}),
    "implementation_types": frozenset({"package"}),
}

#: `is_service`, on both interface kinds; the other additions are member fields, below this granularity.
ADDED_FIELDS_7_11: dict[str, frozenset[str]] = {
    "interfaces": frozenset({"is_service"}),
}

#: A value type's and a reference type's `compu_method_ref`; the application kinds' reference fields belong to
#: `application_types`, which the oracle does not carry.
ADDED_FIELDS_8_0_ELEMENTS: dict[str, frozenset[str]] = {
    "implementation_types": frozenset({"compu_method_ref"}),
}

#: A component's `behavior_name`; the events' and access entries' `name` are member fields.
ADDED_FIELDS_8_0_NAMES: dict[str, frozenset[str]] = {"swcs": frozenset({"behavior_name"})}

#: The trace links: `satisfies` on every linkable layer the frozen vocabulary carries, and the requirement's
#: `version`, `source` and `derived_from`.
ADDED_FIELDS_11_0: dict[str, frozenset[str]] = {
    "swcs": frozenset({"satisfies"}),
    "interfaces": frozenset({"satisfies"}),
    "tasks": frozenset({"satisfies"}),
    "requirements": frozenset({"version", "source", "derived_from"}),
}

#: The requirement's provenance (`document`, `baseline`, `revision`) and its `stub` flag.
ADDED_FIELDS_11_1: dict[str, frozenset[str]] = {
    "requirements": frozenset({"document", "baseline", "revision", "stub"}),
}

#: A component's inter-runnable `variables`; the runnable's variable lists are member fields.
ADDED_FIELDS_12_1: dict[str, frozenset[str]] = {"swcs": frozenset({"variables"})}

#: A component's `exclusive_areas`; the runnable's area lists are member fields.
ADDED_FIELDS_12_2: dict[str, frozenset[str]] = {"swcs": frozenset({"exclusive_areas"})}

#: A requirement's removed fields, gone rather than moved; subtracted on the frozen side only
#: (:func:`test_what_contract_11_removed_is_exactly_what_the_frozen_row_loses` derives the set).
REMOVED_FIELDS_11_0: dict[str, frozenset[str]] = {"requirements": frozenset({"links", "status"})}

#: The union the comparison uses, derived from the tables above.
ADDED_FIELDS: dict[str, frozenset[str]] = {
    layer: ADDED_FIELDS_4_0.get(layer, frozenset())
    | ADDED_FIELDS_4_4.get(layer, frozenset())
    | ADDED_FIELDS_4_5.get(layer, frozenset())
    | ADDED_FIELDS_7_0.get(layer, frozenset())
    | ADDED_FIELDS_7_5.get(layer, frozenset())
    | ADDED_FIELDS_7_8.get(layer, frozenset())
    | ADDED_FIELDS_7_9.get(layer, frozenset())
    | ADDED_FIELDS_7_10.get(layer, frozenset())
    | ADDED_FIELDS_7_11.get(layer, frozenset())
    | ADDED_FIELDS_8_0_ELEMENTS.get(layer, frozenset())
    | ADDED_FIELDS_8_0_NAMES.get(layer, frozenset())
    | ADDED_FIELDS_11_0.get(layer, frozenset())
    | ADDED_FIELDS_11_1.get(layer, frozenset())
    | ADDED_FIELDS_12_1.get(layer, frozenset())
    | ADDED_FIELDS_12_2.get(layer, frozenset())
    for layer in (
        ADDED_FIELDS_4_0.keys()
        | ADDED_FIELDS_4_4.keys()
        | ADDED_FIELDS_4_5.keys()
        | ADDED_FIELDS_7_0.keys()
        | ADDED_FIELDS_7_5.keys()
        | ADDED_FIELDS_7_8.keys()
        | ADDED_FIELDS_7_9.keys()
        | ADDED_FIELDS_7_10.keys()
        | ADDED_FIELDS_7_11.keys()
        | ADDED_FIELDS_8_0_ELEMENTS.keys()
        | ADDED_FIELDS_8_0_NAMES.keys()
        | ADDED_FIELDS_11_0.keys()
        | ADDED_FIELDS_11_1.keys()
        | ADDED_FIELDS_12_1.keys()
        | ADDED_FIELDS_12_2.keys()
    )
}


def _graded(rows: Any) -> Any:
    """*rows* with the replaced layers' entries, and :data:`ADDED_FIELDS` from either side, dropped."""
    if isinstance(rows, dict):
        kept = {key: value for key, value in rows.items() if key not in REPLACED_ATTRIBUTES}
        # Only the two field tables carry lists of names; `_LAYER_MODELS` values
        # are annotations and are handed through untouched.
        return {
            key: (
                [name for name in value if name not in ADDED_FIELDS[key]]
                if key in ADDED_FIELDS and isinstance(value, list)
                else value
            )
            for key, value in kept.items()
        }
    return [row for row in rows if row not in REPLACED_ATTRIBUTES]


def _graded_frozen(rows: Any) -> Any:
    """:func:`_graded` over a frozen table, minus the moved fields."""
    graded = _graded(rows)
    if not isinstance(graded, dict):
        return graded
    result = {}
    for key, value in graded.items():
        if key in MOVED_FIELDS_8_0 and isinstance(value, list):
            value = [name for name in value if name not in MOVED_FIELDS_8_0[key]]
        elif key in REMOVED_FIELDS_11_0 and isinstance(value, list):
            value = [name for name in value if name not in REMOVED_FIELDS_11_0[key]]
        elif key in MOVED_MEMBERS_8_0 and isinstance(value, dict) and "members" in value:
            value = {**value, "members": [m for m in value["members"] if m not in MOVED_MEMBERS_8_0[key]]}
        result[key] = value
    return result


def test_what_moved_to_base_types_is_exactly_what_the_frozen_row_loses() -> None:
    """The frozen-side subtraction is exactly what moved to ``base_types`` and ``application_types``.

    A class or field that is really gone, rather than moved, still fails.
    """
    frozen = _frozen()
    for layer, moved in MOVED_TO_8_0.items():
        assert set(_describe(_LAYER_MODELS[layer])["members"]) == moved, layer
    live_members = set(_describe(_LAYER_MODELS["implementation_types"])["members"])
    assert (
        set(frozen["_LAYER_MODELS"]["implementation_types"]["members"]) - live_members
        == MOVED_MEMBERS_8_0["implementation_types"]
    )
    lost = set(frozen["LAYER_FIELDS"]["implementation_types"]) - LAYER_FIELDS["implementation_types"]
    assert lost == MOVED_FIELDS_8_0["implementation_types"]
    assert {"encoding", "native_declaration", "size"} <= LAYER_FIELDS["base_types"]
    assert {"fields", "literals"} <= LAYER_FIELDS["application_types"]
    assert lost <= LAYER_FIELDS["base_types"] | LAYER_FIELDS["application_types"]


def test_what_contract_11_removed_is_exactly_what_the_frozen_row_loses() -> None:
    """The frozen ``requirements`` row loses exactly ``links`` and ``status``, and no live layer names them."""
    lost = set(_frozen()["LAYER_FIELDS"]["requirements"]) - LAYER_FIELDS["requirements"]
    assert lost == REMOVED_FIELDS_11_0["requirements"]
    assert not any(lost & fields for fields in LAYER_FIELDS.values())


def _describe(annotation: Any) -> dict[str, Any]:
    """One ``_LAYER_MODELS`` value in the fixture's shape, unwrapped as ``authorization._model_classes`` does."""
    metadata = getattr(annotation, "__metadata__", ())
    discriminator = next((getattr(item, "discriminator", None) for item in metadata), None)
    return {
        "form": "discriminated_union" if metadata else "class",
        "discriminator": discriminator,
        "members": [f"{member.__module__}.{member.__qualname__}" for member in _model_classes(annotation)],
    }


@pytest.fixture
def merged() -> ModelRegistry:
    return merged_registry()


# The projections equal the frozen ten rows


def test_the_layer_names_reproduce_the_frozen_order() -> None:
    """Order, not just membership: ``LAYER_NAMES`` is ``ModelSnapshot`` order and stays it."""
    assert _graded(list(LAYER_NAMES)) == _graded(_frozen()["LAYER_NAMES"])


#: The two replacements, retired name first, with the index both sit at: written out because
#: ``REPLACED_ATTRIBUTES`` is a set. Indices count the frozen order without ``systems`` and with the renamed
#: row last.
REPLACEMENTS: tuple[tuple[str, str, int], ...] = (
    ("containers", "compositions", 0),
    ("resources", "hardware", 7),
)


def test_the_replaced_layers_kept_their_positions() -> None:
    """Each replacement sits at its retired layer's index, which the scoped comparison cannot see.

    ``ModelSnapshot``'s field order is a positional contract (``test_snapshot.py``).
    """
    frozen = [name for name in _frozen()["LAYER_NAMES"] if name not in REMOVED_ATTRIBUTES]
    live = [name for name in LAYER_NAMES if name not in ADDED_LAYER_ATTRIBUTES]
    assert len(live) == len(frozen)
    assert [name for name in frozen if name in REPLACED_ATTRIBUTES - set(REMOVED_ATTRIBUTES)] == [
        row[0] for row in REPLACEMENTS
    ]
    assert [name for name in live if name in REPLACED_ATTRIBUTES] == [row[1] for row in REPLACEMENTS]
    for retired, replacement, index in REPLACEMENTS:
        assert live.index(replacement) == frozen.index(retired) == index


def test_the_renamed_row_is_the_recorded_one_moved_last() -> None:
    """:data:`RENAMED_8_0` renames and moves one row last, and reorders nothing else."""
    recorded = _recorded()["LAYER_NAMES"]
    ((old, new),) = RENAMED_8_0.items()
    spelled = old if old in recorded else new
    assert recorded.index(spelled) == 4
    assert _frozen()["LAYER_NAMES"] == [name for name in recorded if name != spelled] + [new]
    carried = [name for name in LAYER_NAMES if name in set(_frozen()["LAYER_NAMES"])]
    assert carried[-1] == new


def test_the_frozen_exclusion_is_exactly_the_replaced_layers() -> None:
    """The excluded scope is exactly the replaced, removed and added layers.

    A further entry would exclude another layer from every comparison without anyone having said so.
    """
    assert len(REPLACED_ATTRIBUTES) == 11
    assert REPLACED_ATTRIBUTES == {
        "resources",
        "hardware",
        "containers",
        "compositions",
        *REMOVED_ATTRIBUTES,
        *ADDED_LAYER_ATTRIBUTES,
    }
    assert set(REMOVED_ATTRIBUTES) <= set(_frozen()["LAYER_NAMES"]) - set(LAYER_NAMES)
    assert set(ADDED_LAYER_ATTRIBUTES) <= set(LAYER_NAMES) - set(_frozen()["LAYER_NAMES"])
    assert set(_frozen()["LAYER_NAMES"]) - REPLACED_ATTRIBUTES == set(LAYER_NAMES) - REPLACED_ATTRIBUTES


def test_the_layer_models_reproduce_the_frozen_annotations() -> None:
    """Every layer's form, discriminator and unwrapped member classes, in order."""
    frozen = _graded_frozen(_frozen()["_LAYER_MODELS"])
    graded = _graded(_LAYER_MODELS)
    assert list(graded) == list(frozen)
    assert {layer: _describe(annotation) for layer, annotation in graded.items()} == frozen


def test_the_identity_fields_reproduce_the_frozen_table() -> None:
    assert _graded(dict(LAYER_IDENTITY_FIELDS)) == _graded(_frozen()["LAYER_IDENTITY_FIELDS"])
    # The replaced layer keeps the identity field the retired one had, so the
    # one row the scope drops is still stated rather than merely skipped.
    assert LAYER_IDENTITY_FIELDS["hardware"] == _frozen()["LAYER_IDENTITY_FIELDS"]["resources"] == "name"


def test_the_layer_vocabulary_reproduces_the_frozen_table() -> None:
    """Including ``connectors`` -> ``from``, the serialization alias the differ reports."""
    live = {layer: sorted(fields) for layer, fields in LAYER_FIELDS.items()}
    assert _graded(live) == _graded_frozen(_frozen()["LAYER_FIELDS"])
    # The replaced layers' vocabularies, which the scoped comparison cannot state: each kind's fields minus
    # the layer identity field.
    assert live["hardware"] == [
        "accessible",
        "asil",
        # A top-level field of two of the layer's kinds, so it joins the union.
        "asil_capability",
        "coexistence",
        "core",
        "description",
        "enforcement",
        "hw_id",
        "kind",
        "satisfies",
        "size",
        "threshold",
        "trust",
        "type",
    ]
    assert live["compositions"] == [
        "delegations",
        "description",
        "members",
        "package",
        "ports",
        "root",
        "satisfies",
        "title",
    ]
    # ...and the fields the scope drops INSIDE a graded layer, for the same
    # reason: `_graded` hides them, so nothing above would notice them missing.
    for layer, added in ADDED_FIELDS_4_0.items():
        assert added <= set(live[layer]), layer


def test_the_fixture_is_not_vacuous() -> None:
    """A positive control: an empty or truncated oracle would pass every check above."""
    frozen = _frozen()
    assert len(frozen["LAYER_NAMES"]) == 10
    assert set(frozen) == {
        "LAYER_NAMES",
        "_LAYER_MODELS",
        "LAYER_IDENTITY_FIELDS",
        "LAYER_FIELDS",
        "DELETE_ELEMENT_LAYERS",
    }


# The functions behind the views


@pytest.mark.parametrize("name", sorted(FUNCTIONS))
def test_each_function_takes_an_optional_registry(name: str) -> None:
    """``registry=None`` resolved in the body, never a default expression evaluated at import."""
    parameter = inspect.signature(FUNCTIONS[name]).parameters["registry"]
    assert parameter.default is None


@pytest.mark.parametrize("name", sorted(FUNCTIONS))
def test_each_function_resolves_the_registry_in_context(name: str, merged: ModelRegistry) -> None:
    """The synthetic layer is described with no source edit, and no argument either."""
    with use_registry(merged):
        answer = FUNCTIONS[name]()
    assert "synthetic" in answer


@pytest.mark.parametrize("name", sorted(FUNCTIONS))
def test_an_explicitly_passed_registry_needs_no_context_block(name: str, merged: ModelRegistry) -> None:
    assert "synthetic" in FUNCTIONS[name](merged)


def test_the_synthetic_layer_is_keyed_by_its_own_name_when_no_adapter_field_exists(merged: ModelRegistry) -> None:
    """``merged_binding`` deliberately adds no ``snapshotAttributes`` row for it."""
    assert merged.layer("synthetic").snapshot_attribute is None
    assert _layer_names(merged)[-1] == "synthetic"


def test_the_synthetic_layers_annotation_is_its_bound_class(merged: ModelRegistry) -> None:
    assert _model_classes(_layer_models(merged)["synthetic"]) == (SyntheticElement,)


def test_the_synthetic_layers_identity_field_is_excluded_from_its_vocabulary(merged: ModelRegistry) -> None:
    """The rule that is easy to get wrong, proven on a layer no row was written for."""
    assert _layer_identity_fields(merged)["synthetic"] == "name"
    assert _layer_fields(merged)["synthetic"] == frozenset({"title", "composition"})


def test_every_layers_identity_field_is_excluded_under_a_merged_registry(merged: ModelRegistry) -> None:
    identities = _layer_identity_fields(merged)
    fields = _layer_fields(merged)
    for layer in _layer_names(merged):
        assert identities[layer] not in fields[layer], f"'{identities[layer]}' must not be in the '{layer}' vocabulary"


def test_the_module_declares_none_of_the_four_deleted_views() -> None:
    """``authorization.py`` publishes the four functions and no module-level table.

    ``packages/tools/tests/test_generic_core_gate.py`` states the same over the whole package.
    """
    for name in FUNCTIONS:
        assert not hasattr(authorization, name), name


def test_an_explicit_registry_beats_the_context(merged: ModelRegistry) -> None:
    """Naming a registry pins the answer whatever ``use_registry`` says."""
    with use_registry(merged):
        for function in FUNCTIONS.values():
            assert "synthetic" not in function(production_registry())


def test_the_two_mappings_stay_immutable() -> None:
    """The views are read-only, and so is what the functions hand back."""
    for mapping in (LAYER_IDENTITY_FIELDS, LAYER_FIELDS, _layer_identity_fields(), _layer_fields()):
        with pytest.raises(TypeError):
            mapping["swcs"] = "tampered"  # type: ignore[index]


# The classifier vocabulary, which stays declared


def test_the_classifier_vocabulary_is_exactly_todays_five() -> None:
    """The classifier vocabulary is pinned as an equality: a widened vocabulary classifies what it refused."""
    assert dict(DELETE_ELEMENT_LAYERS) == {
        "swc": "swcs",
        "requirement": "requirements",
        "connector": "connectors",
        "hardware": "hardware",
        "composition": "compositions",
    }

    # This table is keyed by the classifier's element type, so the retired layer's row is graded on the value
    # and the other rows are still compared key for key.
    def by_attribute(rows: dict[str, str]) -> dict[str, str]:
        return {key: value for key, value in rows.items() if value not in REPLACED_ATTRIBUTES}

    assert by_attribute(dict(DELETE_ELEMENT_LAYERS)) == by_attribute(_frozen()["DELETE_ELEMENT_LAYERS"])
    assert len(by_attribute(dict(DELETE_ELEMENT_LAYERS))) == 3
    # The dropped row: its element type moved with the layer, from `resource` to `hardware`.
    assert _frozen()["DELETE_ELEMENT_LAYERS"]["resource"] == "resources"
    assert DELETE_ELEMENT_LAYERS["hardware"] == "hardware"


def test_deleting_a_composition_is_classified_and_is_not_a_safety_act() -> None:
    """Deleting a composition is classified, and the verdict is empty.

    The negative half, a type still outside the vocabulary, fails if the mapping widens to every element type.
    """
    verdict = classify_safety_act("delete_element", {"element_type": "composition", "name": "Root"}, ())

    assert verdict.changes == ()
    assert verdict.tool == "delete_element"
    assert verdict.is_safety_act is False

    with pytest.raises(SafetyClassificationError, match="composition_port"):
        classify_safety_act("delete_element", {"element_type": "composition_port", "name": "pOut"}, ())


def test_the_classifier_vocabulary_does_not_move_under_another_contract(merged: ModelRegistry) -> None:
    """The classifier vocabulary is identical under a foreign registry.

    No layer becomes deletable by declaring itself.
    """
    before = dict(DELETE_ELEMENT_LAYERS)
    with use_registry(merged):
        assert dict(DELETE_ELEMENT_LAYERS) == before
        assert "synthetic" not in DELETE_ELEMENT_LAYERS


def test_no_structural_rule_over_the_contract_yields_the_classifier_vocabulary() -> None:
    """No structural rule over the contract yields the classifier vocabulary; each widens it.

    - every layer's top-level kind names admits ``task`` and ``sequence``, which the guard refuses;
    - no layer declares a kind named ``hardware``, and a multi-kind layer would give one row per kind.
    """
    registry = production_registry()
    kind_names = {kind.kind for layer in registry.layers for kind in layer.top_level_kinds}

    assert "hardware" not in kind_names
    assert {"processor", "core", "memory"} <= kind_names
    assert {"task", "sequence"} <= kind_names - set(DELETE_ELEMENT_LAYERS)
    assert kind_names != set(DELETE_ELEMENT_LAYERS)


def test_every_classifier_layer_is_a_real_layer() -> None:
    """Declared, but not free-floating: each value still names a layer the projections key."""
    assert set(DELETE_ELEMENT_LAYERS.values()) <= set(_layer_names(production_registry()))
