"""The layer slice: one declared layer's elements or members, filtered by clauses and paged.

``slice_layer`` checks each clause against the layer's declared fields, reads the matching elements or
the rows of a declared member collection, and pages them by a cursor the slice carries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from pydantic import BaseModel

from swcraft_tools.differ import collection_rule
from swcraft_tools.model_registry import (
    CollectionFacts,
    FieldFacts,
    KindFacts,
    LayerFacts,
    ModelRegistry,
    complete_composite_identity,
    current_registry,
)
from swcraft_tools.model_state import ModelState
from swcraft_tools.query.edges import check_limit, read
from swcraft_tools.query.projection import full_element, full_value
from swcraft_tools.query.request import (
    ABSENT,
    DEFAULT_QUERY_FORM,
    MISSING,
    QUERY_FORMS,
    Clause,
    QueryError,
    QueryForm,
    QueryPage,
    Slice,
    canonical,
    holds_no_value,
    literal,
    ordered_elements,
    values_equal,
)


@dataclass(frozen=True)
class _Spelling:
    """How a refusal names the key it is about: as the flag spells it, or as a ``--request`` file does."""

    members: str
    where: str
    after: str
    filter_hint: str
    slice_hint: str


_FLAGS = _Spelling(
    members="--members",
    where="--where/--exists",
    after="--after",
    filter_hint="--where {field}=VALUE",
    slice_hint="--members {field}",
)


REQUEST_SPELLING = _Spelling(
    members="`members`",
    where="`where`",
    after="`after`",
    filter_hint='a `where` clause {{"field": "{field}", ...}}',
    slice_hint='"members": "{field}"',
)


def _check_clauses(
    where: Sequence[Clause], kinds: Sequence[KindFacts], named: str, spelling: _Spelling = _FLAGS
) -> None:
    """Refuse every clause the declarations cannot answer, before any element is tested.

    Refused: a field no kind here declares, and a field declared as a
    collection. A kind that does not declare the field is not a refusal: a
    clause only some kinds satisfy is a selection.

    *named* is the noun phrase the refusal reads out, e.g. "top-level element
    kind of layer 'swc'"; *top-level* because member-only kinds (``runnable``
    under ``swc``) are declared under a layer too and a layer filter
    cannot reach their fields.
    """
    for clause in where:
        declared = [kind.get_field(clause.field) for kind in kinds]
        present = [facts for facts in declared if facts is not None]
        if not present:
            raise QueryError(
                f"{spelling.where} '{clause.field}': not a declared field of any {named}; "
                "`swcraft query` filters by the fields the model contract declares"
            )
        if any(facts.collection is not None for facts in present):
            raise QueryError(
                f"{spelling.where} '{clause.field}': declares a collection and a clause tests one value; "
                f"slice it instead ({spelling.slice_hint.format(field=clause.field)}) and filter the members there"
            )


def _clause_value(dumped: Mapping[str, Any], name: str, placement: Mapping[str, str] | None) -> Any:
    """The value *name* has on one dumped element or member, or :data:`MISSING`.

    Read out of the JSON dump, so the name is the contract's published one
    (``connector.from``, never ``from_``) and a declared field holding ``None``
    is present-and-null, not missing. A ``placement`` field (``swc.group``) lives
    beside the element, in the layer's placement table.
    """
    if name in dumped:
        return dumped[name]
    if placement is not None and name in placement:
        return placement[name]
    return MISSING


def _row_matches(dumped: Mapping[str, Any], where: Sequence[Clause], placement: Mapping[str, str] | None) -> bool:
    """Whether one dumped element or member satisfies every clause (AND)."""
    return all(clause_holds(_clause_value(dumped, clause.field, placement), clause) for clause in where)


def clause_holds(value: Any, clause: Clause) -> bool:
    """Whether *clause* holds for *value*, the one a row carries at its field (:data:`MISSING` where none).

    ``equals`` canonically (:func:`values_equal`); ``exists`` true when there is
    a value (``null`` and ``""`` are none, :func:`holds_no_value`).
    """
    if clause.equals is ABSENT:
        return ((value is not MISSING) and not holds_no_value(value)) == clause.exists
    return value is not MISSING and values_equal(value, clause.equals)


def _collection_shape(facts: CollectionFacts) -> tuple[Any, ...]:
    """What makes two declarations of one collection name the same collection.

    The editing registry's ``CollectionDescriptor`` equality; the declaring kind
    and layer are not part of the shape.
    """
    return (
        facts.collection_kind,
        facts.member_identity,
        facts.member_kinds,
        facts.discriminator_field,
        facts.identity_field,
        facts.identity_fields,
    )


def sliced_path(
    layer: LayerFacts, members: str, registry: ModelRegistry, spelling: _Spelling = _FLAGS
) -> tuple[FieldFacts, ...]:
    """The declared collection each step of ``--members`` names, or the refusal it earns.

    The first step resolves against the layer's **top-level** kinds, each later
    step against the member kinds the step before chose; the **first** declaring
    kind supplies the collection. Refused: a step no kind declares; a step that
    is not a collection; a **map** anywhere but the last step (its entries are
    reached by a key).

    Not reached: a step where two kinds declare one name with different shapes
    or as a non-collection (:func:`_member_rows` skips those owners, e.g. the
    external interface stub's name lists); rows are per component **type**,
    never per placement.
    """
    steps = members.split(".")
    kinds: tuple[KindFacts, ...] = tuple(layer.top_level_kinds)
    chosen: list[FieldFacts] = []
    for position, step in enumerate(steps):
        present = [facts for facts in (kind.get_field(step) for kind in kinds) if facts is not None]
        reached = ".".join(steps[:position])
        if not present:
            if position == 0:
                raise QueryError(
                    f"{spelling.members} '{members}': not a declared field of any top-level element kind "
                    f"of layer '{layer.name}'"
                )
            raise QueryError(
                f"{spelling.members} '{members}': '{step}' is not a declared field of any member kind the members "
                f"'{reached}' reaches in layer '{layer.name}'"
            )
        collections = [facts for facts in present if facts.collection is not None]
        if not collections:
            if position == 0:
                raise QueryError(
                    f"{spelling.members} '{members}': declares no collection — it is one value per element. "
                    f"Filter the layer by it instead ({spelling.filter_hint.format(field=members)})"
                )
            raise QueryError(
                f"{spelling.members} '{members}': '{step}' declares no collection — it is one value per member of "
                f"'{reached}', and every step of a member path names a collection"
            )
        chosen_field = collections[0]
        collection = chosen_field.collection
        assert collection is not None  # narrowed by the filter above
        if collection.collection_kind == "map" and position < len(steps) - 1:
            raise QueryError(
                f"{spelling.members} '{members}': '{step}' is a map collection, and a map may only be the last step "
                "of a member path: its entries are reached by a key, and a step below one has no member to "
                "stand on"
            )
        chosen.append(chosen_field)
        kinds = tuple(registry.kind(name) for name in collection.member_kinds)
    return tuple(chosen)


def _member_identity(rule: Any, member: Any) -> Any:
    """One dumped member's identity, under its collection's declared strategy.

    ``None`` for a member that does not carry the declared identity, the
    fail-closed direction: it matches only another that has none.
    """
    if rule is None:  # pragma: no cover - every declared collection has a rule
        return canonical(member)
    try:
        return canonical(rule.member_identity(member))
    except (KeyError, TypeError, IndexError):
        return None


class _Held(NamedTuple):
    """What one row holds: its JSON dump (what the clauses read), its live value and its placement values.

    The live value is what ``full`` publishes; only a layer's element carries placement values.
    """

    dump: Any
    live: Any
    placement: Mapping[str, str] | None = None


def _member_rows(
    elements: Sequence[Any],
    layer: LayerFacts,
    path: Sequence[FieldFacts],
    registry: ModelRegistry,
) -> list[tuple[Any, _Held]]:
    """``(row identity, held member)`` for every member *path* reaches across the layer.

    The row identity is ``{"element", "member"}`` for a list collection and
    ``{"element", "key"}`` for a map one; it carries the owner because a member
    identity alone is not unique across a layer. A dotted path's row also
    carries ``through`` after ``element``, the identity of each intermediate
    member in path order; a one-step path carries no ``through`` key.

    Each owner is asked about its **own** kind's declaration first, so an owner
    declaring the name as a scalar or as a collection of another shape yields
    no rows.
    """
    rows: list[tuple[Any, _Held]] = []
    for element in elements:
        dumped = element.model_dump(mode="json", by_alias=True)
        identity = dumped.get(layer.identity_field)
        rows.extend(_rows_below(element, dumped, identity, (), path, registry))
    return rows


def _rows_below(
    owner: Any,
    dumped: Mapping[str, Any],
    element: Any,
    through: tuple[Any, ...],
    path: Sequence[FieldFacts],
    registry: ModelRegistry,
) -> list[tuple[Any, _Held]]:
    """The rows *path* reaches below *owner* (the live model), read from *dumped*, its JSON dump."""
    step = path[0]
    collection = step.collection
    assert collection is not None  # `sliced_path` chose a collection at every step
    own = registry.kind_of_model(type(owner))
    declares = own.get_field(step.name) if own is not None else None
    if (
        declares is None
        or declares.collection is None
        or _collection_shape(declares.collection) != _collection_shape(collection)
    ):
        # Not a refusal: a step holds several kinds and only some of them answer.
        return []
    value = dumped.get(step.name)
    if value is None:
        return []
    live = read(owner, step.name)
    if isinstance(value, Mapping):
        # A map is the last step: `sliced_path` refused one anywhere else.
        return [
            (_row(element, through, "key", key), _Held(entry, own))
            for (key, entry), own in zip(value.items(), live.values(), strict=True)
        ]
    rule = collection_rule(registry.bound_class(collection.kind), collection.field, registry=registry)
    rows: list[tuple[Any, _Held]] = []
    for member, member_dump in zip(live, value, strict=True):
        identity = _whole_value_identity(_member_identity(rule, member_dump), member, collection, registry)
        if len(path) == 1:
            rows.append((_row(element, through, "member", identity), _Held(member_dump, member)))
        elif isinstance(member, BaseModel) and isinstance(member_dump, Mapping):
            rows.extend(_rows_below(member, member_dump, element, (*through, identity), path[1:], registry))
    return rows


def _whole_value_identity(identity: Any, member: Any, collection: CollectionFacts, registry: ModelRegistry) -> Any:
    """A ``whole_value`` member's identity without the fields at their declared default.

    ``editing.address.reduce_whole_value``'s rule: removes the optional fields
    whose value equals the contract's declared ``default`` in the member's own
    kind, so a field added with a default changes no identity a client spelled.
    A non-model member (a ``satisfies`` string) is its identity unchanged.
    """
    if collection.member_identity != "whole_value" or not isinstance(identity, Mapping):
        return identity
    own = registry.kind_of_model(type(member))
    if own is None:
        return identity
    defaults = {facts.name: facts.default for facts in own.fields if facts.has_default and not facts.required}
    return {key: item for key, item in identity.items() if not (key in defaults and values_equal(item, defaults[key]))}


def _row(element: Any, through: tuple[Any, ...], name: str, value: Any) -> dict[str, Any]:
    """One row identity, its keys in the protocol's order: element, through, then the member or key."""
    row: dict[str, Any] = {"element": element}
    if through:
        row["through"] = list(through)
    row[name] = value
    return row


def _placement_as_read(field: FieldFacts, _kind: KindFacts, placement: Mapping[str, str] | None) -> Any:
    """A placement field's value at ``full`` on this surface: the table's value as read, else ``None``."""
    return None if placement is None else placement.get(field.name)


def _element_value(held: _Held, registry: ModelRegistry) -> Any:
    """The ``full`` value of one layer element: the protocol's ``publish_element`` projection, whole."""
    return full_element(held.live, registry, placement=held.placement, placement_value=_placement_as_read)


def _member_row_value(identity: Mapping[str, Any], held: _Held, registry: ModelRegistry) -> dict[str, Any]:
    """The ``full`` value of one slice row: its identity minus the member's identity, then ``member`` whole.

    ``{element, [through], member}`` or ``{element, [through], key, member}``, the protocol's key order.
    """
    row = {key: item for key, item in identity.items() if key != "member"}
    row["member"] = full_value(held.live, registry, _placement_as_read)
    return row


def _completed_cursor(after: Any, collection: CollectionFacts | None, registry: ModelRegistry) -> Any:
    """*after* with a composite member identity it abbreviates spelled out in full.

    An identity field every member kind declares optional with the default
    ``null`` reads as ``null`` when left out, so
    ``{"element": "T", "member": {"swc": "A", "runnable": "Run"}}`` continues the
    page its three-key row identity does. Anything else is returned as written,
    and the cursor lookup refuses it.
    """
    if collection is None or collection.member_identity != "composite" or not isinstance(after, Mapping):
        return after
    member = after.get("member")
    optional = registry.optional_identity_fields(collection.identity_fields, collection.member_kinds)
    completed = complete_composite_identity(member, collection.identity_fields, optional)
    return after if completed is member else {**after, "member": completed}


def _cursor_position(units: Sequence[tuple[Any, Any]], after: Any, spelling: _Spelling = _FLAGS) -> int:
    """Where *after* sits in the selected set, or the refusal it earns.

    A **keyset**, never an offset: the model is re-read on every run, and an
    offset into a shorter set would skip an element silently. A cursor whose
    element is gone is refused rather than answered with a different window.
    """
    for position, (identity, _unit) in enumerate(units):
        if values_equal(identity, after):
            return position
    raise QueryError(_cursor_refusal(after, spelling))


def _cursor_refusal(after: Any, spelling: _Spelling) -> str:
    """The sentence a cursor this slice does not carry is refused with -- one for both cursors."""
    return (
        f"{spelling.after} {literal(after)}: no element or member of this slice carries that identity, so there "
        f"is no page to continue past it; {spelling.after} names an identity this slice answered, and one removed "
        "since is refused rather than answered with a different window"
    )


def row_cursor_refusal(rendered: str) -> str:
    """The sentence a rows answer's cursor is refused with: the cursor is a whole row, sent back verbatim.

    Shared by both surfaces; each hands in the cursor *rendered* as it writes a value.
    """
    return (
        f"no row of this answer is {rendered}, so there is no page to continue past it; "
        f"{REQUEST_SPELLING.after} is a row this request answered, sent back verbatim, and one the model no "
        "longer yields is refused rather than answered with a different window"
    )


def slice_layer(
    state: ModelState,
    layer: str,
    *,
    where: Sequence[Clause] = (),
    members: str | None = None,
    limit: int | None = None,
    after: Any = ABSENT,
    registry: ModelRegistry | None = None,
    spelling: _Spelling = _FLAGS,
    form: QueryForm = DEFAULT_QUERY_FORM,
) -> Slice:
    """One layer's identities, filtered, optionally sliced by a collection, and windowed.

    ``members`` chooses **which set**, ``where`` **which** of it, and ``limit``/``after`` **how many**, in that order.

    Args:
        state: the model to read.
        layer: the layer's contract name.
        where: AND-composed clauses over the declared fields at this address (of *members* when given).
        members: a declared collection name or dotted path of them (``runnables.writes``); ``None`` for the elements.
        limit: a page size of at least 1, or ``None`` (the default) for the whole set.
        after: the identity to continue **past**; :data:`ABSENT` (the default) starts at the beginning.
        registry: the kernel registry; ``None`` (the default) resolves the current one.
        spelling: how a refusal names its key: as the flag spells it (the default) or as a request file does.
        form: ``index`` (the default) or ``full`` (each row's value as well, in :attr:`Slice.values`).

    Returns:
        A :class:`Slice`. An empty result is an **answer**, not a refusal.

    Raises:
        KeyError: *layer* is not a declared layer name.
        QueryError: a clause names a field no kind declares, or a collection.
        QueryError: a step of *members* names no declared collection, or a step but the last names a map.
        QueryError: *after* is given without *limit*, or names an identity this slice does not carry.
        ValueError: *limit* is not a whole number (``bool`` and ``float`` refused) or is below 1.
        ValueError: *form* is not one of :data:`~swcraft_tools.query.request.QUERY_FORMS`.
    """
    registry = registry or current_registry()
    facts = registry.layer(layer)
    if form not in QUERY_FORMS:
        raise ValueError(f"form is one of {list(QUERY_FORMS)}, not {form!r}")
    if limit is not None:
        check_limit(limit)
    if after is not ABSENT and limit is None:
        raise QueryError("`after` continues a page, so it is given with a `limit`; a page is a bounded window")

    elements = ordered_elements(state.layers.get(facts.name, []), facts, registry)
    # Each unit is `(identity, held)`: the dump the clauses read, and what `full` publishes.
    scalar_identity = None
    if members is None:
        _check_clauses(where, facts.top_level_kinds, f"top-level element kind of layer '{facts.name}'", spelling)
        placement = state.placement_of(facts.name)
        units: list[tuple[Any, _Held]] = []
        for element in elements:
            dumped = element.model_dump(mode="json", by_alias=True)
            identity = dumped.get(facts.identity_field)
            values = placement.get(identity) if isinstance(identity, str) else None
            if _row_matches(dumped, where, values):
                units.append((identity, _Held(dumped, element, values)))
    else:
        path = sliced_path(facts, members, registry, spelling)
        collection = path[-1].collection
        assert collection is not None  # `sliced_path` chose a collection at every step
        after = _completed_cursor(after, collection, registry)
        member_kinds = tuple(registry.kind(name) for name in collection.member_kinds)
        _check_clauses(where, member_kinds, f"member kind of '{facts.name}.{members}'", spelling)
        scalar_identity = collection.identity_field if collection.member_identity == "field" else None
        units = [
            (identity, held)
            for identity, held in _member_rows(elements, facts, path, registry)
            if _row_matches(held.dump if isinstance(held.dump, Mapping) else {}, where, None)
        ]

    total = len(units)
    if limit is None:
        window = units
        page = None
    else:
        start = 0 if after is ABSENT else _cursor_position(units, after, spelling) + 1
        window = units[start : start + limit]
        follows = start + limit < total
        page = QueryPage(
            total=total,
            returned=len(window),
            truncated=len(window) < total,
            next=window[-1][0] if follows and window else ABSENT,
        )

    full_values: tuple[Any, ...] | None = None
    if form != DEFAULT_QUERY_FORM:
        full_values = tuple(
            _element_value(held, registry) if members is None else _member_row_value(identity, held, registry)
            for identity, held in window
        )
    return Slice(
        layer=facts.name,
        identity_field=facts.identity_field,
        identities=tuple(identity for identity, _unit in window),
        where=tuple(where),
        members=members,
        page=page,
        values=full_values,
        member_identity_field=scalar_identity,
    )
