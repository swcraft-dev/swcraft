"""The renderers of query answers: text and JSON, for a slice and for a walk.

``format_query*`` render a slice's answer, ``format_related*`` the result of a graph walk.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from swcraft_tools.query.edges import DEFAULT_DIRECTION, Dependent, EdgeDetail
from swcraft_tools.query.request import (
    ABSENT,
    QUERY_FULL_SCHEMA_VERSION,
    QUERY_ROWS_SCHEMA_VERSION,
    QUERY_SCHEMA_VERSION,
    Clause,
    QueryJoin,
    QueryPage,
    Slice,
    literal,
)


def _scope_line(parts: Sequence[str]) -> str:
    """One parenthesised line naming every scope the answer was narrowed by.

    Empty when nothing was narrowed, so an unfiltered ``swcraft query LAYER`` stays a plain identity listing.
    """
    return f"({'; '.join(parts)})" if parts else ""


def _rendered_identity(identity: Any) -> str:
    """One identity as the text form prints it.

    A layer element is its declared identity; a member row reads as its address (``Owner.member`` for a list,
    ``Owner[key]`` for a map), with a dotted path's ``through`` chain between owner and member. A composite member
    identity falls back to its JSON object, the shortest spelling still replayable as ``--after``.
    """
    if isinstance(identity, str):
        return identity
    if isinstance(identity, Mapping) and "element" in identity:
        owner = "".join(f".{literal(step)}" for step in identity.get("through", ()))
        if "key" in identity:
            return f"{identity['element']}{owner}[{literal(identity['key'])}]"
        return f"{identity['element']}{owner}.{literal(identity.get('member'))}"
    return literal(identity)


def format_query_scope(
    *,
    where: Sequence[Clause] = (),
    members: str | None = None,
    page: QueryPage | None = None,
    derived: Sequence[str] = (),
    join: Sequence[QueryJoin] = (),
) -> str:
    """The scope footer of a slice: the member slice, the clauses, the computed values, the joins and the window."""
    parts: list[str] = []
    if members is not None:
        parts.append(f"members: {members}")
    parts.extend(f"where: {clause.as_text()}" for clause in where)
    if derived:
        parts.append(f"derived: {', '.join(derived)}")
    if join:
        parts.append(f"join: {', '.join(entry.alias for entry in join)}")
    if page is not None:
        parts.append(f"page: {page.returned} of {page.total}, {'truncated' if page.truncated else 'complete'}")
        if page.next is not ABSENT:
            parts.append(f"next: after={literal(page.next)}")
    return _scope_line(parts)


def format_query(result: Slice) -> str:
    """A slice as one ``layer: identity`` line per row, with a scope footer.

    Newline-terminated, and **empty for a slice that answered nothing under no scope**, as
    ``listing.format_listing`` is. A **rows** answer prints one compact JSON object per row, no ``layer:``
    prefix: JSON is the only spelling of a nested row that stays replayable as a cursor. At ``full`` each
    identity line is followed by one indented ``field: <compact JSON>`` line per declared field it does not
    already show.
    """
    if result.is_rows:
        lines = [_compact_row(row) for row in result.identities]
    else:
        lines = []
        for position, identity in enumerate(result.identities):
            lines.append(f"{result.layer}: {_rendered_identity(identity)}")
            if result.values is not None:
                lines.extend(_value_lines(result, identity, result.values[position]))
    scope = format_query_scope(
        where=result.where, members=result.members, page=result.page, derived=result.derived, join=result.join
    )
    if scope:
        if lines:
            lines.append("")
        lines.append(scope)
    return "".join(f"{line}\n" for line in lines)


def _value_lines(result: Slice, identity: Any, value: Any) -> list[str]:
    """The indented lines that follow one identity line at ``full``.

    Prints each field the identity does not show, ``null`` and declared defaults included; a map entry whose
    value is not an object prints it as ``value``; a member that is a whole scalar prints nothing more.
    """
    shown: set[str] = set()
    if isinstance(identity, Mapping) and "element" in identity:
        member = identity.get("member")
        if isinstance(member, Mapping):
            shown.update(member)
        elif result.member_identity_field is not None and "key" not in identity:
            shown.add(result.member_identity_field)
        value = value["member"]
    else:
        shown.add(result.identity_field)
    if isinstance(value, Mapping):
        return [f"  {field}: {_compact_row(item)}" for field, item in value.items() if field not in shown]
    if isinstance(identity, Mapping) and "key" in identity:
        return [f"  value: {_compact_row(value)}"]
    return []


def _compact_row(row: Any) -> str:
    """One row as the text form prints it: compact JSON, its keys in the order the row carries them."""
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _json_document(document: Mapping[str, Any]) -> str:
    """One document as this module's two ``--format json`` renderers write it.

    Raises ``ValueError`` on a non-finite float: both documents echo the caller's request, and
    ``--where value_min=NaN`` would otherwise emit a bare ``NaN``, which is not RFC 8259 JSON. The caller
    renders inside its ``try``, so this is an ordinary ``Error:`` refusal. Not reached: the text renderer,
    which prints the (empty) slice and exits 0.
    """
    return json.dumps(document, indent=2, allow_nan=False) + "\n"


def format_query_json(result: Slice) -> str:
    """The versioned, machine-readable slice document, newline-terminated.

    ``elements`` is the identity list the editing protocol's ``read`` publishes at ``form: "index"``;
    ``where``, ``members`` and ``page`` are echoed only when the request carried them. **``layer`` is the
    contract layer name** (``implementation_types``), not the singular token :func:`format_related_json` keys
    by. A **rows** answer is :data:`QUERY_ROWS_SCHEMA_VERSION`, with ``rows`` in place of ``elements`` and
    ``derived`` and ``join`` echoed when carried. A ``full`` slice is :data:`QUERY_FULL_SCHEMA_VERSION` with
    ``"form": "full"`` and whole values in ``elements``; its ``page`` stays the identities'.
    """
    full = result.values is not None
    version = QUERY_FULL_SCHEMA_VERSION if full else QUERY_SCHEMA_VERSION
    document: dict[str, Any] = {
        "schemaVersion": QUERY_ROWS_SCHEMA_VERSION if result.is_rows else version,
        "layer": result.layer,
        "identityField": result.identity_field,
    }
    if full:
        document["form"] = "full"
    if result.members is not None:
        document["members"] = result.members
    if result.where:
        document["where"] = [clause.as_json() for clause in result.where]
    if result.is_rows:
        if result.derived:
            document["derived"] = list(result.derived)
        if result.join:
            document["join"] = [entry.as_json() for entry in result.join]
        document["rows"] = list(result.identities)
    else:
        document["elements"] = list(result.values if result.values is not None else result.identities)
    if result.page is not None:
        page: dict[str, Any] = {
            "total": result.page.total,
            "returned": result.page.returned,
            "truncated": result.page.truncated,
        }
        if result.page.next is not ABSENT:
            page["next"] = result.page.next
        document["page"] = page
    return _json_document(document)


def format_related(
    name: str,
    result: Mapping[str, Sequence[Dependent]],
    *,
    direction: str = DEFAULT_DIRECTION,
    depth: int | None = None,
) -> str:
    """One walk's answer, grouped by the **matched** element's layer.

    Deliberately not ``impact.format_impact``'s bytes, so one text output is not two commands' contract; the JSON
    walk document is one (:func:`format_related_json`, which ``swcraft impact --format json`` prints too). The
    heading names the direction, an empty group says ``(none)``, the depth is a scope footer, and the layer
    headings are singular **tokens** (``interface``), as ``swcraft impact`` prints them.
    """
    if not result:
        return f"No element named '{name}' in any layer.\n"
    heading = "Dependents" if direction == DEFAULT_DIRECTION else "Dependencies"
    lines = [f"{heading} of '{name}':"]
    for layer, rows in result.items():
        lines.extend(("", f"{layer}:"))
        if not rows:
            lines.append("  (none)")
            continue
        lines.extend(f"  {row.ref.layer} {row.ref.name}{f' ({row.detail})' if row.detail else ''}" for row in rows)
    scope = _scope_line([f"depth: {depth}"] if depth is not None else [])
    if scope:
        lines.extend(("", scope))
    return "".join(f"{line}\n" for line in lines)


def _detail_json(detail: EdgeDetail) -> dict[str, str]:
    """One ``details`` entry: ``kind`` and ``value``, ``owner`` when qualified, ``ownerLayer`` when that too."""
    entry = {"kind": detail.kind, "value": detail.value}
    if detail.owner is not None:
        entry["owner"] = detail.owner
    if detail.owner_layer is not None:
        entry["ownerLayer"] = detail.owner_layer
    return entry


def format_related_json(
    name: str,
    result: Mapping[str, Sequence[Dependent]],
    *,
    direction: str = DEFAULT_DIRECTION,
    depth: int | None = None,
) -> str:
    """The versioned, machine-readable walk document, newline-terminated.

    ``matched`` is keyed by the **matched** element's layer (a bare name matches, and is walked, in every layer
    that declares it). Each row carries the reached element's layer token, its name, ``detail`` (the rendered
    parenthesis, ``""`` for none) and ``details``, the same edges as ``{"kind", "value"}`` objects in walk order.
    ``kind`` is a key of :data:`_DETAIL_RENDERING` (``port``, ``endpoint``, ``runnable``, ``member``, ``step``,
    ``budget``, ``core``, ``access``); ``value`` is always a string (a budget ``"512"``, an endpoint
    ``owner.port``). ``owner`` is added only when an equal value reached the row from another parent,
    and ``ownerLayer`` only when two such parents share a name across layers, so a consumer never parses
    ``detail``.

    It is the one JSON shape of a walk: ``swcraft query --dependents/--deps`` and ``swcraft impact`` both print
    it. Its keys are singular layer *tokens* (``interface``, ``data_type``), not the contract layer names
    :func:`format_query_json` writes; one :data:`QUERY_SCHEMA_VERSION` covers both vocabularies.
    """
    document = {
        "schemaVersion": QUERY_SCHEMA_VERSION,
        "name": name,
        "direction": direction,
        "depth": depth,
        "matched": {
            layer: [
                {
                    "layer": row.ref.layer,
                    "name": row.ref.name,
                    "detail": row.detail,
                    "details": [_detail_json(detail) for detail in row.details],
                }
                for row in rows
            ]
            for layer, rows in result.items()
        },
    }
    return _json_document(document)
