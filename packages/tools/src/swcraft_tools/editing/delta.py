"""The ``ModelDiff`` -> ``deltaEntry[]`` projection — step 4 of the lifecycle.

The projection is a contract: authorization coverage is **address-prefix
based**, so the projection takes the **finest granularity the address form can
express**.

======================================  ===========================================
``ModelDiff`` shape                     projected as
======================================  ===========================================
an added / removed element              one ``create`` / ``remove`` at
                                        ``{layer, element}``, carrying it whole
a changed scalar field                  one ``update`` at the field's address
a changed object field, same key set    descended into, key by key
a list member added / removed           one ``create`` / ``remove`` at the member
a changed list member                   **none of its own** — recurse into it
a map key added / removed / changed     ``create`` / ``remove`` / ``update`` at it
a reordering                            one ``update`` at the collection's field
======================================  ===========================================

**The entry order is deterministic but is not contract**: layers in registry
order; within a layer removals, additions, then changed elements; within a
changed thing the differ's field order; within a collection removals, additions,
then changed members.

``summary`` is prose, not contract: mechanical, bounded, never parsed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel

from swcraft_tools.differ import ChangedItem, CollectionChange, LayerDiff, ModelDiff
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, canonical_value, collection_at, reduce_whole_value, values_equal
from swcraft_tools.editing.apply import element_kind_of
from swcraft_tools.editing.protocol import ABSENT, DeltaEntry
from swcraft_tools.editing.read import placement_of, publish_element, summarize_published, summary_field_names
from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.snapshot import ModelSnapshot

__all__ = ["elided_fields", "project_delta", "summarize_removed_elements", "summarize_response"]

#: How long a value may be in a ``summary`` before it is elided. The summary is
#: a sentence, not a payload — the payload is ``before``/``after``.
_SUMMARY_VALUE_LIMIT = 60


def project_delta(
    diff: ModelDiff,
    *,
    baseline: ModelSnapshot,
    staged: ModelSnapshot,
    registry: reg.Registry | None = None,
) -> list[DeltaEntry]:
    """*diff* projected onto the flat ``deltaEntry`` list the protocol reviews.

    Args:
        baseline: read only for a **removed** element's placement metadata.
        staged: read only for an **added** element's placement metadata.

    Returns:
        One entry per changed leaf, in the module's deterministic order. Empty
        for an empty diff, which is a no-op change set and not an error.
    """
    registry = reg.or_current(registry)
    entries: list[DeltaEntry] = []
    # By layer name, not a snapshot attribute: a layer with no
    # `snapshotAttributes` row reaches a consumer through `by_layer` or not at all.
    by_layer = diff.by_layer
    for layer in registry.layers:
        if layer.name in by_layer:
            entries.extend(_layer_entries(layer, by_layer[layer.name], baseline, staged, registry))
    return entries


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------


def _layer_entries(
    layer: reg.LayerDescriptor,
    layer_diff: LayerDiff,
    baseline: ModelSnapshot,
    staged: ModelSnapshot,
    registry: reg.Registry,
) -> list[DeltaEntry]:
    """One layer: its removals, then its additions, then what changed inside.

    An added or removed element is carried **whole**, with its placement metadata
    merged in as ``read`` publishes it, read from the side the element exists on.
    ``group`` is immutable, so it only ever appears in an addition or a removal.
    """
    entries: list[DeltaEntry] = []
    for element in layer_diff.removed:
        identity = _identity(element, layer)
        entries.append(
            _entry(
                "remove",
                _address(layer.name, identity, ()),
                before=publish_element(element, registry, placement=placement_of(baseline, layer, identity)),
            )
        )
    for element in layer_diff.added:
        identity = _identity(element, layer)
        entries.append(
            _entry(
                "create",
                _address(layer.name, identity, ()),
                after=publish_element(element, registry, placement=placement_of(staged, layer, identity)),
            )
        )
    for item in layer_diff.changed:
        entries.extend(_item_entries(item, layer.name, str(item.key), registry))
    return entries


def _identity(element: Any, layer: reg.LayerDescriptor) -> str:
    """One element's identity — ``name`` for nine layers, ``id`` for requirements."""
    return str(getattr(element, layer.identity_field))


# ---------------------------------------------------------------------------
# What changed inside one element
# ---------------------------------------------------------------------------


def _item_entries(item: ChangedItem, layer: str, element: str, registry: reg.Registry) -> list[DeltaEntry]:
    """Everything that changed inside one :class:`~swcraft_tools.differ.ChangedItem`.

    The whole-value tuple in ``changed_fields`` is the walk, and the
    :class:`~swcraft_tools.differ.CollectionChange` beside it is the detail: a
    field named by both is projected from the detail, never twice (as in
    ``format_diff``): the two forms are one comparison recorded twice.
    """
    collections = {change.field: change for change in item.collection_changes}
    entries: list[DeltaEntry] = []
    for name, old, new in item.changed_fields:
        change = collections.get(name)
        if change is not None:
            entries.extend(_collection_entries(change, layer, element, old, new, registry))
        else:
            entries.extend(_field_entries(layer, element, (*item.path, {"field": name}), old, new))
    return entries


def _field_entries(layer: str, element: str, path: tuple[Any, ...], old: Any, new: Any) -> list[DeltaEntry]:
    """One changed non-collection field, at the finest address that separates the two values.

    Two plain objects with the same key set are descended into, so retargeting a
    connector endpoint is one ``update`` at ``{field: from}, {field: swc}``. A
    deeper address has more prefixes, so every scope entry that covered the field
    still covers the leaf. Anything else — a scalar, a differing key set (a
    discriminated union that changed kind), a value absent from one side — is one
    ``update`` at the field's own address.
    """
    if isinstance(old, Mapping) and isinstance(new, Mapping) and set(old) == set(new):
        nested = [
            entry
            for key in old
            if not values_equal(old[key], new[key])
            for entry in _field_entries(layer, element, (*path, {"field": key}), old[key], new[key])
        ]
        if nested:
            return nested
    return [_entry("update", _address(layer, element, path), before=_wire(old), after=_wire(new))]


def _collection_entries(
    change: CollectionChange, layer: str, element: str, old: Any, new: Any, registry: reg.Registry
) -> list[DeltaEntry]:
    """One changed collection field — a list or a map, with the two verbs each takes.

    *old* and *new* are the collection's own whole values, which the map shapes
    need: ``CollectionChange.added``/``removed`` hold a map's **keys** (its
    dumped items are the names it is keyed by), and a delta entry carries the
    entry's *value*.
    """
    if change.rule.kind == "mapping":
        return _map_entries(change, layer, element, old, new)
    return _list_entries(change, layer, element, old, new, registry)


def _list_entries(
    change: CollectionChange, layer: str, element: str, old: Any, new: Any, registry: reg.Registry
) -> list[DeltaEntry]:
    """A list collection: one entry per member added or removed, per occurrence.

    A *changed* member contributes nothing of its own; the recursion into its
    :class:`~swcraft_tools.differ.ChangedItem` reports the leaves inside it. A
    changed ``whole_value`` member is a ``remove`` plus a ``create``, never an
    ``update``, counted as a multiset: the differ keys it by value and pairs
    ``(key, occurrence)``, and such a member has no addressable interior.

    A reordering is reported **whenever the differ signals one**, first,
    as the statement about the whole value. Suppressed beside an addition, the
    reviewer of "one argument added" would also approve a permutation of the
    emitted ``CLIENT-SERVER-OPERATION`` arguments or of a ``Sequence``'s steps.
    """
    entries = (
        [_entry("update", _address(layer, element, change.path), before=_wire(old), after=_wire(new))]
        if change.reordered
        else []
    )
    entries.extend(
        _entry(
            "remove",
            _address(layer, element, (*change.path, _member_step(change, member, layer, registry))),
            before=_wire(member),
        )
        for member in change.removed
    )
    entries.extend(
        _entry(
            "create",
            _address(layer, element, (*change.path, _member_step(change, member, layer, registry))),
            after=_wire(member),
        )
        for member in change.added
    )
    for item in change.changed:
        entries.extend(_item_entries(item, layer, element, registry))
    return entries


def _map_entries(change: CollectionChange, layer: str, element: str, old: Any, new: Any) -> list[DeltaEntry]:
    """A map collection: an added key is a ``create``, a removed one a ``remove``, a changed one an ``update``.

    A map entry is addressed by ``{key: ...}``, never ``{member: ...}``: a key is
    unique, so a ``create`` at a held key is an ``identity-conflict`` where a
    second occurrence of a list member is not.
    """
    entries = [
        _entry("remove", _address(layer, element, (*change.path, {"key": key})), before=_wire(_at(old, key)))
        for key in change.removed
    ]
    entries.extend(
        _entry("create", _address(layer, element, (*change.path, {"key": key})), after=_wire(_at(new, key)))
        for key in change.added
    )
    # A map's changed item is the entry itself: `_diff_mapping` records the two
    # values under one pseudo-field whose address is the entry's own, so the
    # entry is an `update` at `{key}` and never a walk into the value.
    entries.extend(
        _entry("update", _address(layer, element, item.path), before=_wire(before), after=_wire(after))
        for item in change.changed
        for _name, before, after in item.changed_fields
    )
    return entries


def _member_step(change: CollectionChange, member: Any, layer: str, registry: reg.Registry) -> dict[str, Any]:
    """``{member: ...}`` for one dumped list member, in the identity form the registry publishes.

    A ``whole_value`` member's identity is reduced
    (:func:`~swcraft_tools.editing.address.reduce_whole_value`), so the step a
    delta publishes is the step a client spells and a scope approves. The entry's
    ``before``/``after`` keep the full value.
    """
    identity = canonical_value(change.rule.member_identity(member))
    return {"member": reduce_whole_value(identity, collection_at(layer, change.path, registry=registry))}


def _at(collection: Any, key: str) -> Any:
    """One map entry's value, or ``ABSENT`` when the collection is not a map after all."""
    return collection[key] if isinstance(collection, Mapping) and key in collection else ABSENT


# ---------------------------------------------------------------------------
# Wire values and addresses
# ---------------------------------------------------------------------------

#: Everything ``$defs/value`` admits. Anything else (the differ's sentinel for a
#: field absent from one side of a ``kind``-changed dump) is left out rather than
#: sent as ``null``: a missing ``before`` and a ``null`` one differ.
_WIRE_TYPES = (str, bool, int, float, Mapping, Sequence, BaseModel, type(None))


def _wire(value: Any) -> Any:
    """One model value as it goes on the wire, or ``ABSENT`` when it is not one."""
    if value is ABSENT or not isinstance(value, _WIRE_TYPES):
        return ABSENT
    return canonical_value(value)


def _address(layer: str, element: str, path: Sequence[Any]) -> Address:
    """The address of one changed leaf, parsed from the differ's own step shape.

    Parsing the differ's ``$defs/pathStep`` JSON keeps one step vocabulary, and a
    step the address model refuses (an ordinal one) cannot reach a delta entry.
    """
    data: dict[str, Any] = {"layer": layer, "element": element}
    if path:
        # Omitted rather than empty: `$defs/address` gives `path` `minItems: 1`.
        data["path"] = [dict(step) for step in path]
    return Address.from_json(data)


def _entry(operation: str, address: Address, *, before: Any = ABSENT, after: Any = ABSENT) -> DeltaEntry:
    """One delta entry, with the mechanical summary that goes beside it."""
    return DeltaEntry(
        operation=operation,
        address=address,
        before=before,
        after=after,
        summary=_summary(operation, address, before, after),
    )


#: The verb each operation reads as. ``format_diff``'s own three, so a delta
#: summary and a rendered diff of the same change do not disagree in sign.
_VERBS = {"create": "+", "remove": "-", "update": "~"}


def _summary(operation: str, address: Address, before: Any, after: Any) -> str:
    """A bounded, mechanical one-line description. Prose: never parsed, never contract."""
    subject = f"{address.layer}/{address.element}" if address.path else address.layer
    detail = _render_path(address)
    values = ""
    if operation == "update" and before is not ABSENT and after is not ABSENT:
        values = f" {_render_value(before)} -> {_render_value(after)}"
    return f"{subject}: {_VERBS[operation]} {detail or address.element}{values}"


def _render_path(address: Address) -> str:
    """The path as a reader sees it: ``runnables[ExampleRun].min_start_interval``."""
    rendered = ""
    for step in address.path:
        json_step = step.to_json()
        if "field" in json_step:
            rendered = f"{rendered}.{json_step['field']}" if rendered else json_step["field"]
        else:
            rendered = f"{rendered}[{_render_value(next(iter(json_step.values())))}]"
    return rendered


def _render_value(value: Any) -> str:
    """One value in a summary, elided past :data:`_SUMMARY_VALUE_LIMIT` characters."""
    if isinstance(value, str):
        rendered = value
    elif isinstance(value, Mapping):
        rendered = "{" + ", ".join(f"{key}: {_render_value(item)}" for key, item in value.items()) + "}"
    elif isinstance(value, (list, tuple)):
        rendered = "[" + ", ".join(_render_value(item) for item in value) + "]"
    else:
        # Not `repr`: a hand-authored `init: 0x<4000 f>` loads (YAML's `int(v, 16)`
        # is exempt from `sys.get_int_max_str_digits()`) and raises inside its own
        # `repr`. The delta's `before`/`after` values still raise in
        # `protocol.py`'s JSON, deliberately.
        rendered = render_model_scalar(value)
    return rendered if len(rendered) <= _SUMMARY_VALUE_LIMIT else f"{rendered[: _SUMMARY_VALUE_LIMIT - 1]}…"


# ---------------------------------------------------------------------------
# The elision at the edge
# ---------------------------------------------------------------------------
#
# The engine gates, classifies and commits the delta whole. What follows applies
# only where the delta LEAVES the engine (the MCP `change_set` response and its
# review text). A removed element's `before` is the one half that grows with the
# model rather than the change, and the one half `read` can still answer before
# the commit, so at the edge it is shown in its `summary` form, flagged
# `before_form: "summary"`. Inside the engine it never is: the safety guard reads
# a removed element's members from `before`.


def summarize_removed_elements(delta: Sequence[DeltaEntry], registry: reg.Registry | None = None) -> list[DeltaEntry]:
    """A copy of *delta* with each whole-element ``remove``'s ``before`` in its ``summary`` form.

    The summary is the one ``read`` answers for ``form: "summary"``, and the entry
    gains ``before_form: "summary"``. Every other entry is returned as it is, and a
    removal whose summary would equal the whole value stays whole. *delta* is not
    mutated.
    """
    registry = reg.or_current(registry)
    return [_summarised(entry, registry) for entry in delta]


def summarize_response(response: Mapping[str, Any], registry: reg.Registry | None = None) -> dict[str, Any]:
    """*response* as it leaves the server: a ``change_set`` response's delta summarised, anything else as it is.

    The JSON form of :func:`summarize_removed_elements`, for the server's dispatch
    site. Returns a copy; any other response comes back equal to what was passed.
    """
    registry = reg.or_current(registry)
    if response.get("response") != "change_set" or not isinstance(response.get("delta"), list):
        return dict(response)
    entries = [DeltaEntry.from_json(entry) for entry in response["delta"]]
    return {**response, "delta": [entry.to_json() for entry in summarize_removed_elements(entries, registry=registry)]}


def elided_fields(entry: DeltaEntry, registry: reg.Registry | None = None) -> tuple[dict[str, int], int]:
    """What a summarised ``before`` dropped: each counted collection with its count, and the other fields left out.

    For the review text, which must say what it shortened, with the count. The
    first half maps each collection field the summary carries to its member count
    (kind's field order, empty collections included); the second counts the
    kind's fields outside :func:`~swcraft_tools.editing.read.summary_field_names`.
    A summary field omitted because it was unset dropped nothing and is not
    counted. ``({}, 0)`` for an entry that is not summarised.
    """
    registry = reg.or_current(registry)
    if entry.before_form is None or not isinstance(entry.before, Mapping):
        return {}, 0
    kind = _removed_kind(entry, registry)
    counted = {
        field.name: entry.before[field.name]
        for field in kind.fields
        if field.collection is not None and isinstance(entry.before.get(field.name), int)
    }
    shown = frozenset(summary_field_names(kind, registry))
    hidden = sum(1 for field in kind.fields if field.name not in shown)
    return counted, hidden


def _summarised(entry: DeltaEntry, registry: reg.Registry) -> DeltaEntry:
    """One entry, its ``before`` summarised where it is a whole element's and the summary narrows."""
    if (
        entry.operation != "remove"
        or entry.address.path
        # A summary's collections are counts; summarising again would hand `len` an int.
        or entry.before_form is not None
        or not isinstance(entry.before, Mapping)
    ):
        return entry
    kind = _removed_kind(entry, registry)
    summary = summarize_published(entry.before, kind, registry)
    if summary == entry.before:
        return entry
    return replace(entry, before=summary, before_form="summary")


def _removed_kind(entry: DeltaEntry, registry: reg.Registry) -> reg.ElementKindDescriptor:
    """The declared kind of the element a ``remove`` at an element address took away."""
    return element_kind_of(registry.layer(entry.address.layer), entry.before, registry=registry)
