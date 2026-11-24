"""Transitive impact analysis over a single ModelSnapshot.

The graph and its walks are :mod:`swcraft_tools.query`'s; this module is the snapshot-shaped
surface over them and renders the results: ``build_reference_graph``, ``impact_of``,
``format_impact``, ``format_impact_summary``, the page over a rendered report
(:func:`page_impact`, :func:`format_impact_scope`) and the count (:func:`count_impact`).

``build_reference_graph(snapshot)`` maps ``referenced_ref -> {dependent_ref: details}``, every
edge oriented **target -> dependent**: the node is the element that was named, the dependent the
top-level element that named it. ``impact_of(snapshot, name)`` matches the bare name in every
declared layer (there is no ``--type`` flag) and walks from each match without looping on cycles.

``Port`` and ``Runnable`` are attributes, not graph nodes: they only appear as a detail of a
``Dependent``, never as their own ``ElementRef`` layer. A reference declared on a *member* kind
(``field:swc.port.interface``) yields an edge to the member's owning top-level element, and the
member is what the detail names; resource budgets follow the same convention.

Two orders are observable output. The **layer grouping** is ``registry.display_order``, also the
order ``format_impact_summary`` regroups the dependents in; it is presentation only. The
**per-node dependent** order is the engine's edge insertion order.

**What this module does not decide.** It reports what an element *says*: a port naming an
undeclared interface, a budget key naming no memory, a task naming no partition all yield edges; the
findings are the validator's. Identity-key uniqueness is ``differ.build_key_map``'s refusal.

**What it does not reach.** A field the contract declares neither a ``reference`` nor a
``relation`` on is invisible to the traversal, however much it looks like a cross-layer name;
nothing in this module or in ``query`` can detect such a field.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from swcraft_tools.model_registry import ModelRegistry, current_registry
from swcraft_tools.query import (
    DEFAULT_DIRECTION,
    DIRECTIONS,
    AdjacencyMap,
    Dependent,
    ElementRef,
    build_graphs,
    related,
)
from swcraft_tools.query.edges import check_limit, layer_token
from swcraft_tools.snapshot import ModelSnapshot

__all__ = [
    "DEFAULT_DIRECTION",
    "DIRECTIONS",
    "Dependent",
    "ElementRef",
    "ImpactCursorNotFound",
    "ImpactPage",
    "build_reference_graph",
    "count_impact",
    "format_impact",
    "format_impact_scope",
    "format_impact_summary",
    "impact_cursor",
    "impact_of",
    "page_impact",
]


def build_reference_graph(snapshot: ModelSnapshot) -> AdjacencyMap:
    """Build the reverse reference graph for one snapshot.

    Reads ``snapshot.state``, so a layer the binding declares no attribute for is still walked.

    Raises:
        ValueError: two elements in the same layer share an identity key,
            naming the layer and the duplicated key.
    """
    return build_graphs(snapshot.state).reverse


def impact_of(
    snapshot: ModelSnapshot,
    name: str,
    *,
    direction: str = DEFAULT_DIRECTION,
    depth: int | None = None,
) -> dict[str, list[Dependent]]:
    """Return the elements related to every element named `name`.

    Delegates to :func:`swcraft_tools.query.related`; at its defaults, the unbounded reverse walk.
    Each :class:`~swcraft_tools.query.Dependent` carries every detail of every edge that reached
    it; what that does not reach is stated there.

    Args:
        direction: ``"dependents"`` (the default) — what breaks if this element
            changes — or ``"dependencies"``, the same edges read the other way.
        depth: ``None`` (the default) for the transitive walk; a positive
            integer for the bounded breadth-first one. Neither walk claims a
            minimal hop count.

    Returns:
        ``{layer: [Dependent, ...]}`` for every layer with a matching
        element (a matched element with no dependents contributes an empty
        list, not an absent key); an empty dict if no layer has an element
        named `name`.

    Raises:
        ValueError: *direction* is not one of :data:`DIRECTIONS`; *depth* is
            not a whole number or is below 1; or two elements in the same layer
            share an identity key.
    """
    return related(snapshot.state, name, direction, depth)


def format_impact(name: str, result: dict[str, list[Dependent]]) -> str:
    """Render `impact_of`'s result as a grouped-by-layer, human-readable report.

    Returns a "not found" message (not an error) when *result* is empty. The parenthesis on a
    dependent's line carries every detail of the edges that reached it: ``(via ports pA, pB, pC)``;
    a connector names, owner first, the endpoint on each element the walk reached it from. What
    "every" does not reach is the walk's, stated on :class:`~swcraft_tools.query.Dependent`.
    """
    if not result:
        return f"Element '{name}' not found in any layer."

    lines = [f"Impact of '{name}':"]
    for layer, deps in result.items():
        lines.append(f"\n{layer}:")
        if not deps:
            lines.append("  (no dependents)")
            continue
        for dep in deps:
            suffix = f" ({dep.detail})" if dep.detail else ""
            lines.append(f"  {dep.ref.layer} {dep.ref.name}{suffix}")

    return "\n".join(lines)


def format_impact_summary(name: str, result: dict[str, list[Dependent]]) -> str:
    """Render `impact_of`'s result as counts and identities, the narrow form.

    The same dependents as :func:`format_impact`, regrouped by the dependent's own layer in the
    declared display order, each group written as its count and its identities; it drops
    ``Dependent.details`` and truncates nothing. The outer grouping and the "not found" line are
    :func:`format_impact`'s.

    What it does not reach: it narrows what is said about each dependent, never how many there are,
    so on a large model the identities alone can still exceed the server's budget, and below a
    handful of dependents it is not smaller at all. A report too large for either form is bounded by
    ``impact_of(..., depth=N)`` or paged by :func:`page_impact`; :func:`count_impact` states its size.
    """
    if not result:
        return format_impact(name, result)

    lines = [f"Impact of '{name}' (summary):"]
    for layer, deps in result.items():
        lines.append(f"\n{layer}:")
        if not deps:
            lines.append("  (no dependents)")
            continue
        grouped: dict[str, list[str]] = {}
        for dep in deps:
            grouped.setdefault(dep.ref.layer, []).append(dep.ref.name)
        for token in _in_display_order(list(grouped)):
            names = grouped[token]
            lines.append(f"  {token} ({len(names)}): {', '.join(names)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The page — a window over the rendered report, not over the graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactPage:
    """What a paged report says about the set it is a window into.

    * ``total`` — the whole listing, counted before the window;
    * ``returned`` — the rows this answer carries;
    * ``truncated`` — ``returned < total``, which is *not* ``next``'s presence: the last page of a
      walk is truncated and has no ``next``;
    * ``next`` — the cursor to continue past, ``None`` on the last page (an impact cursor is always
      a composed string, so ``None`` needs no ``ABSENT`` sentinel as a query cursor does);
    * ``kinds`` — the whole listing's rows counted per dependent kind, in
      :func:`format_impact_summary`'s order; the counts sum to ``total``. Empty means no counts to
      print.
    """

    total: int
    returned: int
    truncated: bool
    next: str | None = None
    kinds: tuple[tuple[str, int], ...] = ()


class ImpactCursorNotFound(LookupError):
    """An ``after`` cursor no row of this report carries.

    Met in normal operation: a keyset walk crossing an edit that removed the row the previous page
    ended on.
    """


def impact_cursor(matched_layer: str, ref: ElementRef) -> str:
    """The cursor for one row of a report: ``"<matched>/<layer>:<name>"``.

    The matched layer makes the cursor unique: a bare name matches in several layers and each match
    is walked separately, so one element can appear once per match. Cursors are compared, never
    parsed, so an identity carrying ``/`` or ``:`` is not ambiguous.
    """
    return f"{matched_layer}/{ref.layer}:{ref.name}"


def page_impact(
    result: dict[str, list[Dependent]],
    limit: int,
    after: str | None = None,
) -> tuple[dict[str, list[Dependent]], ImpactPage]:
    """Window *result*'s flattened listing, keyset-style, and report the whole.

    The listing is every row of every matched layer, in :func:`format_impact`'s order; the window is
    the *limit* rows following *after*. A keyset and not an offset, because the model is re-walked on
    every call and an offset into a shorter report skips a row silently. A matched layer with no rows
    keeps its section, since ``(no dependents)`` is that match's whole answer. ``kinds`` counts the
    listing, not the window.

    Args:
        result: :func:`impact_of`'s answer.
        limit: the page size, at least 1.
        after: a cursor a previous page published as ``next``, or ``None`` to
            start at the beginning.

    Raises:
        ValueError: *limit* is not a whole number (a ``bool`` and a ``float``
            are both refused) or is below 1.
        ImpactCursorNotFound: no row of this report carries *after*.
    """
    check_limit(limit)

    flat = [(layer, dep) for layer, deps in result.items() for dep in deps]
    total = len(flat)

    start = 0
    if after is not None:
        cursors = [impact_cursor(layer, dep.ref) for layer, dep in flat]
        if after not in cursors:
            raise ImpactCursorNotFound(
                f"no row of this report carries the cursor '{after}'; a cursor is one this "
                "report published as `next`, and a walk crossing an edit that removed that row "
                "restarts from the beginning"
            )
        start = cursors.index(after) + 1

    window = flat[start : start + limit]
    carried = {layer for layer, _ in window}
    windowed: dict[str, list[Dependent]] = {layer: [] for layer, deps in result.items() if layer in carried or not deps}
    for layer, dep in window:
        windowed[layer].append(dep)

    kinds = _count_kinds(dep for _, dep in flat)

    following = start + len(window) < total
    return windowed, ImpactPage(
        total=total,
        returned=len(window),
        truncated=len(window) < total,
        next=impact_cursor(window[-1][0], window[-1][1].ref) if following else None,
        kinds=kinds,
    )


def count_impact(result: dict[str, list[Dependent]]) -> dict[str, dict[str, object]]:
    """*result* counted: per matched layer, the total of its rows and their count per kind.

    A projection of :func:`impact_of`'s answer, never a second walk:
    ``{matched_layer: {"total": n, "by_kind": {token: n}}}``, ``by_kind`` in :class:`ImpactPage`'s
    ``kinds`` order. A matched element with no dependents is ``{"total": 0, "by_kind": {}}``; an
    empty *result* counts to ``{}``.

    What it does not reach: it counts the walk it is given, so only a ``depth`` bounds the work;
    only the size of the answer is bounded, by the number of declared layers.
    """
    return {layer: {"total": len(deps), "by_kind": dict(_count_kinds(deps))} for layer, deps in result.items()}


def _count_kinds(dependents: Iterable[Dependent]) -> tuple[tuple[str, int], ...]:
    """*dependents* counted per dependent kind, in :func:`_in_display_order`."""
    counts: dict[str, int] = {}
    for dep in dependents:
        counts[dep.ref.layer] = counts.get(dep.ref.layer, 0) + 1
    return tuple((token, counts[token]) for token in _in_display_order(list(counts)))


def format_impact_scope(
    *,
    direction: str = DEFAULT_DIRECTION,
    depth: int | None = None,
    page: ImpactPage | None = None,
) -> str:
    """One line naming every scope the answer was **not** computed at its default under.

    A footer, so the report's header stays unchanged; empty when the direction, the depth and the
    page are all at their defaults. A truncated page also names the whole report's count per
    dependent kind (``in all: swc 30, connector 1457``), so a window is not read as a sample of the
    whole.
    """
    parts: list[str] = []
    if direction != DEFAULT_DIRECTION:
        parts.append(f"direction: {direction}")
    if depth is not None:
        parts.append(f"depth: {depth}")
    if page is not None:
        parts.append(f"page: {page.returned} of {page.total}, {'truncated' if page.truncated else 'complete'}")
        if page.truncated and page.kinds:
            parts.append("in all: " + ", ".join(f"{token} {count}" for token, count in page.kinds))
        if page.next is not None:
            parts.append(f'next: after="{page.next}"')
    return f"({'; '.join(parts)})" if parts else ""


def _in_display_order(tokens: list[str]) -> list[str]:
    """*tokens* sorted in the declared display order (:func:`_display_tokens`).

    An undeclared token ranks last, then by name, so it is never silently absent from a count.
    """
    rank = {token: index for index, token in enumerate(_display_tokens(current_registry()))}
    return sorted(tokens, key=lambda t: (rank.get(t, len(rank)), t))


def _display_tokens(registry: ModelRegistry) -> tuple[str, ...]:
    """Every declared layer's public token, in ``registry.display_order``."""
    return tuple(layer_token(registry.layer(layer)) for layer in registry.display_order)
