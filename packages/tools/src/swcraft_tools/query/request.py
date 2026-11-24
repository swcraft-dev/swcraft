"""The query request grammar: the schema versions, the request types, and ``parse_request``.

A request arrives as JSON (a ``--request`` file or the MCP tool's argument). ``parse_request`` refuses
an unknown key or a malformed value with a ``QueryError`` and returns a ``QueryRequest``. The value
comparisons that ``where`` clauses use (``values_equal``, ``holds_no_value``) live here as well.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel

from swcraft_tools.model_registry import LayerFacts, ModelRegistry
from swcraft_tools.query.edges import integral

# ---------------------------------------------------------------------------
# The set projections — what `swcraft query` answers over
# ---------------------------------------------------------------------------
#
# The filter, the page and the member slice restate ``editing.read``'s, because
# nothing outside ``editing/`` may import it; they speak the kernel's vocabulary
# and take a ``ModelState``. A test holds the two to one answer (``--format json`` against ``read`` at
# ``index``, ``--form full`` against ``read`` at ``full``). Not reached: ``summary`` and ``count``, an
# address below a layer, and a misplaced element's placement value, which the CLI prints as read and the
# protocol refuses to publish.


#: The version of the ``swcraft query --format json`` document (slice and walk),
#: bumped when the shape changes so a stale environment is refused, not mis-parsed.
#: The composite identity of ``Task.runnables`` and ``Sequence.steps`` is
#: ``(swc, instance, runnable)``, the absent selector ``null``.
QUERY_SCHEMA_VERSION = 3


#: The version of the ``rows`` document (a request carrying ``derived`` or
#: ``join``). Separate because it carries ``rows`` where a slice document carries
#: ``elements``, so a reader of :data:`QUERY_SCHEMA_VERSION` refuses it by number.
QUERY_ROWS_SCHEMA_VERSION = 4


#: The version of the ``full`` document (``--form full`` or ``form: "full"``).
#: Separate for the same reason: its ``elements`` hold whole values, not
#: identities. The document also carries ``"form": "full"``.
QUERY_FULL_SCHEMA_VERSION = 5


#: The widths this surface answers in: ``index`` (the identities, the default)
#: and ``full`` (every declared field). ``summary`` and ``count`` are not answered here.
QueryForm = Literal["index", "full"]
QUERY_FORMS: tuple[QueryForm, ...] = ("index", "full")

#: The width a request that states no ``form`` (and a ``--form``-less command line) is answered in.
DEFAULT_QUERY_FORM: Final[QueryForm] = "index"


class _Absent:
    """No value was given — as against a value that is ``null``.

    ``--where unit=null`` asks for a field holding no value, not for no clause.
    """

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<absent>"


#: The "no value was given" sentinel, public for callers composing a :class:`Clause` or a cursor.
ABSENT = _Absent()


class _Missing:
    """A field the thing being tested does not carry at all."""

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<missing>"


MISSING = _Missing()


class QueryError(ValueError):
    """A request this surface cannot answer.

    Two causes. The model's **declarations** may not answer it: a clause naming
    a field no kind of the layer declares, ``members`` naming no declared
    collection, an ``after`` cursor the slice does not carry, a layer that is no
    layer, a ``derived`` name outside the vocabulary or computed at another set,
    a ``join`` path naming no value of the row it reads, a ``where`` over a
    computed value the request does not ask for, naming no value of it or asking
    a set-valued one for no value (``exists: false``, ``equals: null``). Or the
    **arguments** are no request: the CLI raises it for an argv shape that
    touches no declaration (``--where`` with no ``=``, a slice argument beside a
    walk, an ``--after`` with no ``--limit``) and for an unreadable or non-JSON
    ``--request`` file.

    The CLI prints the message and exits non-zero; each message names the key it
    is about. Not raised for a ``limit`` or ``depth`` below 1 or not a whole
    number: that is a plain ``ValueError`` (:func:`check_limit`,
    :func:`_check_depth`), which the CLI catches alike.
    """


@dataclass(frozen=True)
class Clause:
    """One selection clause: a declared field equals a value, or is carried at all.

    AND-composed with every other clause; equality is canonical (an ``int``
    reaches a ``float`` field of the same value, a ``bool`` never reaches a
    number). There are no comparison operators and no ``or``.
    """

    field: str
    equals: Any = ABSENT
    exists: bool | None = None

    def __post_init__(self) -> None:
        if (self.equals is ABSENT) == (self.exists is None):
            raise ValueError("a clause carries exactly one of `equals` and `exists`")

    def as_json(self) -> dict[str, Any]:
        """The clause as the protocol spells it, for the echo in ``--format json``."""
        if self.equals is ABSENT:
            return {"field": self.field, "exists": self.exists}
        return {"field": self.field, "equals": self.equals}

    def as_text(self) -> str:
        """The clause as the scope footer names it."""
        if self.equals is ABSENT:
            return f"{self.field} {'exists' if self.exists else 'absent'}"
        return f"{self.field}={literal(self.equals)}"


@dataclass(frozen=True)
class QueryPage:
    """What a windowed answer says about the set it is a window into.

    ``truncated`` is ``returned < total`` and **not** ``next``'s presence, so
    the last window of a walk is truncated with no cursor and a ``limit`` above
    the set size is neither.
    """

    total: int
    returned: int
    truncated: bool
    next: Any = ABSENT


@dataclass(frozen=True)
class Slice:
    """One answered slice of one layer: its identities, and what produced them.

    ``identities`` is a layer element's declared identity, or a member slice's
    row identity (``{"element", "member"}`` for a list collection,
    ``{"element", "key"}`` for a map one, with ``through`` between the two for a
    dotted path); a cursor replays the same object.

    ``values`` is the ``full`` width: one whole value per identity, in the same
    order; ``None`` at ``index``, so it alone says which width the slice answers.
    ``member_identity_field`` names the field a member slice's scalar identity is
    the value of; ``None`` otherwise.

    ``derived`` or ``join`` makes the slice a **rows** answer (:attr:`is_rows`):
    every identity is a wrapped row (``{"element": E, ...}``) carrying its
    computed values under ``"derived"`` and each match under its alias,
    identical rows collapsed, and the page cursor is the whole row.
    """

    layer: str
    identity_field: str
    identities: tuple[Any, ...]
    where: tuple[Clause, ...] = ()
    members: str | None = None
    page: QueryPage | None = None
    derived: tuple[str, ...] = ()
    join: tuple[QueryJoin, ...] = ()
    values: tuple[Any, ...] | None = None
    member_identity_field: str | None = None

    @property
    def is_rows(self) -> bool:
        """Whether this is an identity-row answer (the enhanced-row branch) rather than an identity slice."""
        return bool(self.derived or self.join)


# ---------------------------------------------------------------------------
# The request — `swcraft query --request FILE`, parsed by the kernel
# ---------------------------------------------------------------------------
#
# Restates ``editing.protocol.ReadRequest`` for the boundary above, on a
# sub-language: a layer address and a ``form`` absent, ``index`` or ``full``.
# An element, field, member or entry address and ``summary`` are refused. Not a
# runtime JSON Schema: ``jsonschema`` is a dev dependency.

#: ``$defs/layerName``, restated for the boundary reason above.
LAYER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


#: ``$defs/memberPath``: a collection name or a dotted path of them, no step empty.
MEMBERS_PATH_PATTERN = re.compile(r"^[^.]+(\.[^.]+)*$")


#: ``$defs/joinAlias``'s pattern.
JOIN_ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


#: The row keys no join alias may shadow — ``$defs/joinAlias``'s ``not``/``enum``.
RESERVED_JOIN_ALIASES: frozenset[str] = frozenset({"element", "through", "member", "key", "derived"})


#: The most entries one ``join`` may carry — ``$defs/join``'s ``maxItems``,
#: published by ``discover`` as ``reads.join.max_entries``; ``editing/`` imports
#: it from here. A ninth entry is ``schema-violation`` on the protocol and a
#: :class:`QueryError` (exit 1) on the CLI. Every entry's joined read is computed
#: in full before the row ceiling is consulted, so the cap bounds the work; eight
#: leaves room for a chained walk.
JOIN_MAX_ENTRIES = 8


_REQUEST_KEYS = frozenset({"address", "form", "where", "limit", "after", "members", "derived", "join"})


_ADDRESS_KEYS = frozenset({"layer", "element", "path"})


_ADDRESSED_KEYS = frozenset({"address"})


_CLAUSE_KEYS = frozenset({"field", "equals", "exists"})


_CLAUSE_REQUIRED_KEYS = frozenset({"field"})


_JOINED_KEYS = frozenset({"address", "members", "where", "derived"})


_JOIN_ENTRY_KEYS = frozenset({"with", "on", "as"})


_JOIN_CONDITION_KEYS = frozenset({"left", "right"})


_READ_FORMS = ("summary", "full", "index")


@dataclass(frozen=True)
class JoinedQuery:
    """One join entry's read, in the kernel's vocabulary — ``$defs/joinedRead``.

    A layer, optionally a ``members`` path, ``where`` clauses and ``derived``
    names; never a form, a page or a join of its own.
    """

    layer: str
    members: str | None = None
    where: tuple[Clause, ...] = ()
    derived: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """The joined read as the protocol spells it."""
        joined: dict[str, Any] = {"address": {"layer": self.layer}}
        if self.members is not None:
            joined["members"] = self.members
        if self.where:
            joined["where"] = [clause.as_json() for clause in self.where]
        if self.derived:
            joined["derived"] = list(self.derived)
        return joined


@dataclass(frozen=True)
class QueryJoin:
    """One item of ``join``: the joined read, its ``(left, right)`` row paths, and its alias."""

    read: JoinedQuery
    on: tuple[tuple[str, str], ...]
    alias: str

    def as_json(self) -> dict[str, Any]:
        """The entry as the protocol spells it (``with``, ``on``, ``as``)."""
        return {
            "with": self.read.as_json(),
            "on": [{"left": left, "right": right} for left, right in self.on],
            "as": self.alias,
        }


@dataclass(frozen=True)
class QueryRequest:
    """A read request in the kernel's vocabulary — what ``--request FILE`` carries, parsed.

    Built only by :func:`parse_request`, which owns every envelope rule; the
    dataclass itself states none, because no Python caller builds one by hand.
    ``after`` is :data:`ABSENT` when omitted, since ``null`` is a cursor value.
    """

    layer: str
    form: QueryForm | None = None
    where: tuple[Clause, ...] = ()
    members: str | None = None
    limit: int | None = None
    after: Any = ABSENT
    derived: tuple[str, ...] = ()
    join: tuple[QueryJoin, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """The request as the protocol spells it, minus ``operation`` — its normalized form."""
        request: dict[str, Any] = {"address": {"layer": self.layer}}
        if self.members is not None:
            request["members"] = self.members
        if self.form is not None:
            request["form"] = self.form
        if self.where:
            request["where"] = [clause.as_json() for clause in self.where]
        if self.limit is not None:
            request["limit"] = self.limit
        if self.after is not ABSENT:
            request["after"] = self.after
        if self.derived:
            request["derived"] = list(self.derived)
        if self.join:
            request["join"] = [entry.as_json() for entry in self.join]
        return request


def _request_object(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QueryError(f"{what} is a JSON object, not {type(value).__name__}")
    return value


def _request_keys(data: Mapping[str, Any], allowed: frozenset[str], required: frozenset[str], what: str) -> None:
    unknown = sorted(str(key) for key in set(data) - allowed)
    if unknown:
        raise QueryError(f"{what} carries no {unknown}; its keys are {sorted(allowed)}")
    missing = sorted(required - set(data))
    if missing:
        raise QueryError(f"{what} requires {missing}")


def _is_json_array(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _request_layer(value: Any, what: str) -> str:
    """The layer of a request's ``address``, refusing every address that names more than a layer."""
    address = _request_object(value, f"the `address` of {what}")
    unknown = sorted(str(key) for key in set(address) - _ADDRESS_KEYS)
    if unknown:
        raise QueryError(f"the `address` of {what} carries no {unknown}")
    if "element" in address or "path" in address:
        raise QueryError(
            f"the `address` of {what} names a layer and nothing in it: this surface answers a layer's "
            "identities, and an element, field, member or entry is read through the editing protocol's `read`"
        )
    if "layer" not in address:
        raise QueryError(f"the `address` of {what} requires ['layer']")
    layer = address["layer"]
    if not isinstance(layer, str) or not LAYER_NAME_PATTERN.fullmatch(layer):
        raise QueryError(f"the `address` of {what} names a layer ({LAYER_NAME_PATTERN.pattern}), not {layer!r}")
    return layer


def _request_where(data: Mapping[str, Any], what: str) -> tuple[Clause, ...]:
    if "where" not in data:
        return ()
    value = data["where"]
    if not _is_json_array(value) or not value:
        raise QueryError(f"`where` of {what} is an array of at least one clause, or is omitted entirely")
    clauses = []
    for item in value:
        clause = _request_object(item, "a `where` clause")
        _request_keys(clause, _CLAUSE_KEYS, _CLAUSE_REQUIRED_KEYS, "a `where` clause")
        name = clause["field"]
        if not isinstance(name, str) or not name:
            raise QueryError(f"a `where` clause's `field` is a non-empty string, not {name!r}")
        if ("equals" in clause) == ("exists" in clause):
            raise QueryError("a `where` clause carries exactly one of `equals` and `exists`")
        if "exists" in clause:
            if not isinstance(clause["exists"], bool):
                raise QueryError("a `where` clause's `exists` is true or false")
            clauses.append(Clause(field=name, exists=clause["exists"]))
        else:
            clauses.append(Clause(field=name, equals=clause["equals"]))
    return tuple(clauses)


def _request_members(data: Mapping[str, Any], what: str) -> str | None:
    if "members" not in data:
        return None
    value = data["members"]
    if not isinstance(value, str) or not MEMBERS_PATH_PATTERN.match(value):
        raise QueryError(f"`members` of {what} is a collection field or a dotted path of them, and no step is empty")
    return value


def _request_derived(data: Mapping[str, Any], what: str) -> tuple[str, ...]:
    if "derived" not in data:
        return ()
    value = data["derived"]
    if not _is_json_array(value) or not value or not all(isinstance(name, str) and name for name in value):
        raise QueryError(f"`derived` of {what} is an array of at least one computed-value name, or is omitted entirely")
    names = tuple(value)
    if len(set(names)) != len(names):
        raise QueryError(f"`derived` of {what} names each computed value once")
    return names


def join_entries_refusal(entries: int) -> str:
    """The one sentence both parsers refuse a ``join`` above :data:`JOIN_MAX_ENTRIES` with."""
    return (
        f"`join` carries {entries} entries, above the {JOIN_MAX_ENTRIES} a join may carry "
        "(`reads.join.max_entries`): every entry's joined read is computed in full on every page"
    )


def _request_join(data: Mapping[str, Any]) -> tuple[QueryJoin, ...]:
    if "join" not in data:
        return ()
    value = data["join"]
    if not _is_json_array(value) or not value:
        raise QueryError("`join` is an array of at least one entry, or is omitted entirely")
    if len(value) > JOIN_MAX_ENTRIES:
        raise QueryError(join_entries_refusal(len(value)))
    entries: list[QueryJoin] = []
    aliases: set[str] = set()
    for item in value:
        entry = _request_object(item, "a `join` entry")
        _request_keys(entry, _JOIN_ENTRY_KEYS, _JOIN_ENTRY_KEYS, "a `join` entry")
        joined = _request_object(entry["with"], "a joined read (`with`)")
        _request_keys(joined, _JOINED_KEYS, _ADDRESSED_KEYS, "a joined read (`with`)")
        read = JoinedQuery(
            layer=_request_layer(joined["address"], "a joined read"),
            members=_request_members(joined, "a joined read"),
            where=_request_where(joined, "a joined read"),
            derived=_request_derived(joined, "a joined read"),
        )
        conditions = entry["on"]
        if not _is_json_array(conditions) or not conditions:
            raise QueryError("a `join` entry's `on` is an array of at least one condition")
        on: list[tuple[str, str]] = []
        for condition in conditions:
            pair = _request_object(condition, "a join condition")
            _request_keys(pair, _JOIN_CONDITION_KEYS, _JOIN_CONDITION_KEYS, "a join condition")
            left, right = pair["left"], pair["right"]
            if not (isinstance(left, str) and left and isinstance(right, str) and right):
                raise QueryError("a join condition's `left` and `right` are each a non-empty row path")
            on.append((left, right))
        alias = entry["as"]
        if not isinstance(alias, str) or not JOIN_ALIAS_PATTERN.fullmatch(alias):
            raise QueryError(f"a join alias matches {JOIN_ALIAS_PATTERN.pattern}, not {alias!r}")
        if alias in RESERVED_JOIN_ALIASES:
            raise QueryError(f"a join alias may not be '{alias}', a key every row carries of its own")
        if alias in aliases:
            raise QueryError(f"join alias '{alias}' is already an earlier entry's `as`")
        aliases.add(alias)
        entries.append(QueryJoin(read=read, on=tuple(on), alias=alias))
    return tuple(entries)


def _rows_keys(derived: Sequence[str], join: Sequence[QueryJoin]) -> str:
    """The keys among ``derived`` and ``join`` a request carries, as a refusal names them."""
    return " and ".join(name for name, present in (("`derived`", derived), ("`join`", join)) if present)


def parse_request(data: Any) -> QueryRequest:
    """``--request``'s JSON object as a :class:`QueryRequest`, or the :class:`QueryError` it earns.

    The keys of a protocol ``read`` request minus ``operation``. Only the
    request's **shape** is refused here (the CLI exits 1); whether a layer,
    collection, computed value or row path is declared is the answering code's
    question.

    Raises:
        QueryError: *data* is not an object; it carries a key no read request
            carries (``operation`` included); its address names more than a
            layer; its ``form`` is neither ``index`` nor ``full``; or any key is malformed —
            including ``derived`` or ``join`` without an explicit
            ``form: "index"``, a join alias repeated and a ``join`` of more
            than :data:`JOIN_MAX_ENTRIES` entries.
    """
    request = _request_object(data, "a request")
    _request_keys(request, _REQUEST_KEYS, _ADDRESSED_KEYS, "a request")
    layer = _request_layer(request["address"], "a request")
    form = None
    if "form" in request:
        form = request["form"]
        if not isinstance(form, str) or form not in _READ_FORMS:
            raise QueryError(f"`form` is one of {sorted(_READ_FORMS)}, not {form!r}")
        if form not in QUERY_FORMS:
            raise QueryError(
                f"`form: {form!r}` is the MCP `read`'s answer-budget form, and this surface prints "
                "`index` (the identities, the default) or `full` (every declared field): state one or omit the key"
            )
    limit = None
    if "limit" in request:
        limit = integral(request["limit"], 1)
        if limit is None:
            raise QueryError("`limit` is a page size of at least one, or is omitted entirely")
    if "after" in request and limit is None:
        raise QueryError("`after` continues a page, so it is sent with a `limit`; a page is a bounded window")
    derived = _request_derived(request, "a request")
    join = _request_join(request)
    if (derived or join) and form != "index":
        raise QueryError(
            f'a request carrying {_rows_keys(derived, join)} answers identity rows, so it states `form: "index"`'
        )
    return QueryRequest(
        layer=layer,
        form=form,
        where=_request_where(request, "a request"),
        members=_request_members(request, "a request"),
        limit=limit,
        after=request["after"] if "after" in request else ABSENT,
        derived=derived,
        join=join,
    )


def canonical(value: Any) -> Any:
    """The JSON-shaped form of a model value — ``editing.address.canonical_value``'s rule.

    Restated for the ``editing/`` boundary; the dispatch must stay
    ``isinstance(value, BaseModel)``, exactly as the protocol's.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


def comparison_key(value: Any) -> Any:
    """A hashable canonical key — ``editing.address.identity_key``'s two rules.

    A boolean is not an integer (``--where advisory=true`` must not reach a field
    holding ``1``), and an integer and a float of the same value are one number
    -- except an ``int`` no ``float`` holds exactly, which gets a bucket of its
    own so ``float()`` cannot round it onto a neighbour or overflow.
    """
    value = canonical(value)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, float):
        return ("number", value)
    if isinstance(value, int):
        # Sound because these keys are only compared for equality and hashed, never sorted.
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
        return ("map", tuple(sorted(((k, comparison_key(v)) for k, v in value.items()), key=lambda kv: kv[0])))
    if isinstance(value, (list, tuple)):
        return ("list", tuple(comparison_key(item) for item in value))
    return ("other", repr(value))


def values_equal(left: Any, right: Any) -> bool:
    """Canonical equality — the equality every clause and every cursor is compared by."""
    return comparison_key(left) == comparison_key(right)


def holds_no_value(value: Any) -> bool:
    """Whether a value that IS there still carries nothing: ``None`` or the empty string.

    What an ``exists`` clause counts as no value, for a present value only (a
    missing field is tested first). ``""`` is no value because every defaulted
    string field defaults to it; ``0``, ``false`` and ``{}`` are values.
    """
    return value is None or value == ""


def literal(value: Any) -> str:
    """One value as the text renderer writes it: a bare string, else JSON."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def ordered_elements(elements: Sequence[Any], layer: LayerFacts, registry: ModelRegistry) -> list[Any]:
    """One layer's elements in **the read order**: kind rank, then reader order.

    The order ``editing.read.ordered_layer_elements`` publishes, deliberately not
    ``swcraft list``'s plain reader order, so a paging client and a whole-layer
    reader see one sequence.
    """
    rank = {kind.kind: index for index, kind in enumerate(layer.kinds)}

    def kind_rank(element: Any) -> int:
        facts = registry.kind_of_model(type(element))
        return rank.get(facts.kind, len(rank)) if facts is not None else len(rank)

    return sorted(elements, key=kind_rank)
