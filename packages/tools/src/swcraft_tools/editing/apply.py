"""The in-memory apply, and the binding pass B decides over.

Operations are applied in memory, with **no I/O**; every mutated element is
re-validated through its Pydantic class.

**``touched`` means the value actually changed**, not that an operation named it;
it is always a subset of what the operations name.

**Pass B reports every violation in one round trip**; an operation whose address
does not resolve at that point is an :class:`InapplicableOperation` for pass C.

1. **The same-identity ban**: a ``remove`` and a ``create`` at the same identity
   in one change set is ``identity-conflict``.
2. **A ``whole_value`` list is a multiset**: a duplicate is never a conflict, a
   ``remove`` takes one occurrence, and its members are exempt from rule 1. A map
   is not: ``create`` only at an absent key, ``update`` only at a held one.
3. **A list ``create`` appends**: an address has no position. Reordering is an
   ``update`` of the whole collection, which must keep its member identities at
   every depth, omitted collections included.
4. **Every client value is bound against the registry**, at every depth and under
   every verb.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, MutableMapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import (
    Address,
    FieldStep,
    KeyStep,
    MemberStep,
    PathStep,
    identity_key,
    member_identity_of,
    member_indices,
    member_matches,
)
from swcraft_tools.editing.protocol import (
    ChangeOperation,
    CreateOperation,
    RemoveOperation,
    UpdateOperation,
    Violation,
)
from swcraft_tools.editing.read import publish_element
from swcraft_tools.snapshot import ModelSnapshot

__all__ = [
    "IDENTITY_CONFLICT",
    "IMMUTABLE_FIELD",
    "SCHEMA_VIOLATION",
    "UNKNOWN_FIELD",
    "ApplyOutcome",
    "Inapplicable",
    "InapplicableOperation",
    "Refusal",
    "StagedModel",
    "TouchedElement",
    "apply_operations",
    "element_kind_of",
]

#: The four codes pass B can raise.
UNKNOWN_FIELD = "unknown-field"
IMMUTABLE_FIELD = "immutable-field"
IDENTITY_CONFLICT = "identity-conflict"
SCHEMA_VIOLATION = "schema-violation"


class Refusal(Exception):
    """One pass-B violation, raised out of the bind-and-apply of one operation.

    Raised so a refusal deep in a path need not be threaded back by hand.
    :func:`apply_operations` is the only handler: it records the violation and
    moves on to the next operation.
    """

    def __init__(self, error: str, message: str, address: Address) -> None:
        super().__init__(message)
        self.error = error
        self.address = address

    def as_violation(self) -> Violation:
        return Violation(error=self.error, message=str(self), addresses=(self.address,))


class Inapplicable(Exception):
    """The operation's address does not resolve *at this point in the order*.

    Deliberately **not** a :class:`Refusal`: whether this is
    ``partial-change-set`` or ``broken-reference`` depends on whether a *later*
    operation would have supplied the missing target, which is pass C's question.

    ``missing`` is the **shortest prefix of the address that is not there**: an
    ``update`` at ``{swc, Foo, [memory, {key: RAM}]}`` with no ``Foo`` misses at
    ``{swc, Foo}``, which a ``create`` of the element would supply. The client is
    told about the address it wrote.
    """

    def __init__(self, reason: str, message: str, address: Address, missing: Address) -> None:
        super().__init__(message)
        self.reason = reason
        self.address = address
        self.missing = missing


@dataclass(frozen=True)
class InapplicableOperation:
    """One operation pass B could not apply, kept in the order it was given."""

    position: int
    operation: ChangeOperation
    reason: str
    message: str
    missing: Address

    @property
    def address(self) -> Address:
        return self.operation.address


@dataclass(frozen=True)
class TouchedElement:
    """One element whose staged value **differs** from its baseline value.

    ``before is None`` is a created element and ``after is None`` a removed one;
    both present is a changed one. An element an operation named but did not
    move is not here.
    """

    layer: str
    identity: str
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None


# ---------------------------------------------------------------------------
# The staged tree
# ---------------------------------------------------------------------------


@dataclass
class StagedModel:
    """The model being edited, in memory, in ``read``'s published vocabulary.

    ``elements`` is layer name -> the layer's elements in reader order, each a
    plain JSON-shaped dict with its placement values merged in as the declared
    fields they are published as. ``baseline`` is the untouched copy the
    :meth:`touched` comparison is made against.
    """

    registry: reg.Registry
    elements: dict[str, list[dict[str, Any]]]
    baseline: dict[str, dict[str, Any]] = field(repr=False, default_factory=dict)

    @classmethod
    def from_snapshot(cls, snapshot: ModelSnapshot, *, registry: reg.Registry | None = None) -> StagedModel:
        """Every element of *snapshot* published into a staged, mutable tree.

        The layers and their placement tables are read off
        :attr:`~swcraft_tools.snapshot.ModelSnapshot.state` by **layer name**,
        since a layer with no ``snapshotAttributes`` row has no attribute. A
        layer the state does not carry stages as an empty layer.
        """
        registry = reg.or_current(registry)
        elements: dict[str, list[dict[str, Any]]] = {}
        baseline: dict[str, dict[str, Any]] = {}
        state = snapshot.state
        for layer in registry.layers:
            placement_table: Mapping[str, Mapping[str, str]] = state.placement_of(layer.name)
            published: list[dict[str, Any]] = []
            for element in state[layer.name] if layer.name in state else ():
                identity = getattr(element, layer.identity_field, None)
                values = placement_table.get(identity) if isinstance(identity, str) else None
                published.append(publish_element(element, registry, placement=dict(values) if values else None))
            elements[layer.name] = published
            baseline[layer.name] = {item[layer.identity_field]: deepcopy(item) for item in published}
        return cls(registry=registry, elements=elements, baseline=baseline)

    def layer_of(self, address: Address) -> reg.LayerDescriptor:
        """The layer an address names.

        Raises:
            Refusal: ``unknown-field``. ``$defs/layerName`` is a pattern, so an
                undeclared layer is a well-formed envelope and a semantic
                rejection.
        """
        try:
            return self.registry.layer(address.layer)
        except KeyError:
            raise Refusal(
                UNKNOWN_FIELD,
                f"no layer '{address.layer}' is declared; the model has {list(self.registry.layer_names)}",
                address,
            ) from None

    def index_of(self, layer: reg.LayerDescriptor, identity: str | None) -> int | None:
        """The position of one element in its layer, or ``None``."""
        for position, element in enumerate(self.elements[layer.name]):
            if element.get(layer.identity_field) == identity:
                return position
        return None

    def get(self, layer: reg.LayerDescriptor, identity: str | None) -> dict[str, Any] | None:
        position = self.index_of(layer, identity)
        return None if position is None else self.elements[layer.name][position]

    def touched(self) -> tuple[TouchedElement, ...]:
        """Every element whose staged value differs from its baseline value.

        Canonical comparison (:func:`~swcraft_tools.editing.address.identity_key`),
        not ``==``: ``1`` equals ``1.0``, and ``True`` does not equal ``1``.
        """
        result: list[TouchedElement] = []
        for layer in self.registry.layers:
            before_by_identity = self.baseline[layer.name]
            seen: set[str] = set()
            for element in self.elements[layer.name]:
                identity = element[layer.identity_field]
                seen.add(identity)
                before = before_by_identity.get(identity)
                if before is None:
                    result.append(TouchedElement(layer.name, identity, None, deepcopy(element)))
                elif identity_key(before) != identity_key(element):
                    result.append(TouchedElement(layer.name, identity, before, deepcopy(element)))
            for identity, before in before_by_identity.items():
                if identity not in seen:
                    result.append(TouchedElement(layer.name, identity, before, None))
        return tuple(result)


# ---------------------------------------------------------------------------
# The same-identity replacement ban
# ---------------------------------------------------------------------------


class _IdentityHistory:
    """What this change set has already created and removed, by address.

    Keyed by the whole address; :class:`~swcraft_tools.editing.address.MemberStep`
    hashes on the canonical identity, so the ban is about identity, not about JSON
    spelling (key order).
    """

    def __init__(self) -> None:
        self._created: set[tuple[str, str | None, tuple[PathStep, ...]]] = set()
        self._removed: set[tuple[str, str | None, tuple[PathStep, ...]]] = set()

    @staticmethod
    def _key(address: Address) -> tuple[str, str | None, tuple[PathStep, ...]]:
        return (address.layer, address.element, address.path)

    def check(self, operation: str, address: Address, *, exempt: bool) -> None:
        """The refusal of the second half of a same-identity replacement.

        Args:
            operation: ``create`` or ``remove``. ``update`` never participates:
                it is a change *of* a thing, not a replacement of one.
            address: the address the operation names.
            exempt: the address names a member of a ``whole_value`` **list**,
                whose pair is one occurrence out and one in, not a replacement.

        Raises:
            Refusal: ``identity-conflict``.
        """
        if exempt:
            return
        opposite = self._removed if operation == "create" else self._created
        if self._key(address) in opposite:
            other = "removed" if operation == "create" else "created"
            raise Refusal(
                IDENTITY_CONFLICT,
                f"this change set already {other} the same identity, and a remove plus a create at the SAME "
                "identity is banned globally: the semantic differ has no delta entry "
                "meaning 'the same thing, elsewhere or differently identified', so the pair would reach the "
                "review boundary as something other than what was asked for. A change OF identity is a remove "
                "and a create at DIFFERENT identities plus the reference updates",
                address,
            )

    def record(self, operation: str, address: Address, *, exempt: bool) -> None:
        if exempt:
            return
        target = self._created if operation == "create" else self._removed
        target.add(self._key(address))


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyOutcome:
    """What pass B produced: a staged tree, its violations and its misses."""

    staged: StagedModel
    violations: tuple[Violation, ...] = ()
    inapplicable: tuple[InapplicableOperation, ...] = ()
    touched: tuple[TouchedElement, ...] = ()


def apply_operations(
    snapshot: ModelSnapshot,
    operations: Sequence[ChangeOperation],
    *,
    registry: reg.Registry | None = None,
) -> ApplyOutcome:
    """The outcome of binding and applying *operations* against *snapshot*, in the order given.

    Args:
        snapshot: the baseline model; read, never mutated.
        operations: the change set's operations, already parsed by pass A.
        registry: the model description to bind against.

    Returns:
        An :class:`ApplyOutcome`. ``violations`` is in **operation order**: the
        first is the ``errorResult``'s primary, the rest its ``additional``.
    """
    registry = reg.or_current(registry)
    staged = StagedModel.from_snapshot(snapshot, registry=registry)
    history = _IdentityHistory()
    violations: list[Violation] = []
    inapplicable: list[InapplicableOperation] = []

    for position, operation in enumerate(operations):
        try:
            _apply_one(staged, operation, history, registry)
        except Refusal as refusal:
            violations.append(refusal.as_violation())
        except Inapplicable as miss:
            inapplicable.append(InapplicableOperation(position, operation, miss.reason, str(miss), miss.missing))

    return ApplyOutcome(
        staged=staged,
        violations=tuple(violations),
        inapplicable=tuple(inapplicable),
        touched=staged.touched(),
    )


# ---------------------------------------------------------------------------
# One operation
# ---------------------------------------------------------------------------


@dataclass
class _Cursor:
    """Where the walk stands: the value, and what the registry says it is."""

    value: Any
    kind: reg.ElementKindDescriptor | None = None
    collection: reg.CollectionDescriptor | None = None
    #: The list collection the value is a member of, after a member step: lets
    #: a field step tell an identity field from any other immutable field.
    member_of: reg.CollectionDescriptor | None = None


def _apply_one(
    staged: StagedModel,
    operation: ChangeOperation,
    history: _IdentityHistory,
    registry: reg.Registry,
) -> None:
    address = operation.address
    layer = staged.layer_of(address)
    if not address.path:
        _apply_to_element(staged, layer, operation, history, registry)
        return
    _apply_inside_element(staged, layer, operation, history, registry)


# --- element-level create and remove ---------------------------------------


def _apply_to_element(
    staged: StagedModel,
    layer: reg.LayerDescriptor,
    operation: ChangeOperation,
    history: _IdentityHistory,
    registry: reg.Registry,
) -> None:
    address = operation.address
    position = staged.index_of(layer, address.element)

    if isinstance(operation, CreateOperation):
        if position is not None:
            raise Refusal(
                IDENTITY_CONFLICT,
                f"'{address.element}' already exists in layer '{layer.name}'; a create never silently becomes "
                "an overwrite, and changing an existing element is an update",
                address,
            )
        history.check("create", address, exempt=False)
        element = _element_payload(layer, operation.value, address, registry)
        history.record("create", address, exempt=False)
        staged.elements[layer.name].append(element)
        return

    if isinstance(operation, RemoveOperation):
        if position is None:
            raise Inapplicable(
                "no_such_element",
                f"layer '{layer.name}' holds no element '{address.element}'",
                address,
                _element_address(address),
            )
        history.check("remove", address, exempt=False)
        history.record("remove", address, exempt=False)
        del staged.elements[layer.name][position]
        return

    # Pass A already refuses this; a stated refusal because this module is also
    # called directly.
    raise Refusal(
        SCHEMA_VIOLATION,
        "an update names a field, a nested member or a map entry inside an element, never the element itself",
        address,
    )


def _element_payload(
    layer: reg.LayerDescriptor,
    value: Any,
    address: Address,
    registry: reg.Registry,
) -> dict[str, Any]:
    """The complete declared payload of a created element, checked and validated."""
    if not isinstance(value, Mapping):
        raise Refusal(
            SCHEMA_VIOLATION,
            f"a create at a '{layer.name}' element address carries the complete declared payload as an object, "
            f"not {type(value).__name__}",
            address,
        )
    kind = _kind_for_payload(layer.top_level_kinds, _layer_discriminator(layer), value, address, layer.name)
    _bind_payload(kind, value, address, registry)

    declared = value.get(layer.identity_field)
    if declared != address.element:
        raise Refusal(
            SCHEMA_VIOLATION,
            f"the payload's {layer.identity_field} is {declared!r} while the address names "
            f"'{address.element}'; a create states one identity, in both places",
            address,
        )
    return _validated_element(kind, dict(value), address, registry)


# --- everything inside an element ------------------------------------------


def _apply_inside_element(
    staged: StagedModel,
    layer: reg.LayerDescriptor,
    operation: ChangeOperation,
    history: _IdentityHistory,
    registry: reg.Registry,
) -> None:
    address = operation.address
    position = staged.index_of(layer, address.element)
    if position is None:
        raise Inapplicable(
            "no_such_element",
            f"layer '{layer.name}' holds no element '{address.element}'",
            address,
            _element_address(address),
        )

    # Copied, mutated and validated before it is installed, so a refusal halfway
    # leaves the staged tree as it was.
    element = deepcopy(staged.elements[layer.name][position])
    kind = _kind_for_payload(layer.top_level_kinds, _layer_discriminator(layer), element, address, layer.name)
    cursor = _Cursor(value=element, kind=kind)
    for index, step in enumerate(address.path[:-1]):
        cursor = _descend(cursor, step, address, index, registry)

    last = address.path[-1]
    if isinstance(operation, UpdateOperation):
        _update_at(cursor, last, operation.value, address, registry)
    elif isinstance(operation, CreateOperation):
        _create_at(cursor, last, operation.value, address, history, registry)
    else:
        _remove_at(cursor, last, address, history)

    staged.elements[layer.name][position] = _validated_element(kind, element, address, registry)


def _descend(
    cursor: _Cursor,
    step: PathStep,
    address: Address,
    index: int,
    registry: reg.Registry,
) -> _Cursor:
    """One intermediate step of the path, or the reason it cannot be taken."""
    if isinstance(step, FieldStep):
        descriptor = _field_descriptor(cursor, step, address)
        value = cursor.value.get(descriptor.name) if isinstance(cursor.value, Mapping) else None
        return _Cursor(
            value=value,
            kind=_kind_after_field(descriptor, registry),
            collection=descriptor.collection,
        )

    if isinstance(step, MemberStep):
        collection = _list_collection(cursor, address, index)
        matches = member_indices(cursor.value, collection, step.member)
        if not matches:
            raise Inapplicable(
                "no_such_member",
                f"path step {index} names a collection member that is not there",
                address,
                _prefix_address(address, index),
            )
        member = cursor.value[matches[0]]
        return _Cursor(value=member, kind=_member_kind(collection, member, address, registry), member_of=collection)

    _require_map_collection(cursor, address, index)
    if not isinstance(cursor.value, Mapping) or step.key not in cursor.value:
        raise Inapplicable(
            "no_such_key",
            f"the map holds no key '{step.key}'",
            address,
            _prefix_address(address, index),
        )
    return _Cursor(value=cursor.value[step.key])


# --- the last step, per verb ----------------------------------------------


def _update_at(cursor: _Cursor, step: PathStep, value: Any, address: Address, registry: reg.Registry) -> None:
    """The replacement of the value at the last step, the replacement bound first.

    An ``update``'s replacement is a client payload like a ``create``'s, so it is
    bound too; which check applies is the registry's answer at the addressed
    position, never the verb.
    """
    if isinstance(step, FieldStep):
        descriptor = _field_descriptor(cursor, step, address)
        if descriptor.immutable:
            if cursor.member_of is not None and descriptor.name in _identity_fields(cursor.member_of):
                raise Refusal(
                    IMMUTABLE_FIELD,
                    f"'{descriptor.name}' is part of the member's identity: a changed value is that member removed "
                    "and one created at the new identity, in one change set",
                    address,
                )
            raise Refusal(
                IMMUTABLE_FIELD,
                f"'{descriptor.name}' is declared immutable, and an approved scope can never make an immutable "
                "field writable",
                address,
            )
        before = cursor.value.get(descriptor.name) if isinstance(cursor.value, Mapping) else None
        if descriptor.collection is not None:
            _bind_collection(descriptor.collection, value, address, registry, before=before)
        elif descriptor.element_kind is not None:
            _bind_payload(registry.element_kind(descriptor.element_kind), value, address, registry, before=before)
        cursor.value[descriptor.name] = deepcopy(value)
        return

    if isinstance(step, MemberStep):
        collection = _list_collection(cursor, address, len(address.path) - 1)
        matches = member_indices(cursor.value, collection, step.member)
        if not matches:
            raise Inapplicable("no_such_member", "the collection holds no such member", address, address)
        if collection.member_identity == "whole_value":
            raise Refusal(
                IMMUTABLE_FIELD,
                "a member of a `whole_value` collection IS its own identity, so it has no addressable interior "
                "and every field of it is immutable: a changed value is one member removed and one added",
                address,
            )
        missing = _missing_identity_fields(collection, value)
        if missing:
            # Name the field when the payload holds exactly one changeable field.
            only = next(iter(value)) if isinstance(value, Mapping) and len(value) == 1 else None
            field_step = (
                json.dumps(FieldStep(only).to_json(), ensure_ascii=False)
                if isinstance(only, str) and only not in _identity_fields(collection)
                else '{"field": <name>}'
            )
            raise Refusal(
                IMMUTABLE_FIELD,
                "an update at a member's address replaces the whole member, and this replacement does not carry "
                f"the member's identity ({', '.join(missing)}); to change one field, update it at its own address, "
                f"{_path_text(address, field_step)}; to replace the member, send it whole, its identity included",
                address,
            )
        if not member_matches(value, collection, step.member):
            raise Refusal(
                IMMUTABLE_FIELD,
                "a collection member's identity is immutable, and the replacement carries a different one; "
                "changing an identity is a remove and a create at the new identity, in one change set, at two "
                "different identities (a remove and a create at the same identity is identity-conflict)",
                address,
            )
        # The replaced member is the baseline for everything inside it, so the
        # membership rule reaches `[runnables, {member: R}, events]` too.
        _refuse_unknown_member_keys(collection, value, address, registry, before=cursor.value[matches[0]])
        cursor.value[matches[0]] = deepcopy(value)
        return

    _require_map_collection(cursor, address, len(address.path) - 1)
    if not isinstance(cursor.value, MutableMapping) or step.key not in cursor.value:
        # `update` only at a held key: an absent one is pass C's
        # `broken-reference`, never a silent create.
        raise Inapplicable("no_such_key", f"the map holds no key '{step.key}'", address, address)
    assert cursor.collection is not None
    _refuse_unknown_member_keys(cursor.collection, value, address, registry, before=cursor.value[step.key])
    cursor.value[step.key] = deepcopy(value)


def _identity_fields(collection: reg.CollectionDescriptor) -> tuple[str, ...]:
    """The fields that make up one member's identity: the one field, the composite tuple, or none."""
    if collection.member_identity == "field":
        return (collection.identity_field,) if collection.identity_field else ()
    if collection.member_identity == "composite":
        return collection.identity_fields
    return ()


def _missing_identity_fields(collection: reg.CollectionDescriptor, value: Any) -> tuple[str, ...]:
    """The required identity fields a member replacement leaves out.

    A declared-optional identity field (``optional_identity_fields``) left out
    reads as ``None``, as a member address that leaves it out does, so it is
    never missing.
    """
    carried = value if isinstance(value, Mapping) else {}
    return tuple(
        name
        for name in _identity_fields(collection)
        if name not in carried and name not in collection.optional_identity_fields
    )


def _path_text(address: Address, last: str) -> str:
    """*address*'s path in the protocol's own JSON step syntax, with one step appended as written."""
    steps = [json.dumps(step.to_json(), ensure_ascii=False) for step in address.path]
    return "[" + ", ".join([*steps, last]) + "]"


def _create_at(
    cursor: _Cursor,
    step: PathStep,
    value: Any,
    address: Address,
    history: _IdentityHistory,
    registry: reg.Registry,
) -> None:
    if isinstance(step, FieldStep):
        raise Refusal(
            SCHEMA_VIOLATION,
            "a create names an element, a nested collection member or a map entry; a "
            "declared field always exists, and giving it a value is an update",
            address,
        )

    if isinstance(step, MemberStep):
        collection = _list_collection(cursor, address, len(address.path) - 1)
        if not isinstance(cursor.value, list):
            raise Refusal(SCHEMA_VIOLATION, "the addressed collection is not a list", address)
        multiset = collection.member_identity == "whole_value"
        if not multiset and member_indices(cursor.value, collection, step.member):
            raise Refusal(
                IDENTITY_CONFLICT,
                "the collection already holds a member with that identity; a create never silently becomes an "
                "overwrite",
                address,
            )
        history.check("create", address, exempt=multiset)
        if not member_matches(value, collection, step.member):
            raise Refusal(
                SCHEMA_VIOLATION,
                "the member address and the payload denote different members; a create states one identity, in "
                "both places",
                address,
            )
        _refuse_unknown_member_keys(collection, value, address, registry)
        history.record("create", address, exempt=multiset)
        # Appended: an address has no position. Where order carries meaning, pass
        # D checks it over the staged result.
        cursor.value.append(deepcopy(value))
        return

    _require_map_collection(cursor, address, len(address.path) - 1)
    if not isinstance(cursor.value, MutableMapping):
        raise Refusal(SCHEMA_VIOLATION, "the addressed collection is not a map", address)
    if step.key in cursor.value:
        raise Refusal(
            IDENTITY_CONFLICT,
            f"the map already holds the key '{step.key}'; a create is for a key the baseline does NOT hold and "
            "an update for one it does",
            address,
        )
    history.check("create", address, exempt=False)
    assert cursor.collection is not None  # `_require_map_collection` established it
    _refuse_unknown_member_keys(cursor.collection, value, address, registry)
    history.record("create", address, exempt=False)
    cursor.value[step.key] = deepcopy(value)


def _remove_at(cursor: _Cursor, step: PathStep, address: Address, history: _IdentityHistory) -> None:
    if isinstance(step, FieldStep):
        raise Refusal(
            SCHEMA_VIOLATION,
            "a remove names an element, a nested collection member or a map entry; a "
            "declared field is cleared with an update",
            address,
        )

    if isinstance(step, MemberStep):
        collection = _list_collection(cursor, address, len(address.path) - 1)
        matches = member_indices(cursor.value, collection, step.member)
        if not matches:
            raise Inapplicable("no_such_member", "the collection holds no such member", address, address)
        multiset = collection.member_identity == "whole_value"
        history.check("remove", address, exempt=multiset)
        history.record("remove", address, exempt=multiset)
        # One occurrence, never all of them: a `whole_value` list is a multiset,
        # so clearing a value recorded twice takes two removes.
        del cursor.value[matches[0]]
        return

    _require_map_collection(cursor, address, len(address.path) - 1)
    if not isinstance(cursor.value, MutableMapping) or step.key not in cursor.value:
        raise Inapplicable("no_such_key", f"the map holds no key '{step.key}'", address, address)
    history.check("remove", address, exempt=False)
    history.record("remove", address, exempt=False)
    del cursor.value[step.key]


# ---------------------------------------------------------------------------
# The missing target of an inapplicable operation
# ---------------------------------------------------------------------------


def _element_address(address: Address) -> Address:
    """The element half of *address* — the target a miss on the element names."""
    return Address(layer=address.layer, element=address.element)


def _prefix_address(address: Address, index: int) -> Address:
    """*address* truncated after the step at *index* — the target that is absent.

    The step that missed is included: a ``create`` at exactly this address is
    what would supply it.
    """
    return Address(layer=address.layer, element=address.element, path=address.path[: index + 1])


# ---------------------------------------------------------------------------
# Registry lookups, each with the code its failure carries
# ---------------------------------------------------------------------------


def _field_descriptor(cursor: _Cursor, step: FieldStep, address: Address) -> reg.FieldDescriptor:
    if cursor.kind is None:
        raise Refusal(
            UNKNOWN_FIELD,
            f"nothing at this point of the address declares a field '{step.field}'",
            address,
        )
    descriptor = cursor.kind.get_field(step.field)
    if descriptor is None:
        raise Refusal(
            UNKNOWN_FIELD,
            f"element kind '{cursor.kind.kind}' declares no field '{step.field}'; it declares "
            f"{list(cursor.kind.field_names)}",
            address,
        )
    return descriptor


def _kind_after_field(descriptor: reg.FieldDescriptor, registry: reg.Registry) -> reg.ElementKindDescriptor | None:
    """The element kind the cursor stands on after a field step, if any.

    A collection field leaves the cursor on the *collection*, which is not a
    kind; a single object-typed field (``connector.from``) leaves it on the
    nested kind; anything else on a scalar.
    """
    if descriptor.collection is not None or descriptor.element_kind is None:
        return None
    return registry.element_kind(descriptor.element_kind)


def _member_kind(
    collection: reg.CollectionDescriptor,
    member: Any,
    address: Address,
    registry: reg.Registry,
) -> reg.ElementKindDescriptor | None:
    if not collection.member_kinds:
        return None
    kinds = tuple(registry.element_kind(name) for name in collection.member_kinds)
    return _kind_for_payload(kinds, collection.discriminator_field, member, address, "the collection member")


def _list_collection(cursor: _Cursor, address: Address, index: int) -> reg.CollectionDescriptor:
    if cursor.collection is None or cursor.collection.kind != "list":
        raise Refusal(
            SCHEMA_VIOLATION,
            f"path step {index} is a member step, which needs a list collection above it; a map entry is "
            "addressed by its key, because a key is unique by construction",
            address,
        )
    return cursor.collection


def _require_map_collection(cursor: _Cursor, address: Address, index: int) -> None:
    if cursor.collection is None or cursor.collection.kind != "map":
        raise Refusal(
            SCHEMA_VIOLATION,
            f"path step {index} is a key step, which needs a map collection above it",
            address,
        )


def _layer_discriminator(layer: reg.LayerDescriptor) -> str | None:
    for kind in layer.top_level_kinds:
        if kind.discriminator_field:
            return kind.discriminator_field
    return None


def element_kind_of(
    layer: reg.LayerDescriptor,
    element: Mapping[str, Any],
    *,
    registry: reg.Registry | None = None,
) -> reg.ElementKindDescriptor:
    """Which of *layer*'s top-level kinds a published element is.

    Raises:
        Refusal: the element names no declared kind (unreachable for an accepted
            or published element).
    """
    registry = reg.or_current(registry)
    return _kind_for_payload(
        layer.top_level_kinds,
        _layer_discriminator(layer),
        element,
        Address(layer=layer.name),
        layer.name,
    )


def _kind_for_payload(
    kinds: Sequence[reg.ElementKindDescriptor],
    discriminator: str | None,
    payload: Any,
    address: Address,
    what: str,
) -> reg.ElementKindDescriptor:
    """Which of a layer's (or a collection's) kinds a payload is.

    Matched against the discriminator field's declared ``enum`` (its ``Literal``
    members), **not** the kind name: the two coincide for layers but not for a
    collection (``Runnable.events``' kind ``timing_event`` has the literal
    ``timing``).
    """
    if len(kinds) == 1:
        return kinds[0]
    if not kinds:  # pragma: no cover - every layer declares at least one kind
        raise Refusal(SCHEMA_VIOLATION, f"{what} declares no element kind", address)
    if discriminator is None:  # pragma: no cover - a multi-kind union always has one
        raise Refusal(SCHEMA_VIOLATION, f"{what} has several kinds and no discriminator", address)
    admitted = {kind.kind: _discriminator_values(kind, discriminator) for kind in kinds}
    if not isinstance(payload, Mapping) or discriminator not in payload:
        raise Refusal(
            SCHEMA_VIOLATION,
            f"{what} is discriminated by '{discriminator}', which the payload does not carry; its members are "
            f"{sorted(value for values in admitted.values() for value in values)}",
            address,
        )
    declared = payload[discriminator]
    for kind in kinds:
        if declared in admitted[kind.kind]:
            return kind
    raise Refusal(
        SCHEMA_VIOLATION,
        f"'{declared}' is not one of {what}'s declared {discriminator} values "
        f"{sorted(value for values in admitted.values() for value in values)}",
        address,
    )


def _discriminator_values(kind: reg.ElementKindDescriptor, discriminator: str) -> tuple[Any, ...]:
    """The ``Literal`` members the registry derived for one kind's discriminator."""
    descriptor = kind.get_field(discriminator)
    return descriptor.enum if descriptor is not None and descriptor.enum else (kind.kind,)


def _refuse_unknown_keys(
    kind: reg.ElementKindDescriptor,
    payload: Mapping[str, Any],
    address: Address,
    registry: reg.Registry,
) -> None:
    """A payload key the registry does not declare is ``unknown-field``.

    Pydantic ignores an extra key, so the client would be told a dropped field
    succeeded. Reported at the field's own address, which the client has to fix.
    """
    declared = set(registry.all_fields(kind.kind))
    unknown = sorted(set(payload) - declared)
    if unknown:
        raise Refusal(
            UNKNOWN_FIELD,
            f"element kind '{kind.kind}' declares no field {unknown[0]!r}; it declares {sorted(declared)}",
            Address(layer=address.layer, element=address.element, path=(*address.path, FieldStep(unknown[0]))),
        )


#: "No baseline at this position, so no membership rule applies": what a
#: ``create``'s payload and an added member carry. Distinct from ``None``, a
#: baseline holding nothing, where filling the collection IS a membership change.
_UNTRACKED: Any = object()


def _refuse_unknown_member_keys(
    collection: reg.CollectionDescriptor,
    value: Any,
    address: Address,
    registry: reg.Registry,
    *,
    before: Any = _UNTRACKED,
) -> None:
    """The same check for one collection member — created, or replacing one — whose kind is known.

    *before* is the member this value replaces, or :data:`_UNTRACKED` for a
    created one.
    """
    if not collection.member_kinds or not isinstance(value, Mapping):
        return
    kind = _member_kind(collection, value, address, registry)
    if kind is not None:
        _bind_payload(kind, value, address, registry, before=before)


def _bind_payload(
    kind: reg.ElementKindDescriptor,
    payload: Any,
    at: Address,
    registry: reg.Registry,
    *,
    before: Any = _UNTRACKED,
) -> None:
    """The binding of one object-shaped payload against *kind*, all the way down.

    Every verb's payload goes through here: a payload position left unbound is a
    key the client wrote, the engine dropped, and the response called a success.
    *at* is the payload's own address, so a refusal names the field where the
    client wrote it. The recursion follows the payload and needs no visited set.

    *before* is the value this payload replaces, or :data:`_UNTRACKED` (a
    ``create``). It travels down beside the payload so
    :func:`_refuse_membership_change` applies at **every** depth of a replacement.
    A non-object payload is left to :func:`_validated_element`.
    """
    if not isinstance(payload, Mapping):
        return
    _refuse_unknown_keys(kind, payload, at, registry)
    for descriptor in kind.fields:
        field_at = Address(layer=at.layer, element=at.element, path=(*at.path, FieldStep(descriptor.name)))
        field_before = _field_before(before, descriptor.name)
        if descriptor.name not in payload:
            _bind_omitted_collection(kind, descriptor, field_at, registry, before=field_before)
            continue
        value = payload[descriptor.name]
        if descriptor.collection is not None:
            _bind_collection(descriptor.collection, value, field_at, registry, before=field_before)
        elif descriptor.element_kind is not None:
            _bind_payload(
                registry.element_kind(descriptor.element_kind), value, field_at, registry, before=field_before
            )


def _bind_omitted_collection(
    kind: reg.ElementKindDescriptor,
    descriptor: reg.FieldDescriptor,
    field_at: Address,
    registry: reg.Registry,
    *,
    before: Any,
) -> None:
    """The membership rule held over a collection the replacement simply **omits**.

    Re-validation installs an omitted field's **declared default** (``[]`` for
    ``Runnable.events``), so omitting it is held like writing that default.
    """
    if descriptor.collection is None or before is _UNTRACKED:
        return
    info = kind.model.model_fields.get(descriptor.name)
    if info is None or info.is_required():
        return
    installed = _as_published(info.get_default(call_default_factory=True))
    _bind_collection(descriptor.collection, installed, field_at, registry, before=before)


def _as_published(value: Any) -> Any:
    """A declared default in the vocabulary the staged tree speaks.

    A default may hold models, while the membership rule compares JSON-shaped
    values.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _as_published(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_published(item) for item in value]
    return value


def _field_before(before: Any, name: str) -> Any:
    """The baseline value of one field of *before*, or :data:`_UNTRACKED`.

    An untracked parent stays untracked; a tracked non-object parent tracks its
    fields as ``None``, as for an absent key.
    """
    if before is _UNTRACKED:
        return _UNTRACKED
    return before.get(name) if isinstance(before, Mapping) else None


def _bind_collection(
    collection: reg.CollectionDescriptor,
    value: Any,
    at: Address,
    registry: reg.Registry,
    *,
    before: Any = _UNTRACKED,
) -> None:
    """The binding of every member of a whole collection payload, its membership held.

    *at* is the collection field's address. A replacement that is both malformed
    and a membership change is reported as malformed.
    """
    _bind_collection_members(collection, value, at, registry, before)
    if before is not _UNTRACKED:
        _refuse_membership_change(collection, before, value, at)


def _bind_collection_members(
    collection: reg.CollectionDescriptor,
    value: Any,
    at: Address,
    registry: reg.Registry,
    before: Any,
) -> None:
    """The binding of each member of a collection payload against the member it replaces."""
    if not collection.member_kinds:
        return
    if collection.kind == "list":
        if not isinstance(value, list):
            return
        paired = _members_by_identity(before, collection)
        for member in value:
            if not isinstance(member, Mapping):
                continue
            identity = member_identity_of(member, collection)
            member_at = at if identity is None else _step_below(at, MemberStep(identity))
            kind = _member_kind(collection, member, member_at, registry)
            if kind is not None:
                _bind_payload(kind, member, member_at, registry, before=paired.get(identity_key(identity), _UNTRACKED))
        return
    if not isinstance(value, Mapping):
        return
    entries = before if isinstance(before, Mapping) else {}
    for key, member in value.items():
        if not isinstance(member, Mapping):
            continue
        entry_at = _step_below(at, KeyStep(key)) if isinstance(key, str) else at
        kind = _member_kind(collection, member, entry_at, registry)
        if kind is not None:
            _bind_payload(kind, member, entry_at, registry, before=entries.get(key, _UNTRACKED))


def _members_by_identity(before: Any, collection: reg.CollectionDescriptor) -> dict[Any, Any]:
    """The baseline members of a list collection, keyed by canonical identity.

    Empty without a baseline: every member is then :data:`_UNTRACKED`, and the
    membership rule reports the addition.
    """
    if not isinstance(before, list):
        return {}
    paired: dict[Any, Any] = {}
    for member in before:
        paired.setdefault(identity_key(member_identity_of(member, collection)), member)
    return paired


def _step_below(at: Address, step: PathStep) -> Address:
    return Address(layer=at.layer, element=at.element, path=(*at.path, step))


def _refuse_membership_change(
    collection: reg.CollectionDescriptor,
    before: Any,
    after: Any,
    address: Address,
) -> None:
    """An ``update`` of a whole collection preserves its member identities.

    The whole-collection ``update`` is the reordering primitive (an address has
    no position), and a member's identity is immutable with no rename path, so
    only a reorder and per-member content changes are allowed. Otherwise
    it would be a second, unchecked membership channel:

    * identities both **gone and arrived** (rewritten in place): ``immutable-field``;
    * only **arrived** (added) or only **gone** (dropped): ``schema-violation``
      naming the ``create``/``remove`` at the member's own address, where the
      ``identity-conflict``, multiset and same-identity checks live.

    **A list is compared as a multiset, a map as a set of keys.** Not
    ``Counter(mapping)``: that compares the values, refusing ``{"RAM": 2048} ->
    {"RAM": 4096}`` and, since ``Counter`` drops non-positive counts, missing a
    rename between two zero-valued keys. A non-list/non-map replacement is left
    to :func:`_validated_element`.
    """
    added: Any
    removed: Any
    if collection.kind == "list":
        if not isinstance(after, list):
            return
        before_members = before if isinstance(before, list) else []
        gone = Counter(identity_key(member_identity_of(member, collection)) for member in before_members)
        arrived = Counter(identity_key(member_identity_of(member, collection)) for member in after)
        added = arrived - gone
        removed = gone - arrived
    else:
        if not isinstance(after, Mapping):
            return
        before_keys = set(before) if isinstance(before, Mapping) else set()
        after_keys = set(after)
        added = after_keys - before_keys
        removed = before_keys - after_keys
    if not added and not removed:
        return
    what = "key" if collection.kind == "map" else "member"
    if added and removed:
        raise Refusal(
            IMMUTABLE_FIELD,
            f"a collection {what}'s identity is immutable and this replacement rewrites one "
            "in place: renaming a member has no protocol path at all, and a change OF identity is a remove and a "
            "create at DIFFERENT identities plus the reference updates",
            address,
        )
    verb, direction = ("create", "adding") if added else ("remove", "dropping")
    raise Refusal(
        SCHEMA_VIOLATION,
        f"an update of a whole collection is the REORDERING primitive, and this replacement "
        f"changes which {what}s are there; {direction} one is a {verb} at that {what}'s own address, which is "
        "where the identity-conflict, multiset and same-identity checks are made",
        address,
    )


def _validated_element(
    kind: reg.ElementKindDescriptor,
    element: Mapping[str, Any],
    address: Address,
    registry: reg.Registry,
) -> dict[str, Any]:
    """A whole element re-validated through its own Pydantic class, and re-published.

    The only source of ``schema-violation``. Placement values are checked and then
    stripped; the **validated** value is re-published (``"1536"`` becomes ``1536``).
    """
    _check_path_values(kind, element, address, registry)
    names = _placement_names(kind)
    payload = {name: value for name, value in element.items() if name not in names}
    try:
        validated = kind.model.model_validate(payload)
    except ValidationError as error:
        raise Refusal(SCHEMA_VIOLATION, _first_error(error, kind), address) from error
    placement = {name: element[name] for name in names}
    published = publish_element(validated, registry, placement=placement or None)
    assert isinstance(published, dict)  # a declared element kind always publishes an object
    return published


def _check_path_values(
    kind: reg.ElementKindDescriptor,
    element: Mapping[str, Any],
    address: Address,
    registry: reg.Registry,
) -> None:
    """The identity and every placement value are present and are legal path segments.

    The two client values that reach a path component, refused by the
    registry's own composition guards at bind time, each at its own address, as
    a ``schema-violation`` rather than an uncaught ``PathSegmentError`` during
    materialization. ``$defs/identity`` does not refuse an embedded ``..``
    (``a..b``); the guard is stricter. A placement field is always required, so
    an omitted one is refused here too.

    The identity asks :meth:`~swcraft_tools.editing.registry.Registry.identity_refusal`,
    not the bare segment question, because the layer's ``file_suffix`` is
    appended afterwards: ``A.`` composes ``A..yaml``, which the writer refuses.
    """
    if address.element is not None:
        refused = registry.identity_refusal(address.layer, address.element)
        if refused is not None:
            # No path: the identity is not a field, it is what the element IS.
            raise Refusal(SCHEMA_VIOLATION, refused, Address(layer=address.layer, element=address.element))
    for descriptor in kind.fields:
        if not descriptor.placement:
            continue
        at = Address(layer=address.layer, element=address.element, path=(FieldStep(descriptor.name),))
        if descriptor.name not in element:
            raise Refusal(
                SCHEMA_VIOLATION,
                f"'{descriptor.name}' is placement metadata and is required: an element of layer "
                f"'{address.layer}' states where it lives, and the protocol has no relocation primitive that "
                "could supply it later",
                at,
            )
        refusal = reg.segment_refusal(element[descriptor.name], f"{address.layer} {descriptor.name}", directory=True)
        if refusal is not None:
            raise Refusal(SCHEMA_VIOLATION, refusal, at)


def _placement_names(kind: reg.ElementKindDescriptor) -> frozenset[str]:
    return frozenset(descriptor.name for descriptor in kind.fields if descriptor.placement)


def _first_error(error: ValidationError, kind: reg.ElementKindDescriptor) -> str:
    """One readable sentence out of a Pydantic error. The text is not contract."""
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or kind.kind
    return f"{kind.kind}.{location}: {first.get('msg', 'is not valid')}"
