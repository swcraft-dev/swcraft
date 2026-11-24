"""The model contract's artifact-shape gate: is each artifact well formed.

Whether the contract and ``schema.py`` describe the same model is
``test_model_contract_conformance.py``'s question. Here: the meta-schema, the
contract's own declarations, the binding, the coverage shapes and the
migration inventory.

The perturbations are table-driven and per-``$defs``: a schema is trivially
satisfiable unless each shape is shown to accept one fragment and refuse
another, and one shape at a time makes a failure name the shape rather than
the document. The fragments are transcribed from the real model (the ``swc``
placement, ``Port``'s membership, the ``ports`` collection, a ``connector.*``
rule) so the schema cannot drift away from the real declaration.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
from collections import Counter
from typing import Any

import pytest
import validator_package
from conftest import REPO_ROOT
from guide_fixtures import vocabulary_hits
from jsonschema import Draft202012Validator
from model_contract_fixtures import (
    ARXML_BOUND_LAYER_NAMES,
    CONTRACT_PACKAGE_ROOT,
    GRANULARITIES,
    SUPPORTED_CONTRACT_MAJORS,
    UnsupportedContractVersion,
    binding_shape_validator,
    contract_path,
    expand_entries,
    fragment_validator,
    load_binding,
    load_binding_schema,
    load_contract,
    load_meta_schema,
    meta_schema_validator,
    require_supported_contract_version,
    validation_messages,
)
from pydantic import BaseModel
from swcraft_tools.differ import _layer_renders
from swcraft_tools.model_registry import production_registry
from swcraft_tools.validator import CONSISTENCY_RULES

# ---------------------------------------------------------------------------
# The declaration shapes the contract must factor out, and the fragments that exercise them
# ---------------------------------------------------------------------------

#: Every declaration shape `contract.schema.json` must factor out as a named
#: `$defs`; listed rather than derived so deleting one fails this module.
REQUIRED_SHAPES = (
    "layerDecl",
    "kindDecl",
    "fieldDecl",
    "collectionDecl",
    "referenceDecl",
    "relationDecl",
    "memberReferenceDecl",
    "storageDecl",
    "placementDecl",
    "ruleDecl",
    "retiredStorageDecl",
)

#: The identifier patterns the same file must declare, for the same reason.
REQUIRED_IDENTIFIER_SHAPES = ("layerId", "kindId", "fieldId", "collectionId", "ruleId")

_SWC_STORAGE: dict[str, Any] = {
    "directory": "swc",
    "nesting": "group",
    "fileSuffix": ".yaml",
    "filenameFrom": "name",
}

_SWC_PLACEMENT: dict[str, Any] = {
    "kind": "swc",
    "field": "group",
    "type": "string",
    "description": "Placement metadata: the value is the element's directory rather than a key in its file.",
}

#: The relation `field:hardware.memory.accessible` declares, transcribed. A
#: relation differs from a reference by the SOURCE's shape (keys of a map or
#: members of a list); perturbed below into the shapes the meta-schema refuses.
_ACCESSIBLE_RELATION: dict[str, Any] = {
    "layer": "hardware",
    "kind": "core",
    "field": "name",
    "resolution": "list-values",
    "direction": "target-to-source",
    "summary": "Each member of a memory's accessible list names a core by name.",
}

#: The member reference `field:swc.data_write.element` declares, transcribed:
#: the `via` form, the one that reaches another element.
_ELEMENT_MEMBER_REFERENCE: dict[str, Any] = {
    "collections": [
        "collection:interfaces.sender_receiver.data_elements",
        "collection:interfaces.external_interface.data_elements",
    ],
    "via": "port",
    "summary": "Names a data element of the interface of the port `port` names.",
}

_PORTS_COLLECTION: dict[str, Any] = {
    "kind": "list",
    "memberIdentity": "field",
    "memberKinds": ["port"],
    "identityField": "name",
}

#: One fragment per shape the meta-schema must ACCEPT: a schema that rejects
#: everything would pass `PERTURBATIONS` on its own.
VALID_FRAGMENTS: dict[str, Any] = {
    "layerDecl": {
        "id": "layer:swc",
        "name": "swc",
        "optional": False,
        "identityField": "name",
        "storage": _SWC_STORAGE,
        "placement": _SWC_PLACEMENT,
        "description": "Software components. Stored one file per element.",
        "kinds": [],
    },
    "kindDecl": {
        "id": "kind:swc.port",
        "kind": "port",
        "memberOnly": True,
        "memberOf": [{"kind": "swc", "field": "ports"}],
        "description": "One port of a software component.",
        "fields": [],
    },
    "fieldDecl": {
        "id": "field:swc.swc.ports",
        "name": "ports",
        "type": "list[port]",
        "required": False,
        "immutable": False,
        "default": [],
        "description": "The component's ports.",
        "collection": _PORTS_COLLECTION,
    },
    "collectionDecl": _PORTS_COLLECTION,
    "referenceDecl": {"layer": "interfaces", "identityField": "name"},
    "relationDecl": _ACCESSIBLE_RELATION,
    "memberReferenceDecl": _ELEMENT_MEMBER_REFERENCE,
    "storageDecl": _SWC_STORAGE,
    "placementDecl": _SWC_PLACEMENT,
    "ruleDecl": {
        "id": "rule:connector.from-not-provide",
        "family": "cross-element",
        "appliesTo": ["layer:connectors"],
        "summary": "A connector's `from` endpoint must name a providing port.",
    },
    "retiredStorageDecl": {
        "directory": "data_types",
        "since": "8.0.0",
        "movedTo": ["data_types/application", "data_types/implementation", "data_types/base"],
    },
}

#: A whole contract document the meta-schema must accept, the base the
#: top-level perturbations are cut from: perturbing the real declaration would
#: make each failure depend on fifteen layers of unrelated content.
MINIMAL_CONTRACT: dict[str, Any] = {
    "contractVersion": "1.0.0",
    "swcraftVersion": "0.2.0",
    "layers": [VALID_FRAGMENTS["layerDecl"]],
    "rules": [VALID_FRAGMENTS["ruleDecl"]],
}


def _without(fragment: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in fragment.items() if name != key}


def _with(fragment: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {**fragment, **overrides}


#: ``(shape, case, fragment, expected message substring)``: a declaration the
#: meta-schema must refuse, and the reason — a rejection for an unrelated
#: reason proves nothing about the rule.
PERTURBATIONS: tuple[tuple[str, str, Any, str], ...] = (
    (
        "layerDecl",
        "missing-identity-field",
        _without(VALID_FRAGMENTS["layerDecl"], "identityField"),
        "identityField",
    ),
    (
        # The contract's structural fields may not name Python symbols;
        # `additionalProperties: false` makes a smuggled one a rejection.
        "layerDecl",
        "python-class-smuggled-in",
        _with(VALID_FRAGMENTS["layerDecl"], pythonClass="SWComponent"),
        "pythonClass",
    ),
    (
        "layerDecl",
        "identifier-without-prefix",
        _with(VALID_FRAGMENTS["layerDecl"], id="swc"),
        "layer:",
    ),
    (
        "kindDecl",
        "member-only-not-a-boolean",
        _with(VALID_FRAGMENTS["kindDecl"], memberOnly="true"),
        "boolean",
    ),
    (
        "kindDecl",
        "member-of-entry-with-extra-key",
        _with(VALID_FRAGMENTS["kindDecl"], memberOf=[{"kind": "swc", "field": "ports", "index": 0}]),
        "index",
    ),
    (
        # `list[Port]` is the Python spelling; only the published `list[port]`
        # is shipped in the `discover` vectors.
        "fieldDecl",
        "python-type-spelling",
        _with(VALID_FRAGMENTS["fieldDecl"], type="list[Port]"),
        "type",
    ),
    (
        "fieldDecl",
        "missing-immutable",
        _without(VALID_FRAGMENTS["fieldDecl"], "immutable"),
        "immutable",
    ),
    (
        "fieldDecl",
        "empty-enum",
        _with(VALID_FRAGMENTS["fieldDecl"], enum=[]),
        "enum",
    ),
    (
        "collectionDecl",
        "field-identity-without-identity-field",
        _without(_PORTS_COLLECTION, "identityField"),
        "identityField",
    ),
    (
        "collectionDecl",
        "whole-value-with-identity-field",
        _with(_PORTS_COLLECTION, memberIdentity="whole_value"),
        "identityField",
    ),
    (
        "referenceDecl",
        "reference-naming-a-python-class",
        _with(VALID_FRAGMENTS["referenceDecl"], **{"class": "Interface"}),
        "class",
    ),
    (
        "referenceDecl",
        "missing-identity-field",
        _without(VALID_FRAGMENTS["referenceDecl"], "identityField"),
        "identityField",
    ),
    (
        # Each resolution is realized by one bound callable; the closed enum
        # refuses a misspelling instead of leaving it unbound until traversal.
        "relationDecl",
        "unknown-resolution",
        _with(_ACCESSIBLE_RELATION, resolution="fieldValue"),
        "resolution",
    ),
    (
        # Without the target field there is nothing to match a resolved key
        # against, and `identityField` would be silently assumed.
        "relationDecl",
        "missing-target-field",
        _without(_ACCESSIBLE_RELATION, "field"),
        "field",
    ),
    (
        "relationDecl",
        "direction-the-traversal-does-not-build",
        _with(_ACCESSIBLE_RELATION, direction="source-to-target"),
        "direction",
    ),
    (
        # A field resolved both ways would name two things; the exclusion is
        # `fieldDecl`'s negated pairs, so the rejection is the field's.
        "fieldDecl",
        "reference-and-relation-at-once",
        _with(
            VALID_FRAGMENTS["fieldDecl"],
            reference=VALID_FRAGMENTS["referenceDecl"],
            relation=_ACCESSIBLE_RELATION,
        ),
        "relation",
    ),
    (
        # A member reference names a member, a reference an element; one
        # field answering both ways would name two things.
        "fieldDecl",
        "reference-and-member-reference-at-once",
        _with(
            VALID_FRAGMENTS["fieldDecl"],
            reference=VALID_FRAGMENTS["referenceDecl"],
            memberReference=_ELEMENT_MEMBER_REFERENCE,
        ),
        "memberReference",
    ),
    (
        "fieldDecl",
        "relation-and-member-reference-at-once",
        _with(
            VALID_FRAGMENTS["fieldDecl"],
            relation=_ACCESSIBLE_RELATION,
            memberReference=_ELEMENT_MEMBER_REFERENCE,
        ),
        "memberReference",
    ),
    (
        # The collections are the whole declaration: a member reference listing
        # none would name a member of nothing.
        "memberReferenceDecl",
        "no-collection",
        _with(_ELEMENT_MEMBER_REFERENCE, collections=[]),
        "collections",
    ),
    (
        "memberReferenceDecl",
        "collection-without-its-prefix",
        _with(_ELEMENT_MEMBER_REFERENCE, collections=["interfaces.sender_receiver.data_elements"]),
        "collection:",
    ),
    (
        "memberReferenceDecl",
        "missing-summary",
        _without(_ELEMENT_MEMBER_REFERENCE, "summary"),
        "summary",
    ),
    (
        "storageDecl",
        "file-suffix-without-leading-dot",
        _with(_SWC_STORAGE, fileSuffix="yaml"),
        "fileSuffix",
    ),
    (
        # A directory is one or two segments: `data_types/base` is a
        # directory, `model/swc/sensors` is a storage path.
        "storageDecl",
        "storage-path-instead-of-directory",
        _with(_SWC_STORAGE, directory="model/swc/sensors"),
        "directory",
    ),
    (
        "storageDecl",
        "directory-with-a-trailing-separator",
        _with(_SWC_STORAGE, directory="data_types/"),
        "directory",
    ),
    (
        "storageDecl",
        "directory-with-an-empty-segment",
        _with(_SWC_STORAGE, directory="data_types//base"),
        "directory",
    ),
    (
        "storageDecl",
        "directory-rooted-at-the-filesystem",
        _with(_SWC_STORAGE, directory="/data_types"),
        "directory",
    ),
    (
        "storageDecl",
        "directory-segment-not-a-name",
        _with(_SWC_STORAGE, directory="data_types/Base"),
        "directory",
    ),
    (
        "placementDecl",
        "missing-kind",
        _without(_SWC_PLACEMENT, "kind"),
        "kind",
    ),
    (
        "placementDecl",
        "empty-description",
        _with(_SWC_PLACEMENT, description=""),
        "description",
    ),
    (
        # A misspelled family would silently join no family at all.
        "ruleDecl",
        "unknown-family",
        _with(VALID_FRAGMENTS["ruleDecl"], family="cross_element"),
        "family",
    ),
    (
        "ruleDecl",
        "identifier-without-rule-prefix",
        _with(VALID_FRAGMENTS["ruleDecl"], id="connector.from-not-provide"),
        "rule:",
    ),
    (
        "ruleDecl",
        "applies-to-nothing",
        _with(VALID_FRAGMENTS["ruleDecl"], appliesTo=[]),
        "appliesTo",
    ),
    (
        "ruleDecl",
        "applies-to-an-unprefixed-entry",
        _with(VALID_FRAGMENTS["ruleDecl"], appliesTo=["connectors"]),
        "appliesTo",
    ),
    (
        "retiredStorageDecl",
        "missing-directory",
        _without(VALID_FRAGMENTS["retiredStorageDecl"], "directory"),
        "directory",
    ),
    (
        "retiredStorageDecl",
        "missing-since",
        _without(VALID_FRAGMENTS["retiredStorageDecl"], "since"),
        "since",
    ),
    (
        "retiredStorageDecl",
        "missing-moved-to",
        _without(VALID_FRAGMENTS["retiredStorageDecl"], "movedTo"),
        "movedTo",
    ),
    (
        # An entry that names nowhere its files went could only refuse a model
        # without saying where the files belong.
        "retiredStorageDecl",
        "moved-to-nowhere",
        _with(VALID_FRAGMENTS["retiredStorageDecl"], movedTo=[]),
        "movedTo",
    ),
)


# ---------------------------------------------------------------------------
# The meta-schema itself
# ---------------------------------------------------------------------------


def test_meta_schema_is_a_valid_json_schema() -> None:
    """`contract.schema.json` is a valid Draft 2020-12 schema (a misplaced keyword is a silent annotation)."""
    schema = load_meta_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/contract.schema.json"), schema["$id"]


def test_meta_schema_declares_every_required_shape() -> None:
    """Every declaration shape and identifier pattern is a named `$defs`, so a perturbation can target one."""
    schema = load_meta_schema()
    definitions = schema["$defs"]
    missing = [name for name in REQUIRED_SHAPES + REQUIRED_IDENTIFIER_SHAPES if name not in definitions]
    assert not missing, f"contract.schema.json is missing $defs: {missing}"
    assert object_shape_defs(schema) == set(REQUIRED_SHAPES), (
        "the meta-schema's object shapes and REQUIRED_SHAPES have diverged; a new declaration shape needs "
        "a perturbation row and a place in the tuple, not only a `$defs`"
    )


def object_shape_defs(schema: dict[str, Any]) -> set[str]:
    """The names of every `$defs` entry that declares an OBJECT shape.

    Derived from the file: the hand-written `REQUIRED_*_SHAPES` tuples prove
    only that no shape was deleted, not that a new one was covered.
    `"properties" in node` as well as `type == "object"`, because Draft 2020-12
    does not require `type` and a shape without it would escape every sweep.
    """
    return {name for name, node in schema["$defs"].items() if node.get("type") == "object" or "properties" in node}


def _walk_schemas(node: Any) -> list[Any]:
    """Every subschema of the meta-schema, the root included."""
    found: list[Any] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_walk_schemas(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_schemas(item))
    return found


def _external_refs(schema: Any) -> list[str]:
    """Every `$ref` of *schema* that does not point into the file's own `$defs`.

    One strict filter for every schema of the set: a `$ref` into a schema's
    root or a non-`$defs` pointer is as much a resolver requirement as a
    relative file reference.
    """
    return [
        str(node["$ref"])
        for node in _walk_schemas(schema)
        if "$ref" in node and not str(node["$ref"]).startswith("#/$defs/")
    ]


#: The two normative schemas of the contract set, each with the name its failures use. The two
#: evidence schemas (``coverage.schema.json``, ``migration-inventory.schema.json``) are closed records:
#: the closed half of this check runs the same check over them.
_SCHEMAS_OF_THE_SET: tuple[tuple[str, Any], ...] = (
    ("contract.schema.json", load_meta_schema),
    ("binding.schema.json", load_binding_schema),
)


#: The identifier granularities every schema of the set spells for itself:
#: the files are self-contained (no cross-file `$ref`), so the copies must be
#: kept EQUAL by a test.
_SHARED_IDENTIFIER_SHAPES = ("layerId", "kindId", "fieldId", "ruleId")


def _pattern_body(pattern: str) -> str:
    """A `^...$`-anchored pattern with its anchors removed, for composition."""
    assert pattern.startswith("^") and pattern.endswith("$"), pattern
    return pattern[1:-1]


def test_the_identifier_grammar_is_written_four_times_and_says_the_same_thing() -> None:
    """The identifier grammar, compared across the normative files that restate it (guards copies that diverged)."""
    contract = load_meta_schema()["$defs"]
    binding = load_binding_schema()["$defs"]
    for name in _SHARED_IDENTIFIER_SHAPES:
        assert binding[name]["pattern"] == contract[name]["pattern"], (
            f"binding.schema.json's {name} pattern has drifted from contract.schema.json's"
        )

    # The two evidence schemas' `entryIdentifier` alternations are compared by
    # this check's closed half.


@pytest.mark.parametrize(
    ("case", "ref", "external"),
    (
        ("own-defs", "#/$defs/layerId", False),
        ("document-root", "#", True),
        ("non-defs-pointer", "#/notdefs/x", True),
        ("relative-file", "coverage.schema.json#/$defs/entryIdentifier", True),
        ("absolute-file", "https://example.invalid/schema.json", True),
    ),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_cross_file_ref_filter_bites(case: str, ref: str, external: bool) -> None:
    """The filter accepts and refuses, `#/notdefs/x` included, without perturbing a shipped file."""
    found = _external_refs({"$defs": {"x": {"$ref": ref}}})
    assert found == ([ref] if external else []), case


@pytest.mark.parametrize(("label", "loader"), _SCHEMAS_OF_THE_SET, ids=[label for label, _ in _SCHEMAS_OF_THE_SET])
def test_no_schema_of_the_set_declares_a_cross_file_ref(label: str, loader: Any) -> None:
    """Every `$ref` of the set's schemas is internal: a plain `jsonschema` validator must suffice, no resolver."""
    external = _external_refs(loader())
    assert external == [], f"cross-file or non-$defs $ref in {label}: {external}"


def test_every_object_shape_forbids_additional_properties() -> None:
    """No declaration shape admits an undeclared key, such as a smuggled `pythonClass`."""
    schema = load_meta_schema()
    offenders = [
        sorted(node.get("properties", {}))
        for node in _walk_schemas(schema)
        if node.get("type") == "object" and "properties" in node and node.get("additionalProperties") is not False
    ]
    assert not offenders, f"object shapes without `additionalProperties: false`: {offenders}"
    # The walk above needs an explicit `"type": "object"`, which Draft 2020-12
    # does not require (the `if` conditionals carry `properties` without it);
    # every NAMED shape is checked by name as well, which cannot be dodged.
    named = [
        name for name in object_shape_defs(schema) if schema["$defs"][name].get("additionalProperties") is not False
    ]
    assert not named, f"$defs shapes without `additionalProperties: false`: {named}"


def test_the_canonical_contract_directory_holds_no_python() -> None:
    """The package directory is inert data shipped in the wheel, with no Python module beside it."""
    python_files = sorted(path.name for path in CONTRACT_PACKAGE_ROOT.iterdir() if path.suffix == ".py")
    assert python_files == [], python_files
    assert contract_path("contract.schema.json").is_file()


# ---------------------------------------------------------------------------
# The document shape
# ---------------------------------------------------------------------------


def test_a_minimal_contract_document_validates() -> None:
    """The top-level shape is satisfiable — the other half of the rejections below."""
    assert validation_messages(meta_schema_validator(), MINIMAL_CONTRACT) == []


@pytest.mark.parametrize(
    ("case", "document", "expected"),
    (
        ("unknown-top-level-key", {**MINIMAL_CONTRACT, "pythonPackage": "swcraft_tools"}, "pythonPackage"),
        ("missing-contract-version", _without(MINIMAL_CONTRACT, "contractVersion"), "contractVersion"),
        ("missing-swcraft-version", _without(MINIMAL_CONTRACT, "swcraftVersion"), "swcraftVersion"),
        ("non-semantic-contract-version", {**MINIMAL_CONTRACT, "contractVersion": "1"}, "contractVersion"),
        ("no-layers", {**MINIMAL_CONTRACT, "layers": []}, "layers"),
    ),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_malformed_contract_document_is_rejected(case: str, document: Any, expected: str) -> None:
    """A document the meta-schema must refuse, for the reason the case names."""
    messages = validation_messages(meta_schema_validator(), document)
    assert messages, f"{case}: the meta-schema accepted a malformed document"
    assert any(expected in message for message in messages), f"{case}: rejected for the wrong reason: {messages}"


# ---------------------------------------------------------------------------
# One perturbation per declaration shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", REQUIRED_SHAPES)
def test_a_well_formed_fragment_is_accepted(shape: str) -> None:
    """Each declaration shape accepts a fragment transcribed from the real model."""
    assert validation_messages(fragment_validator(shape), VALID_FRAGMENTS[shape]) == []


@pytest.mark.parametrize(
    ("shape", "case", "fragment", "expected"),
    PERTURBATIONS,
    ids=[f"{shape}-{case}" for shape, case, _, _ in PERTURBATIONS],
)
def test_a_perturbed_fragment_is_rejected(shape: str, case: str, fragment: Any, expected: str) -> None:
    """The gate bites: each shape refuses the declaration its rule forbids."""
    messages = validation_messages(fragment_validator(shape), fragment)
    assert messages, f"{shape}/{case}: the meta-schema accepted a malformed fragment"
    assert any(expected in message for message in messages), (
        f"{shape}/{case}: rejected for the wrong reason: {messages}"
    )


@pytest.mark.parametrize("directory", ["swc", "data_types/base", "data_types/mapping_sets"])
def test_a_directory_of_one_or_two_segments_is_accepted(directory: str) -> None:
    """A layer's directory of one or two `name` segments is accepted, the other half of the refusals above."""
    assert validation_messages(fragment_validator("storageDecl"), _with(_SWC_STORAGE, directory=directory)) == []


def test_every_declaration_shape_has_a_perturbation() -> None:
    """Every declared shape has a perturbation proving it rejects."""
    covered = {shape for shape, _, _, _ in PERTURBATIONS}
    assert covered == set(REQUIRED_SHAPES), f"shapes with no perturbation: {sorted(set(REQUIRED_SHAPES) - covered)}"


def test_fragment_validator_refuses_an_unknown_shape() -> None:
    """The loader fails loudly on an undeclared shape, so a renamed `$defs` cannot validate against nothing."""
    with pytest.raises(KeyError, match="collectionDeclaration"):
        fragment_validator("collectionDeclaration")


# ---------------------------------------------------------------------------
# The shipped declaration: layers, storage, placement, optionality, kinds
# ---------------------------------------------------------------------------

#: The layers the model has, in the order the contract's FILE publishes them
#: (`displayOrder` is a separate declaration, asserted apart). Restated rather
#: than read off the contract, because a check comparing the file with itself
#: would pass a contract that had lost a layer.
#:
#: **What this does not reach:** the documented layer list in prose. Nothing
#: parses it, so the two are kept in step by hand.
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
    # The three layers of the forms a type names by reference, written last.
    "units",
    "compu_methods",
    "data_constraints",
)

#: The one layer that nests, checked as a count rather than a membership so
#: that a *second* one fails rather than joining quietly.
NESTING_LAYER = "swc"


def _layers() -> list[dict[str, Any]]:
    return load_contract()["layers"]


def _kinds(layer: dict[str, Any]) -> list[dict[str, Any]]:
    return layer["kinds"]


def test_the_shipped_contract_validates_against_the_meta_schema() -> None:
    """`model-contract.json` is a legal contract document, the shape every later check assumes."""
    assert validation_messages(meta_schema_validator(), load_contract()) == []


def test_the_contract_declares_the_twelve_layers_in_claude_md_order() -> None:
    """The layer list is the documented one, in order: a re-ordering changes published output."""
    assert tuple(layer["name"] for layer in _layers()) == EXPECTED_LAYERS


def test_the_contract_records_the_product_version_it_was_transcribed_from() -> None:
    """`swcraftVersion` quotes the product version literally (`contractVersion` is outside the lockstep)."""
    from swcraft_tools._version import __version__

    assert load_contract()["swcraftVersion"] == __version__


def test_layer_identifiers_are_unique_and_derive_from_the_layer_name() -> None:
    """`layer:<name>`, once per layer: identifiers are the contract's public surface."""
    identifiers = [layer["id"] for layer in _layers()]
    assert identifiers == [f"layer:{layer['name']}" for layer in _layers()]
    assert len(set(identifiers)) == len(identifiers), f"duplicate layer identifiers: {identifiers}"


def test_kind_identifiers_are_unique_and_derive_from_layer_and_kind() -> None:
    """`kind:<layer>.<kind>`, once across the whole contract."""
    identifiers = [kind["id"] for layer in _layers() for kind in _kinds(layer)]
    expected = [f"kind:{layer['name']}.{kind['kind']}" for layer in _layers() for kind in _kinds(layer)]
    assert identifiers == expected
    assert len(set(identifiers)) == len(identifiers), f"duplicate kind identifiers: {identifiers}"


def test_the_collection_identifier_pattern_matches_the_identifiers_it_is_for() -> None:
    """`collectionId` matches the identifiers the declared collections would have, and refuses malformed ones.

    No `collectionDecl` carries an `id`, so the pattern is checked over the
    derived identifiers. The pinned count is the number of declared collection
    fields; a collection added to or removed from the contract moves it.
    """
    pattern = re.compile(load_meta_schema()["$defs"]["collectionId"]["pattern"])
    identifiers = [
        f"collection:{layer['name']}.{kind['kind']}.{field['name']}"
        for layer in _layers()
        for kind in _kinds(layer)
        for field in kind["fields"]
        if "collection" in field
    ]
    assert len(identifiers) == 47, identifiers
    for identifier in identifiers:
        assert pattern.fullmatch(identifier), f"collectionId does not match {identifier}"
    for rejected in ("collection:swc.swc", "swc.swc.ports", "collection:SWC.swc.ports", "collection:swc.swc.ports.x"):
        assert not pattern.fullmatch(rejected), f"collectionId wrongly accepts {rejected}"


def test_field_identifiers_are_unique_and_derive_from_layer_kind_and_alias() -> None:
    """`field:<layer>.<kind>.<alias>`, once across the whole contract.

    Other gates build their entry set *from* the declared `id` values, so a
    mis-composed field identifier would be self-consistent there; identifiers
    are public surface and a rename is a breaking change.
    """
    identifiers = [field["id"] for layer in _layers() for kind in _kinds(layer) for field in kind["fields"]]
    expected = [
        f"field:{layer['name']}.{kind['kind']}.{field['name']}"
        for layer in _layers()
        for kind in _kinds(layer)
        for field in kind["fields"]
    ]
    assert identifiers == expected
    assert len(set(identifiers)) == len(identifiers), (
        f"duplicate field identifiers: {sorted({i for i in identifiers if identifiers.count(i) > 1})}"
    )


def test_no_layer_is_optional() -> None:
    """Every declared layer is one a project must have; a layer declaring `optional: true` fails here."""
    optional = [layer["name"] for layer in _layers() if layer["optional"]]
    assert optional == []


def test_no_layer_declares_an_absent_description() -> None:
    """`absentDescription` belongs only to an optional layer, and no layer is optional: it is nowhere."""
    with_absent = [layer["name"] for layer in _layers() if "absentDescription" in layer]
    assert with_absent == []


def test_exactly_one_layer_nests_and_its_nesting_names_its_own_placement_field() -> None:
    """`storage.nesting` and `placement.field`, two spellings of one fact, agree on the one nesting layer."""
    nesting = [layer for layer in _layers() if layer["storage"]["nesting"] != "flat"]
    assert [layer["name"] for layer in nesting] == [NESTING_LAYER]

    placement_layers = [layer for layer in _layers() if "placement" in layer]
    assert [layer["name"] for layer in placement_layers] == [NESTING_LAYER]

    layer = nesting[0]
    assert layer["storage"]["nesting"] == layer["placement"]["field"]


def test_the_placement_declaration_names_a_declared_kind_of_its_own_layer() -> None:
    """`placement.kind`, carried explicitly rather than derived, names a declared kind of its layer."""
    for layer in _layers():
        placement = layer.get("placement")
        if placement is None:
            continue
        declared = [kind["kind"] for kind in _kinds(layer)]
        assert placement["kind"] in declared, f"{layer['name']}: placement names an undeclared kind: {placement}"


def test_every_layers_filename_comes_from_its_identity_field() -> None:
    """One file per element, named from the value that identifies it, in every layer."""
    offenders = [layer["name"] for layer in _layers() if layer["storage"]["filenameFrom"] != layer["identityField"]]
    assert not offenders, f"layers whose file name is not their identity: {offenders}"


def test_member_only_and_member_of_agree() -> None:
    """A kind has a `memberOf` exactly when it has no file of its own."""
    for layer in _layers():
        for kind in _kinds(layer):
            has_owner = bool(kind.get("memberOf"))
            assert has_owner == kind["memberOnly"], (
                f"{kind['id']}: memberOnly={kind['memberOnly']}, memberOf={kind.get('memberOf')}"
            )


def test_every_member_of_names_a_declared_kind_of_the_same_layer() -> None:
    """A member-only kind's owner is a kind the same layer declares (membership never crosses layers)."""
    for layer in _layers():
        declared = {kind["kind"] for kind in _kinds(layer)}
        for kind in _kinds(layer):
            for owner in kind.get("memberOf", []):
                assert owner["kind"] in declared, f"{kind['id']}: memberOf names an undeclared kind: {owner}"


def test_a_discriminator_field_is_declared_for_every_sibling_or_for_none() -> None:
    """Within a layer, the top-level kinds either all discriminate or none do (a multi-kind layer must)."""
    for layer in _layers():
        top_level = [kind for kind in _kinds(layer) if not kind["memberOnly"]]
        discriminators = {kind.get("discriminatorField") for kind in top_level}
        assert len(discriminators) == 1, (
            f"{layer['name']}: mixed discriminators among top-level kinds: {discriminators}"
        )
        if len(top_level) > 1:
            assert discriminators != {None}, f"{layer['name']}: {len(top_level)} top-level kinds and no discriminator"


# ---------------------------------------------------------------------------
# Presentation facts and field-level relations
# ---------------------------------------------------------------------------

#: The relations the contract declares, by the field each is declared on;
#: listed rather than read off the document, so deleting one fails here.
_SATISFIES = ("requirements", "requirement", "id", "versioned-list-values")

EXPECTED_RELATIONS = {
    "field:swc.swc.memory": ("hardware", "memory", "name", "map-keys"),
    "field:hardware.memory.accessible": ("hardware", "core", "name", "list-values"),
    # A mapping set's own two sides are scalar `reference`s, not relations: a
    # relation names exactly one target kind and each side has to reach two.
    "field:swc.swc.data_type_mappings": ("mapping_sets", "mapping_set", "name", "list-values"),
    # The twelve `satisfies` lists and a requirement's `derived_from` (its
    # requirement-form members; the element form is no member of the relation).
    "field:swc.swc.satisfies": _SATISFIES,
    "field:swc.port.satisfies": _SATISFIES,
    "field:swc.runnable.satisfies": _SATISFIES,
    "field:interfaces.sender_receiver.satisfies": _SATISFIES,
    "field:interfaces.client_server.satisfies": _SATISFIES,
    "field:interfaces.external_interface.satisfies": _SATISFIES,
    "field:requirements.requirement.derived_from": _SATISFIES,
    "field:compositions.composition.satisfies": _SATISFIES,
    "field:tasks.task.satisfies": _SATISFIES,
    "field:hardware.processor.satisfies": _SATISFIES,
    "field:hardware.core.satisfies": _SATISFIES,
    "field:hardware.memory.satisfies": _SATISFIES,
    "field:hardware.partition.satisfies": _SATISFIES,
}


def _fields() -> dict[str, dict[str, Any]]:
    """Every declared field of the shipped contract, by identifier."""
    return {field["id"]: field for layer in _layers() for kind in _kinds(layer) for field in kind.get("fields", [])}


def test_the_contract_declares_the_major_the_layer_retirement_arrives_in() -> None:
    """The shipped contract version and the one supported major.

    The bump rule: removing or renaming an identifier is a MAJOR (unless it was
    declared inside the current, unreleased major), adding one or widening an
    `appliesTo` is a MINOR, prose alone is a PATCH; a released number is never
    reused. Standard citations the contract carries:
    `rule:data-type.application-type-double-mapped` is `[constr_1004]`'s
    *exactly one* half, and the rated `asil` fields' `enum` holds the eighteen
    values of `[TPS_SAFEX_00201]`.
    """
    # The SHIPPED version: moves with every bump, not only with a major.
    assert load_contract()["contractVersion"] == "12.7.1"
    assert SUPPORTED_CONTRACT_MAJORS == frozenset({12})


#: The five identifiers removed with the `systems` layer.
_REMOVED_SYSTEMS_IDENTIFIERS = (
    "layer:systems",
    "kind:systems.system",
    "field:systems.system.name",
    "field:systems.system.title",
    "field:systems.system.description",
)


def _declared_identifiers(document: Any) -> list[str]:
    """Every ``id`` value anywhere in *document*, in document order."""
    found: list[str] = []
    if isinstance(document, dict):
        if isinstance(document.get("id"), str):
            found.append(document["id"])
        for value in document.values():
            found.extend(_declared_identifiers(value))
    elif isinstance(document, list):
        for value in document:
            found.extend(_declared_identifiers(value))
    return found


def test_contract_6_renames_the_sequence_name_rule_and_removes_the_systems_layer() -> None:
    """The sequence-name rule is renamed in place and the `systems` identifiers are gone from contract and binding.

    **What this does not reach:** the Python that still names the layer or the
    old rule id (classes, readers, validator methods, coverage declarations);
    the conformance, wiring and coverage gates hold those.
    """
    contract = load_contract()
    binding = load_binding()
    identifiers = _declared_identifiers(contract)
    rules = {rule["id"]: rule for rule in contract["rules"]}

    assert "rule:sequences.sequence.name.likec4-identifier" not in rules
    renamed = rules["rule:sequences.sequence.name.identifier"]
    assert renamed["family"] == "element-local"
    assert renamed["appliesTo"] == ["field:sequences.sequence.name"]
    assert renamed["summary"] == (
        "A sequence's `name` must be non-blank and match `[A-Za-z_][A-Za-z0-9_]*`: it names the sequence's "
        "diagram files and its identifier on the diagrams page, which is deliberately not the AUTOSAR "
        "identifier rule the emitted names carry."
    )
    assert "rule:sequences.sequence.name.identifier" in binding["rules"]
    assert "rule:sequences.sequence.name.likec4-identifier" not in binding["rules"]

    for removed in _REMOVED_SYSTEMS_IDENTIFIERS:
        assert removed not in identifiers, removed
        for rule in contract["rules"]:
            assert removed not in rule["appliesTo"], (rule["id"], removed)
        for section in binding.values():
            if isinstance(section, dict):
                assert removed not in section, removed

    assert Counter(identifier.split(":", 1)[0] for identifier in identifiers)["layer"] == len(EXPECTED_LAYERS)


def test_contract_6_leaves_no_python_surface_for_the_removed_layer(tmp_path, capsys, monkeypatch) -> None:
    """No Python API, scaffolded directory or listed layer survives for `systems`, nor LikeC4 in the rule's message.

    **What this does not reach:** prose that still counts the old layers, and a
    stray `systems/` directory in a user's project, which the reader never
    looks at.
    """
    from pydantic import ValidationError
    from swcraft_tools import reader, schema
    from swcraft_tools.cli import main
    from swcraft_tools.writer import mutations

    assert not hasattr(schema, "SystemElement")
    assert not hasattr(schema, "_LIKEC4_IDENTIFIER_RE")
    assert not hasattr(reader, "read_systems")
    assert not hasattr(mutations, "create_system")

    assert "name_must_not_be_empty_and_must_be_identifier" in schema.Sequence.__pydantic_decorators__.field_validators
    with pytest.raises(ValidationError) as refused:
        schema.Sequence(name="not-an-identifier", task="T", steps=[{"swc": "S", "runnable": "R"}])
    message = str(refused.value)
    assert "name must be an identifier, because it names the sequence's diagram files" in message
    assert "LikeC4" not in message

    project = tmp_path / "fresh"
    monkeypatch.setattr("sys.argv", ["swcraft", "init", str(project)])
    main()
    model_root = project / "model"
    # Top-level directories only: the data description directories are nested
    # inside `data_types`.
    assert sorted(path.name for path in model_root.iterdir() if path.is_dir()) == [
        "compositions",
        "connectors",
        "data_types",
        "hardware",
        "interfaces",
        "requirements",
        "sequences",
        "swc",
        "tasks",
    ]
    assert sorted(path.name for path in (model_root / "data_types").iterdir() if path.is_dir()) == [
        "application",
        "base",
        "compu_methods",
        "constraints",
        "implementation",
        "mapping_sets",
        "units",
    ]
    capsys.readouterr()

    monkeypatch.setattr("sys.argv", ["swcraft", "list", "--project", str(project / "swcraft.yaml"), "--format", "json"])
    main()
    names = list(json.loads(capsys.readouterr().out)["layers"])
    assert len(names) == len(EXPECTED_LAYERS), names
    assert "systems" not in names


def test_no_contract_string_names_the_retired_diagram_stack() -> None:
    """No `description` or `summary` anywhere in the contract names LikeC4 or the bare token `C4`.

    **What this does not reach:** a string describing a diagram that does not
    exist under another name; nothing checks the prose against the generator.
    """
    offenders: list[tuple[str, str]] = []

    def visit(node: Any, owner: str) -> None:
        if isinstance(node, dict):
            here = node["id"] if isinstance(node.get("id"), str) else owner
            for key in ("description", "summary"):
                text = node.get(key)
                if isinstance(text, str) and re.search(r"(?i)likec4|\bC4\b", text):
                    offenders.append((here, key))
            for value in node.values():
                visit(value, here)
        elif isinstance(node, list):
            for value in node:
                visit(value, owner)

    visit(load_contract(), "contract")
    assert offenders == []


#: The fields `interface_compatibility` reads to reach a verdict, shared by the
#: two rules that judge by it: the interface kind, the member collections and
#: each member's name and type (an argument's direction, never its name, which
#: only a message quotes), and every data type field the structural resolver
#: reads — each kind's discriminator, a base type's three compared attributes,
#: the two `base` references a chain is followed through, and the record, array
#: and enum structure. Transcribed from the contract's own identifiers.
_COMPATIBILITY_READS = [
    "field:interfaces.sender_receiver.kind",
    "field:interfaces.client_server.kind",
    "field:interfaces.external_interface.kind",
    "field:interfaces.external_interface.external_dest",
    "field:interfaces.sender_receiver.data_elements",
    "field:interfaces.data_element.name",
    # The side a prototype states.
    "field:interfaces.data_element.application_type",
    "field:interfaces.data_element.implementation_type",
    "field:interfaces.client_server.operations",
    "field:interfaces.operation.name",
    "field:interfaces.operation.arguments",
    "field:interfaces.argument.direction",
    "field:interfaces.argument.application_type",
    "field:interfaces.argument.implementation_type",
    "field:base_types.base_type.kind",
    "field:implementation_types.value_type.kind",
    "field:implementation_types.reference_type.kind",
    "field:application_types.enum.kind",
    "field:application_types.record.kind",
    "field:application_types.array.kind",
    "field:base_types.base_type.size",
    "field:base_types.base_type.encoding",
    "field:base_types.base_type.native_declaration",
    "field:implementation_types.value_type.base",
    "field:implementation_types.reference_type.base",
    "field:application_types.record.fields",
    "field:application_types.record_field.name",
    "field:application_types.record_field.type",
    "field:application_types.array.element_type",
    "field:application_types.array.length",
    "field:application_types.enum.literals",
    "field:application_types.enum_literal.name",
    "field:application_types.enum_literal.value",
    # The application type's physics (unit, conversion, range): two
    # application types with different physics are not one type however the
    # names line up.
    "field:application_types.application_type.kind",
    "field:application_types.application_type.unit",
    "field:application_types.application_type.conversion",
    "field:application_types.application_type.range",
    "field:application_types.conversion.factor",
    "field:application_types.conversion.offset",
    # A conversion's scale limits are part of the scale `[constr_1163]` compares.
    "field:application_types.conversion.limits",
    "field:application_types.conversion_limits.lower",
    "field:application_types.conversion_limits.upper",
    "field:application_types.conversion_limit_bound.value",
    "field:application_types.conversion_limit_bound.interval",
    "field:application_types.range.min",
    "field:application_types.range.max",
    # The three implementation kinds a chain may reach, by discriminator, and
    # their structure. Appended after every earlier entry, so both lists only
    # gain.
    "field:implementation_types.implementation_record.kind",
    "field:implementation_types.implementation_array.kind",
    "field:implementation_types.pointer.kind",
    "field:implementation_types.implementation_record.elements",
    "field:implementation_types.implementation_element.name",
    "field:implementation_types.implementation_element.type",
    "field:implementation_types.implementation_array.element_type",
    "field:implementation_types.implementation_array.length",
    "field:implementation_types.pointer.target",
    # `is_service` and a data element's queued-ness, the two values of the
    # standard's compatibility constraints the model carries.
    "field:interfaces.sender_receiver.is_service",
    "field:interfaces.client_server.is_service",
    "field:interfaces.data_element.sw_impl_policy",
]


def test_the_two_interface_rules_declare_the_standards_compatibility() -> None:
    """The two interface rules judge the standard's compatibility, not equality.

    Each `appliesTo` names the ports it resolves, the `interface` fields it
    compares and every entry the predicate reads. The delegation rule also
    reads the outer port's `direction`, which selects the standard's clause: a
    subset for a `require` delegation, one shared member for a `provide` one.
    `field:interfaces.argument.name` is in neither list: `[constr_1076]`
    compares an argument pair by type and direction only. An application
    type's unit, conversion and range are judged (`[constr_1051]` 1, 2 and 4
    read fail-closed); an external interface stub's kind is its concrete
    `external_dest`'s, and a stub whose `DEST` is abstract is not judged
    (`[TPS_GST_00173]`); a data element is queued exactly when its counterpart
    is (`[constr_1068]` 1(c)); a conversion's `limits` are part of the scale
    `[constr_1163]` compares. Each summary is `validator.CONSISTENCY_RULES`'
    string byte for byte, the sentence `check-model` prints.

    **What this does not reach:** whether the validator actually reads each
    listed field; the field coverage oracle checks that.
    """
    rules = {rule["id"]: rule for rule in load_contract()["rules"]}

    connector = rules["rule:connector.interface-mismatch"]
    assert connector["family"] == "cross-element"
    assert connector["summary"] == (
        "A connector whose required port's interface is not satisfied by the provided port's: a kind that "
        "differs (an external interface stub's kind is the one its `external_dest` names), a data element or "
        "operation without a counterpart of the same name, or a counterpart whose resolved type or signature "
        "differs, a data element queued on one side only, or an `is_service` that differs. Not judged: an "
        "external interface stub beyond its kind, or whose `external_dest` is abstract; invalidValue, a value "
        "type's own unit and range, interface mappings."
    )
    assert connector["appliesTo"] == [
        "field:connectors.connector.from",
        "field:connectors.connector.to",
        "field:swc.port.interface",
        "field:compositions.composition_port.interface",
        *_COMPATIBILITY_READS,
    ]

    delegation = rules["rule:composition.delegation-interface-mismatch"]
    assert delegation["family"] == "cross-element"
    assert delegation["summary"] == (
        "A delegation whose inner port's interface is not compatible with its outer port's: a kind that "
        "differs (an external interface stub's kind is the one its `external_dest` names); for a require "
        "port, a data element or operation of the inner interface without a compatible counterpart of the "
        "same name in the outer; for a provide port, not one such member. A counterpart is compatible when "
        "its resolved type or signature agrees and it is queued exactly when the member is, and the two "
        "interfaces' `is_service` must agree. Not judged: an external interface stub beyond its kind, or whose "
        "`external_dest` is abstract; invalidValue, a value type's own unit and range, interface mappings."
    )
    assert delegation["appliesTo"] == [
        "field:compositions.delegation.port",
        "field:compositions.delegation.member_port",
        "field:compositions.composition_port.interface",
        "field:swc.port.interface",
        "field:compositions.composition_port.direction",
        *_COMPATIBILITY_READS,
    ]

    for rule_id in ("rule:connector.interface-mismatch", "rule:composition.delegation-interface-mismatch"):
        assert rules[rule_id]["summary"] == CONSISTENCY_RULES[rule_id.removeprefix("rule:")]
    assert [entry for entry in _COMPATIBILITY_READS if entry not in _fields()] == []


def test_the_data_element_declares_its_optional_initial_value() -> None:
    """The data element's `init` is declared key by key, an `= None` default as `"default": null`.

    The `default` key's **presence** is what the conformance gate compares
    against `FieldInfo.has_default`; a shipped precedent is asserted beside it.
    The field names no `reference`, `relation` or `collection`: its value is a
    literal, compared whole-value and never resolved.

    **What this does not reach:** whether the declared shape is *enforced*;
    that is `check-model`'s.
    """
    fields = _fields()
    init = fields["field:interfaces.data_element.init"]
    assert init["name"] == "init"
    assert init["type"] == "value"
    assert init["required"] is False
    assert "default" in init, "an `= None` field declares its default; the key's presence is the gate"
    assert init["default"] is None
    assert init["immutable"] is False
    assert init["description"].strip()
    assert not {"reference", "relation", "collection", "elementKind"} & set(init)

    precedent = fields["field:implementation_types.value_type.value_min"]
    assert precedent["required"] is False and "default" in precedent and precedent["default"] is None


def test_the_data_element_kind_says_it_carries_a_value() -> None:
    """`kind:interfaces.data_element`'s description names `init`, not only the two-field list it enumerated.

    **What this does not reach:** the wording itself, only that the stale
    enumeration is gone and the field is named.
    """
    kind = next(kind for layer in _layers() for kind in _kinds(layer) if kind["id"] == "kind:interfaces.data_element")
    assert "init" in kind["description"]
    assert not kind["description"].endswith("a name and the declared data type it carries.")


def test_no_rule_identifier_names_the_retired_resource_family() -> None:
    """No `rule:resource.*` survives in the contract or the binding; the `rule:hardware.*` ones are in both.

    **What this does not reach**, being a document sweep: the emitting code.
    `validator.CONSISTENCY_RULES`, `validator._RESOURCE_FINDING_RULES` and
    `editing/diagnosis.RULE_CODES` hold the identifiers as Python keys and
    values; `test_model_contract_conformance.py` holds them to the contract.
    """
    declared = [rule["id"] for rule in load_contract()["rules"]]
    assert [rule for rule in declared if rule.startswith("rule:resource.")] == []
    assert [rule for rule in load_binding()["rules"] if rule.startswith("rule:resource.")] == []

    # Named, so a rename that dropped a rule instead of moving it fails here.
    for rule in (
        "rule:hardware.missing-budget",
        "rule:hardware.threshold-breach",
        "rule:hardware.rate-monotonic-violation",
        "rule:hardware.unreachable-memory",
    ):
        assert rule in declared, rule
        assert rule in load_binding()["rules"], rule


def test_no_contract_summary_names_the_retired_layers_noun() -> None:
    """No rule summary, printed by `docs/guide/reference/rules.md`, names the retired `resources` noun.

    **What this does not reach:** the word is screened in rule summaries only;
    elsewhere it is legitimate (`swcraft resource-report`).
    """
    offenders = [rule["id"] for rule in load_contract()["rules"] if re.search(r"(?i)\bresources?\b", rule["summary"])]
    assert offenders == []


#: The heading an unreleased release note gives the contract's own § . Both
#: ``## The model's contract is`` and ``## The model contract is`` are read,
#: the possessive with a straight or a typographic apostrophe: a missed
#: spelling would leave the gate reading no heading at all.
_CONTRACT_SECTION_HEADING = re.compile(r"^## The model(?:['\u2019]s)? contract is `([^`]+)`", re.MULTILINE)


def test_the_release_note_contract_version_extractor_is_not_inert() -> None:
    """The heading grammar really finds a version, so a regex matching nothing cannot pass the gate."""
    assert _CONTRACT_SECTION_HEADING.findall("## The model contract is `9.9.9`\n") == ["9.9.9"]
    assert _CONTRACT_SECTION_HEADING.findall("## The model contract is 9.9.9\n") == []
    assert _CONTRACT_SECTION_HEADING.findall("## The model's contract is `9.9.9`\n") == ["9.9.9"]
    assert _CONTRACT_SECTION_HEADING.findall("## The model\u2019s contract is `9.9.9`\n") == ["9.9.9"]
    assert _CONTRACT_SECTION_HEADING.findall("## The models contract is `9.9.9`\n") == []


#: A backticked token that could be a repository path: it starts at one of the
#: repository's top-level directories and carries no glob or placeholder
#: metacharacter. The trailing ``:12`` / ``:12-34`` of a line citation is
#: stripped before the path is resolved.
_PATH_TOKEN = re.compile(
    r"`((?:docs|packages|tools|mcp_server|agent|vscode_extension|scripts|examples|templates|\.claude|\.github)"
    r"/[A-Za-z0-9_./-]*)(?::\d+(?:-\d+)?)?`"
)


def test_the_release_note_path_token_grammar_is_not_inert() -> None:
    """The path grammar really finds a path and skips a glob, so a regex matching nothing cannot pass."""
    found = [match.group(1) for match in _PATH_TOKEN.finditer("see `docs/guide/upgrading.md` and")]
    assert found == ["docs/guide/upgrading.md"]
    # The line suffix is assembled, so this module carries no line citation of its own.
    with_line = "`packages/tools/no/such/module.py" + ":124`"
    assert [m.group(1) for m in _PATH_TOKEN.finditer(with_line)] == ["packages/tools/no/such/module.py"]
    assert _PATH_TOKEN.findall("`docs/release-notes/0.{2,3}.*`") == []
    assert _PATH_TOKEN.findall("`hardware/memory.yaml`") == []


def _description_gaps(document: dict[str, Any]) -> list[str]:
    """The kind and field identifiers of *document* whose `description` is absent or blank."""
    gaps: list[str] = []
    for layer in document["layers"]:
        for kind in layer["kinds"]:
            if not str(kind.get("description") or "").strip():
                gaps.append(kind["id"])
            for field in kind.get("fields", []):
                if not str(field.get("description") or "").strip():
                    gaps.append(field["id"])
    return gaps


def test_every_kind_and_field_declares_a_description() -> None:
    """Every shipped `kindDecl` and `fieldDecl` carries non-empty prose, the editor's hover text.

    The meta-schema keeps `description` optional (requiring it would be a
    major), so completeness is a property of the SHIPPED document; the
    synthetic extension fixture is exempt by construction.
    """
    assert _description_gaps(load_contract()) == []


#: The enum kind's description, exactly: the one place the contract states what
#: the writer emits for an enumeration.
_ENUM_KIND_DESCRIPTION = (
    "An enumeration type: a set of named values, each with an integer value. Emitted as an "
    "`APPLICATION-PRIMITIVE-DATA-TYPE` of category `VALUE`. Its values are stated in one of two forms: inline "
    "as `literals`, from which a `{name}_CompuMethod` of category `TEXTTABLE` is generated that maps each "
    "literal to its value, or by `compu_method_ref`, naming a `texttable` element of the `compu_methods` layer, "
    "which generates no form."
)


def test_the_enum_kind_describes_the_category_the_writer_emits() -> None:
    """The enum's description says category ``VALUE`` and a ``TEXTTABLE`` compu method.

    The **prose** only; the bytes are pinned by
    ``test_arxml_datatypes_writer.py::TestTheEnumTypeIsCategoryValue``.
    """
    kinds = {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}
    assert kinds["kind:application_types.enum"]["description"] == _ENUM_KIND_DESCRIPTION


def test_the_description_gate_reports_a_bare_kind_and_a_bare_field() -> None:
    """The positive control: the gate bites on an absent AND on a blank description."""
    document = json.loads(json.dumps(load_contract()))
    first_kind = document["layers"][0]["kinds"][0]
    del first_kind["description"]
    first_kind["fields"][0]["description"] = "   "
    assert _description_gaps(document) == [first_kind["id"], first_kind["fields"][0]["id"]]


def _description_sites(document: dict[str, Any]) -> list[tuple[str, str]]:
    """Every `description` the contract declares, as `(site, text)`.

    Spelled out rather than walked, so a shape gaining a `description` is a
    visible edit here: the layer's, its `placement`'s (which reaches no
    generated editor schema), each kind's and each field's.
    """
    sites: list[tuple[str, str]] = []
    for layer in document["layers"]:
        sites.append((f"{layer['id']}.description", layer.get("description", "")))
        placement = layer.get("placement")
        if placement is not None:
            sites.append((f"{layer['id']}.placement.description", placement.get("description", "")))
        for kind in layer.get("kinds", []):
            sites.append((f"{kind['id']}.description", kind.get("description", "")))
            for field in kind.get("fields", []):
                sites.append((f"{field['id']}.description", field.get("description", "")))
    return sites


def _every_description_bearing_node(node: Any, path: str = "") -> list[str]:
    """Every path in *node* at which a mapping carries a `description` key, whatever its shape.

    The independent derivation :func:`_description_sites` is checked against.
    """
    found: list[str] = []
    if isinstance(node, dict):
        if "description" in node:
            found.append(path)
        for key, value in node.items():
            found.extend(_every_description_bearing_node(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_every_description_bearing_node(value, f"{path}[{index}]"))
    return found


def test_the_enumerated_description_sites_are_every_site_the_document_has() -> None:
    """The hand-written description sites match a shape-blind walk, by count and by shape.

    Compared by count and shape because the walk yields JSON paths and the
    enumeration contract identifiers, with no common spelling.

    **What it does not reach**: whether a site's *text* is right, and whether a
    `description` outside `layers` ought to be scanned at all.
    """
    document = load_contract()
    walked = _every_description_bearing_node(document)
    shapes = Counter(re.sub(r"\[\d+\]", "[]", path) for path in walked)
    assert set(shapes) == {
        ".layers[]",
        ".layers[].placement",
        ".layers[].kinds[]",
        ".layers[].kinds[].fields[]",
    }, f"a shape carries a `description` that _description_sites does not enumerate: {sorted(shapes)}"
    assert len(walked) == len(_description_sites(document))


def test_the_walk_finds_a_description_the_enumeration_would_miss() -> None:
    """The positive control: plant one at a shape the enumeration does not visit."""
    document = json.loads(json.dumps(load_contract()))
    document["rules"][0]["description"] = "prose at a shape nobody scans"
    walked = _every_description_bearing_node(document)
    assert ".rules[0]" in walked
    assert len(walked) == len(_description_sites(document)) + 1


def _engineering_vocabulary_findings(document: dict[str, Any]) -> list[str]:
    """`site: term` for every description of *document* written in the engineering vocabulary."""
    return [f"{site}: {label}" for site, text in _description_sites(document) for label in vocabulary_hits(text)]


def test_no_description_cites_the_engineering_record() -> None:
    """No contract `description`, rendered verbatim into the user guide, carries internal vocabulary.

    The vocabulary is `guide_fixtures.FORBIDDEN_VOCABULARY`, shared with the
    guide's page gates. The gate counts SITES, not distinct strings.

    **What this does not reach**: prose that names no listed term and is still
    written for the wrong reader, and the rule `summary` strings, which the
    guide's own vocabulary gate scans.
    """
    assert _engineering_vocabulary_findings(load_contract()) == []


def test_the_vocabulary_gate_reports_a_citation_at_every_description_shape() -> None:
    """The positive control: one forbidden term planted at each of the four shapes."""
    document = json.loads(json.dumps(load_contract()))
    layer = document["layers"][0]
    layer["description"] = "The layer, as Phase 9 shipped it."
    layer["placement"]["description"] = "Placement metadata; see ADR 011."
    kind = layer["kinds"][0]
    kind["description"] = "One software component, as the registry projects it."
    kind["fields"][0]["description"] = "Its identity, declared in binding.json."
    assert _engineering_vocabulary_findings(document) == [
        f"{layer['id']}.description: Phase <n>",
        f"{layer['id']}.placement.description: ADR <n>",
        f"{kind['id']}.description: registry",
        f"{kind['fields'][0]['id']}.description: binding.json",
    ]


def test_every_layer_declares_a_label_and_a_display_order() -> None:
    """Every shipped layer declares a label and a display order, part of the diff renderer's byte output."""
    for layer in _layers():
        assert layer.get("label"), f"{layer['name']}: declares no label"
        assert "displayOrder" in layer, f"{layer['name']}: declares no displayOrder"


#: Every shipped layer's singular token as it stood before tokens were
#: declared: an oracle independent of the contract, since reading
#: `layerDecl.token` back would compare it with itself. `impact` publishes these
#: as `ElementRef.layer`, `validator` keys by them and
#: `model_state.parse_label` spells them, so a changed value is a changed
#: public output. A retired layer's row is dropped: the test grades the
#: declared layers.
PRE_MIGRATION_LAYER_TOKENS: dict[str, str] = {
    "swc": "swc",
    "interfaces": "interface",
    "connectors": "connector",
    "requirements": "requirement",
    "tasks": "task",
    "sequences": "sequence",
}

#: The layers declared after tokens became declarations, which
#: :data:`PRE_MIGRATION_LAYER_TOKENS` says nothing about: there is no
#: before-state to grade them against. A replacement layer is NOT graded by the
#: layer it replaced (`implementation_types` holds `data_types`' kinds under a
#: token of its own): that would assert a continuity the contract does not
#: claim.
LAYERS_ADDED_AFTER_THE_TOKEN_MIGRATION: frozenset[str] = frozenset(
    {
        "hardware",
        "compositions",
        "mapping_sets",
        "base_types",
        "application_types",
        "implementation_types",
        "units",
        "compu_methods",
        "data_constraints",
    }
)


def test_every_layer_declares_a_token() -> None:
    """Every shipped layer declares `token`, optional in the meta-schema, so no public output rests on a fallback."""
    unanswered = [layer["name"] for layer in _layers() if "token" not in layer]
    assert unanswered == [], f"layers that declare no token: {unanswered}"


def test_the_declared_tokens_are_the_ones_the_derivation_produced() -> None:
    """The declared tokens equal both the transcribed table and the old derivation.

    The derivation (strip a trailing `s` from `binding.snapshotAttributes`,
    falling back to the layer name) is restated here because production no
    longer holds it, so the pair is checked as a rule and not only as a table.

    **What it does not reach:** a layer declared after tokens became
    declarations (:data:`LAYERS_ADDED_AFTER_THE_TOKEN_MIGRATION`); every
    declared layer must be in one set or the other, so the exemption set
    cannot grow quietly.
    """
    snapshot_attributes = load_binding()["snapshotAttributes"]
    graded = [layer for layer in _layers() if layer["name"] not in LAYERS_ADDED_AFTER_THE_TOKEN_MIGRATION]
    declared = {layer["name"]: layer["token"] for layer in graded}
    derived = {
        layer["name"]: (lambda word: word[:-1] if word.endswith("s") else word)(
            snapshot_attributes.get(layer["id"]) or layer["name"]
        )
        for layer in graded
    }

    assert declared == PRE_MIGRATION_LAYER_TOKENS
    assert derived == PRE_MIGRATION_LAYER_TOKENS
    assert {layer["name"] for layer in _layers()} == set(PRE_MIGRATION_LAYER_TOKENS) | (
        LAYERS_ADDED_AFTER_THE_TOKEN_MIGRATION
    )


def test_no_two_layers_declare_the_same_token() -> None:
    """No two shipped layers share an effective token (declared, or the layer name as fallback).

    `model_registry._check_layer_tokens` refuses a collision at construction;
    this names one in the shipped document. The effective token catches the
    mixed collision of a declared token with a fallback name.
    """
    tokens = [layer.get("token") or layer["name"] for layer in _layers()]
    assert sorted(tokens) == sorted(set(tokens)), f"duplicate layer tokens: {sorted(tokens)}"


def test_every_layer_answers_arxml_bound_explicitly() -> None:
    """Every shipped layer declares `arxmlBound`, optional in the meta-schema, rather than resting on a fallback."""
    unanswered = [layer["name"] for layer in _layers() if "arxmlBound" not in layer]
    assert unanswered == [], f"layers that do not answer arxmlBound: {unanswered}"
    assert all(isinstance(layer["arxmlBound"], bool) for layer in _layers())


def test_exactly_five_layers_are_arxml_bound() -> None:
    """The scope of the Identifier rule (names that reach ARXML must be legal AUTOSAR Identifiers), pinned both ways.

    The bound layers emit their declared names as AUTOSAR `SHORT-NAME`s; the
    other four emit none, so `[a-zA-Z][a-zA-Z0-9_]*` there would invent a
    constraint AUTOSAR does not ask for. Set equality, because a count passes
    on the wrong set and a dropped layer would switch its identifier checking
    off unnoticed (the ARXML goldens are built from a valid example). A layer
    whose declared name is emitted as a `SHORT-NAME` joins the bound set.
    """
    bound = {layer["name"] for layer in _layers() if layer.get("arxmlBound", False)}
    assert bound == ARXML_BOUND_LAYER_NAMES
    assert len(bound) == 11
    assert len(_layers()) - len(bound) == 4


#: The fields written OUT of a model file while they hold their declared
#: default, listed rather than derived from `omitAtDefault` (which would be the
#: contract compared with itself). The reason is a round trip: a file written
#: before the field existed carries no key and must come back carrying none,
#: while the derived rule EMITS `[]` and `False` (it drops only `None`, `""`
#: and `{}`) — so each list or flag added later would otherwise appear in every
#: rewritten file.
OMIT_AT_DEFAULT_FIELD_IDS = frozenset(
    {
        "field:interfaces.sender_receiver.is_service",
        "field:interfaces.client_server.is_service",
        "field:swc.port.com_specs",
        "field:swc.timing_event.disabled_modes",
        "field:swc.runnable.min_start_interval",
        "field:swc.runnable.concurrent",
        "field:swc.runnable.reads",
        "field:swc.runnable.writes",
        "field:swc.runnable.calls",
        "field:swc.swc.external",
        "field:swc.swc.data_type_mappings",
        "field:compositions.composition.root",
        "field:interfaces.external_interface.operations",
        "field:interfaces.external_interface.data_elements",
        # Left out at `non_trusted`, a string default outside the derived rule.
        "field:hardware.partition.trust",
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
        "field:requirements.requirement.stub",
        "field:swc.swc.variables",
        "field:swc.runnable.reads_variables",
        "field:swc.runnable.writes_variables",
        "field:swc.swc.exclusive_areas",
        "field:swc.runnable.can_enter",
        "field:swc.runnable.runs_inside",
        "field:swc.data_event.disabled_modes",
        "field:swc.operation_call.asynchronous",
        "field:swc.operation_call.poll",
    }
)

#: The defaults the serializer's DERIVED rule already drops; declaring
#: `omitAtDefault` on such a field would restate the rule, not except from it.
DERIVED_OMITTED_DEFAULTS: tuple[Any, ...] = (None, "", {})


def test_the_omit_at_default_set_is_the_declared_six() -> None:
    """The `omitAtDefault` fields are exactly the listed set, and each is a genuine exception.

    Set equality, because a count passes on the wrong field; no other field
    carries the key at all, not even `false`. Each declaring field's default
    sits OUTSIDE the derived `None` / `""` / `{}` rule, or the declaration
    would restate it. `field:tasks.task.runnables` is a falsy default that is
    emitted on purpose, which is why the derived rule is not widened to
    "falsy"; it is checked to still *have* such a default before it is checked
    not to declare the exception.

    **What this does not reach:** an optional list or flag added later without
    the declaration (`test_writer_elements.py`'s byte corpus catches that), and
    whether the writer HONOURS the declaration.
    """
    fields = _fields()
    declared = {field_id for field_id, field in fields.items() if field.get("omitAtDefault", False)}
    assert declared == OMIT_AT_DEFAULT_FIELD_IDS
    # The number of `omitAtDefault` fields; a field declaring the exception moves it.
    assert len(declared) == 38

    carrying = {field_id for field_id, field in fields.items() if "omitAtDefault" in field}
    assert carrying == OMIT_AT_DEFAULT_FIELD_IDS, "a field carries the key without declaring the exception"

    for field_id in sorted(declared):
        field = fields[field_id]
        assert isinstance(field["omitAtDefault"], bool)
        assert "default" in field, f"{field_id}: omits a default it does not declare"
        assert not any(field["default"] == omitted for omitted in DERIVED_OMITTED_DEFAULTS), (
            f"{field_id}: its default is already dropped by the derived rule, so the declaration restates it"
        )

    emitted_falsy = ("field:tasks.task.runnables",)
    for field_id in emitted_falsy:
        field = fields[field_id]
        assert "default" in field, f"{field_id}: declares no default, so it cannot make this point"
        assert not field["default"], f"{field_id}: its default is not falsy any more"
        assert not any(field["default"] == omitted for omitted in DERIVED_OMITTED_DEFAULTS), (
            f"{field_id}: its default is inside the derived set, so the derived rule already drops it"
        )
        assert not field.get("omitAtDefault", False), f"{field_id} is emitted at its falsy default"


#: Every layer's display order: the seven data description layers take 4-10
#: and the four layers presented after them 11-14.
FINAL_DISPLAY_ORDERS = {
    "requirements": 0,
    "compositions": 1,
    "swc": 2,
    "interfaces": 3,
    "application_types": 4,
    "implementation_types": 5,
    "base_types": 6,
    "units": 7,
    "compu_methods": 8,
    "data_constraints": 9,
    "mapping_sets": 10,
    "connectors": 11,
    "tasks": 12,
    "sequences": 13,
    "hardware": 14,
}


def test_the_display_orders_are_distinct_and_the_decided_ones() -> None:
    """The display orders are distinct (which the per-layer meta-schema cannot express) and the decided ones."""
    orders = [layer["displayOrder"] for layer in _layers()]
    assert len(set(orders)) == len(orders)
    assert {layer["name"]: layer["displayOrder"] for layer in _layers()} == FINAL_DISPLAY_ORDERS


def test_the_declared_display_agrees_with_the_differs_own_table() -> None:
    """The declared labels and order agree with `differ._layer_renders()`, joined through the snapshot attributes."""
    attributes = load_binding()["snapshotAttributes"]
    display = {
        render.attribute: (index, render.label) for index, render in enumerate(_layer_renders(production_registry()))
    }
    # The declared values may leave gaps; the differ renders in their rank.
    rank = {value: index for index, value in enumerate(sorted(layer["displayOrder"] for layer in _layers()))}
    for layer in _layers():
        attribute = attributes[layer["id"]]
        assert attribute in display, f"{layer['name']}: no rendered row for {attribute!r}"
        order, label = display[attribute]
        assert (rank[layer["displayOrder"]], layer["label"]) == (order, label), (
            f"{layer['name']}: declares {(layer['displayOrder'], layer['label'])!r}, "
            f"the differ renders {(order, label)!r}"
        )


def test_the_relations_are_the_four_declared_on_their_source_fields() -> None:
    """Relations are declared ON their source field, where `field`-granularity coverage reaches them."""
    declared = {
        field["id"]: (
            field["relation"]["layer"],
            field["relation"]["kind"],
            field["relation"]["field"],
            field["relation"]["resolution"],
        )
        for field in _fields().values()
        if "relation" in field
    }
    assert declared == EXPECTED_RELATIONS


def test_no_field_declares_both_a_reference_and_a_relation() -> None:
    """The exclusion, over the shipped document rather than over a fragment."""
    both = [field["id"] for field in _fields().values() if "reference" in field and "relation" in field]
    assert both == []


def test_every_relation_targets_a_declared_kind_and_a_declared_field() -> None:
    """Every relation's target kind, its layer and the matched field are declared (else it silently matches nothing)."""
    kinds = {
        kind["kind"]: (layer["name"], {field["name"] for field in kind.get("fields", [])})
        for layer in _layers()
        for kind in _kinds(layer)
    }
    for field in _fields().values():
        relation = field.get("relation")
        if relation is None:
            continue
        assert relation["kind"] in kinds, f"{field['id']}: targets undeclared kind {relation['kind']!r}"
        layer_name, field_names = kinds[relation["kind"]]
        assert layer_name == relation["layer"], (
            f"{field['id']}: names layer {relation['layer']!r}, but {relation['kind']!r} belongs to {layer_name!r}"
        )
        assert relation["field"] in field_names, (
            f"{field['id']}: matches field {relation['field']!r}, which {relation['kind']!r} does not declare"
        )


def _field_value_relations_resolving_by_identity(document: dict[str, Any]) -> list[str]:
    """Every `field-value` relation of *document* matching the target's identity: a reference written the long way.

    The collection resolutions are exempt: their members legitimately name the
    target by identity.
    """
    layers = document["layers"]
    identity = {layer["name"]: layer["identityField"] for layer in layers}
    fields = (field for layer in layers for kind in layer["kinds"] for field in kind.get("fields", []))
    offenders = []
    for field in fields:
        relation = field.get("relation")
        if relation is None or relation["resolution"] != "field-value":
            continue
        if relation["field"] == identity[relation["layer"]]:
            offenders.append(field["id"])
    return offenders


def test_no_relation_resolves_by_the_targets_identity_through_a_field_value() -> None:
    """No `field-value` relation resolves by the target's identity; none is declared at all.

    Vacuous over the shipped document, so the next test shows it bites.
    """
    contract = load_contract()
    assert _field_value_relations_resolving_by_identity(contract) == []
    declared = [
        field["id"] for field in _fields().values() if field.get("relation", {}).get("resolution") == "field-value"
    ]
    assert declared == [], f"a `field-value` relation is declared again: {declared}"


def test_the_identity_relation_guard_bites_on_a_planted_field_value_relation() -> None:
    """The positive control: turn one shipped relation back into a `field-value` one."""
    document = json.loads(json.dumps(load_contract()))
    field = next(
        field
        for layer in document["layers"]
        for kind in layer["kinds"]
        for field in kind.get("fields", [])
        if field["id"] == "field:hardware.memory.accessible"
    )
    field["relation"]["resolution"] = "field-value"
    assert _field_value_relations_resolving_by_identity(document) == ["field:hardware.memory.accessible"]


def test_the_binding_binds_exactly_the_declared_relations() -> None:
    """The binding binds exactly the declared relations, both directions."""
    bound = load_binding()["relations"]
    declared = {field["id"] for field in _fields().values() if "relation" in field}
    assert set(bound) == declared


def test_every_bound_resolver_is_importable_and_callable() -> None:
    """The binding names Python that exists — the honesty check, for resolvers."""
    for field_id, row in load_binding()["relations"].items():
        module = importlib.import_module(row["module"])
        resolver = getattr(module, row["callable"], None)
        assert callable(resolver), f"{field_id}: {row['module']}.{row['callable']} is not callable"


def test_each_resolution_is_bound_to_its_own_resolver() -> None:
    """One callable per declared resolution shape, and no shape shares one."""
    fields = _fields()
    bound = load_binding()["relations"]
    by_resolution: dict[str, set[str]] = {}
    for field_id, row in bound.items():
        by_resolution.setdefault(fields[field_id]["relation"]["resolution"], set()).add(row["callable"])
    assert all(len(callables) == 1 for callables in by_resolution.values()), by_resolution
    minted = [next(iter(callables)) for callables in by_resolution.values()]
    assert len(set(minted)) == len(minted), f"two resolutions share a resolver: {by_resolution}"


# ---------------------------------------------------------------------------
# Contract-version compatibility
# ---------------------------------------------------------------------------


def test_the_shipped_contract_declares_a_supported_major() -> None:
    """The shipped contract's declared major is a supported one."""
    declared = load_contract()["contractVersion"]
    require_supported_contract_version({"contractVersion": declared})
    assert int(declared.split(".")[0]) in SUPPORTED_CONTRACT_MAJORS


@pytest.mark.parametrize(
    ("case", "version"),
    (
        ("unknown-future-major", "13.0.0"),
        ("unknown-unreleased-replaced-major", "11.1.0"),
        ("unknown-just-replaced-major", "10.2.0"),
        ("unknown-replaced-before-major", "9.2.0"),
        ("unknown-replaced-major", "8.0.0"),
        # Released majors a project's tooling can still hold: refused, never
        # partially read.
        ("unknown-previous-major", "5.0.0"),
        ("unknown-older-released-major", "4.7.1"),
        ("unknown-released-major", "3.4.0"),
        ("unknown-past-major", "2.0.1"),
        ("unknown-far-future-major", "17.3.1"),
        ("major-only", "1"),
        ("major-minor-only", "1.0"),
        ("not-a-version", "draft"),
        ("empty", ""),
        # The helper runs before the meta-schema and on documents it never
        # sees, so it must not be looser than its `semanticVersion`.
        ("non-numeric-tail", "1.abc"),
        ("zero-padded-major", "01.0.0"),
    ),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_an_unreadable_contract_version_is_rejected(case: str, version: str) -> None:
    """A contract this repository cannot read is refused, never partially read into a truncated model."""
    document = {**load_contract(), "contractVersion": version}
    with pytest.raises(UnsupportedContractVersion) as raised:
        require_supported_contract_version(document)
    assert repr(version) in str(raised.value), f"{case}: the refusal does not name the version it refused"


def test_a_contract_without_a_version_is_rejected() -> None:
    """An unversioned contract is refused rather than read as the newest major."""
    with pytest.raises(UnsupportedContractVersion, match="no contractVersion"):
        require_supported_contract_version(_without(load_contract(), "contractVersion"))


# ---------------------------------------------------------------------------
# The binding: the only artifact of the set that names Python
# ---------------------------------------------------------------------------

#: Every binding form `binding.schema.json` must factor out as a named `$defs`,
#: listed for the reason `REQUIRED_SHAPES` is.
REQUIRED_BINDING_SHAPES = (
    "kindBinding",
    "relationBinding",
    "ruleBinding",
    "callableBinding",
    "methodBinding",
    "configBinding",
    "tableBinding",
    "deprecationDecl",
)

#: The identifier and Python-symbol patterns the same file must declare.
REQUIRED_BINDING_IDENTIFIER_SHAPES = (
    "layerId",
    "kindId",
    "fieldId",
    "ruleId",
    "moduleName",
    "className",
    "pythonName",
    "bindingSection",
)

#: One fragment per binding form that the schema must ACCEPT, transcribed from
#: real symbols; the four rule forms include the two that are not one function
#: per rule.
VALID_BINDING_FRAGMENTS: dict[str, Any] = {
    "kindBinding": {"module": "swcraft_tools.schema", "class": "SWComponent"},
    "relationBinding": {"module": "swcraft_tools.relations", "callable": "resolve_field_value"},
    "callableBinding": {"module": "swcraft_tools.validator.connectors", "callable": "check_connector_compatibility"},
    "methodBinding": {"kind": "kind:tasks.task", "method": "_check_period"},
    "configBinding": {"module": "swcraft_tools.schema", "class": "ModelElement", "configKey": "allow_inf_nan"},
    "tableBinding": {
        "module": "swcraft_tools.validator.hardware",
        "table": "_RESOURCE_FINDING_RULES",
        "key": "rate-monotonic-violation",
    },
    "deprecationDecl": {"reason": "A legacy vocabulary bridge, deleted with the legacy tables."},
}
VALID_BINDING_FRAGMENTS["ruleBinding"] = VALID_BINDING_FRAGMENTS["callableBinding"]

#: A whole binding document the schema must accept, minimal for the reason
#: `MINIMAL_CONTRACT` is.
MINIMAL_BINDING: dict[str, Any] = {
    "kinds": {"kind:swc.swc": VALID_BINDING_FRAGMENTS["kindBinding"]},
    "rules": {},
    "relations": {"field:tasks.task.core": VALID_BINDING_FRAGMENTS["relationBinding"]},
    "fieldNames": {"field:connectors.connector.from": "from_"},
    "snapshotAttributes": {"layer:swc": "swcs"},
    "deprecations": {"snapshotAttributes": VALID_BINDING_FRAGMENTS["deprecationDecl"]},
}

#: ``(shape, case, fragment, expected message substring)``, as `PERTURBATIONS`.
BINDING_PERTURBATIONS: tuple[tuple[str, str, Any, str], ...] = (
    (
        "kindBinding",
        "missing-class",
        _without(VALID_BINDING_FRAGMENTS["kindBinding"], "class"),
        "class",
    ),
    (
        # A binding may only bind: an extra key would state a model fact
        # outside the contract.
        "kindBinding",
        "extra-key",
        _with(VALID_BINDING_FRAGMENTS["kindBinding"], layer="swc"),
        "layer",
    ),
    (
        "kindBinding",
        "class-that-is-not-a-class-name",
        _with(VALID_BINDING_FRAGMENTS["kindBinding"], **{"class": "swcraft_tools.schema.SWComponent"}),
        "class",
    ),
    (
        "relationBinding",
        "missing-callable",
        _without(VALID_BINDING_FRAGMENTS["relationBinding"], "callable"),
        "callable",
    ),
    (
        # Re-stating the contract's `resolution` would give the shape two
        # declarations and let the binding pick.
        "relationBinding",
        "resolution-restated-in-the-binding",
        _with(VALID_BINDING_FRAGMENTS["relationBinding"], resolution="field-value"),
        "resolution",
    ),
    (
        "callableBinding",
        "callable-and-config-key-at-once",
        _with(VALID_BINDING_FRAGMENTS["callableBinding"], configKey="allow_inf_nan"),
        "configKey",
    ),
    (
        "callableBinding",
        "module-spelled-as-a-class",
        _with(VALID_BINDING_FRAGMENTS["callableBinding"], module="swcraft_tools.Validator"),
        "module",
    ),
    (
        # An element-local rule resolves its class through this document's own
        # `kinds` map, so it names a CONTRACT identifier and never a class.
        "methodBinding",
        "kind-named-as-a-python-class",
        _with(VALID_BINDING_FRAGMENTS["methodBinding"], kind="Task"),
        "kind:",
    ),
    (
        "methodBinding",
        "missing-method",
        _without(VALID_BINDING_FRAGMENTS["methodBinding"], "method"),
        "method",
    ),
    (
        "configBinding",
        "missing-config-key",
        _without(VALID_BINDING_FRAGMENTS["configBinding"], "configKey"),
        "configKey",
    ),
    (
        "tableBinding",
        "missing-key",
        _without(VALID_BINDING_FRAGMENTS["tableBinding"], "key"),
        "key",
    ),
    (
        # A row naming both a table and a callable would not say which one
        # produces the rule.
        "tableBinding",
        "table-and-callable-at-once",
        _with(VALID_BINDING_FRAGMENTS["tableBinding"], **{"callable": "check_resource_budgets"}),
        "callable",
    ),
    (
        "ruleBinding",
        "no-form-at-all",
        {},
        "is not valid under any of the given schemas",
    ),
    (
        "deprecationDecl",
        "an-owner-beside-the-reason",
        _with(VALID_BINDING_FRAGMENTS["deprecationDecl"], owner="the next release"),
        "owner",
    ),
    (
        "deprecationDecl",
        "removal-without-a-reason",
        _with(VALID_BINDING_FRAGMENTS["deprecationDecl"], reason=""),
        "reason",
    ),
)


def _bound_classes() -> dict[str, type]:
    """Every contract kind identifier resolved to its bound class, so the binding's claims are resolved."""
    resolved: dict[str, type] = {}
    for kind_id, binding in load_binding()["kinds"].items():
        module = importlib.import_module(binding["module"])
        resolved[kind_id] = getattr(module, binding["class"])
    return resolved


def _contract_kind_ids() -> list[str]:
    return [kind["id"] for layer in _layers() for kind in _kinds(layer)]


def test_binding_schema_is_a_valid_json_schema() -> None:
    """`binding.schema.json` is a valid Draft 2020-12 schema."""
    schema = load_binding_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/binding.schema.json"), schema["$id"]


def test_binding_schema_declares_every_required_shape() -> None:
    """Every binding form and pattern is a named `$defs`.

    Four rule forms: a table-minted rule (`validator._RESOURCE_FINDING_RULES`'s
    rows) bound to a callable would name one whose source does not contain it.
    """
    schema = load_binding_schema()
    definitions = schema["$defs"]
    missing = [name for name in REQUIRED_BINDING_SHAPES + REQUIRED_BINDING_IDENTIFIER_SHAPES if name not in definitions]
    assert not missing, f"binding.schema.json is missing $defs: {missing}"
    # `ruleBinding` is the `oneOf` over the four forms and declares no
    # properties of its own, so it is required by name and is not an object
    # shape; every other object shape must be in the tuple.
    assert object_shape_defs(schema) <= set(REQUIRED_BINDING_SHAPES), (
        f"binding shapes outside REQUIRED_BINDING_SHAPES: "
        f"{sorted(object_shape_defs(schema) - set(REQUIRED_BINDING_SHAPES))}"
    )


def test_every_binding_shape_forbids_additional_properties() -> None:
    """No binding form admits an undeclared key, a place to state a model fact outside the contract."""
    schema = load_binding_schema()
    offenders = [
        sorted(node.get("properties", {}))
        for node in _walk_schemas(schema)
        if node.get("type") == "object" and "properties" in node and node.get("additionalProperties") is not False
    ]
    assert not offenders, f"binding shapes without `additionalProperties: false`: {offenders}"
    named = [
        name for name in object_shape_defs(schema) if schema["$defs"][name].get("additionalProperties") is not False
    ]
    assert not named, f"$defs shapes without `additionalProperties: false`: {named}"


def test_a_minimal_binding_document_validates() -> None:
    """The top-level binding shape is satisfiable — the other half of the rejections below."""
    assert validation_messages(Draft202012Validator(load_binding_schema()), MINIMAL_BINDING) == []


@pytest.mark.parametrize(
    ("case", "document", "expected"),
    (
        ("unknown-top-level-key", {**MINIMAL_BINDING, "layers": {}}, "layers"),
        ("missing-kinds", _without(MINIMAL_BINDING, "kinds"), "kinds"),
        ("missing-deprecations", _without(MINIMAL_BINDING, "deprecations"), "deprecations"),
        (
            # Keys are contract identifiers and values Python symbols; a Python
            # name as a key would introduce an entry the contract does not
            # declare.
            "python-class-as-a-kinds-key",
            {**MINIMAL_BINDING, "kinds": {"SWComponent": VALID_BINDING_FRAGMENTS["kindBinding"]}},
            "kind:",
        ),
        (
            "python-attribute-as-a-field-names-key",
            {**MINIMAL_BINDING, "fieldNames": {"from_": "from_"}},
            "field:",
        ),
        ("missing-relations", _without(MINIMAL_BINDING, "relations"), "relations"),
        (
            # A relation is declared on a FIELD, so its binding is keyed by a
            # field identifier.
            "layer-identifier-as-a-relations-key",
            {**MINIMAL_BINDING, "relations": {"layer:tasks": VALID_BINDING_FRAGMENTS["relationBinding"]}},
            "field:",
        ),
        (
            "deprecation-of-a-section-that-does-not-exist",
            {**MINIMAL_BINDING, "deprecations": {"layers": VALID_BINDING_FRAGMENTS["deprecationDecl"]}},
            "layers",
        ),
    ),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_malformed_binding_document_is_rejected(case: str, document: Any, expected: str) -> None:
    """A binding document the schema must refuse, for the reason the case names."""
    messages = validation_messages(Draft202012Validator(load_binding_schema()), document)
    assert messages, f"{case}: the binding schema accepted a malformed document"
    assert any(expected in message for message in messages), f"{case}: rejected for the wrong reason: {messages}"


@pytest.mark.parametrize("shape", REQUIRED_BINDING_SHAPES)
def test_a_well_formed_binding_fragment_is_accepted(shape: str) -> None:
    """Each binding form accepts a fragment transcribed from real symbols."""
    assert validation_messages(binding_shape_validator(shape), VALID_BINDING_FRAGMENTS[shape]) == []


@pytest.mark.parametrize(
    ("shape", "case", "fragment", "expected"),
    BINDING_PERTURBATIONS,
    ids=[f"{shape}-{case}" for shape, case, _, _ in BINDING_PERTURBATIONS],
)
def test_a_perturbed_binding_fragment_is_rejected(shape: str, case: str, fragment: Any, expected: str) -> None:
    """The gate bites on the binding too, one form at a time."""
    messages = validation_messages(binding_shape_validator(shape), fragment)
    assert messages, f"{shape}/{case}: the binding schema accepted a malformed fragment"
    assert any(expected in message for message in messages), (
        f"{shape}/{case}: rejected for the wrong reason: {messages}"
    )


def test_no_rule_binding_can_satisfy_two_forms() -> None:
    """No instance can satisfy two `ruleBinding` forms: each branch is closed and requires a key no other admits.

    The guarantee comes from the branches, not the `oneOf` (a merged
    instance fails all four on `additionalProperties: false`), so the branches
    are what this checks.
    """
    definitions = load_binding_schema()["$defs"]
    forms = ("callableBinding", "methodBinding", "configBinding", "tableBinding")
    for name in forms:
        assert definitions[name].get("additionalProperties") is False, f"{name} admits unknown keys"
    for left in forms:
        for right in forms:
            if left == right:
                continue
            allowed = set(definitions[left]["properties"])
            assert not set(definitions[right]["required"]) <= allowed, (
                f"{right}'s required keys all fit inside {left}; the two forms could overlap"
            )
    merged = {
        "module": "swcraft_tools.validator.scheduling",
        "callable": "check_task_consistency",
        "kind": "kind:tasks.task",
    }
    assert validation_messages(binding_shape_validator("ruleBinding"), merged) != []


def test_every_binding_shape_has_a_perturbation() -> None:
    """No binding form is declared without something proving it rejects."""
    covered = {shape for shape, _, _, _ in BINDING_PERTURBATIONS}
    assert covered == set(REQUIRED_BINDING_SHAPES), (
        f"binding shapes with no perturbation: {sorted(set(REQUIRED_BINDING_SHAPES) - covered)}"
    )


def test_binding_fragment_validator_refuses_an_unknown_shape() -> None:
    """A renamed `$defs` fails loudly instead of validating against nothing."""
    with pytest.raises(KeyError, match="tableBindings"):
        binding_shape_validator("tableBindings")


def test_the_shipped_binding_validates_against_its_schema() -> None:
    """`binding.json` is a legal binding document — the gate every check below assumes."""
    assert validation_messages(Draft202012Validator(load_binding_schema()), load_binding()) == []


# ---------------------------------------------------------------------------
# The kind binding
# ---------------------------------------------------------------------------


def test_every_declared_kind_has_exactly_one_binding() -> None:
    """Contract kinds and binding rows are the same set, in the same order.

    A kind with no row cannot be instantiated by the kernel; a row naming no
    declared kind is the binding adding an entry of its own.
    """
    declared = _contract_kind_ids()
    bound = list(load_binding()["kinds"])
    assert bound == declared, (
        f"kinds only in the contract: {sorted(set(declared) - set(bound))}; "
        f"kinds only in the binding: {sorted(set(bound) - set(declared))}"
    )


def test_every_bound_class_is_importable_and_a_pydantic_model() -> None:
    """Each row resolves to a real `BaseModel` subclass, whose `model_fields` the structural projection reads."""
    for kind_id, cls in _bound_classes().items():
        assert isinstance(cls, type), f"{kind_id}: binding does not resolve to a class: {cls!r}"
        assert issubclass(cls, BaseModel), f"{kind_id}: bound class is not a pydantic model: {cls!r}"


def test_no_two_kinds_bind_the_same_class() -> None:
    """The kind binding is injective: one class with two contract identities would pass per-kind checks wrongly."""
    resolved = _bound_classes()
    by_class: dict[type, list[str]] = {}
    for kind_id, cls in resolved.items():
        by_class.setdefault(cls, []).append(kind_id)
    duplicates = {cls.__name__: ids for cls, ids in by_class.items() if len(ids) > 1}
    assert not duplicates, f"one class bound to several kinds: {duplicates}"


# ---------------------------------------------------------------------------
# `fieldNames`: the one place a Python attribute spelling exists
# ---------------------------------------------------------------------------

#: The one field whose attribute spelling differs from its serialization alias,
#: pinned by name: an empty binding would also satisfy an empty-set comparison.
KNOWN_DIFFERING_FIELD = ("field:connectors.connector.from", "from_")


def _fields_whose_alias_differs() -> dict[str, str]:
    """Contract field identifier -> Python attribute name, for every bound field whose alias differs.

    A field's contract identifier ends in its serialization ALIAS.
    """
    differing: dict[str, str] = {}
    for kind_id, cls in _bound_classes().items():
        for attribute, info in cls.model_fields.items():
            alias = info.alias or attribute
            if alias != attribute:
                differing[f"field:{kind_id.removeprefix('kind:')}.{alias}"] = attribute
    return differing


def test_field_names_covers_every_alias_that_differs() -> None:
    """Schema to binding: an aliased field with no row fails, since the kernel would read the wrong attribute."""
    assert load_binding()["fieldNames"] == _fields_whose_alias_differs()


def test_every_field_names_row_names_a_field_that_really_differs() -> None:
    """Binding to schema: a row for a field that does not differ fails, so the map is no general rename table."""
    resolved = _bound_classes()
    for field_id, python_name in load_binding()["fieldNames"].items():
        layer, kind, alias = field_id.removeprefix("field:").split(".")
        cls = resolved.get(f"kind:{layer}.{kind}")
        assert cls is not None, f"{field_id}: names a kind the binding does not bind"
        assert python_name in cls.model_fields, f"{field_id}: {cls.__name__} has no attribute {python_name!r}"
        info = cls.model_fields[python_name]
        assert info.alias == alias, f"{field_id}: {cls.__name__}.{python_name} is aliased {info.alias!r}, not {alias!r}"
        assert info.alias != python_name, f"{field_id}: alias and attribute agree; the row states nothing"


def test_the_one_known_differing_alias_is_declared() -> None:
    """`Connector.from_` is `field:connectors.connector.from` (`from` is a Python keyword), by name."""
    field_id, python_name = KNOWN_DIFFERING_FIELD
    assert load_binding()["fieldNames"].get(field_id) == python_name


# ---------------------------------------------------------------------------
# `snapshotAttributes`: the legacy bridge, declared as one
# ---------------------------------------------------------------------------


def test_snapshot_attributes_cover_every_layer_exactly_once() -> None:
    """One attribute per declared layer, and no others, compared as sets.

    The key order is the ``ModelSnapshot``/``ModelDiff`` field order, which
    ``test_the_bridges_key_order_is_the_adapter_field_order`` covers.
    """
    declared = [layer["id"] for layer in _layers()]
    bound = list(load_binding()["snapshotAttributes"])
    assert sorted(bound) == sorted(declared), (
        f"layers only in the contract: {sorted(set(declared) - set(bound))}; "
        f"layers only in the binding: {sorted(set(bound) - set(declared))}"
    )
    assert len(bound) == len(set(bound)), f"a layer bound twice: {bound}"


def test_the_bridges_key_order_is_the_adapter_field_order() -> None:
    """The bridge's key order is the adapters' field order, independent of ``displayOrder``.

    ``ModelSnapshot`` and ``ModelDiff`` generate their fields in it and older
    consumers read them positionally; keeping it here leaves ``displayOrder``
    free to change.
    """
    import dataclasses

    from swcraft_tools.differ import ModelDiff
    from swcraft_tools.snapshot import ModelSnapshot

    bound = list(load_binding()["snapshotAttributes"].values())
    displayed = [
        load_binding()["snapshotAttributes"][layer["id"]]
        for layer in sorted(_layers(), key=lambda layer: layer["displayOrder"])
    ]

    assert [field.name for field in dataclasses.fields(ModelDiff)] == bound
    assert [field.name for field in dataclasses.fields(ModelSnapshot)][: len(bound)] == bound
    assert displayed != bound, "the two orders coincided again; one of the two declarations is stale"


def test_snapshot_attributes_match_the_model_snapshot_vocabulary() -> None:
    """Each attribute is the one the kernel registry records, and it exists on `ModelSnapshot`.

    `LayerFacts.snapshot_attribute` is `None` for a layer the binding gives no
    row, so a dropped row fails here instead of taking a plausible default.
    """
    import dataclasses

    from swcraft_tools.model_registry import production_registry
    from swcraft_tools.snapshot import ModelSnapshot

    expected = {
        f"layer:{layer.name}": layer.snapshot_attribute
        for layer in production_registry().layers
        if layer.snapshot_attribute is not None
    }
    bound = load_binding()["snapshotAttributes"]
    assert bound == expected
    attributes = {field.name for field in dataclasses.fields(ModelSnapshot)}
    missing = sorted(attribute for attribute in bound.values() if attribute not in attributes)
    assert not missing, f"snapshot attributes ModelSnapshot does not declare: {missing}"


def test_only_the_swc_layer_needs_the_snapshot_bridge() -> None:
    """Every attribute but `swc` -> `swcs` is the layer's own name: the map's justification is one row."""
    diverging = {
        layer_id: attribute
        for layer_id, attribute in load_binding()["snapshotAttributes"].items()
        if attribute != layer_id.removeprefix("layer:")
    }
    assert diverging == {"layer:swc": "swcs"}


def test_the_snapshot_attribute_bridge_is_declared_deprecated() -> None:
    """The bridge is declared deprecated machine-readably, by a reason and nothing else."""
    deprecations = load_binding()["deprecations"]
    assert "snapshotAttributes" in deprecations, f"the legacy bridge is not declared deprecated: {deprecations}"
    assert set(deprecations["snapshotAttributes"]) == {"reason"}
    assert deprecations["snapshotAttributes"]["reason"].strip()


def test_no_binding_section_other_than_the_bridge_is_deprecated() -> None:
    """Only the snapshot bridge is deprecated: the other sections are what a binding is for."""
    assert sorted(load_binding()["deprecations"]) == ["snapshotAttributes"]


# ---------------------------------------------------------------------------
# `rules`: gated in both directions
# ---------------------------------------------------------------------------


def test_rule_bindings_and_declared_rules_are_the_same_set() -> None:
    """Every declared rule has a binding and every binding names a declared rule."""
    declared = {rule["id"] for rule in load_contract()["rules"]}
    bound = set(load_binding()["rules"])
    assert bound == declared, (
        f"rules only in the contract: {sorted(declared - bound)}; rules only in the binding: {sorted(bound - declared)}"
    )


# ---------------------------------------------------------------------------
# Entry expansion
# ---------------------------------------------------------------------------


def test_expand_entries_counts_the_shipped_contract() -> None:
    """The four entry sets have the sizes the contract's declarations give, derived from the document.

    Counts, because the conformance gate pins each entry's identity; this
    asserts the expansion reaches all of them and invents none.
    """
    contract = load_contract()
    kinds = [kind for layer in contract["layers"] for kind in layer["kinds"]]
    assert len(expand_entries(contract, "layer")) == len(contract["layers"])
    assert len(expand_entries(contract, "kind")) == len(kinds)
    assert len(expand_entries(contract, "field")) == sum(len(kind.get("fields", ())) for kind in kinds)
    assert len(expand_entries(contract, "rule")) == len(contract["rules"])


@pytest.mark.parametrize("granularity", GRANULARITIES)
def test_expand_entries_returns_unique_sorted_prefixed_identifiers(granularity: str) -> None:
    entries = expand_entries(load_contract(), granularity)
    assert list(entries) == sorted(entries)
    assert len(set(entries)) == len(entries)
    assert all(entry.startswith(f"{granularity}:") for entry in entries)


def test_expand_entries_refuses_an_unknown_granularity() -> None:
    """An unknown granularity is refused, never answered with an empty tuple that passes completeness checks."""
    with pytest.raises(ValueError, match="unknown entry granularity"):
        expand_entries(load_contract(), "collection")


def test_expand_entries_reaches_every_declared_field_including_the_placement_one() -> None:
    """`field:swc.swc.group`, with no `model_fields` counterpart, is still an entry real consumers cover."""
    assert "field:swc.swc.group" in expand_entries(load_contract(), "field")


# ---------------------------------------------------------------------------
# The migration inventory
# ---------------------------------------------------------------------------


#: The deleted read-only registry/legacy parity gate, by repository-relative
#: path: :func:`test_the_legacy_parity_gate_is_gone_and_no_python_module_imports_it`
#: refuses both the file and an import of it.
DELETED_PARITY_GATE = "packages/tools/tests/test_editing_registry_legacy_parity.py"

#: The roots that import scan walks: every project's sources and tests, since
#: the other test trees reach `packages/tools/tests/` on `sys.path` and a
#: production module importing a test module would be worst of all.
_IMPORT_SCAN_ROOTS = (
    "packages/tools/src",
    "packages/tools/tests",
    "packages/mcp/src",
    "packages/mcp/tests",
    "packages/agent/src",
    "packages/agent/tests",
)


def test_the_legacy_parity_gate_is_gone_and_no_python_module_imports_it() -> None:
    """The deleted parity gate is absent and no module of the three projects imports it, in either import form.

    A surviving import would fail at *collection*, a whole-suite failure with
    no useful name on it.

    **What it does not cover:** prose. The name still appears on purpose in
    decision records, completed plans and `migration-inventory.json`'s
    denominator `meaning` fields; nothing gates a stale mention.
    """
    assert not (REPO_ROOT / DELETED_PARITY_GATE).exists(), (
        f"{DELETED_PARITY_GATE} is back. It was deleted at denominator zero; "
        "reinstating it means reinstating its denominators and its self-tests with it"
    )

    module = DELETED_PARITY_GATE.rsplit("/", 1)[-1].removesuffix(".py")
    scanned: dict[str, int] = {}
    importers: list[str] = []
    for root in _IMPORT_SCAN_ROOTS:
        paths = sorted((REPO_ROOT / root).rglob("*.py"), key=lambda path: path.as_posix())
        scanned[root] = len(paths)
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(alias.name == module for alias in node.names):
                    importers.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom) and node.module == module:
                    importers.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")

    assert importers == [], f"modules importing the deleted parity gate: {importers}"
    # Per root, not in total, so a moved package cannot drop its root out of
    # the scan silently. The floor is two, so a root left holding a lone
    # `__init__.py` fails too.
    assert all(count >= 2 for count in scanned.values()), f"a scan root globbed to almost nothing: {scanned}"


# ---------------------------------------------------------------------------
# Instance identity
# ---------------------------------------------------------------------------

_SELECTOR_KINDS = (
    ("tasks", "task", "runnables", "task_runnable_ref"),
    ("sequences", "sequence", "steps", "sequence_step"),
)

#: (rule, the check that reports it, the function of the check's module that mints it -- the check itself
#: or one of its phases, which the check reaches).
_INSTANCE_RULES = (
    ("rule:task.resolved-duplicate-runnable", "check_task_consistency", "_task_entry_records"),
    ("rule:task.instance-ambiguous", "check_task_consistency", "_task_entry_findings"),
    ("rule:task.instance-unknown", "check_task_consistency", "_task_entry_findings"),
    ("rule:task.instance-not-a-component", "check_task_consistency", "_task_entry_findings"),
    ("rule:sequence.resolved-duplicate-step", "check_sequence_consistency", "check_sequence_consistency"),
    ("rule:sequence.step-instance-ambiguous", "check_sequence_consistency", "check_sequence_consistency"),
    ("rule:sequence.step-instance-unknown", "check_sequence_consistency", "check_sequence_consistency"),
    ("rule:sequence.step-instance-not-a-component", "check_sequence_consistency", "check_sequence_consistency"),
)


def _field_decl(contract: dict[str, Any], field_id: str) -> dict[str, Any]:
    for layer in contract["layers"]:
        for kind in layer["kinds"]:
            for field in kind["fields"]:
                if field["id"] == field_id:
                    return field
    raise KeyError(field_id)


@pytest.mark.parametrize(("layer", "owner", "collection", "member"), _SELECTOR_KINDS)
def test_the_collection_identity_is_the_triple(layer: str, owner: str, collection: str, member: str) -> None:
    field = _field_decl(load_contract(), f"field:{layer}.{owner}.{collection}")
    assert field["collection"]["memberIdentity"] == "composite"
    assert field["collection"]["identityFields"] == ["swc", "instance", "runnable"]


@pytest.mark.parametrize(("layer", "owner", "collection", "member"), _SELECTOR_KINDS)
def test_both_selectors_are_optional_immutable_and_only_swc_is_a_reference(
    layer: str, owner: str, collection: str, member: str
) -> None:
    contract = load_contract()
    swc = _field_decl(contract, f"field:{layer}.{member}.swc")
    instance = _field_decl(contract, f"field:{layer}.{member}.instance")
    assert (swc["required"], swc["immutable"], swc["default"]) == (False, True, None)
    assert swc["reference"] == {"layer": "swc", "identityField": "name"}
    assert (instance["required"], instance["immutable"], instance["default"]) == (False, True, None)
    assert "reference" not in instance
    assert _field_decl(contract, f"field:{layer}.{member}.runnable")["required"] is True


@pytest.mark.parametrize(("layer", "owner", "collection", "member"), _SELECTOR_KINDS)
def test_the_selector_rule_is_element_local_on_the_kind_and_bound_to_its_model_validator(
    layer: str, owner: str, collection: str, member: str
) -> None:
    rule_id = f"rule:{layer}.{member}.selector-exactly-one"
    (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == rule_id]
    assert (rule["family"], rule["appliesTo"]) == ("element-local", [f"kind:{layer}.{member}"])
    assert load_binding()["rules"][rule_id] == {"kind": f"kind:{layer}.{member}", "method": "exactly_one_selector"}


@pytest.mark.parametrize(("rule_id", "owner", "bound"), _INSTANCE_RULES)
def test_every_instance_rule_has_an_owner_and_the_validators_summary(rule_id: str, owner: str, bound: str) -> None:
    from swcraft_tools.validator import CONSISTENCY_RULES

    (rule,) = [rule for rule in load_contract()["rules"] if rule["id"] == rule_id]
    assert rule["family"] == "cross-element"
    defining = vars(importlib.import_module("swcraft_tools.validator"))[owner].__module__
    assert load_binding()["rules"][rule_id] == {"module": defining, "callable": bound}
    assert bound in validator_package.reached_functions(importlib.import_module(defining), owner)
    assert CONSISTENCY_RULES[rule_id.removeprefix("rule:")] == rule["summary"]
