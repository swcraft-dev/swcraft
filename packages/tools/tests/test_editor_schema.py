"""Tests of the editor-schema generator.

They cover one layer rendered from the registry, the union layers, the whole-contract sweep, the reference table and
the manifest schema.

The independent oracle is ``examples/basic/model/``: every ``*.yaml`` under it validates
clean (``jsonschema``'s ``Draft7Validator``, a test-only import) against the schema its
layer directory selects. The kinds that corpus does not reach have hand-built documents
under ``packages/tools/tests/fixtures/editor_schema/valid/<layer dir>/``, each also loaded
through ``model_state.load_layer``; ``test_the_fixtures_are_exactly_the_kinds_the_corpus_misses``
re-derives that split in both directions.

What this module does not reach: it never runs the language server, so it says nothing
about what VS Code renders; it builds no whole synthetic contract (the total-consumer
proof is stage 11 of ``test_synthetic_flow_through.py``); and ``jsonschema``'s ``oneOf``
error text is not the language server's diagnostic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, layer_for_path
from jsonschema import Draft7Validator
from model_contract_fixtures import merged_binding, merged_contract
from swcraft_tools import __version__
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.editor_schema import (
    CHECK_MODEL_REQUIRED_FIELDS,
    DIAGRAM_PAGE_SCRIPT_ARTIFACT,
    MANIFEST_ARTIFACT_PATH,
    MANIFEST_FILE_MATCH,
    MAPPING_ARTIFACT_PATH,
    MAPPING_FILE_MATCH,
    REFERENCES_ARTIFACT,
    SCHEMA_DIALECT,
    SCHEMA_ID_PREFIX,
    UnreachableReference,
    UnsupportedFieldType,
    _key_paths,
    all_artifacts,
    artifact_path,
    diagram_page_script_artifact,
    editor_artifacts,
    field_schema,
    layer_schema,
    main,
    manifest_schema,
    mapping_schema,
    references_artifact,
    render,
    write_yaml_validation,
    yaml_validation_entries,
)
from swcraft_tools.guide_reference.model import kind_anchor, layer_section_page
from swcraft_tools.model_registry import (
    CollectionFacts,
    FieldFacts,
    KindFacts,
    LayerFacts,
    MemberReferenceEdge,
    MemberReferenceTarget,
    ModelRegistry,
    ReferenceEdge,
    StorageRecord,
    build_model_registry,
    current_registry,
    use_registry,
)
from swcraft_tools.model_state import load_layer
from swcraft_tools.project import MANIFEST_RUNTIME_FIELDS, OutputPaths, SwcraftProject, manifest_keys
from swcraft_tools.safety import ASIL_LEVELS, ASIL_VALUES
from swcraft_tools.schema import ModelElement
from swcraft_tools.writer.diagrams.page import PAGE_SCRIPT, page_script_sha256, render_view_page
from synthetic_support import SIBLING_NESTED_LAYERS, nested_registry
from windows_newline_support import assert_no_crlf, windows_newline_translation

_CORPUS_ROOT = REPO_ROOT / "examples" / "basic" / "model"
_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "editor_schema" / "valid"


@pytest.fixture(scope="module")
def registry() -> ModelRegistry:
    return current_registry()


def _field(name: str, type_name: str, **overrides: object) -> FieldFacts:
    base = FieldFacts(
        id=f"field:x.thing.{name}",
        layer="x",
        kind="thing",
        name=name,
        type=type_name,
        required=True,
        immutable=False,
        description=f"The {name}.",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# A single-kind layer, field by field


def test_tasks_renders_as_one_closed_object_in_contract_order(registry: ModelRegistry) -> None:
    layer = registry.layer("tasks")
    document = layer_schema(layer)

    assert list(document) == [
        "$schema",
        "$id",
        "title",
        "$comment",
        "type",
        "additionalProperties",
        "properties",
        "required",
        "definitions",
    ]
    assert document["$schema"] == SCHEMA_DIALECT
    assert document["$id"] == f"{SCHEMA_ID_PREFIX}tasks.schema.json"
    assert document["title"] == layer.display_label == "Tasks"
    assert document["$comment"] == layer.description
    assert document["additionalProperties"] is False
    assert "oneOf" not in document, "a single-kind layer renders no branch construct"

    (task,) = layer.top_level_kinds
    assert list(document["properties"]) == [field.name for field in task.fields]
    assert document["required"] == [field.name for field in task.fields if field.required]
    assert document["required"] == ["name", "period", "priority", "partition"]

    properties = document["properties"]
    assert properties["name"] == {"description": task.get_field("name").description, "type": "string"}
    assert properties["period"]["type"] == "number"
    assert properties["period"]["description"].endswith(" (unit: seconds)")
    assert properties["priority"] == {"description": task.get_field("priority").description, "type": "integer"}
    assert properties["partition"] == {"description": task.get_field("partition").description, "type": "string"}
    assert properties["description"]["default"] == ""
    assert properties["runnables"] == {
        "description": task.get_field("runnables").description,
        "type": "array",
        "items": {"$ref": "#/definitions/task_runnable_ref"},
        "default": [],
    }
    assert list(document["definitions"]) == ["task_runnable_ref"]
    member = document["definitions"]["task_runnable_ref"]
    assert member["type"] == "object" and member["additionalProperties"] is False
    # Exactly one of `swc` and `instance` names the component -- a
    # `@model_validator` the editor schemas do not render -- so only `runnable`
    # is required.
    assert list(member["properties"]) == ["swc", "instance", "runnable"]
    assert member["required"] == ["runnable"]


def test_field_order_is_the_registrys_and_not_the_pydantic_classs(registry: ModelRegistry) -> None:
    """`KindFacts.fields` is already contract order under the serialization alias.

    Asserted by comparing against the registry and NOT against `model_fields`:
    the one field whose alias differs from its attribute (`connector.from`) is
    the case that shows the order source is the registry and the name source is
    `field.name`.
    """
    (connector,) = registry.layer("connectors").top_level_kinds
    document = layer_schema(registry.layer("connectors"))
    assert list(document["properties"]) == [field.name for field in connector.fields]
    assert "from" in document["properties"], "the alias, not the Python attribute"
    assert document["properties"]["from"] == {
        "description": connector.get_field("from").description,
        "$ref": "#/definitions/connector_endpoint",
    }
    assert list(document["definitions"]) == ["connector_endpoint"]


def test_swc_renders_its_two_list_collections_and_its_map(registry: ModelRegistry) -> None:
    layer = registry.layer("swc")
    document = layer_schema(layer)
    properties = document["properties"]

    assert properties["ports"]["type"] == "array"
    assert properties["ports"]["items"] == {"$ref": "#/definitions/port"}
    assert properties["runnables"]["items"] == {"$ref": "#/definitions/runnable"}
    assert properties["memory"] == {
        "description": f"{layer.top_level_kinds[0].get_field('memory').description} (unit: bytes)",
        "type": "object",
        "additionalProperties": {"type": "integer"},
        "default": {},
    }
    assert properties["type"]["enum"] == ["application", "service", "ecu_abstraction"]
    assert properties["type"]["default"] == "application"
    # The member-only kinds reachable from `swc`, transitively and in contract order.
    assert list(document["definitions"]) == [
        "port",
        "port_com_spec",
        "runnable",
        "timing_event",
        "disabled_mode",
        "init_event",
        "operation_event",
        "data_event",
        "call_returned_event",
        "data_read",
        "data_write",
        "operation_call",
        "variable",
        "variable_read",
        "variable_write",
        "exclusive_area",
        "can_enter_area",
        "runs_inside_area",
    ]
    assert document["definitions"]["runnable"]["properties"]["events"]["items"] == {
        "oneOf": [
            {"$ref": "#/definitions/timing_event"},
            {"$ref": "#/definitions/init_event"},
            {"$ref": "#/definitions/operation_event"},
            {"$ref": "#/definitions/data_event"},
            {"$ref": "#/definitions/call_returned_event"},
        ]
    }
    assert document["definitions"]["port"]["properties"]["direction"]["enum"] == ["provide", "require"]


def test_the_runnable_renders_its_three_composite_access_lists(registry: ModelRegistry) -> None:
    """The three access lists render closed members with a required pair, each list defaulting to `[]`.

    `by` is nullable with the null inside its `enum` and no `default`. Not expressed by the
    schema: a `(port, element)` pair appearing at most once (`uniqueItems` compares whole
    items) and the port or element existing; both are `check-model`'s, with a fixture each
    in `test_editor_schema_limits.py`.
    """
    document = layer_schema(registry.layer("swc"))
    runnable = registry.kind("runnable")
    properties = document["definitions"]["runnable"]["properties"]
    for list_name, member in (("reads", "data_read"), ("writes", "data_write"), ("calls", "operation_call")):
        assert properties[list_name] == {
            "description": runnable.get_field(list_name).description,
            "type": "array",
            "items": {"$ref": f"#/definitions/{member}"},
            "default": [],
        }
        assert "uniqueItems" not in properties[list_name]

    read = document["definitions"]["data_read"]
    assert read["type"] == "object" and read["additionalProperties"] is False
    assert list(read["properties"]) == ["port", "element", "by", "name"]
    assert read["required"] == ["port", "element"]
    assert read["properties"]["by"]["type"] == ["string", "null"]
    assert read["properties"]["by"]["enum"] == ["value", "implicit", None], (
        "the enum is widened with the type (round 4)"
    )
    assert "default" not in read["properties"]["by"]
    assert "pattern" not in read["properties"]["port"]

    write = document["definitions"]["data_write"]
    assert list(write["properties"]) == ["port", "element", "by", "name"] and write["required"] == ["port", "element"]
    assert write["properties"]["by"]["enum"] == ["implicit", None]
    call = document["definitions"]["operation_call"]
    assert list(call["properties"]) == ["port", "operation", "asynchronous", "poll", "result", "timeout", "name"]
    assert call["required"] == ["port", "operation"]
    for member in (write, call):
        assert member["additionalProperties"] is False
    assert "by" not in call["properties"]


def test_the_placement_field_is_absent_from_properties_and_required(registry: ModelRegistry) -> None:
    """`field:swc.swc.group` has no key in the file: emitting it would flag every SWC."""
    layer = registry.layer("swc")
    assert layer.placement is not None and layer.placement.field == "group"
    (swc,) = layer.top_level_kinds
    assert swc.get_field("group").placement is True, "the registry calls it a placement field"

    document = layer_schema(layer)
    assert "group" not in document["properties"]
    assert "group" not in document["required"]
    assert set(document["properties"]) == {field.name for field in swc.fields if not field.placement}


def test_a_layer_reaching_no_member_kind_carries_no_definitions_key(registry: ModelRegistry) -> None:
    for name in ("requirements",):
        document = layer_schema(registry.layer(name))
        assert "definitions" not in document, name
        assert "$ref" not in render(document), name


# Prose, nullability, the discriminator


def _every_property(document: dict) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    objects = [document, *document.get("oneOf", []), *document.get("definitions", {}).values()]
    for obj in objects:
        for name, prop in obj.get("properties", {}).items():
            found.append((name, prop))
    return found


@pytest.mark.parametrize("layer_name", [layer.name for layer in current_registry().layers])
def test_every_emitted_property_carries_a_non_empty_description(layer_name: str, registry: ModelRegistry) -> None:
    """The contract's description gate, asserted again on the output so the two cannot drift apart."""
    document = layer_schema(registry.layer(layer_name))
    properties = _every_property(document)
    assert properties, layer_name
    bare = [name for name, prop in properties if not prop.get("description")]
    assert bare == [], f"{layer_name}: properties without a description: {bare}"


def _red_hat_schema_lens_title(document: dict, file_name: str) -> str:
    """Red Hat YAML's schema CodeLens title, transcribed from yaml-language-server's ``getSchemaTitle``.

    Read from ``redhat.vscode-yaml``'s ``dist/languageserver.js``: a root ``name`` wins, else
    ``title - description (file)``, else ``title (file)``, else the file name. The Kubernetes
    branch is not transcribed.
    """
    if "name" in document:
        return f"{document['name']} ({file_name})"
    if document.get("title"):
        if document.get("description"):
            return f"{document['title']} - {document['description']} ({file_name})"
        return f"{document['title']} ({file_name})"
    return file_name


#: The longest schema-lens title a layer file may carry. Red Hat YAML draws its lens on
#: the root line ahead of SWCraft's CodeLens, and VS Code clips the row at the editor's
#: width, so a long title pushes every SWCraft lens out of sight.
_SCHEMA_LENS_TITLE_LIMIT = 64


@pytest.mark.parametrize("layer_name", [layer.name for layer in current_registry().layers])
def test_red_hat_yamls_schema_lens_leaves_room_for_the_swcraft_lenses(layer_name: str, registry: ModelRegistry) -> None:
    """The root carries no ``description`` (the prose is kept as ``$comment``).

    So the schema lens reads ``<title> (<file>)`` and SWCraft's lenses stay on screen after it.
    """
    layer = registry.layer(layer_name)
    document = layer_schema(layer)
    assert "description" not in document, f"{layer_name}: a root description lengthens the schema lens"
    if layer.description:
        assert document["$comment"] == layer.description
    title = _red_hat_schema_lens_title(document, artifact_path(layer))
    assert title == f"{layer.display_label} ({artifact_path(layer)})"
    assert len(title) <= _SCHEMA_LENS_TITLE_LIMIT, title


#: What each nullable field's `type` widens to, keyed by the contract spelling.
#: Five of the six widen a single scalar; `value` is the one that widens a
#: whole list, which is why the mapping is by type and not by field.
_WIDENED_NULLABLE_TYPES: dict[str, list[str]] = {
    "boolean": ["boolean", "null"],
    "number": ["number", "null"],
    "integer": ["integer", "null"],
    "string": ["string", "null"],
    "value": ["boolean", "integer", "number", "string", "array", "object", "null"],
    "list[enum_literal]": ["array", "null"],
}


def test_the_none_default_fields_render_nullable_with_no_default_key(registry: ModelRegistry) -> None:
    """Every field with a `None` default renders nullable with no `default` key, in contract order.

    A field null because the OTHER of its pair is set renders merely nullable, deliberately
    weaker than the model ("exactly one of the two" is a `@model_validator`); an
    object-shaped field has no `type` to widen and renders as `anyOf` with `null`.
    """
    nullable = [
        field
        for layer in registry.layers
        for kind in layer.kinds
        for field in kind.fields
        if field.has_default and field.default is None
    ]
    assert [field.id for field in nullable] == [
        "field:swc.swc.asil",
        "field:swc.swc.handle_termination_and_restart",
        "field:swc.swc.behavior_name",
        "field:swc.swc.package",
        "field:swc.port.interface",
        "field:swc.port.observed_interface",
        "field:swc.port_com_spec.enable_update",
        "field:swc.port_com_spec.alive_timeout",
        "field:swc.port_com_spec.handle_never_received",
        "field:swc.port_com_spec.handle_timeout_type",
        "field:swc.port_com_spec.handle_out_of_range",
        "field:swc.port_com_spec.uses_end_to_end_protection",
        "field:swc.port_com_spec.queue_length",
        "field:swc.port_com_spec.init",
        "field:swc.runnable.symbol",
        "field:swc.runnable.wcet",
        "field:swc.runnable.wcet_type",
        "field:swc.timing_event.name",
        "field:swc.init_event.name",
        "field:swc.operation_event.name",
        "field:swc.data_event.name",
        "field:swc.call_returned_event.result",
        "field:swc.call_returned_event.name",
        "field:swc.data_read.by",
        "field:swc.data_read.name",
        "field:swc.data_write.by",
        "field:swc.data_write.name",
        "field:swc.operation_call.result",
        "field:swc.operation_call.timeout",
        "field:swc.operation_call.name",
        "field:swc.variable.application_type",
        "field:swc.variable.implementation_type",
        "field:swc.variable.init",
        "field:swc.variable.sw_calibration_access",
        "field:swc.variable_read.name",
        "field:swc.variable_write.name",
        "field:interfaces.sender_receiver.package",
        "field:interfaces.client_server.package",
        # The side of a prototype's type: exactly one of the pair set.
        "field:interfaces.data_element.application_type",
        "field:interfaces.data_element.implementation_type",
        "field:interfaces.data_element.init",
        "field:interfaces.data_element.asil",
        "field:interfaces.data_element.sw_impl_policy",
        "field:interfaces.data_element.sw_calibration_access",
        "field:interfaces.argument.application_type",
        "field:interfaces.argument.implementation_type",
        "field:connectors.connector.composition",
        "field:connectors.connector_endpoint.swc",
        "field:connectors.connector_endpoint.composition",
        "field:application_types.enum.literals",
        "field:application_types.enum.compu_method_ref",
        "field:application_types.enum.package",
        "field:application_types.record.package",
        "field:application_types.array.package",
        "field:application_types.application_type.conversion",
        "field:application_types.application_type.range",
        # Each reference null where the inline form is used or the property is not stated.
        "field:application_types.application_type.unit_ref",
        "field:application_types.application_type.compu_method_ref",
        "field:application_types.application_type.data_constr_ref",
        "field:application_types.application_type.package",
        "field:application_types.conversion.limits",
        "field:application_types.conversion_limits.lower",
        "field:application_types.conversion_limits.upper",
        "field:application_types.range.min",
        "field:application_types.range.max",
        "field:implementation_types.value_type.value_min",
        "field:implementation_types.value_type.value_max",
        "field:implementation_types.value_type.compu_method_ref",
        "field:implementation_types.value_type.package",
        "field:implementation_types.reference_type.compu_method_ref",
        "field:implementation_types.reference_type.package",
        "field:implementation_types.implementation_record.package",
        "field:implementation_types.implementation_array.package",
        "field:implementation_types.pointer.package",
        "field:base_types.base_type.package",
        "field:requirements.requirement.asil",
        # The requirement's provenance, null while unknown.
        "field:requirements.requirement.document",
        "field:requirements.requirement.baseline",
        "field:requirements.requirement.revision",
        "field:compositions.composition.package",
        "field:compositions.composition_member.type",
        "field:compositions.composition_member.composition",
        "field:tasks.task.wcet",
        # Instance identity: exactly one of each pair set.
        "field:tasks.task_runnable_ref.swc",
        "field:tasks.task_runnable_ref.instance",
        "field:sequences.sequence_step.swc",
        "field:sequences.sequence_step.instance",
        "field:hardware.core.asil_capability",
        "field:hardware.core.hw_id",
        "field:hardware.memory.asil_capability",
        "field:hardware.partition.asil",
        "field:mapping_sets.mapping_set.package",
        "field:units.unit.package",
        "field:compu_methods.texttable.package",
        "field:compu_methods.linear.limits",
        "field:compu_methods.linear.package",
        "field:compu_methods.linear_limits.lower",
        "field:compu_methods.linear_limits.upper",
        "field:data_constraints.data_constr.min",
        "field:data_constraints.data_constr.max",
        "field:data_constraints.data_constr.level",
        "field:data_constraints.data_constr.package",
    ]
    for field in nullable:
        rendered = field_schema(field)
        if field.element_kind is not None:
            assert rendered["anyOf"] == [
                {"$ref": f"#/definitions/{field.element_kind}"},
                {"type": "null"},
            ], field.id
            assert "type" not in rendered, field.id
        else:
            assert rendered["type"] == _WIDENED_NULLABLE_TYPES[field.type], field.id
        assert "default" not in rendered, field.id
    by = field_schema(registry.field_facts("data_read", "by"))
    assert by["enum"] == ["value", "implicit", None], (
        "the receive form keeps its literals, and the enum admits the null"
    )

    # And in the document itself: `wcet` sits on a single-kind layer.
    assert layer_schema(registry.layer("tasks"))["properties"]["wcet"]["type"] == ["string", "null"]
    assert "pattern" not in layer_schema(registry.layer("tasks"))["properties"]["wcet"]


def test_a_default_is_emitted_only_where_the_contract_declares_one() -> None:
    assert "default" not in field_schema(_field("name", "string"))
    assert field_schema(_field("n", "integer", has_default=True, default=3))["default"] == 3
    assert field_schema(_field("tags", "list[string]", has_default=True, default=[]))["default"] == []


def test_every_field_the_editor_requires_beyond_the_contract_is_one_check_model_requires(
    registry: ModelRegistry,
) -> None:
    """Each ``CHECK_MODEL_REQUIRED_FIELDS`` entry is a declared OPTIONAL field a cross-element rule names.

    Otherwise the editor would be stricter than the product with nothing behind
    it. The contract keeps such a field ``required: false`` on purpose: the
    loader accepts a file without it and ``check-model`` reports it.
    """
    from swcraft_tools.contract import load_contract

    declared = {field.id: field for layer in registry.layers for kind in layer.kinds for field in kind.fields}
    cross_element = [rule for rule in load_contract()["rules"] if rule["family"] == "cross-element"]
    assert CHECK_MODEL_REQUIRED_FIELDS, "the set is empty, so this gate guards nothing"
    for field_id in CHECK_MODEL_REQUIRED_FIELDS:
        assert field_id in declared, field_id
        assert declared[field_id].required is False, field_id
        assert any(field_id in rule["appliesTo"] for rule in cross_element), field_id


def test_a_base_type_without_a_native_declaration_is_refused_by_the_editor_and_loaded_by_the_product(
    registry: ModelRegistry,
) -> None:
    """The one entry, in both directions: the editor lists the field as required; the loader does not."""

    # The kind is the one kind of the `base_types` layer, so the layer's document is the
    # kind's object schema itself, with no branch to select.
    document = layer_schema(registry.layer("base_types"))
    assert "oneOf" not in document
    assert "native_declaration" in document["required"]

    without = {"name": "raw_base", "kind": "base_type", "size": 8, "encoding": "NONE"}
    assert not Draft7Validator(document).is_valid(without)
    assert Draft7Validator(document).is_valid({**without, "native_declaration": "uint8_t"})
    from swcraft_tools.schema import BaseDataType

    assert BaseDataType.model_validate(without).native_declaration == ""


def test_a_nullable_enum_admits_the_null_in_the_enum_too() -> None:
    """A nullable enum carries the null in its `enum` too; a non-nullable one keeps its members alone.

    `enum` is a closed list: `["string", "null"]` beside `enum: ["value"]` would still
    reject a spelled-out `null`.
    """
    nullable = field_schema(_field("by", "string", has_default=True, default=None, enum=("value",)))
    assert nullable["type"] == ["string", "null"] and nullable["enum"] == ["value", None]
    assert "default" not in nullable
    assert Draft7Validator(nullable).is_valid(None) and Draft7Validator(nullable).is_valid("value")
    assert not Draft7Validator(nullable).is_valid("argument")
    plain = field_schema(_field("direction", "string", enum=("provide", "require")))
    assert plain["type"] == "string" and plain["enum"] == ["provide", "require"]


def test_a_nullable_collection_widens_its_own_type_and_a_nullable_ref_becomes_a_choice() -> None:
    """`["<type>", "null"]` needs a `type` to widen: a list or map has one, a bare `$ref` does not.

    The `$ref` is widened as `anyOf`, which keeps its own definition the one statement of
    the object's shape.
    """
    nullable_list = field_schema(_field("tags", "list[string]", has_default=True, default=None))
    assert nullable_list == {"description": "The tags.", "type": ["array", "null"], "items": {"type": "string"}}
    nullable_map = field_schema(_field("memory", "map[string, integer]", has_default=True, default=None))
    assert nullable_map["type"] == ["object", "null"] and "default" not in nullable_map
    nullable_ref = field_schema(
        _field("f", "connector_endpoint", has_default=True, default=None, element_kind="connector_endpoint")
    )
    assert nullable_ref == {
        "description": "The f.",
        "anyOf": [{"$ref": "#/definitions/connector_endpoint"}, {"type": "null"}],
    }
    assert "default" not in nullable_ref


def test_a_unit_is_appended_to_the_description_and_nowhere_else() -> None:
    rendered = field_schema(_field("size", "integer", unit="bytes"))
    assert rendered == {"description": "The size. (unit: bytes)", "type": "integer"}
    assert "unit" not in rendered


def _union_kind(enum: tuple[str, ...]) -> KindFacts:
    kind_field = _field("kind", "string", enum=enum, immutable=True)
    name_field = _field("name", "string", immutable=True)
    return KindFacts(
        id="kind:x.thing",
        kind="thing",
        layer="x",
        model=ModelElement,
        fields=(name_field, kind_field),
        discriminator_field="kind",
        description="A kind.",
    )


def _union_layer(kinds: tuple[KindFacts, ...]) -> LayerFacts:
    return LayerFacts(
        id="layer:x",
        name="x",
        optional=False,
        identity_field="name",
        storage=StorageRecord(directory="x", nesting="flat", file_suffix=".yaml", filename_from="name"),
        kinds=kinds,
        element_annotation=None,
        token="x",
        description="A layer.",
    )


def test_a_union_branch_pins_the_discriminator_from_its_own_enum(registry: ModelRegistry) -> None:
    """The `const` is read from the declared enum, never invented from `KindFacts.kind`."""
    document = layer_schema(registry.layer("hardware"))
    branches = document["oneOf"]
    assert [branch["properties"]["kind"]["const"] for branch in branches] == [
        "processor",
        "core",
        "memory",
        "partition",
    ]
    for branch in branches:
        assert "enum" not in branch["properties"]["kind"]
        assert "kind" in branch["required"]
        assert branch["additionalProperties"] is False
    assert "properties" not in document, "a union document has branches, not properties of its own"


@pytest.mark.parametrize("enum", [(), ("a", "b")], ids=["empty", "multi-member"])
def test_a_discriminator_with_an_empty_or_multi_member_enum_is_refused(enum: tuple[str, ...]) -> None:
    kinds = (_union_kind(enum), replace(_union_kind(("other",)), id="kind:x.other", kind="other"))
    with pytest.raises(UnsupportedFieldType) as info:
        layer_schema(_union_layer(kinds))
    assert "field:x.thing.kind" in str(info.value)
    assert "exactly one member" in str(info.value)


def test_a_discriminator_naming_an_undeclared_field_is_refused() -> None:
    """`discriminator_field` points at a field the kind does not declare: refused, naming the kind."""
    undeclared = replace(_union_kind(("thing",)), discriminator_field="sort")
    kinds = (undeclared, replace(_union_kind(("other",)), id="kind:x.other", kind="other"))
    with pytest.raises(UnsupportedFieldType) as info:
        layer_schema(_union_layer(kinds))
    assert info.value.field_id == "kind:x.thing"
    assert "discriminator field 'sort' is not declared" in str(info.value)


def test_a_member_kind_the_layer_does_not_declare_is_refused() -> None:
    """A collection naming a ghost member kind: refused naming the field and the layer, never a dangling `$ref`."""
    collection = CollectionFacts(
        field_id="field:x.thing.parts",
        collection_id="collection:x.thing.parts",
        kind="thing",
        layer="x",
        field="parts",
        collection_kind="list",
        member_identity="named",
        member_kinds=("ghost",),
    )
    parts = _field("parts", "list[ghost]", collection=collection)
    kind = replace(_union_kind(("thing",)), fields=(_field("name", "string"), parts), discriminator_field=None)
    with pytest.raises(UnsupportedFieldType) as info:
        layer_schema(_union_layer((kind,)))
    assert info.value.field_id == "field:x.thing.parts"
    assert "member kind 'ghost' is not declared on layer 'x'" in str(info.value)


# The type mapping and its refusal


def test_an_unmapped_type_is_refused_naming_the_field_and_the_type() -> None:
    field = _field("blob", "bytes")
    with pytest.raises(UnsupportedFieldType) as info:
        field_schema(field)
    assert info.value.field_id == "field:x.thing.blob"
    assert info.value.type_name == "bytes"
    assert "field:x.thing.blob" in str(info.value) and "'bytes'" in str(info.value)


@pytest.mark.parametrize(
    "type_name",
    ["list[thing]", "map[integer, string]", "map[string, thing]", "set[string]", "map[string,integer]"],
)
def test_a_list_or_map_of_an_unmapped_member_is_refused_rather_than_rendered_open(type_name: str) -> None:
    """`list[<kind>]` is only renderable through `member_kinds`; without them the type is refused.

    The last spelling is a structurally equivalent map without the grammar's `, ` separator:
    `contract.schema.json`'s `typeName` pattern is byte-exact, and so is the generator.
    """
    with pytest.raises(UnsupportedFieldType):
        field_schema(_field("f", type_name))


def test_every_contract_scalar_renders_including_the_boolean_no_shipped_field_declares_yet() -> None:
    """The four `typeName` scalars each map to the JSON Schema type of the same name, inside a list or map too."""
    for scalar in ("string", "integer", "number", "boolean"):
        assert field_schema(_field("f", scalar))["type"] == scalar
    assert field_schema(_field("flags", "list[boolean]"))["items"] == {"type": "boolean"}
    assert field_schema(_field("flags", "map[string, boolean]"))["additionalProperties"] == {"type": "boolean"}
    nullable = field_schema(_field("flag", "boolean", has_default=True, default=None))
    assert nullable["type"] == ["boolean", "null"] and "default" not in nullable


def test_the_value_type_renders_every_json_kind_it_may_take_rather_than_refusing() -> None:
    """`value` follows another declaration's type, so it renders every JSON kind it may take.

    The order is `schema.py`'s `InitValue` alias; conformance is `swcraft check-model`'s.
    """
    rendered = field_schema(_field("init", "value"))
    assert rendered == {
        "description": "The init.",
        "type": ["boolean", "integer", "number", "string", "array", "object"],
    }


def test_a_nullable_value_widens_the_whole_type_list_and_emits_no_default() -> None:
    """The widening branch gains the list case, and `null` goes on the end."""
    rendered = field_schema(_field("init", "value", has_default=True, default=None))
    assert rendered["type"] == ["boolean", "integer", "number", "string", "array", "object", "null"]
    assert "default" not in rendered


def test_the_shipped_init_field_carries_the_shape_rule_in_its_own_description(
    registry: ModelRegistry,
) -> None:
    """The permissive rendering is honest only if the field's own description, which hover shows, names the gate."""
    field = next(k for k in registry.layer("interfaces").kinds if k.kind == "data_element").get_field("init")
    assert field is not None
    rendered = field_schema(field)
    assert rendered["type"] == [
        "boolean",
        "integer",
        "number",
        "string",
        "array",
        "object",
        "null",
    ]
    assert rendered["description"] == field.description
    assert "declared type" in rendered["description"]
    assert "model error" in rendered["description"]


def test_the_permissive_rendering_is_the_value_case_and_not_a_new_fall_through() -> None:
    """A genuinely unknown spelling is still refused, so `{}` is still unreachable."""
    for unknown in ("duration", "values", "any", "InitValue"):
        with pytest.raises(UnsupportedFieldType) as info:
            field_schema(_field("f", unknown))
        assert info.value.type_name == unknown


def test_a_union_member_list_renders_as_one_of_two_refs() -> None:
    collection = CollectionFacts(
        field_id="field:x.thing.events",
        collection_id="collection:x.thing.events",
        kind="thing",
        layer="x",
        field="events",
        collection_kind="list",
        member_identity="discriminated",
        member_kinds=("timing_event", "init_event"),
        discriminator_field="type",
    )
    rendered = field_schema(_field("events", "list[timing_event | init_event]", collection=collection))
    assert rendered == {
        "description": "The events.",
        "type": "array",
        "items": {"oneOf": [{"$ref": "#/definitions/timing_event"}, {"$ref": "#/definitions/init_event"}]},
    }


def test_scalar_collections_resolve_through_the_declared_type_without_member_kinds(registry: ModelRegistry) -> None:
    """The `whole_value` collections name no member kind; the declared `type` is the source."""
    memory = registry.field_facts("swc", "memory")
    links = registry.field_facts("swc", "satisfies")
    assert memory.collection is not None and memory.collection.member_kinds == ()
    assert links.collection is not None and links.collection.member_kinds == ()
    assert {key: value for key, value in field_schema(memory).items() if key != "description"} == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
        "default": {},
    }
    assert {key: value for key, value in field_schema(links).items() if key != "description"} == {
        "type": "array",
        "items": {"type": "string"},
        "default": [],
    }


def test_the_thirty_one_type_spellings_of_the_shipped_contract_all_render(registry: ModelRegistry) -> None:
    """Every spelling the contract uses has a rendering; the count is measured, not remembered.

    The figure counts the distinct field `type` spellings of the shipped contract and moves
    when a contract adds or removes one. Pinning it makes a new spelling deliberate: the
    generator raises on a spelling it has no case for rather than rendering an empty schema.
    """
    spellings = {field.type for layer in registry.layers for kind in layer.kinds for field in kind.fields}
    assert len(spellings) == 42
    for layer in registry.layers:
        layer_schema(layer)


# Determinism


_RENDER_SNIPPET = (
    "from swcraft_tools.editor_schema import layer_schema, render\n"
    "from swcraft_tools.model_registry import current_registry\n"
    "import sys\n"
    "sys.stdout.write(render(layer_schema(current_registry().layer('swc'))))\n"
)


def test_rendering_is_byte_identical_in_process_and_across_processes(registry: ModelRegistry) -> None:
    first = render(layer_schema(registry.layer("swc")))
    second = render(layer_schema(registry.layer("swc")))
    assert first == second
    assert first.endswith("}\n") and not first.endswith("\n\n")
    assert "\\u" not in first, "`ensure_ascii=False`: the contract's prose is written verbatim"

    # `encoding="utf-8"` says how this process decodes the pipe, not how the child
    # encodes into it: a redirected stream falls back to the locale codec (cp1252 on
    # Windows), which cannot encode the contract's prose. `PYTHONIOENCODING` is set on
    # the child alone, never exported, because the gate run must not mask the
    # redirected-stream behaviour. Nothing here is a claim about `swcraft`'s own console output.
    fresh = subprocess.run(
        [sys.executable, "-c", _RENDER_SNIPPET],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert fresh.stdout == first


def test_no_document_embeds_the_product_version(registry: ModelRegistry) -> None:
    from swcraft_tools import __version__

    for layer in registry.layers:
        assert __version__ not in render(layer_schema(layer)), layer.name
    assert __version__ not in render(manifest_schema())


# Union layers


@pytest.mark.parametrize(
    ("layer_name", "branches"),
    [
        ("interfaces", ["sender_receiver", "client_server", "external_interface"]),
        (
            "implementation_types",
            [
                "value_type",
                "reference_type",
                "external_type",
                "implementation_record",
                "implementation_array",
                "pointer",
            ],
        ),
        ("hardware", ["processor", "core", "memory", "partition"]),
        ("application_types", ["enum", "record", "array", "application_type"]),
    ],
)
def test_a_union_layer_renders_one_branch_per_top_level_kind_dispatching_on_kind(
    layer_name: str, branches: list[str], registry: ModelRegistry
) -> None:
    layer = registry.layer(layer_name)
    document = layer_schema(layer)
    assert [kind.kind for kind in layer.top_level_kinds] == branches, "the branch list is the contract's"
    assert [branch["properties"]["kind"]["const"] for branch in document["oneOf"]] == branches
    for branch, kind in zip(document["oneOf"], layer.top_level_kinds, strict=True):
        assert branch["description"] == kind.description, "a branch carries its kind's prose"
        assert branch["type"] == "object" and branch["additionalProperties"] is False
        assert "kind" in branch["required"]
        assert list(branch["properties"]) == [field.name for field in kind.fields if not field.placement]
    for key in ("properties", "required", "type", "additionalProperties"):
        assert key not in document, f"{layer_name}: {key!r} belongs to the branches, not the document"
    # A `kind` no branch pins matches nothing; which branch a real document
    # selects is what the corpus and fixture tests below witness per kind.
    assert not Draft7Validator(document).is_valid({"name": "X", "kind": "no_such_kind"})


def _asil_enums(node: Any, found: dict[str, list[list[Any]]]) -> dict[str, list[list[Any]]]:
    """Every generated ``asil`` / ``asil_capability`` property's ``enum``, by name, anywhere in *node*."""
    if isinstance(node, dict):
        for name, value in node.get("properties", {}).items():
            if name in ("asil", "asil_capability") and isinstance(value, dict) and "enum" in value:
                found.setdefault(name, []).append(value["enum"])
        for value in node.values():
            _asil_enums(value, found)
    elif isinstance(node, list):
        for value in node:
            _asil_enums(value, found)
    return found


def test_every_rated_asil_completes_the_eighteen_and_every_capability_the_five(registry: ModelRegistry) -> None:
    """Every ``asil`` completes the eighteen of ``safety.ASIL_VALUES`` and ``null``; every capability the five levels.

    The four rated ``asil`` fields (component, data element, requirement, partition) and the two
    capabilities (core, memory), read from every generated layer document: a field the walk missed
    shows as a count below four or two.
    """
    found: dict[str, list[list[Any]]] = {}
    for layer in registry.layers:
        _asil_enums(layer_schema(layer), found)
    assert found.get("asil") == [[*ASIL_VALUES, None]] * 4
    assert found.get("asil_capability") == [[*ASIL_LEVELS, None]] * 2


def test_hardware_renders_four_branches_with_its_enum_and_its_list(registry: ModelRegistry) -> None:
    """The hardware union's four branches, its four-member ``memory.type`` enum and its list of names.

    Read off the branch the ``kind`` pin selects, because a union layer has no properties of its own.
    """
    layer = registry.layer("hardware")
    document = layer_schema(layer)
    processor, core, memory, partition = document["oneOf"]
    assert [branch["properties"]["kind"]["const"] for branch in document["oneOf"]] == [
        "processor",
        "core",
        "memory",
        "partition",
    ]
    assert partition["required"] == ["name", "kind", "core"]

    assert processor["required"] == ["name", "kind"]
    assert core["required"] == ["name", "kind", "threshold"]
    assert core["properties"]["hw_id"]["type"] == ["integer", "null"], "not required, and no `default` key"
    assert "default" not in core["properties"]["hw_id"]

    assert memory["required"] == ["name", "kind", "type", "size", "accessible", "threshold"]
    assert memory["properties"]["type"] == {
        "description": registry.kind("memory").get_field("type").description,
        "type": "string",
        "enum": ["RAM", "ROM", "EEPROM", "Flash"],
    }, "the four AUTOSAR memoryType literals, in the contract's order"
    assert memory["properties"]["accessible"] == {
        "description": registry.kind("memory").get_field("accessible").description,
        "type": "array",
        "items": {"type": "string"},
    }, "a list of core names; which names are legal is another layer's content"

    valid = Draft7Validator(document).is_valid
    assert valid(
        {"name": "RAM0", "kind": "memory", "type": "RAM", "size": 1, "accessible": ["CORE0"], "threshold": 0.8}
    )
    assert not valid(
        {"name": "RAM0", "kind": "memory", "type": "SRAM", "size": 1, "accessible": ["CORE0"], "threshold": 0.8}
    ), "a memory type the contract does not declare"
    assert not valid(
        {"name": "RAM0", "kind": "memory", "type": "RAM", "size": 1, "accessible": "CORE0", "threshold": 0.8}
    ), "`accessible` is a list, never a bare name"


def test_a_task_partition_renders_as_a_bare_string_with_the_reference_unchecked(registry: ModelRegistry) -> None:
    """`field:tasks.task.partition` carries a `reference`, and JSON Schema has no vocabulary for it.

    The editor accepts any partition name; `check-model` alone refuses an undeclared one,
    gated in ``test_editor_schema_limits.py``.
    """
    partition = registry.kind("task").get_field("partition")
    assert partition.reference is not None, "the contract really declares the reference this test says is unexpressed"

    rendered = layer_schema(registry.layer("tasks"))["properties"]["partition"]
    assert rendered == {"description": partition.description, "type": "string"}
    assert set(rendered) == {"description", "type"}, "nothing narrows the accepted names"

    document = {"name": "T", "period": 0.01, "priority": 1, "partition": "NoSuchPartition"}
    assert list(Draft7Validator(layer_schema(registry.layer("tasks"))).iter_errors(document)) == []


def test_the_single_kind_layers_render_no_branch_construct(registry: ModelRegistry) -> None:
    single = [layer for layer in registry.layers if len(layer.top_level_kinds) == 1]
    assert len(single) == 10, (
        "measured on the shipped contract: ten single-kind layers (six at `6.0.0`, seven until it removed "
        "`systems`, eight since `8.0.0` added `base_types`, ten since its elements added `units` and "
        "`data_constraints`)"
    )
    for layer in single:
        document = layer_schema(layer)
        assert "oneOf" not in document, layer.name
        assert "anyOf" not in document and "if" not in document, layer.name
        assert document["type"] == "object", layer.name


# The whole-contract sweep: all_artifacts()


def test_all_artifacts_covers_every_layer_in_both_directions(registry: ModelRegistry) -> None:
    artifacts = all_artifacts()
    expected = [MANIFEST_ARTIFACT_PATH] + [artifact_path(layer) for layer in registry.layers] + [MAPPING_ARTIFACT_PATH]
    assert list(artifacts) == expected, (
        "the manifest, then one artifact per layer in registry order, then the needs mapping; none extra"
    )
    assert len(artifacts) == len(registry.layers) + 2
    assert artifacts[MANIFEST_ARTIFACT_PATH] == render(manifest_schema())
    assert artifacts[MAPPING_ARTIFACT_PATH] == render(mapping_schema())
    for layer in registry.layers:
        assert artifacts[artifact_path(layer)] == render(layer_schema(layer)), layer.name
    for path, text in artifacts.items():
        assert path.endswith(".schema.json") and "/" not in path
        assert f'"$id": "{SCHEMA_ID_PREFIX}{path}"' in text, "the file name and the `$id` agree"


def test_a_synthetic_layer_bound_with_use_registry_gains_an_artifact_and_an_association() -> None:
    """The seam is real: under `use_registry` the synthetic layer appears in both enumerations.

    Stage 11 of `test_synthetic_flow_through.py` asserts the layer's whole document; this
    asserts only that both helpers read the bound registry, not one captured at import time.
    """
    merged = build_model_registry(contract=merged_contract(), binding=merged_binding())
    synthetic = merged.layer("synthetic")
    assert synthetic.name not in {layer.name for layer in current_registry().layers}
    with use_registry(merged):
        artifacts = all_artifacts()
        entries = yaml_validation_entries()
    assert list(artifacts) == (
        [MANIFEST_ARTIFACT_PATH] + [artifact_path(layer) for layer in merged.layers] + [MAPPING_ARTIFACT_PATH]
    )
    assert artifacts["synthetic.schema.json"] == render(layer_schema(synthetic))
    assert entries[-2] == {"fileMatch": ["**/synthetic/*.yaml"], "url": "./schemas/synthetic.schema.json"}
    assert "synthetic.schema.json" not in all_artifacts(), "outside the block the production registry is back"
    assert all_artifacts(registry=merged) == artifacts, "an explicit registry is the same seam"


@pytest.mark.parametrize("layer_name", [layer.name for layer in current_registry().layers])
def test_every_artifact_is_a_well_formed_draft_7_schema(layer_name: str, registry: ModelRegistry) -> None:
    document = layer_schema(registry.layer(layer_name))
    Draft7Validator.check_schema(document)


# The reference table: references_artifact() and editor_artifacts()


def _independent_key_paths(
    registry: ModelRegistry, navigated: Callable[[FieldFacts], bool]
) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    """Every ``(top-level kind, key path)`` reaching each field *navigated* selects, re-walked breadth-first here.

    Deliberately not the generator's loop: an independent queue reading only ``KindFacts``.
    Sorted, since only the set and its multiplicity are the oracle; the generator's own
    order is pinned by the drift gate.
    """
    found: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for layer in registry.layers:
        kinds = {kind.kind: kind for kind in layer.kinds}
        queue = deque((top.kind, top, (), (top.kind,)) for top in layer.top_level_kinds)
        while queue:
            top, kind, path, seen = queue.popleft()
            for field in kind.fields:
                if navigated(field):
                    found.setdefault(field.id, []).append((top, (*path, field.name)))
                members = list(field.collection.member_kinds) if field.collection is not None else []
                for member in members:
                    if member not in seen:
                        queue.append((top, kinds[member], (*path, field.name, "*"), (*seen, member)))
                if field.element_kind is not None and field.element_kind not in seen:
                    queue.append((top, kinds[field.element_kind], (*path, field.name), (*seen, field.element_kind)))
    return {field_id: sorted(paths) for field_id, paths in found.items()}


def _independent_reference_paths(registry: ModelRegistry) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    return _independent_key_paths(registry, lambda field: field.reference is not None)


def _published_paths(rows: list[dict[str, Any]]) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    return {row["field"]: sorted((path["topKind"], tuple(path["path"])) for path in row["paths"]) for row in rows}


def _references_document(registry: ModelRegistry | None = None) -> dict[str, Any]:
    return json.loads(references_artifact(registry=registry))


def test_every_reference_edge_appears_once_with_its_six_facts(registry: ModelRegistry) -> None:
    document = _references_document()
    rows = document["references"]
    assert [row["field"] for row in rows] == [edge.field_id for edge in registry.references()], (
        "every edge once, in contract order, none invented"
    )
    for row, edge in zip(rows, registry.references(), strict=True):
        assert isinstance(edge, ReferenceEdge)
        assert {key: value for key, value in row.items() if key != "paths"} == {
            "field": edge.field_id,
            "sourceLayer": edge.source_layer,
            "sourceKind": edge.source_kind,
            "sourceField": edge.source_field,
            "targetLayer": edge.target_layer,
            "targetIdentityField": edge.target_identity_field,
        }, edge.field_id


def test_every_reference_path_is_the_independent_walk(registry: ModelRegistry) -> None:
    """The key paths are exactly what a re-walk over ``KindFacts`` finds — none missing, none invented."""
    expected = _independent_reference_paths(registry)
    published = {
        row["field"]: sorted((path["topKind"], tuple(path["path"])) for path in row["paths"])
        for row in _references_document()["references"]
    }
    assert published == expected
    for row in _references_document()["references"]:
        layer = registry.layer(row["sourceLayer"])
        top_kinds = {kind.kind for kind in layer.top_level_kinds}
        for path in row["paths"]:
            assert path["topKind"] in top_kinds, row["field"]
            assert path["path"][-1] == row["sourceField"], row["field"]


def test_no_top_level_kind_reaches_two_reference_fields_by_one_path(registry: ModelRegistry) -> None:
    """The navigator's lookup is ``(topKind, keyPath) -> edge``; it is a function only if no pair repeats."""
    pairs = [
        (row["sourceLayer"], path["topKind"], tuple(path["path"]))
        for row in _references_document()["references"]
        for path in row["paths"]
    ]
    assert len(pairs) == len(set(pairs))


def test_every_layer_carries_its_nine_facts_off_layer_facts(registry: ModelRegistry) -> None:
    layers = _references_document()["layers"]
    assert list(layers) == [layer.name for layer in registry.layers]
    for layer in registry.layers:
        entry = layers[layer.name]
        top_level = layer.top_level_kinds
        assert list(entry) == [
            "token",
            "directory",
            "nesting",
            "fileSuffix",
            "identityField",
            "filenameFrom",
            "discriminator",
            "guidePage",
            "topLevelKinds",
        ], layer.name
        assert entry["token"] == layer.token
        assert entry["directory"] == layer.storage.directory
        assert entry["nesting"] == layer.storage.nesting
        assert entry["fileSuffix"] == layer.storage.file_suffix
        assert entry["identityField"] == layer.identity_field
        assert entry["filenameFrom"] == layer.storage.filename_from
        assert entry["guidePage"] == layer_section_page(layer), layer.name
        assert [kind["kind"] for kind in entry["topLevelKinds"]] == [kind.kind for kind in top_level]
        assert [kind["guideAnchor"] for kind in entry["topLevelKinds"]] == [kind_anchor(kind) for kind in top_level]
        if len(top_level) == 1:
            assert entry["discriminator"] is None, layer.name
            assert entry["topLevelKinds"][0]["discriminatorValue"] is None, layer.name
            continue
        (discriminator,) = {kind.discriminator_field for kind in top_level}
        assert entry["discriminator"] == discriminator, layer.name
        for published, kind in zip(entry["topLevelKinds"], top_level, strict=True):
            field = kind.get_field(discriminator)
            assert field is not None
            assert [published["discriminatorValue"]] == list(field.enum), kind.id


def test_the_two_base_edges_differ_by_top_level_kind(registry: ModelRegistry) -> None:
    """The case the navigator's discriminator read exists for: one field name, two targets."""
    rows = [
        row
        for row in _references_document()["references"]
        if row["sourceLayer"] == "implementation_types" and row["sourceField"] == "base"
    ]
    by_kind = {path["topKind"]: row["targetLayer"] for row in rows for path in row["paths"]}
    assert by_kind == {"value_type": "base_types", "reference_type": "implementation_types"}


def test_the_reference_table_carries_no_version() -> None:
    text = references_artifact()
    assert __version__ not in text
    document = json.loads(text)
    assert list(document) == ["layers", "references", "relations", "collections", "memberReferences"], (
        "no version key, no contract version"
    )


def test_an_unreachable_reference_is_refused_not_rendered(registry: ModelRegistry) -> None:
    edge = registry.references()[0]
    planted = replace(edge, field_id="field:nowhere.thing.ref")
    broken = replace(registry, _references=(*registry.references(), planted))
    with pytest.raises(UnreachableReference, match="no key path") as info:
        references_artifact(registry=broken)
    assert info.value.field_id == "field:nowhere.thing.ref"
    assert "JSON Schema" not in str(info.value), "a reference-table refusal is not a schema-rendering one"


def _collection(field: str, members: tuple[str, ...], kind: str = "thing") -> CollectionFacts:
    return CollectionFacts(
        field_id=f"field:x.{kind}.{field}",
        collection_id=f"collection:x.{kind}.{field}",
        kind=kind,
        layer="x",
        field=field,
        collection_kind="list",
        member_identity="named",
        member_kinds=members,
    )


def _reference_layer(part_fields: tuple[FieldFacts, ...]) -> LayerFacts:
    """Layer ``x``: a top-level ``thing`` whose ``parts`` collect member-only ``part`` kinds."""
    parts = _field("parts", "list[part]", collection=_collection("parts", ("part",)))
    thing = replace(_union_kind(("thing",)), fields=(_field("name", "string"), parts), discriminator_field=None)
    part = KindFacts(
        id="kind:x.part",
        kind="part",
        layer="x",
        model=ModelElement,
        fields=part_fields,
        discriminator_field=None,
        description="A member kind.",
        member_only=True,
    )
    return _union_layer((thing, part))


def test_a_self_recursive_member_kind_is_walked_once_per_path() -> None:
    """``part.children`` collects ``part`` again: the walk does not re-enter it, so one path, no loop."""
    edge = ReferenceEdge(
        field_id="field:x.part.ref",
        source_layer="x",
        source_kind="part",
        source_field="ref",
        target_layer="x",
        target_identity_field="name",
    )
    ref = replace(_field("ref", "string"), id="field:x.part.ref", kind="part", reference=edge)
    children = replace(
        _field("children", "list[part]", collection=_collection("children", ("part",), kind="part")),
        id="field:x.part.children",
        kind="part",
    )
    paths = _key_paths(_reference_layer((ref, children)))
    assert paths == {
        "field:x.thing.parts": [{"topKind": "thing", "path": ["parts"]}],
        "field:x.part.ref": [{"topKind": "thing", "path": ["parts", "*", "ref"]}],
        "field:x.part.children": [{"topKind": "thing", "path": ["parts", "*", "children"]}],
    }, "the collections are collected by the same pass, each path ending at the field"


def test_a_member_kind_the_reference_walk_cannot_find_is_refused() -> None:
    """``part.extras`` collects a ``ghost`` the layer does not declare: refused naming the field, not skipped."""
    extras = replace(
        _field("extras", "list[ghost]", collection=_collection("extras", ("ghost",), kind="part")),
        id="field:x.part.extras",
        kind="part",
    )
    with pytest.raises(UnreachableReference) as info:
        _key_paths(_reference_layer((extras,)))
    assert info.value.field_id == "field:x.part.extras"
    assert "member kind 'ghost' is not declared on layer 'x'" in str(info.value)


def test_every_relation_appears_once_with_its_facts(registry: ModelRegistry) -> None:
    rows = _references_document()["relations"]
    assert [row["field"] for row in rows] == [edge.field_id for edge in registry.relations()], (
        "every relation once, in contract order, none invented"
    )
    for row, edge in zip(rows, registry.relations(), strict=True):
        assert {key: value for key, value in row.items() if key != "paths"} == {
            "field": edge.field_id,
            "sourceLayer": edge.source_layer,
            "targetLayer": edge.target_layer,
            "targetKind": edge.target_kind,
            "targetField": edge.target_field,
            "resolution": edge.resolution,
        }, edge.field_id
        assert row["targetField"] == registry.layer(row["targetLayer"]).identity_field, edge.field_id


def test_every_relation_path_is_the_independent_walk(registry: ModelRegistry) -> None:
    rows = _references_document()["relations"]
    expected = _independent_key_paths(registry, lambda field: field.relation is not None)
    assert _published_paths(rows) == expected
    for row in rows:
        for path in row["paths"]:
            assert path["path"][-1] == row["field"].rsplit(".", 1)[1], "a relation path ends at the field"


def test_a_relation_targeting_a_non_identity_field_is_refused_not_rendered(registry: ModelRegistry) -> None:
    """Navigation resolves a relation by the target's identity; a row matching another field would mislead."""
    edge = registry.relations()[0]
    planted = replace(edge, target_field="not_the_identity")
    broken = replace(registry, _relations=(planted, *registry.relations()[1:]))
    with pytest.raises(UnreachableReference, match="is not the identity field") as info:
        references_artifact(registry=broken)
    assert info.value.field_id == edge.field_id


def test_every_collection_appears_once_with_its_facts(registry: ModelRegistry) -> None:
    rows = _references_document()["collections"]
    assert [row["field"] for row in rows] == [collection.field_id for collection in registry.collections()]
    owners = {
        field.id: (kind, field)
        for layer in registry.layers
        for kind in layer.kinds
        for field in kind.fields
        if field.collection is not None
    }
    assert set(owners) == {row["field"] for row in rows}, "every collection-carrying field, and no other"
    for row in rows:
        kind, field = owners[row["field"]]
        collection = field.collection
        assert collection is not None
        assert {key: value for key, value in row.items() if key != "paths"} == {
            "field": field.id,
            "layer": kind.layer,
            "collectionKind": collection.collection_kind,
            "memberIdentity": collection.member_identity,
            "identityField": collection.identity_field,
            "identityFields": list(collection.identity_fields),
            "memberKinds": list(collection.member_kinds),
            "discriminatorField": collection.discriminator_field,
        }, row["field"]


def test_every_collection_path_is_the_independent_walk(registry: ModelRegistry) -> None:
    rows = _references_document()["collections"]
    assert _published_paths(rows) == _independent_key_paths(registry, lambda field: field.collection is not None)


def test_every_member_reference_appears_once_with_its_facts_and_paths(registry: ModelRegistry) -> None:
    rows = _references_document()["memberReferences"]
    edges = registry.member_references()
    assert [row["field"] for row in rows] == [edge.field_id for edge in edges]
    assert _published_paths(rows) == _independent_key_paths(registry, lambda field: field.member_reference is not None)
    collection_paths = _published_paths(_references_document()["collections"])
    for row, edge in zip(rows, edges, strict=True):
        assert isinstance(edge, MemberReferenceEdge)
        assert row["sourceLayer"] == edge.source_layer
        assert row["via"] == edge.via
        assert [target["id"] for target in row["collections"]] == [target.collection_id for target in edge.collections]
        for published, target in zip(row["collections"], edge.collections, strict=True):
            assert {key: value for key, value in published.items() if key != "path"} == {
                "id": target.collection_id,
                "layer": target.layer,
                "kind": target.kind,
                "identityField": target.identity_field,
            }
            field_id = f"field:{target.layer}.{target.kind}.{target.field}"
            own = [path for top, path in collection_paths[field_id] if top == target.kind]
            assert [tuple(published["path"])] == own, target.collection_id


def test_the_six_member_references_publish_what_the_plan_declares() -> None:
    """A spot check by value, beside the walk-equality tests: the element access reaches both data-element lists."""
    rows = {row["field"]: row for row in _references_document()["memberReferences"]}
    row = rows["field:swc.data_write.element"]
    assert row["via"] == "port"
    assert row["paths"] == [{"topKind": "swc", "path": ["runnables", "*", "writes", "*", "element"]}]
    assert row["collections"] == [
        {
            "id": "collection:interfaces.sender_receiver.data_elements",
            "layer": "interfaces",
            "kind": "sender_receiver",
            "path": ["data_elements"],
            "identityField": "name",
        },
        {
            "id": "collection:interfaces.external_interface.data_elements",
            "layer": "interfaces",
            "kind": "external_interface",
            "path": ["data_elements"],
            "identityField": None,
        },
    ]
    assert rows["field:swc.data_read.port"]["via"] is None
    assert [target["path"] for target in rows["field:swc.data_read.port"]["collections"]] == [["ports"]]


@pytest.mark.parametrize("section", ["relation", "member reference"])
def test_an_unreachable_relation_or_member_reference_is_refused_not_rendered(
    registry: ModelRegistry, section: str
) -> None:
    if section == "relation":
        planted_relation = replace(registry.relations()[0], field_id="field:nowhere.thing.rel")
        broken = replace(registry, _relations=(*registry.relations(), planted_relation))
    else:
        planted_member = replace(registry.member_references()[0], field_id="field:nowhere.thing.member")
        broken = replace(registry, _member_references=(*registry.member_references(), planted_member))
    with pytest.raises(UnreachableReference, match=f"no key path from a top-level kind reaches this {section}"):
        references_artifact(registry=broken)


def test_an_unreachable_collection_is_refused_not_rendered(registry: ModelRegistry) -> None:
    planted = replace(registry.collections()[0], field_id="field:nowhere.thing.items")
    broken = replace(registry, _collections=(*registry.collections(), planted))
    with pytest.raises(UnreachableReference, match="reaches this collection") as info:
        references_artifact(registry=broken)
    assert info.value.field_id == "field:nowhere.thing.items"


def test_a_member_reference_collection_its_own_kind_does_not_reach_is_refused(registry: ModelRegistry) -> None:
    """The published ``path`` is the collection's from its owner as a top-level kind; a member-kind owner has none."""
    edge = registry.member_references()[0]
    target = MemberReferenceTarget(
        collection_id="collection:swc.runnable.reads",
        layer="swc",
        kind="runnable",
        field="reads",
        identity_field=None,
    )
    planted = replace(edge, collections=(target,))
    broken = replace(registry, _member_references=(planted, *registry.member_references()[1:]))
    with pytest.raises(UnreachableReference, match="reached by 0 paths from its own kind 'runnable'"):
        references_artifact(registry=broken)


def test_a_planted_layers_collection_appears_through_use_registry(registry: ModelRegistry) -> None:
    """The seam: a collection of a layer only the bound registry declares is published with its walk's path."""
    layer = _reference_layer((replace(_field("name", "string"), id="field:x.part.name", kind="part"),))
    parts = layer.kinds[0].get_field("parts")
    assert parts is not None and parts.collection is not None
    planted = replace(
        registry,
        layers=(*registry.layers, layer),
        _collections=(*registry.collections(), parts.collection),
    )
    with use_registry(planted):
        rows = _references_document()["collections"]
    assert rows[-1]["field"] == "field:x.thing.parts"
    assert rows[-1]["paths"] == [{"topKind": "thing", "path": ["parts"]}]
    assert "field:x.thing.parts" not in {row["field"] for row in _references_document()["collections"]}


def test_editor_artifacts_are_the_schemas_plus_the_reference_table_and_the_page_script_hash() -> None:
    artifacts = editor_artifacts()
    schemas = all_artifacts()
    assert list(artifacts) == [*schemas, REFERENCES_ARTIFACT, DIAGRAM_PAGE_SCRIPT_ARTIFACT]
    assert {path: artifacts[path] for path in schemas} == schemas
    assert artifacts[REFERENCES_ARTIFACT] == references_artifact()
    assert REFERENCES_ARTIFACT not in schemas, "all_artifacts() stays the schema set"
    assert DIAGRAM_PAGE_SCRIPT_ARTIFACT not in schemas, "all_artifacts() stays the schema set"


def test_the_page_script_hash_is_the_csp_hash_of_the_page_script_as_every_view_page_writes_it() -> None:
    """The extension allows the page's script by this hash alone.

    So it must be the hash of the exact text between ``<script>`` and ``</script>`` of a generated view page.
    """
    document = json.loads(diagram_page_script_artifact())
    assert list(document) == ["sha256"]
    page = render_view_page("P", "ibd-Root", '<svg xmlns="http://www.w3.org/2000/svg"></svg>\n', ["ibd-Root"])
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    assert script == PAGE_SCRIPT
    expected = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    assert document["sha256"] == expected == page_script_sha256()
    assert diagram_page_script_artifact().endswith("}\n")


def test_the_reference_table_sees_a_synthetic_layer_bound_with_use_registry() -> None:
    merged = build_model_registry(contract=merged_contract(), binding=merged_binding())
    with use_registry(merged):
        layers = _references_document()["layers"]
        artifacts = editor_artifacts()
    assert "synthetic" in layers
    assert list(layers) == [layer.name for layer in merged.layers]
    assert "synthetic" not in _references_document()["layers"], "outside the block the production registry is back"
    assert artifacts == editor_artifacts(registry=merged), "an explicit registry is the same seam"


# The corpus oracle: examples/basic/model/ and the hand-built fixtures


def _documents_under(root: Path, registry: ModelRegistry) -> list[tuple[Path, LayerFacts, dict]]:
    """Every ``*.yaml`` under *root*, with the layer whose directory holds it."""
    found: list[tuple[Path, LayerFacts, dict]] = []
    for path in sorted(root.rglob("*.yaml"), key=lambda path: path.as_posix()):
        layer = layer_for_path(path.relative_to(root).as_posix(), registry)
        with path.open(encoding="utf-8") as handle:
            found.append((path, layer, yaml.safe_load(handle)))
    return found


def _top_level_kind_of(layer: LayerFacts, document: dict) -> str:
    if len(layer.top_level_kinds) == 1:
        return layer.top_level_kinds[0].kind
    return str(document["kind"])


def _corpus_documents() -> list[tuple[Path, LayerFacts, dict]]:
    return _documents_under(_CORPUS_ROOT, current_registry())


def _fixture_documents() -> list[tuple[Path, LayerFacts, dict]]:
    return _documents_under(_FIXTURE_ROOT, current_registry())


def _ids(documents: list[tuple[Path, LayerFacts, dict]], root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path, _, _ in documents]


# Read once at collection: both parametrizations and the reachability test below share them.
_CORPUS_DOCUMENTS = _corpus_documents()
_FIXTURE_DOCUMENTS = _fixture_documents()


@pytest.mark.parametrize(("path", "layer", "document"), _CORPUS_DOCUMENTS, ids=_ids(_CORPUS_DOCUMENTS, _CORPUS_ROOT))
def test_every_corpus_file_validates_clean_against_its_layers_schema(
    path: Path, layer: LayerFacts, document: dict
) -> None:
    errors = sorted(Draft7Validator(layer_schema(layer)).iter_errors(document), key=str)
    assert errors == [], f"{path.relative_to(REPO_ROOT)}: {[error.message for error in errors]}"


@pytest.mark.parametrize(("path", "layer", "document"), _FIXTURE_DOCUMENTS, ids=_ids(_FIXTURE_DOCUMENTS, _FIXTURE_ROOT))
def test_every_hand_built_fixture_validates_clean_and_loads_through_the_real_loader(
    path: Path, layer: LayerFacts, document: dict, tmp_path: Path
) -> None:
    """A fixture is valid in both senses: the schema accepts it AND the loader parses it."""
    errors = sorted(Draft7Validator(layer_schema(layer)).iter_errors(document), key=str)
    assert errors == [], f"{path.relative_to(REPO_ROOT)}: {[error.message for error in errors]}"

    model_root = tmp_path / "model"
    target = model_root / layer.storage.directory / path.name
    target.parent.mkdir(parents=True)
    shutil.copyfile(path, target)
    (loaded,) = load_layer(model_root, layer.name)
    assert getattr(loaded, layer.identity_field) == path.stem


@pytest.mark.parametrize(
    ("layer_name", "document"),
    [
        ("tasks", {"name": "Task_null", "period": 0.01, "priority": 1, "partition": "PARTITION0", "wcet": None}),
        (
            "implementation_types",
            {"name": "Speed", "kind": "value_type", "base": "uint16", "value_min": None, "value_max": None},
        ),
    ],
    ids=["tasks.wcet", "implementation_types.alias.value_min+value_max"],
)
def test_an_explicit_null_on_a_nullable_field_is_accepted_by_the_schema_and_by_the_loader(
    layer_name: str, document: dict, registry: ModelRegistry, tmp_path: Path
) -> None:
    """The `["<type>", "null"]` shape is not only a rendering: a spelled-out `null` passes both oracles."""
    layer = registry.layer(layer_name)
    assert list(Draft7Validator(layer_schema(layer)).iter_errors(document)) == []

    model_root = tmp_path / "model"
    target = model_root / layer.storage.directory / f"{document['name']}.yaml"
    target.parent.mkdir(parents=True)
    text = yaml.safe_dump(document, sort_keys=False)
    assert ": null\n" in text, "the YAML really spells the null out"
    target.write_text(text, encoding="utf-8")
    (loaded,) = load_layer(model_root, layer.name)
    assert loaded.name == document["name"]
    for field, value in document.items():
        if value is None:
            assert getattr(loaded, field) is None, field


def test_an_explicit_null_by_on_a_read_is_accepted_by_the_schema_and_by_the_loader(
    registry: ModelRegistry, tmp_path: Path
) -> None:
    """The nullable *enum* field `data_read.by` in both oracles; its own case as swc files sit a directory down."""
    layer = registry.layer("swc")
    document = {
        "name": "NullBySWC",
        "ports": [{"name": "rSpeed", "direction": "require", "interface": "ISpeed"}],
        "runnables": [{"name": "Run", "reads": [{"port": "rSpeed", "element": "value", "by": None}]}],
    }
    assert list(Draft7Validator(layer_schema(layer)).iter_errors(document)) == []

    target = tmp_path / "model" / layer.storage.directory / "example" / "NullBySWC.yaml"
    target.parent.mkdir(parents=True)
    text = yaml.safe_dump(document, sort_keys=False)
    assert "by: null\n" in text, "the YAML really spells the null out"
    target.write_text(text, encoding="utf-8", newline="\n")
    (loaded,) = load_layer(tmp_path / "model", layer.name)
    assert loaded.runnables[0].reads[0].by is None


def test_the_fixtures_are_exactly_the_kinds_the_corpus_misses(registry: ModelRegistry) -> None:
    """The split the module docstring states, re-derived from the files in both directions.

    Not reached: the split covers top-level kinds only, so member kinds the corpus never
    declares (`composition_port`, `delegation`) are validated by no committed document here.
    """
    all_kinds = {kind.kind for layer in registry.layers for kind in layer.top_level_kinds}
    reached_by_corpus = {_top_level_kind_of(layer, doc) for _, layer, doc in _CORPUS_DOCUMENTS}
    reached_by_fixtures = {_top_level_kind_of(layer, doc) for _, layer, doc in _FIXTURE_DOCUMENTS}

    assert len(all_kinds) == 29, (
        "measured on the shipped contract (17 at `6.0.0`, before `7.0.0` declared `application_type` and "
        "`mapping_set`; 19 until `7.5.0` declared `external_interface` and `external_type`; 21 until `7.8.0` "
        "declared `implementation_record`, `implementation_array` and `pointer`; 24 until `8.0.0`'s elements "
        "declared `unit`, `texttable`, `linear` and `data_constr`; 28 until `9.0.0` declared `partition`, "
        "which `examples/basic` declares, so the corpus reaches it)"
    )
    assert reached_by_corpus <= all_kinds and reached_by_fixtures <= all_kinds
    assert reached_by_fixtures == all_kinds - reached_by_corpus, (
        "a fixture for every kind the corpus misses, and none for a kind it reaches: "
        f"missing {sorted(all_kinds - reached_by_corpus - reached_by_fixtures)}, "
        f"redundant {sorted(reached_by_fixtures & reached_by_corpus)}"
    )
    assert reached_by_fixtures == {
        "reference_type",
        "enum",
        "record",
        "array",
        "application_type",
        "mapping_set",
        "external_interface",
        "external_type",
        "implementation_record",
        "implementation_array",
        "pointer",
        "unit",
        "texttable",
        "linear",
        "data_constr",
    }
    # Each fixture kind is one `examples/basic` never declares: the project is authored
    # whole, types every interface by a scalar implementation type and has an empty
    # mapping-set directory. `composition` is reached by its root composition, so a
    # fixture for it would be the redundancy this test refuses.
    assert list((_CORPUS_ROOT / "compositions").glob("*.yaml")) != [], "the corpus reaches `composition` now"


# Schema behaviour: the four rejection shapes


def _errors(layer_name: str, document: dict, registry: ModelRegistry) -> list[str]:
    validator = Draft7Validator(layer_schema(registry.layer(layer_name)))
    return [error.message for error in validator.iter_errors(document)]


_VALID_TASK = {"name": "Task_10ms", "period": 0.01, "priority": 5, "partition": "PARTITION0"}


def test_an_unknown_key_is_rejected_naming_the_key(registry: ModelRegistry) -> None:
    assert _errors("tasks", _VALID_TASK, registry) == []
    messages = _errors("tasks", {**_VALID_TASK, "perriod": 0.02}, registry)
    assert len(messages) == 1 and "'perriod'" in messages[0], messages


def test_a_wrong_scalar_type_is_rejected_naming_the_property(registry: ModelRegistry) -> None:
    validator = Draft7Validator(layer_schema(registry.layer("tasks")))
    (error,) = list(validator.iter_errors({**_VALID_TASK, "priority": "high"}))
    assert list(error.absolute_path) == ["priority"]
    assert "'high' is not of type 'integer'" == error.message


def test_an_invalid_enum_member_is_rejected_naming_the_property(registry: ModelRegistry) -> None:
    validator = Draft7Validator(layer_schema(registry.layer("swc")))
    (error,) = list(validator.iter_errors({"name": "S", "type": "middleware"}))
    assert list(error.absolute_path) == ["type"]
    assert "'middleware' is not one of" in error.message
    assert error.validator == "enum"


def test_a_missing_required_field_is_rejected_naming_the_field(registry: ModelRegistry) -> None:
    messages = _errors("tasks", {"name": "T", "period": 0.01, "partition": "PARTITION0"}, registry)
    assert messages == ["'priority' is a required property"]


def test_a_union_branch_rejects_inside_the_branch_its_kind_selects(registry: ModelRegistry) -> None:
    """The offending property is named under the branch the `kind` pin selects, not only `oneOf`."""
    validator = Draft7Validator(layer_schema(registry.layer("implementation_types")))
    (error,) = list(validator.iter_errors({"name": "T", "kind": "value_type", "base": 32}))
    assert error.validator == "oneOf"
    # The value type's branch is the first.
    alias_context = [ctx for ctx in error.context if ctx.schema_path[0] == 0]
    assert [list(ctx.absolute_path) for ctx in alias_context] == [["base"]]
    assert alias_context[0].message == "32 is not of type 'string'"


# The manifest schema


#: Every field the manifest schema renders, in declaration order — the hand-held
#: list the generated document is held against. `source` is absent because the
#: loader sets it and the generator drops it; `toolchain_version`
#: is the one nullable member, and it is what makes the branch below a branch.
_MANIFEST_FIELDS = (
    "name",
    "schema_version",
    "model_root",
    "dist_root",
    "views_root",
    "outputs",
    "arxml_version",
    "toolchain_version",
)


def test_manifest_schema_carries_the_document_header_and_the_dialects_container() -> None:
    document = manifest_schema()
    assert list(document)[:4] == ["$schema", "$id", "title", "description"]
    assert document["$schema"] == SCHEMA_DIALECT
    assert document["$id"] == f"{SCHEMA_ID_PREFIX}{MANIFEST_ARTIFACT_PATH}"
    assert document["title"] == "SWCraft project manifest"
    assert document["description"].startswith("The `swcraft.yaml` at a project's root")
    assert "$defs" not in document and list(document["definitions"]) == ["OutputPaths"]
    assert document["properties"]["outputs"]["$ref"] == "#/definitions/OutputPaths", (
        "the `$ref` was rewritten with the container"
    )
    assert "$defs" not in render(document)
    Draft7Validator.check_schema(document)


def test_manifest_schema_has_the_declared_fields_and_the_nested_outputs_with_defaults_and_descriptions() -> None:
    document = manifest_schema()
    assert list(document["properties"]) == list(_MANIFEST_FIELDS)
    assert document["required"] == ["name"]
    defaults = SwcraftProject.model_fields
    for name, prop in document["properties"].items():
        assert prop["description"], name
        assert "title" not in prop, "Pydantic's per-property `title` is dropped, as in the layer documents"
        if name == "name":
            assert "default" not in prop and prop["type"] == "string"
        elif name == "outputs":
            assert prop["default"] == OutputPaths().model_dump()
        elif name == "toolchain_version":
            # The one `Optional[...] = None` field: collapsed to the scalar-or-null
            # type the layer documents use, with the `null` default dropped rather
            # than rendered as a value an architect could write.
            assert prop["type"] == ["string", "null"] and "default" not in prop
        else:
            assert prop["type"] == "string" and prop["default"] == defaults[name].default, name
    outputs = document["definitions"]["OutputPaths"]
    assert outputs["description"] == (
        "Where the ARXML and needs.json generators write, relative to the project directory."
    )
    assert list(outputs["properties"]) == ["arxml", "needs"]
    for name, prop in outputs["properties"].items():
        assert prop["description"] and prop["type"] == "string", name
        assert prop["default"] == OutputPaths.model_fields[name].default
        assert list(prop) == ["description", "type", "default"], "the layer documents' key order"
    assert "required" not in outputs


def test_manifest_schema_drops_the_source_provenance_field() -> None:
    assert "source" in SwcraftProject.model_fields, "the runtime marker still exists on the class"
    document = manifest_schema()
    assert "source" not in document["properties"]
    assert "source" not in render(document)


def test_manifest_keys_are_the_manifest_schema_s_properties() -> None:
    """``check-model``'s declared manifest keys are the editor's ``properties``, at every level."""
    document = manifest_schema()
    keys = manifest_keys()
    assert keys[""] == frozenset(document["properties"])
    assert MANIFEST_RUNTIME_FIELDS.isdisjoint(keys[""])
    referenced = {
        name: prop["$ref"].rsplit("/", 1)[-1]
        for name, prop in document["properties"].items()
        if isinstance(prop, dict) and "$ref" in prop
    }
    assert set(keys) - {""} == set(referenced)
    for name, definition in referenced.items():
        assert keys[name] == frozenset(document["definitions"][definition]["properties"]), name


def test_manifest_schema_closes_both_objects() -> None:
    document = manifest_schema()
    assert document["additionalProperties"] is False
    assert document["definitions"]["OutputPaths"]["additionalProperties"] is False


def test_the_example_manifest_validates_clean_and_the_three_rejections_name_the_key() -> None:
    validator = Draft7Validator(manifest_schema())
    manifest = yaml.safe_load((REPO_ROOT / "examples" / "basic" / "swcraft.yaml").read_text(encoding="utf-8"))
    assert list(validator.iter_errors(manifest)) == []

    (unknown,) = list(validator.iter_errors({**manifest, "model_rooot": "model"}))
    assert unknown.validator == "additionalProperties" and "'model_rooot'" in unknown.message

    (wrong_type,) = list(validator.iter_errors({**manifest, "model_root": 7}))
    assert list(wrong_type.absolute_path) == ["model_root"]
    assert wrong_type.message == "7 is not of type 'string'"

    (nested,) = list(validator.iter_errors({**manifest, "outputs": {**manifest["outputs"], "png": "x"}}))
    assert list(nested.absolute_path) == ["outputs"] and "'png'" in nested.message, "closed through the `$ref` too"


def test_arxml_version_is_an_enum_of_the_arxml_version_members() -> None:
    """Metadata derived from `ArxmlVersion`, so the schema cannot name a release the loader refuses."""
    prop = manifest_schema()["properties"]["arxml_version"]
    assert prop["enum"] == [version.value for version in ArxmlVersion] == ["4.2.2", "4.3.0"]
    assert list(prop) == ["description", "type", "enum", "default"]
    (error,) = list(Draft7Validator(manifest_schema()).iter_errors({"name": "p", "arxml_version": "4.4.0"}))
    assert list(error.absolute_path) == ["arxml_version"] and error.validator == "enum"


def test_a_field_added_to_the_class_appears_in_the_manifest_schema() -> None:
    """The drift direction: the Pydantic class is the authority, the schema follows it."""
    from pydantic import Field

    class Extended(SwcraftProject):
        ninth: int = Field(3, description="A field this test declares and no real manifest has.")

    document = manifest_schema(Extended)
    assert list(document["properties"]) == [*_MANIFEST_FIELDS, "ninth"]
    assert document["properties"]["ninth"] == {
        "description": "A field this test declares and no real manifest has.",
        "type": "integer",
        "default": 3,
    }
    assert "source" not in document["properties"]
    assert "ninth" not in manifest_schema()["properties"], "the default argument is the real class"


def test_a_nullable_manifest_field_collapses_like_a_contract_nullable_and_nested_refs_are_rewritten() -> None:
    """`Optional[T] = None` renders `["<type>", "null"]` with no `default`; a `$ref` under `items` is re-pointed."""
    from pydantic import Field

    class Extended(SwcraftProject):
        budget: int | None = Field(None, description="A nullable integer no real manifest has.")
        extra_outputs: list[OutputPaths] = Field(default_factory=list, description="A list of nested objects.")

    document = manifest_schema(Extended)
    assert document["properties"]["budget"] == {
        "description": "A nullable integer no real manifest has.",
        "type": ["integer", "null"],
    }
    assert document["properties"]["extra_outputs"] == {
        "description": "A list of nested objects.",
        "type": "array",
        "items": {"$ref": "#/definitions/OutputPaths"},
    }
    assert "#/$defs/" not in render(document) and "anyOf" not in render(document)
    Draft7Validator.check_schema(document)
    validator = Draft7Validator(document)
    assert validator.is_valid({"name": "p", "budget": None, "extra_outputs": [{"arxml": "a.arxml"}]})
    assert not validator.is_valid({"name": "p", "extra_outputs": [{"png": "x"}]}), "closed through the list too"


def test_a_nullable_manifest_field_that_is_not_one_scalar_is_refused() -> None:
    """`Optional[OutputPaths]` would leave an `anyOf` over a `$ref` that nothing measured; refused, naming the field."""
    from pydantic import Field

    class Extended(SwcraftProject):
        more: OutputPaths | None = Field(None, description="A nullable object.")

    with pytest.raises(UnsupportedFieldType) as info:
        manifest_schema(Extended)
    assert info.value.field_id == "Extended.more"
    assert "only scalar-or-null" in info.value.type_name


def test_a_non_object_definition_is_refused_rather_than_closed() -> None:
    """A definition that is not object-shaped cannot be closed with `additionalProperties: false`."""
    from enum import StrEnum

    from pydantic import Field

    class Flavour(StrEnum):
        plain = "plain"

    class Extended(SwcraftProject):
        flavour: Flavour = Field(Flavour.plain, description="An enum class Pydantic emits as a `$defs` entry.")

    with pytest.raises(UnsupportedFieldType) as info:
        manifest_schema(Extended)
    assert info.value.field_id == "Flavour"
    assert "only an object definition" in info.value.type_name


def test_the_manifest_descriptions_are_metadata_only() -> None:
    """The manifest's descriptions are metadata only: no default, required set or alias moves with them."""
    expected_defaults = {
        "schema_version": "1.0",
        "model_root": "model",
        "dist_root": "dist",
        "views_root": "views",
        "arxml_version": "4.2.2",
        # Defaulted to nothing, so a manifest without the pin pins nothing.
        "toolchain_version": None,
        "source": "manifest",
    }
    for name, default in expected_defaults.items():
        assert SwcraftProject.model_fields[name].default == default, name
    assert SwcraftProject.model_fields["name"].is_required()
    assert SwcraftProject.model_fields["outputs"].default == OutputPaths()
    assert SwcraftProject(name="p").model_dump() == {
        "name": "p",
        "outputs": OutputPaths().model_dump(),
        **expected_defaults,
    }
    for name, field in [*SwcraftProject.model_fields.items(), *OutputPaths.model_fields.items()]:
        assert field.description, name
        assert field.alias is None and field.validation_alias is None and field.serialization_alias is None, name


# The association block (`contributes.yamlValidation`)


def _entry_for(path: str, entries: list[dict]) -> dict:
    (entry,) = [entry for entry in entries if entry["url"] == f"./schemas/{path}"]
    return entry


def test_the_manifest_entry_is_first_and_matches_the_bare_basename() -> None:
    entries = yaml_validation_entries()
    assert entries[0] == {"fileMatch": [MANIFEST_FILE_MATCH], "url": f"./schemas/{MANIFEST_ARTIFACT_PATH}"}
    assert MANIFEST_FILE_MATCH == "swcraft.yaml", "a basename pattern matches at any depth"


def test_the_mapping_entry_is_last_and_matches_any_name_ending_in_needs_mapping_yaml() -> None:
    entries = yaml_validation_entries()
    assert entries[-1] == {"fileMatch": [MAPPING_FILE_MATCH], "url": f"./schemas/{MAPPING_ARTIFACT_PATH}"}
    assert MAPPING_FILE_MATCH == "**/*needs-mapping.yaml"


def test_a_flat_layer_matches_one_level_below_its_directory(registry: ModelRegistry) -> None:
    tasks = registry.layer("tasks")
    assert tasks.storage.nesting == StorageRecord.FLAT
    entry = _entry_for(artifact_path(tasks), yaml_validation_entries())
    assert entry == {"fileMatch": ["**/tasks/*.yaml"], "url": "./schemas/tasks.schema.json"}


def test_the_group_nested_swc_layer_matches_through_its_group_directory(registry: ModelRegistry) -> None:
    swc = registry.layer("swc")
    assert swc.storage.is_recursive
    entry = _entry_for(artifact_path(swc), yaml_validation_entries())
    assert entry == {"fileMatch": ["**/swc/**/*.yaml"], "url": "./schemas/swc.schema.json"}


def test_a_layer_in_a_two_segment_directory_matches_one_level_below_it() -> None:
    """A layer's directory may have two segments; the glob composes it unchanged.

    ``vscode_extension/src/layerFile.ts`` parses this shape back into a rule of
    the directory ``nest/alpha`` (``layerFile.test.ts``).
    """
    nested = nested_registry(*SIBLING_NESTED_LAYERS)
    entries = yaml_validation_entries(registry=nested)
    globs = [glob for entry in entries for glob in entry["fileMatch"]]
    assert "**/nest/alpha/*.yaml" in globs and "**/nest/beta/*.yaml" in globs, globs


def test_every_glob_is_left_for_the_client_to_prefix(registry: ModelRegistry) -> None:
    """No glob starts with `/` or a scheme: the bundled client prefixes `/` itself."""
    entries = yaml_validation_entries()
    assert len(entries) == len(registry.layers) + 2, "the manifest, one per layer, the needs mapping"
    for entry in entries:
        (glob,) = entry["fileMatch"]
        assert not glob.startswith("/") and "://" not in glob, entry


def test_the_association_urls_are_exactly_the_artifact_paths(registry: ModelRegistry) -> None:
    """The block and ``all_artifacts()`` cannot name different files."""
    urls = [entry["url"] for entry in yaml_validation_entries()]
    assert urls == [f"./schemas/{path}" for path in all_artifacts()]


# The CLI entry point (`python -m swcraft_tools.editor_schema --output-dir DIR`)


def _assert_written_byte_identically(output_dir: Path) -> None:
    expected = editor_artifacts()
    written = {path.name: path.read_bytes() for path in output_dir.iterdir()}
    assert written == {name: text.encode("utf-8") for name, text in expected.items()}


def test_main_writes_every_artifact_byte_identically_to_editor_artifacts(tmp_path: Path, capsys) -> None:
    output_dir = tmp_path / "schemas"
    assert main(["--output-dir", str(output_dir)]) == 0
    _assert_written_byte_identically(output_dir)
    summary = capsys.readouterr().out
    assert (
        f"wrote {len(all_artifacts())} schema files, {REFERENCES_ARTIFACT} and {DIAGRAM_PAGE_SCRIPT_ARTIFACT} to "
        in summary
    )
    assert "removed 0 stale" in summary


def test_main_writes_lf_even_where_the_platform_would_translate(tmp_path: Path, capsys) -> None:
    """The generated schemas and the rewritten ``package.json`` carry LF where text writes default to CRLF.

    Both are committed and pinned byte for byte. :func:`windows_newline_translation` supplies
    the CRLF platform on any host, so a call site that forgets ``newline=`` fails here where a
    source scan would not notice.
    """
    output_dir = tmp_path / "schemas"
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps({"name": "swcraft-vscode", "contributes": {}}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with windows_newline_translation():
        assert main(["--output-dir", str(output_dir), "--package-json", str(package_json)]) == 0
    capsys.readouterr()

    for path in sorted(output_dir.iterdir(), key=lambda path: path.as_posix()):
        assert_no_crlf(path, what=f"the generated {path.name}")
    assert_no_crlf(package_json, what="the rewritten contributes.yamlValidation block")
    _assert_written_byte_identically(output_dir)


def test_main_removes_a_stale_schema_file_and_says_so(tmp_path: Path, capsys) -> None:
    """The file of a renamed or removed layer does not survive a regeneration; other files are left alone."""
    output_dir = tmp_path / "schemas"
    output_dir.mkdir()
    stale = json.dumps({"$id": f"{SCHEMA_ID_PREFIX}old_layer.schema.json", "type": "object"}) + "\n"
    (output_dir / "old_layer.schema.json").write_text(stale, encoding="utf-8")
    (output_dir / "notes.txt").write_text("not a schema\n", encoding="utf-8")
    assert main(["--output-dir", str(output_dir)]) == 0
    assert not (output_dir / "old_layer.schema.json").exists()
    assert (output_dir / "notes.txt").exists(), "only `*.schema.json` is the generator's to remove"
    assert "removed 1 stale" in capsys.readouterr().out
    written = {path.name for path in output_dir.glob("*.schema.json")}
    assert written == set(all_artifacts())


def test_main_leaves_a_schema_file_it_did_not_write(tmp_path: Path, capsys) -> None:
    """``--output-dir`` is user-supplied: a ``*.schema.json`` without the generator's ``$id`` is not its to delete."""
    output_dir = tmp_path / "schemas"
    output_dir.mkdir()
    foreign = json.dumps({"$id": "https://example.org/contract.schema.json", "type": "object"}) + "\n"
    (output_dir / "contract.schema.json").write_text(foreign, encoding="utf-8")
    (output_dir / "anonymous.schema.json").write_text("{}\n", encoding="utf-8")
    (output_dir / "broken.schema.json").write_text("not json at all\n", encoding="utf-8")
    assert main(["--output-dir", str(output_dir)]) == 0
    assert (output_dir / "contract.schema.json").read_text(encoding="utf-8") == foreign
    assert (output_dir / "anonymous.schema.json").exists()
    assert (output_dir / "broken.schema.json").exists()
    summary = capsys.readouterr().out
    assert "removed 0 stale" in summary
    assert "left 3 foreign" in summary
    assert "contract.schema.json" in summary and "broken.schema.json" in summary


def test_main_rewrites_the_yaml_validation_block_of_a_package_json(tmp_path: Path, capsys) -> None:
    """`--package-json` replaces `contributes.yamlValidation` and nothing else, in `json.dumps(indent=2)` form."""
    package_json = tmp_path / "package.json"
    before = {"name": "x", "contributes": {"commands": [{"command": "a"}], "yamlValidation": [{"stale": True}]}}
    package_json.write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")
    assert main(["--output-dir", str(tmp_path / "schemas"), "--package-json", str(package_json)]) == 0
    assert "rewrote contributes.yamlValidation" in capsys.readouterr().out
    after = json.loads(package_json.read_text(encoding="utf-8"))
    assert after == {
        "name": "x",
        "contributes": {"commands": [{"command": "a"}], "yamlValidation": yaml_validation_entries()},
    }
    assert package_json.read_text(encoding="utf-8") == render(after)
    # A second run changes nothing and says so.
    assert main(["--output-dir", str(tmp_path / "schemas"), "--package-json", str(package_json)]) == 0
    assert "confirmed contributes.yamlValidation" in capsys.readouterr().out


def test_the_committed_package_json_round_trips_through_the_generators_writer(tmp_path: Path) -> None:
    """`write_yaml_validation` on the real manifest is a byte-identical no-op: the committed form is its output form."""
    committed = REPO_ROOT / "vscode_extension" / "package.json"
    copy = tmp_path / "package.json"
    shutil.copyfile(committed, copy)
    assert write_yaml_validation(copy) is False
    assert copy.read_bytes() == committed.read_bytes()


def test_main_creates_the_output_directory_and_overwrites_in_place(tmp_path: Path) -> None:
    output_dir = tmp_path / "nested" / "schemas"
    assert main(["--output-dir", str(output_dir)]) == 0
    (output_dir / "swc.schema.json").write_text("stale\n", encoding="utf-8")
    assert main(["--output-dir", str(output_dir)]) == 0
    _assert_written_byte_identically(output_dir)


def test_main_has_no_check_mode_and_requires_output_dir(tmp_path: Path) -> None:
    """The drift gate is the comparison; the generator only writes."""
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["--check", "--output-dir", str(tmp_path)])
    assert excinfo.value.code == 2
    assert not any(tmp_path.iterdir()), "a refused invocation writes nothing"


def test_the_module_form_runs_as_a_subprocess(tmp_path: Path) -> None:
    output_dir = tmp_path / "schemas"
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.editor_schema", "--output-dir", str(output_dir)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.stdout.startswith("wrote ")
    _assert_written_byte_identically(output_dir)


def test_the_requirement_schema_accepts_its_provenance_and_still_refuses_an_unknown_key(
    registry: ModelRegistry,
) -> None:
    """A requirement's `document`, `baseline` and `revision` are optional strings; an unknown key is still refused.

    An integer `revision` is refused (YAML's unquoted `7` is no string); `stub` is a boolean,
    as the strict loader reads it.
    """
    validator = Draft7Validator(layer_schema(registry.layer("requirements")))
    base = {"id": "REQ_1", "title": "T", "description": "D", "version": 1, "source": "external"}
    provenance = {"document": "SRS-Brakes", "baseline": "BL-7", "revision": "1.3"}
    assert validator.is_valid({**base, **provenance})
    assert validator.is_valid({**base, "document": None, "baseline": None, "revision": None})
    assert not validator.is_valid({**base, "revison": "1.3"})
    assert not validator.is_valid({**base, "revision": 7})
    assert validator.is_valid({**base, "stub": True})
    assert not validator.is_valid({**base, "stub": "yes"})
