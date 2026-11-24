"""The model query kernel: a read-only, layer-generic view of the model that the CLI and the MCP surface read.

It reads only ``state.layers`` and the contract's declarations, so it names no layer. The modules, in layer
order (a module imports only those listed before it):

- ``edges``: the declared ``reference``/``relation`` edges, their adjacency maps and the walks over them;
- ``request``: the request grammar, its refusals (:class:`QueryError`) and the layer's read order;
- ``projection``: the ``full`` width of one element or member;
- ``slicing``: a layer's identities filtered, sliced by a collection and windowed;
- ``rows``: identity rows, computed values and joins;
- ``answer``: :func:`answer_request`, which answers one parsed request;
- ``formatting``: the text and JSON that ``swcraft query`` prints.

The kernel reports what an element says; findings about it (a dangling reference, a duplicate identity) are
the validator's. A field declared neither a ``reference`` nor a ``relation`` is invisible to the walks.

This facade only re-exports, so a name is patched on the module that looks it up, never here.
"""

from __future__ import annotations

from swcraft_tools.query.answer import answer_request
from swcraft_tools.query.edges import (
    DEFAULT_DIRECTION,
    DIRECTIONS,
    AdjacencyMap,
    Dependent,
    EdgeContext,
    EdgeDetail,
    ElementRef,
    Graphs,
    build_graphs,
    dependencies,
    dependents,
    integral,
    related,
)
from swcraft_tools.query.formatting import (
    format_query,
    format_query_json,
    format_query_scope,
    format_related,
    format_related_json,
)
from swcraft_tools.query.projection import full_element, full_value
from swcraft_tools.query.request import (
    ABSENT,
    JOIN_ALIAS_PATTERN,
    JOIN_MAX_ENTRIES,
    LAYER_NAME_PATTERN,
    MEMBERS_PATH_PATTERN,
    QUERY_FORMS,
    QUERY_ROWS_SCHEMA_VERSION,
    QUERY_SCHEMA_VERSION,
    RESERVED_JOIN_ALIASES,
    Clause,
    JoinedQuery,
    QueryError,
    QueryJoin,
    QueryPage,
    QueryRequest,
    Slice,
    holds_no_value,
    join_entries_refusal,
    ordered_elements,
    parse_request,
    values_equal,
)
from swcraft_tools.query.rows import (
    BARE_ROW_SHAPE,
    DERIVED_CLAUSE_PREFIX,
    SET_VALUED_ROWS,
    Derive,
    DerivedRefusal,
    JoinBound,
    JoinPathOutside,
    JoinProduct,
    RowCursorNotFound,
    RowJoin,
    RowStep,
    answer_rows,
    collapse_rows,
    computed_rows,
    derived_refusal,
    derived_where_refusal,
    identity_rows,
    is_derived_clause,
    join_refusal_prefix,
    join_rows,
    join_shape,
    page_rows,
    row_shape,
    rows_matching,
    split_where,
)
from swcraft_tools.query.slicing import row_cursor_refusal, slice_layer

__all__ = [
    "ABSENT",
    "AdjacencyMap",
    "BARE_ROW_SHAPE",
    "Clause",
    "DEFAULT_DIRECTION",
    "DERIVED_CLAUSE_PREFIX",
    "DIRECTIONS",
    "Dependent",
    "Derive",
    "DerivedRefusal",
    "EdgeContext",
    "EdgeDetail",
    "ElementRef",
    "Graphs",
    "JOIN_ALIAS_PATTERN",
    "JOIN_MAX_ENTRIES",
    "JoinBound",
    "JoinPathOutside",
    "JoinProduct",
    "JoinedQuery",
    "LAYER_NAME_PATTERN",
    "MEMBERS_PATH_PATTERN",
    "QUERY_FORMS",
    "QUERY_ROWS_SCHEMA_VERSION",
    "QUERY_SCHEMA_VERSION",
    "QueryError",
    "QueryJoin",
    "QueryPage",
    "QueryRequest",
    "RESERVED_JOIN_ALIASES",
    "RowCursorNotFound",
    "RowJoin",
    "RowStep",
    "SET_VALUED_ROWS",
    "Slice",
    "answer_request",
    "answer_rows",
    "build_graphs",
    "collapse_rows",
    "computed_rows",
    "dependencies",
    "dependents",
    "derived_refusal",
    "derived_where_refusal",
    "format_query",
    "format_query_json",
    "format_query_scope",
    "format_related",
    "format_related_json",
    "full_element",
    "full_value",
    "holds_no_value",
    "identity_rows",
    "integral",
    "is_derived_clause",
    "join_entries_refusal",
    "join_refusal_prefix",
    "join_rows",
    "join_shape",
    "ordered_elements",
    "page_rows",
    "parse_request",
    "related",
    "row_cursor_refusal",
    "row_shape",
    "rows_matching",
    "slice_layer",
    "split_where",
    "values_equal",
]
