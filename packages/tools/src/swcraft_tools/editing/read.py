"""``read`` — the addressed content, in exactly the vocabulary discovery declared.

A ``read`` takes an address and answers a layer, an element, or anything inside
an element with the same response shape; it needs no scope, writes no audit
record and carries no baseline token.

**The answer is published field by field, off the registry**, never
``model_dump()``, so a read answers in the vocabulary a ``create`` can use.

**No read discloses storage**: nothing in a response is derived from a path, and
a ``group`` that is not a single path segment is refused
(:class:`PlacementNotPublishable`). Elements are ordered by declared kind, then by
the path composed from placement and identity, never a file's own name.

**An answer has a width** (``summary``, ``full``, ``index``) that the address
defaults (:func:`default_form`) and the response echoes; every form reduces width,
never depth. ``members``, ``where`` and ``limit``/``after`` run before the form, in
that order; the first two are echoed, the window answered by a ``page`` block.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

# Read through the owning module at the call, not imported by name, so a test that
# lowers ``query.request.JOIN_MAX_ENTRIES`` lowers what ``discover`` publishes too.
from swcraft_tools import query as _kernel_query
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import (
    Address,
    Miss,
    Resolution,
    Resolved,
    attribute_for,
    canonical_value,
    complete_composite_identity,
    member_identity_of,
    resolve,
    values_equal,
)
from swcraft_tools.editing.protocol import (
    ABSENT,
    DEFAULT_DISCOVER_FORM,
    DISCOVER_FORMS,
    MODEL_MAP_LIST_MAX,
    READ_FORMS,
    AddressShape,
    DiscoverCapability,
    DiscoverForm,
    ErrorResult,
    Hint,
    JoinCapability,
    JoinEntry,
    Measured,
    ModelMapResponse,
    Page,
    ReadForm,
    ReadManyResponse,
    ReadRequest,
    ReadResponse,
    ReadsCapability,
    WhereClause,
    check_rows,
)
from swcraft_tools.project import SwcraftProject
from swcraft_tools.query import ABSENT as QUERY_ABSENT
from swcraft_tools.query import (
    QUERY_FORMS,
    JoinPathOutside,
    JoinProduct,
    RowCursorNotFound,
    RowJoin,
    RowStep,
    answer_rows,
    computed_rows,
    derived_refusal,
    full_element,
    full_value,
    holds_no_value,
    identity_rows,
    join_refusal_prefix,
    join_shape,
    row_cursor_refusal,
    row_shape,
)
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from swcraft_tools.validator import DERIVED, derive_rows
from swcraft_tools.wire_unit import compact_json, escaped_bytes, measure

__all__ = [
    "CLI_ANSWERS_SENTENCE",
    "DEFAULT_FORM_BY_SHAPE",
    "HINT_PAGE_SIZE",
    "JOIN_ROW_CEILING",
    "NARROWING_ORDER",
    "READ_FORMS",
    "RESPONSE_BUDGET_WIRE_BYTES",
    "SET_SHAPES",
    "AddressShape",
    "Answer",
    "FilterNotApplicable",
    "JoinOverBudget",
    "MemberRow",
    "MembersNotApplicable",
    "OverBudget",
    "PageCursorNotFound",
    "PageNotApplicable",
    "PlacementNotPublishable",
    "ReadForm",
    "ReadMissError",
    "ReadsCapability",
    "SummaryNotDerivable",
    "UnknownFilterField",
    "UnknownMemberCollection",
    "address_shape",
    "collection_index",
    "compact_json",
    "default_discover_form",
    "default_form",
    "escaped_bytes",
    "filter_kinds",
    "layer_index",
    "measure",
    "measure_response",
    "model_map",
    "narrower_forms",
    "ordered_layer_elements",
    "paginate",
    "placement_of",
    "publish_element",
    "publish_member_rows",
    "read",
    "read_answer",
    "read_many",
    "read_value",
    "reads_capability",
    "slice_members",
    "summarize",
    "summarize_collection",
    "summarize_published",
    "summary_field_names",
]


#: The response budget in the wire unit: the bytes a host receives for one tool
#: result. A constant, never an environment variable or a manifest field: a budget
#: the model can raise is no bound. ``80_000`` is about 20 000 tokens at four bytes
#: a token, under Claude Code's 25 000-token default result ceiling. Tests lower
#: it through ``monkeypatch``.
RESPONSE_BUDGET_WIRE_BYTES = 80_000


#: The most rows a ``join`` may build in total, summed over its entries and
#: counted exactly before a combined row is built: a bound on the join's work,
#: where :data:`RESPONSE_BUDGET_WIRE_BYTES` bounds the answer. Below it the
#: product is built and paged like any set; above it the request is refused
#: ``over-budget`` with no hints (:exc:`JoinOverBudget`).
#:
#: Cumulative, because a per-entry ceiling would admit up to
#: ``query.JOIN_MAX_ENTRIES`` entries just under it each; every intermediate
#: product counts, even where a later entry drops its rows. The join builds
#: about 73 000 rows a second and every page recomputes it, so 50 000 rows is
#: about 0.7 s a page. Published as ``reads.join.row_ceiling``, read at the call.
#:
#: What it does not reach: every entry's joined read (its slice and ``derived``
#: values) is computed in full before the ceiling is consulted, on every page.
#: The entry cap ``query.JOIN_MAX_ENTRIES``, refused ``schema-violation`` at
#: parse, bounds that repetition, so the ceiling keeps meaning rows of product.
JOIN_ROW_CEILING = 50_000


#: The sentence a size refusal gains where ``swcraft query --request`` can answer
#: the refused request: a layer address in ``index`` or ``full`` form, with or
#: without ``members`` (``derived`` and ``join`` at ``index`` only). The CLI keeps
#: a request's ``limit`` and ``after``, so the sentence says to leave them out.
#: Appended by :func:`_names_the_cli`, never in :meth:`OverBudget.__init__`:
#: ``discover``'s refusal and a ``summary`` or below-layer refusal are not the
#: CLI's to answer.
CLI_ANSWERS_SENTENCE = (
    "`swcraft query --request FILE --project PATH` answers this request with no response budget (PATH is the "
    "project's `swcraft.yaml`); without `limit` and `after` in the file it prints the whole answer"
)


#: The page size a refusal's hint asks for, published in :func:`reads_capability`.
#: A constant rather than a size searched for: re-measuring smaller pages would
#: make the most expensive refusal more expensive, and a hinted page that still
#: does not fit refuses in its turn. Fifty elements of the widest summary fit a
#: small fraction of the budget while a large layer is walked in a few dozen
#: requests. It bounds nothing: a requested ``limit`` is not held to it.
HINT_PAGE_SIZE = 50


#: The forms in narrowing order (not ``discover``'s publication order), the refusal
#: chain: a ``full`` that does not fit offers ``summary`` and ``index``, a
#: ``summary`` offers ``index``, an ``index`` offers nothing.
NARROWING_ORDER: tuple[ReadForm, ...] = ("full", "summary", "index")


class PlacementNotPublishable(ValueError):
    """A placement value on disk that the protocol cannot answer with.

    The reader reports a flat ``model/swc/Foo.yaml`` as ``group: ""`` and
    ``model/swc/a/b/Foo.yaml`` as ``group: "a/b"``. Publishing either would disclose
    a fragment of the storage path and answer a value the write side refuses
    (``apply._check_path_values``, ``registry.compose_path``), so a client echoing it
    in a ``create`` would be refused. The refusal carries the write side's code and
    address, ``schema-violation`` at ``{layer, element, [group]}``; there is no
    honest single-segment value to normalize to.

    Not raised from :func:`publish_element` in general: the staged tree publishes
    every baseline element, and an uncomposable placement on an element a change
    set never writes must not poison its layer. The refusal belongs on the paths
    that put a value in a response.
    """

    def __init__(self, layer: str, element: str, field: str, message: str) -> None:
        super().__init__(message)
        self.layer = layer
        self.element = element
        self.field = field


class ReadMissError(LookupError):
    """The address resolves against nothing, and this is the step that failed.

    It carries the typed :class:`~swcraft_tools.editing.address.Miss` rather than a
    code: the passes choose between ``unknown-field`` and ``broken-reference``.
    """

    def __init__(self, miss: Miss) -> None:
        super().__init__(miss.detail)
        self.miss = miss
        #: The snapshot the miss was found in, set by :func:`read` (``None`` from a direct caller of the kernel).
        self.snapshot: ModelSnapshot | None = None


class SummaryNotDerivable(ValueError):
    """A top-level kind whose layer identity is not one of its declared fields.

    A conforming contract declares that field on every top-level kind, so this is a
    contract defect said out loud rather than a summary missing its identity. Not
    reachable for a member kind, which takes its owning collection's identity (or,
    for ``connector_endpoint``, none).
    """

    def __init__(self, kind: str, layer: str, identity_field: str) -> None:
        super().__init__(
            f"'{kind}' is a top-level kind of layer '{layer}' and does not declare "
            f"its layer's identity field '{identity_field}'"
        )


class UnknownFilterField(ValueError):
    """A ``where`` clause naming a field nothing at this address declares.

    ``unknown-field``, so an undeclared field is told apart from an empty result.
    The address carried back is the one the client sent: the clause is not part of
    the address.
    """

    def __init__(self, address: Address, field: str, message: str) -> None:
        super().__init__(message)
        self.address = address
        self.field = field


class FilterNotApplicable(ValueError):
    """A ``where`` this address cannot be filtered by: ``schema-violation``.

    * **the address names one thing** (an element, a scalar field, a member, a map
      entry): ignoring the filter would tell a client its clause held;
    * **the clause names a collection field**: a question about members asked at
      the wrong level; the message says where to ask it.
    """

    def __init__(self, address: Address, message: str) -> None:
        super().__init__(message)
        self.address = address


class PageNotApplicable(ValueError):
    """A ``limit`` at an address that has no set to window: ``schema-violation``.

    Only a layer and a collection address carry many things. Each refusal is a
    class of its own because its protocol code is a property of the type; one class
    with a code argument would move that decision to every raise site.
    """

    #: The subject of the refusal the read itself words: the client's ``limit``.
    LIMIT_SUBJECT: ClassVar[str] = "a `limit` windows a set"

    def __init__(self, address: Address, shape: Literal["element", "value"]) -> None:
        #: What the address names instead of a set: an element, or a single value.
        self.shape = shape
        super().__init__(self.refusal(self.LIMIT_SUBJECT))
        self.address = address

    def refusal(self, subject: str) -> str:
        """The one refusal sentence, for *subject* (the request's ``limit``, or the engine's ``count``)."""
        names = "an element" if self.shape == "element" else "a single value"
        return f"{subject}, so it applies at a layer address or at a collection address; this address names {names}"


class PageCursorNotFound(LookupError):
    """An ``after`` naming an identity the selected set does not carry.

    ``broken-reference``, the code of every content miss: the cursor's element can
    be removed between two pages, and a keyset cursor says so where an offset would
    silently answer another window.
    """

    def __init__(self, address: Address, after: Any, message: str) -> None:
        super().__init__(message)
        self.address = address
        self.after = after


class UnknownMemberCollection(ValueError):
    """A ``members`` step naming a collection no kind at that step declares: ``unknown-field``.

    The first step is checked against the layer's top-level kinds, each later one
    against the member kinds of the collection before it. A name only some kinds
    declare is a selection, not this refusal. The address carried back is the one
    the client sent.
    """

    def __init__(self, address: Address, members: str, message: str) -> None:
        super().__init__(message)
        self.address = address
        self.members = members


class MembersNotApplicable(ValueError):
    """A ``members`` this address or field cannot answer: ``schema-violation``.

    * **the address is not a layer address**;
    * **the named field is declared and is not a collection** (the mirror of
      :class:`FilterNotApplicable`'s second condition; each message names the key
      that belongs);
    * **a step other than the last names a map**: a map's entries are reached by
      key, so a path cannot step below one.
    """

    def __init__(self, address: Address, message: str) -> None:
        super().__init__(message)
        self.address = address


class UnknownDerivedValue(ValueError):
    """A ``derived`` name outside the published ``reads.derived`` vocabulary: ``unknown-field``."""

    def __init__(self, address: Address, message: str) -> None:
        super().__init__(message)
        self.address = address


class DerivedNotApplicable(ValueError):
    """A ``derived`` name at a set its vocabulary entry's ``at`` does not name: ``schema-violation``.

    For example ``connection_ends`` asked of the ``swc`` layer.
    """

    def __init__(self, address: Address, message: str) -> None:
        super().__init__(message)
        self.address = address


class JoinPathOutsideRow(ValueError):
    """A ``join`` condition's path that names no value of the declared row it reads: ``schema-violation``.

    Decided from the declarations (:func:`~swcraft_tools.query.join_shape`) before
    any row is computed, so it is refused on an empty model too.
    """

    def __init__(self, address: Address, message: str) -> None:
        super().__init__(message)
        self.address = address


class OverBudget(ValueError):
    """The response would exceed the server's budget, so it is refused, never truncated.

    A truncated answer is one whose shape the server no longer knows. The refusal
    carries what a client can act on without a second round trip:

    * :attr:`measured`: how big, against what, over how many elements;
    * :attr:`hints`: the complete, narrower requests the address admits, in
      :data:`NARROWING_ORDER`. Each may refuse in turn with a shorter chain; the
      last carries none, and an empty tuple is no ``hints`` key on the wire.

    The address is ``None`` for ``discover``, which is not addressed; its
    ``errorResult`` then omits ``addresses``.
    """

    def __init__(
        self,
        *,
        address: Address | None,
        measured: Measured,
        hints: tuple[Hint, ...],
    ) -> None:
        # The two message shapes differ by the clause that names the address and
        # by nothing else, so a client reading either meets the same sentence.
        where = f" at {address.to_json()}" if address is not None else ""
        super().__init__(
            f"the response{where} measures {measured.wire_bytes} wire bytes over "
            f"{measured.elements} element(s), above the server's {measured.budget}-byte budget; "
            "it is refused rather than truncated"
        )
        self.address = address
        self.measured = measured
        self.hints = hints


class JoinOverBudget(OverBudget):
    """A join whose combined rows would exceed :data:`JOIN_ROW_CEILING`, refused before one is built.

    The product is counted exactly from the hash join's buckets
    (:class:`~swcraft_tools.query.JoinProduct`), summed over the entries: an entry
    that would take the total above the ceiling is refused before it builds a row.
    ``over-budget``, with :attr:`measured` stating as ``elements`` the total the
    join would have built and, as ``wire_bytes``, the size of the refused entry's
    unpaged ``index`` answer (:func:`_product_wire_bytes`).

    **No hint.** A page narrows the answer and not the work, since every page builds
    the whole join. A ``where`` hint would be a different question picked by the
    server, and the join's own key paths have no ``where`` spelling. The message
    says what narrows it: ``where`` on the outer read or on a joined read.
    """

    def __init__(self, *, address: Address, product: JoinProduct, measured: Measured, ceiling: int) -> None:
        super().__init__(address=address, measured=measured, hints=())
        self.args = (
            f"the join at {address.to_json()} would build {measured.elements} row(s) in total by entry "
            f"'{product.alias}' ({product.rows} of them at that entry), above the server's join row ceiling of "
            f"{ceiling} rows for the whole join (that entry's rows unpaged would be an `index` answer of "
            f"{measured.wire_bytes} wire bytes); it is refused before the entry builds a row, and a page would not "
            "bring it under, since every page builds the whole join: narrow the outer read or a joined read with "
            "`where`",
        )


@dataclass(frozen=True)
class Answer:
    """A published value, the form it was published in, and its page block.

    They travel together because only the code that chose the branch knows the
    form, and the page's figures (``total`` above all) are not recoverable from the
    value. ``page`` is ``None`` when the request carried no ``limit``, and is then
    omitted from the response.
    """

    value: Any
    form: ReadForm
    page: Page | None = None


def address_shape(address: Address, resolution: Resolution) -> AddressShape:
    """Which of the four shapes *address* is, as ``discover`` names them.

    A :class:`~swcraft_tools.editing.address.Miss` classifies on the address alone.
    """
    if address.element is None:
        return "layer"
    field = resolution.field if resolution.found else None
    if field is not None and field.collection is not None:
        return "collection"
    return "field" if address.path else "element"


#: The default form by :func:`address_shape`, stated once: :func:`default_form`
#: applies it and ``discover`` publishes it. ``summary`` for the two shapes that
#: answer many things, ``full`` for the rest. ``index`` is never a default: a
#: summary that fits is more useful than a list of names.
#: instead.
DEFAULT_FORM_BY_SHAPE: Mapping[AddressShape, ReadForm] = MappingProxyType(
    {
        "layer": "summary",
        "collection": "summary",
        "element": "full",
        "field": "full",
    }
)


def default_form(address: Address, resolution: Resolution) -> ReadForm:
    """The form an address answers in when the request named none."""
    return DEFAULT_FORM_BY_SHAPE[address_shape(address, resolution)]


def default_discover_form(layer: str | None, discover_default: DiscoverForm | None = None) -> DiscoverForm:
    """The form a ``discover`` that names none is served in.

    *discover_default* is the channel's project-wide default; ``None`` gives
    :data:`~swcraft_tools.editing.protocol.DEFAULT_DISCOVER_FORM`. A ``discover``
    naming a layer always gets that default: ``baseline`` publishes no descriptor.
    """
    if layer is not None:
        return DEFAULT_DISCOVER_FORM
    return discover_default or DEFAULT_DISCOVER_FORM


def reads_capability(discover_default: DiscoverForm | None = None) -> ReadsCapability:
    """What ``discover`` publishes about reading, composed from the enforced constants at the call.

    *discover_default*: see :func:`default_discover_form`.
    """
    return ReadsCapability(
        forms=READ_FORMS,
        default_form=DEFAULT_FORM_BY_SHAPE,
        budget_wire_bytes=RESPONSE_BUDGET_WIRE_BYTES,
        hint_page_size=HINT_PAGE_SIZE,
        discover=DiscoverCapability(forms=DISCOVER_FORMS, default_form=default_discover_form(None, discover_default)),
        derived=tuple(entry.as_json() for entry in DERIVED),
        join=JoinCapability(row_ceiling=JOIN_ROW_CEILING, max_entries=_kernel_query.request.JOIN_MAX_ENTRIES),
    )


def read(
    project: SwcraftProject,
    address: Address,
    *,
    form: ReadForm | None = None,
    where: Sequence[WhereClause] = (),
    limit: int | None = None,
    after: Any = ABSENT,
    members: str | None = None,
    derived: Sequence[str] = (),
    join: Sequence[JoinEntry] = (),
    registry: reg.Registry | None = None,
    bounded: bool = True,
) -> dict[str, Any]:
    """The answer to a ``read`` request against *project*.

    Args:
        form: the requested width, or ``None`` for :func:`default_form`'s default.
        where: AND-composed filter clauses, applied before the page and the form.
        limit: the page size, or ``None`` for the whole set; *after* is its keyset
            cursor (:func:`paginate`).
        members: a declared collection or a dotted path of them, legal at a layer
            address and applied first (:func:`slice_members`).
        derived: computed values added to each identity row (layer address,
            ``form: "index"``); *join* follows the same rules.
        registry: the projection serving the request, or ``None`` for the current one.
        bounded: whether the answer is held to the response budget.

    Returns:
        A ``readResponse`` as JSON-compatible data, with no baseline token.

    Raises:
        ReadMissError, UnknownFilterField, FilterNotApplicable, PageNotApplicable,
        PageCursorNotFound, UnknownMemberCollection, MembersNotApplicable,
        UnknownDerivedValue, DerivedNotApplicable, JoinPathOutsideRow,
        PlacementNotPublishable, SummaryNotDerivable, OverBudget: the typed refusals,
            each answered by the engine with its own protocol code.
        ElementParseError: a model file under the root fails to parse.
        ValueError: the project's model root does not exist.
    """
    registry = reg.or_current(registry)
    with reg.kernel_context(registry):
        snapshot = load_snapshot(str(project.resolved_model_root))
        try:
            answer = read_answer(
                snapshot,
                address,
                form=form,
                where=where,
                limit=limit,
                after=after,
                members=members,
                derived=derived,
                join=join,
                registry=registry,
                bounded=bounded,
            )
        except ReadMissError as missed:
            # The snapshot the miss was found in rides along, so the nearest names
            # (`engine._with_nearest`) cost no second load of the model.
            missed.snapshot = snapshot
            raise
        # Built through the protocol model that also measures it (`measure_response`),
        # so every key sent is inside the budget.
        return ReadResponse(
            address=address,
            value=answer.value,
            form=answer.form,
            where=tuple(where),
            page=answer.page,
            members=members,
            derived=tuple(derived),
            join=tuple(join),
        ).to_json()


def read_value(
    snapshot: ModelSnapshot,
    address: Address,
    *,
    form: ReadForm | None = None,
    where: Sequence[WhereClause] = (),
    limit: int | None = None,
    after: Any = ABSENT,
    members: str | None = None,
    derived: Sequence[str] = (),
    join: Sequence[JoinEntry] = (),
    registry: reg.Registry | None = None,
) -> Any:
    """The published value at *address*, or a :class:`ReadMissError`.

    For a caller holding a snapshot (a test's reference model, a staged copy) and
    no project. :func:`read_answer` is the same call that also returns the form and
    the page block.
    """
    registry = reg.or_current(registry)
    return read_answer(
        snapshot,
        address,
        form=form,
        where=where,
        limit=limit,
        after=after,
        members=members,
        derived=derived,
        join=join,
        registry=registry,
    ).value


def read_answer(
    snapshot: ModelSnapshot,
    address: Address,
    *,
    form: ReadForm | None = None,
    where: Sequence[WhereClause] = (),
    limit: int | None = None,
    after: Any = ABSENT,
    members: str | None = None,
    derived: Sequence[str] = (),
    join: Sequence[JoinEntry] = (),
    registry: reg.Registry | None = None,
    bounded: bool = True,
) -> Answer:
    """The published value at *address*, the form it was published in, and its page.

    The address is walked once, and the branch that publishes the value names the
    form, so the two cannot disagree.

    Raises:
        OverBudget: the response would exceed :data:`RESPONSE_BUDGET_WIRE_BYTES`,
            unless *bounded* is ``False`` (``engine._answer_count`` alone, whose page
            is discarded). What this does not reach: the change-set path (``apply``,
            ``delta``, ``derive``) calls :func:`publish_element` directly, so a
            staged snapshot and its delta are unbounded.
        EnvelopeError: *derived* or *join* without ``form: "index"``, away from a
            layer address, with too many join entries or a repeated alias
            (:func:`~swcraft_tools.editing.protocol.check_rows`), checked again for
            a direct caller.
    """
    check_rows(address, form, tuple(derived), tuple(join))
    registry = reg.or_current(registry)
    layer = _layer_or_none(registry, address.layer)
    placement = placement_of(snapshot, layer, address.element)
    resolution: Resolution = resolve(address, snapshot, registry=registry, placement=placement)
    if not resolution.found:
        raise ReadMissError(resolution)

    # Classified once: the default form, the publishing branch, the chain and the
    # element count all read it.
    shape = address_shape(address, resolution)
    # Whether `swcraft query --request` could answer this request, decided before a member slice
    # reclassifies the shape below: the CLI answers a layer address and nothing narrower
    # (`query.parse_request`); `_refuse_over_budget` reads the form (`QUERY_FORMS`) off the served answer.
    at_a_layer = shape == "layer"
    if members is not None:
        # The slice runs first: it changes what the set at this address is, and the
        # keys below select from, window and publish whatever set they are handed.
        resolution = slice_members(resolution, shape, address, members, layer=layer, registry=registry)
        # A member slice answers a set of members, so from here the address takes the
        # collection shape's rules: default form, filter vocabulary, narrowing, count.
        shape = "collection"
    # The filter runs before the form: a slice of a summary could not see a field
    # the form dropped. A clause over a computed value (`derived.NAME.PATH`) waits
    # for the row kernel (`_answer_rows`).
    declared_where, derived_where = _kernel_query.split_where(where)
    if derived_where and not (derived or join):
        _refuse_derived_clauses(address, derived_where, (), _kernel_query.BARE_ROW_SHAPE)
    resolution = select(resolution, shape, address, declared_where, snapshot, layer=layer, registry=registry)
    if derived or join:
        # Computed values and the join apply after the filter and before the page:
        # both multiply rows, and the page windows rows. Each join entry is counted
        # against the join row ceiling before it is built (`_join_bound`); the page
        # is measured against the response budget below.
        try:
            answer = _answer_rows(
                snapshot,
                address,
                resolution,
                members=members,
                derived=derived,
                join=join,
                where=where,
                limit=limit,
                after=after,
                layer=layer,
                registry=registry,
            )
        except JoinOverBudget as over_ceiling:
            # An identity-row request is a layer address in `index` form by the
            # envelope's rule (`check_rows`), so the CLI answers every one of
            # them; the flag is still the one the paged refusal reads.
            if at_a_layer:
                _names_the_cli(over_ceiling)
            raise
        if bounded:
            _refuse_over_budget(
                answer,
                address,
                resolution,
                shape,
                registry=registry,
                layer=layer,
                where=where,
                limit=limit,
                after=after,
                members=members,
                derived=derived,
                join=join,
                at_a_layer=at_a_layer,
            )
        return answer
    # The page runs between the filter and the form: `total` counts the selected
    # set, and the form publishes whatever the window holds.
    resolution, page = paginate(
        resolution, shape, address, limit, after, layer=layer, registry=registry, members=members
    )
    answer = replace(
        _answer_at(
            snapshot,
            address,
            resolution,
            shape,
            layer=layer,
            registry=registry,
            placement=placement,
            form=form,
            members=members,
        ),
        page=page,
    )
    if bounded:
        _refuse_over_budget(
            answer,
            address,
            resolution,
            shape,
            registry=registry,
            layer=layer,
            where=where,
            limit=limit,
            after=after,
            members=members,
            at_a_layer=at_a_layer,
        )
    return answer


def _names_the_cli(refusal: OverBudget) -> OverBudget:
    """*refusal* with :data:`CLI_ANSWERS_SENTENCE` appended to its message, and nothing else changed.

    The sentence is prose for a reader, never a hint: a hint is a request a client
    replays, and a command line is not one.
    """
    refusal.args = (f"{refusal.args[0]}; {CLI_ANSWERS_SENTENCE}",)
    return refusal


def _answer_rows(
    snapshot: ModelSnapshot,
    address: Address,
    resolution: Resolved,
    *,
    members: str | None,
    derived: Sequence[str],
    join: Sequence[JoinEntry],
    where: Sequence[WhereClause],
    limit: int | None,
    after: Any,
    layer: reg.LayerDescriptor | None,
    registry: reg.Registry,
) -> Answer:
    """Identity rows carrying *derived*, collapsed, joined and paged: the enhanced-row branch.

    The answer has ``index``'s shape, ``{"count", "identities"}``. Refusals are
    decided from the declarations before any value is computed, except each joined
    read's ``where``; a joined read's refusal keeps its type, code and address and
    gains the alias prefix. The ceiling does not reach the joined reads, which are
    computed in full on every page.

    Raises:
        UnknownDerivedValue: a name the vocabulary does not carry.
        DerivedNotApplicable: a name at a set its entry's ``at`` does not name.
        JoinPathOutsideRow: a join path names no value of its declared row.
        JoinOverBudget: the rows built so far would exceed :data:`JOIN_ROW_CEILING`.
        PageCursorNotFound: ``after`` is no row of the computed set.
    """
    _check_derived(address, derived, members)
    shape = row_shape(_row_steps(members, address, layer=layer, registry=registry), derived, DERIVED)
    derived_where = _kernel_query.split_where(where)[1]
    _refuse_derived_clauses(address, derived_where, derived, shape)
    joined_layers: list[tuple[JoinEntry, tuple[tuple[str, str], ...], reg.LayerDescriptor | None, Resolved]] = []
    for entry in join:
        joined = entry.read
        joined_layer = _layer_or_none(registry, joined.address.layer)
        try:
            joined_resolution: Resolution = resolve(joined.address, snapshot, registry=registry)
            if not joined_resolution.found:
                raise ReadMissError(joined_resolution)
            steps = _row_steps(joined.members, joined.address, layer=joined_layer, registry=registry)
            _check_derived(joined.address, joined.derived, joined.members)
            right = row_shape(steps, joined.derived, DERIVED)
            _refuse_derived_clauses(joined.address, _kernel_query.split_where(joined.where)[1], joined.derived, right)
        except _JOINED_READ_REFUSALS as refused:
            raise _named_by_alias(entry.alias, refused) from refused
        on = tuple((condition.left, condition.right) for condition in entry.on)
        try:
            shape = join_shape(shape, on, entry.alias, right)
        except JoinPathOutside as outside:
            raise JoinPathOutsideRow(address, str(outside)) from outside
        joined_layers.append((entry, on, joined_layer, joined_resolution))
    joins: list[RowJoin] = []
    for entry, on, joined_layer, joined_resolution in joined_layers:
        try:
            identities = _joined_identities(snapshot, entry, joined_resolution, layer=joined_layer, registry=registry)
        except _JOINED_READ_REFUSALS as refused:
            raise _named_by_alias(entry.alias, refused) from refused
        joins.append(
            RowJoin(
                rows=computed_rows(
                    identity_rows(identities, sliced=entry.read.members is not None),
                    entry.read.derived,
                    derive=derive_rows,
                    state=snapshot.state,
                    where=_kernel_clauses(_kernel_query.split_where(entry.read.where)[1]),
                ),
                on=on,
                alias=entry.alias,
            )
        )
    rows = identity_rows(
        _selected_identities(resolution, members, layer=layer, registry=registry), sliced=members is not None
    )
    try:
        window, kernel_page = answer_rows(
            rows,
            derived,
            derive=derive_rows,
            state=snapshot.state,
            joins=joins,
            limit=limit,
            after=QUERY_ABSENT if after is ABSENT else after,
            bound=_join_bound(address, where=where, members=members, derived=derived, join=join),
            where=_kernel_clauses(derived_where),
        )
    except RowCursorNotFound as missing:
        raise PageCursorNotFound(address, after, row_cursor_refusal(json.dumps(after, ensure_ascii=False))) from missing
    page = None
    if kernel_page is not None:
        page = Page(
            total=kernel_page.total,
            returned=kernel_page.returned,
            truncated=kernel_page.truncated,
            next=ABSENT if kernel_page.next is QUERY_ABSENT else kernel_page.next,
        )
    return Answer({"count": len(window), "identities": [dict(row) for row in window]}, "index", page)


def _join_bound(
    address: Address,
    *,
    where: Sequence[WhereClause],
    members: str | None,
    derived: Sequence[str],
    join: Sequence[JoinEntry],
) -> Callable[[JoinProduct], None]:
    """The bound :func:`~swcraft_tools.query.join_rows` asks before each entry builds a row.

    It holds the cumulative row count
    (:attr:`~swcraft_tools.query.JoinProduct.built`) against :data:`JOIN_ROW_CEILING`,
    read at the call. Over it the entry is refused :exc:`JoinOverBudget`, with
    :func:`_product_wire_bytes` computed only then.

    What it does not reach: it bounds rows, not bytes or row width (a combined row
    nests each earlier match); an admitted page over the response budget is refused
    by :func:`_refuse_over_budget`. The joined reads and the outer side's own
    computation are neither measured nor refused; the entry cap
    ``query.JOIN_MAX_ENTRIES`` bounds how many joined reads there are.
    """

    def bound(product: JoinProduct) -> None:
        ceiling = JOIN_ROW_CEILING
        if product.built <= ceiling:
            return
        size = _product_wire_bytes(product, address, where=where, members=members, derived=derived, join=join)
        raise JoinOverBudget(
            address=address,
            product=product,
            measured=Measured(wire_bytes=size, budget=RESPONSE_BUDGET_WIRE_BYTES, elements=product.built),
            ceiling=ceiling,
        )

    return bound


def _product_wire_bytes(
    product: JoinProduct,
    address: Address,
    *,
    where: Sequence[WhereClause],
    members: str | None,
    derived: Sequence[str],
    join: Sequence[JoinEntry],
) -> int:
    """The wire bytes of the ``readResponse`` carrying *product*'s rows unpaged, without building one row.

    :exc:`JoinOverBudget`'s ``measured.wire_bytes``: the answer the refused entry's
    rows would make were it the last entry. Linear in the two sides, never in the
    product.

    Exact, not modelled: :func:`measure` escapes per character, so a concatenation's
    escaped size is the sum of its parts' (each without its two wrapping quotes);
    and a combined row ``{**left, AS: right}`` is ``left``'s text without its
    closing brace, ``,"AS":``, ``right``'s text and the brace. The response is the
    envelope with an empty ``identities`` list and the real ``count``, plus every
    row's content, plus one comma between each two rows.
    """
    envelope = measure_response(
        {"count": product.rows, "identities": []},
        address,
        "index",
        where=where,
        members=members,
        derived=derived,
        join=join,
    )
    separator = escaped_bytes("," + compact_json(product.alias) + ":") - 2
    rows = 0
    for lefts, rights in product.groups:
        right_bytes = sum(measure(right) - 2 for right in rights)
        rows += sum(measure(left) - 2 + separator for left in lefts) * len(rights) + len(lefts) * right_bytes
    return envelope + rows + max(product.rows - 1, 0)


#: The refusals a joined read's own keys can earn — its address, member path,
#: computed names and ``where`` — each re-raised by :func:`_named_by_alias`.
_JOINED_READ_REFUSALS = (
    ReadMissError,
    UnknownMemberCollection,
    MembersNotApplicable,
    UnknownDerivedValue,
    DerivedNotApplicable,
    UnknownFilterField,
    FilterNotApplicable,
)


def _named_by_alias(alias: str, refused: Exception) -> Exception:
    """*refused* again — same type, same address, so the same code — its message prefixed ``join 'AS': ``."""
    prefix = join_refusal_prefix(alias)
    if isinstance(refused, ReadMissError):
        return ReadMissError(replace(refused.miss, detail=prefix + refused.miss.detail))
    if isinstance(refused, UnknownFilterField):
        return UnknownFilterField(refused.address, refused.field, prefix + str(refused))
    if isinstance(refused, UnknownMemberCollection):
        return UnknownMemberCollection(refused.address, refused.members, prefix + str(refused))
    assert isinstance(refused, (MembersNotApplicable, UnknownDerivedValue, DerivedNotApplicable, FilterNotApplicable))
    return type(refused)(refused.address, prefix + str(refused))


def _refuse_derived_clauses(
    address: Address, clauses: Sequence[WhereClause], derived: Sequence[str], shape: Mapping[str, Any]
) -> None:
    """The refusal of a derived clause the row cannot answer: a value not asked for, a path it lacks, an absent set.

    Answered ``unknown-field`` (:class:`UnknownFilterField`), the code of an
    undeclared filter field.
    """
    refused = _kernel_query.derived_where_refusal(_kernel_clauses(clauses), derived, shape, DERIVED)
    if refused is not None:
        raise UnknownFilterField(address, refused[0], refused[1])


def _kernel_clauses(clauses: Sequence[WhereClause]) -> tuple[_kernel_query.Clause, ...]:
    """The protocol's clauses in the row kernel's spelling — its own ``ABSENT`` for an ``exists`` clause."""
    return tuple(
        _kernel_query.Clause(field=clause.field, exists=clause.exists)
        if clause.equals is ABSENT
        else _kernel_query.Clause(field=clause.field, equals=clause.equals)
        for clause in clauses
    )


def _check_derived(address: Address, derived: Sequence[str], members: str | None) -> None:
    """The refusal of a ``derived`` name outside the vocabulary or at a set it does not apply at."""
    refusal = derived_refusal(DERIVED, derived, address.layer, members)
    if refusal is not None:
        refused = UnknownDerivedValue if refusal.kind == "unknown" else DerivedNotApplicable
        raise refused(address, refusal.message)


def _row_steps(
    members: str | None, address: Address, *, layer: reg.LayerDescriptor | None, registry: reg.Registry
) -> tuple[RowStep, ...]:
    """Each step of *members* as the declared row shape reads it — its container and identity rule.

    Read off the steps :func:`_resolve_member_path` resolves (and refuses), so
    the shape a join path is checked against is the one the rows are published
    in. A layer read has no step.
    """
    if members is None or layer is None:
        return ()
    steps: list[RowStep] = []
    for field in _resolve_member_path(members, address, layer, registry):
        collection = field.collection
        assert collection is not None  # every step resolved to a collection field
        steps.append(RowStep(collection.kind, collection.member_identity, tuple(collection.identity_fields)))
    return tuple(steps)


def _selected_identities(
    resolution: Resolved, members: str | None, *, layer: reg.LayerDescriptor | None, registry: reg.Registry
) -> list[Any]:
    """The identities the ``index`` form publishes for a selected set, before they are wrapped as rows."""
    if members is not None:
        return [_row_identity(row, resolution.field) for row in resolution.value]
    if layer is None:  # pragma: no cover - an undeclared layer always misses above
        return []
    return [
        canonical_value(getattr(element, layer.identity_field, None))
        for element in ordered_layer_elements(resolution.value, layer, registry)
    ]


def _joined_identities(
    snapshot: ModelSnapshot,
    entry: JoinEntry,
    resolution: Resolved,
    *,
    layer: reg.LayerDescriptor | None,
    registry: reg.Registry,
) -> list[Any]:
    """A joined read's selected identities: its member path, then its ``where`` — the outer read's order.

    Answered through the same :func:`slice_members` and :func:`select` the
    request's own read goes through, and **never measured**: only the joined
    answer is (:func:`read_answer`).
    """
    joined = entry.read
    shape = address_shape(joined.address, resolution)
    if joined.members is not None:
        resolution = slice_members(resolution, shape, joined.address, joined.members, layer=layer, registry=registry)
        shape = "collection"
    declared_where = _kernel_query.split_where(joined.where)[0]
    resolution = select(resolution, shape, joined.address, declared_where, snapshot, layer=layer, registry=registry)
    return _selected_identities(resolution, joined.members, layer=layer, registry=registry)


def _answer_at(
    snapshot: ModelSnapshot,
    address: Address,
    resolution: Resolved,
    shape: AddressShape,
    *,
    layer: reg.LayerDescriptor | None,
    registry: reg.Registry,
    placement: Mapping[str, str] | None,
    form: ReadForm | None,
    members: str | None = None,
) -> Answer:
    """The publication branch, chosen once: the body of :func:`read_answer`.

    Split out so the budget is measured over its result at one site.
    """
    if layer is None:  # pragma: no cover - an undeclared layer always misses above
        return Answer(_publish(resolution.value, registry), "full")

    requested = DEFAULT_FORM_BY_SHAPE[shape] if form is None else form
    if members is not None:
        # A member slice's rows are `{element, member}` objects; every width publishes
        # the members inside them through the collection's own projections.
        return publish_member_rows(resolution.value, resolution.field, requested, registry)

    if shape == "layer":
        if requested == "index":
            return Answer(layer_index(resolution.value, layer, registry), "index")
        if requested == "summary":
            return Answer(_summarize_layer(resolution.value, snapshot, layer, registry), "summary")
        return Answer(_publish_layer(resolution.value, snapshot, layer, registry), "full")

    if shape == "collection":
        # `collection` is the shape whose resolution *has* a field with a
        # collection on it — that is what `address_shape` classified it by.
        field = resolution.field
        if field is None:
            raise RuntimeError(f"a collection address resolves to a field; one in {address.layer!r} did not")
        if requested == "index":
            return Answer(collection_index(resolution.value, field), "index")
        if requested == "summary":
            return Answer(summarize_collection(resolution.value, field, registry), "summary")
        return Answer(_publish(resolution.value, registry), "full")

    kind = resolution.kind
    if requested == "summary" and kind is not None:
        # Asked first, for its exception alone: a top-level kind without its layer's
        # identity field is refused `schema-violation`, as at a layer address.
        # `_summary_narrows_kind` below swallows that exception (rightly, for the
        # hint chain it also serves).
        summary_field_names(kind, registry)
    if requested == "summary" and kind is not None and _summary_narrows_kind(kind, registry):
        # Placement belongs to the element alone, so a path address summarizes with
        # none. `_summary_narrows_kind` serves `full` (echoed `full`) for a kind
        # whose summary selects nothing (`conversion`, `range`: `{}` would read as
        # the identity conversion) or selects every field with no collection to
        # count (no narrower than `full`). The hint chain reads the same predicate.
        summarized = summarize(resolution.value, kind, registry, placement=None if address.path else placement)
        return Answer(summarized, "summary")

    # A scalar, a map entry and a `whole_value` member are one value already, and
    # `index` has nothing to index: served and echoed as `full`.
    if address.path:
        return Answer(_publish(resolution.value, registry), "full")
    return Answer(publish_element(resolution.value, registry, placement=placement), "full")


# ---------------------------------------------------------------------------
# The budget, and the chain a refusal offers instead
# ---------------------------------------------------------------------------
#
# The unit (`compact_json`, `escaped_bytes`, `measure`) lives in the leaf
# `swcraft_tools.wire_unit`, so a module outside `editing/` can measure without
# importing this package; it is re-exported here.


def measure_response(
    value: Any,
    address: Address,
    form: ReadForm,
    *,
    where: Sequence[WhereClause] = (),
    page: Page | None = None,
    members: str | None = None,
    derived: Sequence[str] = (),
    join: Sequence[JoinEntry] = (),
) -> int:
    """The wire bytes of the whole ``readResponse`` that would carry *value*.

    The response is built and measured, not decomposed into envelope plus value:
    the escaped count is not additive over keys. Building it through
    :class:`~swcraft_tools.editing.protocol.ReadResponse`, the constructor
    :func:`read` sends, keeps every envelope key inside the figure, so an admitted
    response satisfies ``wire <= RESPONSE_BUDGET_WIRE_BYTES`` strictly.
    """
    return measure(
        ReadResponse(
            address=address,
            value=value,
            form=form,
            where=tuple(where),
            page=page,
            members=members,
            derived=tuple(derived),
            join=tuple(join),
        ).to_json()
    )


def _refuse_over_budget(
    answer: Answer,
    address: Address,
    resolution: Resolved,
    shape: AddressShape,
    *,
    registry: reg.Registry,
    layer: reg.LayerDescriptor | None,
    where: Sequence[WhereClause] = (),
    limit: int | None = None,
    after: Any = ABSENT,
    members: str | None = None,
    derived: Sequence[str] = (),
    join: Sequence[JoinEntry] = (),
    at_a_layer: bool = False,
) -> None:
    """The refusal of *answer* if the response carrying it would exceed the budget.

    :func:`measure_response` of the **served** form against
    :data:`RESPONSE_BUDGET_WIRE_BYTES`. The hints keep ``where``, ``members`` and
    ``after``: the narrower forms first, then a page of :data:`HINT_PAGE_SIZE` when
    more units than that were served; under ``derived`` or ``join``, the page alone.
    A layer address served in a ``QUERY_FORMS`` form gains :data:`CLI_ANSWERS_SENTENCE`.

    What this does not reach:

    * a single value over the budget: once its narrower forms are exhausted, the chain is;
    * a set windowed to :data:`HINT_PAGE_SIZE` or fewer units that still busts: no hint at all;
    * a join's joined reads, never measured, and recomputed on every page;
    * the CLI sentence is decided by the request's shape, not by whether the CLI's
      answer would fit a terminal.
    """
    size = measure_response(
        answer.value,
        address,
        answer.form,
        where=where,
        page=answer.page,
        members=members,
        derived=derived,
        join=join,
    )
    if size <= RESPONSE_BUDGET_WIRE_BYTES:
        return
    # Under `derived` or `join` no narrower form is offered: an identity-row
    # request states `form: "index"`, and a hint at another form would be one
    # the envelope refuses. The page alone is the step left.
    rows = bool(derived or join)
    hints: list[Hint] = [
        ReadRequest(address=address, form=narrower, where=tuple(where), limit=limit, after=after, members=members)
        for narrower in ([] if rows else narrower_forms(resolution, shape, answer.form, registry, layer))
    ]
    served = _element_count(answer, shape)
    if shape in SET_SHAPES and served > HINT_PAGE_SIZE:
        hints.append(
            ReadRequest(
                address=address,
                form=answer.form,
                where=tuple(where),
                limit=HINT_PAGE_SIZE,
                after=after,
                members=members,
                derived=tuple(derived),
                join=tuple(join),
            )
        )
    refusal = OverBudget(
        address=address,
        measured=Measured(wire_bytes=size, budget=RESPONSE_BUDGET_WIRE_BYTES, elements=served),
        hints=tuple(hints),
    )
    raise _names_the_cli(refusal) if at_a_layer and answer.form in QUERY_FORMS else refusal


def narrower_forms(
    resolution: Resolved,
    shape: AddressShape,
    served: ReadForm,
    registry: reg.Registry,
    layer: reg.LayerDescriptor | None,
) -> tuple[ReadForm, ...]:
    """The forms the client should try instead of *served* at this address.

    The width half of the chain; :func:`_refuse_over_budget` adds the page step. The
    chain comes from the address' declaration, not the answer's size, and is not
    filtered to the forms that would fit. A served form that does not narrow here
    resumes the chain at ``full``'s place, since ``full`` can fit where it did not.
    """
    chain = _narrowing_chain(resolution, shape, registry, layer)
    position = _chain_position(resolution, shape, served, registry, layer)
    if position not in chain:  # pragma: no cover - `_chain_position` returns a step of the chain
        return ()
    start = chain.index(position)
    return chain[start:] if position != served else chain[start + 1 :]


def _chain_position(
    resolution: Resolved,
    shape: AddressShape,
    served: ReadForm,
    registry: reg.Registry,
    layer: reg.LayerDescriptor | None,
) -> ReadForm:
    """Which step of the chain *served* actually stands at: ``full`` when it does not narrow here."""
    if served == "summary" and not _summary_narrows_at(resolution, shape, registry, layer):
        return "full"
    if served == "index" and not _index_narrows_at(resolution, shape, registry):
        return "full"
    return served


def _collection_of(resolution: Resolved, shape: AddressShape) -> reg.CollectionDescriptor | None:
    """The collection *shape* addresses, or ``None`` for anything else."""
    if shape != "collection":
        return None
    field = resolution.field
    return None if field is None else field.collection


def _summary_narrows_at(
    resolution: Resolved, shape: AddressShape, registry: reg.Registry, layer: reg.LayerDescriptor | None
) -> bool:
    """Whether ``summary`` carries strictly less than ``full`` at this address.

    A layer narrows when one of its top-level kinds does; a scalar or a map entry
    has no kind and never narrows.
    """
    collection = _collection_of(resolution, shape)
    if collection is not None:
        return _summary_narrows_collection(collection, registry)
    if shape == "layer":
        # `read_answer` misses on an undeclared layer before it gets here, so
        # `layer` is never None at a layer address.
        return layer is not None and any(_summary_narrows_kind(kind, registry) for kind in layer.top_level_kinds)
    return resolution.kind is not None and _summary_narrows_kind(resolution.kind, registry)


def _index_narrows_at(resolution: Resolved, shape: AddressShape, registry: reg.Registry) -> bool:
    """Whether ``index`` carries strictly less than the wider forms at this address.

    A collection is decided by :func:`_index_narrows_collection`; a layer always
    narrows (:func:`layer_index` publishes the identity field alone, and every kind
    declares more); any other address is one value with nothing to index.
    """
    collection = _collection_of(resolution, shape)
    if collection is not None:
        return _index_narrows_collection(collection, registry)
    return shape == "layer"


def _summary_narrows_collection(collection: reg.CollectionDescriptor, registry: reg.Registry) -> bool:
    """Whether ``summary`` carries strictly less than ``full`` at *collection*.

    Not for a ``whole_value`` collection (answered verbatim), nor where every member
    kind's summary selects every field; one member kind that narrows is enough.
    """
    if collection.member_identity == "whole_value":
        return False
    return any(_summary_narrows_kind(registry.element_kind(name), registry) for name in collection.member_kinds)


def _summary_narrows_kind(kind: reg.ElementKindDescriptor, registry: reg.Registry) -> bool:
    """Whether the summary rules drop or shrink anything of *kind*.

    **An empty selection answers False.** ``conversion`` and ``range`` declare only
    optional scalars and are members of an ``element_kind`` field, so no identity
    clause applies and nothing is selected. ``{}`` is also the full answer for
    ``conversion: {}``, the identity conversion, so a client could not tell it from
    a narrowed answer: not a width reduction.
    """
    if any(field.collection is not None for field in kind.fields):
        return True  # a collection field is answered by its count
    try:
        selected = frozenset(summary_field_names(kind, registry))
    except SummaryNotDerivable:  # pragma: no cover - every shipped member kind derives a summary
        # The summary cannot be served here, so it is no step of the chain. An
        # explicit request never relies on this: `_answer_at` and
        # `_summarize_layer` derive the summary first and refuse it.
        return False
    if not selected:
        return False
    return selected != frozenset(kind.field_names)


def _index_narrows_collection(collection: reg.CollectionDescriptor, registry: reg.Registry) -> bool:
    """Whether ``index`` carries strictly less than the wider forms at *collection*.

    A map always narrows, a ``whole_value`` list never does, and any other list
    narrows where some member kind declares a field its identity does not name.
    Read off the declaration, never the value's type (an optional collection may be ``None``).
    """
    if collection.kind == "map":
        return True
    if collection.member_identity == "whole_value":
        return False
    identity = _identity_field_names(collection)
    return any(set(registry.element_kind(name).field_names) - identity for name in collection.member_kinds)


def _identity_field_names(collection: reg.CollectionDescriptor) -> frozenset[str]:
    """Which declared fields a list collection's member identity names.

    ``whole_value`` names none, and no caller asks it.
    """
    if collection.member_identity == "field":
        return frozenset({collection.identity_field} if collection.identity_field else ())
    if collection.member_identity == "composite":
        return frozenset(collection.identity_fields)
    return frozenset()  # pragma: no cover - `whole_value` is decided before this is asked


def _narrowing_chain(
    resolution: Resolved, shape: AddressShape, registry: reg.Registry, layer: reg.LayerDescriptor | None
) -> tuple[ReadForm, ...]:
    """The forms of this address that are each strictly narrower than the last; ``full`` is always one.

    What this does not reach: whether a step fits, so a walked chain can end with
    every step refused.
    """
    narrows = {
        "full": True,
        "summary": _summary_narrows_at(resolution, shape, registry, layer),
        "index": _index_narrows_at(resolution, shape, registry),
    }
    return tuple(form for form in NARROWING_ORDER if narrows[form])


def _element_count(answer: Answer, shape: AddressShape) -> int:
    """How many things the refused response would have carried.

    Decided from the address' shape and the served form, never by sniffing the
    value: an element's published value is a mapping too, and ``len()`` of it would
    count fields. It is a count of the value as published.
    """
    value = answer.value
    if answer.form == "index":
        # `layer_index`/`collection_index`'s own shape, this module's and not a guess.
        return int(value["count"])
    if shape in ("layer", "collection"):
        return len(value) if isinstance(value, (list, Mapping)) else 1
    return 1


# ---------------------------------------------------------------------------
# The member slice: a layer-wide collection, before everything else
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemberRow:
    """One member of one element's collection, beside the identity of its owner.

    The unit a member slice selects, windows and publishes; :func:`publish_member_rows`
    turns it into ``{"element", ["through"], ["key"], "member"}``, the address
    steps that reach the member from the layer plus the member itself.

    ``through`` is the owner chain below the element for a dotted ``members`` path:
    each intermediate member's identity in path order
    (:func:`~swcraft_tools.editing.address.member_identity_of`). A one-step path
    carries ``()``, published as no key at all.

    ``key`` carries the map key for a map collection and is
    :data:`~swcraft_tools.editing.protocol.ABSENT` for a list, the distinction the
    address vocabulary draws between a ``key`` step and a ``member`` step.
    """

    element: Any
    member: Any
    key: Any = ABSENT
    through: tuple[Any, ...] = ()


def _resolve_member_path(
    members: str, address: Address, layer: reg.LayerDescriptor, registry: reg.Registry
) -> tuple[reg.FieldDescriptor, ...]:
    """The declared collection field each step of *members* names, or the refusal.

    The first step resolves against the layer's top-level kinds, each later one
    against the member kinds of the collection before it. At every step the first
    declaring kind supplies the descriptor; the walk then skips any member whose own
    kind declares the name as a collection of another shape (:func:`slice_members`).
    """
    steps = members.split(".")
    kinds: tuple[reg.ElementKindDescriptor, ...] = layer.top_level_kinds
    chosen: list[reg.FieldDescriptor] = []
    for position, step in enumerate(steps):
        present = [descriptor for descriptor in (kind.get_field(step) for kind in kinds) if descriptor is not None]
        reached = (
            f"layer '{layer.name}'"
            if position == 0
            else f"the members '{'.'.join(steps[:position])}' reaches in layer '{layer.name}'"
        )
        if not present:
            raise UnknownMemberCollection(
                address,
                members,
                f"'{step}' is not a declared field of an element kind at that step of `members` ({reached}); "
                "`discover` publishes the collections a member slice may name: the first step a collection of "
                "the layer's top-level kinds, each later step a collection of the member kinds the step before "
                "it holds",
            )
        collections = [descriptor for descriptor in present if descriptor.collection is not None]
        if not collections:
            if position == 0:
                raise MembersNotApplicable(
                    address,
                    f"'{step}' declares no collection: it is one value per element, and `members` answers a set "
                    f"of them. Filter this layer by it instead ({{'field': '{step}'}} as a `where` clause)",
                )
            raise MembersNotApplicable(
                address,
                f"'{step}' declares no collection at that step of `members` ({reached}): it is one value per "
                "member, and every step of a member path names a collection",
            )
        field = collections[0]
        collection = field.collection
        assert collection is not None  # narrowed by the filter above
        if collection.kind == "map" and position < len(steps) - 1:
            raise MembersNotApplicable(
                address,
                f"'{step}' is a map collection, and a map may only be the last step of `members`: its entries "
                "are reached by a key, and a step below one has no member to stand on",
            )
        chosen.append(field)
        kinds = tuple(registry.kinds[name] for name in collection.member_kinds if name in registry.kinds)
    return tuple(chosen)


def slice_members(
    resolution: Resolved,
    shape: AddressShape,
    address: Address,
    members: str,
    *,
    layer: reg.LayerDescriptor | None,
    registry: reg.Registry,
) -> Resolved:
    """*resolution*, replaced by every member of one collection across the layer.

    Rows (:class:`MemberRow`) come in published element order, then member order;
    the result's ``field`` is the last step's collection. *members* may be a dotted
    path (``runnables.writes``), and each row then carries ``through``.

    What it does not reach:

    * two kinds declaring one collection name with different shapes: the first
      declaration wins and the other kind's members are skipped;
    * a sibling kind declaring the name as a non-collection: skipped, not iterated;
    * a map anywhere but the last step: refused (:class:`MembersNotApplicable`);
    * rows per component type, not per instance: a type placed twice answers once.

    Raises:
        MembersNotApplicable: *address* is not a layer address, a step names a
            declared field that is not a collection, or a non-last step names a map.
        UnknownMemberCollection: no kind at some step declares that step at all.
    """
    if shape != "layer" or layer is None:
        named = {
            "collection": "one collection already, whose members it answers directly",
            "element": "an element",
        }.get(shape, "a single value")
        raise MembersNotApplicable(
            address,
            "`members` slices one collection across a whole layer, so it applies at a layer address; "
            f"this address names {named}",
        )

    path = _resolve_member_path(members, address, layer, registry)
    field = path[-1]
    rows: list[MemberRow] = []
    for element in ordered_layer_elements(resolution.value, layer, registry):
        identity = canonical_value(getattr(element, layer.identity_field, None))
        rows.extend(_rows_below(element, identity, (), path, registry))
    return replace(resolution, value=rows, field=field, collection=field.collection, kind=None)


def _rows_below(
    owner: Any,
    element: Any,
    through: tuple[Any, ...],
    path: Sequence[reg.FieldDescriptor],
    registry: reg.Registry,
) -> list[MemberRow]:
    """The rows *path* reaches below *owner*, in the order *owner* carries them.

    *owner* is the element at the first call and an intermediate member below it;
    *through* is the identities of the intermediate members already passed.
    """
    step = path[0]
    own = registry.kind_of_model(type(owner))
    declares = own.get_field(step.name) if own is not None else None
    if declares is None or declares.collection != step.collection:
        # The owner's own kind does not declare the name as this collection: skipped,
        # not refused, since a step holds several kinds. Asking the owner's own kind
        # keeps a sibling's scalar of the same name from being iterated one
        # character to a row, and a differently identified collection out of a page.
        return []
    attribute = attribute_for(type(owner), step.name)
    value = None if attribute is None else getattr(owner, attribute)
    if value is None:  # pragma: no cover - no shipped collection field is optional
        # Declared optional and carrying none: no rows for this owner.
        return []
    if isinstance(value, Mapping):
        # A map is the last step: `_resolve_member_path` refused one anywhere else.
        return [
            MemberRow(element=element, member=entry, key=canonical_value(key), through=through)
            for key, entry in value.items()
        ]
    if len(path) == 1:
        return [MemberRow(element=element, member=member, through=through) for member in value]
    collection = step.collection
    assert collection is not None  # every step resolved to a collection field
    rows: list[MemberRow] = []
    for member in value:
        rows.extend(
            _rows_below(member, element, (*through, member_identity_of(member, collection)), path[1:], registry)
        )
    return rows


def publish_member_rows(
    rows: Sequence[MemberRow], field: reg.FieldDescriptor | None, form: ReadForm, registry: reg.Registry
) -> Answer:
    """A member slice's rows, at *form*: the slice's one publication branch.

    The members are projected through the collection's own functions (``full`` is
    :func:`_publish`, ``summary`` is :func:`summarize_collection`, ``index`` the row
    identities alone), so a slice answers in the vocabulary the same members have at
    one element's collection address. The ``index`` identity is the row minus the
    member, the object the page cursor carries.
    """
    if form == "index":
        return Answer({"count": len(rows), "identities": [_row_identity(row, field) for row in rows]}, "index")
    values = [row.member for row in rows]
    if form == "summary" and field is not None:
        published: list[Any] = summarize_collection(values, field, registry)
        served: ReadForm = "summary"
    else:
        # `full`, and a slice with no declared field: every member whole.
        published = _publish(values, registry)
        served = "full"
    return Answer([_publish_row(row, member) for row, member in zip(rows, published, strict=True)], served)


def _publish_row(row: MemberRow, member: Any) -> dict[str, Any]:
    """One row as a client reads it: how to address the member, then the member.

    ``through`` is published only when the path had an intermediate step.
    """
    published: dict[str, Any] = {"element": row.element}
    if row.through:
        published["through"] = list(row.through)
    if row.key is not ABSENT:
        published["key"] = row.key
    published["member"] = member
    return published


def _row_identity(row: MemberRow, field: reg.FieldDescriptor | None) -> dict[str, Any]:
    """The row's identity: the row minus the member, which is how to address it.

    A map entry is named by its key and a list member by its collection's declared
    identity (:func:`~swcraft_tools.editing.address.member_identity_of`), so a
    cursor out of a slice is replayable verbatim. ``through`` is kept: two runnables
    of one component may write the same ``(port, element)`` pair.
    """
    identity: dict[str, Any] = {"element": row.element}
    if row.through:
        identity["through"] = list(row.through)
    if row.key is not ABSENT:
        identity["key"] = row.key
        return identity
    collection = field.collection if field is not None else None
    identity["member"] = (
        canonical_value(row.member) if collection is None else member_identity_of(row.member, collection)
    )
    return identity


# ---------------------------------------------------------------------------
# The filter: selection at the address, before the form
# ---------------------------------------------------------------------------


class _Missing:
    """A field the thing being tested does not carry.

    Distinct from ``None``, a declared field holding no value: ``{"field": "unit",
    "equals": null}`` matches the second and not the first, and ``{"field": "unit",
    "exists": false}`` matches both. Private, as ``address.py``'s own sentinel is,
    because it never leaves this function group.
    """

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<missing>"


_MISSING = _Missing()

#: The two address shapes that answer with a set: a layer answers elements, a
#: collection members. The filter and the page both read this one tuple, because
#: "nothing to select from" and "nothing to window" are the same fact.
SET_SHAPES: tuple[AddressShape, ...] = ("layer", "collection")


def filter_kinds(
    resolution: Resolution, shape: AddressShape, layer: reg.LayerDescriptor | None, registry: reg.Registry
) -> tuple[reg.ElementKindDescriptor, ...]:
    """The declared kinds a filter at this address may name a field of.

    A collection with no declared member kind answers with none, so every clause
    there is an :class:`UnknownFilterField`.
    """
    if shape == "layer":
        return () if layer is None else layer.top_level_kinds
    field = resolution.field if resolution.found else None
    collection = field.collection if field is not None else None
    if collection is None:  # pragma: no cover - a collection shape resolved a collection field
        return ()
    return tuple(registry.element_kind(name) for name in collection.member_kinds)


def select(
    resolution: Resolved,
    shape: AddressShape,
    address: Address,
    where: Sequence[WhereClause],
    snapshot: ModelSnapshot,
    *,
    layer: reg.LayerDescriptor | None,
    registry: reg.Registry,
) -> Resolved:
    """*resolution*, narrowed to what *where* selects, or unchanged when there is no filter.

    The clauses are AND-composed and all checked against the declared vocabulary
    before any element is tested, so a request is refused whole. An empty result is
    an answer, published under the address' default form; the refusals concern the
    request.
    """
    if not where:
        return resolution
    if shape not in SET_SHAPES:
        raise FilterNotApplicable(
            address,
            "a `where` selects from a set, so it applies at a layer address or at a collection "
            f"address; this address names {'an element' if shape == 'element' else 'a single value'}",
        )
    kinds = filter_kinds(resolution, shape, layer, registry)
    for clause in where:
        _check_clause(clause, kinds, address, shape, layer)

    value = resolution.value
    if isinstance(value, Mapping):  # pragma: no cover - a map collection declares no member kind to filter by
        # A map collection's members. Unreachable on the shipped contract, whose one
        # map declares no member kind (every clause is an `UnknownFilterField`
        # first); kept for a map of a declared kind.
        return replace(resolution, value={key: item for key, item in value.items() if _matches(item, where, None)})
    table = _placement_table(snapshot, layer) if shape == "layer" and layer is not None else {}
    return replace(
        resolution,
        value=[item for item in value if _matches(item, where, _raw_placement(item, layer, table))],
    )


def _raw_placement(
    element: Any, layer: reg.LayerDescriptor | None, table: Mapping[str, Mapping[str, str]]
) -> Mapping[str, str] | None:
    """One element's placement values as the snapshot holds them, for testing only.

    Not :func:`placement_of`'s publishable answer: this value is compared, never
    published, so a misplaced sibling must not fail a filter. An element that stays
    in the answer still passes :func:`_publishable` in :func:`_layer_rows`.
    """
    if layer is None:  # pragma: no cover - every address names a declared layer
        return None
    identity = getattr(element, layer.identity_field, None)
    return table.get(identity) if isinstance(identity, str) else None


def _check_clause(
    clause: WhereClause,
    kinds: tuple[reg.ElementKindDescriptor, ...],
    address: Address,
    shape: AddressShape,
    layer: reg.LayerDescriptor | None,
) -> None:
    """The refusal of one clause the address' declarations cannot answer.

    Two refusals, both read off the declaration: no kind at this address declares
    the name, or what it declares is a collection. A kind that does not declare the
    field is not a refusal: a layer holds several kinds, and a clause only one of
    them can satisfy is a selection.

    The message branches on the shape because it points at ``discover``: at a layer
    address only the top-level kinds are filtered (``discover`` marks the others
    ``member_only``); at a collection address, or a ``members`` slice, the
    vocabulary is the kinds published as the collection's ``member_kinds``.
    """
    declared = [kind.get_field(clause.field) for kind in kinds]
    present = [descriptor for descriptor in declared if descriptor is not None]
    if not present:
        if shape == "layer" and layer is not None:
            message = (
                f"'{clause.field}' is not a declared field of any top-level element kind of layer "
                f"'{layer.name}'; `discover` publishes the field vocabulary a filter may name, marking the "
                "kinds a filter here cannot reach with `member_only`"
            )
        else:
            message = (
                f"'{clause.field}' is not a declared field of any member kind of this collection; the "
                "vocabulary here is the declared fields of the kinds `discover` publishes as this "
                "collection's `member_kinds`, and a collection that publishes none holds plain values and "
                "declares no field to filter by"
            )
        raise UnknownFilterField(address, clause.field, message)
    if any(descriptor.collection is not None for descriptor in present):
        raise FilterNotApplicable(
            address,
            f"'{clause.field}' declares a collection and a clause tests one value; address the "
            f"collection itself ({{'field': '{clause.field}'}} as the last path step) and filter "
            "its members there",
        )


def _matches(element: Any, where: Sequence[WhereClause], placement: Mapping[str, str] | None) -> bool:
    """Whether one element or member satisfies every clause (AND).

    A member slice's :class:`MemberRow` is tested by its member, the thing sliced;
    filtering by the owner is the layer address's question.
    """
    tested = element.member if isinstance(element, MemberRow) else element
    return all(_clause_matches(clause, _clause_value(tested, clause.field, placement)) for clause in where)


def _clause_matches(clause: WhereClause, value: Any) -> bool:
    """One clause against one value, with :data:`_MISSING` for a field not carried.

    Branches on ``equals is ABSENT``, the key the clause's invariant is stated over.
    An ``exists`` clause asks whether a value is carried: a missing field, ``None``
    and the empty string are no value (:func:`~swcraft_tools.query.holds_no_value`).
    """
    if clause.equals is ABSENT:
        return (value is not _MISSING and not holds_no_value(value)) == clause.exists
    return value is not _MISSING and values_equal(value, clause.equals)


def _clause_value(element: Any, name: str, placement: Mapping[str, str] | None) -> Any:
    """The value *name* has on one element or member, or :data:`_MISSING`.

    Read through the accessors the answer is published through
    (:func:`~swcraft_tools.editing.address.attribute_for`, the placement mapping),
    so a clause compares the value the response would carry.
    """
    if not isinstance(element, BaseModel):  # pragma: no cover - a plain member declares no field to filter by
        # A `whole_value` collection's members are plain values. Unreachable: such a
        # collection declares no member kind, so every clause is refused first.
        return _MISSING
    attribute = attribute_for(type(element), name)
    if attribute is None:
        return placement.get(name, _MISSING) if placement is not None else _MISSING
    return getattr(element, attribute)


# ---------------------------------------------------------------------------
# The page: a keyset window, after the filter and before the form
# ---------------------------------------------------------------------------


def paginate(
    resolution: Resolved,
    shape: AddressShape,
    address: Address,
    limit: int | None,
    after: Any,
    *,
    layer: reg.LayerDescriptor | None,
    registry: reg.Registry,
    members: str | None = None,
) -> tuple[Resolved, Page | None]:
    """*resolution*, windowed to one page of it, or unchanged when no ``limit`` was named.

    Keyset, never offset: ``after`` names the identity to continue past, in the
    order every form publishes in. ``total`` is the selected set's size (after the
    filter, before the page), and ``next`` is present only when something follows.

    Raises:
        PageNotApplicable: the address names one thing, so there is no set to window.
        PageCursorNotFound: ``after`` names an identity the selected set does not carry.
    """
    if limit is None:
        # `after` without `limit` never reaches here: `$defs/readRequest`'s
        # `dependentRequired` and `ReadRequest.__post_init__` both refuse it.
        return resolution, None
    if shape not in SET_SHAPES:
        raise PageNotApplicable(address, "element" if shape == "element" else "value")

    units = _paged_units(resolution, shape, layer, registry, members)
    total = len(units)
    start = 0 if after is ABSENT else _cursor_position(units, after, address, resolution, shape, members) + 1
    window = units[start : start + limit]
    follows = start + limit < total
    page = Page(
        total=total,
        returned=len(window),
        truncated=len(window) < total,
        next=window[-1][0] if follows and window else ABSENT,
    )
    return replace(resolution, value=_windowed(resolution.value, window)), page


def _paged_units(
    resolution: Resolved,
    shape: AddressShape,
    layer: reg.LayerDescriptor | None,
    registry: reg.Registry,
    members: str | None = None,
) -> list[tuple[Any, Any]]:
    """``(identity, thing)`` for everything at a set address, in the published order.

    The identity is read by the rule that publishes it in the ``index`` form, so a
    ``next`` cursor is a value the client has been shown and can send back
    verbatim, and a composite identity is an object.
    """
    value = resolution.value
    if members is not None:
        # A member identity alone is not unique across the layer (two components may
        # each carry a port `pSpeed`), so the cursor names the owner too.
        return [(_row_identity(row, resolution.field), row) for row in value]
    if shape == "layer":
        if layer is None:  # pragma: no cover - an undeclared layer always misses above
            return []
        return [
            (canonical_value(getattr(element, layer.identity_field, None)), element)
            for element in ordered_layer_elements(value, layer, registry)
        ]
    if isinstance(value, Mapping):
        # A map entry is addressed by a `key` step, so its key is its identity, as
        # `collection_index` answers.
        return [(canonical_value(key), (key, entry)) for key, entry in value.items()]
    if value is None:  # pragma: no cover - no shipped collection field is optional
        return []
    collection = _collection_of(resolution, shape)
    if collection is None:  # pragma: no cover - a collection shape resolved a collection field
        return [(canonical_value(member), member) for member in value]
    return [(member_identity_of(member, collection), member) for member in value]


def _windowed(value: Any, window: Sequence[tuple[Any, Any]]) -> Any:
    """The page's units back in the container shape the address answers with.

    A paged map stays a map, or an entry would be published without its key.
    """
    if isinstance(value, Mapping):
        return {key: entry for _identity, (key, entry) in window}
    return [member for _identity, member in window]


def _cursor_position(
    units: Sequence[tuple[Any, Any]],
    after: Any,
    address: Address,
    resolution: Resolved,
    shape: AddressShape,
    members: str | None,
) -> int:
    """Where *after* sits in the selected set, or :class:`PageCursorNotFound`.

    Compared through :func:`~swcraft_tools.editing.address.values_equal`, the
    equality a ``member`` step resolves by, after completing a composite identity
    with ``None`` for an omitted optional field
    (:func:`~swcraft_tools.editing.address.complete_composite_identity`), so a
    ``summary`` row's identity continues the page its full form does.
    """
    wanted = after
    collection = resolution.field.collection if resolution.field is not None else None
    if members is not None:
        if isinstance(after, Mapping) and "member" in after:
            wanted = {**after, "member": complete_composite_identity(after["member"], collection)}
    elif shape == "collection":
        wanted = complete_composite_identity(after, collection)
    for position, (identity, _thing) in enumerate(units):
        if values_equal(identity, wanted):
            return position
    raise PageCursorNotFound(
        address,
        after,
        f"no element or member at this address carries the identity {json.dumps(after, ensure_ascii=False)}, "
        "so there is no page to continue past it; `after` names an identity this address answered, and one "
        "removed since is refused rather than answered with a different window",
    )


# ---------------------------------------------------------------------------
# Placement metadata: read beside the element, never out of it
# ---------------------------------------------------------------------------


def _layer_or_none(registry: reg.Registry, name: str) -> reg.LayerDescriptor | None:
    try:
        return registry.layer(name)
    except KeyError:
        return None


def _placement_table(snapshot: ModelSnapshot, layer: reg.LayerDescriptor) -> Mapping[str, Mapping[str, str]]:
    """The snapshot's placement metadata for one layer, or an empty mapping.

    Read from :meth:`~swcraft_tools.model_state.ModelState.placement_of` by layer
    name, never through a composed attribute name such as ``swc_placement``, which
    would match only while the nesting layer is named ``swc``.
    """
    table = snapshot.state.placement_of(layer.name)
    return table if isinstance(table, Mapping) else {}


def placement_of(
    snapshot: ModelSnapshot, layer: reg.LayerDescriptor | None, element: str | None
) -> dict[str, str] | None:
    """One element's placement values, keyed by its declared identity.

    Public because the delta needs the same answer: a ``create`` or ``remove``
    entry carries the whole element, and one lookup keeps ``group`` the same in a
    read and in the delta of the change that produced it.
    """
    if layer is None or element is None:
        return None
    values = _placement_table(snapshot, layer).get(element)
    return None if values is None else _publishable(dict(values), layer, element)


def _publishable(values: dict[str, str], layer: reg.LayerDescriptor, element: str) -> dict[str, str]:
    """*values*, or :class:`PlacementNotPublishable` for one the protocol cannot answer with.

    The rule is the registry's own (:func:`~swcraft_tools.editing.registry.segment_refusal`,
    the guard :meth:`~swcraft_tools.editing.registry.Registry.compose_path` uses), so
    what a read publishes and what a ``create`` may send are one statement.
    """
    for kind in layer.element_kinds:
        for descriptor in kind.fields:
            if not descriptor.placement or descriptor.name not in values:
                continue
            refusal = reg.segment_refusal(values[descriptor.name], f"{layer.name} {descriptor.name}", directory=True)
            if refusal is not None:
                raise PlacementNotPublishable(layer.name, element, descriptor.name, refusal)
    return values


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


def ordered_layer_elements(elements: Any, layer: reg.LayerDescriptor, registry: reg.Registry) -> list[Any]:
    """One layer's elements in the module's ordering rule: kind rank, then reader order.

    Shared by every form, so a client stepping from ``full`` to ``summary`` to
    ``index`` never sees the layer reshuffle.
    """
    rank = {kind.kind: index for index, kind in enumerate(layer.element_kinds)}

    def kind_rank(element: Any) -> int:
        descriptor = registry.kind_of_model(type(element))
        return rank.get(descriptor.kind, len(rank)) if descriptor is not None else len(rank)

    return sorted(elements, key=kind_rank)  # stable: the readers' order survives inside a kind


def _publish_layer(
    elements: Any, snapshot: ModelSnapshot, layer: reg.LayerDescriptor, registry: reg.Registry
) -> list[Any]:
    """A whole layer: every element, in registry order, each fully published.

    Every placement goes through :func:`_publishable`, so one misplaced file refuses
    the whole layer read: a response is one value and has no shape for a hole.
    """
    return _layer_rows(elements, snapshot, layer, registry, publish_element)


def _summarize_layer(
    elements: Any, snapshot: ModelSnapshot, layer: reg.LayerDescriptor, registry: reg.Registry
) -> list[Any]:
    """A whole layer in ``summary`` form: every element, same order, fewer fields.

    The same elements in the same order as :func:`_publish_layer`, under the same
    placement guard: a summary that dropped a misplaced element would answer a
    count the model does not have.
    """
    return _layer_rows(elements, snapshot, layer, registry, _summarized_element)


def _summarized_element(element: Any, registry: reg.Registry, *, placement: Mapping[str, str] | None) -> Any:
    """One layer element by the top-level summary rule, in :func:`publish_element`'s signature."""
    kind = registry.kind_of_model(type(element))
    if kind is None:  # pragma: no cover - every model a snapshot holds is a declared kind
        return canonical_value(element)
    return summarize(element, kind, registry, placement=placement)


def _layer_rows(
    elements: Any,
    snapshot: ModelSnapshot,
    layer: reg.LayerDescriptor,
    registry: reg.Registry,
    publish: Callable[..., Any],
) -> list[Any]:
    """Every element of one layer, ordered and placement-guarded, published by *publish*.

    The ordering and the guard belong to the layer address, not to its width, so
    they are written once.
    """
    table = _placement_table(snapshot, layer)
    published = []
    for element in ordered_layer_elements(elements, layer, registry):
        identity = getattr(element, layer.identity_field, None)
        if not isinstance(identity, str):
            identity = None
        values = None if identity is None else table.get(identity)
        placement = _publishable(dict(values), layer, identity) if values and identity is not None else None
        published.append(publish(element, registry, placement=placement))
    return published


def publish_element(element: Any, registry: reg.Registry, *, placement: Mapping[str, str] | None) -> Any:
    """One element or nested member, field by field, in the registry's order (``query.full_element``).

    Public because the change-set lifecycle stages the model in this vocabulary
    (:mod:`swcraft_tools.editing.apply`): a ``create`` authored against a read binds
    against the same shape. A placement field is :func:`_placement_value`'s answer.
    """
    return full_element(element, registry, placement=placement, placement_value=_placement_value)


def _placement_value(
    field: reg.FieldDescriptor, descriptor: reg.ElementKindDescriptor, placement: Mapping[str, str] | None
) -> str:
    """A placement field's value, from beside the element rather than out of it.

    Shared by :func:`publish_element` and :func:`summarize` so the two cannot answer
    ``group`` differently.
    """
    if placement is None or field.name not in placement:
        raise ReadMissError(
            Miss(
                "placement_unavailable",
                Address(layer=descriptor.layer),
                f"'{field.name}' is placement metadata and the snapshot carries none for this element",
            )
        )
    return placement[field.name]


def _publish(value: Any, registry: reg.Registry) -> Any:
    """Any addressed value, published recursively (``query.full_value``), a nested model off its own kind."""
    return full_value(value, registry, _placement_value)


# ---------------------------------------------------------------------------
# The narrow forms: the summary rules and the index projection
# ---------------------------------------------------------------------------


def summary_field_names(kind: reg.ElementKindDescriptor, registry: reg.Registry) -> tuple[str, ...]:
    """The fields the summary rules put in *kind*'s summary, in the registry's field order.

    The identity (and, for a top-level kind, placement and discriminator) fields,
    every required scalar, every collection (as its count) and every scalar
    reference; a required object is left out.

    What this does not reach: whether a summary is small. It reduces width, never
    depth, so a long required scalar (``requirement.description``) is carried whole.

    Raises:
        SummaryNotDerivable: *kind* is top-level and does not declare its layer's
            identity field.
    """
    identity = _summary_identity_names(kind, registry)
    return tuple(
        field.name
        for field in kind.fields
        if field.name in identity
        or field.collection is not None
        or (field.required and field.element_kind is None and not field.placement)
        or _is_scalar_reference(field)
    )


def _is_scalar_reference(field: reg.FieldDescriptor) -> bool:
    """The reference clause: a field naming another element, neither a collection nor an object.

    Required or optional alike; the clause adds the optional scalar references
    (``port.interface``, ``connector.composition`` and the like), which
    :func:`summarize` publishes only when set.
    """
    return field.reference is not None and field.collection is None and field.element_kind is None


def _summary_identity_names(kind: reg.ElementKindDescriptor, registry: reg.Registry) -> frozenset[str]:
    """The identity clause for one kind: the one place the two rules differ."""
    if kind.member_only:
        names: set[str] = set()
        for owner, field_name in kind.member_of:
            declaration = registry.element_kind(owner).get_field(field_name)
            collection = declaration.collection if declaration is not None else None
            if collection is None:  # a required object, not a collection: no identity clause
                continue
            if collection.identity_field is not None:
                names.add(collection.identity_field)
            names.update(collection.identity_fields)
        return frozenset(names)

    layer = registry.layer(kind.layer)
    if layer.identity_field not in kind.field_names:
        raise SummaryNotDerivable(kind.kind, layer.name, layer.identity_field)
    names = {layer.identity_field} | {field.name for field in kind.fields if field.placement}
    if kind.discriminator_field is not None:
        names.add(kind.discriminator_field)
    return frozenset(names)


def _optional_identity_names(kind: reg.ElementKindDescriptor, registry: reg.Registry) -> frozenset[str]:
    """The identity fields an owning collection declares optional, for a member kind."""
    if not kind.member_only:
        return frozenset()
    names: set[str] = set()
    for owner, field_name in kind.member_of:
        declaration = registry.element_kind(owner).get_field(field_name)
        if declaration is not None and declaration.collection is not None:
            names.update(declaration.collection.optional_identity_fields)
    return frozenset(names)


def summarize(
    element: Any,
    kind: reg.ElementKindDescriptor,
    registry: reg.Registry,
    *,
    placement: Mapping[str, str] | None,
) -> dict[str, Any]:
    """One element or member in its declared summary: the same vocabulary, fewer fields.

    Every key is a field :func:`summary_field_names` selected, published as
    :func:`publish_element` publishes it, except that a collection is answered by
    its count. *placement* is the element's placement values (:func:`placement_of`);
    a summary that needs one and has none misses as a full read does.
    """

    def value_of(field: reg.FieldDescriptor) -> Any:
        if field.placement:
            return _placement_value(field, kind, placement)
        attribute = attribute_for(type(element), field.name)
        if attribute is None:  # pragma: no cover - the registry derives from model_fields
            return ABSENT
        return getattr(element, attribute)

    return _summary(kind, registry, value_of, lambda value: _publish(value, registry))


def _summary(
    kind: reg.ElementKindDescriptor,
    registry: reg.Registry,
    value_of: Callable[[reg.FieldDescriptor], Any],
    publish: Callable[[Any], Any],
) -> dict[str, Any]:
    """The summary rules, stated once for :func:`summarize` and :func:`summarize_published`.

    *value_of* reads a field's value (:data:`ABSENT` where there is none) and
    *publish* publishes a non-collection value; the two callers differ in those
    alone.
    """
    names = frozenset(summary_field_names(kind, registry))
    optional_identity = _optional_identity_names(kind, registry)
    summary: dict[str, Any] = {}
    for field in kind.fields:
        if field.name not in names:
            continue
        value = value_of(field)
        if value is ABSENT:
            continue
        if field.placement:
            summary[field.name] = value
            continue
        if value is None and (_is_scalar_reference(field) or field.name in optional_identity):
            # An unset optional reference and an absent selector of a composite
            # identity are omitted, never published as null; the keys left still
            # address the member.
            continue
        summary[field.name] = _count(value) if field.collection is not None else publish(value)
    return summary


def summarize_collection(value: Any, field: reg.FieldDescriptor, registry: reg.Registry) -> Any:
    """A collection address in ``summary`` form: each member by the member rule.

    A ``whole_value`` collection answers verbatim. For ``runnable.events``, whose
    members are kinds, that still holds: an event publishes two or three fields, and
    summarizing it would save nothing. ``None`` (a collection an element does not
    carry) is answered as itself, as :func:`_count` and :func:`_member_identities`
    read it, so an optional collection cannot raise a ``TypeError``.
    """
    collection = field.collection
    if value is None or collection is None or collection.member_identity == "whole_value":
        return _publish(value, registry)
    if isinstance(value, Mapping):
        # pragma reason: the one shipped map collection (`swc.memory`) is
        # `whole_value` and returns above. A declared map of a member *kind* is
        # what this branch is for, and the contract declares none today.
        return {key: _summarize_member(item, registry) for key, item in value.items()}  # pragma: no cover
    return [_summarize_member(item, registry) for item in value]


def _summarize_member(value: Any, registry: reg.Registry) -> Any:
    """One member of a collection, summarized where it is a declared kind."""
    if isinstance(value, BaseModel):
        descriptor = registry.kind_of_model(type(value))
        if descriptor is not None:
            return summarize(value, descriptor, registry, placement=None)
    return _publish(value, registry)  # pragma: no cover - every shipped member kind is declared


def layer_index(value: Any, layer: reg.LayerDescriptor, registry: reg.Registry) -> dict[str, Any]:
    """A whole layer as ``{"count": N, "identities": [...]}``: the narrowest form.

    A layer can exceed the budget even summarized, and a refusal needs a narrower
    step to offer. Ordered by :func:`ordered_layer_elements` like the other forms,
    deliberately not ``swcraft list``'s reader order: the two differ on layers with
    several element kinds (``data_types``).
    """
    identities = [
        canonical_value(getattr(element, layer.identity_field, None))
        for element in ordered_layer_elements(value, layer, registry)
    ]
    return {"count": len(identities), "identities": identities}


def collection_index(value: Any, field: reg.FieldDescriptor) -> dict[str, Any]:
    """One collection as ``{"count": N, "identities": [...]}``: the narrowest form.

    Each identity is what a ``member`` step (a ``key`` step for a map) accepts.
    """
    return {"count": _count(value), "identities": _member_identities(value, field)}


def _member_identities(value: Any, field: reg.FieldDescriptor) -> list[Any]:
    """Every member's identity inside the collection *field* declares.

    ``None`` (a collection an element does not carry) is no members, as
    :func:`_count` and :func:`summarize_collection` read it.
    """
    collection = field.collection
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [canonical_value(key) for key in value]
    if collection is None:  # pragma: no cover - `collection_index` is reached at a declared collection alone
        return [canonical_value(item) for item in value]
    return [member_identity_of(item, collection) for item in value]


def _count(value: Any) -> int:
    """How many members a collection has — ``0`` for one an element does not carry."""
    return 0 if value is None else len(value)


def read_many(
    addresses: Sequence[Address],
    form: ReadForm | None,
    answer: Callable[[ReadRequest], ReadResponse | ErrorResult],
) -> dict[str, Any]:
    """A batched ``read``: each address answered as a single read, in order, under one budget.

    *answer* serves one single read and composes a refusal into that entry's
    ``errorResult``, so an element a task is about to create is answered
    ``broken-reference`` beside the reads that succeeded.

    Raises:
        OverBudget: the whole answer measures over :data:`RESPONSE_BUDGET_WIRE_BYTES`.
            Its hints are the single reads, and its ``elements`` the number of
            answers.
    """
    requests = tuple(ReadRequest(address=address, form=form) for address in addresses)
    answers = tuple(answer(request) for request in requests)
    response = ReadManyResponse(answers=answers).to_json()
    size = measure(response)
    if size > RESPONSE_BUDGET_WIRE_BYTES:
        raise OverBudget(
            address=None,
            measured=Measured(wire_bytes=size, budget=RESPONSE_BUDGET_WIRE_BYTES, elements=len(answers)),
            hints=requests,
        )
    return response


def model_map(
    project: SwcraftProject,
    baseline: str,
    registry: reg.Registry,
    budget: int = RESPONSE_BUDGET_WIRE_BYTES,
) -> dict[str, Any]:
    """The bounded model map: every layer's count, the small layers' identities, the baseline token.

    Orientation, never enumeration: ``counts`` names every declared layer, and
    ``layers`` lists the identities of each layer holding at most
    :data:`~swcraft_tools.editing.protocol.MODEL_MAP_LIST_MAX` elements, in the order
    its ``index`` read answers. *baseline* is the caller's token of the state read.
    While the answer measures over *budget*, the largest listed layer is dropped from
    ``layers``; *budget* is a parameter so a test can force the shrink.

    Raises:
        OverBudget: the counts alone exceed *budget*. Its hints are the per-layer
            ``index`` reads, paged at ``MODEL_MAP_LIST_MAX`` where the layer holds
            more; its ``elements`` is the model's element count.
        ElementParseError: a model file under the root fails to parse.
    """
    with reg.kernel_context(registry):  # `load_snapshot` takes none and resolves the ambient one
        state = load_snapshot(str(project.resolved_model_root)).state
    identities: dict[str, list[Any]] = {}
    for layer in registry.layer_names:
        descriptor = registry.layer(layer)
        # A declared layer this state does not carry is counted 0 rather than
        # refusing the whole orientation answer.
        elements = state[descriptor.name] if descriptor.name in state else []
        identities[layer] = layer_index(elements, descriptor, registry)["identities"]
    listed = [layer for layer, names in identities.items() if len(names) <= MODEL_MAP_LIST_MAX]
    counts = {layer: len(names) for layer, names in identities.items()}
    while True:
        response = ModelMapResponse(
            baseline=baseline,
            counts=counts,
            layers={layer: tuple(identities[layer]) for layer in listed},
        ).to_json()
        size = measure(response)
        if size <= budget:
            return response
        if not listed:
            raise OverBudget(
                address=None,
                measured=Measured(wire_bytes=size, budget=budget, elements=sum(counts.values())),
                hints=tuple(
                    ReadRequest(
                        address=Address(layer=layer),
                        form="index",
                        limit=MODEL_MAP_LIST_MAX if counts[layer] > MODEL_MAP_LIST_MAX else None,
                    )
                    for layer in identities
                ),
            )
        listed.remove(max(listed, key=lambda layer: (counts[layer], layer)))


def summarize_published(
    published: Mapping[str, Any], kind: reg.ElementKindDescriptor, registry: reg.Registry
) -> dict[str, Any]:
    """:func:`summarize`'s answer, computed from an element already published by :func:`publish_element`.

    For a caller holding the published dict, not the model: a delta entry's
    ``before`` (:func:`swcraft_tools.editing.delta.summarize_removed_elements`). The
    rules are :func:`summarize`'s (:func:`_summary`); a key the dict does not carry
    is skipped.
    """
    return _summary(kind, registry, lambda field: published.get(field.name, ABSENT), lambda value: value)
