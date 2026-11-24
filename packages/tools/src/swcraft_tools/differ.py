"""Semantic diff between two ModelSnapshots.

:func:`diff_snapshots` compares every layer the model contract declares by identity key (``.name``,
or ``.id`` for requirements) and reports per layer the elements added, removed or changed. There is
no rename detection: a renamed element is a removal plus an addition, and :func:`_rename_hints` adds
a hint only for an unambiguous 1:1 match equal once identity and declared defaults are stripped.

A collection field (an SWC's ``ports``, a task's ``runnables``) is compared item by item under its
:func:`collection_identity` rule, into a :class:`CollectionChange` beside the whole-value tuple (its
docstring says when); ``Runnable.events`` is a multiset keyed by value, so a changed ``period`` is one
event removed and one added. :func:`build_key_map` raises ``ValueError`` on a duplicate identity key
in a top-level layer only. A :class:`ChangedItem` and a :class:`CollectionChange` carry their
``pathStep`` path (``field``/``member``/``key``, never an ordinal, never a storage name).

:func:`format_diff` renders text; :func:`format_diff_json` renders the ``--format json`` document,
``{"schema_version": 2, "layers": {...}}``, by ``json.dumps(..., indent=2, allow_nan=False)`` plus a
trailing newline:

- ``schema_version`` is the format's major version: removing or renaming a field, changing a type or
  a meaning raises it, an optional field does not. A consumer rejects a version it does not know.
- ``layers`` holds every declared layer, always, keyed by ``ModelDiff`` attribute in ``displayOrder``,
  each with ``added``, ``removed`` (identity keys), ``changed`` and ``rename_hints``
  (``{"removed_key", "added_key"}``, a possibility, both keys also listed).
- A changed item is ``{"key", "fields", "collections"}`` at every depth: ``key`` a string or a list
  of a composite's parts (``["A", null, "Run"]``) or of an event's value; ``fields`` holds
  ``{"field", "old", "new"}`` with ``null`` for absent; ``collections`` the per-item detail; a field
  is in one of the two only. A collection is ``{"field", "added", "removed", "changed",
  "rename_hints", "reordered"}``, ``reordered`` always present. A non-finite float is spelled
  ``"NaN"``, ``"Infinity"`` or ``"-Infinity"``.
"""

from __future__ import annotations

import dataclasses
import json
import math
import weakref
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, fields
from operator import attrgetter, itemgetter
from types import MappingProxyType
from typing import Any, Literal, Self, TypeVar, get_args

from pydantic import BaseModel

from swcraft_tools.model_registry import (
    CollectionFacts,
    MemberIdentity,
    ModelRegistry,
    current_registry,
)
from swcraft_tools.model_state import ModelState
from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.snapshot import ModelSnapshot


class _Missing:
    """Sentinel for a field absent from one side of a `kind`-changed dump."""

    def __repr__(self) -> str:
        return "<absent>"


_MISSING = _Missing()


#: Sentinel substituted for one field's value to see whether the identity reads it
#: (:func:`_identity_residue`); it never reaches a renderer.
_PROBE = object()


# The key functions are frozen dataclasses rather than closures because a diff must survive a
# ``pickle`` round trip comparing equal to itself, and two rules over the same declaration must
# compare equal.


@dataclass(frozen=True)
class _FieldKey:
    """A key over one declared identity field, which the contract names."""

    identity_field: str

    def __call__(self, item: Any) -> Any:
        return item[self.identity_field]


@dataclass(frozen=True)
class _CompositeKey:
    """A key over a declared tuple of fields — ``Task.runnables``, ``Sequence.steps``.

    Both members are a ``{swc, instance, runnable}`` triple (one selector set, the other ``None``)
    that their owners validate as unique. The key is **syntactic**: an entry rewritten from
    ``swc: A`` to ``instance: A`` is a remove and an add, because generic matching never resolves
    an occurrence. The fields are read from ``fieldDecl.collection.identityFields``.
    """

    identity_fields: tuple[str, ...]

    def __call__(self, item: Any) -> Any:
        return tuple(item[name] for name in self.identity_fields)


@dataclass(frozen=True)
class _ValueKey:
    """``Runnable.events`` — the *value* is the key.

    The key is the discriminator (``fieldDecl.collection.discriminatorField``) followed by every
    remaining field in declaration order: ``("init",)``, ``("timing", period)``,
    ``("operation", port, operation)``. Without a declared discriminator the values alone, in
    declaration order, form the key. Items are compared by the *count* of each key
    (:func:`_diff_collection`).

    A field still at its contract default is left out of the key, the rule
    :func:`~swcraft_tools.editing.address.reduce_whole_value` states; ``defaults`` holds, per member
    kind, the discriminator literal and the declared defaults. A non-default value (a non-empty
    ``disabled_modes``) is part of the key, frozen into a tuple so the key stays countable.
    """

    discriminator: str | None
    defaults: tuple[tuple[Any, tuple[tuple[str, Any], ...]], ...] = ()

    def __call__(self, item: Any) -> Any:
        defaults = self._defaults_for(item)
        kept = [
            (name, _hashable(value))
            for name, value in item.items()
            if not (name in defaults and _same_value(_hashable(value), defaults[name]))
        ]
        if self.discriminator is None:
            return tuple(value for _name, value in kept)
        return (item[self.discriminator], *(value for name, value in kept if name != self.discriminator))

    def _defaults_for(self, item: Any) -> dict[str, Any]:
        if len(self.defaults) == 1:
            return dict(self.defaults[0][1])
        if self.discriminator is None or self.discriminator not in item:
            return {}
        for literal, declared in self.defaults:
            if literal == item[self.discriminator]:
                return dict(declared)
        return {}


def _same_value(value: Any, default: Any) -> bool:
    """``==`` with a boolean never equal to a number, the rule ``editing.address.identity_key`` states."""
    return isinstance(value, bool) == isinstance(default, bool) and value == default


def _hashable(value: Any) -> Any:
    """A dumped value as a hashable key: a list as a tuple, a mapping as a tuple of its items."""
    if isinstance(value, dict):
        return tuple((key, _hashable(item)) for key, item in value.items())
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    return value


def _self_key(item: Any) -> Any:
    """The key of a scalar-member collection (no ``memberKinds``): the member itself."""
    return item


#: How one collection's members are identified: one declared field, a declared tuple of them,
#: or the member's whole value.
MemberIdentityStrategy = MemberIdentity


@dataclass(frozen=True)
class CollectionRule:
    """How one collection field is compared item by item.

    ``key`` is applied to each element of the *dumped* collection (the items of a list, the keys
    of a mapping) and returns its identity. ``kind`` decides whether a reorder can reach
    ``changed_fields``: a list compares order-sensitively under ``model_dump``, a mapping by
    content. ``strategy`` and ``identity_fields`` say what ``key`` reads, for
    :meth:`member_identity`; they are declared because no probe of the key function could tell
    ``TaskRunnableRef``'s ``composite`` (the triple is the whole member) from a ``whole_value``.
    """

    kind: Literal["list", "mapping"]
    key: Callable[[Any], Any]
    strategy: MemberIdentityStrategy
    identity_fields: tuple[str, ...] = ()

    def member_identity(self, item: Any) -> Any:
        """One dumped member's identity, as an address step's ``member`` value.

        - ``field`` — the identity field's value, a string (``"pSpeed"``);
        - ``composite`` — an object carrying exactly the declared tuple
          (``{"swc": ..., "instance": ..., "runnable": ...}``, the absent selector ``None``);
        - ``whole_value`` — the member itself: the value is the identity and has no addressable
          interior.

        The result is the dumped value, exactly as ``key`` reads it. A mapping's entries are
        addressed by their key, the entry name its iteration yields.
        """
        if self.strategy == "whole_value":
            return item
        if self.strategy == "field":
            return item[self.identity_fields[0]]
        return {name: item[name] for name in self.identity_fields}

    @property
    def reports_reorder(self) -> bool:
        """Whether a pure reorder of this collection is a reportable fact.

        True for a list: a reorder alone puts a list field into ``changed_fields``, so it must be
        representable. A mapping compares by content and a reorder never reaches
        ``changed_fields``. The criterion is mechanical, so ``SWComponent.satisfies`` (order
        meaningless, but list-backed) gets the signal and ``SWComponent.memory`` does not.
        """
        return self.kind == "list"


#: How ``differ``'s ``kind`` spells the contract's ``collectionKind``.
_COLLECTION_KINDS: Mapping[str, Literal["list", "mapping"]] = MappingProxyType({"list": "list", "map": "mapping"})


def _identity_fields(collection: CollectionFacts) -> tuple[str, ...]:
    """The declared field(s) :meth:`CollectionRule.member_identity` reads, or ``()``."""
    if collection.member_identity == "field":
        if collection.identity_field is None:
            raise ValueError(f"{collection.field_id}: a 'field' member identity with no identityField")
        return (collection.identity_field,)
    if collection.member_identity == "composite":
        return collection.identity_fields
    return ()


def _collection_key(collection: CollectionFacts, registry: ModelRegistry) -> Callable[[Any], Any]:
    """The key function one declared collection is compared by.

    The two ``whole_value`` shapes are told apart by ``memberKinds``: a member kind has an
    interior to key on, a scalar member is its own key.
    """
    if collection.member_identity == "field":
        return _FieldKey(_identity_fields(collection)[0])
    if collection.member_identity == "composite":
        return _CompositeKey(collection.identity_fields)
    if collection.member_kinds:
        return _ValueKey(collection.discriminator_field, _member_defaults(collection, registry))
    return _self_key


def _member_defaults(
    collection: CollectionFacts, registry: ModelRegistry
) -> tuple[tuple[Any, tuple[tuple[str, Any], ...]], ...]:
    """``(discriminator literal, ((field, default), ...))`` per member kind of a whole-value collection."""
    rows = []
    for name in collection.member_kinds:
        kind = registry.kinds.get(name)
        if kind is None:
            continue
        literal = None
        if collection.discriminator_field is not None:
            declared = kind.get_field(collection.discriminator_field)
            if declared is not None and len(declared.enum) == 1:
                literal = declared.enum[0]
        defaults = tuple(
            (facts.name, _hashable(facts.default)) for facts in kind.fields if facts.has_default and not facts.required
        )
        rows.append((literal, defaults))
    return tuple(rows)


def _collection_identity(registry: ModelRegistry) -> Mapping[tuple[type[BaseModel], str], CollectionRule]:
    """``(owner class, field name) -> rule``, for every collection *registry* declares.

    Keyed by the bound Pydantic class, because :func:`_diff_element` never learns which layer
    its owners came from. Not reached: a collection field the contract does not declare as a
    ``collection``; :func:`_collection_rule_for` answers ``None`` and it is compared whole.
    """
    rows: dict[tuple[type[BaseModel], str], CollectionRule] = {}
    for collection in registry.collections():
        owner = registry.bound_class(collection.kind)
        rows[(owner, collection.field)] = CollectionRule(
            kind=_COLLECTION_KINDS[collection.collection_kind],
            key=_collection_key(collection, registry),
            strategy=collection.member_identity,
            identity_fields=_identity_fields(collection),
        )
    return MappingProxyType(rows)


#: One projection per registry, built on first use. Cached so ``CollectionChange.rule`` is the
#: very object :func:`collection_rule` answers; a row holds only facts fixed for the registry's
#: lifetime. The key is weak so a swapped contract does not pin its registry for the process.
_COLLECTION_IDENTITIES: weakref.WeakKeyDictionary[
    ModelRegistry, Mapping[tuple[type[BaseModel], str], CollectionRule]
] = weakref.WeakKeyDictionary()


def collection_identity(
    registry: ModelRegistry | None = None,
) -> Mapping[tuple[type[BaseModel], str], CollectionRule]:
    """The collection rules, projected from *registry* (the current one by default)."""
    registry = registry or current_registry()
    rows = _COLLECTION_IDENTITIES.get(registry)
    if rows is None:
        rows = _collection_identity(registry)
        _COLLECTION_IDENTITIES[registry] = rows
    return rows


def collection_rule(
    owner: type[BaseModel], field_name: str, *, registry: ModelRegistry | None = None
) -> CollectionRule | None:
    """The rule for one collection field, or ``None`` if it is not a collection."""
    return collection_identity(registry).get((owner, field_name))


def _common_subsequence_length(old_keys: list[Any], new_keys: list[Any]) -> int:
    """The length of the longest subsequence common to both key sequences."""
    previous = [0] * (len(new_keys) + 1)
    for old_key in old_keys:
        current = [0]
        for index, new_key in enumerate(new_keys):
            current.append(previous[index] + 1 if old_key == new_key else max(previous[index + 1], current[index]))
        previous = current
    return previous[-1]


def _reordered(old_keys: list[Any], new_keys: list[Any]) -> bool:
    """Whether the items present on **both** sides changed their relative order.

    Only surviving items take part, so an insertion at the front is not a reorder. A key present
    on both sides survives ``min(old count, new count)`` times, and the longest common subsequence
    picks which copies, so losing one of two identical events is not a reorder and two swapped
    identical keys are none either. Returns True when no explanation of the two key sequences
    (each in collection order) keeps every surviving key in its relative order.
    """
    surviving = sum((Counter(old_keys) & Counter(new_keys)).values())
    return _common_subsequence_length(old_keys, new_keys) < surviving


#: An identity key: a ``str`` for every layer and ``.name``-keyed collection, a tuple for a
#: composite key or an ``events`` value key.
ItemKey = str | tuple[Any, ...]

#: One step of the path to a changed thing, in ``$defs/pathStep``'s JSON shape: a one-key object,
#: ``field``, ``member`` or ``key``. A plain ``dict``, because this module imports nothing from the
#: editing protocol that owns the vocabulary.
AddressStep = dict[str, Any]

#: The steps from the addressed *element* down to one changed thing, in order.
#: Empty for an element itself, which an address names by its identity alone.
AddressPath = tuple[AddressStep, ...]


def _field_step(field_name: str) -> AddressStep:
    """``{"field": ...}`` — one declared field of the thing the path stands on."""
    return {"field": field_name}


def _member_step(identity: Any) -> AddressStep:
    """``{"member": ...}`` — one member of a *list* collection, by its identity."""
    return {"member": identity}


def _key_step(key: str) -> AddressStep:
    """``{"key": ...}`` — one entry of a *mapping* collection, by its key."""
    return {"key": key}


@dataclass
class RenameHint:
    """A note that one removed and one added item look like a rename.

    Both entries remain in ``removed`` and ``added``; see :func:`_rename_hints`.
    """

    removed_key: ItemKey
    added_key: ItemKey


@dataclass
class ChangedItem:
    """One element (or one collection item) present on both sides but different.

    ``changed_fields`` is the whole-value form; ``collection_changes`` is the per-item detail, one
    entry per changed collection field. Rename hints live on :class:`LayerDiff` and
    :class:`CollectionChange`, not here.

    ``path`` is the steps from the element down to this item: empty for the element itself, one
    ``{"member": ...}`` or ``{"key": ...}`` step per collection level. Each name in
    ``changed_fields`` extends it by one ``{"field": ...}`` step, except a mapping entry's
    :data:`_MAPPING_VALUE` pseudo-field, whose address is the entry's own.
    """

    key: ItemKey
    changed_fields: list[tuple[str, Any, Any]] = field(default_factory=list)
    collection_changes: list[CollectionChange] = field(default_factory=list)
    path: AddressPath = ()


@dataclass
class CollectionChange:
    """Per-item detail for one changed collection field.

    ``field`` is the field's YAML/alias name, as in the matching ``changed_fields`` tuple.
    ``added``, ``removed`` and each ``ChangedItem``'s values are the **dumped** items
    (``model_dump(by_alias=True)``). ``rule`` is the :class:`CollectionRule` that produced this
    change, carried because a field name alone is ambiguous (``runnables`` belongs to both
    ``SWComponent`` and ``Task``); ``rule.key`` names an added or removed item the way
    ``ChangedItem.key`` does.

    A changed collection field keeps its whole-value tuple in :attr:`ChangedItem.changed_fields`
    *and* gains this change, except when its two sides both reduce to empty: present-but-empty on
    one, absent from the other. ``schema.py`` refuses an empty collection everywhere but an external
    interface stub's two member lists, so that is reached only when a stub replaces a declared
    interface of the same name; then only the tuple is reported, and :func:`format_diff` prints it.

    ``reordered`` is set when the items present on both sides changed their relative order. A
    change carrying only ``reordered=True`` is valid and emitted, which is what lets a renderer
    skip ``changed_fields`` for this field; an empty, non-reordered change is never built.

    ``path`` runs from the element down to and including this collection's ``{"field": ...}``
    step; an added or removed item extends it by ``{"member": rule.member_identity(item)}``
    (``{"key": ...}`` for a mapping).
    """

    field: str
    rule: CollectionRule
    # `dataclasses.field` by its module: the field named `field` shadows the bare name.
    added: list[Any] = dataclasses.field(default_factory=list)
    removed: list[Any] = dataclasses.field(default_factory=list)
    changed: list[ChangedItem] = dataclasses.field(default_factory=list)
    rename_hints: list[RenameHint] = dataclasses.field(default_factory=list)
    reordered: bool = False
    path: AddressPath = ()


@dataclass
class LayerDiff:
    added: list[BaseModel] = field(default_factory=list)
    removed: list[BaseModel] = field(default_factory=list)
    changed: list[ChangedItem] = field(default_factory=list)
    rename_hints: list[RenameHint] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.removed and not self.changed


@dataclass
class ModelStateDiff:
    """The semantic diff of two :class:`~swcraft_tools.model_state.ModelState`\\ s, keyed by **layer name**.

    Keys are the contract's layer names (``state_diff["swc"]``), not :class:`ModelDiff`'s
    attribute names (``swcs``). It lives here, not in :mod:`~swcraft_tools.model_state`, because
    ``model_state`` sits below this module and must never import it. A plain value: equal content
    compares equal. It carries every
    layer the registry declares, including one with no ``snapshotAttributes`` row; a layer
    present in a state but declared by no registry is not compared or reported.
    """

    layers: Mapping[str, LayerDiff]

    def __getitem__(self, layer: str) -> LayerDiff:
        return self.layers[layer]

    def __contains__(self, layer: object) -> bool:
        return layer in self.layers

    def __iter__(self) -> Iterator[str]:
        return iter(self.layers)

    @property
    def layer_names(self) -> tuple[str, ...]:
        """Every layer this diff carries, in the order it was built in."""
        return tuple(self.layers)

    @property
    def is_empty(self) -> bool:
        return all(layer_diff.is_empty for layer_diff in self.layers.values())


# `ModelDiff`'s fields are the binding's `snapshotAttributes`, in that section's key order.

_D = TypeVar("_D", bound="ModelDiff")


def _from_state_diff(cls: type[_D], state_diff: ModelStateDiff, *, registry: ModelRegistry | None = None) -> _D:
    """*state_diff*, in the per-layer attribute shape, carrying *state_diff* itself beside it."""
    registry = registry or current_registry()
    # `_DIFF_ATTRIBUTES` is defined after `ModelDiff`, from its fields.
    values: dict[str, LayerDiff] = {attribute: LayerDiff() for attribute in _DIFF_ATTRIBUTES}
    for name in state_diff.layer_names:
        try:
            facts = registry.layer(name)
        except KeyError:
            # An undeclared layer stays reachable through `.by_layer` and reaches no attribute.
            continue
        if facts.snapshot_attribute in values:
            values[facts.snapshot_attribute] = state_diff[name]
    diff = cls(**values)
    # `_by_layer` must stay outside `fields()`, so the class cannot declare it,
    # and a plain assignment to an undeclared attribute is a type error.
    setattr(diff, "_by_layer", state_diff)
    return diff


def _by_layer_of(self: ModelDiff, registry: ModelRegistry | None = None) -> ModelStateDiff:
    """The layer-keyed diff behind the attributes — the carried one, or a built one."""
    carried: ModelStateDiff | None = getattr(self, "_by_layer", None)
    if carried is not None:
        return carried
    registry = registry or current_registry()
    layers: dict[str, LayerDiff] = {}
    for name in registry.layer_names:
        facts = registry.layer(name)
        if facts.snapshot_attribute not in _DIFF_ATTRIBUTES:
            continue
        layers[name] = getattr(self, facts.snapshot_attribute)
    return ModelStateDiff(layers=layers)


def _diff_is_empty(self: ModelDiff) -> bool:
    return self.by_layer.is_empty


@dataclass
class ModelDiff:
    """One semantic diff, one :class:`LayerDiff` attribute per layer.

    Built by :meth:`from_state_diff`, or by keyword or position with one ``LayerDiff`` per field
    (``ModelSnapshot``'s layer attributes, in the same order, none with a default).

    :attr:`by_layer` is the :class:`ModelStateDiff` behind the attributes. A diff built by
    :meth:`from_state_diff` carries the one it was built from, including layers with no
    attribute, outside the dataclass fields, ``repr`` and comparison; any other construction
    (including ``dataclasses.replace()``) rebuilds it from the fields and loses such layers.
    """

    compositions: LayerDiff
    swcs: LayerDiff
    interfaces: LayerDiff
    connectors: LayerDiff
    requirements: LayerDiff
    tasks: LayerDiff
    sequences: LayerDiff
    hardware: LayerDiff
    mapping_sets: LayerDiff
    application_types: LayerDiff
    implementation_types: LayerDiff
    base_types: LayerDiff
    units: LayerDiff
    compu_methods: LayerDiff
    data_constraints: LayerDiff

    @classmethod
    def from_state_diff(cls, state_diff: ModelStateDiff, *, registry: ModelRegistry | None = None) -> Self:
        """*state_diff* in the attribute shape, carrying *state_diff* itself (default registry: the current one)."""
        return _from_state_diff(cls, state_diff, registry=registry)

    @property
    def by_layer(self) -> ModelStateDiff:
        """The carried layer-keyed diff, or one built from the fields."""
        return _by_layer_of(self)

    @property
    def is_empty(self) -> bool:
        """Whether no layer of :attr:`by_layer` carries a change."""
        return _diff_is_empty(self)


#: The attribute names, in field order.
_DIFF_ATTRIBUTES: tuple[str, ...] = tuple(f.name for f in fields(ModelDiff))


def build_key_map(items: list[BaseModel], key_fn: Callable[[Any], str], layer_name: str) -> dict[str, BaseModel]:
    """Map identity-key -> item, raising ValueError on a duplicate key.

    Hand-edited YAML can declare one identity key in two files; that is raised, never resolved by
    keeping the last item.
    """
    key_map: dict[str, BaseModel] = {}
    for item in items:
        key = key_fn(item)
        if key in key_map:
            raise ValueError(f"duplicate key '{key}' in layer '{layer_name}'")
        key_map[key] = item
    return key_map


#: The name a changed mapping entry records its value under in
#: :attr:`ChangedItem.changed_fields`: the entry's key is the item's key, so the value is
#: the only thing left to report.
_MAPPING_VALUE = "value"


def _model_members(annotation: Any) -> tuple[type[BaseModel], ...]:
    """Every ``BaseModel`` class one annotation can produce, in declaration order.

    Unwraps ``Annotated[...]`` and recurses through ``list[...]`` and union arguments, so every
    member of a discriminated union is seen.
    """
    if hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation,)
    members: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        members.extend(member for member in _model_members(argument) if member not in members)
    return tuple(members)


def _item_owners(owner: type[BaseModel], field_name: str) -> tuple[type[BaseModel], ...]:
    """The classes one item of ``owner.field_name`` can be, read from ``schema.py``.

    Empty for a collection of scalars or plain values; several for a union item type (``events``).
    """
    field_info = owner.model_fields.get(field_name)
    if field_info is None:
        return ()
    return _model_members(field_info.annotation)


def _collection_items(value: Any) -> list[Any]:
    """The comparable elements of one dumped collection value.

    An absent value (``_MISSING``, an element that changed ``kind``) or ``None`` reads as empty,
    so every item on the other side is reported one by one.
    """
    if value is _MISSING or value is None:
        return []
    return list(value)


def _mapping_entries(value: Any) -> dict[Any, Any]:
    """The entries of one dumped mapping value; absent or ``None`` reads as empty."""
    if not isinstance(value, Mapping):
        return {}
    return dict(value)


def _declared_defaults(owners: tuple[type[BaseModel], ...]) -> dict[str, Any]:
    """The declared default of every field of *owners*, under its **dumped** name.

    Keyed by ``alias or name``, because a residue is computed over ``model_dump(by_alias=True)``.
    A required field answers ``PydanticUndefined``, which equals no dumped value, so it is never
    dropped from a residue. Over several owners the first one declaring a field wins.
    """
    defaults: dict[str, Any] = {}
    for owner in owners:
        for name, field_info in owner.model_fields.items():
            defaults.setdefault(field_info.alias or name, field_info.get_default(call_default_factory=True))
    return defaults


def _identity_residue(
    item: Any,
    key_fn: Callable[[Any], Any],
    owners: tuple[type[BaseModel], ...] = (),
) -> Mapping[str, Any] | None:
    """What is left of one dumped item once its identity and its defaults are gone.

    The identity's fields are discovered: each field in turn is replaced by a sentinel and the
    item re-keyed, and it belongs to the identity when the key changes. A field still at its
    declared default is dropped too, being nothing two items could be paired on.

    ``None`` means nothing is left to pair on: an ``events`` value (its identity is its whole
    value), a scalar item, a ``Task.runnables`` reference, and a content-free item such as a
    ``SequenceStep`` with an empty ``note``.

    Args:
        item: one dumped item, or a scalar for a collection whose item is its own key.
        key_fn: the identity of one dumped item.
        owners: the classes the item can be; empty drops nothing for being default-valued.
    """
    if not isinstance(item, Mapping):
        return None
    identity = key_fn(item)
    defaults = _declared_defaults(owners)
    residue = {
        name: value
        for name, value in item.items()
        if key_fn({**item, name: _PROBE}) == identity and value != defaults.get(name, _MISSING)
    }
    return residue or None


def _canonical(value: Any) -> Any:
    """A hashable form of one dumped value, so residues can be grouped.

    Mappings canonicalise by content and lists by order, as ``model_dump`` compares them.
    """
    if isinstance(value, Mapping):
        return tuple(sorted((name, _canonical(entry)) for name, entry in value.items()))
    if isinstance(value, list):
        return tuple(_canonical(entry) for entry in value)
    return value


def _group_by_residue(
    items: list[Any],
    key_fn: Callable[[Any], Any],
    owners: tuple[type[BaseModel], ...],
) -> dict[Any, list[ItemKey]]:
    """Identity keys grouped by their item's residue, in the items' own order."""
    grouped: dict[Any, list[ItemKey]] = {}
    for item in items:
        residue = _identity_residue(item, key_fn, owners)
        if residue is None:
            continue
        grouped.setdefault(_canonical(residue), []).append(key_fn(item))
    return grouped


def _rename_hints(
    removed: list[Any],
    added: list[Any],
    key_fn: Callable[[Any], Any],
    owners: tuple[type[BaseModel], ...],
) -> list[RenameHint]:
    """Rename hints between removed and added items, at a layer's or one collection's level.

    The candidates are one layer's elements or one parent's collection items, never across
    parents. A pair qualifies only when both are equal after :func:`_identity_residue` and the
    match is an unambiguous **1:1**: two identical removed items beside two added ones give no
    hint. Shared emptiness is not evidence, so items with nothing but identity and defaults never
    hint.

    Args:
        removed: the dumped items present only on the old side.
        added: the dumped items present only on the new side.
        key_fn: the identity of one dumped item.
        owners: the classes such an item can be, for their declared defaults.

    Returns:
        One hint per unambiguous pair, in the removed items' order.
    """
    removed_groups = _group_by_residue(removed, key_fn, owners)
    added_groups = _group_by_residue(added, key_fn, owners)
    return [
        RenameHint(removed_key=removed_keys[0], added_key=added_groups[residue][0])
        for residue, removed_keys in removed_groups.items()
        if len(removed_keys) == 1 and len(added_groups.get(residue, ())) == 1
    ]


def _diff_mapping(
    field_name: str,
    rule: CollectionRule,
    old_value: Any,
    new_value: Any,
    path: AddressPath,
) -> CollectionChange | None:
    """Compare one changed mapping field entry by entry.

    An entry is added or removed by name; an entry on both sides with a different value is one
    **changed** item keyed by that name, so a changed budget is not a removal plus an addition.
    An entry's address is a ``{"key": ...}`` step. A mapping carries no reorder signal (it
    compares by content) and no rename hint (its item is its own key).
    """
    old_entries = _mapping_entries(old_value)
    new_entries = _mapping_entries(new_value)

    added = [name for name in new_entries if name not in old_entries]
    removed = [name for name in old_entries if name not in new_entries]
    changed = [
        ChangedItem(
            key=rule.key(name),
            changed_fields=[(_MAPPING_VALUE, old_entry, new_entries[name])],
            path=(*path, _key_step(name)),
        )
        for name, old_entry in old_entries.items()
        if name in new_entries and old_entry != new_entries[name]
    ]

    if not (added or removed or changed):
        return None
    return CollectionChange(field=field_name, rule=rule, added=added, removed=removed, changed=changed, path=path)


def _occurrences(keys: list[ItemKey]) -> list[int]:
    """For each key, how many times the same key occurred before it.

    Pairing the n-th occurrence with the n-th keeps a collection holding a duplicate key
    comparable instead of dropping an item or raising.
    """
    seen: Counter[ItemKey] = Counter()
    positions = []
    for key in keys:
        positions.append(seen[key])
        seen[key] += 1
    return positions


def _diff_collection(
    field_name: str,
    rule: CollectionRule,
    item_owners: tuple[type[BaseModel], ...],
    old_value: Any,
    new_value: Any,
    path: AddressPath,
    *,
    registry: ModelRegistry | None = None,
) -> CollectionChange | None:
    """Compare one changed collection field item by item.

    Items are matched by :attr:`CollectionRule.key`, never by position; order is reported
    separately by :func:`_reordered`. A paired item that differs is diffed by
    :func:`_diff_element` with *item_owners*, which is the recursion. A mapping goes to
    :func:`_diff_mapping`. *path* is this field's own address; a changed item extends it by its
    member step.

    Returns:
        The change, or ``None``; never an empty, non-reordered :class:`CollectionChange`.
    """
    if rule.kind == "mapping":
        return _diff_mapping(field_name, rule, old_value, new_value, path)

    old_items = _collection_items(old_value)
    new_items = _collection_items(new_value)
    old_keys = [rule.key(item) for item in old_items]
    new_keys = [rule.key(item) for item in new_items]
    # Key plus occurrence, so two items with one key still pair one-to-one.
    old_pairs = list(zip(old_keys, _occurrences(old_keys), strict=True))
    new_pairs = list(zip(new_keys, _occurrences(new_keys), strict=True))
    old_positions = {pair: index for index, pair in enumerate(old_pairs)}

    added: list[Any] = []
    changed: list[ChangedItem] = []
    for index, pair in enumerate(new_pairs):
        old_index = old_positions.get(pair)
        if old_index is None:
            added.append(new_items[index])
            continue
        old_item, new_item = old_items[old_index], new_items[index]
        if old_item == new_item:
            continue
        # The new side, because it is the state the address denotes once the change is applied.
        item_path = (*path, _member_step(rule.member_identity(new_item)))
        changed_fields, collection_changes = _diff_element(
            old_item, new_item, item_owners, item_path, registry=registry
        )
        changed.append(
            ChangedItem(
                key=pair[0],
                changed_fields=changed_fields,
                collection_changes=collection_changes,
                path=item_path,
            )
        )

    surviving = set(new_pairs) & set(old_pairs)
    removed = [item for item, pair in zip(old_items, old_pairs, strict=True) if pair not in surviving]

    reordered = rule.reports_reorder and _reordered(old_keys, new_keys)
    if not (added or removed or changed or reordered):
        return None
    return CollectionChange(
        field=field_name,
        rule=rule,
        added=added,
        removed=removed,
        changed=changed,
        rename_hints=_rename_hints(removed, added, rule.key, item_owners),
        reordered=reordered,
        path=path,
    )


def _collection_rule_for(
    owners: tuple[type[BaseModel], ...], field_name: str, *, registry: ModelRegistry | None = None
) -> tuple[type[BaseModel], CollectionRule] | None:
    """The declaring owner and the rule for *field_name*, or ``None``.

    Two owners are passed when an element changed ``kind``; the declaring owner comes back so
    the caller can read the item type from its annotation. *registry* is threaded rather than
    resolved here so a diff's layer set and its collection detail come from one registry;
    ``None`` resolves the current one.
    """
    for owner in owners:
        rule = collection_rule(owner, field_name, registry=registry)
        if rule is not None:
            return owner, rule
    return None


def _diff_element(
    old_dump: Mapping[str, Any],
    new_dump: Mapping[str, Any],
    owners: tuple[type[BaseModel], ...],
    path: AddressPath = (),
    *,
    registry: ModelRegistry | None = None,
) -> tuple[list[tuple[str, Any, Any]], list[CollectionChange]]:
    """One pass over two dumped elements: what changed, in both forms.

    The whole-value tuple is recorded first and the per-item detail built from it, so the two
    cannot disagree. The fields compared are the union of both dumps, because an element that
    changed ``kind`` has different key sets. *owners* are the classes the dumps can be (empty for
    an item with no model type); *path* leads to the compared thing, and each field extends it by
    a ``{"field": ...}`` step.
    """
    changed_fields: list[tuple[str, Any, Any]] = []
    collection_changes: list[CollectionChange] = []
    for field_name in dict.fromkeys([*old_dump, *new_dump]):
        old_value = old_dump.get(field_name, _MISSING)
        new_value = new_dump.get(field_name, _MISSING)
        if old_value == new_value:
            continue
        changed_fields.append((field_name, old_value, new_value))
        found = _collection_rule_for(owners, field_name, registry=registry)
        if found is None:
            continue
        owner, rule = found
        collection_change = _diff_collection(
            field_name,
            rule,
            _item_owners(owner, field_name),
            old_value,
            new_value,
            (*path, _field_step(field_name)),
            registry=registry,
        )
        if collection_change is not None:
            collection_changes.append(collection_change)
    return changed_fields, collection_changes


def diff_layer(
    old_items: list[BaseModel],
    new_items: list[BaseModel],
    key_attr: str,
    layer_name: str,
    *,
    registry: ModelRegistry | None = None,
) -> LayerDiff:
    """Compare one model layer by identity key only (no rename merging).

    Elements present in both are compared via ``model_dump(by_alias=True)``, so aliased fields
    (Connector's ``from``/``to``) compare under their YAML names. *key_attr* is the identity
    field name (``name``, or ``id`` for ``requirements``). An element's own path is empty. An
    SWC's ``swc/{group}`` placement is not compared; ``group`` is immutable.
    """
    key_fn: Callable[[Any], str] = attrgetter(key_attr)
    old_map = build_key_map(old_items, key_fn, layer_name)
    new_map = build_key_map(new_items, key_fn, layer_name)

    added = [new_map[key] for key in new_map if key not in old_map]
    removed = [old_map[key] for key in old_map if key not in new_map]

    changed: list[ChangedItem] = []
    for key in old_map:
        if key not in new_map:
            continue
        old_element, new_element = old_map[key], new_map[key]
        old_dump = old_element.model_dump(by_alias=True)
        new_dump = new_element.model_dump(by_alias=True)
        if old_dump == new_dump:
            continue
        changed_fields, collection_changes = _diff_element(
            old_dump, new_dump, (type(old_element), type(new_element)), registry=registry
        )
        changed.append(ChangedItem(key=key, changed_fields=changed_fields, collection_changes=collection_changes))

    return LayerDiff(
        added=added,
        removed=removed,
        changed=changed,
        rename_hints=_rename_hints(
            [element.model_dump(by_alias=True) for element in removed],
            [element.model_dump(by_alias=True) for element in added],
            itemgetter(key_attr),
            # Every class among the candidates, so a union layer's declared defaults are all read.
            tuple(dict.fromkeys(type(element) for element in [*removed, *added])),
        ),
    )


@dataclass(frozen=True)
class _LayerRender:
    """One layer, as the two renderers need it.

    The layer name, the JSON key (the layer name when it has no snapshot attribute), the label
    and the identity field.
    """

    layer: str
    attribute: str
    label: str
    identity_field: str


def _layer_renders(registry: ModelRegistry) -> tuple[_LayerRender, ...]:
    """Every layer *registry* declares, in its declared ``displayOrder``.

    Both renderers emit their layers in this order; :class:`ModelDiff`'s field order is not it.
    """
    renders = []
    for name in registry.display_order:
        facts = registry.layer(name)
        renders.append(
            _LayerRender(
                layer=facts.name,
                attribute=facts.snapshot_attribute or facts.name,
                label=facts.display_label,
                identity_field=facts.identity_field,
            )
        )
    return tuple(renders)


#: An element (or item) sits this far in from the line naming what holds it.
_ITEM_STEP = 2
#: An item's own fields sit this far in from its ``+``/``-``/``~`` line, deeper than a sibling
#: item, so a field is never misread as another item.
_FIELD_STEP = 4


def _render_key(key: ItemKey, rule: CollectionRule | None = None) -> str:
    """One identity key as the reader sees it.

    A **composite** key is rendered as the model writes the reference: its set parts joined by
    ``.`` (``Chassis/Brake/Monitor.DetectFade``, ``pSpeed.speed``), the absent selector left out,
    as the validator's ``written_selector`` spells it. Any other tuple (an ``events`` value key)
    is a parenthesised list of its parts, so it cannot be mistaken for a name containing a space.
    """
    if isinstance(key, tuple):
        # `render_model_scalar` and not `repr`: one policy for every value read off a model element.
        parts = [part if isinstance(part, str) else render_model_scalar(part) for part in key]
        if rule is not None and rule.strategy == "composite":
            return ".".join(part for part, raw in zip(parts, key, strict=True) if raw is not None)
        return "(" + ", ".join(parts) + ")"
    return str(key)


def _render_hint(hint: RenameHint, indent: int, rule: CollectionRule | None = None) -> str:
    """A rename hint, worded as a possibility, never a verdict."""
    removed, added = _render_key(hint.removed_key, rule), _render_key(hint.added_key, rule)
    return f"{' ' * indent}(possibly a rename: {removed} -> {added})"


def _render_collection(change: CollectionChange, indent: int) -> list[str]:
    """One changed collection field: the items that differ, never the two lists.

    Added and removed items are named by ``change.rule``; a pure reorder renders as one
    ``(order changed)`` line.
    """
    item_indent = indent + _ITEM_STEP
    lines = [f"{' ' * indent}{change.field}:"]
    rule = change.rule
    lines.extend(f"{' ' * item_indent}+ {_render_key(rule.key(item), rule)} (added)" for item in change.added)
    lines.extend(f"{' ' * item_indent}- {_render_key(rule.key(item), rule)} (removed)" for item in change.removed)
    for changed_item in change.changed:
        lines.extend(_render_changed_item(changed_item, item_indent, change.rule))
    if change.reordered:
        lines.append(f"{' ' * item_indent}(order changed)")
    lines.extend(_render_hint(hint, item_indent, change.rule) for hint in change.rename_hints)
    return lines


def _render_changed_item(changed: ChangedItem, indent: int, rule: CollectionRule | None = None) -> list[str]:
    """One changed element, port, runnable or argument — the same shape at every depth.

    Fields come in declaration order, each printed **either** whole **or** as its per-item
    detail, never both.
    """
    detailed = {entry.field: entry for entry in changed.collection_changes}
    field_indent = indent + _FIELD_STEP
    lines = [f"{' ' * indent}~ {_render_key(changed.key, rule)}"]
    for field_name, old_value, new_value in changed.changed_fields:
        collection = detailed.get(field_name)
        if collection is None:
            lines.append(
                f"{' ' * field_indent}{field_name}: "
                f"{render_model_scalar(old_value)} -> {render_model_scalar(new_value)}"
            )
        else:
            lines.extend(_render_collection(collection, field_indent))
    return lines


def format_layer(layer_diff: LayerDiff, label: str, key_attr: str) -> str:
    """Render one layer's diff under *label*, each item named by its *key_attr*: added, removed, changed."""
    lines = [f"{label}:"]
    for item in layer_diff.added:
        lines.append(f"{' ' * _ITEM_STEP}+ {getattr(item, key_attr)} (added)")
    for item in layer_diff.removed:
        lines.append(f"{' ' * _ITEM_STEP}- {getattr(item, key_attr)} (removed)")
    for changed in layer_diff.changed:
        lines.extend(_render_changed_item(changed, _ITEM_STEP))
    lines.extend(_render_hint(hint, _ITEM_STEP) for hint in layer_diff.rename_hints)
    return "\n".join(lines)


def format_diff(diff: ModelDiff, *, registry: ModelRegistry | None = None) -> str:
    """Render a ModelDiff as plain text, ``+``/``-``/``~`` grouped by layer (valid Markdown as-is).

    Unchanged layers, and layers :attr:`ModelDiff.by_layer` does not carry, produce no output;
    ``"No changes."`` when none changed. Sections follow the declared ``displayOrder`` under the
    declared ``label``. A changed collection field prints its per-item detail.

    *registry* chooses the sections and builds the layer-keyed diff when the diff carries none.
    It does not reach a diff that carries its own (every :func:`diff_snapshots` result): that one
    is read as it was taken.
    """
    registry = registry or current_registry()
    by_layer = _by_layer_of(diff, registry)
    sections = [
        format_layer(by_layer[render.layer], render.label, render.identity_field)
        for render in _layer_renders(registry)
        if render.layer in by_layer and not by_layer[render.layer].is_empty
    ]
    if not sections:
        return "No changes."
    return "\n\n".join(sections)


#: The ``--format json`` document's major version (the module docstring says what raises it).
DIFF_SCHEMA_VERSION = 2

#: How a non-finite float is spelled in the document: the literals ``json.dumps`` would emit bare,
#: as JSON strings every parser reads.
NON_FINITE_NAMES = ("NaN", "Infinity", "-Infinity")


def _json_finite(value: Any) -> Any:
    """The whole document with every non-finite float replaced by its name.

    RFC 8259 has no literal for NaN or infinity; a strict parser rejects the bare literals and
    ``jq`` reads ``NaN`` as ``null``. The name is a **string**, not ``null``, which already means
    "absent from this side"; it can collide with a legitimate string ``"NaN"``, which the field's
    type disambiguates. The value is not rejected here: ``swcraft diff`` is read-only, and a
    malformed model is ``check-model`` / ``validate``'s business.

    Not reached: dict **keys** are not converted; a non-finite key raises under
    ``allow_nan=False`` in :func:`format_diff_json`.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, Mapping):
        return {key: _json_finite(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_finite(item) for item in value]
    return value


def _json_key(key: ItemKey) -> Any:
    """One identity key as JSON: a string, or a list of a composite's parts.

    Unlike :func:`_render_key`, every part is kept, so a program need not parse punctuation.
    """
    if isinstance(key, tuple):
        return [_json_key(part) for part in key]
    return key


def _json_value(value: Any) -> Any:
    """One whole-value side, with the absent-field sentinel rendered as ``null``.

    A field is absent from one side only when an element changed ``kind``.
    """
    return None if value is _MISSING else value


def _json_hint(hint: RenameHint) -> dict[str, Any]:
    return {"removed_key": _json_key(hint.removed_key), "added_key": _json_key(hint.added_key)}


def _json_collection(change: CollectionChange) -> dict[str, Any]:
    """One changed collection field, carrying every fact the text renderer shows.

    ``added``/``removed`` are named by ``change.rule``, as :func:`_render_collection` names them.
    ``reordered`` is always emitted.
    """
    return {
        "field": change.field,
        "added": [_json_key(change.rule.key(item)) for item in change.added],
        "removed": [_json_key(change.rule.key(item)) for item in change.removed],
        "changed": [_json_changed_item(item) for item in change.changed],
        "rename_hints": [_json_hint(hint) for hint in change.rename_hints],
        "reordered": change.reordered,
    }


def _json_changed_item(changed: ChangedItem) -> dict[str, Any]:
    """One changed element, port, runnable, argument or memory budget.

    Each field of ``changed_fields``, in declaration order, goes to ``fields`` *or* to
    ``collections``, never both.
    """
    detailed = {entry.field: entry for entry in changed.collection_changes}
    fields: list[dict[str, Any]] = []
    collections: list[dict[str, Any]] = []
    for field_name, old_value, new_value in changed.changed_fields:
        collection = detailed.get(field_name)
        if collection is None:
            fields.append({"field": field_name, "old": _json_value(old_value), "new": _json_value(new_value)})
        else:
            collections.append(_json_collection(collection))
    return {"key": _json_key(changed.key), "fields": fields, "collections": collections}


def _json_layer(layer_diff: LayerDiff, key_attr: str) -> dict[str, Any]:
    return {
        "added": [getattr(item, key_attr) for item in layer_diff.added],
        "removed": [getattr(item, key_attr) for item in layer_diff.removed],
        "changed": [_json_changed_item(changed) for changed in layer_diff.changed],
        "rename_hints": [_json_hint(hint) for hint in layer_diff.rename_hints],
    }


def format_diff_json(diff: ModelDiff, *, registry: ModelRegistry | None = None) -> str:
    """Render a ModelDiff as the versioned JSON document the module docstring describes.

    An empty diff is the same envelope with four empty lists under every layer the registry
    declares, whether or not the diff carries it. ``allow_nan=False`` turns a non-finite value
    :func:`_json_finite` missed into a ``ValueError`` rather than unparseable output. *registry*
    is used as in :func:`format_diff`.
    """
    registry = registry or current_registry()
    by_layer = _by_layer_of(diff, registry)
    document = {
        "schema_version": DIFF_SCHEMA_VERSION,
        "layers": {
            render.attribute: _json_layer(by_layer.layers.get(render.layer, LayerDiff()), render.identity_field)
            for render in _layer_renders(registry)
        },
    }
    return json.dumps(_json_finite(document), indent=2, allow_nan=False) + "\n"


#: How many identities one verb of one layer names in :func:`summarize_diff`; the rest are
#: counted. The bound is on names, not bytes, so very long names can still exceed a byte budget,
#: which is why the MCP ``model_diff`` measures the summary and refuses it above its budget.
DIFF_SUMMARY_IDENTITIES = 50

#: The three verbs of the summary, in the order :func:`format_diff` prints them.
DIFF_SUMMARY_VERBS = ("added", "removed", "changed")


def _summary_verb(identities: list[Any]) -> dict[str, Any]:
    return {"count": len(identities), "identities": identities[:DIFF_SUMMARY_IDENTITIES]}


def _summary_layer(layer_diff: LayerDiff, key_attr: str) -> dict[str, Any]:
    """One layer's verbs as counts and bounded identities — :func:`_json_layer` without the detail."""
    return {verb: _summary_verb(_summary_identities(layer_diff, verb, key_attr)) for verb in DIFF_SUMMARY_VERBS}


def _summary_identities(layer_diff: LayerDiff, verb: str, key_attr: str) -> list[Any]:
    if verb == "changed":
        return [_json_key(changed.key) for changed in layer_diff.changed]
    return [getattr(item, key_attr) for item in getattr(layer_diff, verb)]


def summarize_diff(diff: ModelDiff, *, registry: ModelRegistry | None = None) -> dict[str, Any]:
    """The diff as per-layer counts and identities, at most :data:`DIFF_SUMMARY_IDENTITIES` per verb.

    ``{"layers": {layer: {verb: {"count", "identities"}}}, "totals": {verb: n}}``, keyed by the
    contract's **layer name** in the declared ``displayOrder``, carrying only changed layers. An
    empty diff has no layer and zero totals. No value is carried, only identities.
    """
    registry = registry or current_registry()
    by_layer = _by_layer_of(diff, registry)
    layers = {
        render.layer: _summary_layer(by_layer[render.layer], render.identity_field)
        for render in _layer_renders(registry)
        if render.layer in by_layer and not by_layer[render.layer].is_empty
    }
    totals = {verb: sum(layer[verb]["count"] for layer in layers.values()) for verb in DIFF_SUMMARY_VERBS}
    return {"layers": layers, "totals": totals}


def diff_model_states(old: ModelState, new: ModelState, *, registry: ModelRegistry | None = None) -> ModelStateDiff:
    """Compare two model states, layer by layer, in the contract's own vocabulary.

    Every layer *registry* declares is compared; a state that lacks one contributes ``[]``, so a
    layer added in full reads as additions. A layer present in a state but declared by no
    registry is not compared. The layer name in a duplicate-key refusal is the snapshot
    attribute where one is declared (``swcs``). *registry* is threaded down the whole
    comparison, so one diff comes from one registry.

    Raises:
        ValueError: two elements of one layer share an identity key
            (:func:`build_key_map`).
    """
    registry = registry or current_registry()
    layers: dict[str, LayerDiff] = {}
    for name in registry.layer_names:
        facts = registry.layer(name)
        layers[name] = diff_layer(
            old.layers.get(name, []),
            new.layers.get(name, []),
            facts.identity_field,
            facts.snapshot_attribute or facts.name,
            registry=registry,
        )
    return ModelStateDiff(layers=layers)


def diff_snapshots(old: ModelSnapshot, new: ModelSnapshot) -> ModelDiff:
    """Compare two ModelSnapshots across every model layer.

    The adapter over :func:`diff_model_states`, using the current registry; the result carries
    the layer-keyed diff as :attr:`ModelDiff.by_layer`.
    """
    return ModelDiff.from_state_diff(diff_model_states(old.state, new.state))
