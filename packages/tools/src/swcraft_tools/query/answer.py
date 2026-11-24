"""``answer_request``: one parsed query request answered over one model state.

It refuses a request naming an undeclared layer, slices the layer, then runs the row steps the
request asks for.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from swcraft_tools.model_registry import LayerFacts, ModelRegistry, current_registry
from swcraft_tools.model_state import ModelState
from swcraft_tools.query.request import DEFAULT_QUERY_FORM, Clause, QueryError, QueryRequest, Slice, literal
from swcraft_tools.query.rows import (
    BARE_ROW_SHAPE,
    Derive,
    RowCursorNotFound,
    RowJoin,
    RowStep,
    answer_rows,
    computed_rows,
    derived_refusal,
    derived_where_refusal,
    identity_rows,
    join_refusal_prefix,
    join_shape,
    row_shape,
    split_where,
)
from swcraft_tools.query.slicing import REQUEST_SPELLING, row_cursor_refusal, slice_layer, sliced_path


def answer_request(
    state: ModelState,
    request: QueryRequest,
    *,
    vocabulary: Sequence[Any],
    derive: Derive,
    registry: ModelRegistry | None = None,
) -> Slice:
    """``swcraft query --request FILE``'s answer: the parsed request, through :func:`slice_layer`.

    A request carrying only the keys the flags also spell (``where``, ``members``, ``limit``, ``after``)
    answers exactly the slice the flags answer, being the same call; ``where`` also reaches
    ``{"field": F, "exists": false}``. ``form`` is the width, as ``--form`` is: absent or ``index`` answers
    the identities, ``full`` every declared field.

    A request carrying ``derived`` or ``join`` answers **identity rows**: every name and join path is
    checked before any value is computed, then the rows are computed by *derive*, collapsed, joined and
    paged with the whole row as the cursor (:func:`answer_rows`). *vocabulary* and *derive* come from the
    caller, so this module imports no owner of a computed value. A refusal about a joined read names the
    entry (``join 'AS': …``). A ``where`` clause naming ``derived.NAME.PATH`` is set aside from the slice
    and applied once the values exist, before the page, the step at which the protocol's ``read`` applies it;
    a joined read's derived clause filters that read's rows the same way.

    What it does not reach: **no bound**. No read-only command refuses on size, so a join's product is built
    whole however large it is; the protocol's ``read`` refuses the same request ``over-budget`` above
    ``editing.read.JOIN_ROW_CEILING``. Here the request's ``where`` and a joined read's keep it small.

    Raises:
        QueryError: its layer, or a joined read's, is not a declared layer; :func:`slice_layer` refuses it
            or a joined read; a ``derived`` name is outside *vocabulary* or computed at another set; a join
            path names no value of its declared row (:class:`JoinPathOutside`); a ``where`` over a computed
            value names one the request does not ask for or a path its row does not carry, or asks a
            set-valued one for no value (``exists: false``, ``equals: null``); or its ``after`` names no row
            of a rows answer.
    """
    registry = registry or current_registry()
    declared = [facts.name for facts in registry.layers]
    if request.layer not in declared:
        raise QueryError(_undeclared_layer_refusal(request.layer, declared))
    where, derived_where = split_where(request.where)
    if not request.derived and not request.join:
        refused_where = derived_where_refusal(derived_where, (), BARE_ROW_SHAPE)
        if refused_where is not None:
            raise QueryError(refused_where[1])
        return slice_layer(
            state,
            request.layer,
            where=request.where,
            members=request.members,
            limit=request.limit,
            after=request.after,
            registry=registry,
            spelling=REQUEST_SPELLING,
            form=request.form or DEFAULT_QUERY_FORM,
        )
    whole = slice_layer(
        state,
        request.layer,
        where=where,
        members=request.members,
        registry=registry,
        spelling=REQUEST_SPELLING,
    )
    refusal = derived_refusal(vocabulary, request.derived, request.layer, request.members)
    if refusal is not None:
        raise QueryError(refusal.message)
    shape = row_shape(_row_steps(registry.layer(request.layer), request.members, registry), request.derived, vocabulary)
    refused_where = derived_where_refusal(derived_where, request.derived, shape, vocabulary)
    if refused_where is not None:
        raise QueryError(refused_where[1])
    joined_where: list[tuple[tuple[Clause, ...], tuple[Clause, ...]]] = []
    for entry in request.join:
        joined = entry.read
        try:
            if joined.layer not in declared:
                raise QueryError(_undeclared_layer_refusal(joined.layer, declared))
            steps = _row_steps(registry.layer(joined.layer), joined.members, registry)
            joined_refusal = derived_refusal(vocabulary, joined.derived, joined.layer, joined.members)
            if joined_refusal is not None:
                raise QueryError(joined_refusal.message)
            right = row_shape(steps, joined.derived, vocabulary)
            joined_declared, joined_derived = split_where(joined.where)
            refused_where = derived_where_refusal(joined_derived, joined.derived, right, vocabulary)
            if refused_where is not None:
                raise QueryError(refused_where[1])
        except QueryError as refused:
            raise QueryError(join_refusal_prefix(entry.alias) + str(refused)) from refused
        joined_where.append((joined_declared, joined_derived))
        shape = join_shape(shape, entry.on, entry.alias, right)
    joins: list[RowJoin] = []
    for entry, (joined_declared, joined_derived) in zip(request.join, joined_where, strict=True):
        joined = entry.read
        try:
            answered = slice_layer(
                state,
                joined.layer,
                where=joined_declared,
                members=joined.members,
                registry=registry,
                spelling=REQUEST_SPELLING,
            )
        except QueryError as refused:
            raise QueryError(join_refusal_prefix(entry.alias) + str(refused)) from refused
        joins.append(
            RowJoin(
                rows=computed_rows(
                    identity_rows(answered.identities, sliced=joined.members is not None),
                    joined.derived,
                    derive=derive,
                    state=state,
                    where=joined_derived,
                ),
                on=entry.on,
                alias=entry.alias,
            )
        )
    rows = identity_rows(whole.identities, sliced=request.members is not None)
    try:
        window, page = answer_rows(
            rows,
            request.derived,
            derive=derive,
            state=state,
            joins=joins,
            limit=request.limit,
            after=request.after,
            where=derived_where,
        )
    except RowCursorNotFound as missing:
        raise QueryError(row_cursor_refusal(literal(missing.after))) from missing
    return Slice(
        layer=whole.layer,
        identity_field=whole.identity_field,
        identities=tuple(window),
        where=request.where,
        members=whole.members,
        page=page,
        derived=request.derived,
        join=request.join,
    )


def _undeclared_layer_refusal(layer: str, declared: Sequence[str]) -> str:
    """The sentence a request's layer, or a joined read's, is refused with when no layer declares it."""
    return f"the `address` names no declared layer: '{layer}' is none of {list(declared)}"


def _row_steps(facts: LayerFacts, members: str | None, registry: ModelRegistry) -> tuple[RowStep, ...]:
    """Each step of *members* as the declared row shape reads it, off :func:`sliced_path`'s resolution."""
    if members is None:
        return ()
    steps: list[RowStep] = []
    for field_facts in sliced_path(facts, members, registry, REQUEST_SPELLING):
        collection = field_facts.collection
        assert collection is not None  # `sliced_path` chose a collection at every step
        steps.append(RowStep(collection.collection_kind, collection.member_identity, collection.identity_fields))
    return tuple(steps)
