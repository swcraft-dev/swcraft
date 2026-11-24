"""The kernel model registry.

**The oracle is frozen, and it is not this code.**
``packages/tools/tests/fixtures/recorded_oracles/registry_tables.json`` holds the six declared
tables of ``editing/registry.py`` as they stood before the kernel's projection existed.
Every assertion below compares the kernel's projection against that file. A
disagreement is a defect in the projection; the fixture is never re-recorded
from the new code (only a description the contract itself rewords is re-recorded,
which keeps the row graded), and its recorder is not committed.

**Where the projection is read from.** ``editing/registry.py`` publishes no table;
the projection lives in the private ``_PRODUCTION_TABLES`` /
``_PRODUCTION_FIELD_ANNOTATIONS`` that :func:`build_registry` falls back to. This
module reads those two deliberately: the frozen file is an oracle for what the
contract projects, and comparing ``build_registry()``'s finished :class:`Registry`
would compare the descriptors and lose the step the oracle protects.

**What "the same table" means for a mapping.** The fixture serializes each dict as a
list of rows, because JSON has no tuple keys. ``KIND_NAMES``, ``KIND_DESCRIPTIONS``
and ``FIELD_ANNOTATIONS`` are compared as ordered lists. ``COLLECTION_SPECS`` is
compared as a mapping: its frozen rows were grouped by hand while the contract
declares each collection in field order, and every reader indexes it by
``(kind, field)``.

**What the seam has to prove.** That the resolution happens **at call time**: the
same helper, called twice with the context switched in between, must answer about
two different contracts. A default-argument expression would pass the first call
and fail the second, so the two-call form is the assertion.
"""

from __future__ import annotations

import ast
import copy
import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import REPO_ROOT
from model_contract_fixtures import ARXML_BOUND_LAYER_NAMES, load_synthetic_layer, merged_contract
from pydantic import BaseModel, TypeAdapter
from swcraft_tools import model_registry as kernel
from swcraft_tools import relations
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.editing import registry as reg
from swcraft_tools.model_registry import (
    ModelRegistry,
    ModelRegistryError,
    StorageRecord,
    build_model_registry,
    compose_storage_path,
    current_registry,
    production_registry,
    use_registry,
)
from swcraft_tools.schema import Memory, Requirement, SWComponent, Task
from synthetic_support import (
    OVERLAPPING_NESTED_LAYERS,
    SIBLING_NESTED_LAYERS,
    SyntheticElement,
    merged_registry,
    nested_registry,
)

FROZEN_TABLES = json.loads(
    (REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "recorded_oracles" / "registry_tables.json").read_text(
        encoding="utf-8"
    )
)


# ---------------------------------------------------------------------------
# The synthetic layer, bound to a test-local class
# ---------------------------------------------------------------------------
#
# :class:`SyntheticElement` and :func:`merged_registry` live in
# ``packages/tools/tests/support/synthetic_support.py``, shared with
# ``test_model_state.py`` so there is one answer to "what is a synthetic element".


@pytest.fixture
def merged() -> ModelRegistry:
    return merged_registry()


# ---------------------------------------------------------------------------
# Serializers: the projection in the frozen file's own vocabulary
# ---------------------------------------------------------------------------


def _class_path(model: type[BaseModel]) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def _serialize_annotation(annotation: Any) -> dict[str, Any]:
    """One ``LayerSpec.annotation`` as the frozen file records it."""
    metadata = getattr(annotation, "__metadata__", ())
    if metadata:
        discriminator = next(
            (getattr(meta, "discriminator", None) for meta in metadata if getattr(meta, "discriminator", None)),
            None,
        )
        return {
            "form": "discriminated_union",
            "discriminator": discriminator,
            "members": [_class_path(member) for member in annotation.__origin__.__args__],
        }
    return {"form": "class", "discriminator": None, "members": [_class_path(annotation)]}


def _serialize_layer_specs(specs: tuple[reg.LayerSpec, ...]) -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "annotation": _serialize_annotation(spec.annotation),
            "identity_field": spec.identity_field,
            "snapshot_attribute": spec.snapshot_attribute,
            "storage": {
                "directory": spec.storage.directory,
                "nesting": spec.storage.nesting,
                "file_suffix": spec.storage.file_suffix,
                "filename_from": spec.storage.filename_from,
            },
            "optional": spec.optional,
            "description": spec.description,
            "absent_description": spec.absent_description,
        }
        for spec in specs
    ]


def _serialize_placement_fields(specs: tuple[reg.PlacementFieldSpec, ...]) -> list[dict[str, Any]]:
    return [
        {
            "layer": spec.layer,
            "kind": spec.kind,
            "name": spec.name,
            "type": spec.type,
            "description": spec.description,
        }
        for spec in specs
    ]


def _serialize_kind_names(names: dict[type[BaseModel], str]) -> list[dict[str, str]]:
    return [{"model": _class_path(model), "kind": kind} for model, kind in names.items()]


def _serialize_field_annotations(annotations: dict[tuple[str, str], reg.FieldAnnotation]) -> list[dict[str, Any]]:
    return [
        {
            "kind": kind,
            "field": name,
            "description": annotation.description,
            "unit": annotation.unit,
            "reference": (
                None
                if annotation.reference is None
                else {"layer": annotation.reference.layer, "identity_field": annotation.reference.identity_field}
            ),
            "immutable": annotation.immutable,
        }
        for (kind, name), annotation in annotations.items()
    ]


def _serialize_collection_specs(specs: dict[tuple[str, str], reg.CollectionSpec]) -> list[dict[str, Any]]:
    return [
        {
            "kind": kind,
            "field": name,
            "member_identity": spec.member_identity,
            "identity_field": spec.identity_field,
            "identity_fields": list(spec.identity_fields),
            "description": spec.description,
        }
        for (kind, name), spec in specs.items()
    ]


def _rows_by_key(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(row["kind"], row["field"]): row for row in rows}


#: The shipped contract's projection, named once so that a restructured
#: projection fails this module in one place.
_TABLES = reg._PRODUCTION_TABLES
_FIELD_ANNOTATIONS = reg._PRODUCTION_FIELD_ANNOTATIONS


#: The layers with no row to compare on one side, excluded from every frozen
#: comparison by name on both sides. A replaced layer has no before-state for
#: its successor (``resources`` -> ``hardware``, ``containers`` ->
#: ``compositions``); a removed one (``systems``) has a frozen row and no
#: projected one; an added one (``mapping_sets``, ``units``, ``compu_methods``,
#: ``data_constraints``, and ``base_types`` and ``application_types``, which took
#: kinds out of ``data_types``) has a projected row and no frozen one. The kinds a
#: split layer took stay graded: each was re-recorded in its new position.
#: Every other row of every table is graded;
#: :func:`test_the_frozen_exclusion_is_exactly_the_replaced_layer` asserts these
#: sets against the two contracts.
UNGRADED_LAYERS = frozenset(
    {
        "resources",
        "hardware",
        "containers",
        "compositions",
        "systems",
        "mapping_sets",
        "base_types",
        "application_types",
        "units",
        "compu_methods",
        "data_constraints",
    }
)

#: The kinds of the replaced layers, on both sides of each replacement.
#: ``memory`` is on both sides and a different kind on each (a flat pool with a
#: ``size`` before, a processor's memory now), so naming it hides both.
UNGRADED_KINDS = frozenset(
    {
        "cpu_core",
        "memory",
        "processor",
        "core",
        "container",
        "composition",
        "composition_port",
        "composition_member",
        "delegation",
        "system",
        # The `hardware` layer's fourth kind, hidden with its layer.
        "partition",
    }
)

#: Contract ``7.0.0``'s application type with its member kinds ``conversion`` and
#: ``range``, and the kinds of its ``mapping_sets`` layer. A part of ``ADDED_KINDS``
#: and excluded by every other contract's addition test; no test grades it on its own.
ADDED_KINDS_7_0 = frozenset({"application_type", "conversion", "range", "mapping_set", "map"})

#: Contract ``7.0.0``'s one field on a graded kind: the component's reference to
#: the mapping sets its behaviour maps with.
ADDED_FIELDS_7_0 = frozenset({("swc", "data_type_mappings")})

#: Contract ``7.6.0``'s port hint ``observed_interface``.
ADDED_FIELDS_7_6 = frozenset({("port", "observed_interface")})

#: Contract ``7.7.0``'s runnable ``concurrent``; its member kind is
#: :data:`ADDED_KINDS_7_7`.
ADDED_FIELDS_7_7 = frozenset({("runnable", "concurrent")})

#: Contract ``7.9.0``'s component field, graded positively by
#: :func:`test_the_7_9_0_addition_is_exactly_the_added_row`.
ADDED_FIELDS_7_9 = frozenset({("swc", "handle_termination_and_restart")})

#: Contract ``7.10.0``'s ``description`` on six member kinds the frozen table
#: carries; the seventh, ``implementation_element``'s, is hidden with its kind
#: (:data:`ADDED_KINDS_7_8`). Graded positively by
#: :func:`test_the_7_10_0_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_7_10 = frozenset(
    {
        ("runnable", "description"),
        ("data_element", "description"),
        ("operation", "description"),
        ("argument", "description"),
        ("enum_literal", "description"),
        ("record_field", "description"),
    }
)

#: The same contract's ``package`` on the eight graded kinds of the fifteen
#: emitted directly into a root package; the other seven are hidden with their
#: kind or layer. Graded positively by
#: :func:`test_the_7_10_0_package_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_7_10_PACKAGE = frozenset(
    {
        ("swc", "package"),
        ("sender_receiver", "package"),
        ("client_server", "package"),
        ("base_type", "package"),
        ("value_type", "package"),
        ("enum", "package"),
        ("record", "package"),
        ("array", "package"),
    }
)

#: Contract ``7.11.0``'s fields on graded kinds, each its kind's last field; the
#: port's own ``init`` sits on ``port_com_spec``, one of :data:`ADDED_KINDS_7_11`.
#: Graded positively by :func:`test_the_7_11_0_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_7_11 = frozenset(
    {
        ("sender_receiver", "is_service"),
        ("client_server", "is_service"),
        ("data_element", "sw_impl_policy"),
        ("data_element", "sw_calibration_access"),
        ("port", "com_specs"),
        ("timing_event", "disabled_modes"),
        ("runnable", "symbol"),
    }
)

#: Contract ``8.0.0``'s ``application_type`` on a data element and an argument:
#: exactly one of it and ``type`` is set, and ``type`` keeps its graded row.
#: Graded positively by :func:`test_the_8_0_0_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_8_0 = frozenset({("data_element", "application_type"), ("argument", "application_type")})

#: Contract ``8.0.0``'s ``compu_method_ref`` on the enumeration and the value
#: type; the application type's and the reference type's sit on added kinds.
#: Graded positively by
#: :func:`test_the_8_0_0_elements_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_8_0_ELEMENTS = frozenset({("enum", "compu_method_ref"), ("value_type", "compu_method_ref")})

#: Contract ``8.0.0``'s stated names on graded kinds; the other events' and the
#: access entries' ``name`` sit on :data:`ADDED_KINDS_7_7` and
#: :data:`ADDED_KINDS_3_2`, the stub's two lists on :data:`ADDED_KINDS_7_5`.
#: Graded positively by
#: :func:`test_the_8_0_0_stated_names_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_8_0_NAMES = frozenset({("swc", "behavior_name"), ("timing_event", "name"), ("init_event", "name")})

#: Contract ``9.0.0``'s task ``partition``, in the slot of the retired ``core``
#: (still hidden in :data:`UNGRADED_FIELDS`). Graded positively by
#: :func:`test_the_9_0_0_partition_addition_is_exactly_the_added_row`.
ADDED_FIELDS_9_0 = frozenset({("task", "partition")})

#: Contract ``9.0.0``'s ``instance`` selector beside ``swc`` on a task's runnable
#: entry and a sequence step. Graded positively by
#: :func:`test_the_9_0_0_instance_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_9_0_INSTANCE = frozenset({("task_runnable_ref", "instance"), ("sequence_step", "instance")})

#: A runnable's ``wcet`` and ``wcet_type`` (contract ``10.0.0``).
ADDED_FIELDS_10_0 = frozenset({("runnable", "wcet"), ("runnable", "wcet_type")})

#: Contract ``11.0.0``'s trace links: ``satisfies``, last, on every graded kind
#: that has it, and the requirement's ``version``, ``source`` and
#: ``derived_from``. The other kinds' ``satisfies`` sit on ungraded or added kinds.
#: Graded positively by :func:`test_the_11_0_0_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_11_0 = frozenset(
    {
        ("swc", "satisfies"),
        ("port", "satisfies"),
        ("runnable", "satisfies"),
        ("sender_receiver", "satisfies"),
        ("client_server", "satisfies"),
        ("task", "satisfies"),
        ("requirement", "version"),
        ("requirement", "source"),
        ("requirement", "derived_from"),
    }
)

#: Contract ``11.1.0``'s requirement provenance, ``document``, ``baseline`` and
#: ``revision``, after ``asil``, and ``stub`` after them. Graded positively by
#: :func:`test_the_11_1_0_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_11_1 = frozenset(
    {("requirement", "document"), ("requirement", "baseline"), ("requirement", "revision"), ("requirement", "stub")}
)

#: Contract ``12.1.0``'s component ``variables`` and runnable ``reads_variables``
#: and ``writes_variables``. Graded positively by
#: :func:`test_the_12_1_0_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_12_1 = frozenset({("swc", "variables"), ("runnable", "reads_variables"), ("runnable", "writes_variables")})

#: Contract ``12.2.0``'s component ``exclusive_areas`` and runnable ``can_enter``
#: and ``runs_inside``. Graded positively by
#: :func:`test_the_12_2_0_addition_is_exactly_the_added_rows`.
ADDED_FIELDS_12_2 = frozenset({("swc", "exclusive_areas"), ("runnable", "can_enter"), ("runnable", "runs_inside")})

#: The fields outside the ungraded layers whose declaration changed when
#: ``hardware`` replaced ``resources``: ``task.core`` became a reference to a core
#: by name and a component's memory map keys an identity. Both are
#: ``(kind, field)`` pairs, the key of the frozen field and collection tables.
UNGRADED_FIELDS = frozenset({("task", "core"), ("swc", "memory")})

#: Contract ``3.2.0``'s three access entry kinds. Unlike :data:`UNGRADED_KINDS`, no
#: added set hides a row the frozen table carries;
#: :func:`test_the_frozen_exclusion_is_exactly_the_replaced_layer` asserts that
#: direction, and :func:`test_the_access_additions_are_exactly_the_added_rows`
#: grades what these project.
ADDED_KINDS_3_2 = frozenset({"data_read", "data_write", "operation_call"})

#: Contract ``3.2.0``'s three access lists: pairs on a kind the frozen table carries,
#: so they cannot be hidden one granularity up as the added kinds' own fields are.
ADDED_FIELDS_3_2 = frozenset({("runnable", "reads"), ("runnable", "writes"), ("runnable", "calls")})

#: Contract ``4.0.0``'s fields on graded kinds, with ``compositions``: a connector's
#: and a connector endpoint's ``composition``, and a component's three stub
#: fields. Graded positively by
#: :func:`test_the_compositions_additions_are_exactly_the_added_rows`.
ADDED_FIELDS_4_0 = frozenset(
    {
        ("connector", "composition"),
        ("connector_endpoint", "composition"),
        ("swc", "external"),
        ("swc", "external_ref"),
        ("swc", "external_dest"),
    }
)

#: Contract ``4.4.0``'s safety integrity level on the requirement and on the component.
ADDED_FIELDS_4_4 = frozenset({("swc", "asil"), ("requirement", "asil")})

#: Contract ``4.5.0``'s three more, keyed by a MEMBER kind, as ``FIELD_ANNOTATIONS`` keys every row
#: (``("runnable", "reads")`` above).
ADDED_FIELDS_4_5 = frozenset({("port", "measures"), ("task", "coexistence"), ("data_element", "asil")})

#: Contract ``5.0.0``'s ``reference_type``. Its ``base_type`` and ``value_type``,
#: which replaced ``primitive`` and ``alias`` with the same fields, were
#: re-recorded under the new names and stay graded. Graded positively by
#: :func:`test_the_reference_type_addition_is_exactly_the_added_rows`.
ADDED_KINDS_5_0 = frozenset({"reference_type"})

#: Contract ``7.5.0``'s external stub kinds. Graded positively by
#: :func:`test_the_external_stub_addition_is_exactly_the_added_rows`.
ADDED_KINDS_7_5 = frozenset({"external_interface", "external_type"})

#: Contract ``7.7.0``'s ``operation_event``, the runnable's third event kind.
#: Graded positively by :func:`test_the_operation_event_addition_is_exactly_the_added_rows`.
ADDED_KINDS_7_7 = frozenset({"operation_event"})

#: Contract ``7.8.0``'s three implementation kinds and the member kind
#: ``implementation_element`` of the record's ``elements``. Graded positively by
#: :func:`test_the_implementation_composite_addition_is_exactly_the_added_rows`.
ADDED_KINDS_7_8 = frozenset({"implementation_record", "implementation_array", "pointer", "implementation_element"})

#: Contract ``7.11.0``'s member kinds: ``port_com_spec`` (a port's ``com_specs``)
#: and ``disabled_mode`` (a timing event's ``disabled_modes``). Graded positively
#: by :func:`test_the_7_11_0_addition_is_exactly_the_added_rows`.
ADDED_KINDS_7_11 = frozenset({"port_com_spec", "disabled_mode"})

#: The kinds of contract ``8.0.0``'s three added layers. Graded positively by
#: :func:`test_the_8_0_0_elements_addition_is_exactly_the_added_rows`.
ADDED_KINDS_8_0_ELEMENTS = frozenset({"unit", "texttable", "linear", "table_entry", "data_constr"})

#: Contract ``8.0.0``'s scale limits: the member kinds of a ``linear``'s and a
#: ``conversion``'s ``limits`` and of their bounds. Both ``limits`` fields sit on
#: added kinds, so no field set grows. Graded positively by
#: :func:`test_the_8_0_0_scale_limits_addition_is_exactly_the_added_rows`.
ADDED_KINDS_8_0_LIMITS = frozenset(
    {"linear_limits", "linear_limit_bound", "conversion_limits", "conversion_limit_bound"}
)

#: Contract ``12.1.0``'s inter-runnable ``variable`` and its two access kinds.
#: Graded positively by :func:`test_the_12_1_0_addition_is_exactly_the_added_rows`.
ADDED_KINDS_12_1 = frozenset({"variable", "variable_read", "variable_write"})

#: Contract ``12.2.0``'s ``exclusive_area`` and its two entry kinds. Graded
#: positively by :func:`test_the_12_2_0_addition_is_exactly_the_added_rows`.
ADDED_KINDS_12_2 = frozenset({"exclusive_area", "can_enter_area", "runs_inside_area"})

#: Contract ``12.3.0``'s ``data_event``, the runnable's fourth event kind; it adds
#: no field to a graded kind. Graded positively by
#: :func:`test_the_12_3_0_addition_is_exactly_the_added_rows`.
ADDED_KINDS_12_3 = frozenset({"data_event"})

#: Contract ``12.7.0``'s ``call_returned_event``, the runnable's fifth event kind;
#: the fields it adds sit on added kinds. Graded positively by
#: :func:`test_the_12_7_0_addition_is_exactly_the_added_rows`.
ADDED_KINDS_12_7 = frozenset({"call_returned_event"})

#: Every kind the oracle never carried: no before-state, so excluded by name on
#: the projected side only.
ADDED_KINDS = (
    ADDED_KINDS_3_2
    | ADDED_KINDS_5_0
    | ADDED_KINDS_7_0
    | ADDED_KINDS_7_5
    | ADDED_KINDS_7_7
    | ADDED_KINDS_7_8
    | ADDED_KINDS_7_11
    | ADDED_KINDS_8_0_ELEMENTS
    | ADDED_KINDS_8_0_LIMITS
    | ADDED_KINDS_12_1
    | ADDED_KINDS_12_2
    | ADDED_KINDS_12_3
    | ADDED_KINDS_12_7
)

#: Every ``(kind, field)`` pair hidden from the two field-keyed comparisons, in
#: one name: the pairs whose rows really disagree and the pairs the oracle never
#: carried. :func:`test_the_frozen_exclusion_is_exactly_the_replaced_layer`
#: derives this whole union from the two documents.
HIDDEN_FIELDS = (
    UNGRADED_FIELDS
    | ADDED_FIELDS_3_2
    | ADDED_FIELDS_4_0
    | ADDED_FIELDS_4_4
    | ADDED_FIELDS_4_5
    | ADDED_FIELDS_7_0
    | ADDED_FIELDS_7_6
    | ADDED_FIELDS_7_7
    | ADDED_FIELDS_7_9
    | ADDED_FIELDS_7_10
    | ADDED_FIELDS_7_10_PACKAGE
    | ADDED_FIELDS_7_11
    | ADDED_FIELDS_8_0
    | ADDED_FIELDS_8_0_ELEMENTS
    | ADDED_FIELDS_8_0_NAMES
    | ADDED_FIELDS_9_0
    | ADDED_FIELDS_9_0_INSTANCE
    | ADDED_FIELDS_10_0
    | ADDED_FIELDS_11_0
    | ADDED_FIELDS_11_1
    | ADDED_FIELDS_12_1
    | ADDED_FIELDS_12_2
)


def _graded_layer_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["name"] not in UNGRADED_LAYERS]


def _graded_kind_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["kind"] not in UNGRADED_KINDS | ADDED_KINDS]


def _graded_field_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row["kind"] not in UNGRADED_KINDS | ADDED_KINDS and (row["kind"], row["field"]) not in HIDDEN_FIELDS
    ]


# ---------------------------------------------------------------------------
# The six tables, against the frozen oracle
# ---------------------------------------------------------------------------


def test_the_projected_layer_specs_are_the_frozen_table() -> None:
    """Every layer fact, ``optional`` and ``absent_description`` included, is the frozen table's.

    A projection that dropped either would leave the ``discover`` response's
    absent-layer prose with nothing behind it.
    """
    assert _graded_layer_rows(_serialize_layer_specs(_TABLES.layer_specs)) == _graded_layer_rows(
        FROZEN_TABLES["LAYER_SPECS"]
    )


def test_the_projected_kind_names_are_the_frozen_table_in_order() -> None:
    """Class -> kind name, in the contract's order, which the discovery vectors publish a layer's kinds in."""
    assert _graded_kind_rows(_serialize_kind_names(_TABLES.kind_names)) == _graded_kind_rows(
        FROZEN_TABLES["KIND_NAMES"]
    )


def test_the_projected_placement_fields_are_the_frozen_table() -> None:
    assert _serialize_placement_fields(_TABLES.placement_fields) == FROZEN_TABLES["PLACEMENT_FIELDS"]


def test_the_projected_kind_descriptions_are_the_frozen_table() -> None:
    """Every kind's description, compared as an ordered list of items, which a bare ``dict == dict`` is not.

    A re-recorded row is a compared row; hiding it in :data:`UNGRADED_KINDS` would not be.
    """
    hidden = UNGRADED_KINDS | ADDED_KINDS
    projected = [item for item in _TABLES.kind_descriptions.items() if item[0] not in hidden]
    frozen = [item for item in FROZEN_TABLES["KIND_DESCRIPTIONS"].items() if item[0] not in hidden]
    assert projected == frozen


def test_the_projected_field_annotations_are_the_frozen_table() -> None:
    """Description, unit, reference and immutability override match the frozen table.

    The projection writes an override only where the contract's declared
    ``immutable`` disagrees with what ``_is_immutable`` derives, so an invented
    override shows up as an extra row;
    :func:`test_the_one_immutability_override_is_the_base_types_kind_literal` counts
    them over the whole projection.
    """
    assert _graded_field_rows(_serialize_field_annotations(_FIELD_ANNOTATIONS)) == _graded_field_rows(
        FROZEN_TABLES["FIELD_ANNOTATIONS"]
    )


def test_the_one_immutability_override_is_the_base_types_kind_literal() -> None:
    """The one override is ``base_type.kind``.

    Its layer declares no discriminator, so ``_is_immutable`` derives ``kind``
    mutable, while the contract keeps it ``immutable: true``: a one-value literal no
    update can change. Counted over the whole projection, because an override on an
    ungraded (say, hardware) field sits in the rows the scoped comparison skips.
    """
    overriding = [
        (kind, field) for (kind, field), annotation in _FIELD_ANNOTATIONS.items() if annotation.immutable is not None
    ]
    assert overriding == [("base_type", "kind")], (
        f"a field's declared immutability disagrees with the derivation: {overriding}"
    )


def test_the_projected_collection_specs_are_the_frozen_rows() -> None:
    """The collections match the frozen rows as a mapping (see the module docstring).

    Both counts are asserted, so a collection cannot arrive unnoticed behind the
    exclusion.
    """
    projected = _rows_by_key(_graded_field_rows(_serialize_collection_specs(_TABLES.collection_specs)))
    frozen = _rows_by_key(_graded_field_rows(FROZEN_TABLES["COLLECTION_SPECS"]))
    assert projected == frozen
    # The graded collections: the frozen rows outside the hidden kinds and fields.
    assert len(projected) == len(frozen) == 10
    # Every collection the contract declares; an added collection moves it.
    assert len(_TABLES.collection_specs) == 47


def _fields_the_frozen_tables_disagree_about() -> frozenset[tuple[str, str]]:
    """Every ``(kind, field)`` whose frozen and projected rows differ, in either field-keyed table.

    This is what :data:`HIDDEN_FIELDS` must equal. It does not reach a pair whose
    kind is in :data:`UNGRADED_KINDS` or :data:`ADDED_KINDS`: those rows are excluded
    by the kind sets, which the caller derives from the two contracts.
    """
    disagreements: set[tuple[str, str]] = set()
    for projected_rows, frozen_rows in (
        (_serialize_field_annotations(_FIELD_ANNOTATIONS), FROZEN_TABLES["FIELD_ANNOTATIONS"]),
        (_serialize_collection_specs(_TABLES.collection_specs), FROZEN_TABLES["COLLECTION_SPECS"]),
    ):
        projected = _rows_by_key(projected_rows)
        frozen = _rows_by_key(frozen_rows)
        for key in set(projected) | set(frozen):
            if key[0] in UNGRADED_KINDS | ADDED_KINDS:
                continue
            if projected.get(key) != frozen.get(key):
                disagreements.add(key)
    return frozenset(disagreements)


def test_the_frozen_exclusion_is_exactly_the_replaced_layer() -> None:
    """The ungraded and added sets are read off the two documents rather than trusted.

    A filter nothing checks grows, and every row it hides stops being graded silently.
    """
    frozen_layers = {row["name"] for row in FROZEN_TABLES["LAYER_SPECS"]}
    shipped_layers = {layer.name for layer in production_registry().layers}
    assert frozen_layers ^ shipped_layers == UNGRADED_LAYERS

    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    shipped_kinds = set(production_registry().kinds)
    # ``memory`` is the one kind name both contracts declare with two meanings, so
    # the symmetric difference cannot find it.
    assert (frozen_kinds ^ shipped_kinds) | {"memory"} == UNGRADED_KINDS | ADDED_KINDS
    # An added kind is on the shipped side only. ``base_type`` and ``value_type``
    # (once ``primitive`` and ``alias``) are on neither side: their rows were
    # re-recorded under the new names and stay graded.
    for added in (
        ADDED_KINDS_3_2,
        ADDED_KINDS_5_0,
        ADDED_KINDS_7_5,
        ADDED_KINDS_7_7,
        ADDED_KINDS_7_11,
        ADDED_KINDS_8_0_ELEMENTS,
        ADDED_KINDS_8_0_LIMITS,
        ADDED_KINDS_12_1,
        ADDED_KINDS_12_2,
        ADDED_KINDS_12_3,
        ADDED_KINDS_12_7,
    ):
        assert added.isdisjoint(frozen_kinds) and added <= shipped_kinds
        assert added.isdisjoint(UNGRADED_KINDS)
    assert ADDED_KINDS_3_2.isdisjoint(ADDED_KINDS_5_0)
    assert ADDED_KINDS_7_5.isdisjoint(ADDED_KINDS_3_2 | ADDED_KINDS_5_0 | ADDED_KINDS_7_0)
    assert ADDED_KINDS_7_7.isdisjoint(ADDED_KINDS_3_2 | ADDED_KINDS_5_0 | ADDED_KINDS_7_0 | ADDED_KINDS_7_5)
    assert {"base_type", "value_type"} <= frozen_kinds & shipped_kinds
    assert not {"primitive", "alias"} & (frozen_kinds | shipped_kinds)

    frozen_fields = {(row["kind"], row["field"]) for row in FROZEN_TABLES["FIELD_ANNOTATIONS"]}
    outside = {pair for pair in UNGRADED_FIELDS if pair[0] not in UNGRADED_KINDS}
    assert outside == UNGRADED_FIELDS, (
        "a pair here is already hidden by UNGRADED_KINDS, so naming it says nothing: "
        f"{sorted(UNGRADED_FIELDS - outside)}"
    )
    assert outside <= frozen_fields
    # The added fields are pairs the frozen table does NOT carry, on a kind it
    # does, so no kind set can hide them.
    for added in (
        ADDED_FIELDS_3_2,
        ADDED_FIELDS_4_0,
        ADDED_FIELDS_4_4,
        ADDED_FIELDS_4_5,
        ADDED_FIELDS_7_6,
        ADDED_FIELDS_7_7,
        ADDED_FIELDS_7_9,
        ADDED_FIELDS_7_10,
        ADDED_FIELDS_7_10_PACKAGE,
        ADDED_FIELDS_7_11,
        ADDED_FIELDS_8_0,
        ADDED_FIELDS_8_0_ELEMENTS,
        ADDED_FIELDS_8_0_NAMES,
        ADDED_FIELDS_9_0,
        ADDED_FIELDS_9_0_INSTANCE,
        ADDED_FIELDS_10_0,
        ADDED_FIELDS_11_0,
        ADDED_FIELDS_11_1,
        ADDED_FIELDS_12_1,
        ADDED_FIELDS_12_2,
    ):
        assert added.isdisjoint(frozen_fields)
        assert {pair[0] for pair in added} <= frozen_kinds - UNGRADED_KINDS
        assert added.isdisjoint(UNGRADED_FIELDS)
    assert ADDED_FIELDS_3_2.isdisjoint(ADDED_FIELDS_4_0)
    assert ADDED_FIELDS_4_4.isdisjoint(ADDED_FIELDS_3_2 | ADDED_FIELDS_4_0)
    assert ADDED_FIELDS_4_5.isdisjoint(ADDED_FIELDS_4_4 | ADDED_FIELDS_3_2 | ADDED_FIELDS_4_0)

    # The assertions above only say each named pair is well-formed; none would
    # notice a hidden pair the two tables agree about, which drops a row out of
    # the oracle with nothing failing. So the set is derived.
    assert _fields_the_frozen_tables_disagree_about() == HIDDEN_FIELDS, (
        "UNGRADED_FIELDS | ADDED_FIELDS_3_2 | ADDED_FIELDS_4_0 | ADDED_FIELDS_4_4 "
        "is not the set of rows that differ: "
        f"unexplained {sorted(_fields_the_frozen_tables_disagree_about() - HIDDEN_FIELDS)}, "
        f"needlessly hidden {sorted(HIDDEN_FIELDS - _fields_the_frozen_tables_disagree_about())}"
    )


#: What each access addition projects, written out by hand: the projection's
#: expected value may not be read from the document it projects.
_ADDED_KIND_CLASSES_3_2 = {
    "data_read": "swcraft_tools.schema.DataReadRef",
    "data_write": "swcraft_tools.schema.DataWriteRef",
    "operation_call": "swcraft_tools.schema.OperationCallRef",
}
_ADDED_COLLECTION_IDENTITIES_3_2 = {
    ("runnable", "reads"): ["port", "element"],
    ("runnable", "writes"): ["port", "element"],
    ("runnable", "calls"): ["port", "operation"],
}
_ADDED_KIND_FIELDS_3_2 = {
    # Each entry's optional stated `name` comes last.
    "data_read": ["port", "element", "by", "name"],
    # A write's form, and a call's synchrony with its three companions.
    "data_write": ["port", "element", "by", "name"],
    "operation_call": ["port", "operation", "asynchronous", "poll", "result", "timeout", "name"],
}


def test_the_access_additions_are_exactly_the_added_rows() -> None:
    """The access additions' rows in the four tables, which the frozen oracle cannot grade.

    An access names a port of its OWN component, not an element of another layer,
    so no field carries a ``reference``; ``by`` is deliberately absent from
    ``reads``' identity. What it does not reach: the descriptions' wording (the
    prose oracle is ``test_model_contract_conformance.py``'s, over the ``full``
    discover vector), and the ``omitAtDefault`` fact, which is not a projected
    table -- :func:`test_the_omit_at_default_field_is_projected_onto_field_facts`
    holds it.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"]
        not in frozen_kinds
        | UNGRADED_KINDS
        | ADDED_KINDS_5_0
        | ADDED_KINDS_7_0
        | ADDED_KINDS_7_5
        | ADDED_KINDS_7_7
        | ADDED_KINDS_7_8
        | ADDED_KINDS_7_11
        | ADDED_KINDS_8_0_ELEMENTS
        | ADDED_KINDS_8_0_LIMITS
        | ADDED_KINDS_12_1
        | ADDED_KINDS_12_2
        | ADDED_KINDS_12_3
        | ADDED_KINDS_12_7
    }
    assert added == _ADDED_KIND_CLASSES_3_2
    order = [row["kind"] for row in projected_names]
    # The runnable's event kinds come first, `call_returned_event` the last of them.
    first = order.index("call_returned_event") + 1
    assert order[first : first + 3] == ["data_read", "data_write", "operation_call"]

    for kind in ADDED_KINDS_3_2:
        assert _TABLES.kind_descriptions[kind].strip(), kind

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    added_rows = {key for key in annotations if key[0] in ADDED_KINDS_3_2} | ADDED_FIELDS_3_2
    assert {key for key in annotations if key[0] in ADDED_KINDS_3_2} == {
        (kind, field) for kind, names in _ADDED_KIND_FIELDS_3_2.items() for field in names
    }
    for key in sorted(added_rows):
        row = annotations[key]
        # A call's `timeout` is the one with a unit.
        unit = "seconds" if key == ("operation_call", "timeout") else None
        assert row["description"] and row["unit"] == unit and row["reference"] is None and row["immutable"] is None, row

    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    for key, identity in _ADDED_COLLECTION_IDENTITIES_3_2.items():
        assert collections[key] == {
            "kind": key[0],
            "field": key[1],
            "member_identity": "composite",
            "identity_field": None,
            "identity_fields": identity,
            "description": None,
        }
    assert {key for key in collections if key not in _rows_by_key(FROZEN_TABLES["COLLECTION_SPECS"])} == (
        set(_ADDED_COLLECTION_IDENTITIES_3_2)
        | {("memory", "accessible"), ("port", "measures")}
        | set(_ADDED_COLLECTION_IDENTITIES_4_0)
        # Contract `7.0.0`'s two: a component's list of mapping sets, and a set's
        # pairs, in a layer the oracle predates.
        | {("swc", "data_type_mappings"), ("mapping_set", "maps")}
        # Contract `7.8.0`'s one: an implementation record's elements.
        | {("implementation_record", "elements")}
        # Contract `8.0.0`'s two stub lists: an interface stub's `operations` and
        # `data_elements`.
        | {("external_interface", "operations"), ("external_interface", "data_elements")}
        # Contract `7.11.0`'s two: a port's com-specs and a timing event's
        # disabled modes.
        | {("port", "com_specs"), ("timing_event", "disabled_modes")}
        # Contract `8.0.0`'s elements' one: a texttable's entries.
        | {("texttable", "entries")}
        # Contract `11.0.0`'s thirteen: the twelve `satisfies` lists and a
        # requirement's `derived_from`.
        | {
            (kind, "satisfies")
            for kind in (
                "swc",
                "port",
                "runnable",
                "sender_receiver",
                "client_server",
                "external_interface",
                "composition",
                "task",
                "processor",
                "core",
                "memory",
                "partition",
            )
        }
        | {("requirement", "derived_from")}
        # Contract `12.1.0`'s three: a component's variables and a runnable's two
        # variable access lists.
        | ADDED_FIELDS_12_1
        # Contract `12.2.0`'s three: a component's exclusive areas and a
        # runnable's two area lists.
        | ADDED_FIELDS_12_2
        # Contract `12.3.0`'s one: a data-received event's disabled modes.
        | {("data_event", "disabled_modes")}
    )


#: What each field the compositions added to a graded kind projects, written out
#: by hand for :data:`_ADDED_KIND_CLASSES_3_2`'s reason. The two ``composition``
#: rows name a composition BY NAME, the identity field every other reference
#: uses; the three ``swc`` stub fields name no other layer.
_ADDED_FIELD_REFERENCES_4_0: dict[tuple[str, str], dict[str, str] | None] = {
    ("connector", "composition"): {"layer": "compositions", "identity_field": "name"},
    ("connector_endpoint", "composition"): {"layer": "compositions", "identity_field": "name"},
    ("swc", "external"): None,
    ("swc", "external_ref"): None,
    ("swc", "external_dest"): None,
}

#: The composition's three collections, keyed the way the contract declares
#: them. All three carry ``member_identity`` ``field`` over a single
#: ``identity_field`` — a member is addressed by its own ``name`` — unlike the
#: runnable's ``composite`` access lists above, which have no single key.
_ADDED_COLLECTION_IDENTITIES_4_0 = {
    ("composition", "ports"): "name",
    ("composition", "members"): "name",
    ("composition", "delegations"): "name",
}


def test_the_compositions_additions_are_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_FIELDS_4_0` and the ``compositions`` kinds project.

    The three ``swc`` stub fields reference nothing: ``external_ref`` and
    ``external_dest`` are raw ARXML paths into a document SWCraft does not own.
    What it does not reach, deliberately: the wording of any description (the
    prose oracle over the ``full`` discover vector), the four kinds' position in
    ``KIND_NAMES`` (the fixture has no position for a kind it never carried), and
    the ``omitAtDefault`` fact on ``composition.root`` and ``swc.external``, which
    :func:`test_the_omit_at_default_field_is_projected_onto_field_facts` holds.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for key, reference in _ADDED_FIELD_REFERENCES_4_0.items():
        row = annotations[key]
        assert row["description"] and row["unit"] is None and row["immutable"] is None, row
        assert row["reference"] == reference, row

    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    # Every shipped kind the fixture lacks, minus the other additions and the
    # hardware kinds, so that a kind arriving on any layer lands here.
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"] not in frozen_kinds | ADDED_KINDS | {"processor", "core", "partition"}
    }
    assert added == {
        "composition": "swcraft_tools.schema.Composition",
        "composition_port": "swcraft_tools.schema.CompositionPort",
        "composition_member": "swcraft_tools.schema.CompositionMember",
        "delegation": "swcraft_tools.schema.Delegation",
    }
    for kind in added:
        assert _TABLES.kind_descriptions[kind].strip(), kind

    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    for key, identity in _ADDED_COLLECTION_IDENTITIES_4_0.items():
        assert collections[key] == {
            "kind": key[0],
            "field": key[1],
            "member_identity": "field",
            "identity_field": identity,
            "identity_fields": [],
            "description": None,
        }


#: What ``reference_type``'s fields project, written out by hand for
#: :data:`_ADDED_KIND_CLASSES_3_2`'s reason.
_ADDED_FIELD_REFERENCES_5_0: dict[str, dict[str, str] | None] = {
    "name": None,
    "kind": None,
    "base": {"layer": "implementation_types", "identity_field": "name"},
    # The texttable it names, before the description.
    "compu_method_ref": {"layer": "compu_methods", "identity_field": "name"},
    "description": None,
    "package": None,
}


def test_the_reference_type_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_5_0` projects in the four tables.

    What it does not reach: the descriptions' wording (the prose oracle over the
    ``full`` discover vector), and which kinds ``base`` may name, which is the
    validator's reference-role rule family and not a projected table.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"]
        not in frozen_kinds
        | UNGRADED_KINDS
        | ADDED_KINDS_3_2
        | ADDED_KINDS_7_0
        | ADDED_KINDS_7_5
        | ADDED_KINDS_7_7
        | ADDED_KINDS_7_8
        | ADDED_KINDS_7_11
        | ADDED_KINDS_8_0_ELEMENTS
        | ADDED_KINDS_8_0_LIMITS
        | ADDED_KINDS_12_1
        | ADDED_KINDS_12_2
        | ADDED_KINDS_12_3
        | ADDED_KINDS_12_7
    }
    assert added == {"reference_type": "swcraft_tools.schema.ReferenceDataType"}
    order = [row["kind"] for row in projected_names]
    assert order[order.index("value_type") + 1] == "reference_type"
    assert _TABLES.kind_descriptions["reference_type"].strip()

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    assert [field for kind, field in annotations if kind == "reference_type"] == list(_ADDED_FIELD_REFERENCES_5_0)
    for field, reference in _ADDED_FIELD_REFERENCES_5_0.items():
        row = annotations[("reference_type", field)]
        assert row["description"] and row["unit"] is None and row["immutable"] is None, row
        assert row["reference"] == reference, row

    assert not [key for key in _TABLES.collection_specs if key[0] == "reference_type"]


def test_the_external_stub_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_7_5` projects in the four tables.

    No field carries a reference: ``external_ref`` is a literal AUTOSAR path, never
    an edge inside the model. What it does not reach: which ``external_dest``
    values each layer admits, which is the validator's
    ``external-incomplete-reference`` pair and not a projected table.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"]
        not in frozen_kinds
        | UNGRADED_KINDS
        | ADDED_KINDS_3_2
        | ADDED_KINDS_5_0
        | ADDED_KINDS_7_0
        | ADDED_KINDS_7_7
        | ADDED_KINDS_7_8
        | ADDED_KINDS_7_11
        | ADDED_KINDS_8_0_ELEMENTS
        | ADDED_KINDS_8_0_LIMITS
        | ADDED_KINDS_12_1
        | ADDED_KINDS_12_2
        | ADDED_KINDS_12_3
        | ADDED_KINDS_12_7
    }
    assert added == {
        "external_interface": "swcraft_tools.schema.ExternalInterface",
        "external_type": "swcraft_tools.schema.ExternalDataType",
    }
    order = [row["kind"] for row in projected_names]
    assert order[order.index("client_server") + 1] == "external_interface"
    assert order[order.index("reference_type") + 1] == "external_type"

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    # An interface stub names the members its ports use, after the reference pair.
    stub_lists = {"external_interface": ["operations", "data_elements"], "external_type": []}
    # The interface stub's `satisfies` comes last.
    linked = {"external_interface": ["satisfies"], "external_type": []}
    for kind in sorted(ADDED_KINDS_7_5):
        assert _TABLES.kind_descriptions[kind].strip(), kind
        declared = ["name", "kind", "external_ref", "external_dest", *stub_lists[kind], "description", *linked[kind]]
        assert [field for owner, field in annotations if owner == kind] == declared
        for field in declared:
            row = annotations[(kind, field)]
            assert row["description"] and row["unit"] is None and row["reference"] is None, row
        assert sorted(key[1] for key in _TABLES.collection_specs if key[0] == kind) == sorted(
            [*stub_lists[kind], *linked[kind]]
        )
        for field in stub_lists[kind]:
            assert _TABLES.collection_specs[(kind, field)].member_identity == "whole_value", field


def test_the_operation_event_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_7_7` and :data:`ADDED_FIELDS_7_7` project in the four tables.

    The port and the operation resolve inside the component and its port's
    interface (``check-model``'s resolution), never by a layer reference. What it
    does not reach: the descriptions' wording (the prose oracle over the ``full``
    discover vector), and the ``omitAtDefault`` fact on ``concurrent``, which
    :func:`test_the_omit_at_default_field_is_projected_onto_field_facts` holds.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"]
        not in frozen_kinds
        | UNGRADED_KINDS
        | ADDED_KINDS_3_2
        | ADDED_KINDS_5_0
        | ADDED_KINDS_7_0
        | ADDED_KINDS_7_5
        | ADDED_KINDS_7_8
        | ADDED_KINDS_7_11
        | ADDED_KINDS_8_0_ELEMENTS
        | ADDED_KINDS_8_0_LIMITS
        | ADDED_KINDS_12_1
        | ADDED_KINDS_12_2
        | ADDED_KINDS_12_3
        | ADDED_KINDS_12_7
    }
    assert added == {"operation_event": "swcraft_tools.schema.OperationEvent"}
    order = [row["kind"] for row in projected_names]
    assert order[order.index("init_event") + 1] == "operation_event"
    assert _TABLES.kind_descriptions["operation_event"].strip()

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    # The event's optional stated `name` comes last.
    assert [field for owner, field in annotations if owner == "operation_event"] == [
        "type",
        "port",
        "operation",
        "name",
    ]
    keys = [("operation_event", "type"), ("operation_event", "port"), ("operation_event", "operation")]
    for key in keys + sorted(ADDED_FIELDS_7_7):
        row = annotations[key]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row
    assert not [key for key in _TABLES.collection_specs if key[0] == "operation_event"]


def test_the_7_9_0_addition_is_exactly_the_added_row() -> None:
    """The component's ``handle_termination_and_restart`` follows ``asil``, described, with no unit and no reference.

    What it does not reach: the description's wording, which the prose oracle over
    the ``full`` discover vector holds.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    swc_fields = [field for owner, field in annotations if owner == "swc"]
    assert swc_fields[swc_fields.index("asil") + 1] == "handle_termination_and_restart"
    for key in sorted(ADDED_FIELDS_7_9):
        row = annotations[key]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row


def test_the_9_0_0_partition_addition_is_exactly_the_added_row() -> None:
    """The task's ``partition`` takes the retired ``core``'s slot after ``priority`` and references ``hardware``.

    Described, with no unit and no immutability override; the task's ``core`` is
    gone, and the partition's own ``core`` is a second reference within the layer.
    What it does not reach: the wording, which the prose oracle over the ``full``
    discover vectors holds.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    task_fields = [field for owner, field in annotations if owner == "task"]
    assert "core" not in task_fields
    assert task_fields[task_fields.index("priority") + 1] == "partition"
    for key in sorted(ADDED_FIELDS_9_0):
        row = annotations[key]
        assert row["description"] and row["unit"] is None and row["immutable"] is None, row
        assert row["reference"] == {"layer": "hardware", "identity_field": "name"}, row
    partition_core = annotations[("partition", "core")]
    assert partition_core["reference"] == {"layer": "hardware", "identity_field": "name"}, partition_core


def test_the_9_0_0_instance_addition_is_exactly_the_added_rows() -> None:
    """Each kind's ``instance`` follows its ``swc`` with no reference, and both collections key on the triple.

    Described, with no unit and no override: a path of member names is not a
    declared reference, so ``impact`` and ``query --dependents`` do not follow it,
    and the derivation makes it immutable as a member identity field. What it does
    not reach: the wording, which the prose oracle over the ``full`` discover
    vectors holds.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, field in sorted(ADDED_FIELDS_9_0_INSTANCE):
        kind_fields = [name for owner, name in annotations if owner == kind]
        assert kind_fields[kind_fields.index("swc") + 1] == field, kind
        row = annotations[(kind, field)]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row
        assert row["immutable"] is None, row
    specs = {(row["kind"], row["field"]): row for row in _serialize_collection_specs(_TABLES.collection_specs)}
    for key in (("task", "runnables"), ("sequence", "steps")):
        assert specs[key]["identity_fields"] == ["swc", "instance", "runnable"], key


def test_the_11_0_0_addition_is_exactly_the_added_rows() -> None:
    """Each graded kind's ``satisfies`` comes last, and the requirement's three follow ``req_type``.

    Each is described, with no unit, no reference (a link is a relation, never a
    ``reference``) and no override; each ``satisfies`` and ``derived_from`` is a
    whole-value collection. What it does not reach: the wording, which the prose
    oracle over the ``full`` discover vectors holds.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    for kind, field in sorted(ADDED_FIELDS_11_0):
        row = annotations[(kind, field)]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row
        assert row["immutable"] is None, row
        if field == "satisfies":
            assert [name for owner, name in annotations if owner == kind][-1] == field, kind
        if field in {"satisfies", "derived_from"}:
            assert collections[(kind, field)]["member_identity"] == "whole_value", (kind, field)
    requirement = [name for owner, name in annotations if owner == "requirement"]
    # Its own prefix only: the fields after `asil` are `ADDED_FIELDS_11_1`.
    assert requirement[:8] == [
        "id",
        "title",
        "description",
        "req_type",
        "version",
        "source",
        "derived_from",
        "asil",
    ]


def test_the_11_1_0_addition_is_exactly_the_added_rows() -> None:
    """The requirement's last four fields are ``document``, ``baseline``, ``revision`` and ``stub``.

    Each is described, with no unit, no reference, no override and no collection:
    plain scalars name nothing in another layer. What it does not reach: the
    wording, which the prose oracle over the ``full`` discover vectors holds.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    for kind, field in sorted(ADDED_FIELDS_11_1):
        row = annotations[(kind, field)]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row
        assert row["immutable"] is None, row
        assert (kind, field) not in collections, (kind, field)
    requirement = [name for owner, name in annotations if owner == "requirement"]
    assert requirement[-4:] == ["document", "baseline", "revision", "stub"]


#: The fields each of :data:`ADDED_KINDS_12_1` declares, in contract order, with
#: the reference each names.
_ADDED_FIELDS_12_1_KINDS: dict[str, dict[str, dict[str, str] | None]] = {
    "variable": {
        "name": None,
        "access": None,
        "application_type": {"layer": "application_types", "identity_field": "name"},
        "implementation_type": {"layer": "implementation_types", "identity_field": "name"},
        "init": None,
        "description": None,
        "sw_calibration_access": None,
    },
    "variable_read": {"variable": None, "name": None},
    "variable_write": {"variable": None, "name": None},
}


def test_the_12_1_0_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_12_1` and :data:`ADDED_FIELDS_12_1` project in the four tables.

    What it does not reach: the descriptions' wording, which the prose oracle over
    the ``full`` discover vector holds, and the member reference from an access to
    a variable, which is no projected table.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"] not in frozen_kinds | UNGRADED_KINDS | (ADDED_KINDS - ADDED_KINDS_12_1)
    }
    assert added == {
        "variable": "swcraft_tools.schema.Variable",
        "variable_read": "swcraft_tools.schema.VariableReadRef",
        "variable_write": "swcraft_tools.schema.VariableWriteRef",
    }
    order = [row["kind"] for row in projected_names]
    start = order.index("operation_call") + 1
    assert order[start : start + 3] == ["variable", "variable_read", "variable_write"]

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, declared in _ADDED_FIELDS_12_1_KINDS.items():
        assert _TABLES.kind_descriptions[kind].strip(), kind
        assert [field for owner, field in annotations if owner == kind] == list(declared), kind
        for field, reference in declared.items():
            row = annotations[(kind, field)]
            assert row["description"] and row["unit"] is None, row
            assert row["reference"] == reference, row
    swc = [name for owner, name in annotations if owner == "swc"]
    assert swc[swc.index("runnables") + 1] == "variables"
    runnable = [name for owner, name in annotations if owner == "runnable"]
    assert runnable[runnable.index("calls") + 1 : runnable.index("calls") + 3] == [
        "reads_variables",
        "writes_variables",
    ]

    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    assert not [key for key in collections if key[0] in ADDED_KINDS_12_1]
    for (kind, field), identity in {
        ("swc", "variables"): "name",
        ("runnable", "reads_variables"): "variable",
        ("runnable", "writes_variables"): "variable",
    }.items():
        spec = collections[(kind, field)]
        assert (spec["member_identity"], spec["identity_field"]) == ("field", identity), spec


#: The fields each of :data:`ADDED_KINDS_12_2` declares, in contract order, with
#: the reference each names.
_ADDED_FIELDS_12_2_KINDS: dict[str, dict[str, dict[str, str] | None]] = {
    "exclusive_area": {"name": None, "description": None},
    "can_enter_area": {"area": None},
    "runs_inside_area": {"area": None},
}


def test_the_12_2_0_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_12_2` and :data:`ADDED_FIELDS_12_2` project in the four tables.

    What it does not reach: the descriptions' wording, which the prose oracle over
    the ``full`` discover vector holds, and the member reference from an entry to
    an area, which is no projected table.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"] not in frozen_kinds | UNGRADED_KINDS | (ADDED_KINDS - ADDED_KINDS_12_2)
    }
    assert added == {
        "exclusive_area": "swcraft_tools.schema.ExclusiveArea",
        "can_enter_area": "swcraft_tools.schema.CanEnterArea",
        "runs_inside_area": "swcraft_tools.schema.RunsInsideArea",
    }
    order = [row["kind"] for row in projected_names]
    start = order.index("variable_write") + 1
    assert order[start : start + 3] == ["exclusive_area", "can_enter_area", "runs_inside_area"]

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, declared in _ADDED_FIELDS_12_2_KINDS.items():
        assert _TABLES.kind_descriptions[kind].strip(), kind
        assert [field for owner, field in annotations if owner == kind] == list(declared), kind
        for field, reference in declared.items():
            row = annotations[(kind, field)]
            assert row["description"] and row["unit"] is None, row
            assert row["reference"] == reference, row
    swc = [name for owner, name in annotations if owner == "swc"]
    assert swc[swc.index("variables") + 1] == "exclusive_areas"
    runnable = [name for owner, name in annotations if owner == "runnable"]
    at = runnable.index("writes_variables")
    assert runnable[at + 1 : at + 3] == ["can_enter", "runs_inside"]

    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    assert not [key for key in collections if key[0] in ADDED_KINDS_12_2]
    for (kind, field), identity in {
        ("swc", "exclusive_areas"): "name",
        ("runnable", "can_enter"): "area",
        ("runnable", "runs_inside"): "area",
    }.items():
        spec = collections[(kind, field)]
        assert (spec["member_identity"], spec["identity_field"]) == ("field", identity), spec


def test_the_12_3_0_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_12_3` projects in the four tables.

    What it does not reach: the descriptions' wording, which the prose oracle over
    the ``full`` discover vector holds, and the two member references, which are no
    projected table.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"] not in frozen_kinds | UNGRADED_KINDS | (ADDED_KINDS - ADDED_KINDS_12_3)
    }
    assert added == {"data_event": "swcraft_tools.schema.DataEvent"}
    order = [row["kind"] for row in projected_names]
    assert order[order.index("operation_event") + 1] == "data_event"

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    assert _TABLES.kind_descriptions["data_event"].strip()
    assert [field for owner, field in annotations if owner == "data_event"] == [
        "type",
        "port",
        "element",
        "disabled_modes",
        "name",
    ]
    for field in ("type", "port", "element", "disabled_modes", "name"):
        row = annotations[("data_event", field)]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row

    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    assert [key for key in collections if key[0] in ADDED_KINDS_12_3] == [("data_event", "disabled_modes")]
    assert collections[("data_event", "disabled_modes")]["member_identity"] == "whole_value"


def test_the_12_7_0_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_12_7` projects in the four tables.

    What it does not reach: the descriptions' wording, which the prose oracle over
    the ``full`` discover vector holds, and the two member references, which are no
    projected table.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"] not in frozen_kinds | UNGRADED_KINDS | (ADDED_KINDS - ADDED_KINDS_12_7)
    }
    assert added == {"call_returned_event": "swcraft_tools.schema.CallReturnedEvent"}
    order = [row["kind"] for row in projected_names]
    assert order[order.index("data_event") + 1] == "call_returned_event"

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    assert _TABLES.kind_descriptions["call_returned_event"].strip()
    fields = ["type", "port", "operation", "result", "name"]
    assert [field for owner, field in annotations if owner == "call_returned_event"] == fields
    for field in fields:
        row = annotations[("call_returned_event", field)]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row

    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    assert [key for key in collections if key[0] in ADDED_KINDS_12_7] == []


def test_the_7_10_0_addition_is_exactly_the_added_rows() -> None:
    """Each of the six kinds' ``description`` is its last own field, with no unit, reference or override.

    What it does not reach: the wording, which the prose oracle over the ``full``
    discover vectors holds.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, field in sorted(ADDED_FIELDS_7_10):
        # The fields later contracts appended come after it.
        before_7_11 = [
            name
            for owner, name in annotations
            if owner == kind and (owner, name) not in ADDED_FIELDS_7_11 | ADDED_FIELDS_10_0 | ADDED_FIELDS_11_0
        ]
        assert before_7_11[-1] == field, kind
        row = annotations[(kind, field)]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row
        assert row["immutable"] is None, row


def test_the_7_10_0_package_addition_is_exactly_the_added_rows() -> None:
    """Each of the eight kinds' ``package`` is its last own field, with no unit, reference or override.

    The value names a sub-package, not an element of another layer, and an author
    may set it.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, field in sorted(ADDED_FIELDS_7_10_PACKAGE):
        # The fields later contracts appended come after it.
        before_7_11 = [
            name
            for owner, name in annotations
            if owner == kind and (owner, name) not in ADDED_FIELDS_7_11 | ADDED_FIELDS_10_0 | ADDED_FIELDS_11_0
        ]
        assert before_7_11[-1] == field, kind
        row = annotations[(kind, field)]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row
        assert row["immutable"] is None, row


#: What contract ``7.11.0`` adds, written out by hand for
#: :data:`_ADDED_KIND_CLASSES_3_2`'s reason: the two member kinds with their
#: classes and fields in contract order, and each new list's collection identity.
_ADDED_KIND_CLASSES_7_11 = {
    "port_com_spec": "swcraft_tools.schema.PortComSpec",
    "disabled_mode": "swcraft_tools.schema.DisabledMode",
}
_ADDED_KIND_FIELDS_7_11 = {
    "port_com_spec": [
        "data_element",
        "enable_update",
        "alive_timeout",
        "handle_never_received",
        "handle_timeout_type",
        "handle_out_of_range",
        "uses_end_to_end_protection",
        "queue_length",
        # The port's own init value.
        "init",
    ],
    "disabled_mode": ["port", "mode_group_ref", "mode_ref"],
}
_ADDED_COLLECTION_IDENTITIES_7_11 = {
    ("port", "com_specs"): ("field", "data_element"),
    ("timing_event", "disabled_modes"): ("whole_value", None),
}


def test_the_7_11_0_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_7_11` and :data:`ADDED_FIELDS_7_11` project in the four tables.

    What it does not reach: the descriptions' wording (the prose oracle over the
    ``full`` discover vectors), and the ``omitAtDefault`` facts, which
    :func:`test_the_omit_at_default_field_is_projected_onto_field_facts` holds.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"]
        not in frozen_kinds
        | UNGRADED_KINDS
        | ADDED_KINDS_3_2
        | ADDED_KINDS_5_0
        | ADDED_KINDS_7_0
        | ADDED_KINDS_7_5
        | ADDED_KINDS_7_7
        | ADDED_KINDS_7_8
        | ADDED_KINDS_8_0_ELEMENTS
        | ADDED_KINDS_8_0_LIMITS
        | ADDED_KINDS_12_1
        | ADDED_KINDS_12_2
        | ADDED_KINDS_12_3
        | ADDED_KINDS_12_7
    }
    assert added == _ADDED_KIND_CLASSES_7_11
    order = [row["kind"] for row in projected_names]
    assert order[order.index("port") + 1] == "port_com_spec"
    assert order[order.index("timing_event") + 1] == "disabled_mode"

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, declared in _ADDED_KIND_FIELDS_7_11.items():
        assert _TABLES.kind_descriptions[kind].strip(), kind
        assert [field for owner, field in annotations if owner == kind] == declared, kind
        for field in declared:
            row = annotations[(kind, field)]
            assert row["description"] and row["reference"] is None and row["immutable"] is None, row
            assert row["unit"] == ("seconds" if (kind, field) == ("port_com_spec", "alive_timeout") else None), row
    for kind, field in sorted(ADDED_FIELDS_7_11):
        # The last field the kind declared at 7.11.0: the fields later contracts
        # appended come after it.
        later = ADDED_FIELDS_8_0_NAMES | ADDED_FIELDS_10_0 | ADDED_FIELDS_11_0
        assert [name for owner, name in annotations if owner == kind and (kind, name) not in later][-1] in {
            name for owner, name in ADDED_FIELDS_7_11 if owner == kind
        }, kind
        row = annotations[(kind, field)]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row
        assert row["immutable"] is None, row

    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    for key, (identity, identity_field) in _ADDED_COLLECTION_IDENTITIES_7_11.items():
        spec = collections[key]
        assert (spec["member_identity"], spec["identity_field"]) == (identity, identity_field), key


def test_the_8_0_0_addition_is_exactly_the_added_rows() -> None:
    """Each prototype's ``application_type`` directly precedes its ``implementation_type``.

    That is the position the single ``type`` held, which every hand-written dump
    writes the set field in. It is described, with no unit and no override, and
    references ``application_types`` where ``implementation_type`` references
    ``implementation_types``.
    What it does not reach: the description's wording, which the prose oracle over
    the ``full`` discover vectors holds, and "exactly one of the two", which is the
    element-local ``type-form`` rule's and not a projected table.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, field in sorted(ADDED_FIELDS_8_0):
        fields = [name for owner, name in annotations if owner == kind]
        assert fields[fields.index(field) + 1] == "implementation_type", kind
        row = annotations[(kind, field)]
        assert row["description"] and row["unit"] is None and row["immutable"] is None, row
        assert row["reference"] == {"layer": "application_types", "identity_field": "name"}, row
        assert annotations[(kind, "implementation_type")]["reference"] == {
            "layer": "implementation_types",
            "identity_field": "name",
        }


#: What contract ``8.0.0``'s elements project, written out by hand for
#: :data:`_ADDED_KIND_CLASSES_3_2`'s reason: each kind's class, and each kind's
#: fields in contract order with the reference each carries (``None`` for a
#: scalar or a collection).
_ADDED_KIND_CLASSES_8_0_ELEMENTS = {
    "unit": "swcraft_tools.schema.Unit",
    "texttable": "swcraft_tools.schema.TextTable",
    "linear": "swcraft_tools.schema.Linear",
    "table_entry": "swcraft_tools.schema.TableEntry",
    "data_constr": "swcraft_tools.schema.DataConstr",
}
_ADDED_KIND_FIELDS_8_0_ELEMENTS: dict[str, dict[str, dict[str, str] | None]] = {
    "unit": {"name": None, "display_name": None, "description": None, "package": None},
    "texttable": {"name": None, "kind": None, "entries": None, "description": None, "package": None},
    "linear": {
        "name": None,
        "kind": None,
        "unit": {"layer": "units", "identity_field": "name"},
        "factor": None,
        "offset": None,
        # The scale limits.
        "limits": None,
        "description": None,
        "package": None,
    },
    "table_entry": {"name": None, "value": None, "description": None},
    "data_constr": {"name": None, "min": None, "max": None, "level": None, "description": None, "package": None},
}
#: The reference fields the same contract adds to the four type kinds, each the
#: field directly before ``description`` on its kind and in this order, with the
#: layer each names an element of.
_ADDED_REFERENCE_FIELDS_8_0_ELEMENTS: dict[str, dict[str, str]] = {
    "application_type": {
        "unit_ref": "units",
        "compu_method_ref": "compu_methods",
        "data_constr_ref": "data_constraints",
    },
    "enum": {"compu_method_ref": "compu_methods"},
    "value_type": {"compu_method_ref": "compu_methods"},
    "reference_type": {"compu_method_ref": "compu_methods"},
}


def test_the_8_0_0_elements_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_8_0_ELEMENTS` and :data:`ADDED_FIELDS_8_0_ELEMENTS` project.

    What it does not reach: the descriptions' wording, and the form rules (at most
    one of each pair, exactly one of an enumeration's two), which are element-local
    rules and not a projected table.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"] not in frozen_kinds | UNGRADED_KINDS | (ADDED_KINDS - ADDED_KINDS_8_0_ELEMENTS)
    }
    assert added == _ADDED_KIND_CLASSES_8_0_ELEMENTS

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, declared in _ADDED_KIND_FIELDS_8_0_ELEMENTS.items():
        assert _TABLES.kind_descriptions[kind].strip(), kind
        assert [field for owner, field in annotations if owner == kind] == list(declared), kind
        for field, reference in declared.items():
            row = annotations[(kind, field)]
            assert row["description"] and row["unit"] is None and row["reference"] == reference, row
    for kind, references in _ADDED_REFERENCE_FIELDS_8_0_ELEMENTS.items():
        fields = [name for owner, name in annotations if owner == kind]
        at = fields.index("description")
        assert fields[at - len(references) : at] == list(references), kind
        for field, layer in references.items():
            row = annotations[(kind, field)]
            assert row["description"] and row["unit"] is None and row["immutable"] is None, row
            assert row["reference"] == {"layer": layer, "identity_field": "name"}, row

    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    added_collections = {key for key in collections if key not in _rows_by_key(FROZEN_TABLES["COLLECTION_SPECS"])}
    assert ("texttable", "entries") in added_collections
    entries = collections[("texttable", "entries")]
    assert (entries["member_identity"], entries["identity_field"]) == ("field", "name")


#: What the scale limits project, written out by hand for
#: :data:`_ADDED_KIND_CLASSES_3_2`'s reason: each kind's class and its fields in
#: contract order (none carries a reference).
_ADDED_KIND_CLASSES_8_0_LIMITS = {
    "linear_limits": "swcraft_tools.schema.LinearLimits",
    "linear_limit_bound": "swcraft_tools.schema.LinearLimitBound",
    "conversion_limits": "swcraft_tools.schema.ConversionLimits",
    "conversion_limit_bound": "swcraft_tools.schema.ConversionLimitBound",
}
_ADDED_KIND_FIELDS_8_0_LIMITS = {
    "linear_limits": ("lower", "upper"),
    "linear_limit_bound": ("value", "interval"),
    "conversion_limits": ("lower", "upper"),
    "conversion_limit_bound": ("value", "interval"),
}


def test_the_8_0_0_scale_limits_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_8_0_LIMITS` projects in the four tables.

    What it does not reach: the coherence rules (element-local, no projected
    table) and the descriptions' wording.
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"] not in frozen_kinds | UNGRADED_KINDS | (ADDED_KINDS - ADDED_KINDS_8_0_LIMITS)
    }
    assert added == _ADDED_KIND_CLASSES_8_0_LIMITS
    order = [row["kind"] for row in projected_names]
    for owner, prefix in (("linear", "linear"), ("conversion", "conversion")):
        at = order.index(owner)
        assert order[at + 1 : at + 3] == [f"{prefix}_limits", f"{prefix}_limit_bound"], owner

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, declared in _ADDED_KIND_FIELDS_8_0_LIMITS.items():
        assert _TABLES.kind_descriptions[kind].strip(), kind
        assert tuple(field for owner, field in annotations if owner == kind) == declared, kind
        for field in declared:
            row = annotations[(kind, field)]
            assert row["description"] and row["unit"] is None and row["reference"] is None, row
            assert row["immutable"] is None, row
    for owner in ("linear", "conversion"):
        fields = [field for kind, field in annotations if kind == owner]
        assert fields[fields.index("offset") + 1] == "limits", owner
    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    assert not [key for key in collections if key[0] in ADDED_KINDS_8_0_LIMITS]


#: The fields each kind contract ``7.8.0`` adds declares, in contract order,
#: with the reference each carries (``None`` for a scalar or a collection).
_ADDED_FIELDS_7_8: dict[str, dict[str, dict[str, str] | None]] = {
    "implementation_record": {"name": None, "kind": None, "elements": None, "description": None, "package": None},
    "implementation_array": {
        "name": None,
        "kind": None,
        "element_type": {"layer": "implementation_types", "identity_field": "name"},
        "length": None,
        "element_name": None,
        "description": None,
        "package": None,
    },
    "pointer": {
        "name": None,
        "kind": None,
        "target": {"layer": "implementation_types", "identity_field": "name"},
        "description": None,
        "package": None,
    },
    "implementation_element": {
        "name": None,
        "type": {"layer": "implementation_types", "identity_field": "name"},
        "description": None,
    },
}


def test_the_8_0_0_stated_names_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_FIELDS_8_0_NAMES` and the four names on added kinds project.

    What it does not reach: the descriptions' wording, and what reads the fields
    (the writer, the rules, the importers), which no projected table carries.
    """
    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    swc_fields = [field for owner, field in annotations if owner == "swc"]
    assert swc_fields[swc_fields.index("behavior_name") + 1] == "package"
    events = ("timing_event", "init_event", "operation_event")
    for kind in (*events, "data_read", "data_write", "operation_call"):
        assert [field for owner, field in annotations if owner == kind][-1] == "name", kind
    for kind, field in sorted(
        ADDED_FIELDS_8_0_NAMES
        | {("operation_event", "name"), ("data_read", "name"), ("data_write", "name"), ("operation_call", "name")}
    ):
        row = annotations[(kind, field)]
        assert row["description"] and row["unit"] is None and row["reference"] is None, row
        assert (kind, field) not in _TABLES.collection_specs


def test_the_implementation_composite_addition_is_exactly_the_added_rows() -> None:
    """The rows :data:`ADDED_KINDS_7_8` projects in the four tables.

    What it does not reach: which kinds each reference may name, which is the
    validator's reference-role rule family and not a projected table, and the
    descriptions' wording (the prose oracle over the ``full`` discover vector).
    """
    frozen_kinds = {row["kind"] for row in FROZEN_TABLES["KIND_NAMES"]}
    projected_names = _serialize_kind_names(_TABLES.kind_names)
    added = {
        row["kind"]: row["model"]
        for row in projected_names
        if row["kind"]
        not in frozen_kinds
        | UNGRADED_KINDS
        | ADDED_KINDS_3_2
        | ADDED_KINDS_5_0
        | ADDED_KINDS_7_0
        | ADDED_KINDS_7_5
        | ADDED_KINDS_7_7
        | ADDED_KINDS_7_11
        | ADDED_KINDS_8_0_ELEMENTS
        | ADDED_KINDS_8_0_LIMITS
        | ADDED_KINDS_12_1
        | ADDED_KINDS_12_2
        | ADDED_KINDS_12_3
        | ADDED_KINDS_12_7
    }
    assert added == {
        "implementation_record": "swcraft_tools.schema.ImplementationRecordDataType",
        "implementation_array": "swcraft_tools.schema.ImplementationArrayDataType",
        "pointer": "swcraft_tools.schema.PointerDataType",
        "implementation_element": "swcraft_tools.schema.ImplementationElement",
    }
    order = [row["kind"] for row in projected_names]
    start = order.index("external_type") + 1
    assert order[start : start + 3] == ["implementation_record", "implementation_array", "pointer"]
    # The record field is `application_types`' member kind, so the implementation
    # element follows the pointer, the last top-level kind of `data_types`.
    assert order[order.index("pointer") + 1] == "implementation_element"

    annotations = _rows_by_key(_serialize_field_annotations(_FIELD_ANNOTATIONS))
    for kind, declared in _ADDED_FIELDS_7_8.items():
        assert _TABLES.kind_descriptions[kind].strip(), kind
        assert [field for owner, field in annotations if owner == kind] == list(declared), kind
        for field, reference in declared.items():
            row = annotations[(kind, field)]
            assert row["description"] and row["unit"] is None and row["immutable"] is None, row
            assert row["reference"] == reference, row

    collections = _rows_by_key(_serialize_collection_specs(_TABLES.collection_specs))
    assert [key for key in collections if key[0] in ADDED_KINDS_7_8] == [("implementation_record", "elements")]
    spec = collections[("implementation_record", "elements")]
    assert (spec["member_identity"], spec["identity_field"]) == ("field", "name")


def test_every_projected_attribute_is_compared_against_the_frozen_table() -> None:
    """Every attribute of the four spec classes is serialized into the comparison.

    The serializers hand-list the attributes they compare and the frozen files
    cannot grow, so a new attribute would otherwise go uncompared with every test
    above green. Adding one means serializing it here or recording it below as
    deliberately uncompared; ``FieldAnnotation`` is pinned the same way beside its
    own test.
    """
    assert {field.name for field in fields(reg.LayerSpec)} == {
        "name",
        "annotation",
        "identity_field",
        "snapshot_attribute",
        "storage",
        "optional",
        "description",
        "absent_description",
    }
    assert {field.name for field in fields(reg.StorageRecord)} == {
        "directory",
        "nesting",
        "file_suffix",
        "filename_from",
    }
    assert {field.name for field in fields(reg.PlacementFieldSpec)} == {
        "layer",
        "kind",
        "name",
        "type",
        "description",
    }
    assert {field.name for field in fields(reg.CollectionSpec)} == {
        "member_identity",
        "identity_field",
        "identity_fields",
        "description",
    }

    # And each serializer really carries every one of them into the comparison.
    layer_row = _serialize_layer_specs(_TABLES.layer_specs)[0]
    assert set(layer_row) == {field.name for field in fields(reg.LayerSpec)}
    assert set(layer_row["storage"]) == {field.name for field in fields(reg.StorageRecord)}
    assert set(_serialize_placement_fields(_TABLES.placement_fields)[0]) == {
        field.name for field in fields(reg.PlacementFieldSpec)
    }
    collection_row = _serialize_collection_specs(_TABLES.collection_specs)[0]
    assert set(collection_row) - {"kind", "field"} == {field.name for field in fields(reg.CollectionSpec)}


def test_the_frozen_tables_are_all_six_and_none_is_empty() -> None:
    """The guard that keeps the five comparisons above from passing vacuously."""
    assert set(FROZEN_TABLES) == {
        "KIND_NAMES",
        "LAYER_SPECS",
        "PLACEMENT_FIELDS",
        "KIND_DESCRIPTIONS",
        "FIELD_ANNOTATIONS",
        "COLLECTION_SPECS",
    }
    assert all(FROZEN_TABLES[name] for name in FROZEN_TABLES)


# ---------------------------------------------------------------------------
# Layer facts
# ---------------------------------------------------------------------------


def test_the_layer_names_are_the_contracts_own_order() -> None:
    registry = production_registry()
    declared = [layer["name"] for layer in load_contract()["layers"]]
    assert list(registry.layer_names) == declared


def test_the_declared_display_order_is_not_the_contracts_own() -> None:
    """The two orders differ, which is why ``displayOrder`` is declared at all.

    Contract order puts components first; presentation order puts requirements
    first, then the structure that realizes them. A kernel that published only one
    would silently reorder the diff renderer's output, which is a byte oracle.
    """
    registry = production_registry()
    assert registry.display_order != registry.layer_names
    assert registry.display_order[:3] == ("requirements", "compositions", "swc")


def test_the_adapter_order_is_the_bindings_own_and_not_the_display_order() -> None:
    """The legacy field order is a separate declared fact, in a separate document.

    ``binding.snapshotAttributes``' key order is the ``ModelSnapshot`` /
    ``ModelDiff`` field order, a backward-compatibility surface pinned positionally
    by ``packages/tools/tests/test_snapshot.py``; the contract's ``displayOrder``
    is presentation, which lists requirements first in ``diff`` and ``impact``.
    """
    registry = production_registry()
    binding_order = [layer_id.removeprefix("layer:") for layer_id in load_binding()["snapshotAttributes"]]

    assert list(registry.adapter_order) == binding_order
    assert registry.adapter_order[:2] == ("compositions", "swc")
    assert registry.adapter_order != registry.display_order
    assert sorted(registry.adapter_order) == sorted(registry.display_order), "the same nine, ordered differently"


def test_a_layer_the_binding_gives_no_snapshot_attribute_is_absent_from_the_adapter_order() -> None:
    """A layer with no adapter field, the synthetic one, has nothing to order.

    ``display_order`` names every declared layer, ``adapter_order`` only the ones
    the deprecated bridge still reaches.
    """
    registry = merged_registry()

    assert "synthetic" in registry.display_order
    assert "synthetic" not in registry.adapter_order
    assert registry.layer("synthetic").snapshot_attribute is None


def test_a_layer_with_no_declared_order_sorts_after_every_layer_that_has_one() -> None:
    """Ties are broken by contract order.

    So a contract declaring none yields contract order unchanged, and a layer added
    without a presentation position does not reshuffle the others.
    """
    stripped = _contract_without_display()
    registry = build_model_registry(contract=stripped, binding=load_binding())
    assert registry.display_order == registry.layer_names

    partial = _contract_without_display()
    for layer in partial["layers"]:
        if layer["name"] in {"tasks", "swc"}:
            layer["displayOrder"] = {"tasks": 1, "swc": 2}[layer["name"]]
    declared = build_model_registry(contract=partial, binding=load_binding())
    assert declared.display_order[:2] == ("tasks", "swc")
    assert declared.display_order[2:] == tuple(n for n in declared.layer_names if n not in {"tasks", "swc"})


def test_two_layers_claiming_one_display_order_are_refused() -> None:
    """A duplicate fails nowhere else: the tie-break would just pick one.

    The registry is the only reader that sees every layer at once, which is also
    why ``contract.schema.json`` cannot express the rule: it constrains one layer
    declaration at a time.
    """
    contract = copy.deepcopy(load_contract())
    tasks = next(layer for layer in contract["layers"] if layer["name"] == "tasks")
    swc = next(layer for layer in contract["layers"] if layer["name"] == "swc")
    tasks["displayOrder"] = swc["displayOrder"]
    with pytest.raises(ModelRegistryError, match="both declare displayOrder"):
        build_model_registry(contract=contract, binding=load_binding())


def _contract_without_display() -> dict[str, Any]:
    contract = copy.deepcopy(load_contract())
    for layer in contract["layers"]:
        layer.pop("label", None)
        layer.pop("displayOrder", None)
    return contract


def test_the_declared_labels_are_the_rendered_headings() -> None:
    """``SWCs`` and ``Implementation Types`` are neither the layer name nor derivable from it."""
    registry = production_registry()
    labels = {layer.name: layer.display_label for layer in registry.layers}
    assert labels["swc"] == "SWCs"
    assert labels["implementation_types"] == "Implementation Types"


def test_the_label_defaults_to_the_layer_name() -> None:
    """A layer declaring none renders under its own name, the synthetic layer's case."""
    registry = build_model_registry(contract=_contract_without_display(), binding=load_binding())
    assert [layer.display_label for layer in registry.layers] == list(registry.layer_names)


def test_the_arxml_bound_layers_are_projected_onto_layer_facts() -> None:
    """``layerDecl.arxmlBound`` reaches :attr:`LayerFacts.arxml_bound`, both ways.

    Set equality rather than a containment: a containment passes when a layer
    silently stops being ARXML-bound, which would switch a whole layer's
    identifier checking off (``validator._identifier_layer_tokens``).
    """
    registry = production_registry()
    bound = {layer.name for layer in registry.layers if layer.arxml_bound}
    assert bound == ARXML_BOUND_LAYER_NAMES
    assert {layer.name for layer in registry.layers} - bound == {
        "requirements",
        "tasks",
        "sequences",
        "hardware",
    }


def test_a_layer_declaring_no_arxml_bound_is_not_arxml_bound() -> None:
    """The optional key falls back to ``false``, the safe direction.

    A layer nobody answered for must not acquire an AUTOSAR constraint. The
    synthetic layer answers ``false`` explicitly, so the absence is exercised by
    stripping the key from every shipped layer.
    """
    stripped = copy.deepcopy(load_contract())
    for layer in stripped["layers"]:
        assert layer.pop("arxmlBound") is not None, f"{layer['name']} does not declare arxmlBound"
    registry = build_model_registry(contract=stripped, binding=load_binding())
    assert [layer.arxml_bound for layer in registry.layers] == [False] * len(registry.layers)


def test_the_synthetic_layer_is_not_arxml_bound() -> None:
    """A layer added to the contract does not become identifier-checked by arriving."""
    registry = merged_registry()
    assert registry.layer("synthetic").arxml_bound is False
    assert {layer.name for layer in registry.layers if layer.arxml_bound} == ARXML_BOUND_LAYER_NAMES


#: The fields the writer leaves OUT of a model file while they hold their
#: declared default, written out by hand: the projection's expected value may
#: not be read from the document it projects. Each is a flag or a list whose key
#: belongs only in the file of an element that sets it.
OMIT_AT_DEFAULT_FIELDS = frozenset(
    {
        "field:swc.runnable.min_start_interval",
        "field:swc.runnable.concurrent",
        "field:swc.swc.data_type_mappings",
        "field:swc.runnable.reads",
        "field:swc.runnable.writes",
        "field:swc.runnable.calls",
        "field:compositions.composition.root",
        "field:swc.swc.external",
        # Contract `7.11.0`: the two `false` flags and the two empty lists the
        # derived rule would write out, left out so every file written before
        # the fields existed round-trips byte-identically.
        "field:interfaces.sender_receiver.is_service",
        "field:interfaces.client_server.is_service",
        "field:swc.port.com_specs",
        "field:swc.timing_event.disabled_modes",
        # Contract `8.0.0`'s stub lists: empty, they are left out, as an empty
        # access list is.
        "field:interfaces.external_interface.operations",
        "field:interfaces.external_interface.data_elements",
        # Contract `9.1.0`: a partition's `trust` at `non_trusted`, which the
        # derived rule would write out.
        "field:hardware.partition.trust",
        # Contract `11.0.0`: the twelve `satisfies` lists and a requirement's
        # `derived_from`, left out while empty.
        "field:swc.swc.satisfies",
        "field:swc.port.satisfies",
        "field:swc.runnable.satisfies",
        "field:interfaces.sender_receiver.satisfies",
        "field:interfaces.client_server.satisfies",
        "field:interfaces.external_interface.satisfies",
        "field:requirements.requirement.derived_from",
        "field:compositions.composition.satisfies",
        "field:tasks.task.satisfies",
        "field:hardware.processor.satisfies",
        "field:hardware.core.satisfies",
        "field:hardware.memory.satisfies",
        "field:hardware.partition.satisfies",
        # Contract `11.1.0`: a requirement's `stub`, left out at `false` -- only a
        # stub carries it.
        "field:requirements.requirement.stub",
        # Contract `12.1.0`: a component's `variables` and a runnable's two
        # variable access lists, left out while empty.
        "field:swc.swc.variables",
        "field:swc.runnable.reads_variables",
        "field:swc.runnable.writes_variables",
        # Contract 12.2.0: a component's `exclusive_areas` and a runnable's two
        # area lists, left out while empty for the same reason.
        "field:swc.swc.exclusive_areas",
        "field:swc.runnable.can_enter",
        "field:swc.runnable.runs_inside",
        # Contract 12.3.0: a data-received event's `disabled_modes`, the timing
        # event's rule, left out while empty.
        "field:swc.data_event.disabled_modes",
        # Contract 12.7.0: a call's `asynchronous` and `poll`, left out at
        # `false` as the runnable's `concurrent` is.
        "field:swc.operation_call.asynchronous",
        "field:swc.operation_call.poll",
    }
)


def test_the_omit_at_default_field_is_projected_onto_field_facts() -> None:
    """``fieldDecl.omitAtDefault`` reaches :attr:`FieldFacts.omit_at_default`, both ways.

    Set equality over every declared field, so a field gaining the exception fails
    as loudly as one losing it, which would break the round trip
    ``writer/elements.py`` keeps for older runnable YAML.
    """
    registry = production_registry()
    declared = {
        facts.id for layer in registry.layers for kind in layer.kinds for facts in kind.fields if facts.omit_at_default
    }
    assert declared == OMIT_AT_DEFAULT_FIELDS


def test_a_field_declaring_no_omit_at_default_is_written_out() -> None:
    """The optional key falls back to ``false``, the safe direction.

    A field nobody answered for is written out at its default. Unlike
    ``arxmlBound`` the shipped contract answers it only on the exceptions: a table
    listing every non-exception would not be an exception table.
    """
    stripped = copy.deepcopy(load_contract())
    popped = [
        field.pop("omitAtDefault")
        for layer in stripped["layers"]
        for kind in layer["kinds"]
        for field in kind["fields"]
        if "omitAtDefault" in field
    ]
    assert popped == [True] * len(OMIT_AT_DEFAULT_FIELDS), popped
    registry = build_model_registry(contract=stripped, binding=load_binding())
    assert not any(facts.omit_at_default for layer in registry.layers for kind in layer.kinds for facts in kind.fields)


@pytest.mark.parametrize(
    ("layer", "kinds"),
    [
        ("interfaces", ("sender_receiver", "client_server", "external_interface")),
        (
            "implementation_types",
            (
                "value_type",
                "reference_type",
                "external_type",
                "implementation_record",
                "implementation_array",
                "pointer",
            ),
        ),
        ("hardware", ("processor", "core", "memory", "partition")),
        # The application kinds' own union.
        ("application_types", ("enum", "record", "array", "application_type")),
    ],
)
def test_a_multi_kind_layer_publishes_its_top_level_kinds(layer: str, kinds: tuple[str, ...]) -> None:
    """The layers whose union lives only in ``schema.py`` and is bound by nothing."""
    facts = production_registry().layer(layer)
    assert tuple(kind.kind for kind in facts.top_level_kinds) == kinds


def test_a_single_kind_layers_annotation_is_its_bound_class() -> None:
    registry = production_registry()
    for name in registry.layer_names:
        facts = registry.layer(name)
        if len(facts.top_level_kinds) == 1:
            assert facts.element_annotation is facts.top_level_kinds[0].model


@pytest.mark.parametrize(
    ("layer", "accepted", "rejected"),
    [
        (
            "interfaces",
            {
                "name": "IFace",
                "kind": "sender_receiver",
                "data_elements": [{"name": "d", "implementation_type": "uint8"}],
            },
            {"name": "IFace", "kind": "no_such_kind"},
        ),
        (
            "hardware",
            {"name": "CORE0", "kind": "core", "threshold": 0.8},
            {"name": "CORE0", "threshold": 0.8},
        ),
    ],
)
def test_the_synthesized_union_discriminates_the_way_schema_py_does(
    layer: str, accepted: dict[str, Any], rejected: dict[str, Any]
) -> None:
    """The layer annotation is synthesized here, not read from ``schema.py``.

    This asserts the pair the synthesis has to get right for the kernel to be
    usable at all: a valid document of one member kind, and a document whose
    discriminator is wrong or missing.
    """
    adapter = TypeAdapter(production_registry().layer(layer).element_annotation)
    assert adapter.validate_python(accepted) is not None
    with pytest.raises(ValueError):
        adapter.validate_python(rejected)


def test_a_multi_kind_layer_whose_kinds_disagree_on_the_discriminator_is_refused() -> None:
    contract = copy.deepcopy(load_contract())
    hardware = next(layer for layer in contract["layers"] if layer["name"] == "hardware")
    hardware["kinds"][1]["discriminatorField"] = "name"
    with pytest.raises(ModelRegistryError, match="discriminated union needs exactly one"):
        build_model_registry(contract=contract, binding=load_binding())


def test_a_collection_whose_member_identity_is_undeclared_is_refused() -> None:
    """``memberIdentity`` is one of three strategies; any other value fails construction, naming it."""
    contract = copy.deepcopy(load_contract())
    compositions = next(layer for layer in contract["layers"] if layer["name"] == "compositions")
    declared = next(field["collection"] for field in compositions["kinds"][0]["fields"] if field["name"] == "members")
    declared["memberIdentity"] = "by_position"
    with pytest.raises(ModelRegistryError, match="by_position"):
        build_model_registry(contract=contract, binding=load_binding())


# ---------------------------------------------------------------------------
# Construction failures
# ---------------------------------------------------------------------------


def test_an_unbound_kind_fails_construction_naming_the_kind_id() -> None:
    """The merged contract without the merged binding fails, naming the unbound kind.

    A kernel that quietly skipped the unbound kind would build a layer that can
    parse no element, and every consumer would report an empty layer rather than a
    defect.
    """
    with pytest.raises(ModelRegistryError, match="kind:synthetic.synthetic"):
        build_model_registry(contract=merged_contract(), binding=load_binding())


def test_a_kind_bound_to_something_that_is_not_a_pydantic_model_is_refused() -> None:
    binding = copy.deepcopy(load_binding())
    binding["kinds"]["kind:swc.swc"] = {"module": "swcraft_tools.schema", "class": "ModelElement"}
    build_model_registry(contract=load_contract(), binding=binding)  # a model: accepted

    binding["kinds"]["kind:swc.swc"] = {"module": "swcraft_tools.identifier", "class": "is_valid_identifier"}
    with pytest.raises(ModelRegistryError, match="not a Pydantic model"):
        build_model_registry(contract=load_contract(), binding=binding)


def test_a_layer_with_no_top_level_kind_is_refused() -> None:
    contract = copy.deepcopy(load_contract())
    sequences = next(layer for layer in contract["layers"] if layer["name"] == "sequences")
    for kind in sequences["kinds"]:
        kind["memberOnly"] = True
    with pytest.raises(ModelRegistryError, match="no top-level element kind"):
        build_model_registry(contract=contract, binding=load_binding())


# ---------------------------------------------------------------------------
# The merged contract: a sixteenth layer with no adapter field
# ---------------------------------------------------------------------------


def test_the_merged_contract_yields_one_layer_more_than_the_shipped_one(merged: ModelRegistry) -> None:
    assert len(merged.layers) == len(production_registry().layers) + 1 == 16
    assert merged.layer_names[-1] == "synthetic"


def test_the_synthetic_layer_has_no_snapshot_attribute(merged: ModelRegistry) -> None:
    """A layer the binding gives no ``snapshotAttributes`` row is allowed, not refused.

    ``ModelSnapshot`` gets a field per *bound* attribute, and a layer with none
    simply has no field: that is how a layer survives an adapter that predates it.
    """
    assert merged.layer("synthetic").snapshot_attribute is None
    assert [layer.name for layer in merged.layers if layer.snapshot_attribute is None] == ["synthetic"]


def test_the_synthetic_layer_carries_its_bound_class_and_storage(merged: ModelRegistry) -> None:
    facts = merged.layer("synthetic")
    assert facts.element_annotation is SyntheticElement
    assert facts.storage == StorageRecord("synthetic", "flat", ".yaml", "name")
    assert merged.bound_class("synthetic") is SyntheticElement
    assert merged.kind_of_model(SyntheticElement) is merged.kind("synthetic")


def test_the_synthetic_layers_reference_is_published(merged: ModelRegistry) -> None:
    """The synthetic layer's one reference field, in the same shape as the thirty-four shipped ones."""
    edges = {edge.field_id: edge for edge in merged.references()}
    assert len(edges) == 35
    synthetic = edges["field:synthetic.synthetic.composition"]
    assert (synthetic.source_layer, synthetic.target_layer, synthetic.target_identity_field) == (
        "synthetic",
        "compositions",
        "name",
    )


def test_the_shipped_references_are_the_seventeen_the_contract_declares() -> None:
    """Every published reference is a field the contract declares one on, in contract order."""
    registry = production_registry()
    declared = [
        field["id"]
        for layer in load_contract()["layers"]
        for kind in layer["kinds"]
        for field in kind["fields"]
        if "reference" in field
    ]
    assert [edge.field_id for edge in registry.references()] == declared
    # The reference fields the shipped contract declares; a contract adding or
    # retiring one moves it.
    assert len(declared) == 34


# ---------------------------------------------------------------------------
# Relations: the edges an identity lookup cannot express
# ---------------------------------------------------------------------------


def _into_requirements(field_id: str, source_layer: str) -> tuple[str, str, str, str, str, str]:
    """One of the thirteen ``versioned-list-values`` relations into the requirements layer."""
    return (field_id, source_layer, "requirements", "requirement", "id", "versioned-list-values")


def test_the_shipped_relations_are_the_sixteen_declared_on_their_fields() -> None:
    """In contract order, and every one of them carrying its declared target."""
    registry = production_registry()
    assert [
        (edge.field_id, edge.source_layer, edge.target_layer, edge.target_kind, edge.target_field, edge.resolution)
        for edge in registry.relations()
    ] == [
        ("field:swc.swc.memory", "swc", "hardware", "memory", "name", "map-keys"),
        (
            "field:swc.swc.data_type_mappings",
            "swc",
            "mapping_sets",
            "mapping_set",
            "name",
            "list-values",
        ),
        _into_requirements("field:swc.swc.satisfies", "swc"),
        _into_requirements("field:swc.port.satisfies", "swc"),
        _into_requirements("field:swc.runnable.satisfies", "swc"),
        _into_requirements("field:interfaces.sender_receiver.satisfies", "interfaces"),
        _into_requirements("field:interfaces.client_server.satisfies", "interfaces"),
        _into_requirements("field:interfaces.external_interface.satisfies", "interfaces"),
        _into_requirements("field:requirements.requirement.derived_from", "requirements"),
        _into_requirements("field:compositions.composition.satisfies", "compositions"),
        _into_requirements("field:tasks.task.satisfies", "tasks"),
        _into_requirements("field:hardware.processor.satisfies", "hardware"),
        _into_requirements("field:hardware.core.satisfies", "hardware"),
        ("field:hardware.memory.accessible", "hardware", "hardware", "core", "name", "list-values"),
        _into_requirements("field:hardware.memory.satisfies", "hardware"),
        _into_requirements("field:hardware.partition.satisfies", "hardware"),
    ]


def test_no_relation_resolves_a_scalar_field_any_more() -> None:
    """``resolve_field_value`` is bound by nothing, and the callable stays.

    ``field-value`` is still one of the shapes the meta-schema's closed
    ``resolution`` enum admits, and ``relations.py`` is a leaf whose vocabulary
    keeps its resolver; what is asserted is that no relation declares the shape
    and the binding names it nowhere.
    """
    assert [edge.resolution for edge in production_registry().relations() if edge.resolution == "field-value"] == []
    assert callable(relations.resolve_field_value)
    assert "resolve_field_value" not in {row["callable"] for row in load_binding()["relations"].values()}


def test_no_relation_reaches_the_published_field_descriptor() -> None:
    """``discover`` output carries no ``relation``, the reason it is not a ``reference``.

    The wire descriptor carries a ``reference`` and nothing beside it; widening it
    would change shipped protocol bytes. The frozen-table comparison already fails
    on such a change; this states the guarantee.
    """
    published = {name for spec in _FIELD_ANNOTATIONS.values() for name in vars(spec)}
    assert "relation" not in published
    assert {field.name for field in fields(reg.FieldAnnotation)} == {"description", "unit", "reference", "immutable"}
    accessible = _FIELD_ANNOTATIONS[("memory", "accessible")]
    assert accessible.reference is None
    assert production_registry().field_facts("memory", "accessible").relation is not None


def test_a_relation_is_published_on_the_field_that_declares_it() -> None:
    """The published fact rides the field, which is what the coverage gate reaches."""
    registry = production_registry()
    facts = registry.field_facts("memory", "accessible")
    assert facts is not None and facts.relation is not None
    assert facts.relation.field_id == "field:hardware.memory.accessible"
    assert registry.field_facts("memory", "size") is not None
    assert registry.field_facts("memory", "size").relation is None


def _relation(field_id: str) -> Any:
    return next(edge for edge in production_registry().relations() if edge.field_id == field_id)


@pytest.mark.parametrize(
    ("field_id", "element", "expected"),
    [
        (
            "field:swc.swc.memory",
            lambda: SWComponent(name="A", memory={"ram": 1024, "rom": 2048}),
            ("ram", "rom"),
        ),
        (
            "field:swc.swc.satisfies",
            lambda: SWComponent(name="A", satisfies=["R-1@2", "R-2", "R-3@x"]),
            ("R-1", "R-2", "R-3"),
        ),
        (
            "field:requirements.requirement.derived_from",
            lambda: Requirement(
                id="R-4",
                title="t",
                description="d",
                version=1,
                source="architecture",
                derived_from=["R-1@1", "hardware:CORE0"],
            ),
            ("R-1",),
        ),
        (
            "field:hardware.memory.accessible",
            lambda: Memory(name="RAM0", kind="memory", type="RAM", size=1024, accessible=["CORE0"], threshold=0.8),
            ("CORE0",),
        ),
    ],
    ids=["swc.memory", "swc.satisfies", "requirement.derived_from", "memory.accessible"],
)
def test_each_bound_resolver_reads_its_own_source_field(field_id: str, element: Any, expected: tuple[Any, ...]) -> None:
    """The resolver arrives bound to its field, so the caller passes the element alone.

    ``versioned-list-values`` yields the text before a member's first ``@`` and
    skips an element reference.
    """
    assert tuple(_relation(field_id).resolver(element())) == expected


def test_a_resolver_reports_a_key_no_element_answers_to() -> None:
    """Unconditional, and that is ``impact.py``'s documented behavior, not a gap.

    The complaint about an undeclared pool belongs to ``check_resource_budgets``. A
    resolver that filtered against the model would move the finding out of the
    check that owns it and would need the whole model to answer about one element.
    """
    swc = SWComponent(name="A", memory={"no_such_pool": 8})
    assert tuple(_relation("field:swc.swc.memory").resolver(swc)) == ("no_such_pool",)


def test_an_empty_source_value_resolves_to_no_key() -> None:
    """Naming nothing and naming something absent are different statements."""
    assert tuple(_relation("field:swc.swc.memory").resolver(SWComponent(name="A"))) == ()
    empty = Requirement(id="R-1", title="t", description="d", version=1, source="architecture")
    assert tuple(_relation("field:requirements.requirement.derived_from").resolver(empty)) == ()
    assert tuple(_relation("field:swc.swc.satisfies").resolver(SWComponent(name="A"))) == ()


def test_an_unbound_relation_fails_construction_naming_the_field() -> None:
    """A declared relation nothing resolves would be a topology nothing walks.

    The edge would never be built and nothing would fail, which is why the binding
    row is required rather than looked up lazily.
    """
    binding = copy.deepcopy(load_binding())
    del binding["relations"]["field:hardware.memory.accessible"]
    with pytest.raises(ModelRegistryError, match="field:hardware.memory.accessible"):
        build_model_registry(contract=load_contract(), binding=binding)


def test_a_resolver_bound_to_a_field_that_declares_no_relation_is_refused() -> None:
    """The mirror direction, which would otherwise rot in silence."""
    binding = copy.deepcopy(load_binding())
    binding["relations"]["field:tasks.task.period"] = {
        "module": "swcraft_tools.relations",
        "callable": "resolve_field_value",
    }
    with pytest.raises(ModelRegistryError, match="declare no relation"):
        build_model_registry(contract=load_contract(), binding=binding)


def _field_decl(contract: dict[str, Any], field_id: str) -> dict[str, Any]:
    for layer in contract["layers"]:
        for kind in layer["kinds"]:
            for field in kind["fields"]:
                if field["id"] == field_id:
                    return field
    raise AssertionError(field_id)


@pytest.mark.parametrize(
    ("field_id", "shape"),
    [("field:swc.swc.memory", "map[string, integer]"), ("field:swc.swc.satisfies", "list[string]")],
    ids=["map", "list"],
)
def test_a_reference_declared_on_a_collection_field_is_refused(field_id: str, shape: str) -> None:
    """The hazard the meta-schema cannot state, closed at the kernel.

    ``fieldDecl`` forbids ``reference`` and ``relation`` *together* and says
    nothing about a field holding many values. ``impact._keys`` would read a
    reference's key as the field's OWN value, the whole collection, and the
    traversal would produce no edge, indistinguishable from a model without such an
    element. So it is a construction failure naming the field and its shape. The
    relation is dropped in each case so that the exclusion is not what refuses the
    document.
    """
    contract = copy.deepcopy(load_contract())
    field = _field_decl(contract, field_id)
    field.pop("relation")
    field["reference"] = {"layer": "swc", "identityField": "name"}
    binding = copy.deepcopy(load_binding())
    del binding["relations"][field_id]

    with pytest.raises(ModelRegistryError) as raised:
        build_model_registry(contract=contract, binding=binding)

    message = str(raised.value)
    assert field_id in message
    assert shape in message


def test_a_reference_on_a_scalar_field_is_still_built() -> None:
    """The positive control: the guard reads the shape, not the presence of a reference."""
    registry = build_model_registry(contract=load_contract(), binding=load_binding())
    assert [edge.field_id for edge in registry.references() if edge.field_id == "field:tasks.task.partition"] == [
        "field:tasks.task.partition"
    ]


def test_a_relation_naming_an_undeclared_target_field_is_refused() -> None:
    """It would resolve keys forever and match nothing, which looks like an empty model."""
    contract = copy.deepcopy(load_contract())
    _relation_decl(contract, "field:hardware.memory.accessible")["field"] = "names"
    with pytest.raises(ModelRegistryError, match="declares no such field"):
        build_model_registry(contract=contract, binding=load_binding())


def test_a_relation_naming_an_undeclared_target_kind_is_refused() -> None:
    contract = copy.deepcopy(load_contract())
    _relation_decl(contract, "field:hardware.memory.accessible")["kind"] = "cpu"
    with pytest.raises(ModelRegistryError, match="undeclared"):
        build_model_registry(contract=contract, binding=load_binding())


def test_a_relation_naming_the_wrong_layer_for_its_kind_is_refused() -> None:
    """Both halves of the target are checked, not just the one a lookup would use."""
    contract = copy.deepcopy(load_contract())
    _relation_decl(contract, "field:hardware.memory.accessible")["layer"] = "tasks"
    with pytest.raises(ModelRegistryError, match="belongs to layer"):
        build_model_registry(contract=contract, binding=load_binding())


def test_a_relation_bound_to_something_that_is_not_callable_is_refused() -> None:
    binding = copy.deepcopy(load_binding())
    binding["relations"]["field:hardware.memory.accessible"] = {
        "module": "swcraft_tools.relations",
        "callable": "__doc__",
    }
    with pytest.raises(ModelRegistryError, match="not callable"):
        build_model_registry(contract=load_contract(), binding=binding)


def test_a_resolution_over_the_wrong_shape_is_reported_rather_than_returning_nothing() -> None:
    """The contract and the bound class have disagreed; an empty answer would hide it.

    ``resolve_list_values`` refuses a ``str`` although a string is a sequence:
    iterating one yields characters, and an SWC named ``Speed`` would resolve
    to five targets that read as plausible model names.
    """
    task = Task(name="T", period=0.01, priority=1, partition="PARTITION0")
    with pytest.raises(relations.RelationResolutionError, match="not a mapping"):
        relations.resolve_map_keys(task, "partition")
    with pytest.raises(relations.RelationResolutionError, match="not a list of members"):
        relations.resolve_list_values(task, "name")


def test_an_absent_value_resolves_to_no_key_rather_than_refusing() -> None:
    """``None`` names nothing -- for a map, for a list, and for a scalar alike.

    An optional relation field left unset contributes no edge rather than failing
    a whole traversal.
    """
    absent = SimpleNamespace(core=None, tags=None, name=None)

    assert tuple(relations.resolve_map_keys(absent, "core")) == ()
    assert tuple(relations.resolve_list_values(absent, "tags")) == ()
    assert tuple(relations.resolve_field_value(absent, "name")) == ()


def test_a_duplicate_element_kind_name_is_refused_naming_the_kind() -> None:
    """Two contract kinds sharing a ``kind`` name are refused.

    The second would overwrite the first in ``ModelRegistry._kinds``, so every
    ``kind()``, ``bound_class()`` and ``member_of`` lookup for that name would
    answer about the wrong element.
    """
    contract = copy.deepcopy(load_contract())
    layers = {layer["name"]: layer for layer in contract["layers"]}
    borrowed = copy.deepcopy(layers["requirements"]["kinds"][0])
    borrowed["id"] = "kind:compositions.requirement"
    layers["compositions"]["kinds"].append(borrowed)

    binding = copy.deepcopy(load_binding())
    binding["kinds"]["kind:compositions.requirement"] = binding["kinds"]["kind:requirements.requirement"]

    with pytest.raises(ModelRegistryError, match="duplicate element-kind name 'requirement'"):
        build_model_registry(contract=contract, binding=binding)


def test_two_layers_sharing_a_name_or_a_storage_directory_are_refused() -> None:
    """Neither ``contract.schema.json`` nor the meta-schema can express this.

    Both constrain one layer declaration at a time. A duplicate ``name`` silently
    loses a row from every layer-keyed structure the kernel builds, and a duplicate
    ``storage.directory`` makes ``session._layer_of_directory`` -- the commit gate's
    fail-closed path -- resolve a written file to the first declaring layer.
    """
    contract = copy.deepcopy(load_contract())
    contract["layers"].append(copy.deepcopy(contract["layers"][0]))
    with pytest.raises(ModelRegistryError, match="duplicate layer name"):
        build_model_registry(contract=contract, binding=load_binding())

    contract = copy.deepcopy(load_contract())
    duplicate = copy.deepcopy(contract["layers"][0])
    duplicate["name"] = f"{duplicate['name']}_twin"
    duplicate["id"] = f"{duplicate['id']}_twin"
    contract["layers"].append(duplicate)
    with pytest.raises(ModelRegistryError, match="both store elements in"):
        build_model_registry(contract=contract, binding=load_binding())

    contract = copy.deepcopy(load_contract())
    twin = copy.deepcopy(contract["layers"][0])
    twin["name"] = f"{twin['name']}_twin"
    twin["storage"]["directory"] = f"{twin['storage']['directory']}_twin"
    contract["layers"].append(twin)
    with pytest.raises(ModelRegistryError, match="duplicate layer id"):
        build_model_registry(contract=contract, binding=load_binding())


class TestTheDirectoryPolicy:
    """A directory of one or two segments, and the checks that land with it.

    The registry refuses two layers whose directories are equal, differ only in
    case, or one of which is a prefix of the other, segment by segment. The pairs
    below are built from a dictionary the meta-schema never saw, the case the
    case-only check exists for: ``contract.schema.json``'s lowercase pattern
    already keeps such a pair out of a schema-valid document.
    ``synthetic_support.OVERLAPPING_NESTED_LAYERS`` is the shape the prefix check
    refuses.
    """

    def test_two_sibling_nested_directories_are_accepted(self) -> None:
        registry = nested_registry(*SIBLING_NESTED_LAYERS)
        assert [registry.storage(name).directory for name, _ in SIBLING_NESTED_LAYERS] == ["nest/alpha", "nest/beta"]

    def test_two_equal_nested_directories_are_refused(self) -> None:
        with pytest.raises(ModelRegistryError, match="layers 'nest_alpha' and 'nest_beta' both store elements in"):
            nested_registry(("nest_alpha", "nest/alpha"), ("nest_beta", "nest/alpha"))

    @pytest.mark.parametrize(
        ("first", "second"),
        [("nest/alpha", "nest/Alpha"), ("nest/alpha", "Nest/alpha"), ("synthetic", "Synthetic")],
    )
    def test_two_directories_differing_only_in_case_are_refused(self, first: str, second: str) -> None:
        layers = [("nest_beta", second)] if first == "synthetic" else [("nest_alpha", first), ("nest_beta", second)]
        with pytest.raises(ModelRegistryError, match="differ only in case") as refused:
            nested_registry(*layers)
        assert f"'{second}'" in str(refused.value)

    def test_a_directory_inside_another_layers_directory_is_refused(self) -> None:
        with pytest.raises(ModelRegistryError, match="may not be a prefix of another's") as refused:
            nested_registry(*OVERLAPPING_NESTED_LAYERS)
        assert "'synthetic'" in str(refused.value)
        assert "'synthetic/alpha'" in str(refused.value)

    @pytest.mark.parametrize(
        ("first", "second"),
        [("nest", "nest/alpha"), ("nest/alpha", "nest"), ("nest", "Nest/alpha")],
    )
    def test_a_prefix_is_refused_whichever_layer_is_declared_first_and_in_any_case(
        self, first: str, second: str
    ) -> None:
        with pytest.raises(ModelRegistryError, match="may not be a prefix of another's"):
            nested_registry(("nest_alpha", first), ("nest_beta", second))

    def test_a_shared_leading_text_that_is_no_whole_segment_is_no_prefix(self) -> None:
        registry = nested_registry(("nest_alpha", "nest/alpha"), ("nest_beta", "nest/alphabet"))
        assert registry.storage("nest_beta").directory == "nest/alphabet"

    def test_the_shipped_type_layers_are_siblings_under_a_directory_no_layer_holds(self) -> None:
        registry = production_registry()
        directories = {registry.storage(name).directory for name in registry.layer_names}
        assert "data_types" not in directories
        assert {
            "data_types/application",
            "data_types/implementation",
            "data_types/base",
            "data_types/mapping_sets",
        } <= directories


def test_two_layers_sharing_a_derived_join_token_are_refused() -> None:
    """The key `_check_layer_declarations` does not reach.

    `session._touched_key` is derived -- `snapshotAttributes` falling back to the
    layer name -- so a collision is invisible to the declared-key guards above. It
    is **fail-open in the commit gate**: `_touched_identities` builds its
    dictionary by assignment, so one layer's reviewed identities are dropped and
    `_is_explained` answers that layer's written files out of the other layer's
    set, saying a file is explained by a change that never touched it.

    Exercised here: a layer *name* equal to another layer's bound attribute; a
    duplicated `snapshotAttributes` *value* is the next test's, and a duplicated
    declared token `test_two_layers_declaring_the_same_token_are_refused`'s.
    """
    contract = copy.deepcopy(load_contract())
    twin = copy.deepcopy(contract["layers"][0])
    twin["name"] = "swcs"  # `swc`'s bound snapshot attribute; no attribute of its own
    twin["id"] = "layer:swcs_twin"
    twin["storage"]["directory"] = "swcs_twin"
    twin["token"] = "swcs_twin"  # so that the token guard is not what answers
    contract["layers"].append(twin)
    with pytest.raises(ModelRegistryError, match="share the touched-identity key 'swcs'"):
        build_model_registry(contract=contract, binding=load_binding())


def test_two_layers_bound_to_the_same_snapshot_attribute_are_refused() -> None:
    """A duplicated `snapshotAttributes` value gives two layers the one touched-identity key."""
    binding = copy.deepcopy(load_binding())
    binding["snapshotAttributes"]["layer:interfaces"] = "swcs"  # `swc`'s bound attribute
    with pytest.raises(ModelRegistryError, match="share the touched-identity key 'swcs'"):
        build_model_registry(contract=load_contract(), binding=binding)


def _added_layer(contract: dict, *, source: str, name: str, **overrides: object) -> dict:
    """A deep copy of *source* re-keyed as a brand-new layer named *name*.

    Every identifier the registry indexes by is rewritten, so that the only
    collision a test creates is the one it is about; otherwise the twin trips
    `duplicate element-kind name` first and the assertion passes on the wrong
    refusal.

    *source* must be a layer with one top-level kind, no member kinds, no
    collection and no placement field: re-keying those would mean rewriting every
    cross-reference between them. The shape is asserted, so a caller reaching for a
    richer layer is told to widen this deliberately.

    An override whose value is `None` REMOVES the key, which is how a caller asks
    for a layer that declares no token at all.
    """
    # No shipped layer has that plain shape, so `source="synthetic"` borrows the
    # fixture layer.
    candidates = [*contract["layers"], *load_synthetic_layer()["layers"]]
    layer = copy.deepcopy(next(entry for entry in candidates if entry["name"] == source))
    assert "placement" not in layer, f"{source}: _added_layer cannot re-key a placement field"
    assert len(layer["kinds"]) == 1, f"{source}: _added_layer cannot re-key a multi-kind layer"
    kind = layer["kinds"][0]
    assert "memberOf" not in kind and not any("collection" in field for field in kind["fields"]), (
        f"{source}: _added_layer cannot re-key membership or a collection"
    )

    layer["name"] = name
    layer["id"] = f"layer:{name}"
    layer["storage"]["directory"] = name
    kind["kind"] = f"{name}_{kind['kind']}"
    kind["id"] = f"kind:{name}.{kind['kind']}"
    for field in kind["fields"]:
        field["id"] = f"field:{name}.{kind['kind']}.{field['name']}"
    for key, value in overrides.items():
        if value is None:
            layer.pop(key, None)
        else:
            layer[key] = value
    return layer


def test_two_layers_declaring_the_same_token_are_refused() -> None:
    """The declared token is unique across the contract, and the refusal names both layers.

    `contract.schema.json` constrains one layer declaration at a time. A collision
    merges two layers into one section of `swcraft impact`'s report, which fails
    nothing and is invisible in the output. Both layers are named because the
    contract does not say which one is the newcomer.
    """
    contract = copy.deepcopy(load_contract())
    contract["layers"].append(
        # `swc`'s own declared token, on a layer that is otherwise entirely its own.
        _added_layer(contract, source="synthetic", name="widgets", displayOrder=99, token="swc")
    )

    with pytest.raises(ModelRegistryError) as raised:
        build_model_registry(contract=contract, binding=load_binding())
    message = str(raised.value)
    assert "swc" in message and "widgets" in message, message
    assert "both declare the token 'swc'" in message, message


def test_a_declared_token_collides_with_an_undeclared_layers_fallback() -> None:
    """The mixed case only the effective-token check reaches.

    Two declaring layers are caught above, two falling back by
    `_check_layer_declarations`' name uniqueness, since the fallback IS the name.
    One layer declaring the token another is simply named by is the shape a
    contract acquires while the key is optional.
    """
    contract = copy.deepcopy(load_contract())
    contract["layers"] += [
        # `gadget` declares nothing and falls back to its own name; `gadgets` declares it.
        _added_layer(contract, source="synthetic", name="gadget", displayOrder=98, token=None),
        _added_layer(contract, source="synthetic", name="gadgets", displayOrder=99, token="gadget"),
    ]

    with pytest.raises(ModelRegistryError, match="'gadget' and 'gadgets' both declare the token 'gadget'"):
        build_model_registry(contract=contract, binding=load_binding())


def test_a_layer_declaring_no_token_falls_back_to_its_own_name() -> None:
    """The optional key falls back to the layer's NAME.

    Deliberately not `snapshot_attribute`, the `deprecations`-owned adapter bridge
    this declaration exists to stop depending on: falling back to it would derive a
    live public output from a legacy binding section, and would answer `swcs` for
    `swc`.
    """
    stripped = copy.deepcopy(load_contract())
    for layer in stripped["layers"]:
        assert layer.pop("token") is not None, f"{layer['name']} declares no token"
    registry = build_model_registry(contract=stripped, binding=load_binding())

    assert [layer.token for layer in registry.layers] == list(registry.layer_names)
    assert registry.layer("swc").snapshot_attribute == "swcs"
    assert registry.layer("swc").token == "swc"


def test_the_declared_token_is_projected_onto_layer_facts() -> None:
    """``layerDecl.token`` reaches :attr:`LayerFacts.token`, for every layer.

    Written out rather than read from the contract, since the projection's expected
    value may not be the document it projects: these are the values
    ``swcraft impact`` publishes as ``ElementRef.layer``.
    """
    registry = production_registry()
    assert {layer.name: layer.token for layer in registry.layers} == {
        "swc": "swc",
        "interfaces": "interface",
        "connectors": "connector",
        "implementation_types": "implementation_type",
        "requirements": "requirement",
        "compositions": "composition",
        "tasks": "task",
        "sequences": "sequence",
        "hardware": "hardware",
        "mapping_sets": "mapping_set",
        "base_types": "base_type",
        "application_types": "application_type",
        "units": "unit",
        "compu_methods": "compu_method",
        "data_constraints": "data_constraint",
    }


def test_an_aliased_identity_field_is_refused_at_construction() -> None:
    """The assumption three readers make, turned into a refusal.

    `model_state._read_state`, `differ.diff_layer`'s `attrgetter` and
    `session`'s identity index all read the identity field off an element as a
    plain Python attribute, so all three assume the contract's field *name* is
    the attribute. `binding.fieldNames` exists because the two can differ
    (`connector.from` -> `from_`); an aliased identity would surface as an
    `AttributeError` while loading a model, far from the declaration that caused
    it.
    """
    binding = copy.deepcopy(load_binding())
    binding["fieldNames"]["field:tasks.task.name"] = "name_"

    with pytest.raises(ModelRegistryError, match="an identity field may not be aliased"):
        build_model_registry(contract=load_contract(), binding=binding)


def test_kind_of_model_answers_none_for_a_class_no_layer_declares() -> None:
    """The `None` return for a class no layer declares.

    `impact` and `differ` both branch on it, so a rewrite that raised instead would
    turn a foreign element into a crash rather than into "not a declared kind".
    """
    registry = production_registry()

    assert registry.kind_of_model(Task) is not None
    assert registry.kind_of_model(SyntheticElement) is None


def test_a_binding_naming_a_module_that_does_not_import_is_refused() -> None:
    """Both import-resolving sites refuse a module that does not import.

    The message they raise is the only thing that tells an author which binding
    row is wrong.
    """
    binding = copy.deepcopy(load_binding())
    binding["kinds"]["kind:tasks.task"] = {"module": "swcraft_tools.no_such_module", "class": "Task"}
    with pytest.raises(ModelRegistryError, match="which does not import"):
        build_model_registry(contract=load_contract(), binding=binding)

    binding = copy.deepcopy(load_binding())
    binding["relations"]["field:hardware.memory.accessible"] = {
        "module": "swcraft_tools.no_such_module",
        "callable": "resolve_list_values",
    }
    with pytest.raises(ModelRegistryError, match="which does not import"):
        build_model_registry(contract=load_contract(), binding=binding)


def test_the_resolvers_read_an_attribute_off_whatever_they_are_handed() -> None:
    """No import from ``swcraft_tools`` at all -- the generic core's deepest leaf.

    A layer whose class is bound from outside the package must resolve exactly as
    the shipped ones do, so the resolvers may not know ``schema.py`` exists.
    """
    source = (REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "relations.py").read_text(encoding="utf-8")
    imported = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("swcraft_tools")
    ]
    assert imported == []


def _relation_decl(contract: dict[str, Any], field_id: str) -> dict[str, Any]:
    for layer in contract["layers"]:
        for kind in layer["kinds"]:
            for field in kind["fields"]:
                if field["id"] == field_id:
                    return field["relation"]
    raise AssertionError(field_id)


# ---------------------------------------------------------------------------
# Pure path composition
# ---------------------------------------------------------------------------


def test_pure_composition_reproduces_every_layers_composed_path() -> None:
    """Layer by layer, the kernel and the guarded composer agree on a legal input.

    ``swc`` is included with its group, because it is the one layer whose path
    has a directory between the layer and the file and therefore the one the
    two composers could disagree about.
    """
    registry = production_registry()
    for name in registry.layer_names:
        placement = {"group": "sensors"} if registry.storage(name).is_recursive else None
        identity = "REQ-1" if name == "requirements" else "Element"
        assert registry.compose_path(name, identity, placement) == reg.REGISTRY.compose_path(name, identity, placement)

    assert registry.compose_path("swc", "SpeedSensor", {"group": "sensors"}) == "swc/sensors/SpeedSensor.yaml"
    assert registry.compose_path("requirements", "REQ-1") == "requirements/REQ-1.yaml"


@pytest.mark.parametrize("group", [".", "a\nb", "R" * 300])
def test_pure_composition_accepts_a_placement_the_guarded_composer_refuses(group: str) -> None:
    """The asymmetry the kernel exists to preserve, asserted from both sides.

    ``writer/mutations.py``'s ``create_swc`` accepts a bare ``.``, a
    control-character and an over-long ``group``
    (``fixtures/recorded_oracles/writer_refusals.json``), while
    ``editing.Registry.compose_path`` refuses all three, so routing the writers
    through the guarded composer would tighten public input validation. This test
    fails if a guard is ever added to the pure composer.

    Accepting is not keeping the value as a segment: the composer builds its answer
    as one ``Path``, so the bare ``.`` contributes no segment and composes the file
    ``create_swc`` writes, ``swc/X.yaml``.
    """
    expected = "swc/X.yaml" if group == "." else f"swc/{group}/X.yaml"
    assert production_registry().compose_path("swc", "X", {"group": group}) == expected
    with pytest.raises(reg.PathSegmentError):
        reg.REGISTRY.compose_path("swc", "X", {"group": group})


def test_pure_composition_refuses_a_nesting_layer_with_no_placement_value() -> None:
    with pytest.raises(ModelRegistryError, match="needs a 'group' placement value"):
        production_registry().compose_path("swc", "X")


def test_the_free_composer_is_the_one_the_registry_delegates_to() -> None:
    """``compose_storage_path`` over a record no registry holds — the perturbation seam.

    ``editing/registry.py``'s guarded composer runs against registries built
    from *mutated* storage records that belong to no :class:`ModelRegistry` at
    all, so the composition rule has to be reachable as a free function over a
    record.
    """
    record = StorageRecord("elsewhere", "folder", ".json", "name")
    assert compose_storage_path(record, "X", {"folder": "f"}) == "elsewhere/f/X.json"
    assert compose_storage_path(StorageRecord("flat", "flat", ".yaml", "name"), "X") == "flat/X.yaml"


def test_the_composed_path_does_not_depend_on_the_hosts_path_flavour(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backslash inside a placement value stays inside that segment, on every host.

    The composed path is a file name and the element order key, so it must not
    depend on which concrete ``Path`` the host has: on Windows a backslash parses
    as a separator, and ``swc/a\\b/Foo.yaml`` would compose ``swc/a/b/Foo.yaml``
    there and ``swc/a\\b/Foo.yaml`` everywhere else. Measured by handing the module
    the Windows flavour: the composer parses a ``PurePosixPath`` and never the name
    ``Path``, so the answer is unchanged. Only the non-validating ``create_*``
    mutations reach such a value — the editing protocol refuses a separator.
    """
    from pathlib import PureWindowsPath

    import swcraft_tools.model_registry as model_registry_module

    swc = production_registry().storage("swc")
    # `raising=False`: the module needs no concrete `Path` at all once it parses a `PurePosixPath`.
    monkeypatch.setattr(model_registry_module, "Path", PureWindowsPath, raising=False)

    assert compose_storage_path(swc, "Foo", {"group": "a\\b"}) == "swc/a\\b/Foo.yaml"


def test_a_placement_value_that_is_no_segment_composes_no_segment() -> None:
    """An empty or bare-``.`` placement value composes the file, ``swc/{name}.yaml``, by construction.

    Joining the parts with ``"/"`` would compose ``swc//Foo.yaml`` for an empty
    value, a spelling no file has that sorts in front of every group directory
    (``/`` is 0x2F); the answer is one ``Path`` over the joined parts, spelled
    ``as_posix()``.

    One ``Path`` over the joined string, and **not** a ``Path`` join per part --
    the ``/abs`` row is why. A per-part join re-roots on a value that parses as
    absolute (``Path("swc") / "/abs"`` is ``/abs``, and on Windows a ``C:`` part is
    a drive). The composer still validates nothing -- ``/abs`` is refused by the
    guard in front of it and by the writers' own segment check, never here -- and
    it keeps its one refusal, a nesting record with no placement value at all.
    """
    swc = production_registry().storage("swc")
    assert compose_storage_path(swc, "Foo", {"group": "sensors"}) == "swc/sensors/Foo.yaml"
    assert compose_storage_path(swc, "Foo", {"group": ""}) == "swc/Foo.yaml"
    assert compose_storage_path(swc, "Foo", {"group": "."}) == "swc/Foo.yaml"
    assert compose_storage_path(swc, "Foo", {"group": "/abs"}) == "swc/abs/Foo.yaml"
    assert production_registry().compose_path("swc", "Foo", {"group": ""}) == "swc/Foo.yaml"
    for placement in (None, {}):
        with pytest.raises(ModelRegistryError, match="needs a 'group' placement value"):
            compose_storage_path(swc, "Foo", placement)


def test_the_synthetic_layers_path_composes_with_no_production_edit(merged: ModelRegistry) -> None:
    assert merged.compose_path("synthetic", "S1") == "synthetic/S1.yaml"


# ---------------------------------------------------------------------------
# The injection seam
# ---------------------------------------------------------------------------


def test_current_registry_is_the_production_one_outside_a_block() -> None:
    assert current_registry() is production_registry()


def test_use_registry_sets_and_restores(merged: ModelRegistry) -> None:
    before = current_registry()
    with use_registry(merged) as active:
        assert active is merged
        assert current_registry() is merged
    assert current_registry() is before


def test_use_registry_restores_when_the_block_raises(merged: ModelRegistry) -> None:
    before = current_registry()
    with pytest.raises(RuntimeError, match="deliberate"), use_registry(merged):
        raise RuntimeError("deliberate")
    assert current_registry() is before


def _layer_count(registry: ModelRegistry | None = None) -> int:
    """A helper written the way every migrated one is: resolved in the body."""
    registry = registry or current_registry()
    return len(registry.layers)


def test_a_registry_none_parameter_resolves_at_call_time_and_not_at_definition_time(
    merged: ModelRegistry,
) -> None:
    """The assertion is the *pair* of calls, and a single call would not make it.

    A default-argument expression (``registry: ModelRegistry = current_registry()``)
    is evaluated once, when the module is imported, and would answer ``15`` in
    both calls below. Switching the context between two calls of the same helper
    is what tells the two spellings apart.
    """
    assert _layer_count() == 15
    with use_registry(merged):
        assert _layer_count() == 16
    assert _layer_count() == 15

    # And an explicitly passed registry still wins over the active context.
    with use_registry(merged):
        assert _layer_count(production_registry()) == 15


def test_no_default_argument_expression_captures_a_registry() -> None:
    """The rule stated over the source, so a future helper cannot reintroduce the bug.

    ``registry: ModelRegistry | None = None`` is the only admissible spelling of
    the seam. A call in a default position — ``= current_registry()`` — binds
    the production contract at import and no context switch can reach it.
    """
    import ast

    for relative in ("model_registry.py", "editing/registry.py"):
        source = (REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / relative).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            defaults = [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]
            offenders = [
                ast.unparse(default)
                for default in defaults
                if isinstance(default, ast.Call) and "registry" in ast.unparse(default.func)
            ]
            assert offenders == [], f"{relative}:{node.name} resolves a registry in a default argument: {offenders}"


# ---------------------------------------------------------------------------
# `editing/registry.py` as a projection
# ---------------------------------------------------------------------------


def test_the_production_projection_is_the_public_registry() -> None:
    """``REGISTRY`` keeps its identity: the concrete projection of the shipped contract."""
    assert reg.current() is reg.REGISTRY
    assert reg.current(production_registry()) is reg.REGISTRY


def test_the_projection_is_cached_per_model_registry(merged: ModelRegistry) -> None:
    first = reg.current(merged)
    assert reg.current(merged) is first
    assert first is not reg.REGISTRY
    assert merged_registry() is not merged  # a fresh kernel registry is a fresh projection
    assert reg.current(merged_registry()) is not first


def test_the_projection_resolves_the_context_when_no_registry_is_passed(merged: ModelRegistry) -> None:
    with use_registry(merged):
        assert reg.current() is reg.current(merged)
    assert reg.current() is reg.REGISTRY


def test_the_synthetic_layer_reaches_the_editing_registry(merged: ModelRegistry) -> None:
    """The projection is generic: the synthetic layer gets a descriptor with no source edit."""
    projection = reg.current(merged)
    assert projection.layer_names[-1] == "synthetic"
    descriptor = projection.layer("synthetic")
    assert [kind.kind for kind in descriptor.element_kinds] == ["synthetic"]
    assert descriptor.identity_field == "name"
    assert descriptor.snapshot_attribute == "synthetic"
    assert projection.compose_path("synthetic", "S1") == "synthetic/S1.yaml"


def test_a_one_description_perturbation_changes_exactly_that_descriptor() -> None:
    """A contract edit reaches the published descriptor, and reaches nothing else.

    The projection is only load-bearing if a change to the contract is *visible*
    in what ``discover`` publishes — and only safe if it is visible in nothing
    else. Both halves are asserted here over a temp contract that differs from
    the shipped one by one string.
    """
    contract = copy.deepcopy(load_contract())
    field = next(
        f
        for layer in contract["layers"]
        for kind in layer["kinds"]
        for f in kind["fields"]
        if f["id"] == "field:tasks.task.priority"
    )
    field["description"] = "A perturbed description."

    perturbed = reg.current(build_model_registry(contract=contract, binding=load_binding()))
    changed = perturbed.element_kind("task").get_field("priority")
    assert changed is not None and changed.description == "A perturbed description."

    def _rows(registry: reg.Registry) -> list[tuple[str, str, str | None]]:
        return [
            (kind.kind, descriptor.name, descriptor.description)
            for kind in registry.kinds.values()
            for descriptor in kind.fields
        ]

    differences = [
        (before, after) for before, after in zip(_rows(reg.REGISTRY), _rows(perturbed), strict=True) if before != after
    ]
    assert differences == [
        (("task", "priority", "Must be >= 0; higher is more urgent."), ("task", "priority", "A perturbed description."))
    ]


def test_the_storage_record_is_the_kernels_own_class() -> None:
    """``editing/registry.py`` re-exports the kernel's class: one class, not two."""
    assert reg.StorageRecord is StorageRecord
    assert reg.REGISTRY.storage("swc") == StorageRecord("swc", "group", ".yaml", "name")


def test_the_kernel_imports_nothing_from_the_editing_package() -> None:
    """The kernel imports nothing from the ``editing`` package.

    ``test_editing_package_boundaries.py`` sweeps every module outside
    ``editing/``; this states the same about the kernel, so that a reader of this
    file learns why the kernel is a leaf rather than a member of the package that
    consumes it.
    """
    import ast

    tree = ast.parse(Path(kernel.__file__).read_text(encoding="utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert [name for name in imported if name.startswith("swcraft_tools.editing")] == []
    assert "swcraft_tools.contract" in imported


def _member_kinds_declaring(*declared: dict[str, Any]) -> SimpleNamespace:
    """A stand-in registry whose member kinds ``k0``, ``k1``, … declare the optional fields of *declared*.

    Each dict maps a field name to its declared default; a name a dict leaves out
    is a field that kind does not declare optional with a default.
    """

    def kind(defaults: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            get_field=lambda name: (
                SimpleNamespace(required=False, has_default=True, default=defaults[name]) if name in defaults else None
            )
        )

    return SimpleNamespace(_kinds={f"k{index}": kind(defaults) for index, defaults in enumerate(declared)})


@pytest.mark.parametrize(
    ("member_defaults", "expected"),
    [
        pytest.param(({"swc": None, "instance": None},), ("swc", "instance"), id="null-default-in-the-one-kind"),
        pytest.param(({"swc": "A", "instance": None},), ("instance",), id="a-non-null-default-is-not-optional"),
        pytest.param(({"swc": None, "instance": None}, {"instance": None}), ("instance",), id="mixed-member-kinds"),
        pytest.param(({"instance": None}, {"swc": None, "instance": ""}), (), id="a-default-other-than-null"),
        pytest.param((), (), id="no-member-kind"),
    ],
)
def test_an_identity_field_is_optional_only_with_a_null_default_in_every_member_kind(
    member_defaults: tuple[dict[str, Any], ...], expected: tuple[str, ...]
) -> None:
    stand_in = _member_kinds_declaring(*member_defaults)
    answer = ModelRegistry.optional_identity_fields(
        stand_in,  # type: ignore[arg-type]
        ("swc", "instance", "runnable"),
        tuple(stand_in._kinds),
    )
    assert answer == expected


def test_the_projection_carries_the_kernels_optional_identity_fields() -> None:
    """The kernel decides the fact and ``editing/registry.py`` projects it: one answer on both sides."""
    registry = kernel.production_registry()
    composite = [c for c in registry.collections() if c.member_identity == "composite"]
    assert composite
    for collection in composite:
        projected = reg.REGISTRY.element_kind(collection.kind).get_field(collection.field).collection
        assert projected is not None
        assert projected.optional_identity_fields == registry.optional_identity_fields(
            collection.identity_fields, collection.member_kinds
        )


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        pytest.param({"swc": "A", "runnable": "Run"}, {"swc": "A", "instance": None, "runnable": "Run"}, id="two-keys"),
        pytest.param({"runnable": "Run"}, {"swc": None, "instance": None, "runnable": "Run"}, id="no-selector"),
        pytest.param({"swc": "A", "instance": None, "runnable": "Run"}, None, id="already-complete"),
        pytest.param({"swc": "A"}, None, id="a-required-field-missing"),
        pytest.param({"swc": "A", "runnable": "Run", "extra": 1}, None, id="a-key-outside-the-tuple"),
        pytest.param(["A", "Run"], None, id="not-an-object"),
    ],
)
def test_a_composite_identity_is_completed_only_by_its_optional_fields(identity: Any, expected: Any) -> None:
    answer = kernel.complete_composite_identity(identity, ("swc", "instance", "runnable"), ("swc", "instance"))
    if expected is None:
        assert answer is identity
    else:
        assert answer == expected
