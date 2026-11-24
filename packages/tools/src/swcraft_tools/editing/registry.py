"""The schema-derived editing registry: one description of the model.

Builds the records the protocol's layer, element-kind, field, reference and
collection descriptors publish: *derived* from ``schema.py`` where Pydantic knows
it, otherwise *declared*, projected from :mod:`swcraft_tools.model_registry`.

**Invariants.**

1. An annotation may only annotate: one naming a missing field raises
   :class:`RegistryError`, and a kind's published fields are exactly its
   ``model_fields`` under aliases.
2. Storage is internal: :meth:`Registry.compose_path` has no inverse, and no
   accessor returns a directory outside this package.
3. Every client value reaching a path component is a single validated segment,
   refused (never sanitized) with :class:`PathSegmentError`.
4. The one placement field (``swc.group``) is declared, not a Pydantic field;
   a second is refused.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType, UnionType
from typing import Any, Literal, Union, get_args, get_origin
from weakref import ReferenceType, WeakKeyDictionary, ref

from pydantic import BaseModel
from typing_extensions import TypeAliasType

from swcraft_tools.model_registry import (
    ModelRegistry,
    StorageRecord,
    compose_storage_path,
    current_registry,
    path_refusal,
    production_registry,
    segment_refusal,
    use_registry,
)

__all__ = [
    "CollectionDescriptor",
    "ElementKindDescriptor",
    "FieldDescriptor",
    "LayerDescriptor",
    "PathSegmentError",
    "REGISTRY",
    "ReferenceDescriptor",
    "Registry",
    "RegistryError",
    "StorageRecord",
    "build_registry",
    "current",
    "kernel_context",
    "or_current",
    "segment_refusal",
]


class RegistryError(RuntimeError):
    """The registry could not be constructed from ``schema.py`` plus its annotations.

    Raised at import for a defect no client can provoke: an annotation naming a
    missing field, a second placement field, a collection with no declared member
    identity, a duplicate element-kind name. Also raised by
    :func:`kernel_context` for a projection whose kernel registry was collected,
    a lifetime mistake in the calling code.
    """


class PathSegmentError(ValueError):
    """A value the registry would have composed into a path is not a single segment.

    Refused before any filesystem access, never sanitized into a legal one.
    """


# ---------------------------------------------------------------------------
# Published records — exactly `protocol.schema.json`'s descriptor shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceDescriptor:
    """``$defs/referenceDescriptor``: a reference field's target layer and identity field."""

    layer: str
    identity_field: str


@dataclass(frozen=True)
class CollectionDescriptor:
    """``$defs/collectionDescriptor``: a nested list or map and how one member is identified."""

    kind: Literal["list", "map"]
    member_identity: Literal["field", "composite", "whole_value"]
    member_kinds: tuple[str, ...] = ()
    discriminator_field: str | None = None
    identity_field: str | None = None
    identity_fields: tuple[str, ...] = ()
    #: ``(discriminator literal, ((field, contract default), ...))`` per member
    #: kind: the declared defaults of optional fields, which
    #: :func:`~swcraft_tools.editing.address.reduce_whole_value` leaves out of a
    #: ``whole_value`` member's identity. Carried for matching; not published and
    #: not part of equality.
    member_defaults: tuple[tuple[Any, tuple[tuple[str, Any], ...]], ...] = field(
        default=(), compare=False, hash=False, repr=False
    )
    #: The ``identity_fields`` the member kind declares optional; an omitted one
    #: reads as ``null``
    #: (:func:`~swcraft_tools.editing.address.complete_composite_identity`).
    #: Carried for matching; not published and not part of equality.
    optional_identity_fields: tuple[str, ...] = field(default=(), compare=False, hash=False, repr=False)

    def defaults_for(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """The declared defaults of the member kind *value* is, or nothing when it cannot be told."""
        if len(self.member_defaults) == 1:
            return dict(self.member_defaults[0][1])
        if self.discriminator_field is None or self.discriminator_field not in value:
            return {}
        for literal, defaults in self.member_defaults:
            if literal == value[self.discriminator_field]:
                return dict(defaults)
        return {}


@dataclass(frozen=True)
class FieldDescriptor:
    """``$defs/fieldDescriptor``: one declared field of one element kind.

    ``placement`` is not published: it records which half of
    :meth:`Registry.all_fields` the field belongs to.
    """

    name: str
    type: str
    required: bool
    immutable: bool
    description: str | None = None
    unit: str | None = None
    enum: tuple[Any, ...] = ()
    reference: ReferenceDescriptor | None = None
    element_kind: str | None = None
    collection: CollectionDescriptor | None = None
    placement: bool = False


@dataclass(frozen=True)
class ElementKindDescriptor:
    """``$defs/elementKindDescriptor``: one element kind of a layer.

    ``member_only`` marks a kind that exists only inside another element (no
    file, no ``{layer, element}`` address); ``member_of`` says where it appears.
    Both are derived from the layer's field graph.
    """

    kind: str
    layer: str
    model: type[BaseModel]
    fields: tuple[FieldDescriptor, ...]
    discriminator_field: str | None = None
    member_only: bool = False
    member_of: tuple[tuple[str, str], ...] = ()
    description: str | None = None

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def get_field(self, name: str) -> FieldDescriptor | None:
        for descriptor in self.fields:
            if descriptor.name == name:
                return descriptor
        return None


@dataclass(frozen=True)
class LayerDescriptor:
    """``$defs/layerDescriptor`` minus its one probe.

    ``present`` is not here: it is project state, answered by discovery.
    ``optional`` is a registry fact; the shipped contract declares every layer
    mandatory, but a contract may declare an optional one.

    ``snapshot_attribute`` is the ``ModelSnapshot``/``ModelDiff`` attribute the
    layer's elements live in (``swc`` -> ``swcs``; otherwise the layer's name).
    It is a legacy vocabulary bridge, deprecated in the binding, kept for as
    long as the snapshot and diff adapters use attribute names.

    ``absent_description`` is the prose published when the layer is not
    present (see :meth:`described`). The shipped contract declares none.
    """

    name: str
    optional: bool
    identity_field: str
    snapshot_attribute: str
    element_kinds: tuple[ElementKindDescriptor, ...]
    description: str | None = None
    absent_description: str | None = None

    def described(self, *, present: bool) -> str | None:
        """The description to publish for a layer that is (or is not) present."""
        if present or self.absent_description is None:
            return self.description
        return self.absent_description

    @property
    def top_level_kinds(self) -> tuple[ElementKindDescriptor, ...]:
        return tuple(kind for kind in self.element_kinds if not kind.member_only)


# ---------------------------------------------------------------------------
# The annotation tables
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerSpec:
    """One row of the layer table: the parts of a layer no Pydantic model knows."""

    name: str
    annotation: Any
    identity_field: str
    snapshot_attribute: str
    storage: StorageRecord
    optional: bool = False
    description: str | None = None
    #: See :attr:`LayerDescriptor.absent_description`.
    absent_description: str | None = None


@dataclass(frozen=True)
class FieldAnnotation:
    """Metadata attached to a field that already exists. It may only annotate."""

    description: str | None = None
    unit: str | None = None
    reference: ReferenceDescriptor | None = None
    immutable: bool | None = None


@dataclass(frozen=True)
class PlacementFieldSpec:
    """A declared field whose persisted form is a component of the element's location.

    Always required and immutable: only a ``create`` fixes one, so no protocol
    operation can move, rename or overwrite an existing file.
    """

    layer: str
    kind: str
    name: str
    type: str = "string"
    description: str | None = None


@dataclass(frozen=True)
class CollectionSpec:
    """How one member of one nested collection is identified.

    ``differ.py``'s ``COLLECTION_IDENTITY``, in the protocol's vocabulary:
    ``_FieldKey`` -> ``field``, ``_CompositeKey`` -> ``composite``,
    ``_ValueKey``/``_self_key`` -> ``whole_value``; its ``"mapping"`` is the
    protocol's ``"map"``.
    """

    member_identity: Literal["field", "composite", "whole_value"]
    identity_field: str | None = None
    identity_fields: tuple[str, ...] = ()
    description: str | None = None


# ---------------------------------------------------------------------------
# The tables, projected from the model contract
# ---------------------------------------------------------------------------
#
# Private, because a projection is not a second description of the model:
# callers reach registry content through `REGISTRY`, `current()` or the
# `registry` argument they were handed. Projecting `immutable`, the one fact the
# registry also derives, is not mechanical; `_project_field_annotations` says how.


@dataclass(frozen=True)
class _ProjectedTables:
    """The five tables that project straight from the contract.

    The field annotations are not among them: they need a built
    :class:`Registry` as their oracle (:func:`_project_field_annotations`).
    """

    layer_specs: tuple[LayerSpec, ...]
    placement_fields: tuple[PlacementFieldSpec, ...]
    kind_names: dict[type[BaseModel], str]
    kind_descriptions: dict[str, str]
    collection_specs: dict[tuple[str, str], CollectionSpec]


def _project_tables(model_registry: ModelRegistry) -> _ProjectedTables:
    """The five declaration tables, read off *model_registry*.

    ``snapshot_attribute`` falls back to the layer's own name when the binding
    declares none, the spelling every layer but ``swc`` already has. The kernel
    keeps ``None``, which tells the snapshot adapter to give such a layer no
    field.
    """
    layer_specs: list[LayerSpec] = []
    placement_fields: list[PlacementFieldSpec] = []
    kind_names: dict[type[BaseModel], str] = {}
    kind_descriptions: dict[str, str] = {}
    collection_specs: dict[tuple[str, str], CollectionSpec] = {}

    for layer in model_registry.layers:
        layer_specs.append(
            LayerSpec(
                name=layer.name,
                annotation=layer.element_annotation,
                identity_field=layer.identity_field,
                snapshot_attribute=layer.snapshot_attribute or layer.name,
                storage=layer.storage,
                optional=layer.optional,
                description=layer.description,
                absent_description=layer.absent_description,
            )
        )
        if layer.placement is not None:
            placement_fields.append(
                PlacementFieldSpec(
                    layer=layer.placement.layer,
                    kind=layer.placement.kind,
                    name=layer.placement.field,
                    type=layer.placement.type,
                    description=layer.placement.description,
                )
            )
        for kind in layer.kinds:
            kind_names[kind.model] = kind.kind
            if kind.description is not None:
                kind_descriptions[kind.kind] = kind.description

    for collection in model_registry.collections():
        collection_specs[collection.kind, collection.field] = CollectionSpec(
            member_identity=collection.member_identity,
            identity_field=collection.identity_field,
            identity_fields=collection.identity_fields,
        )

    return _ProjectedTables(
        layer_specs=tuple(layer_specs),
        placement_fields=tuple(placement_fields),
        kind_names=kind_names,
        kind_descriptions=kind_descriptions,
        collection_specs=collection_specs,
    )


#: The kernel registry over the shipped contract, resolved once at import.
#: The production registry and not the context: :func:`build_registry`'s
#: fallback must not bind whichever registry was in scope at first import.
_PRODUCTION_MODEL: ModelRegistry = production_registry()

#: The shipped contract's five straight projections:
#:
#: * ``kind_names``: model class -> published element-kind name;
#:   :func:`build_registry` checks it against each discriminated class's literal.
#: * ``layer_specs``: every layer, in contract order.
#: * ``placement_fields``: exactly one; :func:`build_registry` refuses a second.
#: * ``kind_descriptions``: a row exactly where the contract declares one.
#: * ``collection_specs``: (element kind, published field) -> member identity.
#:   Every derived collection needs a row and every row a collection, so no
#:   collection is published without an addressable member identity.
_PRODUCTION_TABLES: _ProjectedTables = _project_tables(_PRODUCTION_MODEL)

# ---------------------------------------------------------------------------
# Derivation from `schema.py`
# ---------------------------------------------------------------------------

_NONE_TYPE = type(None)
_SCALAR_NAMES: dict[type, str] = {str: "string", bool: "boolean", int: "integer", float: "number"}

#: What a named recursive type alias is published as: any JSON value. Its real
#: shape depends on another field (the data element's `type`), which
#: `check-model` enforces, so a type name spelling it would be a weaker second
#: statement. Matched by the alias's type, never its name, so no model symbol is
#: named here.
_RECURSIVE_ALIAS_TYPE_NAME = "value"


def _union_members(annotation: Any) -> tuple[Any, ...] | None:
    """The members of ``A | B`` / ``Union[A, B]``, or ``None`` if it is not a union."""
    if get_origin(annotation) in (Union, UnionType):
        return get_args(annotation)
    return None


def _strip_optional(annotation: Any) -> Any:
    """``X | None`` -> ``X``. A wider union is left alone."""
    members = _union_members(annotation)
    if members is None:
        return annotation
    non_none = [member for member in members if member is not _NONE_TYPE]
    return non_none[0] if len(non_none) == 1 else annotation


def _model_members(annotation: Any) -> tuple[type[BaseModel], ...]:
    """Every ``BaseModel`` class an annotation can hold, in declaration order."""
    annotation = _strip_optional(annotation)
    if hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    members = _union_members(annotation)
    if members is not None:
        return tuple(m for m in members if isinstance(m, type) and issubclass(m, BaseModel))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation,)
    return ()


def _literal_args(annotation: Any) -> tuple[Any, ...]:
    return get_args(annotation) if get_origin(annotation) is Literal else ()


def _is_exact_number(annotation: Any) -> bool:
    """Whether *annotation* is ``int | float``, optional or not: a number held exactly, published as ``number``.

    The union keeps an integer bound from rounding through a float; a client
    sees a JSON number either way.
    """
    members = _union_members(annotation)
    return members is not None and {member for member in members if member is not _NONE_TYPE} == {int, float}


def _kind_name(model: type[BaseModel], kind_names: dict[type[BaseModel], str]) -> str:
    try:
        return kind_names[model]
    except KeyError:  # pragma: no cover - a new schema class with no declared name
        raise RegistryError(f"no element-kind name declared for {model.__name__}") from None


def _scalar_type_name(annotation: Any, kind_names: dict[type[BaseModel], str]) -> str:
    if _is_exact_number(annotation):
        return "number"
    literals = _literal_args(annotation)
    if literals:
        return _SCALAR_NAMES.get(type(literals[0]), "string")
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _kind_name(annotation, kind_names)
    if isinstance(annotation, TypeAliasType):
        return _RECURSIVE_ALIAS_TYPE_NAME
    return _SCALAR_NAMES.get(annotation, "string")


def _type_name(annotation: Any, kind_names: dict[type[BaseModel], str]) -> str:
    """The published ``fieldDescriptor.type`` for one annotation.

    ``list[Port]`` -> ``list[port]``, ``list[TimingEvent | InitEvent]`` ->
    ``list[timing_event | init_event]``, ``dict[str, int]`` ->
    ``map[string, integer]``, ``float | None`` and ``int | float | None`` ->
    ``number``, and a named recursive alias -> ``value``.
    """
    annotation = _strip_optional(annotation)
    origin = get_origin(annotation)
    if origin is list:
        (inner,) = get_args(annotation)
        members = _union_members(inner)
        if members is not None:
            names = [_scalar_type_name(member, kind_names) for member in members]
            return f"list[{' | '.join(names)}]"
        return f"list[{_scalar_type_name(inner, kind_names)}]"
    if origin is dict:
        key, value = get_args(annotation)
        return f"map[{_scalar_type_name(key, kind_names)}, {_scalar_type_name(value, kind_names)}]"
    return _scalar_type_name(annotation, kind_names)


def _union_discriminator(models: tuple[type[BaseModel], ...]) -> str | None:
    """The field that tells a union's members apart: a single-valued ``Literal`` on each, all different.

    Derived rather than declared so it cannot disagree with ``schema.py``
    (e.g. ``Runnable.events``' members' ``type``).
    """
    if len(models) < 2:
        return None
    common = set(models[0].model_fields)
    for model in models[1:]:
        common &= set(model.model_fields)
    for name in models[0].model_fields:
        if name not in common:
            continue
        values = []
        for model in models:
            args = _literal_args(model.model_fields[name].annotation)
            if len(args) != 1:
                break
            values.append(args[0])
        else:
            if len(set(values)) == len(models):
                return name
    return None


# ---------------------------------------------------------------------------
# Path composition — one way, and never from a value that is not a segment
# ---------------------------------------------------------------------------
#
# The rules live in the kernel (`segment_refusal`, `path_refusal`), because they
# depend on the layer's storage record. This module turns their answer into
# `PathSegmentError` in `Registry.compose_path` and `Registry.identity_refusal`,
# and re-exports `segment_refusal` for the package's callers rather than wrapping it.


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Registry:
    """One derived description of the model: its layers and their storage.

    Storage is held beside the published layers, not on them; the only public
    way out is :meth:`compose_path`, which has no inverse.
    """

    layers: tuple[LayerDescriptor, ...]
    _storage: dict[str, StorageRecord] = field(repr=False, default_factory=dict)
    _kinds: dict[str, ElementKindDescriptor] = field(repr=False, default_factory=dict)
    #: The kernel registry this projection was derived from, for
    #: :func:`kernel_context`. Weak, because :data:`_PROJECTIONS` is keyed weakly
    #: by it and a strong back-reference would keep every entry alive.
    #: ``compare=False`` keeps two projections of the same tables equal.
    _model: ReferenceType[ModelRegistry] | None = field(repr=False, compare=False, default=None)

    @property
    def layer_names(self) -> tuple[str, ...]:
        return tuple(descriptor.name for descriptor in self.layers)

    @property
    def kernel_model(self) -> ModelRegistry | None:
        """The kernel registry this projection was derived from, if it is still alive.

        ``None`` both for a projection built from perturbed tables and for one
        whose kernel registry was collected.
        """
        return None if self._model is None else self._model()

    @property
    def kinds(self) -> MappingProxyType[str, ElementKindDescriptor]:
        """Every element kind by name, layer by layer, owners before the members they hold."""
        return MappingProxyType(self._kinds)

    def layer(self, name: str) -> LayerDescriptor:
        for descriptor in self.layers:
            if descriptor.name == name:
                return descriptor
        raise KeyError(name)

    def element_kind(self, kind: str) -> ElementKindDescriptor:
        return self._kinds[kind]

    def kind_of_model(self, model: type) -> ElementKindDescriptor | None:
        """The element kind a Pydantic class is, or ``None`` for a class no layer declares.

        The inverse of :attr:`ElementKindDescriptor.model`, computed in one place
        so resolution and publication cannot disagree.
        """
        for descriptor in self._kinds.values():
            if descriptor.model is model:
                return descriptor
        return None

    def kind_names(self) -> tuple[str, ...]:
        return tuple(self._kinds)

    def all_fields(self, kind: str) -> tuple[str, ...]:
        """Every published field of an element kind — the schema-derived half plus placement."""
        return self._kinds[kind].field_names

    def schema_fields(self, kind: str) -> tuple[str, ...]:
        """The schema-derived half only, without placement; discovery and ``read`` publish :meth:`all_fields`."""
        return tuple(f.name for f in self._kinds[kind].fields if not f.placement)

    def storage(self, layer: str) -> StorageRecord:
        """Registry-internal. Not published, not accepted, and no request field takes one."""
        return self._storage[layer]

    @property
    def max_address_depth(self) -> int:
        """The most path steps any address into this registry's model can have.

        A member of a ``whole_value`` collection is a leaf. Ten collection chains
        reach the deepest depth, five, through every field of the member kind they
        end in:
        ``swc/X.runnables[member ...].events[member ...].<field>``,
        ``interfaces/X.operations[member ...].arguments[member ...].<field>``,
        ``swc/X.runnables[member ...].reads[member ...].<field>``,
        ``swc/X.runnables[member ...].writes[member ...].<field>``,
        ``swc/X.runnables[member ...].calls[member ...].<field>``,
        ``swc/X.ports[member ...].com_specs[member ...].<field>``,
        ``swc/X.runnables[member ...].reads_variables[member ...].<field>``,
        ``swc/X.runnables[member ...].writes_variables[member ...].<field>``,
        ``swc/X.runnables[member ...].can_enter[member ...].<field>`` and
        ``swc/X.runnables[member ...].runs_inside[member ...].<field>``.
        """
        kinds = {kind.kind: kind for layer in self.layers for kind in layer.element_kinds}

        def depth(kind: ElementKindDescriptor, seen: frozenset[str]) -> int:
            if kind.kind in seen:  # pragma: no cover - the field graph is acyclic today
                return 0
            below = seen | {kind.kind}
            deepest = 0
            for descriptor in kind.fields:
                steps = 1
                if descriptor.collection is not None:
                    steps += 1
                    members = [kinds[name] for name in descriptor.collection.member_kinds if name in kinds]
                    if descriptor.collection.member_identity == "whole_value":
                        # A whole-value member is a leaf: one step for any of
                        # its fields, and no descent into a collection of it.
                        steps += 1 if any(member.fields for member in members) else 0
                    else:
                        steps += max((depth(member, below) for member in members), default=0)
                deepest = max(deepest, steps)
            return deepest

        return max(depth(kind, frozenset()) for layer in self.layers for kind in layer.top_level_kinds)

    def compose_path(self, layer: str, identity: str, placement: dict[str, str] | None = None) -> str:
        """``(layer, identity, placement)`` -> the model-root-relative path of that element.

        No inverse. The result is ``/``-joined and never leaves the package in a
        response.

        Raises:
            PathSegmentError: a value is not a single legal segment.
        """
        record = self._storage[layer]
        values = dict(placement or {})
        refusal = path_refusal(record, identity, values, layer=layer)
        if refusal is not None:
            raise PathSegmentError(refusal)
        return compose_storage_path(record, identity, values)

    def identity_refusal(self, layer: str, identity: Any) -> str | None:
        """Why *identity* is not one :meth:`compose_path` would store in *layer*, or ``None``.

        A method because it also checks the file name the layer's suffix
        composes (``A.`` is a legal segment, ``A..yaml`` is not). Every reader
        asking whether an identity is storable asks this, rather than
        :func:`segment_refusal` on the identity alone, so they cannot disagree.
        """
        return path_refusal(self._storage[layer], identity, None, layer=layer)


def _build_fields(
    model: type[BaseModel],
    kind: str,
    layer: LayerSpec,
    *,
    member_only: bool,
    discriminator_field: str | None,
    whole_value_member: bool,
    identity_field_names: frozenset[str],
    kind_names: dict[type[BaseModel], str],
    field_annotations: dict[tuple[str, str], FieldAnnotation],
    collection_specs: dict[tuple[str, str], CollectionSpec],
    placement_fields: tuple[PlacementFieldSpec, ...],
    used_annotations: set[tuple[str, str]],
    used_collections: set[tuple[str, str]],
    declared_defaults: dict[str, dict[str, Any]],
    contract: ModelRegistry,
) -> tuple[FieldDescriptor, ...]:
    descriptors: list[FieldDescriptor] = []
    for attribute, info in model.model_fields.items():
        name = info.alias or attribute
        annotation = info.annotation
        stripped = _strip_optional(annotation)
        origin = get_origin(stripped)
        annotation_record = field_annotations.get((kind, name))
        if annotation_record is not None:
            used_annotations.add((kind, name))

        collection: CollectionDescriptor | None = None
        element_kind_link: str | None = None
        if origin in (list, dict):
            spec = collection_specs.get((kind, name))
            if spec is None:
                raise RegistryError(f"collection '{kind}.{name}' has no declared member identity")
            used_collections.add((kind, name))
            if origin is list:
                (inner,) = get_args(stripped)
                members = _model_members(inner)
                discriminator = _union_discriminator(members)
                collection = CollectionDescriptor(
                    kind="list",
                    member_identity=spec.member_identity,
                    member_kinds=tuple(_kind_name(m, kind_names) for m in members),
                    discriminator_field=discriminator,
                    identity_field=spec.identity_field,
                    identity_fields=spec.identity_fields,
                    member_defaults=tuple(
                        (
                            _discriminator_literal(m, discriminator),
                            tuple(declared_defaults.get(_kind_name(m, kind_names), {}).items()),
                        )
                        for m in members
                    ),
                    optional_identity_fields=contract.optional_identity_fields(
                        spec.identity_fields, tuple(_kind_name(m, kind_names) for m in members)
                    ),
                )
            else:
                collection = CollectionDescriptor(
                    kind="map",
                    member_identity=spec.member_identity,
                    identity_field=spec.identity_field,
                    identity_fields=spec.identity_fields,
                )
        else:
            object_members = _model_members(stripped)
            if object_members:
                element_kind_link = _kind_name(object_members[0], kind_names)

        immutable = _is_immutable(
            name,
            layer=layer,
            member_only=member_only,
            discriminator_field=discriminator_field,
            whole_value_member=whole_value_member,
            identity_field_names=identity_field_names,
            annotation=annotation_record,
        )
        descriptors.append(
            FieldDescriptor(
                name=name,
                type=_type_name(annotation, kind_names),
                required=info.is_required(),
                immutable=immutable,
                description=annotation_record.description if annotation_record else None,
                unit=annotation_record.unit if annotation_record else None,
                enum=_literal_args(stripped),
                reference=annotation_record.reference if annotation_record else None,
                element_kind=element_kind_link,
                collection=collection,
            )
        )

    for placement in placement_fields:
        if placement.kind != kind:
            continue
        if placement.name in {d.name for d in descriptors}:
            raise RegistryError(
                f"placement field '{kind}.{placement.name}' collides with a schema field; "
                "a placement field exists precisely because the schema has none"
            )
        index = next((i for i, d in enumerate(descriptors) if d.name == layer.identity_field), -1)
        descriptors.insert(
            index + 1,
            FieldDescriptor(
                name=placement.name,
                type=placement.type,
                required=True,
                immutable=True,
                description=placement.description,
                placement=True,
            ),
        )
    return tuple(descriptors)


def _is_immutable(
    name: str,
    *,
    layer: LayerSpec,
    member_only: bool,
    discriminator_field: str | None,
    whole_value_member: bool,
    identity_field_names: frozenset[str],
    annotation: FieldAnnotation | None,
) -> bool:
    """Whether a field is immutable, derived from what makes a field immutable.

    An explicit override in *annotation* wins. Deriving rather than listing
    makes a new identity-bearing field immutable the moment it is declared.
    """
    if annotation is not None and annotation.immutable is not None:
        return annotation.immutable
    if whole_value_member:
        # A `whole_value` list member's identity is its whole value: no field of it is mutable.
        return True
    if name == discriminator_field:
        return True
    if member_only:
        return name in identity_field_names
    return name == layer.identity_field


def build_registry(
    *,
    layer_specs: tuple[LayerSpec, ...] | None = None,
    kind_names: dict[type[BaseModel], str] | None = None,
    kind_descriptions: dict[str, str] | None = None,
    field_annotations: dict[tuple[str, str], FieldAnnotation] | None = None,
    collection_specs: dict[tuple[str, str], CollectionSpec] | None = None,
    placement_fields: tuple[PlacementFieldSpec, ...] | None = None,
    model_registry: ModelRegistry | None = None,
) -> Registry:
    """The registry derived from ``schema.py`` and the annotation tables.

    Every table argument falls back to the shipped contract's projection, so the
    production registry is ``build_registry()``; they are parameters so a test
    can perturb one table and see the invariant bite. Fallbacks are resolved in
    the body, never in a default expression (see :func:`or_current`).

    ``model_registry`` has no fallback: it is the kernel registry the tables were
    projected from, recorded for :func:`kernel_context`. A caller passing
    perturbed tables omits it, which :func:`kernel_context` reads as "leave the
    context alone".
    """
    layer_specs = _PRODUCTION_TABLES.layer_specs if layer_specs is None else layer_specs
    placement_fields = _PRODUCTION_TABLES.placement_fields if placement_fields is None else placement_fields
    kind_names = dict(_PRODUCTION_TABLES.kind_names if kind_names is None else kind_names)
    kind_descriptions = dict(_PRODUCTION_TABLES.kind_descriptions if kind_descriptions is None else kind_descriptions)
    field_annotations = dict(_PRODUCTION_FIELD_ANNOTATIONS if field_annotations is None else field_annotations)
    collection_specs = dict(_PRODUCTION_TABLES.collection_specs if collection_specs is None else collection_specs)

    if len(placement_fields) > 1:
        raise RegistryError(
            "there is exactly one placement field (`swc.group`); a second is a registry decision, "
            f"not a table row: {[p.name for p in placement_fields]}"
        )

    used_annotations: set[tuple[str, str]] = set()
    used_collections: set[tuple[str, str]] = set()
    # Declared defaults for the whole-value identity reduction. Perturbed tables
    # derive from the shipped contract, so that is where they are read without one.
    contract = _PRODUCTION_MODEL if model_registry is None else model_registry
    declared_defaults = _declared_defaults(contract)
    layers_out: list[LayerDescriptor] = []
    storage: dict[str, StorageRecord] = {}
    kinds_index: dict[str, ElementKindDescriptor] = {}

    for spec in layer_specs:
        top_level = _model_members(spec.annotation)
        discriminator = _annotation_discriminator(spec.annotation)
        ordered = _walk_kinds(top_level)
        member_of = _member_of_map(ordered, kind_names)
        whole_value_members = _whole_value_member_classes(ordered, kind_names, collection_specs)
        identity_fields_by_kind = _member_identity_fields(ordered, kind_names, collection_specs)

        kind_descriptors: list[ElementKindDescriptor] = []
        for model in ordered:
            kind = _kind_name(model, kind_names)
            member_only = model not in top_level
            fields = _build_fields(
                model,
                kind,
                spec,
                member_only=member_only,
                discriminator_field=discriminator if not member_only else None,
                whole_value_member=model in whole_value_members,
                identity_field_names=identity_fields_by_kind.get(kind, frozenset()),
                kind_names=kind_names,
                field_annotations=field_annotations,
                collection_specs=collection_specs,
                placement_fields=placement_fields,
                used_annotations=used_annotations,
                used_collections=used_collections,
                declared_defaults=declared_defaults,
                contract=contract,
            )
            if discriminator is not None and not member_only:
                literal = next((f.enum for f in fields if f.name == discriminator), ())
                if len(literal) == 1 and literal[0] != kind:
                    raise RegistryError(
                        f"element-kind name '{kind}' disagrees with its own discriminator literal '{literal[0]}'"
                    )
            kind_descriptors.append(
                ElementKindDescriptor(
                    kind=kind,
                    layer=spec.name,
                    model=model,
                    fields=fields,
                    discriminator_field=discriminator if not member_only else None,
                    member_only=member_only,
                    member_of=member_of.get(kind, ()),
                    description=kind_descriptions.get(kind),
                )
            )

        for descriptor in kind_descriptors:
            if descriptor.kind in kinds_index:
                raise RegistryError(f"duplicate element-kind name '{descriptor.kind}'")
            kinds_index[descriptor.kind] = descriptor

        layers_out.append(
            LayerDescriptor(
                name=spec.name,
                optional=spec.optional,
                identity_field=spec.identity_field,
                snapshot_attribute=spec.snapshot_attribute,
                element_kinds=tuple(kind_descriptors),
                description=spec.description,
                absent_description=spec.absent_description,
            )
        )
        storage[spec.name] = spec.storage

    _check_annotations_only_annotate(field_annotations, used_annotations, collection_specs, used_collections)
    for placement in placement_fields:
        if placement.kind not in kinds_index:
            raise RegistryError(f"placement field '{placement.name}' names an unknown element kind '{placement.kind}'")

    return Registry(
        layers=tuple(layers_out),
        _storage=storage,
        _kinds=kinds_index,
        _model=None if model_registry is None else ref(model_registry),
    )


def _declared_defaults(model_registry: ModelRegistry) -> dict[str, dict[str, Any]]:
    """Every kind's optional fields with the contract's declared ``default``, by published name."""
    return {
        kind.kind: {facts.name: facts.default for facts in kind.fields if facts.has_default and not facts.required}
        for kind in model_registry.kinds.values()
    }


def _discriminator_literal(model: type[BaseModel], discriminator: str | None) -> Any:
    """The one literal *model*'s discriminator field admits, or ``None`` when there is none."""
    if discriminator is None or discriminator not in model.model_fields:
        return None
    literal = _literal_args(_strip_optional(model.model_fields[discriminator].annotation))
    return literal[0] if len(literal) == 1 else None


def _annotation_discriminator(annotation: Any) -> str | None:
    """``Annotated[A | B, Field(discriminator="kind")]`` -> ``"kind"``, else ``None``."""
    for meta in getattr(annotation, "__metadata__", ()):
        discriminator = getattr(meta, "discriminator", None)
        if isinstance(discriminator, str):
            return discriminator
    return None


def _walk_kinds(top_level: tuple[type[BaseModel], ...]) -> tuple[type[BaseModel], ...]:
    """Every element kind of a layer, breadth first, so an owner precedes the members it holds."""
    ordered: list[type[BaseModel]] = list(top_level)
    index = 0
    while index < len(ordered):
        model = ordered[index]
        index += 1
        for info in model.model_fields.values():
            annotation = _strip_optional(info.annotation)
            if get_origin(annotation) is list:
                (inner,) = get_args(annotation)
                candidates = _model_members(inner)
            elif get_origin(annotation) is dict:
                candidates = ()
            else:
                candidates = _model_members(annotation)
            for candidate in candidates:
                if candidate not in ordered:
                    ordered.append(candidate)
    return tuple(ordered)


def _member_of_map(
    ordered: tuple[type[BaseModel], ...], kind_names: dict[type[BaseModel], str]
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Every place a nested kind appears, as ``(owner kind, field name)`` pairs."""
    result: dict[str, list[tuple[str, str]]] = {}
    for model in ordered:
        owner = _kind_name(model, kind_names)
        for attribute, info in model.model_fields.items():
            name = info.alias or attribute
            annotation = _strip_optional(info.annotation)
            if get_origin(annotation) is list:
                (inner,) = get_args(annotation)
                members = _model_members(inner)
            elif get_origin(annotation) is dict:
                members = ()
            else:
                members = _model_members(annotation)
            for member in members:
                result.setdefault(_kind_name(member, kind_names), []).append((owner, name))
    return {kind: tuple(pairs) for kind, pairs in result.items()}


def _whole_value_member_classes(
    ordered: tuple[type[BaseModel], ...],
    kind_names: dict[type[BaseModel], str],
    collection_specs: dict[tuple[str, str], CollectionSpec],
) -> frozenset[type[BaseModel]]:
    """The member classes of a ``whole_value`` **list**, whose every field is immutable.

    A ``whole_value`` map is excluded: its key is the identity and its value is
    ordinary mutable content.
    """
    result: set[type[BaseModel]] = set()
    for model in ordered:
        owner = _kind_name(model, kind_names)
        for attribute, info in model.model_fields.items():
            name = info.alias or attribute
            annotation = _strip_optional(info.annotation)
            if get_origin(annotation) is not list:
                continue
            spec = collection_specs.get((owner, name))
            if spec is None or spec.member_identity != "whole_value":
                continue
            (inner,) = get_args(annotation)
            result.update(_model_members(inner))
    return frozenset(result)


def _member_identity_fields(
    ordered: tuple[type[BaseModel], ...],
    kind_names: dict[type[BaseModel], str],
    collection_specs: dict[tuple[str, str], CollectionSpec],
) -> dict[str, frozenset[str]]:
    """Per member kind, the fields that identify it inside the collection that holds it."""
    result: dict[str, set[str]] = {}
    for model in ordered:
        owner = _kind_name(model, kind_names)
        for attribute, info in model.model_fields.items():
            name = info.alias or attribute
            annotation = _strip_optional(info.annotation)
            if get_origin(annotation) is not list:
                continue
            spec = collection_specs.get((owner, name))
            if spec is None:
                continue
            (inner,) = get_args(annotation)
            identifiers: tuple[str, ...] = ()
            if spec.member_identity == "field" and spec.identity_field:
                identifiers = (spec.identity_field,)
            elif spec.member_identity == "composite":
                identifiers = spec.identity_fields
            for member in _model_members(inner):
                result.setdefault(_kind_name(member, kind_names), set()).update(identifiers)
    return {kind: frozenset(names) for kind, names in result.items()}


def _check_annotations_only_annotate(
    field_annotations: dict[tuple[str, str], FieldAnnotation],
    used_annotations: set[tuple[str, str]],
    collection_specs: dict[tuple[str, str], CollectionSpec],
    used_collections: set[tuple[str, str]],
) -> None:
    """An annotation may only annotate a field that exists."""
    unused = sorted(set(field_annotations) - used_annotations)
    if unused:
        raise RegistryError(
            f"field annotations name fields that do not exist in schema.py (an annotation may only annotate): {unused}"
        )
    stale = sorted(set(collection_specs) - used_collections)
    if stale:
        raise RegistryError(f"collection specs name fields that are not collections in schema.py: {stale}")


# ---------------------------------------------------------------------------
# The one table whose oracle is a built registry
# ---------------------------------------------------------------------------


def _project_field_annotations(
    model_registry: ModelRegistry, tables: _ProjectedTables
) -> dict[tuple[str, str], FieldAnnotation]:
    """(element kind, published field name) -> the metadata Pydantic cannot carry.

    ``description``, ``unit`` and ``reference`` are read straight off the
    contract; a row exists for each field declaring one (placement excluded).

    ``immutable`` is a three-valued override (``None`` lets :func:`_is_immutable`
    decide), so copying the contract's boolean into every row would silence the
    derivation that checks it. Instead a provisional registry is built with no
    overrides, and a row gets one exactly where the contract disagrees with the
    derivation: today only a base type's ``kind``, an immutable literal on a
    one-kind layer with no discriminator. A new disagreement thus shows up as a
    new row, which the tests pin, rather than as a silently changed verdict.
    """
    annotations: dict[tuple[str, str], FieldAnnotation] = {}
    for layer in model_registry.layers:
        for kind in layer.kinds:
            for facts in kind.fields:
                if facts.placement:
                    continue
                if facts.description is None and facts.unit is None and facts.reference is None:
                    continue
                annotations[kind.kind, facts.name] = FieldAnnotation(
                    description=facts.description,
                    unit=facts.unit,
                    reference=(
                        None
                        if facts.reference is None
                        else ReferenceDescriptor(facts.reference.target_layer, facts.reference.target_identity_field)
                    ),
                )

    provisional = build_registry(
        layer_specs=tables.layer_specs,
        kind_names=tables.kind_names,
        kind_descriptions=tables.kind_descriptions,
        field_annotations=annotations,
        collection_specs=tables.collection_specs,
        placement_fields=tables.placement_fields,
    )
    for layer in model_registry.layers:
        for kind in layer.kinds:
            derived = provisional.element_kind(kind.kind)
            for facts in kind.fields:
                if facts.placement:
                    continue
                published = derived.get_field(facts.name)
                if published is None or published.immutable == facts.immutable:
                    continue
                key = (kind.kind, facts.name)
                existing = annotations.get(key, FieldAnnotation())
                annotations[key] = replace(existing, immutable=facts.immutable)
    return annotations


#: The shipped contract's field annotations, :func:`build_registry`'s fallback.
#: Bound here because it needs a built :class:`Registry`.
_PRODUCTION_FIELD_ANNOTATIONS: dict[tuple[str, str], FieldAnnotation] = _project_field_annotations(
    _PRODUCTION_MODEL, _PRODUCTION_TABLES
)


# ---------------------------------------------------------------------------
# The production registry, and the projection of any other
# ---------------------------------------------------------------------------

#: The projection of the shipped contract. No production module but this one
#: reads it: callers use :func:`current` or the ``registry`` argument they were
#: handed, so a ``use_registry`` context is honoured. Exported for tests.
REGISTRY: Registry = build_registry(model_registry=_PRODUCTION_MODEL)

#: One concrete :class:`Registry` per :class:`ModelRegistry`. Weak, so a test's
#: registry does not outlive the test; the production entry is held elsewhere.
_PROJECTIONS: MutableMapping[ModelRegistry, Registry] = WeakKeyDictionary()
_PROJECTIONS[_PRODUCTION_MODEL] = REGISTRY


def current(registry: ModelRegistry | None = None) -> Registry:
    """The concrete registry for *registry*, or for the kernel registry in scope.

    The injection seam. Resolved in the body, never in a default expression,
    so ``use_registry`` takes effect. An entry point such as
    :func:`~swcraft_tools.editing.engine.handle_request` calls this once and
    threads the projection explicitly.
    """
    model_registry = current_registry() if registry is None else registry
    projection = _PROJECTIONS.get(model_registry)
    if projection is None:
        tables = _project_tables(model_registry)
        projection = build_registry(
            layer_specs=tables.layer_specs,
            kind_names=tables.kind_names,
            kind_descriptions=tables.kind_descriptions,
            field_annotations=_project_field_annotations(model_registry, tables),
            collection_specs=tables.collection_specs,
            placement_fields=tables.placement_fields,
            model_registry=model_registry,
        )
        _PROJECTIONS[model_registry] = projection
    return projection


def or_current(registry: Registry | None) -> Registry:
    """*registry* if the caller supplied one, else the one its context is running under.

    The one form an optional ``registry`` parameter takes inside this package:
    ``= None``, then this call as the first statement.
    """
    return current() if registry is None else registry


@contextlib.contextmanager
def kernel_context(registry: Registry) -> Iterator[None]:
    """A block run with *registry*'s own kernel registry as the ambient one.

    Consumers that resolve :func:`~swcraft_tools.model_registry.current_registry`
    themselves then answer the same contract as the projection.

    Raises:
        RegistryError: the projection's kernel registry was collected; keep the
            ``ModelRegistry`` alive for as long as its projection is used.

    Does not reach: a projection built from perturbed tables never had a kernel
    registry, and its block runs with the context untouched, silently.
    """
    model = registry.kernel_model
    if model is None:
        if registry._model is not None:
            raise RegistryError(
                "this projection's kernel registry has been collected; keep a reference to the "
                "ModelRegistry for as long as the projection is used"
            )
        yield
        return
    with use_registry(model):
        yield
