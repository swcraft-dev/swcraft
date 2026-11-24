"""The answer's rows: derived values, collapse, paging, computed rows, row shapes and joins.

These functions take the rows a slice produced and shape them into the rows a request asked for:
they compute ``derived`` values, collapse and page the rows, describe their shape, and join a second
layer's rows onto them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeVar

from swcraft_tools.model_state import ModelState
from swcraft_tools.query.edges import check_limit
from swcraft_tools.query.request import ABSENT, MISSING, Clause, QueryError, QueryPage, comparison_key, values_equal
from swcraft_tools.query.slicing import clause_holds

# Identity rows: a request carrying ``derived`` or ``join`` answers the ``index`` identities wrapped as
# ``{"element": E, ...}``, with computed values under ``"derived"`` and join matches under their aliases.
# Both surfaces call this code; the computed-value vocabulary and its function are handed in, so this
# module reads no field and knows no computed-value name.


@dataclass(frozen=True)
class DerivedRefusal:
    """Why a request's ``derived`` cannot be answered where it reads.

    ``kind`` is ``"unknown"`` for a name outside the vocabulary (the protocol's ``unknown-field``) and
    ``"not-applicable"`` for a name at a set its entry's ``at`` does not name (``schema-violation``);
    ``message`` names the refused name. The CLI answers both with exit 1.
    """

    kind: Literal["unknown", "not-applicable"]
    message: str


def _set_text(layer: Any, paths: Sequence[str] | None) -> str:
    """A set a computed value applies at, or a request reads, as a refusal names it."""
    if paths is None:
        return f"the elements of layer '{layer}'"
    return f"the members {', '.join(repr(path) for path in paths)} of layer '{layer}'"


def derived_refusal(
    vocabulary: Sequence[Any], names: Sequence[str], layer: str, members: str | None
) -> DerivedRefusal | None:
    """The first of *names* the request cannot be answered with, or ``None``.

    *vocabulary* entries carry ``name`` and ``at``. A name applies at the elements of ``at["layer"]`` when
    ``at`` names no ``members``, and otherwise at a member path ``at["members"]`` lists, spelled exactly as the
    request spells it. Checked after the member path and the ``where`` clauses, so a misspelt collection is
    reported first.
    """
    entries = {entry.name: entry for entry in vocabulary}
    for name in names:
        entry = entries.get(name)
        if entry is None:
            return DerivedRefusal(
                "unknown",
                f"`derived` names {name!r}, which is no computed value this server publishes; the vocabulary is "
                f"{sorted(entries)}, published by `discover` as `reads.derived`",
            )
        paths = entry.at.get("members")
        applies = entry.at.get("layer") == layer and (members is None if paths is None else members in paths)
        if not applies:
            reads = _set_text(layer, None if members is None else [members])
            return DerivedRefusal(
                "not-applicable",
                f"`derived` names {name!r}, which is computed at {_set_text(entry.at.get('layer'), paths)}, and this "
                f"request reads {reads}",
            )
    return None


class _NamesAField(Protocol):
    """A clause as :func:`split_where` reads it: anything with a ``field`` name."""

    @property
    def field(self) -> str: ...


_C = TypeVar("_C", bound=_NamesAField)


#: The prefix of a ``where`` field naming a computed value of the row (``derived.NAME.PATH``); no declared
#: field is named ``derived``, so the prefix has one reading.
DERIVED_CLAUSE_PREFIX = "derived."


def is_derived_clause(field_name: str) -> bool:
    """Whether a clause's *field_name* names a computed value (:data:`DERIVED_CLAUSE_PREFIX`)."""
    return field_name.startswith(DERIVED_CLAUSE_PREFIX)


def split_where(where: Sequence[_C]) -> tuple[tuple[_C, ...], tuple[_C, ...]]:
    """*where* as ``(declared, derived)``: the clauses the filter tests, then the ones the row kernel does.

    Generic over the clause class; reads only ``field``.
    """
    declared = tuple(clause for clause in where if not is_derived_clause(clause.field))
    derived = tuple(clause for clause in where if is_derived_clause(clause.field))
    return declared, derived


#: The ``rows`` value of a set-valued computed value, spelled here because this package imports no owner
#: of a computed value.
SET_VALUED_ROWS = "one per value"


#: The declared shape of a row that computes nothing: its element alone.
BARE_ROW_SHAPE: Mapping[str, Any] = MappingProxyType({"element": None})


def derived_where_refusal(
    clauses: Sequence[Clause], derived: Sequence[str], shape: Mapping[str, Any], vocabulary: Sequence[Any] = ()
) -> tuple[str, str] | None:
    """The first derived clause of *clauses* the row cannot answer, as ``(field, message)``, or ``None``.

    Three refusals, all decided from the declarations before any row is computed:

    - a clause naming a computed value the request does not ask for in *derived*;
    - a clause whose path names no leaf of the declared row *shape* (:func:`row_shape`);
    - a clause asking for **no value** (``exists: false`` or ``equals: null``) of a computed value *vocabulary*
      declares set-valued (``rows`` is :data:`SET_VALUED_ROWS`), at any leaf. A set-valued value with no value
      yields no row, so such a clause would answer a confident 0; the rows without the value are the layer's
      index minus the rows the value yields.

    A single-valued name yields one row per input row, so absence over it keeps its meaning and is not refused.
    All three are answered as an undeclared filter field: ``unknown-field`` on the protocol, exit 1 on the CLI.
    What this does not reach: an ``exists: false`` over a leaf a single-valued value always carries answers 0
    rows, correctly, and is not refused.
    """
    set_valued = {entry.name for entry in vocabulary if entry.rows == SET_VALUED_ROWS}
    for clause in clauses:
        name = clause.field
        parts = name.split(".")
        computed = parts[1] if len(parts) > 1 else ""
        if computed not in derived:
            asked = f"names {list(derived)}" if derived else "is absent"
            return name, (
                f"`where` '{name}': a where over a derived value asks for it in `derived`, and this request's "
                f"`derived` {asked}"
            )
        if not _names_a_leaf(shape, name):
            values = [path for path in _leaf_paths(shape) if is_derived_clause(path)]
            return name, (
                f"`where` '{name}' names no value of the row: a where over a derived value names one value of a "
                f"computed value the request asks for, and this row's are {values}"
            )
        asks_absence = clause.exists is False if clause.equals is ABSENT else clause.equals is None
        if asks_absence and computed in set_valued:
            spelled = "`exists: false`" if clause.equals is ABSENT else "`equals: null`"
            return name, (
                f"`where` '{name}' asks {spelled} of '{computed}', a set-valued computed value: a computed value "
                f"with no value yields no row, so the clause never selects a row without one; 'none' is the layer's "
                f"index minus these rows — ask the rows '{computed}' yields and take them from the index"
            )
    return None


def _row_value(row: Mapping[str, Any], path: str) -> Any:
    """The value at *path* of *row*, or :data:`MISSING` where the row carries no such key."""
    value: Any = row
    for step in path.split("."):
        if not isinstance(value, Mapping) or step not in value:
            return MISSING
        value = value[step]
    return value


def rows_matching(rows: Sequence[Mapping[str, Any]], where: Sequence[Clause]) -> list[Mapping[str, Any]]:
    """The rows of *rows* every clause of *where* holds for, read at a row path."""
    return [row for row in rows if all(clause_holds(_row_value(row, clause.field), clause) for clause in where)]


def identity_rows(identities: Sequence[Any], *, sliced: bool) -> list[dict[str, Any]]:
    """The ``index`` identities as rows: a layer element wrapped as ``{"element": E}``.

    A member slice's identity is a row already and is copied, so every row has an ``element``.
    """
    if not sliced:
        return [{"element": identity} for identity in identities]
    return [dict(identity) for identity in identities]


def collapse_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """*rows* with every row identical to an earlier one dropped, first occurrence kept.

    Identity is the whole row under :func:`comparison_key`, so each row is unique and is its own page cursor.
    """
    seen: set[Any] = set()
    collapsed: list[Mapping[str, Any]] = []
    for row in rows:
        key = comparison_key(row)
        if key in seen:
            continue
        seen.add(key)
        collapsed.append(row)
    return collapsed


class RowCursorNotFound(LookupError):
    """An ``after`` naming no row of a rows answer; each surface answers it in its own words."""

    def __init__(self, after: Any) -> None:
        super().__init__("no row of this answer is the cursor")
        self.after = after


def page_rows(
    rows: Sequence[Mapping[str, Any]], limit: int | None, after: Any = ABSENT
) -> tuple[list[Mapping[str, Any]], QueryPage | None]:
    """One page of *rows* and its page block, the cursor being the **whole row**.

    *after* is compared by :func:`values_equal` against each whole row, ``derived`` included: it is the
    ``next`` a previous page carried, replayed verbatim.

    Raises:
        RowCursorNotFound: *after* is no row of *rows*.
    """
    if limit is None:
        return list(rows), None
    check_limit(limit)
    start = 0
    if after is not ABSENT:
        position = next((index for index, row in enumerate(rows) if values_equal(row, after)), None)
        if position is None:
            raise RowCursorNotFound(after)
        start = position + 1
    window = list(rows[start : start + limit])
    follows = start + limit < len(rows)
    return window, QueryPage(
        total=len(rows),
        returned=len(window),
        truncated=len(window) < len(rows),
        next=window[-1] if follows and window else ABSENT,
    )


#: The function that computes a request's ``derived`` values over rows; handed in by each caller.
Derive = Callable[[Sequence[Mapping[str, Any]], Sequence[str], ModelState], list[dict[str, Any]]]


def computed_rows(
    rows: Sequence[Mapping[str, Any]],
    names: Sequence[str],
    *,
    derive: Derive,
    state: ModelState,
    where: Sequence[Clause] = (),
) -> list[Mapping[str, Any]]:
    """*rows* carrying the computed values *names* and kept by *where*, or *rows* unchanged when nothing is asked.

    A set-valued name multiplies a row; the derived clauses are tested on each row it yields. The caller has
    checked every clause (:func:`derived_where_refusal`).
    """
    return rows_matching(derive(rows, names, state) if names else list(rows), where)


def answer_rows(
    rows: Sequence[Mapping[str, Any]],
    derived: Sequence[str],
    *,
    derive: Derive,
    state: ModelState,
    joins: Sequence[RowJoin] = (),
    limit: int | None = None,
    after: Any = ABSENT,
    bound: JoinBound | None = None,
    where: Sequence[Clause] = (),
) -> tuple[list[Mapping[str, Any]], QueryPage | None]:
    """The enhanced-row branch after the filter: compute, filter, collapse, join, page — in that order.

    *rows* are :func:`identity_rows` of the selected set; *where* holds the request's derived clauses, already
    checked, so the page and a ``count`` see the filtered set. Every join path is checked by the caller
    (:func:`join_shape`) before this is called. Collapsing once, before the join, leaves the product
    collapsed: an alias is none of a row's own keys and :func:`join_rows` collapses each joined read, so the
    count *bound* sees is the count of rows the answer carries.

    What it does not reach: every page computes the whole join again before windowing it. *bound* limits the
    combined rows only and does not reach the joined reads, which (at most :data:`JOIN_MAX_ENTRIES`) arrive
    computed in full; a row's width is the caller's to weigh. Without a *bound* (``swcraft query --request``)
    the product is unbounded.

    Raises:
        RowCursorNotFound: *after* is no row of the computed set.
        Exception: whatever *bound* raises, before the entry it refuses builds a row.
    """
    outer = collapse_rows(computed_rows(rows, derived, derive=derive, state=state, where=where))
    if joins:
        outer = join_rows(outer, joins, bound=bound)
    return page_rows(outer, limit, after)


# The join: inner, on equal values at row paths checked against each row's declared shape. It branches on no
# kind and reads no field.


@dataclass(frozen=True)
class RowStep:
    """One step of a member path, as the declared row shape reads it.

    ``container`` is the collection kind (``"list"`` or ``"map"``), ``identity`` its member identity rule
    (``"field"``, ``"composite"`` or ``"whole_value"``), and ``fields`` a ``composite`` rule's identity fields.
    """

    container: str
    identity: str
    fields: tuple[str, ...] = ()


def _identity_shape(step: RowStep) -> dict[str, None] | None:
    """A member identity's shape: one leaf, or an object of leaves for a ``composite`` rule."""
    if step.identity == "composite":
        return dict.fromkeys(step.fields)
    return None


def _shape_of_value(shape: Any) -> Any:
    """A vocabulary entry's declared ``shape`` with its ``"string"`` leaves read as leaves."""
    if isinstance(shape, Mapping):
        return {str(key): _shape_of_value(item) for key, item in shape.items()}
    return None


def row_shape(steps: Sequence[RowStep], derived: Sequence[str], vocabulary: Sequence[Any]) -> dict[str, Any]:
    """The declared shape of one identity row: the paths a join may name in it.

    A nested mapping whose leaves are ``None``: ``element`` always; ``through`` with one key per intermediate
    step (``"0"``, ``"1"``, …) for a path of more than one step; ``key`` for a map's last step, ``member``
    otherwise; and ``derived`` with each name's declared ``shape`` off *vocabulary*. A ``composite`` identity
    is an object of its identity fields. *steps* is empty for a layer read, and *derived* must have passed
    :func:`derived_refusal`.
    """
    shape: dict[str, Any] = dict(BARE_ROW_SHAPE)
    if len(steps) > 1:
        shape["through"] = {str(position): _identity_shape(step) for position, step in enumerate(steps[:-1])}
    if steps:
        last = steps[-1]
        if last.container == "map":
            shape["key"] = None
        else:
            shape["member"] = _identity_shape(last)
    if derived:
        entries = {entry.name: entry for entry in vocabulary}
        shape["derived"] = {name: _shape_of_value(entries[name].shape) for name in derived}
    return shape


def _leaf_paths(shape: Mapping[str, Any], prefix: str = "") -> list[str]:
    """Every path that ends at a leaf of *shape*, in the shape's own order."""
    paths: list[str] = []
    for key, item in shape.items():
        path = f"{prefix}{key}"
        if isinstance(item, Mapping):
            paths.extend(_leaf_paths(item, f"{path}."))
        else:
            paths.append(path)
    return paths


def _names_a_leaf(shape: Mapping[str, Any], path: str) -> bool:
    """Whether *path* walks *shape* key by key and ends at a leaf."""
    node: Any = shape
    for step in path.split("."):
        if not isinstance(node, Mapping) or step not in node:
            return False
        node = node[step]
    return not isinstance(node, Mapping)


class JoinPathOutside(QueryError):
    """A join path that names no leaf of the declared row it reads.

    Raised before any row is computed, so a bad path is refused on an empty model as on a full one. The CLI
    answers it with exit 1, the protocol as ``schema-violation``. *alias*, *side* and *path* say which path.
    """

    def __init__(self, alias: str, side: str, path: str, message: str) -> None:
        super().__init__(message)
        self.alias = alias
        self.side = side
        self.path = path


def join_refusal_prefix(alias: str) -> str:
    """``join 'AS': ``, the prefix of every refusal about one join entry, on both surfaces."""
    return f"join '{alias}': "


def join_shape(
    left: Mapping[str, Any], on: Sequence[tuple[str, str]], alias: str, right: Mapping[str, Any]
) -> dict[str, Any]:
    """The row shape after one join entry, or the refusal its paths earn.

    Each ``left`` path is checked against *left* (the outer shape with earlier aliases nested, so it may reach
    an earlier ``as``), each ``right`` path against *right*. A path must name one **leaf**: ``element``,
    ``through.N`` (``through.N.F``), ``member`` (``member.F``), ``key``, ``derived.NAME.…`` or ``AS.…``. A path
    stopping at an object is refused, because the equality is stated on single values.

    Returns:
        *left* with *right* nested under *alias*.

    Raises:
        JoinPathOutside: a path names no leaf of its row.
    """
    for left_path, right_path in on:
        for side, path, shape, row in (
            ("left", left_path, left, "the row so far"),
            ("right", right_path, right, "the joined read's row"),
        ):
            if not _names_a_leaf(shape, path):
                raise JoinPathOutside(
                    alias,
                    side,
                    path,
                    f"{join_refusal_prefix(alias)}`{side}` path '{path}' names no value of {row}; a path names one "
                    f"value of the declared row, and that row's are {_leaf_paths(shape)}",
                )
    return {**left, alias: dict(right)}


@dataclass(frozen=True)
class RowJoin:
    """One join entry with its joined read answered: rows, equalities, alias.

    *rows* are the joined read's identity rows after its own ``where``, carrying its own computed values.
    """

    rows: Sequence[Mapping[str, Any]]
    on: tuple[tuple[str, str], ...]
    alias: str


def _value_at(row: Any, path: str) -> Any:
    """The value at a checked *path* of *row*; a value the row does not carry reads as ``null``.

    ``through`` is indexed by step number; :func:`join_shape` checked the path, so an index out of range is a
    defect and raises. A missing key or a step below ``null`` reads as ``null``, the contract's reading of an
    omitted optional identity field.
    """
    value = row
    for step in path.split("."):
        if value is None:
            return None
        value = value.get(step) if isinstance(value, Mapping) else value[int(step)]
    return value


@dataclass(frozen=True)
class JoinProduct:
    """What one join entry would build, counted before a combined row is built.

    *rows* is the **exact** number of combined rows the entry builds (``null`` keys on neither side);
    *position* and *alias* name the entry. *built* is the running total over this entry and every earlier one;
    a bound on a request's rows reads *built*, since k entries each under a per-entry limit build up to k times
    it. *matched* and *index* are the count's buckets, handed over uncopied.
    """

    position: int
    alias: str
    rows: int
    built: int
    matched: Mapping[Any, Sequence[Mapping[str, Any]]] = field(repr=False, compare=False)
    index: Mapping[Any, Sequence[Mapping[str, Any]]] = field(repr=False, compare=False)

    @property
    def groups(self) -> tuple[tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]], ...]:
        """The matched pairs, one ``(outer rows, joined rows)`` per shared key, in no stated order."""
        return tuple((tuple(lefts), tuple(self.index[key])) for key, lefts in self.matched.items())


#: Asked by :func:`join_rows` with each entry's :class:`JoinProduct` before the entry builds a row; it
#: refuses by raising. Handed in by the caller that owns a limit, because this module has none.
JoinBound = Callable[[JoinProduct], None]


def _join_key(row: Mapping[str, Any], paths: Sequence[str]) -> tuple[Any, ...] | None:
    """The comparison key of *row* at *paths*, or ``None`` when any of them reads ``null``."""
    values = [_value_at(row, path) for path in paths]
    if any(value is None for value in values):
        return None
    return tuple(comparison_key(value) for value in values)


def join_rows(
    rows: Sequence[Mapping[str, Any]], joins: Sequence[RowJoin], *, bound: JoinBound | None = None
) -> list[Mapping[str, Any]]:
    """*rows* inner-joined with each entry of *joins*, in order — a hash join.

    Equality is :func:`values_equal`'s. An outer row combines with every joined row whose ``right`` values equal
    its ``left`` values, the match nested under the alias; an outer row with no match is **dropped**. Rows come
    out in outer order, then in each entry's order. The joined rows are collapsed here; the outer rows are the
    caller's to collapse.

    **A ``null`` matches nothing**, as in an SQL inner join: equal ``null`` values would otherwise join every
    unresolved row with every other one. (A clause, by contrast, treats ``null`` as equal to ``null``.)

    **The product is counted before it is built**: *bound* is handed each entry's :class:`JoinProduct` before
    the entry builds a row, entry by entry because a later ``left`` path may reach an earlier alias. A later
    entry can only drop rows, yet an entry is refused on the rows it would build: the bound is on the work.

    What it does not reach: without a *bound* nothing is refused and the product is built whole. The count is
    of rows, not their width, and the joined reads' own cost is the caller's.

    Raises:
        Exception: whatever *bound* raises, before the entry it refuses builds a row.
    """
    current: list[Mapping[str, Any]] = list(rows)
    built = 0
    for position, entry in enumerate(joins):
        index: dict[Any, list[Mapping[str, Any]]] = {}
        for right in collapse_rows(entry.rows):
            key = _join_key(right, [path for _left, path in entry.on])
            if key is not None:
                index.setdefault(key, []).append(right)
        keyed = [(left, _join_key(left, [path for path, _right in entry.on])) for left in current]
        if bound is not None:
            outer: dict[Any, list[Mapping[str, Any]]] = {}
            for left, key in keyed:
                if key is not None and key in index:
                    outer.setdefault(key, []).append(left)
            count = sum(len(lefts) * len(index[key]) for key, lefts in outer.items())
            built += count
            bound(
                JoinProduct(
                    position=position,
                    alias=entry.alias,
                    rows=count,
                    built=built,
                    matched=outer,
                    index=index,
                )
            )
        matched: list[Mapping[str, Any]] = []
        for left, key in keyed:
            if key is None:
                continue
            for right in index.get(key, ()):
                matched.append({**left, entry.alias: right})
        current = matched
    return current
