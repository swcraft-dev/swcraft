"""Pure loaders, projections and checkers for the model-contract artifacts.

* **Canonical, normative package data** —
  ``packages/tools/src/swcraft_tools/model_contract/``: ``contract.schema.json``,
  ``model-contract.json``, ``binding.schema.json``, ``binding.json``. They sit
  under the package because the kernel loads these exact bytes from an installed
  wheel (``swcraft_tools.contract``, through ``importlib.resources``).
* **Design-time evidence** — the consumer inventory, the coverage declarations
  and the migration inventory are closed records whose loaders are not in this
  tree; the synthetic-extension fixtures are open test data under
  ``packages/tools/tests/fixtures/contracts/``.

**The loaders resolve every path from ``conftest.REPO_ROOT``**, never through
``swcraft_tools.__file__``: the conformance gate reads the contract through them
before comparing it with the production classes, and a loader that went through
the import path under test would make an installed-versus-checkout mismatch
invisible.

**The structural projection deliberately imports production code**, and only
that half of the module does: ``editing/registry.py::_type_name`` and
``editing/registry.py::_scalar_type_name`` produce the exact type strings the
shipped ``discover`` vectors publish, so a normalization of this module's own
could be structurally right and still change published output. The projection
calls the derivation instead of reproducing it.
"""

from __future__ import annotations

import copy
import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, get_args, get_origin

from conftest import REPO_ROOT
from jsonschema import Draft202012Validator
from pydantic import BaseModel
from swcraft_tools import contract as contract_module
from swcraft_tools.editing.registry import (
    _kind_name,
    _literal_args,
    _model_members,
    _strip_optional,
    _type_name,
    _union_discriminator,
)

# ---------------------------------------------------------------------------
# Where the artifacts live
# ---------------------------------------------------------------------------

#: The canonical, normative contract files, as package data.
CONTRACT_PACKAGE_ROOT = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "model_contract"

#: The four normative files :data:`CONTRACT_PACKAGE_ROOT` holds. Named rather
#: than derived, so a file deleted from both the source tree and the wheel at
#: once is still a failure. Shared by the packaging guard and the extension guard.
NORMATIVE_CONTRACT_FILES: tuple[str, ...] = (
    "contract.schema.json",
    "model-contract.json",
    "binding.schema.json",
    "binding.json",
)


def contract_path(name: str) -> Path:
    """The path of one canonical package-data contract file.

    Resolved from the repository root, for the reason in the module docstring.
    The file need not exist, so a failure names the file the caller asked for.
    """
    return CONTRACT_PACKAGE_ROOT / name


def _read_json(path: Path) -> Any:
    """One JSON artifact, with a failure that names the file rather than the offset."""
    if not path.exists():
        raise FileNotFoundError(f"model-contract artifact not authored yet: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Contract-version compatibility
# ---------------------------------------------------------------------------

# The version guard (an unknown major is refused, never partially read) lives
# with the production reader and is re-exported, so there is exactly one
# supported-major set. Only the guard is imported: path resolution stays on
# ``conftest.REPO_ROOT``; ``packages/tools/tests/test_model_contract_loader.py``
# compares the two readers.
SUPPORTED_CONTRACT_MAJORS = contract_module.SUPPORTED_CONTRACT_MAJORS
UnsupportedContractVersion = contract_module.UnsupportedContractVersion
require_supported_contract_version = contract_module.require_supported_contract_version


# ---------------------------------------------------------------------------
# The loaders
# ---------------------------------------------------------------------------


def load_meta_schema() -> dict[str, Any]:
    """``contract.schema.json`` — the meta-schema ``model-contract.json`` is held to."""
    return _read_json(contract_path("contract.schema.json"))


def load_contract() -> dict[str, Any]:
    """``model-contract.json`` — the normative declaration itself.

    The ``contractVersion`` major is checked before the document is handed
    back, so a contract this repository cannot read is refused.
    """
    document = _read_json(contract_path("model-contract.json"))
    require_supported_contract_version(document)
    return document


def load_binding_schema() -> dict[str, Any]:
    """``binding.schema.json`` — the shape of the one file that names Python."""
    return _read_json(contract_path("binding.schema.json"))


def load_binding() -> dict[str, Any]:
    """``binding.json`` — contract identifiers to runtime Python symbols."""
    return _read_json(contract_path("binding.json"))


# ---------------------------------------------------------------------------
# Validators over the meta-schema
# ---------------------------------------------------------------------------


def meta_schema_validator() -> Draft202012Validator:
    """A validator for a whole ``model-contract.json`` document."""
    return Draft202012Validator(load_meta_schema())


def shape_validator(schema: Mapping[str, Any], def_name: str, *, source: str) -> Draft202012Validator:
    """A validator for one ``$defs`` shape of *schema*, in isolation.

    Lets a perturbation target a single declaration shape, so the failure names
    the shape that rejected it. The whole ``$defs`` block travels with the
    fragment because the shapes reference one another; no schema declares a
    cross-file ``$ref``. *source* names the file when the shape is unknown.
    """
    definitions = schema["$defs"]
    if def_name not in definitions:
        raise KeyError(f"{source} declares no $defs/{def_name}")
    return Draft202012Validator({"$defs": definitions, "$ref": f"#/$defs/{def_name}"})


def fragment_validator(def_name: str) -> Draft202012Validator:
    """One ``$defs`` shape of the meta-schema — :func:`shape_validator` bound to it."""
    return shape_validator(load_meta_schema(), def_name, source="contract.schema.json")


def binding_shape_validator(def_name: str) -> Draft202012Validator:
    """One ``$defs`` shape of ``binding.schema.json``."""
    return shape_validator(load_binding_schema(), def_name, source="binding.schema.json")


def validation_messages(validator: Draft202012Validator, instance: Any) -> list[str]:
    """Every validation message for *instance*, in document order.

    Returned rather than raised so a test can assert on the whole set: a
    perturbation rejected for an unrelated reason proves nothing.
    """
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(instance), key=lambda error: list(error.absolute_path))
    ]


# ---------------------------------------------------------------------------
# The structural projection: everything a bound class states about itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldProjection:
    """One bound field of one bound class, reduced to its structural facts.

    Alias, requiredness, normalized type shape, default and discriminator. The
    facts a class cannot state about itself (``immutable``, ``description``,
    ``unit``, ``reference``, a collection's member identity) are absent: they are
    compared against ``editing/registry.py``'s declared tables instead.

    ``has_default`` is carried because ``None`` is a legal declared default; the
    contract states the difference by the presence of the ``default`` key.
    """

    name: str
    type: str
    required: bool
    has_default: bool
    default: Any = None
    enum: tuple[Any, ...] = ()
    element_kind: str | None = None
    collection_kind: str | None = None
    member_kinds: tuple[str, ...] = ()
    discriminator_field: str | None = None


def bound_classes() -> dict[str, type[BaseModel]]:
    """Every contract kind identifier resolved through ``binding.json``.

    Shared by the conformance gate and the wiring module, which reads the
    classes' Pydantic decorators.
    """
    resolved: dict[str, type[BaseModel]] = {}
    for kind_id, binding in load_binding()["kinds"].items():
        module = importlib.import_module(binding["module"])
        resolved[kind_id] = getattr(module, binding["class"])
    return resolved


def structural_projection(
    model_class: type[BaseModel],
    kind_names: Mapping[type[BaseModel], str],
) -> dict[str, FieldProjection]:
    """Project *model_class*'s ``model_fields`` into the contract's vocabulary.

    Keyed by the serialization alias, the name the model file, the editing
    protocol and the contract all use (``Connector.from_`` projects as
    ``from``). ``_type_name`` / ``_scalar_type_name`` are called rather than
    reproduced, for the reason in the module docstring.

    *kind_names* maps a bound class to the contract's bare kind name; passed in
    so the projection can run over fixture classes the registry has never seen.
    """
    projection: dict[str, FieldProjection] = {}
    for attribute, info in model_class.model_fields.items():
        name = info.alias or attribute
        annotation = info.annotation
        stripped = _strip_optional(annotation)
        origin = get_origin(stripped)

        collection_kind: str | None = None
        member_kinds: tuple[str, ...] = ()
        discriminator_field: str | None = None
        element_kind: str | None = None
        if origin is list:
            collection_kind = "list"
            (inner,) = get_args(stripped)
            members = _model_members(inner)
            member_kinds = tuple(_kind_name(member, dict(kind_names)) for member in members)
            discriminator_field = _union_discriminator(members)
        elif origin is dict:
            collection_kind = "map"
        else:
            object_members = _model_members(stripped)
            if object_members:
                element_kind = _kind_name(object_members[0], dict(kind_names))

        required = info.is_required()
        projection[name] = FieldProjection(
            name=name,
            type=_type_name(annotation, dict(kind_names)),
            required=required,
            has_default=not required,
            default=None if required else info.get_default(call_default_factory=True),
            enum=_literal_args(stripped),
            element_kind=element_kind,
            collection_kind=collection_kind,
            member_kinds=member_kinds,
            discriminator_field=discriminator_field,
        )
    return projection


# ---------------------------------------------------------------------------
# The bidirectional structural comparison
# ---------------------------------------------------------------------------


def contract_kind_names(
    contract: Mapping[str, Any],
    kind_classes: Mapping[str, type[BaseModel]],
) -> dict[type[BaseModel], str]:
    """Bound class -> the contract's bare kind name, for the whole contract.

    Built from the contract and the supplied binding rather than from
    ``editing/registry.py``'s projection, so the names ``_type_name`` renders
    into ``list[port]`` are the contract's own. A kind with no supplied class
    contributes nothing here; :func:`structural_conformance_failures` reports it.
    """
    names: dict[type[BaseModel], str] = {}
    for layer in contract["layers"]:
        for kind in layer["kinds"]:
            model_class = kind_classes.get(kind["id"])
            if model_class is not None:
                names[model_class] = kind["kind"]
    return names


def comparison_fields(kind_decl: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The declared fields the bidirectional comparison covers, keyed by name.

    Contract fields minus those flagged ``placement`` (``field:swc.swc.group``,
    which has no ``model_fields`` counterpart by design). The exclusion is kept
    narrow by :func:`kind_conformance_failures`, which refuses a flagged field
    the bound class declares, and by :func:`placement_failures`.
    """
    return {field["name"]: field for field in kind_decl.get("fields", []) if not field.get("placement", False)}


def _same_scalar(declared: Any, projected: Any) -> bool:
    """Whether a JSON scalar and a Python one are the same value.

    ``==`` alone would match a contract ``0`` with a ``False`` default.
    Integer-versus-float equality is kept: JSON has one number type.
    """
    if isinstance(declared, bool) != isinstance(projected, bool):
        return False
    return bool(declared == projected)


def field_fact_failures(
    kind_id: str,
    field_decl: Mapping[str, Any],
    projected: FieldProjection,
) -> list[str]:
    """Every disagreement between one declared field and its projection.

    Only the facts the projection carries; ``immutable``, ``description``,
    ``unit`` and ``reference`` are compared against ``editing/registry.py``'s
    declared tables elsewhere.
    """
    failures: list[str] = []
    field_id = field_decl.get("id", f"{kind_id}.{field_decl['name']}")

    if field_decl["type"] != projected.type:
        failures.append(
            f"{field_id}: the contract declares type {field_decl['type']!r}, the class projects {projected.type!r}"
        )
    if field_decl["required"] != projected.required:
        failures.append(
            f"{field_id}: the contract declares required={field_decl['required']}, "
            f"the class projects required={projected.required}"
        )

    declared_default = "default" in field_decl
    if declared_default != projected.has_default:
        failures.append(
            f"{field_id}: the contract "
            f"{'declares a default' if declared_default else 'declares no default'}, "
            f"the class {'has one' if projected.has_default else 'has none'}"
        )
    elif declared_default and not _same_scalar(field_decl["default"], projected.default):
        failures.append(
            f"{field_id}: the contract declares default {field_decl['default']!r}, "
            f"the class projects {projected.default!r}"
        )

    declared_enum = tuple(field_decl.get("enum", ()))
    if len(declared_enum) != len(projected.enum) or not all(
        _same_scalar(a, b) for a, b in zip(declared_enum, projected.enum, strict=True)
    ):
        failures.append(
            f"{field_id}: the contract declares enum {list(declared_enum)!r}, "
            f"the class projects {list(projected.enum)!r} (order is part of the contract)"
        )

    if field_decl.get("elementKind") != projected.element_kind:
        failures.append(
            f"{field_id}: the contract declares elementKind {field_decl.get('elementKind')!r}, "
            f"the class projects {projected.element_kind!r}"
        )

    collection = field_decl.get("collection")
    if (collection is None) != (projected.collection_kind is None):
        failures.append(
            f"{field_id}: the contract "
            f"{'declares a collection' if collection is not None else 'declares no collection'}, "
            f"the class projects {projected.collection_kind!r}"
        )
    elif collection is not None:
        if collection["kind"] != projected.collection_kind:
            failures.append(
                f"{field_id}: the contract declares a {collection['kind']!r} collection, "
                f"the class projects {projected.collection_kind!r}"
            )
        declared_members = tuple(collection.get("memberKinds", ()))
        if declared_members != projected.member_kinds:
            failures.append(
                f"{field_id}: the contract declares member kinds {list(declared_members)!r}, "
                f"the class projects {list(projected.member_kinds)!r}"
            )
        if collection.get("discriminatorField") != projected.discriminator_field:
            failures.append(
                f"{field_id}: the contract declares collection discriminator "
                f"{collection.get('discriminatorField')!r}, the class projects {projected.discriminator_field!r}"
            )
    return failures


def kind_conformance_failures(
    kind_id: str,
    kind_decl: Mapping[str, Any],
    model_class: type[BaseModel],
    kind_names: Mapping[type[BaseModel], str],
) -> list[str]:
    """The bidirectional field comparison for one kind, in document order.

    A flagged ``placement`` field the bound class declares is refused outright:
    a placement field exists because the schema has none, and otherwise
    ``placement: true`` would hide any field from the gate. There is no vacuous
    mode: an empty contract ``fields`` array fails naming every class field.
    """
    projection = structural_projection(model_class, kind_names)
    declared = {field["name"]: field for field in kind_decl.get("fields", [])}
    compared = comparison_fields(kind_decl)
    flagged = set(declared) - set(compared)

    failures: list[str] = []
    for name in sorted(flagged & set(projection)):
        failures.append(
            f"{kind_id}: field '{name}' is flagged placement, but {model_class.__name__}.model_fields declares it; "
            "a placement field exists precisely because the schema has none"
        )
    for name in sorted(set(compared) - set(projection)):
        failures.append(
            f"{kind_id}: the contract declares field '{name}', which {model_class.__name__}.model_fields does not"
        )
    for name in sorted(set(projection) - set(compared) - flagged):
        failures.append(
            f"{kind_id}: {model_class.__name__}.model_fields declares '{name}', which the contract does not"
        )
    for field in kind_decl.get("fields", []):
        name = field["name"]
        if name in compared and name in projection:
            failures.extend(field_fact_failures(kind_id, field, projection[name]))
    return failures


def placement_failures(contract: Mapping[str, Any]) -> list[str]:
    """The placement fact, stated in three places, tied together.

    ``layerDecl.storage.nesting``, ``layerDecl.placement`` and the ``fieldDecl``
    flagged ``placement``: the layer that nests declares a placement, the
    placement names the kind and the field of the one flagged ``fieldDecl``, and
    the layer-level ``type`` and ``description`` are that field's own.
    """
    failures: list[str] = []
    for layer in contract["layers"]:
        placement = layer.get("placement")
        nesting = layer["storage"]["nesting"]
        flagged = [
            (kind, field)
            for kind in layer["kinds"]
            for field in kind.get("fields", [])
            if field.get("placement", False)
        ]

        if placement is None:
            if nesting != "flat":
                failures.append(
                    f"{layer['id']}: storage.nesting is {nesting!r}, but the layer declares no placement field"
                )
            for _, field in flagged:
                failures.append(
                    f"{field['id']}: flagged placement, but layer '{layer['name']}' declares no placement field"
                )
            continue

        if nesting != placement["field"]:
            failures.append(
                f"{layer['id']}: storage.nesting is {nesting!r}, but the layer's placement field is "
                f"{placement['field']!r}"
            )
        if len(flagged) != 1:
            failures.append(
                f"{layer['id']}: declares the placement field '{placement['kind']}.{placement['field']}', but "
                f"{len(flagged)} of its fields are flagged placement; the exclusion is exactly one field wide"
            )
        for kind, field in flagged:
            if kind["kind"] != placement["kind"]:
                failures.append(
                    f"{field['id']}: flagged placement on kind '{kind['kind']}', but layer '{layer['name']}' "
                    f"declares its placement field on '{placement['kind']}'"
                )
            elif field["name"] != placement["field"]:
                failures.append(
                    f"{field['id']}: flagged placement, but layer '{layer['name']}' declares its placement field "
                    f"as '{placement['field']}'"
                )
            else:
                if placement["type"] != field["type"]:
                    failures.append(
                        f"{field['id']}: the layer declares placement type {placement['type']!r}, the field "
                        f"declares {field['type']!r}"
                    )
                if placement["description"] != field.get("description"):
                    failures.append(
                        f"{field['id']}: the layer's placement description differs from the field's own; the "
                        "contract must state the placement fact once, not twice"
                    )
    return failures


def member_of_failures(contract: Mapping[str, Any]) -> list[str]:
    """``memberOf`` re-derived from the contract's own collections and element kinds.

    A kind is a member of exactly the fields that hold it
    (``collection.memberKinds`` for a list or map, ``elementKind`` for a single
    one), so the declared ``memberOf`` restates a derivable fact. It stays
    declared, because it is published contract; this ties it to the derivation,
    pair for pair. Order is compared, not just membership:
    ``connector_endpoint`` is a member of ``connector.from`` and ``connector.to``,
    and which one a client reads first is published protocol output.
    """
    derived: dict[str, list[dict[str, str]]] = {}
    for layer in contract["layers"]:
        for kind in layer["kinds"]:
            for field in kind.get("fields", []):
                collection = field.get("collection") or {}
                members = collection.get("memberKinds") or ([field["elementKind"]] if field.get("elementKind") else [])
                for member in members:
                    derived.setdefault(member, []).append({"kind": kind["kind"], "field": field["name"]})

    failures: list[str] = []
    for layer in contract["layers"]:
        for kind in layer["kinds"]:
            declared = kind.get("memberOf")
            expected = derived.get(kind["kind"])
            if declared != expected:
                failures.append(
                    f"{kind['id']}: declares memberOf={declared!r}, but the contract's own collections and "
                    f"element kinds put it in {expected!r}"
                )
    return failures


def structural_conformance_failures(
    contract: Mapping[str, Any],
    kind_classes: Mapping[str, type[BaseModel]],
) -> list[str]:
    """Every structural disagreement between *contract* and the bound classes.

    Returned as a list so a test can assert on the whole set; an empty list is
    conformance, and every entry names the contract identifier or class it is
    about. Both directions of the kind set are checked as well as of every
    kind's field set: a kind missing a binding would leave its fields unexamined.
    """
    failures: list[str] = []
    kind_names = contract_kind_names(contract, kind_classes)
    declared_ids = {kind["id"] for layer in contract["layers"] for kind in layer["kinds"]}

    for kind_id in sorted(set(kind_classes) - declared_ids):
        failures.append(f"{kind_id}: bound to {kind_classes[kind_id].__name__}, but the contract declares no such kind")
    for layer in contract["layers"]:
        for kind in layer["kinds"]:
            model_class = kind_classes.get(kind["id"])
            if model_class is None:
                failures.append(f"{kind['id']}: the contract declares the kind, but no class is bound to it")
                continue
            failures.extend(kind_conformance_failures(kind["id"], kind, model_class, kind_names))
    failures.extend(placement_failures(contract))
    failures.extend(member_of_failures(contract))
    return failures


# ---------------------------------------------------------------------------
# Entry expansion: what a coverage declaration is complete over
# ---------------------------------------------------------------------------

#: The four entry granularities the contract is addressable at, in widening
#: order; a consumer declares which of them apply to it.
GRANULARITIES: tuple[str, ...] = ("layer", "kind", "field", "rule")


def expand_entries(contract: Mapping[str, Any], granularity: str) -> tuple[str, ...]:
    """Every contract entry identifier at *granularity*, sorted.

    Sorted, so a gap list is stable across runs. A ``collection`` is not a
    fifth granularity: it is declared on the field that holds it, so expanding it
    would give one fact two dispositions.

    Raises:
        ValueError: *granularity* is not one of :data:`GRANULARITIES` (an empty
            answer would let a misspelling pass every completeness check).
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"unknown entry granularity {granularity!r} (expected one of {list(GRANULARITIES)})")
    if granularity == "rule":
        return tuple(sorted(rule["id"] for rule in contract["rules"]))
    if granularity == "layer":
        return tuple(sorted(layer["id"] for layer in contract["layers"]))
    kinds = [kind for layer in contract["layers"] for kind in layer["kinds"]]
    if granularity == "kind":
        return tuple(sorted(kind["id"] for kind in kinds))
    return tuple(sorted(field["id"] for kind in kinds for field in kind.get("fields", ())))


def parent_entry(entry: str) -> str | None:
    """The entry one granularity up from *entry*, or ``None`` if it has none.

    ``field:swc.swc.ports`` is owned by ``kind:swc.swc``, which is owned by
    ``layer:swc``. Read off the identifier, which is built from the ownership;
    a closed check asserts the two agree over the whole shipped contract.
    ``layer`` has no parent, and
    neither has ``rule``: ``appliesTo`` names a set of entries, so the kind
    rollup (:func:`coverage_gaps`) does not reach rules.
    """
    prefix, _, path = entry.partition(":")
    if prefix == "field":
        return f"kind:{path.rsplit('.', 1)[0]}"
    if prefix == "kind":
        return f"layer:{path.rsplit('.', 1)[0]}"
    return None


# ---------------------------------------------------------------------------
# The completeness checker
# ---------------------------------------------------------------------------

#: The consumer declares no disposition for an entry it is applicable to: a
#: `partial` consumer owes a row for every applicable entry, with no default and
#: no wildcard-except form, so silence is a gap rather than an exclusion.
GAP_MISSING_DISPOSITION = "missing-disposition"

#: The consumer declares a disposition keyed by something that is not a contract
#: entry at any granularity it declared, such as a row surviving a renamed entry.
GAP_UNKNOWN_ENTRY = "unknown-entry"

#: A whole granularity the consumer declared is owed rather than populated. Its
#: own kind: as a :data:`GAP_MISSING_DISPOSITION` it would make the "no missing
#: dispositions" assertion unsatisfiable, and as silence it would be an implicit
#: omission.
GAP_DEFERRED = "deferred"

#: A ``delegated`` row names a consumer that does not resolve to ``supported``
#: at the same entry: it has no declaration, declares nothing or
#: ``not-applicable`` there, or its own row is itself ``delegated``. The last is
#: the non-transitivity refusal, which also refuses a cycle: ``a -> b -> a`` is
#: reported once from each side, without a cycle search.
GAP_BROKEN_DELEGATION = "broken-delegation"

#: A row says ``supported`` while the row that owns it — its kind, or that
#: kind's layer — says something else: two answers to one question. Not raised
#: for a child that agrees with its parent, which is extra rather than wrong.
GAP_PARENT_CONTRADICTION = "parent-contradiction"


class CoverageGap(NamedTuple):
    """One consumer's one gap: ``(consumer, entry, kind)``, plus a delegate.

    A tuple because the sort order is ``(consumer, entry, kind)``, so a gap list
    is stably ordered and diffable.

    ``entry`` is a contract entry identifier, or a bare granularity name
    (``field``, ``rule``) for :data:`GAP_DEFERRED`, owed for the granularity as a
    whole; an identifier always carries its ``<granularity>:`` prefix.

    ``delegate`` is the consumer a :data:`GAP_BROKEN_DELEGATION` row pointed at,
    ``""`` otherwise. A separate field, because ``entry`` is compared against
    entry identifiers; defaulted, so a three-argument construction still builds
    an equal tuple. :func:`format_gaps` prints it only when it is set.
    """

    consumer: str
    entry: str
    kind: str
    delegate: str = ""


def resolve_disposition(
    contract: Mapping[str, Any],
    coverage_declarations: Mapping[str, Mapping[str, Any]],
    consumer: str,
    entry: str,
) -> str | None:
    """*consumer*'s status at *entry*, or ``None`` if it states none.

    * a **partial** consumer answers with its row's ``status``, or ``None``
      where it carries no row;
    * a **total** consumer answers ``"supported"`` at every granularity it
      declared and ``None`` outside them;
    * a consumer with **no declaration** answers ``None`` rather than raising, so
      a delegation to an undeclared slug is reported as a gap.

    It does **not** decide whether the answer is true of the code: it reads the
    declaration, and a ``supported`` row no call site backs is a review finding.
    """
    declaration = coverage_declarations.get(consumer)
    if declaration is None:
        return None
    if declaration.get("nature") == "total":
        for granularity in declaration.get("granularities", ()):
            if entry in expand_entries(contract, granularity):
                return "supported"
        return None
    row = declaration.get("dispositions", {}).get(entry)
    if row is None:
        return None
    status = row.get("status")
    return str(status) if status is not None else None


def coverage_gaps(
    contract: Mapping[str, Any],
    coverage_declarations: Mapping[str, Mapping[str, Any]],
) -> list[CoverageGap]:
    """Every coverage gap between *contract* and *coverage_declarations*, sorted.

    Pure: it reads nothing from disk, imports no production module and decides nothing about which gaps are
    acceptable. *coverage_declarations* is keyed by consumer slug, as :func:`load_coverage` returns it.

    Per declaration:

    * a ``total`` consumer contributes no rows;
    * a ``partial`` consumer's applicable set is :func:`expand_entries` over every granularity it declared, deferred
      ones included; each granularity is either deferred (one :data:`GAP_DEFERRED` row) or populated, and then every
      owed entry without a disposition is a :data:`GAP_MISSING_DISPOSITION`;
    * a ``field`` is owed a row only when the consumer's row at its owning ``kind:`` is ``supported``; under an
      unanswered kind it stays owed (fail-closed); the narrowing touches the owed set only;
    * a ``supported`` row whose answering parent (:func:`parent_entry`) says otherwise is a
      :data:`GAP_PARENT_CONTRADICTION`, at every granularity with a parent;
    * a ``delegated`` row whose ``to`` consumer does not answer ``supported`` at the same entry
      (:func:`resolve_disposition`) is one :data:`GAP_BROKEN_DELEGATION`; delegation is not transitive, a row at a
      deferred granularity is still resolved, and a row already an ``unknown-entry`` gap is not.

    It does **not** reach whether the delegate is actually called on this consumer's paths, nor whether a parent row
    is true of the code (a wrong ``not-applicable`` kind silences its fields too), nor ``rule`` entries.

    Raises:
        ValueError: a declaration names a granularity outside :data:`GRANULARITIES` (from :func:`expand_entries`),
            or declares ``field`` granularity without ``kind``.
    """
    gaps: list[CoverageGap] = []
    for consumer, declaration in coverage_declarations.items():
        if declaration.get("nature") != "partial":
            continue

        granularities = tuple(declaration.get("granularities", ()))
        deferred = set(declaration.get("deferredGranularities", {}))
        dispositions = set(declaration.get("dispositions", {}))

        if "field" in granularities and "kind" not in granularities:
            raise ValueError(
                f"{consumer} declares 'field' granularity without 'kind': a field entry is owed a "
                "row only when its owning kind resolves to 'supported' (the kind rollup), so with "
                "no kind granularity there is nothing to resolve the rollup against"
            )

        applicable: set[str] = set()
        for granularity in granularities:
            applicable.update(expand_entries(contract, granularity))

        for entry in dispositions - applicable:
            gaps.append(CoverageGap(consumer, entry, GAP_UNKNOWN_ENTRY))

        for granularity in granularities:
            if granularity in deferred:
                gaps.append(CoverageGap(consumer, granularity, GAP_DEFERRED))
                continue
            owed = set(expand_entries(contract, granularity))
            if granularity == "field":
                # Owed unless the owning kind answers with something other than
                # `supported`; an unanswered kind suppresses nothing, so a new
                # kind's fields stay visible until somebody dispositions the kind.
                owed = {
                    entry
                    for entry in owed
                    if resolve_disposition(contract, coverage_declarations, consumer, f"{parent_entry(entry)}")
                    in (None, "supported")
                }
            for entry in owed - dispositions:
                gaps.append(CoverageGap(consumer, entry, GAP_MISSING_DISPOSITION))

        for entry, row in declaration.get("dispositions", {}).items():
            if entry not in applicable:
                continue
            status = row.get("status")
            parent = parent_entry(entry)
            if status == "delegated":
                delegate = str(row.get("to", ""))
                if resolve_disposition(contract, coverage_declarations, delegate, entry) != "supported":
                    gaps.append(CoverageGap(consumer, entry, GAP_BROKEN_DELEGATION, delegate))
            elif status == "supported" and parent is not None:
                parent_status = resolve_disposition(contract, coverage_declarations, consumer, parent)
                if parent_status is not None and parent_status != "supported":
                    gaps.append(CoverageGap(consumer, entry, GAP_PARENT_CONTRADICTION))

    return sorted(gaps)


# ---------------------------------------------------------------------------
# The shipped-artifact expectations more than one test module states
# ---------------------------------------------------------------------------
#
# Expectations rather than loaders, shared here because a test module importing
# another makes a single disagreement fail twice under two names.

#: The eighteen consumers whose nature is `partial`; a consumer declared or
#: retired with that nature moves it.
PARTIAL_CONSUMERS = frozenset(
    {
        "allocation",
        "change-report",
        "data-arrival",
        "metrics",
        "needs-export",
        "passport",
        "requirements-import",
        "test-tables",
        "trace-search",
        "validator",
        "arxml-generation",
        "diagrams",
        "writer-elements",
        "resource-analysis",
        "arxml-import",
        "component-exchange",
        "arxml-reader",
        "resource-report",
    }
)

#: The four original ARXML-bound layers, in the order the identifier walk
#: reaches them — which is also the order
#: violations are reported in, so the tuple and not just its set is the oracle.
#: A fixed before-state: today's set is :data:`ARXML_BOUND_LAYER_NAMES`, which
#: adds :data:`ARXML_BOUND_LAYERS_ADDED_SINCE`.
#:
#: Written out rather than derived from ``layerDecl.arxmlBound``, which would be
#: the contract compared with itself; declared once, because one independent
#: oracle is the whole argument for writing it out.
ARXML_BOUND_LAYER_ORDER: tuple[str, ...] = ("swc", "interfaces", "connectors", "implementation_types")

#: The other ARXML-bound layers, in the order the derived walk reaches them
#: (the contract's layer list, filtered). Each declared name is emitted as a
#: ``SHORT-NAME``: ``COMPOSITION-SW-COMPONENT-TYPE``, ``DATA-TYPE-MAPPING-SET``,
#: ``SW-BASE-TYPE``, ``APPLICATION-*-DATA-TYPE``, ``UNIT``, ``COMPU-METHOD``,
#: ``DATA-CONSTR``. Kept apart from :data:`ARXML_BOUND_LAYER_ORDER` so that tuple
#: stays a fixed before-state rather than a guess.
ARXML_BOUND_LAYERS_ADDED_SINCE: tuple[str, ...] = (
    "compositions",
    "mapping_sets",
    "base_types",
    "application_types",
    "units",
    "compu_methods",
    "data_constraints",
)

#: The layers that are ARXML-bound TODAY, as a set, for the assertions that do
#: not care about order: the pre-migration four plus everything declared since.
#: Composed from the two independent tuples rather than read off
#: ``layerDecl.arxmlBound``, for the reason the first one gives — an expectation
#: derived from the flag could not tell a layer that silently stopped being
#: ARXML-bound from one that was never meant to be.
ARXML_BOUND_LAYER_NAMES = frozenset(ARXML_BOUND_LAYER_ORDER) | frozenset(ARXML_BOUND_LAYERS_ADDED_SINCE)

# ---------------------------------------------------------------------------
# The synthetic extension fixture
# ---------------------------------------------------------------------------

#: The synthetic-extension fixtures. Design-time evidence like the coverage
#: declarations beside them, and **test-process-only** on top of that: they are
#: merged into in-memory copies of the shipped artifacts and never written back.
SYNTHETIC_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "model-contract" / "fixtures"


def load_synthetic_layer() -> dict[str, Any]:
    """``fixtures/synthetic-layer.json`` — one added layer, as a contract fragment.

    It carries no ``contractVersion``; :func:`merged_contract` turns it into a contract.
    """
    return _read_json(SYNTHETIC_ROOT / "synthetic-layer.json")


def load_synthetic_coverage() -> dict[str, Any]:
    """``fixtures/synthetic-coverage.json`` — the deliberate dispositions.

    The rows a partial consumer gets for the synthetic layer, so the expected
    gap is a specific asserted list.
    """
    return _read_json(SYNTHETIC_ROOT / "synthetic-coverage.json")


def merged_contract() -> dict[str, Any]:
    """The shipped contract with the synthetic layer appended, **in memory only**.

    Deep-copied before the append, so a caller that mutates the result cannot
    reach a later :func:`load_contract`'s document; nothing is written to disk.

    Raises:
        ValueError: the fragment declares a layer the contract already has (a
            silent override would let the fixture change a shipped layer).
    """
    contract = copy.deepcopy(load_contract())
    declared = {layer["id"] for layer in contract["layers"]}
    for layer in load_synthetic_layer()["layers"]:
        if layer["id"] in declared:
            raise ValueError(f"the synthetic fragment redeclares a shipped layer: {layer['id']}")
        contract["layers"].append(copy.deepcopy(layer))
    return contract


def merged_binding() -> dict[str, Any]:
    """The shipped binding with ``kind:synthetic.synthetic`` bound to a test-local class.

    The companion of :func:`merged_contract`, so a kernel that refuses an
    unbound kind can be built over the pair. A re-export with the import inside
    the body: ``synthetic_support.py`` imports :func:`merged_contract` from here,
    so a module-level import would be a cycle.
    """
    from synthetic_support import merged_binding as _merged_binding

    return _merged_binding()
