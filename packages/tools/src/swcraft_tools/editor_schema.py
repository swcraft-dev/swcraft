"""JSON Schema per model layer, and the reference table, derived from the model contract.

A total consumer of the contract: one schema document per registry layer and one property per declared field,
read off :func:`~swcraft_tools.model_registry.current_registry`, never off the Pydantic classes. A contract
``type`` with no rendering (not in :data:`_SCALAR_TYPES`, not a ``list[...]``, ``map[...]`` or
``elementKind`` field) is refused with :class:`UnsupportedFieldType` rather than rendered as ``{}``, which
would accept anything.

The documents are read by Red Hat YAML through the extension's ``contributes.yamlValidation`` block:
``draft-07``, ``definitions`` rather than ``$defs``, a plain ``oneOf`` for a union layer and
``additionalProperties: false``. Output order is contract order and :func:`render` never sorts keys; the
product version appears nowhere in the output.

**What the schemas do not express**: ``FieldFacts.immutable`` (a protocol fact), ``reference``, ``relation``
and ``memberReference`` (cross-file facts, published in ``references.json`` instead), ``omit_at_default`` (a
writer serialization rule), and the AUTOSAR Identifier rule, whose scope is not a contract fact.

:func:`references_artifact` renders ``references.json``: every layer's storage, identity and discriminator
facts and its guide target, every reference, identity relation, collection and member reference, each with
the key paths that reach its field, so the extension navigates by key path and never parses a schema.
:func:`all_artifacts` is the schema set; :func:`editor_artifacts` is the whole file set ``main()`` writes.

**The manifest and the needs mapping are not derived from the contract**: :func:`manifest_schema` and
:func:`mapping_schema` post-process the Pydantic classes' ``model_json_schema()`` into the same dialect and
container (:func:`_pydantic_document`), so a field added to the class appears without a second declaration.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from swcraft_tools.guide_reference.model import kind_anchor, layer_section_page
from swcraft_tools.model_registry import (
    FieldFacts,
    KindFacts,
    LayerFacts,
    ModelRegistry,
    StorageRecord,
    current_registry,
)
from swcraft_tools.project import MANIFEST_RUNTIME_FIELDS, SwcraftProject
from swcraft_tools.requirements_mapping import NeedsMapping
from swcraft_tools.writer.diagrams.page import page_script_sha256

__all__ = [
    "CHECK_MODEL_REQUIRED_FIELDS",
    "DIAGRAM_PAGE_SCRIPT_ARTIFACT",
    "MANIFEST_ARTIFACT_PATH",
    "MANIFEST_FILE_MATCH",
    "MAPPING_ARTIFACT_PATH",
    "MAPPING_FILE_MATCH",
    "REFERENCES_ARTIFACT",
    "SCHEMA_DIALECT",
    "SCHEMA_ID_PREFIX",
    "UnreachableReference",
    "UnsupportedFieldType",
    "all_artifacts",
    "artifact_path",
    "diagram_page_script_artifact",
    "editor_artifacts",
    "field_schema",
    "layer_schema",
    "main",
    "manifest_schema",
    "mapping_schema",
    "references_artifact",
    "render",
    "write_yaml_validation",
    "yaml_validation_entries",
]

#: The dialect every emitted document declares; the YAML language server treats ``draft-07`` and ``2020-12``
#: identically, and ``jsonschema``'s ``Draft7Validator`` reads it.
SCHEMA_DIALECT = "http://json-schema.org/draft-07/schema#"

#: The ``$id`` root. A stable path and no version, on purpose.
SCHEMA_ID_PREFIX = "https://swcraft.dev/schemas/"

#: The container for member-only kinds. ``definitions`` is draft-07's spelling.
_DEFINITIONS = "definitions"

#: Pydantic's container, renamed to :data:`_DEFINITIONS` — together with every ``$ref`` into it.
_PYDANTIC_DEFINITIONS = "$defs"

#: The relative path of the manifest schema; the layer artifacts sit beside it.
MANIFEST_ARTIFACT_PATH = "manifest.schema.json"

#: The relative path of the needs mapping schema (:func:`mapping_schema`), beside the manifest's.
MAPPING_ARTIFACT_PATH = "requirements-mapping.schema.json"

#: The relative path of the reference table (:func:`references_artifact`). Not a
#: schema and not a ``yamlValidation`` target: the SWCraft extension reads it.
REFERENCES_ARTIFACT = "references.json"

#: The relative path of the diagram page script's hash (:func:`diagram_page_script_artifact`). Not a
#: schema and not a ``yamlValidation`` target: the SWCraft extension's diagrams tab reads it.
DIAGRAM_PAGE_SCRIPT_ARTIFACT = "diagram-page-script.json"

#: The key-path step for a collection member's index or map key in ``references.json``.
_MEMBER_STEP = "*"

#: The ``fileMatch`` for the manifest: a bare basename, which the language
#: server matches at any depth.
MANIFEST_FILE_MATCH = "swcraft.yaml"

#: The ``fileMatch`` for a needs mapping file: any file whose name ends in
#: ``needs-mapping.yaml``, at any depth. The ``*`` also matches nothing, so both
#: ``needs-mapping.yaml`` and ``team.needs-mapping.yaml`` are reached; a mapping
#: under another name is read by the import and gets no editor help.
MAPPING_FILE_MATCH = "**/*needs-mapping.yaml"

#: Where ``contributes.yamlValidation[].url`` points, relative to the extension
#: root -- the bundled client resolves a ``./`` url with ``joinPath(extensionUri, url)``.
_SCHEMA_URL_PREFIX = "./schemas/"

#: Fields the editor lists under ``required`` although the contract declares them ``required: false``: the
#: loader accepts a file without one, and ``check-model`` reports it through a cross-element rule. What it does
#: not reach: a blank value still satisfies the schema, and ``check-model`` reports that too.
CHECK_MODEL_REQUIRED_FIELDS: frozenset[str] = frozenset({"field:base_types.base_type.native_declaration"})

_MANIFEST_TITLE = "SWCraft project manifest"
_MANIFEST_DESCRIPTION = (
    "The `swcraft.yaml` at a project's root: its name, where the YAML model lives, where the "
    "generators write, the AUTOSAR release the ARXML targets and the SWCraft release the project "
    "is pinned to. Every path is relative to the directory holding this file."
)

_MAPPING_TITLE = "SWCraft needs mapping"
_MAPPING_DESCRIPTION = (
    "How a requirements team's Sphinx-Needs needs map to SWCraft's requirements, passed to "
    "`swcraft import requirements --mapping FILE`. It maps the team's need types and field names to "
    "SWCraft's; a key left out keeps SWCraft's own name. It is never a key of `swcraft.yaml`, and it "
    "lies outside the model root."
)

#: The four scalar spellings of the contract's type vocabulary; each is its own JSON Schema ``type``.
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})

#: The contract's one spelling whose shape another declaration decides: ``field:interfaces.data_element.init``
#: follows the element's own declared data type — a scalar, an enumeration literal's name, a map for a record,
#: a list for an array, composites free to nest (no depth is promised). JSON Schema cannot say "whatever that
#: type admits", so every JSON kind the value may take is named and conformance is left to ``swcraft
#: check-model``'s ``rule:interface.init-*`` rules, which depend on the other file the element's ``type``
#: names and on the interpreter's integer limit.
_VALUE_TYPE = "value"

#: The JSON kinds a ``value`` may take, in the order of ``schema.py``'s ``InitValue`` union. ``null`` comes
#: from the nullable widening in :func:`field_schema`.
_VALUE_JSON_TYPES: tuple[str, ...] = ("boolean", "integer", "number", "string", "array", "object")

#: The key order of one rendered property: prose, then the shape, then ``enum``, then ``default``.
#: Shape keys (``type``, ``items``, ``$ref``, ...) rank together and keep their relative order.
_PROPERTY_KEY_RANK = {"description": 0, "enum": 2, "default": 3}

_LIST_TYPE = re.compile(r"^list\[(?P<members>.+)\]$")
#: ``map[<key>, <value>]`` with the grammar's exact ``, `` separator — the contract's ``typeName``
#: pattern is byte-exact, so a re-spelling is refused here as it is there.
_MAP_TYPE = re.compile(r"^map\[(?P<key>[a-z][a-z0-9_]*), (?P<value>[a-z][a-z0-9_]*)\]$")


class UnsupportedFieldType(ValueError):
    """A contract ``type`` this generator has no JSON Schema rendering for; raised instead of emitting ``{}``."""

    def __init__(self, field_id: str, type_name: str) -> None:
        self.field_id = field_id
        self.type_name = type_name
        super().__init__(f"{field_id}: no JSON Schema rendering for contract type {type_name!r}")


class UnreachableReference(ValueError):
    """A row ``references.json`` cannot publish truthfully, with the reason.

    Raised for a ``reference``, ``relation``, ``memberReference`` or collection no key path reaches, for a walk
    that meets a member kind its layer does not declare, for a relation whose target field is not the target
    layer's identity field (navigation resolves by identity), and for a member reference's collection that no
    top-level path of its own kind reaches.
    """

    def __init__(self, field_id: str, reason: str) -> None:
        self.field_id = field_id
        self.reason = reason
        super().__init__(f"{field_id}: no reference-table row: {reason}")


def _resolve(registry: ModelRegistry | None) -> ModelRegistry:
    """The registry to generate from, resolved at call time and never at definition time."""
    return current_registry() if registry is None else registry


def _ref(kind: str) -> dict[str, str]:
    return {"$ref": f"#/{_DEFINITIONS}/{kind}"}


def _type_schema(field: FieldFacts) -> dict[str, Any]:
    """The shape part of one property — type, items, ``$ref`` — without its prose.

    A ``list[...]`` resolves its members through ``collection.member_kinds`` where that is non-empty, else
    through the declared ``type``; ``value`` (:data:`_VALUE_TYPE`) is the one branch returning a list of types.
    """
    if field.element_kind is not None:
        return _ref(field.element_kind)
    member_kinds = field.collection.member_kinds if field.collection is not None else ()
    if member_kinds:
        refs = [_ref(kind) for kind in member_kinds]
        items: dict[str, Any] = refs[0] if len(refs) == 1 else {"oneOf": refs}
        return {"type": "array", "items": items}
    if field.type in _SCALAR_TYPES:
        return {"type": field.type}
    if field.type == _VALUE_TYPE:
        return {"type": list(_VALUE_JSON_TYPES)}
    if (match := _LIST_TYPE.match(field.type)) is not None:
        members = match.group("members")
        if members in _SCALAR_TYPES:
            return {"type": "array", "items": {"type": members}}
        raise UnsupportedFieldType(field.id, field.type)
    if (match := _MAP_TYPE.match(field.type)) is not None:
        if match.group("key") == "string" and match.group("value") in _SCALAR_TYPES:
            return {"type": "object", "additionalProperties": {"type": match.group("value")}}
        raise UnsupportedFieldType(field.id, field.type)
    raise UnsupportedFieldType(field.id, field.type)


def field_schema(field: FieldFacts) -> dict[str, Any]:
    """One property, rendered from one :class:`FieldFacts`.

    Key order is fixed — ``description``, the shape, ``enum``, ``default`` — because the bytes are committed.
    A field defaulting to ``None`` becomes ``["<type>", "null"]`` with **no** ``default`` key, since
    ``"default": null`` would make completion insert a literal ``null``. A nullable ``$ref`` (no ``type`` to
    widen) becomes ``anyOf: [{$ref}, {"type": "null"}]``; any other shape with no type to widen is refused. A
    nullable field's ``enum`` gains ``null`` too, because a closed list would still reject the ``null`` the
    loader accepts.
    """
    schema: dict[str, Any] = {}
    if field.description:
        schema["description"] = field.description if field.unit is None else f"{field.description} (unit: {field.unit})"
    shape = _type_schema(field)
    nullable = field.has_default and field.default is None
    if nullable:
        declared = shape.get("type")
        if isinstance(declared, str):
            shape = {**shape, "type": [declared, "null"]}
        elif isinstance(declared, list):
            shape = {**shape, "type": [*declared, "null"]}
        elif "$ref" in shape:
            shape = {"anyOf": [dict(shape), {"type": "null"}]}
        else:
            raise UnsupportedFieldType(field.id, f"{field.type} (nullable: no type to widen)")
    schema.update(shape)
    if field.enum:
        schema["enum"] = [*field.enum, None] if nullable else list(field.enum)
    if field.has_default and field.default is not None:
        schema["default"] = field.default  # parsed JSON: already a JSON value
    return schema


def _discriminator_const(kind: KindFacts) -> str:
    """The value a union branch pins its discriminator to, read off that field's own ``enum``.

    Refuses an enum that is empty or has more than one member rather than inventing the value from
    ``KindFacts.kind``.
    """
    assert kind.discriminator_field is not None  # only called with pin_discriminator, i.e. for a union branch
    field = kind.get_field(kind.discriminator_field)
    if field is None:
        raise UnsupportedFieldType(kind.id, f"discriminator field {kind.discriminator_field!r} is not declared")
    if len(field.enum) != 1:
        raise UnsupportedFieldType(
            field.id, f"discriminator enum must have exactly one member, got {list(field.enum)!r}"
        )
    return str(field.enum[0])


def _object_schema(kind: KindFacts, *, pin_discriminator: bool, describe: bool = True) -> dict[str, Any]:
    """One kind as a closed JSON object: its fields as properties, in contract order.

    A ``placement`` field has no key in the file (the directory is the placement), so only
    :attr:`~swcraft_tools.model_registry.KindFacts.file_fields` are rendered. ``describe=False`` leaves the
    kind's prose out, for the single-kind document whose root carries the layer's description.
    """
    # Resolved first, so an undeclared discriminator is refused rather than silently skipped.
    const = _discriminator_const(kind) if pin_discriminator else None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in kind.file_fields:
        rendered = field_schema(field)
        pinned = const is not None and field.name == kind.discriminator_field
        if pinned:
            rendered.pop("enum", None)
            rendered["const"] = const
        properties[field.name] = rendered
        if field.required or pinned or field.id in CHECK_MODEL_REQUIRED_FIELDS:
            required.append(field.name)
    schema: dict[str, Any] = {}
    if describe and kind.description:
        schema["description"] = kind.description
    schema["type"] = "object"
    schema["additionalProperties"] = False
    schema["properties"] = properties
    if required:
        schema["required"] = required
    return schema


def _referenced_member_kinds(layer: LayerFacts) -> list[KindFacts]:
    """The member-only kinds reachable from the layer's top-level kinds, in contract order.

    Transitive; ordered by ``layer.kinds`` declaration, not discovery, so ``definitions`` reads like the contract.
    """
    by_kind = {kind.kind: kind for kind in layer.kinds}
    reachable: set[str] = set()
    frontier = list(layer.top_level_kinds)
    while frontier:
        kind = frontier.pop()
        for field in kind.fields:
            names = list(field.collection.member_kinds) if field.collection is not None else []
            if field.element_kind is not None:
                names.append(field.element_kind)
            for name in names:
                if name in reachable:
                    continue
                member = by_kind.get(name)
                if member is None:
                    raise UnsupportedFieldType(
                        field.id, f"member kind {name!r} is not declared on layer {layer.name!r}"
                    )
                reachable.add(name)
                frontier.append(member)
    return [kind for kind in layer.kinds if kind.member_only and kind.kind in reachable]


def layer_schema(layer: LayerFacts) -> dict[str, Any]:
    """The JSON Schema document for one layer.

    A single-kind layer renders the kind's object directly; a layer with more than one top-level kind renders
    ``oneOf`` with one branch per kind, each pinning its discriminator with ``const`` and requiring it.
    ``title`` is the layer's ``display_label``, ``$comment`` its contract prose, ``$id`` a stable versionless
    path. Member-only kinds the layer reaches go into ``definitions``.
    """
    document: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "$id": f"{SCHEMA_ID_PREFIX}{artifact_path(layer)}",
        "title": layer.display_label,
    }
    if layer.description:
        # `$comment`, never `description`: Red Hat YAML puts a root description into the CodeLens on every model
        # file's identity line, ahead of SWCraft's own lenses, and pushes them out of sight.
        document["$comment"] = layer.description
    top_level = layer.top_level_kinds
    if len(top_level) == 1:
        document.update(_object_schema(top_level[0], pin_discriminator=False, describe=False))
    else:
        document["oneOf"] = [_object_schema(kind, pin_discriminator=True) for kind in top_level]
    members = _referenced_member_kinds(layer)
    if members:
        document[_DEFINITIONS] = {kind.kind: _object_schema(kind, pin_discriminator=False) for kind in members}
    return document


def artifact_path(layer: LayerFacts) -> str:
    """The relative path one layer's artifact is written to, derived from ``layer.name``."""
    return f"{layer.name}.schema.json"


def _rewrite_refs(node: Any) -> Any:
    """Every ``$ref`` string pointing into Pydantic's ``$defs`` re-pointed at ``definitions``, recursively."""
    if isinstance(node, dict):
        return {
            key: (
                f"#/{_DEFINITIONS}/" + value[len(f"#/{_PYDANTIC_DEFINITIONS}/") :]
                if key == "$ref" and isinstance(value, str) and value.startswith(f"#/{_PYDANTIC_DEFINITIONS}/")
                else _rewrite_refs(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_rewrite_refs(item) for item in node]
    return node


def _manifest_property(owner: str, name: str, prop: dict[str, Any]) -> dict[str, Any]:
    """One Pydantic property in :func:`field_schema`'s shape and key order.

    Pydantic's per-property ``title`` is dropped. An ``Optional[...] = None`` field's
    ``anyOf: [{"type": T}, {"type": "null"}]`` with ``"default": null`` is collapsed to :func:`field_schema`'s
    ``["<type>", "null"]``-and-no-``default`` shape; a nullable whose other branch is not one scalar type is
    refused.
    """
    prop = {key: value for key, value in prop.items() if key != "title"}
    if "anyOf" in prop:
        branches = prop.pop("anyOf")
        scalar = [branch for branch in branches if branch.get("type") in _SCALAR_TYPES and len(branch) == 1]
        if len(branches) != 2 or len(scalar) != 1 or {"type": "null"} not in branches:
            raise UnsupportedFieldType(f"{owner}.{name}", f"anyOf {branches!r} (only scalar-or-null is rendered)")
        prop["type"] = [scalar[0]["type"], "null"]
        if prop.get("default", ...) is None:
            del prop["default"]
    return dict(sorted(prop.items(), key=lambda item: _PROPERTY_KEY_RANK.get(item[0], 1)))


def _close_object(owner: str, schema: dict[str, Any]) -> dict[str, Any]:
    """One Pydantic object schema in the layer documents' shape: closed, untitled, same key order.

    A definition of any other shape (an enum class, a scalar alias) would become an object rejecting every
    value, so it is refused.
    """
    if schema.get("type") != "object":
        raise UnsupportedFieldType(owner, f"{schema.get('type')!r} (only an object definition is rendered)")
    properties = {name: _manifest_property(owner, name, prop) for name, prop in schema.get("properties", {}).items()}
    closed: dict[str, Any] = {}
    if "description" in schema:
        closed["description"] = schema["description"]
    closed["type"] = "object"
    closed["additionalProperties"] = False
    closed["properties"] = properties
    if schema.get("required"):
        closed["required"] = list(schema["required"])
    return closed


def _pydantic_document(
    model: type[BaseModel], *, artifact: str, title: str, description: str, drop: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """One editor document post-processed from a Pydantic class's ``model_json_schema()``.

    The dialect set to :data:`SCHEMA_DIALECT`; ``$defs`` renamed to ``definitions`` with every ``$ref`` into
    it; ``additionalProperties: false`` on the object and every definition; ``$id``, ``title`` and
    ``description`` set (the class docstring is replaced); and every property in *drop* removed.
    """
    raw = _rewrite_refs(model.model_json_schema())
    for dropped in drop:
        raw["properties"].pop(dropped, None)
    if "required" in raw:
        # A dropped field is defaulted and never required; filtered anyway so a class change cannot leak it.
        raw["required"] = [name for name in raw["required"] if name not in drop]
    raw["description"] = description
    document: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "$id": f"{SCHEMA_ID_PREFIX}{artifact}",
        "title": title,
    }
    document.update(_close_object(model.__name__, raw))
    definitions = raw.get(_PYDANTIC_DEFINITIONS, {})
    if definitions:
        document[_DEFINITIONS] = {name: _close_object(name, member) for name, member in definitions.items()}
    return document


def manifest_schema(model: type[BaseModel] = SwcraftProject) -> dict[str, Any]:
    """The JSON Schema document for ``swcraft.yaml``, post-processed from Pydantic.

    :data:`~swcraft_tools.project.MANIFEST_RUNTIME_FIELDS` are dropped: the loader sets ``source``, and
    offering it in completion would invite writing a key the loader treats as internal.
    """
    return _pydantic_document(
        model,
        artifact=MANIFEST_ARTIFACT_PATH,
        title=_MANIFEST_TITLE,
        description=_MANIFEST_DESCRIPTION,
        drop=frozenset(MANIFEST_RUNTIME_FIELDS),
    )


def mapping_schema() -> dict[str, Any]:
    """The JSON Schema document for a needs mapping file, post-processed from Pydantic.

    The model is :class:`~swcraft_tools.requirements_mapping.NeedsMapping`. The rules the mapping reader alone
    holds (a duplicate key, a blank name, a key both read and ignored, the names a mapping may not use) are not
    in it.
    """
    return _pydantic_document(
        NeedsMapping, artifact=MAPPING_ARTIFACT_PATH, title=_MAPPING_TITLE, description=_MAPPING_DESCRIPTION
    )


def all_artifacts(*, registry: ModelRegistry | None = None) -> dict[str, str]:
    """Every rendered document, keyed by its relative path: the manifest first, then the layers, then the mapping."""
    resolved = _resolve(registry)
    artifacts = {MANIFEST_ARTIFACT_PATH: render(manifest_schema())}
    artifacts.update({artifact_path(layer): render(layer_schema(layer)) for layer in resolved.layers})
    artifacts[MAPPING_ARTIFACT_PATH] = render(mapping_schema())
    return artifacts


def diagram_page_script_artifact() -> str:
    """``diagram-page-script.json``: ``{"sha256": <P>}``, the diagram page script's CSP hash.

    The diagrams' VS Code tab allows the page's one executable script by this hash alone.
    """
    return json.dumps({"sha256": page_script_sha256()}) + "\n"


#: The artifacts of :func:`editor_artifacts` that are no schema, in the order ``main()``'s summary names them.
_NON_SCHEMA_ARTIFACTS = (REFERENCES_ARTIFACT, DIAGRAM_PAGE_SCRIPT_ARTIFACT)


def editor_artifacts(*, registry: ModelRegistry | None = None) -> dict[str, str]:
    """The whole file set ``main()`` writes.

    :func:`all_artifacts`, :data:`REFERENCES_ARTIFACT` and :data:`DIAGRAM_PAGE_SCRIPT_ARTIFACT`
    (:data:`_NON_SCHEMA_ARTIFACTS`).
    """
    resolved = _resolve(registry)
    artifacts = all_artifacts(registry=resolved)
    artifacts[REFERENCES_ARTIFACT] = references_artifact(registry=resolved)
    artifacts[DIAGRAM_PAGE_SCRIPT_ARTIFACT] = diagram_page_script_artifact()
    return artifacts


def _key_paths(layer: LayerFacts) -> dict[str, list[dict[str, Any]]]:
    """Every key path reaching a navigated field of *layer*, keyed by the field id.

    A field is navigated when it declares a ``reference``, a ``relation`` or a ``memberReference``, or is a
    ``collection``. A path runs from a top-level kind through ``collection.member_kinds`` (the field's name,
    then :data:`_MEMBER_STEP` for the member index or map key) and ``element_kind`` (the field's name) and ends
    at the field itself. A kind already on the current path is not entered again, so recursion ends the walk.
    """
    by_kind = {kind.kind: kind for kind in layer.kinds}
    paths: dict[str, list[dict[str, Any]]] = {}

    def walk(top: str, kind: KindFacts, prefix: list[str], on_path: frozenset[str]) -> None:
        for field in kind.fields:
            if (
                field.reference is not None
                or field.relation is not None
                or field.member_reference is not None
                or field.collection is not None
            ):
                paths.setdefault(field.id, []).append({"topKind": top, "path": [*prefix, field.name]})
            steps: list[tuple[str, list[str]]] = []
            if field.collection is not None:
                steps.extend((name, [field.name, _MEMBER_STEP]) for name in field.collection.member_kinds)
            if field.element_kind is not None:
                steps.append((field.element_kind, [field.name]))
            for name, step in steps:
                if name in on_path:
                    continue
                member = by_kind.get(name)
                if member is None:
                    raise UnreachableReference(
                        field.id, f"member kind {name!r} is not declared on layer {layer.name!r}"
                    )
                walk(top, member, [*prefix, *step], on_path | {name})

    for top in layer.top_level_kinds:
        walk(top.kind, top, [], frozenset({top.kind}))
    return paths


def _paths_of(paths: dict[str, list[dict[str, Any]]], field_id: str, what: str) -> list[dict[str, Any]]:
    """The walk's paths to *field_id*, refused with :class:`UnreachableReference` when there are none."""
    found = paths.get(field_id)
    if not found:
        raise UnreachableReference(field_id, f"no key path from a top-level kind reaches this {what}")
    return found


def _layer_entry(layer: LayerFacts) -> dict[str, Any]:
    """One layer's facts as ``references.json`` states them, read off :class:`LayerFacts`.

    ``nesting`` is :attr:`StorageRecord.nesting` verbatim — ``flat``, or the placement field a nesting layer
    groups by — so a reader tests it against ``flat``. ``discriminator`` and every ``discriminatorValue`` are
    ``null`` on a layer with one top-level kind; on a multi-kind layer the value is read off the discriminator
    field's own ``enum``. ``guidePage`` and each ``guideAnchor`` come from the functions that write the guide
    page.
    """
    top_level = layer.top_level_kinds
    multi = len(top_level) > 1
    return {
        "token": layer.token,
        "directory": layer.storage.directory,
        "nesting": layer.storage.nesting,
        "fileSuffix": layer.storage.file_suffix,
        "identityField": layer.identity_field,
        "filenameFrom": layer.storage.filename_from,
        "discriminator": top_level[0].discriminator_field if multi else None,
        "guidePage": layer_section_page(layer),
        "topLevelKinds": [
            {
                "kind": kind.kind,
                "discriminatorValue": _discriminator_const(kind) if multi else None,
                "guideAnchor": kind_anchor(kind),
            }
            for kind in top_level
        ],
    }


def references_artifact(*, registry: ModelRegistry | None = None) -> str:
    """The rendered ``references.json``: the layers' storage facts and every navigated field with its key paths.

    Every section follows the registry's order, each row's ``paths`` the walk order of :func:`_key_paths`. The
    document carries no version.

    Refused with :class:`UnreachableReference` rather than rendered: a row no key path reaches; a relation
    whose ``target_field`` is not the target layer's identity field (the extension resolves by identity, so the
    row would open the wrong element); and a member reference's collection not reached by exactly one path from
    its own kind as a top-level kind.
    """
    resolved = _resolve(registry)
    paths: dict[str, list[dict[str, Any]]] = {}
    for layer in resolved.layers:
        paths.update(_key_paths(layer))
    references = [
        {
            "field": edge.field_id,
            "sourceLayer": edge.source_layer,
            "sourceKind": edge.source_kind,
            "sourceField": edge.source_field,
            "targetLayer": edge.target_layer,
            "targetIdentityField": edge.target_identity_field,
            "paths": _paths_of(paths, edge.field_id, "reference"),
        }
        for edge in resolved.references()
    ]
    relations = []
    for edge in resolved.relations():
        identity = resolved.layer(edge.target_layer).identity_field
        if edge.target_field != identity:
            raise UnreachableReference(
                edge.field_id,
                f"relation target field {edge.target_field!r} is not the identity field {identity!r} "
                f"of layer {edge.target_layer!r}",
            )
        relations.append(
            {
                "field": edge.field_id,
                "sourceLayer": edge.source_layer,
                "targetLayer": edge.target_layer,
                "targetKind": edge.target_kind,
                "targetField": edge.target_field,
                "resolution": edge.resolution,
                "paths": _paths_of(paths, edge.field_id, "relation"),
            }
        )
    collections = [
        {
            "field": collection.field_id,
            "layer": collection.layer,
            "collectionKind": collection.collection_kind,
            "memberIdentity": collection.member_identity,
            "identityField": collection.identity_field,
            "identityFields": list(collection.identity_fields),
            "memberKinds": list(collection.member_kinds),
            "discriminatorField": collection.discriminator_field,
            "paths": _paths_of(paths, collection.field_id, "collection"),
        }
        for collection in resolved.collections()
    ]
    member_references = []
    for member_edge in resolved.member_references():
        targets = []
        for target in member_edge.collections:
            declared = resolved.kinds[target.kind].get_field(target.field)
            if declared is None:
                raise UnreachableReference(
                    member_edge.field_id, f"collection {target.collection_id!r} names no declared field"
                )
            owned = [found["path"] for found in paths.get(declared.id, []) if found["topKind"] == target.kind]
            if len(owned) != 1:
                raise UnreachableReference(
                    member_edge.field_id,
                    f"collection {target.collection_id!r} is reached by {len(owned)} paths from its own "
                    f"kind {target.kind!r}, not one",
                )
            targets.append(
                {
                    "id": target.collection_id,
                    "layer": target.layer,
                    "kind": target.kind,
                    "path": owned[0],
                    "identityField": target.identity_field,
                }
            )
        member_references.append(
            {
                "field": member_edge.field_id,
                "sourceLayer": member_edge.source_layer,
                "collections": targets,
                "via": member_edge.via,
                "paths": _paths_of(paths, member_edge.field_id, "member reference"),
            }
        )
    document = {
        "layers": {layer.name: _layer_entry(layer) for layer in resolved.layers},
        "references": references,
        "relations": relations,
        "collections": collections,
        "memberReferences": member_references,
    }
    return render(document)


def render(document: dict[str, Any]) -> str:
    """The committed text form: two-space indent, UTF-8 verbatim, one trailing newline, no key sorting."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _file_match(storage: StorageRecord) -> str:
    """One layer's ``fileMatch`` glob, rooted on the layer directory.

    ``**/<dir>/*<suffix>`` for a ``flat`` layer and ``**/<dir>/**/*<suffix>`` for a nesting one. Rooting on the
    directory makes the association independent of the manifest's ``model_root``.
    """
    nested = "**/" if storage.is_recursive else ""
    return f"**/{storage.directory}/{nested}*{storage.file_suffix}"


def yaml_validation_entries(*, registry: ModelRegistry | None = None) -> list[dict[str, Any]]:
    """The extension's ``contributes.yamlValidation`` block: the manifest, one entry per layer, the mapping.

    Each ``fileMatch`` derives from the layer's :class:`StorageRecord` and each ``url`` from
    :func:`artifact_path`, so the block and the artifact file names cannot disagree.
    """
    resolved = _resolve(registry)
    entries = [{"fileMatch": [MANIFEST_FILE_MATCH], "url": f"{_SCHEMA_URL_PREFIX}{MANIFEST_ARTIFACT_PATH}"}]
    entries.extend(
        {"fileMatch": [_file_match(layer.storage)], "url": f"{_SCHEMA_URL_PREFIX}{artifact_path(layer)}"}
        for layer in resolved.layers
    )
    entries.append({"fileMatch": [MAPPING_FILE_MATCH], "url": f"{_SCHEMA_URL_PREFIX}{MAPPING_ARTIFACT_PATH}"})
    return entries


def write_yaml_validation(package_json: Path) -> bool:
    """Replace ``contributes.yamlValidation`` in *package_json* with :func:`yaml_validation_entries`.

    Parsed, the one key replaced, and written back through :func:`render`, the form the committed
    ``package.json`` has, so a rewrite that changes nothing is byte-identical. Returns whether the block changed.
    """
    manifest = json.loads(package_json.read_text(encoding="utf-8"))
    entries = yaml_validation_entries()
    changed = manifest.setdefault("contributes", {}).get("yamlValidation") != entries
    manifest["contributes"]["yamlValidation"] = entries
    # `newline="\n"`: the file is committed, and a translating host would produce a diff that exists only there.
    package_json.write_text(render(manifest), encoding="utf-8", newline="\n")
    return changed


def _is_generator_owned(path: Path) -> bool:
    """Whether *path* is this generator's: a JSON object whose ``$id`` starts with :data:`SCHEMA_ID_PREFIX`.

    The prune in :func:`main` deletes nothing else.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return (
        isinstance(document, dict)
        and isinstance(document.get("$id"), str)
        and document["$id"].startswith(SCHEMA_ID_PREFIX)
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m swcraft_tools.editor_schema --output-dir DIR [--package-json FILE]``.

    Writes every artifact of :func:`editor_artifacts` into ``DIR`` and, when ``--package-json`` names the
    extension manifest, rewrites its ``contributes.yamlValidation`` block from the same registry.

    A ``*.schema.json`` already in ``DIR`` that no artifact produces is removed only when
    :func:`_is_generator_owned` says the generator wrote it; any other one (a hand-authored schema, a file that
    is not JSON) is left alone and named in the summary, since ``--output-dir`` is user-supplied. There is no
    ``--check`` mode: the drift test does the byte comparison.
    """
    parser = argparse.ArgumentParser(
        prog="python -m swcraft_tools.editor_schema",
        description=(
            "Generate the editor JSON Schemas (one per model layer, plus the manifest and the needs mapping) "
            "and the reference table references.json from the model contract."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="directory the *.schema.json files and references.json are written into (created when absent)",
    )
    parser.add_argument(
        "--package-json",
        type=Path,
        default=None,
        help="the extension's package.json whose contributes.yamlValidation block is rewritten (optional)",
    )
    args = parser.parse_args(argv)
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = editor_artifacts()
    schema_count = sum(1 for relative in artifacts if relative not in _NON_SCHEMA_ARTIFACTS)
    for relative, text in artifacts.items():
        # `newline="\n"`: these files are committed and compared byte for byte, on Windows too.
        (output_dir / relative).write_text(text, encoding="utf-8", newline="\n")
    unknown = sorted(
        (path for path in output_dir.glob("*.schema.json") if path.name not in artifacts),
        key=lambda path: path.as_posix(),
    )
    stale = [path for path in unknown if _is_generator_owned(path)]
    foreign = [path for path in unknown if path not in stale]
    for path in stale:
        path.unlink()
    summary = (
        f"wrote {schema_count} schema files, {' and '.join(_NON_SCHEMA_ARTIFACTS)} to {output_dir}, "
        f"removed {len(stale)} stale"
    )
    if foreign:
        summary += f", left {len(foreign)} foreign (not generator output): {', '.join(path.name for path in foreign)}"
    if args.package_json is not None:
        verb = "rewrote" if write_yaml_validation(args.package_json) else "confirmed"
        summary += f"; {verb} contributes.yamlValidation in {args.package_json}"
    print(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through a subprocess in the tests
    raise SystemExit(main())
