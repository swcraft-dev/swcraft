"""The address model: ``{layer, element?, path?}``, resolution and prefix coverage.

The wire shapes are the protocol schema's ``$defs/address``, ``$defs/pathStep``
and ``$defs/identity``. Nothing here writes, stages or validates: it answers
*where* an address points and *whether* one address covers another.

**Storage is never addressable.** No directory, filename, extension, separator or
``swc/{group}`` directory appears in an address; the ``element`` identity is held
to a single segment (``.`` and ``..`` refused).

**There is no ordinal step.** A step is a :class:`FieldStep`, :class:`MemberStep`
or :class:`KeyStep`; an index would denote another member once anything is
inserted before it.

**A ``whole_value`` LIST is a MULTISET** (members are counted, a duplicate is never
an identity conflict); **a ``whole_value`` MAP is not** (an entry is reached by a
:class:`KeyStep`, and :func:`member_indices` refuses a map).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from swcraft_tools.editing.registry import (
    CollectionDescriptor,
    ElementKindDescriptor,
    FieldDescriptor,
    Registry,
    or_current,
)
from swcraft_tools.model_registry import complete_composite_identity as _complete_identity

__all__ = [
    "Address",
    "AddressError",
    "ChangeOperation",
    "FieldStep",
    "IDENTITY_PATTERN",
    "KeyStep",
    "LAYER_NAME_PATTERN",
    "MemberStep",
    "Miss",
    "MissReason",
    "PathStep",
    "Resolution",
    "Resolved",
    "ScopeEntry",
    "attribute_for",
    "canonical_value",
    "collection_at",
    "covers",
    "covers_any",
    "identity_key",
    "is_address_prefix",
    "member_identity_of",
    "member_indices",
    "member_matches",
    "reduce_address",
    "reduce_whole_value",
    "resolve",
    "values_equal",
]


#: ``$defs/identity``: the one client-supplied token the registry composes a
#: storage path from, so it is held to a single path segment. Deliberately NOT
#: the AUTOSAR ``Identifier`` pattern, which ``requirements`` ids
#: (``REQ-SPEED-001``) legitimately violate; that rule is checked on the
#: *emitted* form and reported as ``invalid-identifier``.
IDENTITY_PATTERN = re.compile(r"^(?!\.\.?$)[^/\\\x00]+$")

#: ``$defs/layerName``. A *pattern*, not an enum: the registry derives the layer
#: set from ``schema.py``, so an undeclared layer is an ``unknown-field``
#: failure at resolution rather than a malformed envelope.
LAYER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: ``$defs/changeOperationName``.
ChangeOperation = Literal["create", "update", "remove"]

_CHANGE_OPERATIONS: frozenset[str] = frozenset({"create", "update", "remove"})


class AddressError(ValueError):
    """An address or a path step is not well formed.

    Every condition is structural (an unknown step form, a ``path`` without an
    ``element``, an empty ``path``, a storage path, a traversal identity). Pass A
    maps it onto ``schema-violation``; it is never a semantic rejection.
    """


# ---------------------------------------------------------------------------
# Canonical values — one comparison rule for every identity in the protocol
# ---------------------------------------------------------------------------


def canonical_value(value: Any) -> Any:
    """The JSON-shaped form of a model value, for comparison against client JSON.

    ``by_alias=True`` matches ``differ.py``'s dump, so the address vocabulary
    and the review surface's are one.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {key: canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    return value


def identity_key(value: Any) -> Any:
    """A hashable canonical key for one value — ``differ.py``'s ``_ValueKey``, generalized.

    Where raw ``==`` is wrong for an identity:

    - **a boolean is not an integer** (``True == 1`` in Python);
    - **an integer and a float are one number where numerically equal**: JSON
      spells ``1`` where a ``float`` field dumps ``1.0``. An ``int`` no float
      holds exactly (a 64-bit bound, an unbounded ``init``) is keyed as itself:
      through ``float()`` two neighbouring bounds would become one number, and a
      sole edit between them no edit.
    """
    value = canonical_value(value)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, float):
        return ("number", value)
    if isinstance(value, int):
        # Sound because these keys are only compared for equality and hashed,
        # never sorted: a wide `int` equals only an `int` of the same value.
        try:
            as_float = float(value)
        except OverflowError:
            return ("number-wide", value)
        if as_float != value:
            return ("number-wide", value)
        return ("number", as_float)
    if isinstance(value, str):
        return ("string", value)
    if value is None:
        return ("null", None)
    if isinstance(value, Mapping):
        return ("map", tuple(sorted(((key, identity_key(item)) for key, item in value.items()), key=lambda kv: kv[0])))
    if isinstance(value, (list, tuple)):
        return ("list", tuple(identity_key(item) for item in value))
    return ("other", repr(value))


def values_equal(left: Any, right: Any) -> bool:
    """Canonical equality — the equality every member step and every step comparison uses."""
    return identity_key(left) == identity_key(right)


# ---------------------------------------------------------------------------
# Whole-value identity — a default-equal field is not part of it
# ---------------------------------------------------------------------------


def reduce_whole_value(value: Any, collection: CollectionDescriptor | None) -> Any:
    """A whole-value member's **identity**: its dumped value without the fields at their default.

    Removes every key equal to the **contract's** declared default for the member
    kind the discriminator selects (never Pydantic's ``exclude_defaults``); a
    required field is never removed. **Identities only**: a value keeps the full
    form. A non-mapping member, or one whose kind cannot be told, is returned
    unchanged. One level deep only.
    """
    if collection is None or collection.member_identity != "whole_value":
        return value
    value = canonical_value(value)
    if not isinstance(value, Mapping):
        return value
    defaults = collection.defaults_for(value)
    if not defaults:
        return value
    return {key: item for key, item in value.items() if not (key in defaults and values_equal(item, defaults[key]))}


# ---------------------------------------------------------------------------
# Path steps — three forms, and deliberately no fourth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldStep:
    """``{field: "ports"}`` — a declared field of the element the cursor stands on."""

    field: str

    def to_json(self) -> dict[str, Any]:
        return {"field": self.field}


@dataclass(frozen=True)
class KeyStep:
    """``{key: "RAM"}`` — one entry of a map collection, addressed by its key.

    In a finding it may also name a key no field declares (``model.unknown-key``,
    through :func:`~swcraft_tools.editing.diagnosis.subject_address`): a key the
    loader dropped. Nothing resolves such an address, so the repair is by hand.
    """

    key: str

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key}


@dataclass(frozen=True, eq=False)
class MemberStep:
    """``{member: ...}`` — one member of a list collection, addressed by its identity.

    The value is a string for ``field``, an object for ``composite`` and the
    member's own JSON value, of any type, for ``whole_value``. Equality is
    canonical (:func:`identity_key`), so steps holding equal dicts compare
    and hash equal.
    """

    member: Any

    @property
    def key(self) -> Any:
        return identity_key(self.member)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MemberStep) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def to_json(self) -> dict[str, Any]:
        return {"member": canonical_value(self.member)}


PathStep = FieldStep | MemberStep | KeyStep

#: The three step forms ``$defs/pathStep`` admits; any other is refused, never
#: reinterpreted.
_STEP_KEYS: dict[str, type] = {"field": FieldStep, "member": MemberStep, "key": KeyStep}


def _step_from_json(data: Any, position: int) -> PathStep:
    if not isinstance(data, Mapping):
        raise AddressError(f"path step {position} must be an object, not {type(data).__name__}")
    keys = set(data)
    if len(keys) != 1:
        raise AddressError(
            f"path step {position} must carry exactly one of {sorted(_STEP_KEYS)}, not {sorted(keys) or 'nothing'}"
        )
    (name,) = keys
    if name not in _STEP_KEYS:
        raise AddressError(
            f"path step {position} has an unknown form '{name}'; the three forms are {sorted(_STEP_KEYS)} "
            "and there is deliberately no ordinal step"
        )
    value = data[name]
    if name == "member":
        return MemberStep(value)
    if not isinstance(value, str) or not value:
        raise AddressError(f"path step {position} '{name}' must be a non-empty string")
    return _STEP_KEYS[name](value)


# ---------------------------------------------------------------------------
# The address
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Address:
    """``$defs/address``: ``{layer, element?, path?}``, and never a string path.

    Enforced on construction, not only on parsing: **a ``path`` needs an
    ``element``** (``dependentRequired``, which deliberately makes a per-layer
    field wildcard inexpressible in a scope), and **an ``element`` is a single
    path segment** (``$defs/identity``).
    """

    layer: str
    element: str | None = None
    path: tuple[PathStep, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.layer, str) or not LAYER_NAME_PATTERN.fullmatch(self.layer):
            raise AddressError(f"layer '{self.layer}' is not a layer name")
        if self.element is not None:
            if not isinstance(self.element, str) or not IDENTITY_PATTERN.match(self.element):
                raise AddressError(
                    f"element identity '{self.element}' is not a single path segment: '.' and '..' are refused, "
                    "and so is any value carrying '/', '\\\\' or NUL"
                )
        if self.path and self.element is None:
            raise AddressError("a path without an element is not an address (dependentRequired: path -> element)")
        object.__setattr__(self, "path", tuple(self.path))

    @classmethod
    def from_json(cls, data: Any) -> Address:
        """The parsed ``$defs/address``; everything it does not admit is refused.

        A string is refused outright: a storage path is not an under-specified
        address, it is a different kind of thing.
        """
        if not isinstance(data, Mapping):
            raise AddressError(
                f"an address must be an object, not {type(data).__name__}; no request field takes a storage path"
            )
        unknown = sorted(set(data) - {"layer", "element", "path"})
        if unknown:
            raise AddressError(f"an address carries no {unknown} (additionalProperties: false)")
        if "layer" not in data:
            raise AddressError("an address must carry a layer")
        # Key PRESENCE, never truthiness: an omitted key is a wildcard, so an
        # explicit `null` read as omitted would silently widen an element-scoped
        # approval to the whole layer (and `to_json` would hide it on echo). The
        # schema admits no `null` either.
        if "element" in data and not isinstance(data["element"], str):
            raise AddressError(
                f"element must be a string, not {type(data['element']).__name__}; omit the key for the "
                "layer-wide wildcard — an explicit null is not the same statement"
            )
        steps: tuple[PathStep, ...] = ()
        if "path" in data:
            raw_path = data["path"]
            if not isinstance(raw_path, Sequence) or isinstance(raw_path, (str, bytes)):
                raise AddressError(
                    f"path must be an array, not {type(raw_path).__name__}; omit the key for the "
                    "whole-element wildcard — an explicit null is not the same statement"
                )
            if not raw_path:
                raise AddressError("path must carry at least one step (minItems: 1)")
            steps = tuple(_step_from_json(step, index) for index, step in enumerate(raw_path))
        return cls(layer=data["layer"], element=data.get("element"), path=steps)

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"layer": self.layer}
        if self.element is not None:
            result["element"] = self.element
        if self.path:
            result["path"] = [step.to_json() for step in self.path]
        return result

    @property
    def is_element_address(self) -> bool:
        """``$defs/elementAddress`` — an address that names an element."""
        return self.element is not None

    @property
    def is_field_address(self) -> bool:
        """``$defs/fieldAddress`` — an address that names something inside an element."""
        return self.element is not None and bool(self.path)


# ---------------------------------------------------------------------------
# Coverage — the authorization relation, stated once
# ---------------------------------------------------------------------------


def is_address_prefix(prefix: Address, candidate: Address) -> bool:
    """Whether ``prefix`` is an **address prefix** of ``candidate``.

    The coverage rule's conditions 2 and 3: the same layer,
    ``prefix.element`` absent or equal, and ``prefix.path`` absent or an
    element-wise prefix of ``candidate.path``.

    **Omission is the wildcard.** Leaving ``element`` out covers every element of
    the layer; leaving ``path`` out covers every change inside the element. A
    per-layer *field* wildcard is not expressible.
    """
    if prefix.layer != candidate.layer:
        return False
    if prefix.element is None:
        return True
    if prefix.element != candidate.element:
        return False
    if not prefix.path:
        return True
    if len(prefix.path) > len(candidate.path):
        return False
    return all(mine == theirs for mine, theirs in zip(prefix.path, candidate.path))


@dataclass(frozen=True)
class ScopeEntry:
    """One entry of an approved scope: an operation and an address.

    The operation is part of the key and the match is **exact**, with no
    implication between operations: approving an ``update`` of an element never
    authorizes *removing* it.
    """

    operation: ChangeOperation
    address: Address

    def __post_init__(self) -> None:
        if self.operation not in _CHANGE_OPERATIONS:
            raise AddressError(f"operation '{self.operation}' is not one of {sorted(_CHANGE_OPERATIONS)}")

    @classmethod
    def from_json(cls, data: Any) -> ScopeEntry:
        if not isinstance(data, Mapping):
            raise AddressError(f"a scope entry must be an object, not {type(data).__name__}")
        unknown = sorted(set(data) - {"operation", "address"})
        if unknown:
            raise AddressError(f"a scope entry carries no {unknown}")
        if "operation" not in data or "address" not in data:
            raise AddressError("a scope entry must carry an operation and an address")
        return cls(operation=data["operation"], address=Address.from_json(data["address"]))

    def to_json(self) -> dict[str, Any]:
        return {"operation": self.operation, "address": self.address.to_json()}

    def covers(self, operation: str, address: Address) -> bool:
        return covers(self, operation, address)


def covers(entry: ScopeEntry, operation: str, address: Address) -> bool:
    """Whether one scope entry covers one delta entry: all three conditions, in order."""
    return entry.operation == operation and is_address_prefix(entry.address, address)


def covers_any(entries: Iterable[ScopeEntry], operation: str, address: Address) -> bool:
    """Whether a delta entry is covered by **at least one** scope entry.

    A scope entry no delta entry matches is unused, not an error: over-approval
    is the user's own authority.
    """
    return any(covers(entry, operation, address) for entry in entries)


# ---------------------------------------------------------------------------
# Member matching — the three strategies, and the multiset
# ---------------------------------------------------------------------------


def attribute_for(model: type[BaseModel], published: str) -> str | None:
    """The Python attribute behind a published field name (``from`` -> ``from_``).

    Aliases are resolved once, here, against ``model_fields``, as the registry
    derives its field names; the Python attribute name never leaves this function.
    """
    for attribute, info in model.model_fields.items():
        if (info.alias or attribute) == published:
            return attribute
    return None


def _published_field_value(member: Any, name: str, optional: frozenset[str] | tuple[str, ...] = ()) -> Any:
    """One field of a collection member, by the name the registry publishes.

    *optional* names the identity fields the contract declares optional: a
    Mapping that leaves one out reads it as ``None``, as a model member does. A
    missing field outside *optional* is still missing.
    """
    if isinstance(member, BaseModel):
        attribute = attribute_for(type(member), name)
        if attribute is None:
            return _MISSING
        return getattr(member, attribute)
    if isinstance(member, Mapping):
        return member.get(name, None if name in optional else _MISSING)
    return _MISSING


def complete_composite_identity(identity: Any, descriptor: CollectionDescriptor | None) -> Any:
    """A composite identity object with every declared-optional identity field it leaves out spelled ``None``.

    Anything not completed is returned as written; resolving it is what refuses it.
    """
    if descriptor is None or descriptor.member_identity != "composite":
        return identity
    return _complete_identity(identity, descriptor.identity_fields, descriptor.optional_identity_fields)


class _Missing:
    """A field a member does not carry — never equal to anything a client can send."""

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<missing>"


_MISSING = _Missing()


def member_matches(member: Any, descriptor: CollectionDescriptor, identity: Any) -> bool:
    """Whether one collection member carries the given identity, under its declared strategy.

    ``field`` — the declared identity field equals the identity string.
    ``composite`` — the identity object carries exactly the declared tuple and
    every field equals. ``whole_value`` — the whole value is compared
    canonically, both sides reduced by :func:`reduce_whole_value`.
    """
    if descriptor.member_identity == "field":
        if not descriptor.identity_field:
            return False
        value = _published_field_value(member, descriptor.identity_field)
        return value is not _MISSING and values_equal(value, identity)
    if descriptor.member_identity == "composite":
        if not isinstance(identity, Mapping):
            return False
        identity = complete_composite_identity(identity, descriptor)
        if set(identity) != set(descriptor.identity_fields):
            return False
        for name in descriptor.identity_fields:
            value = _published_field_value(member, name, descriptor.optional_identity_fields)
            if value is _MISSING or not values_equal(value, identity[name]):
                return False
        return True
    return values_equal(reduce_whole_value(member, descriptor), reduce_whole_value(identity, descriptor))


def member_identity_of(member: Any, descriptor: CollectionDescriptor) -> Any:
    """The identity one collection member carries — the inverse of :func:`member_matches`.

    ``None`` for a member that does not carry the declared identity, deliberately
    not an error: the refusal is the owning element's Pydantic validation. A
    caller comparing identities treats "no identity" as one more value, the
    fail-closed direction: it matches only another member that has none.
    """
    if descriptor.member_identity == "field":
        if not descriptor.identity_field:
            return None
        value = _published_field_value(member, descriptor.identity_field)
        return None if value is _MISSING else canonical_value(value)
    if descriptor.member_identity == "composite":
        identity: dict[str, Any] = {}
        for name in descriptor.identity_fields:
            value = _published_field_value(member, name, descriptor.optional_identity_fields)
            if value is _MISSING:
                return None
            identity[name] = canonical_value(value)
        return identity
    return reduce_whole_value(member, descriptor)


def member_indices(collection: Any, descriptor: CollectionDescriptor, identity: Any) -> tuple[int, ...]:
    """**Every** index in a list collection whose member carries the identity (the multiset rule).

    Raises:
        AddressError: for a map, whose entries are addressed by :class:`KeyStep`.
    """
    if descriptor.kind != "list":
        raise AddressError(
            f"a {descriptor.kind} collection has no members to match: its entries are addressed by key, "
            "and a key is unique by construction"
        )
    if not isinstance(collection, Sequence) or isinstance(collection, (str, bytes)):
        return ()
    return tuple(index for index, member in enumerate(collection) if member_matches(member, descriptor, identity))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

MissReason = Literal[
    "no_such_layer",
    "no_such_element",
    "no_such_field",
    "no_such_member",
    "no_such_key",
    "not_traversable",
    "placement_unavailable",
]


@dataclass(frozen=True)
class Resolved:
    """An address that resolves, and what it resolves to.

    ``container`` is the object the last step was taken from — the snapshot for
    a layer address, the element for a field step, the collection for a member
    or key step — which is what an in-memory apply mutates.
    """

    value: Any
    container: Any = None
    kind: ElementKindDescriptor | None = None
    field: FieldDescriptor | None = None
    collection: CollectionDescriptor | None = None

    found: ClassVar[Literal[True]] = True


@dataclass(frozen=True)
class Miss:
    """A typed miss, carrying the step that failed.

    ``step_index`` is ``None`` for a miss at the layer or the element. The
    reason is deliberately finer than the protocol's codes, so a pass can decide
    between ``unknown-field`` and ``broken-reference``.
    """

    reason: MissReason
    address: Address
    detail: str
    step_index: int | None = None
    step: PathStep | None = None

    found: ClassVar[Literal[False]] = False


Resolution = Resolved | Miss


def resolve(
    address: Address,
    snapshot: Any,
    *,
    registry: Registry | None = None,
    placement: Mapping[str, str] | None = None,
) -> Resolution:
    """The resolution of an address against a snapshot, or which step failed.

    ``snapshot`` is anything that answers
    :attr:`~swcraft_tools.snapshot.ModelSnapshot.state` (the real snapshot or a
    staged copy); the layer is read **by layer name**, since a layer with no
    ``snapshotAttributes`` row has no attribute. ``placement`` supplies the
    element's placement values (``swc.group``), which live beside the parsed
    element rather than in it.
    """
    registry = or_current(registry)
    try:
        layer = registry.layer(address.layer)
    except KeyError:
        return Miss("no_such_layer", address, f"no layer '{address.layer}' is declared")
    state = snapshot.state
    if layer.name not in state:
        return Miss("no_such_layer", address, f"the snapshot carries no '{layer.snapshot_attribute}'")
    elements = state[layer.name]

    if address.element is None:
        return Resolved(value=elements, container=snapshot)

    identity_field = layer.identity_field
    element = next(
        (
            candidate
            for candidate in elements
            if values_equal(_published_field_value(candidate, identity_field), address.element)
        ),
        None,
    )
    if element is None:
        return Miss(
            "no_such_element",
            address,
            f"no '{address.layer}' element with {identity_field} '{address.element}'",
        )

    cursor: Any = element
    container: Any = elements
    kind = registry.kind_of_model(type(element))
    field_descriptor: FieldDescriptor | None = None
    collection: CollectionDescriptor | None = None
    # A `whole_value` member has no addressable interior: its fields are leaves,
    # so a member or key step below it is `not_traversable`.
    whole_value_member = False

    for index, step in enumerate(address.path):
        if isinstance(step, FieldStep):
            if kind is None:
                return Miss("not_traversable", address, f"'{step.field}' has no declared fields", index, step)
            descriptor = kind.get_field(step.field)
            if descriptor is None:
                return Miss(
                    "no_such_field",
                    address,
                    f"element kind '{kind.kind}' declares no field '{step.field}'",
                    index,
                    step,
                )
            if descriptor.placement:
                if placement is None or descriptor.name not in placement:
                    return Miss(
                        "placement_unavailable",
                        address,
                        f"'{descriptor.name}' is placement metadata and none was supplied for this element",
                        index,
                        step,
                    )
                container, cursor = cursor, placement[descriptor.name]
            else:
                attribute = attribute_for(type(cursor), descriptor.name)
                if attribute is None:  # pragma: no cover - the registry derives from model_fields
                    return Miss("no_such_field", address, f"no attribute behind '{descriptor.name}'", index, step)
                container, cursor = cursor, getattr(cursor, attribute)
            field_descriptor = descriptor
            collection = None if whole_value_member else descriptor.collection
            kind = registry.kind_of_model(type(cursor)) if isinstance(cursor, BaseModel) else None
            continue

        if isinstance(step, MemberStep):
            if collection is None or collection.kind != "list":
                return Miss("not_traversable", address, "a member step needs a list collection above it", index, step)
            matches = member_indices(cursor, collection, step.member)
            if not matches:
                return Miss(
                    "no_such_member",
                    address,
                    f"no member of '{field_descriptor.name if field_descriptor else '?'}' carries that identity",
                    index,
                    step,
                )
            whole_value_member = collection.member_identity == "whole_value"
            container, cursor = cursor, cursor[matches[0]]
            kind = registry.kind_of_model(type(cursor)) if isinstance(cursor, BaseModel) else None
            field_descriptor = None
            collection = None
            continue

        if collection is None or collection.kind != "map":
            return Miss("not_traversable", address, "a key step needs a map collection above it", index, step)
        if not isinstance(cursor, Mapping) or step.key not in cursor:
            return Miss(
                "no_such_key",
                address,
                f"'{field_descriptor.name if field_descriptor else '?'}' holds no key '{step.key}'",
                index,
                step,
            )
        container, cursor = cursor, cursor[step.key]
        kind = None
        field_descriptor = None
        collection = None

    return Resolved(value=cursor, container=container, kind=kind, field=field_descriptor, collection=collection)


# ---------------------------------------------------------------------------
# The registry walk an address is parsed against
# ---------------------------------------------------------------------------


def _walk_steps(
    layer: str, steps: Sequence[PathStep], registry: Registry
) -> tuple[tuple[PathStep, ...], CollectionDescriptor | None]:
    """*steps* walked through the registry's kinds, without a model: reduced steps, and the collection they end on.

    Every :class:`MemberStep` into a ``whole_value`` list is replaced by its
    :func:`reduce_whole_value` form. A layer holding several kinds (the
    ``interfaces`` layer) is walked through every kind that declares the field,
    the first declaration with a collection answering. A step the registry
    cannot place is kept as it is and ends the walk; the resolver refuses it.
    """
    try:
        kinds: list[ElementKindDescriptor] = list(registry.layer(layer).top_level_kinds)
    except KeyError:
        return tuple(steps), None
    collection: CollectionDescriptor | None = None
    whole_value_member = False
    reduced: list[PathStep] = []
    for index, step in enumerate(steps):
        if isinstance(step, FieldStep):
            declared = [d for d in (k.get_field(step.field) for k in kinds) if d is not None]
            with_collection = next((d for d in declared if d.collection is not None), None)
            descriptor = with_collection or (declared[0] if declared else None)
            reduced.append(step)
            if descriptor is None:
                return (*reduced, *steps[index + 1 :]), None
            collection = None if whole_value_member else descriptor.collection
            element_kind = descriptor.element_kind
            kinds = (
                [registry.kinds[element_kind]] if element_kind is not None and element_kind in registry.kinds else []
            )
            continue
        if isinstance(step, MemberStep) and collection is not None and collection.kind == "list":
            if collection.member_identity == "whole_value":
                reduced.append(MemberStep(reduce_whole_value(step.member, collection)))
                whole_value_member = True
            elif collection.member_identity == "composite":
                # One spelling: a declared-optional identity field left out is `null`.
                reduced.append(MemberStep(complete_composite_identity(step.member, collection)))
            else:
                reduced.append(step)
            kinds = [registry.kinds[name] for name in collection.member_kinds if name in registry.kinds]
            collection = None
            continue
        reduced.append(step)
        if not isinstance(step, KeyStep) or collection is None:
            return (*reduced, *steps[index + 1 :]), None
        kinds = []
        collection = None
    return tuple(reduced), collection


def collection_at(layer: str, path: Sequence[Any], registry: Registry | None = None) -> CollectionDescriptor | None:
    """The collection a path of ``$defs/pathStep`` JSON (or parsed steps) ends on, or ``None``.

    Asked by the delta projection, so a changed member's ``{member: ...}`` step
    is published in its reduced form.
    """
    registry = or_current(registry)
    steps = tuple(
        step if isinstance(step, (FieldStep, MemberStep, KeyStep)) else _step_from_json(step, index)
        for index, step in enumerate(path)
    )
    return _walk_steps(layer, steps, registry)[1]


def reduce_address(address: Address, registry: Registry | None = None) -> Address:
    """*address* with every whole-value member step in its reduced form.

    Only the steps the registry can place are reduced; resolving the rest is what refuses it.
    """
    registry = or_current(registry)
    if not address.path:
        return address
    steps, _ = _walk_steps(address.layer, address.path, registry)
    if steps == address.path:
        return address
    return Address(layer=address.layer, element=address.element, path=steps)
