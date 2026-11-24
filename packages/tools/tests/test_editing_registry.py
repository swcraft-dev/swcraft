"""The schema-derived editing registry.

1. **the inventory** — the layers in the contract's order, the ``requirements``
   -> ``id`` special case, the kind-discriminated layers, no optional layer, and
   the full field set of every element kind;
2. **the six invariants** — every published field is a derived field, storage is
   one-way, every client value that reaches a path is a single segment, a
   placement field is immutable, no accessor creates a directory, one
   vocabulary end to end — also stated as properties over every kind and layer;
3. **the failure cases** — an annotation for an absent field, a second placement
   field, and a path composed from a value that is not a single segment.

The immutable set is pinned item by item rather than against the registry's own
derivation, because a derivation that publishes the set short is the defect.
"""

from __future__ import annotations

import gc
import json
import os
import re
from pathlib import Path

import editing_vector_fixtures as fixtures
import pytest
from conftest import REPO_ROOT
from model_contract_fixtures import merged_binding, merged_contract
from swcraft_tools import model_registry as kernel
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import attribute_for
from swcraft_tools.editing.registry import _scalar_type_name, _type_name
from swcraft_tools.schema import Core, DataElement, SWComponent
from swcraft_tools.writer.elements import _resolve_target

# The inventory

#: The production tables every perturbation below starts from, so a self-test
#: compares a real table with a broken copy of it, never a hand-written one.
_TABLES = reg._PRODUCTION_TABLES
_FIELD_ANNOTATIONS = reg._PRODUCTION_FIELD_ANNOTATIONS

#: The contract's layer order, which is also the order both project-wide discovery
#: vectors publish. ``ModelSnapshot`` declares the same fifteen layers in the
#: binding's own order instead, and no test holds it to this tuple.
EXPECTED_LAYERS = (
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


def test_fifteen_layers_in_claude_md_order() -> None:
    assert reg.REGISTRY.layer_names == EXPECTED_LAYERS


def test_requirements_is_the_one_layer_identified_by_id() -> None:
    by_id = {layer.name for layer in reg.REGISTRY.layers if layer.identity_field == "id"}
    assert by_id == {"requirements"}
    assert all(layer.identity_field == "name" for layer in reg.REGISTRY.layers if layer.name != "requirements")


def test_no_layer_is_optional() -> None:
    """No shipped layer is optional; a project contract may still declare one."""
    assert [layer.name for layer in reg.REGISTRY.layers if layer.optional] == []
    assert all(layer.optional is False for layer in reg.REGISTRY.layers)
    # The shipped contract's declared layers; a new layer moves it.
    assert len(reg.REGISTRY.layers) == 15


def test_the_kind_discriminated_layers_and_their_kinds() -> None:
    discriminated = {
        layer.name: tuple(kind.kind for kind in layer.top_level_kinds)
        for layer in reg.REGISTRY.layers
        if all(kind.discriminator_field == "kind" for kind in layer.top_level_kinds)
    }
    assert discriminated == {
        "interfaces": ("sender_receiver", "client_server", "external_interface"),
        "implementation_types": (
            "value_type",
            "reference_type",
            "external_type",
            "implementation_record",
            "implementation_array",
            "pointer",
        ),
        "hardware": ("processor", "core", "memory", "partition"),
        "application_types": ("enum", "record", "array", "application_type"),
        "compu_methods": ("texttable", "linear"),
    }
    for layer in reg.REGISTRY.layers:
        if layer.name not in discriminated:
            assert all(kind.discriminator_field is None for kind in layer.element_kinds)


def test_snapshot_attribute_differs_from_the_layer_name_for_swc_alone() -> None:
    renamed = {
        layer.name: layer.snapshot_attribute for layer in reg.REGISTRY.layers if layer.snapshot_attribute != layer.name
    }
    assert renamed == {"swc": "swcs"}


def test_nested_member_kinds_are_element_kinds_of_their_own_layer() -> None:
    member_only = {
        layer.name: tuple(kind.kind for kind in layer.element_kinds if kind.member_only)
        for layer in reg.REGISTRY.layers
    }
    assert member_only == {
        # In the registry's walk order.
        "swc": (
            "port",
            "runnable",
            "variable",
            "exclusive_area",
            "port_com_spec",
            "timing_event",
            "init_event",
            "operation_event",
            "data_event",
            "call_returned_event",
            "data_read",
            "data_write",
            "operation_call",
            "variable_read",
            "variable_write",
            "can_enter_area",
            "runs_inside_area",
            "disabled_mode",
        ),
        "interfaces": ("data_element", "operation", "argument"),
        "connectors": ("connector_endpoint",),
        "implementation_types": ("implementation_element",),
        "requirements": (),
        "compositions": ("composition_port", "composition_member", "delegation"),
        "tasks": ("task_runnable_ref",),
        "sequences": ("sequence_step",),
        "hardware": (),
        "mapping_sets": ("map",),
        "base_types": (),
        # A conversion and a range are fixed records with no identity of their
        # own, like `connector_endpoint`.
        "application_types": (
            "enum_literal",
            "record_field",
            "conversion",
            "range",
            "conversion_limits",
            "conversion_limit_bound",
        ),
        "units": (),
        "compu_methods": ("table_entry", "linear_limits", "linear_limit_bound"),
        "data_constraints": (),
    }


def test_every_member_only_kind_says_where_it_appears() -> None:
    for descriptor in reg.REGISTRY.kinds.values():
        if descriptor.member_only:
            assert descriptor.member_of, f"{descriptor.kind} is member_only with no member_of"
            for owner, field_name in descriptor.member_of:
                owner_kind = reg.REGISTRY.element_kind(owner)
                assert owner_kind.layer == descriptor.layer
                assert owner_kind.get_field(field_name) is not None
        else:
            assert descriptor.member_of == ()


def test_connector_endpoint_is_reached_by_two_object_fields() -> None:
    endpoint = reg.REGISTRY.element_kind("connector_endpoint")
    assert endpoint.member_of == (("connector", "from"), ("connector", "to"))
    connector = reg.REGISTRY.element_kind("connector")
    for name in ("from", "to"):
        descriptor = connector.get_field(name)
        assert descriptor is not None
        assert descriptor.element_kind == "connector_endpoint"
        assert descriptor.collection is None


def test_connector_from_is_published_under_its_serialization_alias() -> None:
    assert "from" in reg.REGISTRY.all_fields("connector")
    assert "from_" not in reg.REGISTRY.all_fields("connector")


# Member identity of every collection, in the protocol vocabulary

EXPECTED_COLLECTIONS = {
    ("swc", "ports"): ("list", "field", "name"),
    ("swc", "runnables"): ("list", "field", "name"),
    ("swc", "memory"): ("map", "whole_value", None),
    # A map on a member kind.
    ("port", "measures"): ("map", "whole_value", None),
    ("sender_receiver", "data_elements"): ("list", "field", "name"),
    ("client_server", "operations"): ("list", "field", "name"),
    ("operation", "arguments"): ("list", "field", "name"),
    ("enum", "literals"): ("list", "field", "name"),
    ("record", "fields"): ("list", "field", "name"),
    ("task", "runnables"): ("list", "composite", None),
    ("sequence", "steps"): ("list", "composite", None),
    ("runnable", "events"): ("list", "whole_value", None),
    ("memory", "accessible"): ("list", "whole_value", None),
    # Composite on the `(port, element)` / `(port, operation)` pair.
    ("runnable", "reads"): ("list", "composite", None),
    ("runnable", "writes"): ("list", "composite", None),
    ("runnable", "calls"): ("list", "composite", None),
    ("composition", "ports"): ("list", "field", "name"),
    ("composition", "members"): ("list", "field", "name"),
    ("composition", "delegations"): ("list", "field", "name"),
    # A pair is addressed by the application type it maps, which may appear at
    # most once in a set.
    ("mapping_set", "maps"): ("list", "field", "application"),
    ("swc", "data_type_mappings"): ("list", "whole_value", None),
    ("implementation_record", "elements"): ("list", "field", "name"),
    # A com-spec is identified by the data element it names. Disabled modes sit
    # inside a whole-value member, so they have no addressable interior either.
    ("port", "com_specs"): ("list", "field", "data_element"),
    ("timing_event", "disabled_modes"): ("list", "whole_value", None),
    ("data_event", "disabled_modes"): ("list", "whole_value", None),
    ("texttable", "entries"): ("list", "field", "name"),
    # An interface stub's two lists of member names.
    ("external_interface", "operations"): ("list", "whole_value", None),
    ("external_interface", "data_elements"): ("list", "whole_value", None),
    # The trace links: `whole_value` lists of `<id>@<version>` strings.
    ("swc", "satisfies"): ("list", "whole_value", None),
    ("port", "satisfies"): ("list", "whole_value", None),
    ("runnable", "satisfies"): ("list", "whole_value", None),
    ("sender_receiver", "satisfies"): ("list", "whole_value", None),
    ("client_server", "satisfies"): ("list", "whole_value", None),
    ("external_interface", "satisfies"): ("list", "whole_value", None),
    ("composition", "satisfies"): ("list", "whole_value", None),
    ("task", "satisfies"): ("list", "whole_value", None),
    ("processor", "satisfies"): ("list", "whole_value", None),
    ("core", "satisfies"): ("list", "whole_value", None),
    ("memory", "satisfies"): ("list", "whole_value", None),
    ("partition", "satisfies"): ("list", "whole_value", None),
    ("requirement", "derived_from"): ("list", "whole_value", None),
    # A component's variables and areas by `name`; an access entry by the
    # variable or area it names.
    ("swc", "variables"): ("list", "field", "name"),
    ("runnable", "reads_variables"): ("list", "field", "variable"),
    ("runnable", "writes_variables"): ("list", "field", "variable"),
    ("swc", "exclusive_areas"): ("list", "field", "name"),
    ("runnable", "can_enter"): ("list", "field", "area"),
    ("runnable", "runs_inside"): ("list", "field", "area"),
}


def _declared_collections() -> dict[tuple[str, str], tuple[str, str, str | None]]:
    found: dict[tuple[str, str], tuple[str, str, str | None]] = {}
    for descriptor in reg.REGISTRY.kinds.values():
        for field_descriptor in descriptor.fields:
            collection = field_descriptor.collection
            if collection is not None:
                found[(descriptor.kind, field_descriptor.name)] = (
                    collection.kind,
                    collection.member_identity,
                    collection.identity_field,
                )
    return found


def test_forty_seven_collections_publish_their_member_identity() -> None:
    # The declared collections; a collection field added or retired moves it.
    assert _declared_collections() == EXPECTED_COLLECTIONS
    assert len(EXPECTED_COLLECTIONS) == 47


def test_composite_collections_publish_the_swc_runnable_pair() -> None:
    for kind, field_name in (("task", "runnables"), ("sequence", "steps")):
        collection = reg.REGISTRY.element_kind(kind).get_field(field_name).collection
        assert collection.member_identity == "composite"
        # The two selectors are optional and read as `null` when a payload or an
        # address leaves them out.
        assert collection.identity_fields == ("swc", "instance", "runnable")
        assert collection.optional_identity_fields == ("swc", "instance")
        assert collection.identity_field is None


def test_only_the_two_composite_collections_carry_optional_identity_fields() -> None:
    """Over every declared collection: only the two composite selectors are optional identity fields."""
    carrying = {
        (descriptor.kind, f.name): c.optional_identity_fields
        for descriptor in reg.REGISTRY.kinds.values()
        for f in descriptor.fields
        if (c := f.collection) is not None and c.optional_identity_fields
    }
    assert carrying == {("task", "runnables"): ("swc", "instance"), ("sequence", "steps"): ("swc", "instance")}


def test_the_one_union_collection_publishes_its_discriminator() -> None:
    events = reg.REGISTRY.element_kind("runnable").get_field("events").collection
    assert events.member_kinds == ("timing_event", "init_event", "operation_event", "data_event", "call_returned_event")
    assert events.discriminator_field == "type"
    others = [
        c
        for descriptor in reg.REGISTRY.kinds.values()
        for f in descriptor.fields
        if (c := f.collection) is not None and c is not events and c.discriminator_field is not None
    ]
    assert others == []


def test_scalar_collections_declare_no_member_kind() -> None:
    for kind, field_name in (("requirement", "derived_from"), ("swc", "satisfies"), ("swc", "memory")):
        assert reg.REGISTRY.element_kind(kind).get_field(field_name).collection.member_kinds == ()


# Invariant 1 — every published field is a derived field


def test_published_fields_are_exactly_model_fields_plus_the_placement_field() -> None:
    for descriptor in reg.REGISTRY.kinds.values():
        derived = {info.alias or attribute for attribute, info in descriptor.model.model_fields.items()}
        assert set(reg.REGISTRY.schema_fields(descriptor.kind)) == derived, descriptor.kind
        placement = {f.name for f in descriptor.fields if f.placement}
        assert set(reg.REGISTRY.all_fields(descriptor.kind)) == derived | placement, descriptor.kind


def test_schema_fields_and_all_fields_differ_for_swc_alone() -> None:
    differing = {
        kind: tuple(sorted(set(reg.REGISTRY.all_fields(kind)) - set(reg.REGISTRY.schema_fields(kind))))
        for kind in reg.REGISTRY.kinds
        if reg.REGISTRY.all_fields(kind) != reg.REGISTRY.schema_fields(kind)
    }
    assert differing == {"swc": ("group",)}


def test_group_is_declared_as_a_placement_field_and_not_a_schema_field() -> None:
    assert "group" not in SWComponent.model_fields
    group = reg.REGISTRY.element_kind("swc").get_field("group")
    assert group is not None
    assert (group.placement, group.required, group.immutable, group.type) == (True, True, True, "string")


def test_swc_field_order_places_the_placement_field_after_the_identity() -> None:
    assert reg.REGISTRY.all_fields("swc") == (
        "name",
        "group",
        "type",
        "description",
        "external",
        "external_ref",
        "external_dest",
        "ports",
        "runnables",
        "variables",
        "exclusive_areas",
        "memory",
        "asil",
        "handle_termination_and_restart",
        "data_type_mappings",
        "behavior_name",
        "package",
        "satisfies",
    )


def test_required_and_enum_are_read_off_the_schema() -> None:
    swc = reg.REGISTRY.element_kind("swc")
    assert swc.get_field("name").required is True
    assert swc.get_field("type").required is False
    assert swc.get_field("type").enum == ("application", "service", "ecu_abstraction")
    # A nullable field is optional and publishes the type of its non-None member.
    wcet = reg.REGISTRY.element_kind("task").get_field("wcet")
    assert (wcet.required, wcet.type, wcet.unit) == (False, "string", None)


def test_units_that_are_not_derivable_from_the_type_are_published() -> None:
    task = reg.REGISTRY.element_kind("task")
    assert task.get_field("period").unit == "seconds"
    assert reg.REGISTRY.element_kind("swc").get_field("memory").unit == "bytes"


def test_reference_fields_name_a_layer_and_its_identity_field() -> None:
    references = {
        (descriptor.kind, f.name): (f.reference.layer, f.reference.identity_field)
        for descriptor in reg.REGISTRY.kinds.values()
        for f in descriptor.fields
        if f.reference is not None
    }
    assert references == {
        ("port", "interface"): ("interfaces", "name"),
        ("data_element", "implementation_type"): ("implementation_types", "name"),
        ("argument", "implementation_type"): ("implementation_types", "name"),
        # A prototype states the side of its type: exactly one of the two is set.
        ("variable", "application_type"): ("application_types", "name"),
        ("variable", "implementation_type"): ("implementation_types", "name"),
        ("data_element", "application_type"): ("application_types", "name"),
        ("argument", "application_type"): ("application_types", "name"),
        ("connector_endpoint", "swc"): ("swc", "name"),
        # The one reference into `base_types`; `implementation_types` holds no
        # base type.
        ("value_type", "base"): ("base_types", "name"),
        ("reference_type", "base"): ("implementation_types", "name"),
        # An application composite's member resolves in `application_types` alone.
        ("array", "element_type"): ("application_types", "name"),
        ("record_field", "type"): ("application_types", "name"),
        ("composition_port", "interface"): ("interfaces", "name"),
        ("composition_member", "type"): ("swc", "name"),
        ("composition_member", "composition"): ("compositions", "name"),
        ("connector", "composition"): ("compositions", "name"),
        ("connector_endpoint", "composition"): ("compositions", "name"),
        ("task_runnable_ref", "swc"): ("swc", "name"),
        ("sequence", "task"): ("tasks", "name"),
        ("sequence_step", "swc"): ("swc", "name"),
        # The target layer holds several kinds, which a `referenceDecl` cannot
        # narrow; `hardware.partition-not-a-partition` says which kind it may name.
        ("task", "partition"): ("hardware", "name"),
        ("partition", "core"): ("hardware", "name"),
        # References, not relations: a relation names exactly one target kind and
        # each of these reaches several, so which kinds it may name is the validator's.
        ("map", "application"): ("application_types", "name"),
        ("map", "implementation"): ("implementation_types", "name"),
        # Which kinds each may name is the validator's reference-role family.
        ("implementation_element", "type"): ("implementation_types", "name"),
        ("implementation_array", "element_type"): ("implementation_types", "name"),
        ("pointer", "target"): ("implementation_types", "name"),
        ("application_type", "unit_ref"): ("units", "name"),
        ("application_type", "compu_method_ref"): ("compu_methods", "name"),
        ("application_type", "data_constr_ref"): ("data_constraints", "name"),
        ("enum", "compu_method_ref"): ("compu_methods", "name"),
        ("value_type", "compu_method_ref"): ("compu_methods", "name"),
        ("reference_type", "compu_method_ref"): ("compu_methods", "name"),
        ("linear", "unit"): ("units", "name"),
    }
    for layer_name, _ in references.values():
        assert layer_name in reg.REGISTRY.layer_names


# The published type vocabulary — the named recursive alias

#: The type-name grammar apart from `value`: a scalar, an element-kind name,
#: `list[<member>]` (` | `-separated for a discriminated union) or
#: `map[<key>, <value>]`. Written out rather than taken from `$defs/typeName`,
#: which accepts `value` and so cannot say that nothing else moved.
_PRE_3_1_0_TYPE_NAMES = re.compile(
    r"^([a-z][a-z0-9_]*"
    r"|list\[[a-z][a-z0-9_]*( \| [a-z][a-z0-9_]*)*\]"
    r"|map\[[a-z][a-z0-9_]*, [a-z][a-z0-9_]*\])$"
)


def test_a_named_recursive_alias_publishes_as_value() -> None:
    """`DataElement.init`, a named recursive `TypeAliasType`, is published as `value`.

    Unrecognised, it fell through to `"string"`. Asserted over the raw
    annotation and the published descriptor, which can disagree.
    """
    assert _type_name(DataElement.model_fields["init"].annotation, {}) == "value"
    init = reg.REGISTRY.element_kind("data_element").get_field("init")
    assert (init.type, init.required, init.immutable) == ("value", False, False)
    assert init.collection is None and init.reference is None and init.element_kind is None
    assert init.enum == ()


def test_value_is_published_at_exactly_one_field_and_nothing_else_moved() -> None:
    """`value` is published exactly at the fields annotated with the recursive alias.

    Every other published type matches the grammar without `value`.
    Does not reach whether each other name is the *same* name it was:
    `test_model_contract_conformance.py` compares that.
    """
    published = {
        (descriptor.kind, field.name): field.type
        for descriptor in reg.REGISTRY.kinds.values()
        for field in descriptor.fields
    }
    valued = [("variable", "init"), ("port_com_spec", "init"), ("data_element", "init")]
    assert [address for address, name in published.items() if name == "value"] == valued
    for address, name in published.items():
        if address in valued:
            continue
        assert _PRE_3_1_0_TYPE_NAMES.match(name), (address, name)
        assert name != "value", address


def test_an_annotation_the_projection_does_not_recognise_still_falls_through_to_string() -> None:
    """A bare union is not a `TypeAliasType` and still publishes as `"string"`, which is wrong.

    Pinned so the alias branch is not credited with a fix it did not make.
    """

    class _Unrecognised:
        pass

    assert _scalar_type_name(_Unrecognised, {}) == "string"
    assert _type_name(int | str, {}) == "string"
    assert _type_name(int | str | None, {}) == "string"


# Invariant 6 — one vocabulary end to end


def test_element_kind_names_are_globally_unique() -> None:
    declared = [kind.kind for layer in reg.REGISTRY.layers for kind in layer.element_kinds]
    assert len(declared) == len(set(declared))
    assert tuple(reg.REGISTRY.kinds) == tuple(declared)


def test_every_collection_member_kind_is_an_element_kind_of_the_same_layer() -> None:
    for descriptor in reg.REGISTRY.kinds.values():
        for f in descriptor.fields:
            member_kinds = f.collection.member_kinds if f.collection else ()
            for member in (*member_kinds, *(([f.element_kind]) if f.element_kind else [])):
                assert reg.REGISTRY.element_kind(member).layer == descriptor.layer


def test_a_discriminated_kind_name_equals_its_own_discriminator_literal() -> None:
    for layer in reg.REGISTRY.layers:
        for kind in layer.top_level_kinds:
            if kind.discriminator_field is None:
                continue
            assert kind.get_field(kind.discriminator_field).enum == (kind.kind,)


# The immutable set, pinned item by item


def _immutable_pairs() -> set[tuple[str, str]]:
    return {
        (descriptor.kind, f.name)
        for descriptor in reg.REGISTRY.kinds.values()
        for f in descriptor.fields
        if f.immutable
    }


#: The immutable set, written out rather than derived: the layer identity
#: fields, the identity of every nested collection member, every field of a
#: `whole_value` LIST member kind, the `kind` discriminators and `swc.group`.
RECORDED_IMMUTABLE = {
    # the layer identity fields (`name`, `id`)
    ("swc", "name"),
    ("sender_receiver", "name"),
    ("client_server", "name"),
    ("connector", "name"),
    ("base_type", "name"),
    ("value_type", "name"),
    ("reference_type", "name"),
    ("enum", "name"),
    ("record", "name"),
    ("array", "name"),
    ("requirement", "id"),
    ("composition", "name"),
    ("task", "name"),
    ("sequence", "name"),
    ("processor", "name"),
    ("core", "name"),
    ("memory", "name"),
    ("partition", "name"),
    ("application_type", "name"),
    # the identity of every nested collection member
    ("port", "name"),
    ("runnable", "name"),
    ("data_element", "name"),
    ("operation", "name"),
    ("argument", "name"),
    ("enum_literal", "name"),
    ("record_field", "name"),
    ("composition_port", "name"),
    ("composition_member", "name"),
    ("delegation", "name"),
    ("mapping_set", "name"),
    # A pair's identity inside its set is the application type it maps; the
    # implementation side is content.
    ("map", "application"),
    ("task_runnable_ref", "swc"),
    ("task_runnable_ref", "runnable"),
    ("sequence_step", "swc"),
    ("sequence_step", "runnable"),
    # The second selector is part of the composite identity too.
    ("task_runnable_ref", "instance"),
    ("sequence_step", "instance"),
    # both halves of each access entry's composite identity; `by` on a read is
    # a form and stays mutable
    ("data_read", "port"),
    ("data_read", "element"),
    ("data_write", "port"),
    ("data_write", "element"),
    ("operation_call", "port"),
    ("operation_call", "operation"),
    # a com-spec entry's identity inside its port
    ("port_com_spec", "data_element"),
    # every field of a `whole_value` LIST member kind
    ("timing_event", "type"),
    ("timing_event", "period"),
    # the event's disabled modes are part of its whole value, and each disabled
    # mode is a `whole_value` member of its own
    ("timing_event", "disabled_modes"),
    ("disabled_mode", "port"),
    ("disabled_mode", "mode_group_ref"),
    ("disabled_mode", "mode_ref"),
    ("init_event", "type"),
    ("operation_event", "type"),
    ("operation_event", "port"),
    ("operation_event", "operation"),
    ("data_event", "type"),
    ("data_event", "port"),
    ("data_event", "element"),
    ("data_event", "disabled_modes"),
    ("data_event", "name"),
    ("call_returned_event", "type"),
    ("call_returned_event", "port"),
    ("call_returned_event", "operation"),
    ("call_returned_event", "result"),
    ("call_returned_event", "name"),
    # an event's stated name is part of its whole value too
    ("timing_event", "name"),
    ("init_event", "name"),
    ("operation_event", "name"),
    # two of the interfaces' three `kind` discriminators (the stub's is below)
    ("sender_receiver", "kind"),
    ("client_server", "kind"),
    # the one declared override: `base_type.kind` is an ordinary literal,
    # explicitly declared immutable
    ("base_type", "kind"),
    # the data type kinds' discriminators
    ("value_type", "kind"),
    ("reference_type", "kind"),
    ("enum", "kind"),
    ("record", "kind"),
    ("array", "kind"),
    ("application_type", "kind"),
    # the external stubs' discriminator and identity
    ("external_interface", "kind"),
    ("external_type", "kind"),
    ("external_interface", "name"),
    ("external_type", "name"),
    # the implementation kinds' discriminators and identities, and the
    # implementation record member's identity
    ("implementation_record", "kind"),
    ("implementation_array", "kind"),
    ("pointer", "kind"),
    ("implementation_record", "name"),
    ("implementation_array", "name"),
    ("pointer", "name"),
    ("implementation_element", "name"),
    # the compu method and unit kinds' identities and discriminators, and a
    # texttable entry's identity
    ("unit", "name"),
    ("texttable", "name"),
    ("linear", "name"),
    ("data_constr", "name"),
    ("texttable", "kind"),
    ("linear", "kind"),
    ("table_entry", "name"),
    ("processor", "kind"),
    ("core", "kind"),
    ("memory", "kind"),
    ("partition", "kind"),
    # the SWC group, the placement field
    ("swc", "group"),
    # a variable's and an exclusive area's identity, and the variable or area
    # each access entry names
    ("variable", "name"),
    ("variable_read", "variable"),
    ("variable_write", "variable"),
    ("exclusive_area", "name"),
    ("can_enter_area", "area"),
    ("runs_inside_area", "area"),
}


def test_the_immutable_set_is_exactly_the_recorded_pairs() -> None:
    assert _immutable_pairs() == RECORDED_IMMUTABLE


def test_a_whole_value_map_entry_keeps_a_mutable_value() -> None:
    # The key is the identity and the budget is ordinary content.
    memory = reg.REGISTRY.element_kind("swc").get_field("memory")
    assert memory.collection.kind == "map"
    assert memory.immutable is False


def test_a_nested_object_is_not_a_collection_member_and_stays_mutable() -> None:
    endpoint = reg.REGISTRY.element_kind("connector_endpoint")
    assert [f.immutable for f in endpoint.fields] == [False, False, False]


def test_the_one_field_immutable_by_annotation_alone_is_the_base_type_kind() -> None:
    """`base_type.kind` is the one field immutable by annotation alone.

    `_is_immutable`'s derivation is the oracle for the declared `immutable`, so
    an override is a field the oracle does not check. A planted override on a
    mutable field is the positive control that the mechanism still works.
    """
    overrides = {key for key, annotation in _FIELD_ANNOTATIONS.items() if annotation.immutable is not None}
    assert overrides == {("base_type", "kind")}

    annotations = dict(_FIELD_ANNOTATIONS)
    annotations[("core", "threshold")] = reg.FieldAnnotation(
        description=_FIELD_ANNOTATIONS[("core", "threshold")].description, immutable=True
    )
    perturbed = reg.build_registry(field_annotations=annotations)
    assert perturbed.element_kind("core").get_field("threshold").immutable is True
    assert reg.REGISTRY.element_kind("core").get_field("threshold").immutable is False
    assert Core.model_fields["threshold"].is_required()


# Invariants 2, 3 and 5 — storage is one-way and nothing maps back out


def test_compose_path_is_the_only_way_a_layer_becomes_a_path() -> None:
    assert reg.REGISTRY.compose_path("swc", "SpeedSensorSWC", {"group": "sensors"}) == "swc/sensors/SpeedSensorSWC.yaml"
    assert reg.REGISTRY.compose_path("implementation_types", "uint16") == "data_types/implementation/uint16.yaml"
    assert reg.REGISTRY.compose_path("requirements", "REQ-SPEED-001") == "requirements/REQ-SPEED-001.yaml"


def test_a_nested_layer_refuses_to_compose_without_its_placement_value() -> None:
    with pytest.raises(reg.PathSegmentError, match="group"):
        reg.REGISTRY.compose_path("swc", "Foo")


def test_no_published_descriptor_carries_a_directory_suffix_or_separator() -> None:
    # Prose is exempt: a description may quote a path. The parts a client
    # consumes structurally may not.
    structural: list[str] = []
    for layer_descriptor in reg.REGISTRY.layers:
        structural += [layer_descriptor.name, layer_descriptor.identity_field, layer_descriptor.snapshot_attribute]
        for kind in layer_descriptor.element_kinds:
            structural.append(kind.kind)
            for f in kind.fields:
                structural += [f.name, f.type, f.element_kind or "", f.unit or ""]
                if f.reference:
                    structural += [f.reference.layer, f.reference.identity_field]
    for value in structural:
        for token in (".yaml", "model_root", "/", "\\"):
            assert token not in value, f"{token!r} leaked into the published value {value!r}"


def test_storage_records_are_registry_internal_and_state_the_nesting_once() -> None:
    recursive = {name for name in reg.REGISTRY.layer_names if reg.REGISTRY.storage(name).is_recursive}
    assert recursive == {"swc"}
    assert reg.REGISTRY.storage("swc").nesting == "group"
    assert all(reg.REGISTRY.storage(name).file_suffix == ".yaml" for name in reg.REGISTRY.layer_names)
    assert reg.REGISTRY.storage("requirements").filename_from == "id"
    assert not hasattr(reg.REGISTRY.layer("swc"), "storage")


@pytest.mark.parametrize(
    "bad",
    ["", "   ", ".", "..", "../etc", "a/b", "a\\b", "a..b", "a\x00b"],
)
def test_a_value_that_is_not_a_single_segment_is_refused_before_any_path_is_built(bad: str) -> None:
    with pytest.raises(reg.PathSegmentError):
        reg.REGISTRY.compose_path("implementation_types", bad)
    with pytest.raises(reg.PathSegmentError):
        reg.REGISTRY.compose_path("swc", "Foo", {"group": bad})


@pytest.mark.parametrize(
    "bad",
    ["a\nb", "a\rb", "a\tb", "a\x1b[2Jb", "a\u2028b", "a\x85b"],
    ids=["lf", "cr", "tab", "ansi", "line-sep", "nel"],
)
def test_a_segment_carrying_a_control_character_is_refused(bad: str) -> None:
    """A segment becomes a file name and the identity `editing/review.py` shows a human approver."""
    with pytest.raises(reg.PathSegmentError):
        reg.REGISTRY.compose_path("requirements", bad)
    with pytest.raises(reg.PathSegmentError):
        reg.REGISTRY.compose_path("swc", "Foo", {"group": bad})


def test_a_segment_longer_than_a_file_name_can_hold_is_refused_rather_than_composed() -> None:
    """An over-long identity is refused before staging, not an ``OSError`` mid-materialization.

    That ``OSError`` audited as "the model may be partially applied", which was
    not true.
    """
    assert reg.REGISTRY.compose_path("requirements", "R" * kernel._MAX_SEGMENT_BYTES)
    with pytest.raises(reg.PathSegmentError) as refusal:
        reg.REGISTRY.compose_path("requirements", "R" * (kernel._MAX_SEGMENT_BYTES + 1))
    assert str(kernel._MAX_SEGMENT_BYTES) in str(refusal.value)


def test_the_length_bound_is_over_encoded_bytes_because_that_is_what_a_filesystem_caps() -> None:
    # The 255-byte component limit filesystems enforce is over the encoded name.
    identity = "\u4e2d" * (kernel._MAX_SEGMENT_BYTES // 3 + 1)
    assert len(identity) < kernel._MAX_SEGMENT_BYTES
    with pytest.raises(reg.PathSegmentError):
        reg.REGISTRY.compose_path("requirements", identity)


def test_a_placement_directory_gets_the_whole_path_component_and_not_a_file_name_s_share() -> None:
    """A placement directory carries no extension or staging suffix, so it gets the whole component.

    `read._publishable` asks the same guard of a stored ``group``, so the
    identity's bound refused a whole-layer read of a project that reads fine.
    """
    group = "g" * (kernel._MAX_SEGMENT_BYTES + 1)
    composed = reg.REGISTRY.compose_path("swc", "Foo", {"group": group})

    assert group in composed
    assert all(len(component.encode("utf-8")) <= 255 for component in composed.split("/"))
    with pytest.raises(reg.PathSegmentError):
        reg.REGISTRY.compose_path("swc", "Foo", {"group": "g" * 256})
    assert reg.segment_refusal(group, "swc group", directory=True) is None
    assert reg.segment_refusal(group, "swc identity") is not None


def test_the_bound_leaves_room_for_the_extension_and_the_commit_staging_suffix() -> None:
    # `.yaml` plus `.<32 hex>.swcraft-commit.tmp`, under the 255-byte component limit.
    composed = reg.REGISTRY.compose_path("requirements", "R" * kernel._MAX_SEGMENT_BYTES)
    longest = f"{composed.rsplit('/', 1)[-1]}.{'0' * 32}.swcraft-commit.tmp"
    assert len(longest.encode("utf-8")) <= 255


def test_the_bound_is_exactly_what_the_staged_file_name_leaves() -> None:
    """The bound is exactly what ``session._staging_path``'s staged name leaves, both sides.

    An extra reserve refused legal identities already on disk, which then read
    but could not be edited.
    """
    from pathlib import Path

    from swcraft_tools.session import _staging_path

    def staged_length(identity_bytes: int) -> int:
        target = Path("/model/requirements") / f"{'R' * identity_bytes}.yaml"
        return len(_staging_path(target).name.encode("utf-8"))

    assert staged_length(kernel._MAX_SEGMENT_BYTES) == kernel._MAX_COMPONENT_BYTES
    assert staged_length(kernel._MAX_SEGMENT_BYTES + 1) == kernel._MAX_COMPONENT_BYTES + 1
    assert reg.segment_refusal("R" * kernel._MAX_SEGMENT_BYTES, "requirements identity") is None
    assert reg.segment_refusal("R" * (kernel._MAX_SEGMENT_BYTES + 1), "requirements identity") is not None


def test_a_non_string_identity_is_refused_rather_than_coerced() -> None:
    with pytest.raises(reg.PathSegmentError):
        reg.REGISTRY.compose_path("implementation_types", 17)  # type: ignore[arg-type]


def test_the_segment_rule_is_at_least_as_strict_as_the_writers_own_guard() -> None:
    """The registry may be stricter than ``check_path_segment`` but never accepts what it refuses."""
    from swcraft_tools.writer.mutations import check_path_segment

    for candidate in ("sensors", "a/b", "a\\b", "..", "a..b", "../x", "group.1"):
        try:
            check_path_segment(candidate, "group")
        except ValueError:
            with pytest.raises(reg.PathSegmentError):
                reg.REGISTRY.compose_path("swc", "Foo", {"group": candidate})


# Three shipped discovery vectors, compared structurally: the registry can publish
# every descriptor they state, so a mismatch is found here, not in the serializer.
# Does NOT reach the absent-layer vector (its shape is the engine's) or the
# per-layer `discover-layer-<layer>-full.json` recordings; `test_editing_vectors.py`
# executes them all.

VECTOR_DIR = "packages/tools/tests/fixtures/contracts/editing-protocol/examples"


def _publish_field(descriptor: reg.FieldDescriptor) -> dict[str, object]:
    out: dict[str, object] = {
        "name": descriptor.name,
        "type": descriptor.type,
        "required": descriptor.required,
        "immutable": descriptor.immutable,
    }
    if descriptor.unit:
        out["unit"] = descriptor.unit
    if descriptor.enum:
        out["enum"] = list(descriptor.enum)
    if descriptor.reference:
        out["reference"] = {
            "layer": descriptor.reference.layer,
            "identity_field": descriptor.reference.identity_field,
        }
    if descriptor.element_kind:
        out["element_kind"] = descriptor.element_kind
    if descriptor.collection:
        collection: dict[str, object] = {
            "kind": descriptor.collection.kind,
            "member_identity": descriptor.collection.member_identity,
        }
        if descriptor.collection.identity_field:
            collection["identity_field"] = descriptor.collection.identity_field
        if descriptor.collection.identity_fields:
            collection["identity_fields"] = list(descriptor.collection.identity_fields)
        if descriptor.collection.member_kinds:
            collection["member_kinds"] = list(descriptor.collection.member_kinds)
        if descriptor.collection.discriminator_field:
            collection["discriminator_field"] = descriptor.collection.discriminator_field
        out["collection"] = collection
    return out


def _publish_layer(descriptor: reg.LayerDescriptor, present: bool) -> dict[str, object]:
    kinds: list[dict[str, object]] = []
    for kind in descriptor.element_kinds:
        entry: dict[str, object] = {"kind": kind.kind}
        if kind.discriminator_field:
            entry["discriminator_field"] = kind.discriminator_field
        if kind.member_only:
            entry["member_only"] = True
            entry["member_of"] = [{"kind": owner, "field": name} for owner, name in kind.member_of]
        entry["fields"] = [_publish_field(f) for f in kind.fields]
        kinds.append(entry)
    return {
        "layer": descriptor.name,
        "optional": descriptor.optional,
        "present": present,
        "identity_field": descriptor.identity_field,
        "element_kinds": kinds,
    }


def _strip_descriptions(node: object) -> object:
    """Drop every ``description`` key at every depth."""
    if isinstance(node, dict):
        return {key: _strip_descriptions(value) for key, value in node.items() if key != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(item) for item in node]
    return node


@pytest.mark.parametrize(
    ("vector", "present"),
    [
        ("discover-project-all-layers-present.json", {}),
        ("discover-layer-implementation-types.json", {}),
        ("discover-layer-hardware.json", {}),
    ],
)
def test_the_registry_publishes_what_the_discovery_vectors_state(vector: str, present: dict[str, bool]) -> None:
    """The registry's published layer equals the shipped vector, descriptions stripped.

    A prose-only contract patch is not a change to what the registry publishes.
    The vectors are ``summary`` answers, so the omitted ``false`` booleans are
    restored first.
    """
    payload = json.loads((REPO_ROOT / VECTOR_DIR / vector).read_text(encoding="utf-8"))
    assert payload["expected"]["response"]["form"] == "summary", vector
    expected = fixtures.with_the_summary_defaults_restored(payload["expected"]["response"]["layers"])
    covered = [entry["layer"] for entry in expected]
    actual = [_publish_layer(reg.REGISTRY.layer(name), present.get(name, True)) for name in covered]
    assert _strip_descriptions(actual) == _strip_descriptions(expected)


# Construction errors


def test_an_annotation_for_a_field_that_does_not_exist_fails_construction() -> None:
    annotations = dict(_FIELD_ANNOTATIONS)
    annotations[("swc", "safety_level")] = reg.FieldAnnotation(description="not a schema.py field")
    with pytest.raises(reg.RegistryError, match="only annotate"):
        reg.build_registry(field_annotations=annotations)


def test_an_annotation_for_an_element_kind_that_does_not_exist_fails_construction() -> None:
    annotations = dict(_FIELD_ANNOTATIONS)
    annotations[("widget", "name")] = reg.FieldAnnotation(description="no such kind")
    with pytest.raises(reg.RegistryError, match="only annotate"):
        reg.build_registry(field_annotations=annotations)


def test_a_second_placement_field_is_refused() -> None:
    with pytest.raises(reg.RegistryError, match="exactly one placement field"):
        reg.build_registry(
            placement_fields=(
                *_TABLES.placement_fields,
                reg.PlacementFieldSpec(layer="tasks", kind="task", name="folder"),
            )
        )


def test_a_placement_field_may_not_shadow_a_schema_field() -> None:
    with pytest.raises(reg.RegistryError, match="collides with a schema field"):
        reg.build_registry(
            placement_fields=(reg.PlacementFieldSpec(layer="swc", kind="swc", name="description"),),
        )


def test_a_collection_with_no_declared_member_identity_fails_construction() -> None:
    specs = dict(_TABLES.collection_specs)
    del specs[("swc", "ports")]
    with pytest.raises(reg.RegistryError, match="no declared member identity"):
        reg.build_registry(collection_specs=specs)


def test_a_collection_spec_for_a_field_that_is_not_a_collection_fails_construction() -> None:
    specs = dict(_TABLES.collection_specs)
    specs[("swc", "description")] = reg.CollectionSpec("whole_value")
    with pytest.raises(reg.RegistryError, match="not collections"):
        reg.build_registry(collection_specs=specs)


def test_an_element_kind_name_disagreeing_with_its_discriminator_literal_fails() -> None:
    names = dict(_TABLES.kind_names)
    names[Core] = "cpu"
    # `core.satisfies` is keyed by the kind's name: re-keyed with it, so the
    # construction reaches the discriminator check, not the missing member identity.
    specs = dict(_TABLES.collection_specs)
    specs[("cpu", "satisfies")] = specs.pop(("core", "satisfies"))
    with pytest.raises(reg.RegistryError, match="disagrees with its own discriminator literal"):
        reg.build_registry(kind_names=names, collection_specs=specs)


# The six invariants, over every element kind and every layer, so a kind added to
# `schema.py` joins each without a sample to extend

#: Every element kind the registry knows, member-only kinds included.
ALL_KINDS = tuple(reg.REGISTRY.kinds)

#: Values that are not a single path segment, for both halves of invariant 3.
NOT_A_SEGMENT = (
    "",
    "   ",
    ".",
    "..",
    "../etc",
    "a/b",
    "a\\b",
    "a..b",
    "a\x00b",
    "swc/example/Foo.yaml",
    "a\nb",
    "a\x1b[2Jb",
    "R" * 300,
)


def test_every_kind_is_reachable_and_the_inventory_is_not_trivially_small() -> None:
    # Keeps the quantified tests below from holding vacuously.
    assert len(ALL_KINDS) >= 20
    assert {kind.kind for layer in reg.REGISTRY.layers for kind in layer.element_kinds} == set(ALL_KINDS)


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_invariant_1_every_annotation_only_annotates_an_existing_field(kind: str) -> None:
    """No annotation of any kind adds, hides or renames a field."""
    descriptor = reg.REGISTRY.element_kind(kind)
    published = set(descriptor.field_names)
    annotated = {name for (owner, name) in _FIELD_ANNOTATIONS if owner == kind}
    collections = {name for (owner, name) in _TABLES.collection_specs if owner == kind}
    assert annotated <= published, f"{kind} annotates fields it does not publish: {sorted(annotated - published)}"
    assert collections <= published, f"{kind} declares collections it does not publish"
    # An unannotated field is published on its derived facts, not dropped.
    derived = {info.alias or attribute for attribute, info in descriptor.model.model_fields.items()}
    assert derived <= published


@pytest.mark.parametrize("layer_name", EXPECTED_LAYERS)
def test_invariant_2_storage_is_one_way_for_every_layer(layer_name: str) -> None:
    """``(layer, identity, placement)`` goes in; nothing comes back out."""
    storage = reg.REGISTRY.storage(layer_name)
    placement = {"group": "sensors"} if storage.is_recursive else None
    composed = reg.REGISTRY.compose_path(layer_name, "Probe", placement)

    assert composed.startswith(f"{storage.directory}/")
    assert composed.endswith(storage.file_suffix)
    # No published descriptor, kind or field carries the record back out.
    descriptor = reg.REGISTRY.layer(layer_name)
    carriers = [descriptor, *descriptor.element_kinds, *(f for k in descriptor.element_kinds for f in k.fields)]
    for carrier in carriers:
        leaks = [name for name in vars(carrier) if "storage" in name or "director" in name]
        assert not leaks, f"{carrier} publishes {leaks}"


@pytest.mark.parametrize("bad", NOT_A_SEGMENT)
@pytest.mark.parametrize("layer_name", EXPECTED_LAYERS)
def test_invariant_3_no_layer_composes_a_path_from_a_value_that_is_not_a_segment(layer_name: str, bad: str) -> None:
    """Both client values, at every layer — the identity and the placement field."""
    placement = {"group": "sensors"} if reg.REGISTRY.storage(layer_name).is_recursive else None
    with pytest.raises(reg.PathSegmentError):
        reg.REGISTRY.compose_path(layer_name, bad, placement)
    if placement is not None:
        with pytest.raises(reg.PathSegmentError):
            reg.REGISTRY.compose_path(layer_name, "Probe", {"group": bad})


#: Identities the two composers have to agree about, legal and not; the property
#: never says which is which. `A.` is a legal identity, and `A.` + `.yaml` is `A..yaml`.
COMPOSER_CANDIDATES = [
    "Probe",
    "REQ-SPEED-001",
    "a.b",
    "A.",
    "A..",
    ".A.",
    "A.yaml.",
    ".",
    "..",
    "a..b",
    "a/b",
    "a\\b",
    "",
    "   ",
    " Probe",
    "Probe ",
    "Probe.yaml",
    "x" * kernel._MAX_SEGMENT_BYTES,
]


@pytest.mark.parametrize("identity", COMPOSER_CANDIDATES)
@pytest.mark.parametrize("layer_name", EXPECTED_LAYERS)
def test_the_two_composers_cannot_disagree_about_a_legal_target(layer_name: str, identity: str) -> None:
    """Whatever ``compose_path`` composes, ``writer.elements._resolve_target`` writes.

    ``editing.audit.element_address_of_target`` classifies by composing, so a
    disagreement would let ``block_is_complete`` accept an entry no writer could log,
    such as ``tasks/A..yaml`` for ``A.``, which ``_resolve_target`` refuses.
    """
    placement = {"group": "sensors"} if reg.REGISTRY.storage(layer_name).is_recursive else None
    refusal = reg.REGISTRY.identity_refusal(layer_name, identity)
    try:
        target = reg.REGISTRY.compose_path(layer_name, identity, placement)
    except reg.PathSegmentError:
        # `identity_refusal` is the question form of the same guard.
        assert refusal is not None, identity
        return
    assert refusal is None, identity
    # No `pytest.raises`: the claim is that this cannot raise at all.
    _resolve_target("/model", target)


def test_an_identity_whose_suffix_composes_an_illegal_segment_is_refused() -> None:
    """The one input, named, so a reader of the property above sees what it caught."""
    with pytest.raises(reg.PathSegmentError, match=r"A\.\.yaml"):
        reg.REGISTRY.compose_path("tasks", "A.")
    refusal = reg.REGISTRY.identity_refusal("tasks", "A.")
    assert refusal is not None
    # It names both values and why the identity alone looked fine.
    assert "A." in refusal and "A..yaml" in refusal and ".yaml" in refusal
    # The suffix is reserved in `_MAX_SEGMENT_BYTES` already, so re-checking the
    # composed name must not cost the last bytes of a legal range.
    assert reg.REGISTRY.identity_refusal("tasks", "x" * kernel._MAX_SEGMENT_BYTES) is None


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_invariant_4_every_placement_field_of_every_kind_is_immutable(kind: str) -> None:
    """Only a ``create`` fixes one, so it is immutable wherever it is declared."""
    for field in reg.REGISTRY.element_kind(kind).fields:
        if field.placement:
            assert field.immutable and field.required, f"{kind}.{field.name}"


def test_invariant_5_no_registry_accessor_touches_the_filesystem(tmp_path: Path) -> None:
    """Every registry accessor, over every layer and kind, leaves an empty working directory empty."""
    empty = tmp_path / "empty"
    empty.mkdir()
    original = os.getcwd()
    os.chdir(empty)
    try:
        for layer_name in reg.REGISTRY.layer_names:
            descriptor = reg.REGISTRY.layer(layer_name)
            storage = reg.REGISTRY.storage(layer_name)
            reg.REGISTRY.compose_path(layer_name, "Probe", {"group": "sensors"} if storage.is_recursive else None)
            for kind in descriptor.element_kinds:
                reg.REGISTRY.element_kind(kind.kind)
                reg.REGISTRY.all_fields(kind.kind)
                reg.REGISTRY.schema_fields(kind.kind)
    finally:
        os.chdir(original)
    assert list(empty.rglob("*")) == []


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_invariant_6_every_published_name_maps_back_onto_its_own_model_attribute(kind: str) -> None:
    """One vocabulary end to end: ``attribute_for`` maps every published name back."""
    descriptor = reg.REGISTRY.element_kind(kind)
    for name in reg.REGISTRY.schema_fields(kind):
        attribute = attribute_for(descriptor.model, name)
        assert attribute is not None, f"{kind}.{name} is published but does not resolve"
        info = descriptor.model.model_fields[attribute]
        assert (info.alias or attribute) == name
    # A placement field has no model attribute by construction.
    for field in descriptor.fields:
        if field.placement:
            assert attribute_for(descriptor.model, field.name) is None


# The maximal address chains behind `max_address_depth`; the depth itself is
# pinned by `test_whole_value_identity.py`


def _maximal_chains() -> dict[str, tuple[str, ...]]:
    """Every deepest address chain of the production registry.

    Returns ``{"<layer>/X.<collection descent>.<field>": (leaf field, ...)}``.
    Mirrors ``max_address_depth``'s recursion rather than importing it.
    """
    kinds = {kind.kind: kind for layer in reg.REGISTRY.layers for kind in layer.element_kinds}

    def walk(kind: reg.ElementKindDescriptor, seen: frozenset[str]) -> list[tuple[int, str, str]]:
        """``(depth, rendered descent, leaf field)`` for every path below *kind*."""
        if kind.kind in seen:  # pragma: no cover - the field graph is acyclic today
            return []
        below = seen | {kind.kind}
        paths: list[tuple[int, str, str]] = []
        for descriptor in kind.fields:
            if descriptor.collection is None:
                paths.append((1, "", descriptor.name))
                continue
            members = [name for name in descriptor.collection.member_kinds if name in kinds]
            if not members:
                paths.append((2, f"{descriptor.name}[member ...]", ""))
                continue
            if descriptor.collection.member_identity == "whole_value":
                # A whole-value member is a leaf: each of its fields one step,
                # and no descent into a collection of it.
                step = f"{descriptor.name}[member ...]"
                paths.extend((3, step, field.name) for name in members for field in kinds[name].fields)
                continue
            for name in members:
                for depth, descent, leaf in walk(kinds[name], below):
                    step = f"{descriptor.name}[member ...]"
                    paths.append((2 + depth, f"{step}.{descent}" if descent else step, leaf))
        return paths

    deepest = reg.REGISTRY.max_address_depth
    chains: dict[str, list[str]] = {}
    for layer in reg.REGISTRY.layers:
        for kind in layer.top_level_kinds:
            for depth, descent, leaf in walk(kind, frozenset()):
                if depth != deepest:
                    continue
                chains.setdefault(f"{layer.name}/X.{descent}.<field>", []).append(leaf)
    return {chain: tuple(sorted(set(leaves))) for chain, leaves in sorted(chains.items())}


def test_the_maximal_address_chains_are_the_ten_the_docstring_names() -> None:
    """The maximal chains, named, so a schema change that moves one fails naming it."""
    assert _maximal_chains() == {
        "interfaces/X.operations[member ...].arguments[member ...].<field>": (
            "application_type",
            "description",
            "direction",
            "implementation_type",
            "name",
        ),
        "swc/X.ports[member ...].com_specs[member ...].<field>": (
            "alive_timeout",
            "data_element",
            "enable_update",
            "handle_never_received",
            "handle_out_of_range",
            "handle_timeout_type",
            "init",
            "queue_length",
            "uses_end_to_end_protection",
        ),
        "swc/X.runnables[member ...].calls[member ...].<field>": (
            "asynchronous",
            "name",
            "operation",
            "poll",
            "port",
            "result",
            "timeout",
        ),
        "swc/X.runnables[member ...].events[member ...].<field>": (
            "disabled_modes",
            "element",
            "name",
            "operation",
            "period",
            "port",
            "result",
            "type",
        ),
        "swc/X.runnables[member ...].reads[member ...].<field>": ("by", "element", "name", "port"),
        "swc/X.runnables[member ...].writes[member ...].<field>": ("by", "element", "name", "port"),
        "swc/X.runnables[member ...].reads_variables[member ...].<field>": ("name", "variable"),
        "swc/X.runnables[member ...].writes_variables[member ...].<field>": ("name", "variable"),
        "swc/X.runnables[member ...].can_enter[member ...].<field>": ("area",),
        "swc/X.runnables[member ...].runs_inside[member ...].<field>": ("area",),
    }


def test_no_maximal_chain_depends_on_a_single_leaf_field() -> None:
    """No single leaf field lowers the maximum depth.

    Only the two exclusive area chains hold through one field (``area``); every
    other chain has several leaves, and several chains reach the maximum.
    """
    single_field = {
        "swc/X.runnables[member ...].can_enter[member ...].<field>",
        "swc/X.runnables[member ...].runs_inside[member ...].<field>",
    }
    chains = _maximal_chains()
    assert {chain for chain, leaves in chains.items() if len(leaves) == 1} == single_field
    for chain, leaves in chains.items():
        if chain not in single_field:
            assert len(leaves) > 1, f"{chain} reaches the maximum through one field only; the docstring says otherwise"
    assert len(set(chains) - single_field) > 1


# The kernel binding a threaded projection carries


def test_a_projection_names_the_kernel_registry_it_was_built_from() -> None:
    """`Registry.kernel_model` is the registry `current()` projected, by identity.

    Consumers that take no registry resolve `current_registry()` themselves, so
    `kernel_context` binds this inverse of `_PROJECTIONS`.
    """
    assert reg.REGISTRY.kernel_model is kernel.production_registry()
    assert reg.current(kernel.production_registry()).kernel_model is kernel.production_registry()


def test_a_projection_built_from_perturbed_tables_claims_no_kernel_registry() -> None:
    """A projection from perturbed tables has `kernel_model` `None` and `_model` `None`.

    `_model`, not `kernel_model`, separates this state from a collected one,
    which `kernel_context` treats differently.
    """
    assert reg.build_registry().kernel_model is None
    assert reg.build_registry()._model is None
    assert reg.build_registry(layer_specs=reg._PRODUCTION_TABLES.layer_specs).kernel_model is None


def test_the_kernel_binding_is_entered_for_a_projection_that_has_one() -> None:
    """`kernel_context` binds the projection's own registry, not the one in scope, and restores it."""
    merged = kernel.build_model_registry(contract=merged_contract(), binding=merged_binding())
    projection = reg.current(merged)
    assert kernel.current_registry() is not merged

    before = kernel.current_registry()
    with reg.kernel_context(projection):
        assert kernel.current_registry() is merged
    assert kernel.current_registry() is before


def test_the_kernel_binding_leaves_the_context_alone_when_there_is_nothing_to_bind() -> None:
    """A projection that never had a kernel registry is a no-op, not a reset to production.

    Binding production would silently repair a perturbed projection.
    """
    perturbed = reg.build_registry()
    assert perturbed.kernel_model is None
    assert perturbed._model is None, "the no-op branch is keyed on this, not on kernel_model"

    merged = kernel.build_model_registry(contract=merged_contract(), binding=merged_binding())
    with kernel.use_registry(merged):
        with reg.kernel_context(perturbed):
            assert kernel.current_registry() is merged, "the surrounding block's registry, untouched"
        assert kernel.current_registry() is merged


def test_the_kernel_binding_refuses_a_projection_whose_registry_has_been_collected() -> None:
    """A projection whose kernel registry was collected is refused on entry, the context untouched.

    `reg.current(build_model_registry(...))` holds only the projection. A no-op
    would read the model under the ambient registry and resolve addresses under
    the projection — silently.
    """
    projection = reg.current(kernel.build_model_registry(contract=merged_contract(), binding=merged_binding()))
    gc.collect()
    assert projection.kernel_model is None, "the registry this projection names was collected"
    assert projection._model is not None, "and it is not the perturbed-tables state"

    before = kernel.current_registry()
    entered = False
    with pytest.raises(reg.RegistryError, match="kernel registry has been collected"):
        with reg.kernel_context(projection):
            entered = True
    assert not entered, "the refusal must happen on the way in, before anything reads the model"
    assert kernel.current_registry() is before


def test_the_weak_back_reference_does_not_outlive_the_registry_it_names() -> None:
    """`_PROJECTIONS` stays weak: a projection does not keep its own cache key alive."""
    before = len(reg._PROJECTIONS)
    merged = kernel.build_model_registry(contract=merged_contract(), binding=merged_binding())
    projection = reg.current(merged)
    assert projection.kernel_model is merged
    assert len(reg._PROJECTIONS) == before + 1

    del merged, projection
    gc.collect()
    assert len(reg._PROJECTIONS) == before, "the projection kept its own cache key alive"
